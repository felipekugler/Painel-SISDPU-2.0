"""Sincronizador SISDPU -> PAJs/<PAJ>/.

Orquestra:
1. Login no SISDPU (Playwright)
2. Lista caixa de entrada
3. Para cada PAJ: abre detalhamento, extrai movs, baixa PDFs linkados e OCRa
4. Monta sisdpu.txt no formato conhecido e metadata.json via ingestao.parser
5. Remove PROMPT_MAX.md (forca regeneracao no proximo acesso)

Versao enxuta do preparar_pajs.py original, SEM blocos DataJud/TNU/STJ — esses
ficariam redundantes para PAJs de 1a instancia DPU-AP.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from collections.abc import Callable

from config import (
    MAX_ANEXOS_POR_PAJ,
    PAJS_DIR,
    RATE_LIMIT_SISDPU,
    TIMEOUT_OCR_POR_PAGINA_SEG,
    TIMEOUT_TOTAL,
)
from ingestao import ocr, parser, sisdpu_client
import contextlib


# Formato canonico: AAAA/UUU-NNNNN (5 digitos no numero). Alinhado com
# services.paj_service.PAJ_NORM_REGEX — toda a app trabalha com largura fixa,
# entao manter aqui tambem evita aceitar formato que as rotas FastAPI rejeitariam.
PAJ_REGEX = re.compile(r"^(\d{4})/(\d{3})-(\d{5})$")


def _decompor_paj(paj: str) -> tuple[str, str, str] | None:
    """'2026/044-00311' -> ('2026', '044', '00311'). None se invalido."""
    m = PAJ_REGEX.match(paj.strip())
    if not m:
        return None
    ano = m.group(1)
    unidade = m.group(2)
    numero = m.group(3)
    return ano, unidade, numero


def _normalizar_paj(paj: str) -> str:
    """'2026/044-00311' -> 'PAJ-2026-044-00311'."""
    return "PAJ-" + paj.replace("/", "-")


def _iso_para_br(iso: str) -> str:
    """'2023-06-22' -> '22/06/2023'. Aceita ISO datetime tambem ('2023-06-22T...').
    Devolve string vazia se entrada vazia; devolve a entrada original se nao
    bater o formato esperado (defensivo)."""
    if not iso:
        return ""
    s = str(iso)[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    return str(iso)


def _remover_html(texto: str) -> str:
    """Remove tags HTML de um texto. Preserva espaçamento entre tags."""
    if not texto:
        return ""
    # Remove tags HTML, preservando espaços entre elementos
    sem_html = re.sub(r'<[^>]+>', '', texto)
    # Normaliza múltiplos espaços/quebras de linha em um único espaço
    normalizado = re.sub(r'\s+', ' ', sem_html)
    return normalizado.strip()


def _montar_sisdpu_txt(item_caixa: dict, det: dict) -> str:
    """Monta sisdpu.txt no mesmo formato dos arquivos existentes.

    Formato:
        PAJ: <id>
        Assistido: <nome>
        Status: <status>
        Pretensão: <pretensao>
        Data de Abertura: <dd/mm/yyyy>
        Ofício: <oficio>

        MOVIMENTAÇÕES RELEVANTES:
        [data] <descricao>
        ...
    """
    linhas: list[str] = []

    paj_id = item_caixa.get("paj") or det.get("paj", "")
    assistido = det.get("assistido") or item_caixa.get("assistido") or ""
    status = det.get("status_paj") or "ATIVO"
    pretensao = det.get("pretensao", "")
    data_abertura = det.get("data_abertura", "")
    # Oficio atual: SEMPRE da caixa de entrada (parenteses na linha do PAJ). A pagina
    # de detalhamento tem a palavra "Ofício" em varios lugares (cabecalho + textos de
    # movimentacao mencionando oficios expedidos), e o regex de captura pega a primeira
    # ocorrencia — as vezes e' descricao de movimentacao, nao o oficio atual do PAJ.
    # A caixa, em contraste, tem "( A. 03º OFÍCIO GERAL )" limpo e inequivoco.
    # Fallback para det.oficio so se a caixa vier vazia E o det.oficio nao for lixo.
    oficio_caixa = (item_caixa.get("oficio") or "").strip()
    oficio_det = (det.get("oficio") or "").strip()
    if oficio_caixa:
        oficio = oficio_caixa
    elif oficio_det and not oficio_det.lower().startswith("anterior:"):
        oficio = oficio_det
    else:
        oficio = ""
    processos_judiciais = det.get("processos_judiciais") or []
    processo_judicial = det.get("processo_judicial", "")
    juizo = det.get("juizo", "")
    foro_detalhado = (det.get("foro_detalhado") or "").strip()
    decurso = (det.get("decurso") or "").strip()

    linhas.append(f"PAJ: {paj_id}")
    if assistido:
        linhas.append(f"Assistido: {assistido}")
    if status:
        linhas.append(f"Status: {status}")
    if pretensao:
        linhas.append(f"Pretensão: {pretensao}")
    if data_abertura:
        linhas.append(f"Data de Abertura: {data_abertura}")
    if decurso:
        linhas.append(f"Decurso: {decurso}")
    if oficio:
        linhas.append(f"Ofício: {oficio}")
    if processos_judiciais:
        # Lista TODOS os processos vinculados (o PAJ pode ter mais de um) — fiel
        # ao que está cadastrado, não só o último (comportamento antigo).
        for _p in processos_judiciais:
            _num = (_p.get("processo_judicial") or "").strip()
            if not _num:
                continue
            _info_extra = foro_detalhado or (_p.get("juizo") or "").strip()
            _sufixo = f" ({_info_extra})" if _info_extra else ""
            linhas.append(f"PROCESSO JUDICIAL VINCULADO: {_num}{_sufixo}")
    elif processo_judicial:
        # Caminho antigo (ex.: extração via busca global, que só produz um valor).
        # Foro detalhado tem precedencia sobre juizo na anotacao entre
        # parenteses — e' o que o paj_detail.html consome (meta.foro_detalhado).
        info_extra = foro_detalhado or juizo
        sufixo = f" ({info_extra})" if info_extra else ""
        linhas.append(f"PROCESSO JUDICIAL VINCULADO: {processo_judicial}{sufixo}")

    narrativa_html = (det.get("narrativa") or "").strip()
    if narrativa_html:
        # Remover tags HTML para arquivo de texto (sisdpu.txt vai para IA)
        # metadata.json mantém o HTML original para renderização no painel
        narrativa_texto = _remover_html(narrativa_html)
        linhas.append("")
        linhas.append("NARRATIVA:")
        linhas.append(narrativa_texto)

    linhas.append("")
    linhas.append("MOVIMENTAÇÕES:")

    movs = det.get("movimentacoes", []) or []
    # Ordenar em ordem crescente por seq
    movs_ordenadas = sorted(movs, key=lambda x: int(str(x.get("seq", 0) or 0).strip()) if str(x.get("seq", 0) or 0).strip().isdigit() else 0)
    for m in movs_ordenadas:
        seq = (str(m.get("seq", "") or "")).strip()
        data_orig = (m.get("data_original") or "").strip()
        fases = (m.get("fases") or "").strip()
        descricao = (m.get("descricao") or "").strip()
        descricao_retorno = (m.get("descricao_retorno") or "").strip()

        # Formata cada movimentação com quebras de linha para legibilidade
        if seq:
            linhas.append("")
            linhas.append(f"[Seq {seq}] {data_orig}")
            if fases:
                linhas.append(f"Fase: {fases}")
            # Se for "Atendimento de retorno" com conteúdo expandido, usa descricao_retorno
            descricao_final = descricao_retorno if ("Atendimento de retorno" in fases and descricao_retorno) else descricao
            if descricao_final:
                linhas.append(f"Descrição: {descricao_final}")

    return "\n".join(linhas) + "\n"


_NOME_VALIDO_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


def _sanitizar_nome(nome: str) -> str:
    """Normaliza nome de arquivo pra Windows: remove caracteres invalidos,
    corta acentos, limita tamanho."""
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", nome or "")
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    limpo = _NOME_VALIDO_RE.sub("_", sem_acento).strip(" ._")
    if len(limpo) > 120:
        stem, dot, ext = limpo.rpartition(".")
        if dot and len(ext) <= 5:  # noqa: SIM108 - ternario fica menos legivel aqui
            limpo = stem[: 115 - len(ext)] + "." + ext
        else:
            limpo = limpo[:120]
    return limpo or "anexo"


def _nome_anexo(idx: int, descricao: str, arquivo: str) -> str:
    """Nome determinista pro anexo baixado: <seq>_<descricao-slug>[_<nome-original>].

    Indice no inicio garante ordenacao estavel; descricao + nome_original ajudam
    a reconhecer visualmente na pasta. A EXTENSAO vai ser apendida depois pelo
    cliente a partir do `download.suggested_filename` (Content-Disposition).

    Quando `arquivo` ja traz extensao conhecida (.pdf/.docx/.jpg/etc), ela e'
    removida aqui pra nao duplicar — o cliente anexa a real no final.
    """
    prefixo = f"{idx:02d}"
    desc_slug = _sanitizar_nome(descricao)[:40].strip("_") if descricao else ""
    # Remove qualquer extensao conhecida do nome_orig pra evitar "arquivo.pdf.pdf"
    nome_orig = _sanitizar_nome(arquivo) if arquivo else ""
    if nome_orig:
        stem, dot, ext = nome_orig.rpartition(".")
        if dot and ext.lower() in {
            "pdf",
            "docx",
            "doc",
            "jpg",
            "jpeg",
            "png",
            "gif",
            "xlsx",
            "xls",
            "txt",
            "zip",
        }:
            nome_orig = stem
    partes = [prefixo]
    if desc_slug:
        partes.append(desc_slug)
    if nome_orig:
        partes.append(nome_orig)
    nome = "_".join(partes)
    return nome


_MAGIC_BYTES = [
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),  # tambem e' a assinatura de docx/xlsx/pptx
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"%!PS", ".ps"),
]


def _detectar_ext_por_magic(path: Path) -> str | None:
    """Le os primeiros bytes e devolve a extensao real (com ponto).

    Para arquivos zip-based (docx/xlsx/pptx/zip), faz distincao adicional
    procurando pela estrutura interna — aqui simplificado: se [Content_Types].xml
    estiver nos primeiros 2KB, consideramos docx.
    """
    try:
        head = path.read_bytes()[:2048]
    except Exception:
        return None
    for sig, ext in _MAGIC_BYTES:
        if head.startswith(sig):
            if ext == ".zip":
                # Heuristica: docx/xlsx tem "word/" ou "xl/" perto do comeco
                if b"word/" in head:
                    return ".docx"
                if b"xl/" in head:
                    return ".xlsx"
                if b"ppt/" in head:
                    return ".pptx"
                return ".zip"
            return ext
    return None


# Extensoes "conhecidas" que nao precisam de rebatizacao por magic bytes
_EXTS_LEGITIMAS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".webp",
    ".txt",
    ".json",
    ".md",
    ".zip",
    ".rar",
    ".7z",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".html",
    ".xml",
    ".csv",
}


def rebatizar_bins_existentes(pasta_pecas: Path, log: Callable[[str], None]) -> int:
    """Rebatiza arquivos com extensao suspeita (.bin, sem ext, ou sufixo bizarro
    como .3100) via magic bytes. Retorna quantos foram renomeados.

    Critério: um arquivo entra em rebatizacao se seu suffix.lower() NAO esta
    em `_EXTS_LEGITIMAS`. Isso cobre:
      - .bin (placeholder legado)
      - sem extensao (ex: "arquivo_sem_ponto")
      - pseudo-extensoes (ex: "nome_1009620-02.2019.4.01.3100" — suffix=".3100")
    """
    renomeados = 0
    if not pasta_pecas.exists():
        return 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file():
            continue
        # Pula companions OCR (.txt tem stem=mesmo do PDF, nao deve ser rebatizado)
        if f.suffix.lower() == ".txt":
            continue
        if f.suffix.lower() in _EXTS_LEGITIMAS:
            continue
        ext_real = _detectar_ext_por_magic(f)
        if not ext_real:
            continue
        # Stem preserva o nome sem o suffix atual (ex: "arquivo.3100" -> "arquivo")
        # Mas se o suffix "falso" era parte do nome (tipo numero de processo),
        # melhor preservar o nome completo. Regra: se suffix tem so digitos ou
        # parece numero de processo, mantem; senao, substitui.
        suffix_atual = f.suffix
        if suffix_atual and (suffix_atual[1:].isdigit() or len(suffix_atual) > 5):
            # Trata suffix como parte do stem (ex: "...3100" deve virar "...3100.pdf")
            novo = f.parent / (f.name + ext_real)
        else:
            novo = f.with_suffix(ext_real)
        if novo.exists():
            log(f"    [bin] conflito ao renomear {f.name} -> {novo.name} (já existe)")
            continue
        try:
            f.rename(novo)
            log(f"    [bin] {f.name} -> {novo.name}")
            renomeados += 1
        except Exception as e:
            log(f"    [bin] falha ao renomear {f.name}: {type(e).__name__}: {e}")
    return renomeados


def limpar_ocr_vazios(pasta_pecas: Path, log: Callable[[str], None]) -> int:
    """Remove .txt companions vazios ou com placeholder '[OCR indisponivel'.

    Nao apaga nada que nao seja .txt. Retorna quantos foram removidos.
    """
    removidos = 0
    if not pasta_pecas.exists():
        return 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".txt":
            continue
        try:
            tam = f.stat().st_size
            if tam == 0:
                f.unlink()
                removidos += 1
                log(f"    [ocr] removido .txt vazio: {f.name}")
                continue
            # Le so os primeiros bytes pra conferir placeholder
            head = f.read_bytes()[:200].decode("utf-8", errors="ignore")
            if "[OCR indisponivel" in head and tam < 100:
                f.unlink()
                removidos += 1
                log(f"    [ocr] removido placeholder: {f.name}")
        except Exception as e:
            log(f"    [ocr] falha ao avaliar {f.name}: {type(e).__name__}: {e}")
    return removidos


def _parse_data_arquivo(s: str):
    """Converte 'DD/MM/YYYY' do campo data do anexo SISDPU em date.

    Retorna `date.min` quando nao consegue interpretar — itens nessa situacao
    ficam no fim do sort decrescente (sao os menos prioritarios).
    """
    import datetime as _dt

    try:
        return _dt.datetime.strptime((s or "").strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError, TypeError):
        return _dt.date.min


async def _baixar_anexos_sisdpu(
    pasta_pecas: Path,
    log: Callable[[str], None],
    limite: int,
    deve_cancelar: Callable[[], bool] | None = None,
    filtro_data_min=None,
    seq_por_file_id: dict[str, int] | None = None,
    rodar_ocr: bool = False,
) -> dict:
    """Abre o dialogo 'Arquivos' do PAJ atual e baixa os anexos listados.

    PRE-CONDICAO: a page do Playwright ja deve estar no detalhamento do PAJ
    (chamar `movimentacoes_paj(...)` antes).

    `seq_por_file_id`: repassado a `sisdpu_client.baixar_todos_arquivos` pra
    nomear os arquivos com a seq REAL da movimentacao (vinda da extracao por
    seq), nao o indice desta listagem.

    Comportamento:
      - Lista TODAS as tabelas de anexos da pagina (multi-categoria).
      - Ordena os itens por DATA decrescente (mais recentes primeiro).
      - Se `filtro_data_min` (date) for informado, baixa SO os com data >= filtro
        — sem aplicar `limite` (modo "completar a partir de uma data").
      - Caso contrario, aplica `limite`: baixa os `limite` mais recentes; se
        houver mais que isso no SISDPU, registra overflow no resultado.

    Retorna dict:
      {
        "total_no_sisdpu": int,         # anexos visiveis na pagina
        "baixados": int,
        "ocr_ok": int,
        "overflow": dict | None         # presente se total > limite (e nao houve filtro)
            {
              "total_no_sisdpu": int,
              "baixados": int,
              "data_min_baixada": "YYYY-MM-DD",   # data mais antiga que entrou
              "data_min_disponivel": "YYYY-MM-DD",# data mais antiga existente
              "limite_aplicado": int,
            }
      }
    """
    pasta_pecas.mkdir(parents=True, exist_ok=True)

    # Legado: renomeia .bin pra extensao real detectada por magic bytes.
    # Isso cobre anexos baixados por versoes anteriores do sincronizador que
    # salvaram tudo como .bin.
    try:
        n_renomeados = rebatizar_bins_existentes(pasta_pecas, log)
        if n_renomeados:
            log(f"    [bin] {n_renomeados} arquivo(s) .bin rebatizados")
    except Exception as e:
        log(f"    [bin] erro ao rebatizar legados: {type(e).__name__}: {e}")

    try:
        resultado = await sisdpu_client.listar_arquivos_paj()
    except Exception as e:
        log(f"  [anexos] erro ao listar: {type(e).__name__}: {e}")
        return {"total_no_sisdpu": 0, "baixados": 0, "ocr_ok": 0, "overflow": None}

    # Nova assinatura: {"ok": bool, "itens": [...], "diag": {...}}
    lista = resultado.get("itens", []) if isinstance(resultado, dict) else (resultado or [])
    diag = resultado.get("diag", {}) if isinstance(resultado, dict) else {}

    total = len(lista)
    if total == 0:
        log("  [anexos] nenhum anexo na tela 'Arquivos'")
        # Diagnostico detalhado — imprime o que o scraper viu
        if diag:
            candidatos = diag.get("candidatos_botao_arquivos", []) or []
            log(f"    [diag] candidatos ao botão Arquivos: {len(candidatos)}")
            for c in candidatos[:5]:
                log(
                    f"      - <{c.get('tag')}> '{c.get('texto')}' id={c.get('id')} cls={c.get('classes', '')[:60]}"
                )
            log(f"    [diag] clique: {diag.get('clicou', 'N/A')}")
            dlgs = diag.get("dialogs_visiveis", []) or []
            log(f"    [diag] dialogs visíveis: {len(dlgs)}")
            for d in dlgs[:5]:
                log(
                    f"      - id={d.get('id')} título='{d.get('titulo')}' linhas={d.get('linhas_tabela')}"
                )
            log(f"    [diag] estrategia_localizacao: {diag.get('estrategia', 'N/A')}")
            log(
                f"    [diag] total_linhas_vistas: {diag.get('total_linhas', 0)} filtradas: {diag.get('linhas_filtradas', 0)}"
            )
            snippet = (diag.get("html_snippet") or "").replace("\n", " ")[:400]
            if snippet:
                log(f"    [diag] html[:400]: {snippet}")
        return {"total_no_sisdpu": 0, "baixados": 0, "ocr_ok": 0, "overflow": None}

    log(f"  [anexos] {total} anexos listados no SISDPU")

    # Ordena por data DESC (mais recentes primeiro). Anexos sem data parseavel
    # vao pro fim — sao os menos prioritarios.
    # Estabilidade: mantem ordem do DOM como tiebreaker (sorted e' stable).
    lista_ordenada = sorted(
        lista,
        key=lambda i: _parse_data_arquivo(i.get("data", "")),
        reverse=True,
    )

    overflow_info = None

    if filtro_data_min is not None:
        # Modo "completar desde data": baixa todos com data >= filtro_data_min,
        # sem aplicar limite.
        lista_para_baixar = [
            i
            for i in lista_ordenada
            if _parse_data_arquivo(i.get("data", "")) >= filtro_data_min
        ]
        log(
            f"  [anexos] modo 'desde data': filtrando data >= "
            f"{filtro_data_min.isoformat()} → {len(lista_para_baixar)} de {total}"
        )
    elif limite > 0 and total > limite:
        # Modo padrao com overflow: baixa os `limite` mais recentes
        lista_para_baixar = lista_ordenada[:limite]
        cortados = lista_ordenada[limite:]
        data_min_baixada = _parse_data_arquivo(lista_para_baixar[-1].get("data", ""))
        data_min_disponivel = _parse_data_arquivo(cortados[-1].get("data", ""))
        overflow_info = {
            "total_no_sisdpu": total,
            "baixados": len(lista_para_baixar),
            "data_min_baixada": data_min_baixada.isoformat(),
            "data_min_disponivel": data_min_disponivel.isoformat(),
            "limite_aplicado": limite,
        }
        log(
            f"  [anexos] {total} > limite {limite} — baixando os {limite} mais "
            f"recentes (data >= {data_min_baixada.isoformat()})"
        )
        log(
            f"  [anexos] {len(cortados)} anexos mais antigos NAO baixados "
            f"(data >= {data_min_disponivel.isoformat()} ate "
            f"{_parse_data_arquivo(cortados[0].get('data', '')).isoformat()})"
        )
    else:
        lista_para_baixar = lista_ordenada

    lista = lista_para_baixar
    baixados = 0
    ocr_ok = 0

    import re as _re

    def _stem_sem_prefixo_idx(stem: str) -> str:
        """Remove prefixo numerico inicial 'NN_' (ex: '01_', '23_') do stem.

        Permite comparar arquivos baixados em runs diferentes — se o numero de
        anexos crescer (nova categoria aparecer no SISDPU), o `idx` pode mudar
        sem que o conteudo do arquivo tenha mudado. Comparamos o restante.
        """
        return _re.sub(r"^\d{2,4}_", "", stem)

    interrompido = False

    # Usa nova estrategia: fetch() + servidor local (mais robusto que Playwright clicks)
    log(f"  [anexos] baixando {len(lista)} arquivo(s) via fetch...")
    resultado_fetch = await sisdpu_client.baixar_todos_arquivos(
        pasta_pecas, lista_arquivos=lista, log=log, seq_por_file_id=seq_por_file_id
    )

    if resultado_fetch.get("total", 0) > 0:
        baixados = resultado_fetch.get("baixados", 0)
        total = resultado_fetch['total']
        erros_count = len(resultado_fetch.get("erros", []))
        log("")
        log(f"✓ {baixados}/{total} arquivo(s) baixado(s) com sucesso   /   {erros_count} erros")

    # OCR simples (rapido, qualidade baixa) — desabilitado por padrao. Para
    # texto de qualidade, use o botao "Melhorar OCR" (OCR local + Sonnet
    # visao), que roda por fora deste fluxo, sob demanda.
    if not rodar_ocr:
        log("  [ocr] etapa desabilitada (ative 'OCR simples' se quiser rodar aqui)")
    else:
        pdfs_pasta = sorted(
            [p for p in pasta_pecas.glob("*.pdf")],
            key=lambda p: p.stat().st_mtime,
        )

        if pdfs_pasta:
            log(f"  [ocr] processando {len(pdfs_pasta)} PDF(s)...")
            await asyncio.sleep(0.01)  # Permite que o asyncio processe/flush o log

        for idx, pdf_arquivo in enumerate(pdfs_pasta, start=1):
            # Checkpoint de cancelamento — usuario pode parar o OCR
            if deve_cancelar is not None and deve_cancelar():
                log(
                    f"  ✗ OCR CANCELADO pelo usuario apos {ocr_ok}/{len(pdfs_pasta)}"
                )
                interrompido = True
                break

            txt_path = pdf_arquivo.with_suffix(".txt")
            if txt_path.exists() and txt_path.stat().st_size > 0:
                log(f"    [{idx}/{len(pdfs_pasta)}] {pdf_arquivo.name[:50]} ... (já processado)")
                await asyncio.sleep(0.01)
                continue

            if deve_cancelar is not None and deve_cancelar():
                log(f"    [{idx}/{len(pdfs_pasta)}] {pdf_arquivo.name[:50]} ... cancelado")
                await asyncio.sleep(0.01)
                continue

            # Log ANTES de começar o OCR
            nome_curto = pdf_arquivo.name[:60]
            log(f"    [{idx}/{len(pdfs_pasta)}] {nome_curto} ... processando")
            await asyncio.sleep(0.01)

            try:
                texto = ocr.extrair_texto(
                    pdf_arquivo,
                    deve_cancelar=deve_cancelar,
                    timeout_por_pagina_seg=TIMEOUT_OCR_POR_PAGINA_SEG,
                )
                txt_path.write_text(texto, encoding="utf-8")

                if "[OCR abortado pelo usuario" in texto:
                    log(f"             ... OCR abortado")
                elif texto and "[OCR indisponivel" not in texto:
                    ocr_ok += 1
                    log(f"             ... OCR processado")
                else:
                    log(f"             ... OCR indisponível")
            except Exception as e:
                log(f"             ... ERRO  ✗ {type(e).__name__}")

            await asyncio.sleep(0.01)

        if pdfs_pasta:
            log("")
            log(f"✓ {ocr_ok}/{len(pdfs_pasta)} PDF(s) processado(s) com sucesso   /   {len(pdfs_pasta) - ocr_ok} erros")

    # Fecha o dialogo pra nao atrapalhar proximo PAJ (quando usado no loop)
    with contextlib.suppress(Exception):
        await sisdpu_client.fechar_dialogo_arquivos()

    # Limpeza automatica: remove .txt OCR vazios/placeholder (evita poluicao).
    # Camada 1 de gestao de disco — rebatizar_bins_existentes ja rodou no inicio.
    try:
        n_ocr_limpos = limpar_ocr_vazios(pasta_pecas, log)
        if n_ocr_limpos:
            log(f"    [ocr] {n_ocr_limpos} .txt vazio(s)/placeholder removido(s)")
    except Exception as e:
        log(f"    [ocr] erro ao limpar vazios: {type(e).__name__}: {e}")

    return {
        "total_no_sisdpu": total,
        "baixados": baixados,
        "ocr_ok": ocr_ok,
        "overflow": overflow_info,
        "interrompido": interrompido,
    }


async def _processar_paj(
    item: dict,
    log: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool = True,
    esperas_curtas: bool = False,
) -> tuple[str, bool]:
    """Processa um PAJ vindo da CAIXA DE ENTRADA. Retorna (paj_norm, novo_ou_atualizado).

    `deve_cancelar` e' propagado ate o OCR pra permitir cancelamento imediato
    durante processamento de um unico PAJ com PDFs grandes.

    `baixar_anexos=False` pula o download/OCR da aba 'Arquivos' — util pra
    sincronizacao rapida (so movs/metadata).

    `extrair_documentos=False` pula a extracao dos documentos por seq (abre um
    popup por movimentacao com anexo — caro em PAJ nunca sincronizado). Usado
    pela aba Retornos, que so precisa das movs/narrativa pra IA. Documentos ja
    conhecidos de sync anterior sao PRESERVADOS.

    `esperas_curtas=True` reduz as esperas fixas pos-navegacao (repassado a
    `movimentacoes_paj`) — tambem usado pela Retornos.

    `rodar_ocr=False` (padrao) pula o OCR simples nos PDFs baixados — so
    baixa os arquivos. O OCR simples e' de qualidade baixa; para texto de
    qualidade use o botao "Melhorar OCR" (OCR local + Sonnet visao) sob
    demanda, ja disponivel na aba Anexos SISDPU.

    PRE-CONDICAO: o PAJ esta na caixa atual (item veio do scrape da caixa).
    Para PAJs fora da caixa (concluidos, ex: watchlist), use
    `rodar_paj_via_busca_global` que entra pelo campo de pesquisa do SIS.
    """
    paj = item.get("paj", "")
    dec = _decompor_paj(paj)
    if not dec:
        log(f"  [skip] PAJ mal-formado: {paj!r}")
        return ("", False)
    ano, unidade, numero = dec
    paj_norm = _normalizar_paj(paj)

    log(f"  [sisdpu] abrindo detalhamento {paj}")
    try:
        det = await sisdpu_client.movimentacoes_paj(
            numero, ano, unidade, esperas_curtas=esperas_curtas)
    except Exception as e:
        log(f"  [ERRO] movimentacoes_paj {paj}: {type(e).__name__}: {e}")
        return (paj_norm, False)

    if det.get("erro"):
        log(f"  [ERRO] {det['erro']}")
        return (paj_norm, False)

    return await _processar_paj_pos_detalhamento(
        item,
        det,
        log,
        deve_cancelar=deve_cancelar,
        baixar_anexos=baixar_anexos,
        rodar_ocr=rodar_ocr,
        extrair_documentos=extrair_documentos,
        via_busca_global=False,
    )


async def _processar_paj_pos_detalhamento(
    item: dict,
    det: dict,
    log: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool = True,
    via_busca_global: bool = False,
) -> tuple[str, bool]:
    """Etapa pos-detalhamento: a page do Playwright ja esta no detalhamento do
    PAJ (via clique na caixa ou via campo de pesquisa global) e `det` ja foi
    extraido. Grava sisdpu.txt/metadata.json, detecta prazos, baixa anexos.

    `via_busca_global=True`: nao sobrescreve `em_caixa_atual` (preserva o
    valor anterior — a busca global nao responde se o PAJ ainda esta na
    caixa do defensor), e preserva `etiqueta_sisdpu` do metadata antigo
    (o campo de pesquisa do SIS nao expoe a etiqueta da caixa).
    """
    paj = item.get("paj", "") or det.get("paj", "")
    dec = _decompor_paj(paj)
    if not dec:
        log(f"  [skip] PAJ mal-formado: {paj!r}")
        return ("", False)
    paj_norm = _normalizar_paj(paj)
    pasta = PAJS_DIR / paj_norm
    ja_existia = pasta.exists()
    pasta.mkdir(parents=True, exist_ok=True)

    # Captura movs antigas ANTES de sobrescrever metadata.json — usado pra
    # detectar intimacoes/citacoes novas e enviar pro calendar-pendentes.
    # Tambem preserva campos de ciclo de vida quando vindo pela busca global.
    movs_antigas: list[dict] = []
    assistido_prev = ""
    sync_incompleto_anterior = False
    sync_incompleto_em_anterior = ""
    meta_antiga: dict = {}
    try:
        meta_path_antiga = pasta / "metadata.json"
        if meta_path_antiga.exists():
            meta_antiga = json.loads(meta_path_antiga.read_text(encoding="utf-8"))
            det_antiga = meta_antiga.get("detalhes_sisdpu", {}) or {}
            movs_antigas = det_antiga.get("movimentacoes", []) or []
            assistido_prev = meta_antiga.get("assistido_caixa", "") or ""
            sync_incompleto_anterior = bool(meta_antiga.get("sync_incompleto", False))
            sync_incompleto_em_anterior = meta_antiga.get("sync_incompleto_em", "")
    except Exception:
        movs_antigas = []
        meta_antiga = {}

    texto_sisdpu = _montar_sisdpu_txt(item, det)
    (pasta / "sisdpu.txt").write_text(texto_sisdpu, encoding="utf-8")

    try:
        metadata = parser.montar_metadata(paj_norm, texto_sisdpu)
    except Exception as e:
        log(f"  [ERRO] parser metadata: {type(e).__name__}: {e}")
        metadata = {"paj": paj, "paj_norm": paj_norm, "erro_parser": str(e)}

    # Snapshot da estrutura bruta devolvida pelo SISDPU para diagnostico/posteridade
    metadata["sisdpu_raw"] = det

    # TODOS os processos judiciais vinculados ao PAJ — direto do `det` (sem reparse
    # de texto, evita perda de dados), mesmo padrão usado para movimentações abaixo.
    # `processo_judicial` (singular, em metadata via parser) continua existindo
    # para os consumidores antigos (compat), representando o ÚLTIMO da lista.
    if det.get("processos_judiciais"):
        metadata["processos_judiciais"] = det["processos_judiciais"]

    # Preenchido abaixo (se houver movimentacoes) e usado no bloco de anexos,
    # mais adiante, pra nomear os arquivos baixados com a seq real.
    seq_por_file_id: dict[str, int] = {}

    # Se temos movimentacoes estruturadas do sisdpu_client, usa direto sem perda
    # (evita parsing regex que perde campos como movimentacao, fases, usuario, anexo)
    if det.get("movimentacoes"):
        mov_estruturadas = parser.parsear_movimentacoes_estruturadas(det["movimentacoes"])
        if "detalhes_sisdpu" not in metadata:
            metadata["detalhes_sisdpu"] = {}
        metadata["detalhes_sisdpu"]["movimentacoes"] = mov_estruturadas

        # Documentos por seq (icone de "Anexo" na tabela de movimentacoes).
        # Incremental: so reextrai seqs que ainda nao tinham `documentos` no
        # metadata anterior — evita reabrir popups de movimentacoes ja
        # conhecidas a cada sync (roda em rapida e completa, sem gate em
        # baixar_anexos, porque o custo por sync fica proporcional so ao que
        # mudou desde a ultima vez).
        documentos_conhecidos_por_seq = {
            str(mov_antiga.get("seq")): mov_antiga["documentos"]
            for mov_antiga in movs_antigas
            if mov_antiga.get("documentos")
        }
        # `extrair_documentos=False` (ex.: aba Retornos): PULA a extracao por seq
        # (cara — 1 popup por movimentacao com anexo), mas ainda preserva os
        # documentos ja conhecidos de sync anterior (nada e' perdido). Um sync
        # completo posterior reextrai as seqs que faltarem (custo incremental).
        if extrair_documentos:
            try:
                resultado_docs = await sisdpu_client.extrair_documentos_por_seq(
                    seqs_conhecidos=list(documentos_conhecidos_por_seq.keys()),
                    deve_cancelar=deve_cancelar,
                    log=log,
                )
            except Exception as e:
                log(f"  [docs] erro ao extrair documentos por seq: {type(e).__name__}: {e}")
                resultado_docs = {}
        else:
            log("  [docs] extracao por seq PULADA (sync rapida — documentos conhecidos preservados)")
            resultado_docs = {}

        docs_novos_por_seq = {
            str(mov_doc.get("seq")): mov_doc.get("documentos", [])
            for mov_doc in resultado_docs.get("movimentacoes_com_documentos", [])
        }

        n_documentos_indexados = 0
        for mov in mov_estruturadas:
            seq_str = str(mov.get("seq"))
            documentos = docs_novos_por_seq.get(seq_str) or documentos_conhecidos_por_seq.get(seq_str) or []
            mov["documentos"] = documentos
            n_documentos_indexados += len(documentos)
        metadata["n_documentos_indexados"] = n_documentos_indexados

        # Mapa {id_do_arquivo -> seq_real}, usado na nomeacao dos arquivos
        # baixados mais abaixo (bloco de anexos) — permite ao frontend casar
        # o link da coluna "Documentos" com a copia local pelo ID, nao pelo
        # nome (que pode se repetir).
        for mov in mov_estruturadas:
            for doc in mov.get("documentos", []):
                file_id = sisdpu_client.extrair_id_arquivo(doc.get("urlDownload", ""))
                if file_id:
                    seq_por_file_id[file_id] = mov.get("seq")

    if via_busca_global:
        # Busca global nao da informacao sobre estar na caixa atual — preserva
        # o que tinhamos (se houver). Tambem nao tem acesso a etiqueta da
        # caixa, entao mantemos a antiga.
        if "em_caixa_atual" in meta_antiga:
            metadata["em_caixa_atual"] = meta_antiga["em_caixa_atual"]
        metadata["etiqueta_sisdpu"] = meta_antiga.get("etiqueta_sisdpu", "") or ""
        # Campos de ciclo de vida que nao vem do detalhamento — preservar.
        for k in (
            "via_watchlist",
            "concluido_em",
            "arquivado_em",
            "n_anexos_removidos",
            "anexos_removidos_em",
        ):
            if k in meta_antiga:
                metadata[k] = meta_antiga[k]
    else:
        # Marca explicitamente: este PAJ esta na caixa atual
        metadata["em_caixa_atual"] = True
        # Etiqueta SISDPU (rotulo livre aplicado pelo Defensor na caixa, tipo "Aleg finais")
        etiqueta = (item.get("etiqueta") or "").strip()
        metadata["etiqueta_sisdpu"] = etiqueta
        # Descricao do tramite como aparece na caixa de entrada (coluna "Descricao"
        # do dashboard). Vem da linha da caixa (item.desc / item.descricao); o parser
        # de sisdpu.txt deixa desc_mov_caixa vazio no fluxo atual, entao gravamos aqui.
        desc_caixa = (item.get("desc") or item.get("descricao") or "").strip()
        if desc_caixa:
            metadata["desc_mov_caixa"] = desc_caixa[:500]
        # Data do tramite que esta na caixa de entrada (data da linha da caixa).
        # Substitui o fallback do parser do sisdpu.txt (que caia na data de
        # ABERTURA). Convertida p/ ISO (YYYY-MM-DD) — e' a "Data" do dashboard.
        _data_caixa_iso = parser.parse_data_br(item.get("data") or "")
        if _data_caixa_iso:
            metadata["data_mov_caixa"] = _data_caixa_iso

    # Categoria de submenu (retorno/SAT/geral) — regra ÚNICA em services.categorias_caixa.
    # Fail-safe: descrição que não casa nenhuma regra → "" (geral, aparece no dashboard).
    # Na busca global (sem descrição confiável da caixa) preserva a categoria já gravada.
    from services import categorias_caixa
    if via_busca_global:
        metadata["categoria_caixa"] = meta_antiga.get("categoria_caixa", "")
    else:
        metadata["categoria_caixa"] = categorias_caixa.classificar(
            item.get("desc", ""), item.get("descricao", ""), item.get("etiqueta", ""))

    (pasta / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Anexos SISDPU: usa o botao "Arquivos" do PAJ (lista consolidada de todos
    # os anexos, tipo/descricao/arquivo). Pre-condicao: movimentacoes_paj foi
    # chamado acima e a page do Playwright esta no detalhamento deste PAJ.
    if baixar_anexos:
        # Checkpoint de cancelamento ANTES de comecar o download de anexos.
        # Se o usuario ja sinalizou cancel apos movimentacoes_paj() retornar,
        # marca o PAJ como incompleto e nao tenta baixar nada.
        if deve_cancelar is not None and deve_cancelar():
            log("  [anexos] CANCELADO pelo usuario antes do download — marcando PAJ incompleto")
            metadata["n_anexos_sisdpu"] = metadata.get("n_anexos_sisdpu", 0)
            metadata["sync_incompleto"] = True
            import datetime as _dt

            metadata["sync_incompleto_em"] = _dt.datetime.now().isoformat()
            metadata["sync_incompleto_motivo"] = "cancelado antes do download de anexos"
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            log(f"  [ok] {paj_norm} {'(novo)' if not ja_existia else '(atualizado parcialmente)'}")
            return (paj_norm, True)

        resultado_anexos = await _baixar_anexos_sisdpu(
            pasta / "Arquivos",
            log,
            limite=MAX_ANEXOS_POR_PAJ,
            deve_cancelar=deve_cancelar,
            seq_por_file_id=seq_por_file_id,
            rodar_ocr=rodar_ocr,
        )
        total_anexos = resultado_anexos["total_no_sisdpu"]
        baixados = resultado_anexos["baixados"]
        ocr_ok = resultado_anexos["ocr_ok"]
        overflow = resultado_anexos["overflow"]
        interrompido = resultado_anexos.get("interrompido", False)
        metadata["n_anexos_sisdpu"] = total_anexos
        if overflow:
            metadata["anexos_extras"] = overflow
        else:
            # Tudo baixado nesta rodada — limpa flag antigo se existia
            metadata.pop("anexos_extras", None)

        # Flag de sincronizacao incompleta: setada quando o usuario cancela
        # NO MEIO do download de anexos. Bloqueia "Elaborar" na UI ate ser
        # ressincronizado com sucesso.
        if interrompido:
            import datetime as _dt

            metadata["sync_incompleto"] = True
            metadata["sync_incompleto_em"] = _dt.datetime.now().isoformat()
            metadata["sync_incompleto_motivo"] = (
                f"cancelado durante download de anexos ({baixados} de {total_anexos} baixados)"
            )
        else:
            # Sync completou — limpa qualquer flag antigo
            metadata.pop("sync_incompleto", None)
            metadata.pop("sync_incompleto_em", None)
            metadata.pop("sync_incompleto_motivo", None)

        # Re-grava metadata.json com a contagem atualizada
        (pasta / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log(f"  [anexos] total={total_anexos} baixados={baixados} ocr={ocr_ok}")
        if overflow:
            # Usa a data de INSTAURAÇÃO do PAJ (real, vinda do header do SIS),
            # nao a data_min_disponivel calculada a partir do parser de descricao
            # dos anexos (que pode resultar em datas ficcionais quando os anexos
            # mais antigos nao trazem data parseavel — ex: 0001-01-01).
            data_inst_br = (
                _iso_para_br(metadata.get("data_abertura", ""))
                or "data de instauracao indisponivel"
            )
            log(
                f"  [anexos] OVERFLOW: {overflow['total_no_sisdpu'] - overflow['baixados']} "
                f"anexo(s) mais antigos pendentes (use 'Baixar mais antigos' "
                f"desde a instauração do PAJ em {data_inst_br})"
            )
        if interrompido:
            log(
                "  [warn] PAJ marcado como SINCRONIZACAO INCOMPLETA — re-sincronize antes de elaborar"
            )
    else:
        # Modo rapido: nao toca no flag sync_incompleto. Se ja era incompleto,
        # continua incompleto (rapido nao baixa anexos novos).
        if sync_incompleto_anterior:
            metadata["sync_incompleto"] = True
            if sync_incompleto_em_anterior:
                metadata["sync_incompleto_em"] = sync_incompleto_em_anterior
            metadata["sync_incompleto_motivo"] = "anexos pendentes (modo rapido nao baixa anexos)"
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        log("  [anexos] modo rápido — download pulado")

    # Forca regeneracao do PROMPT_MAX na proxima visita
    prompt_max = pasta / "PROMPT_MAX.md"
    if prompt_max.exists():
        with contextlib.suppress(Exception):
            prompt_max.unlink()

    log(f"  [ok] {paj_norm} {'(novo)' if not ja_existia else '(atualizado)'}")
    return (paj_norm, True)


def _apagar_anexos_pasta(pasta: Path, log: Callable[[str], None]) -> tuple[int, int]:
    """Apaga binarios em pasta/Arquivos/, preservando .txt OCR companions.

    Retorna (n_removidos, bytes_liberados). Usado na transicao de PAJ para
    "concluido" (fora-da-caixa) — libera espaco mantendo OCR extraido e
    registros na raiz.
    """
    pasta_pecas = pasta / "Arquivos"
    if not pasta_pecas.exists() or not pasta_pecas.is_dir():
        return (0, 0)
    removidos = 0
    bytes_lib = 0
    for f in list(pasta_pecas.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() == ".txt":
            continue  # preserva OCR companion
        try:
            tam = f.stat().st_size
            f.unlink()
            removidos += 1
            bytes_lib += tam
        except Exception as e:
            log(f"    [limpa] falha ao remover {f.name}: {type(e).__name__}: {e}")
    return (removidos, bytes_lib)


def _reconciliar_concluidos(
    pajs_na_caixa: set[str],
    log: Callable[[str], None],
) -> int:
    """Marca em_caixa_atual=False para pastas PAJs/PAJ-* que nao estao na
    caixa atual. Aqui "concluido" significa apenas "saiu da caixa de entrada"
    — o PAJ pode ter sido formalmente arquivado, tramitado, finalizado etc.;
    a distincao formal de arquivamento e' um status do SIS-DPU (status_paj).

    - PAJs na caixa ja foram marcados True durante _processar_paj.
    - PAJs no disco mas fora da caixa (antigos, concluidos, sairam do oficio): False.
    - Na TRANSICAO de ativo->concluido, apaga automaticamente os anexos
      binarios (Arquivos/*.pdf, etc.), preservando OCR companions e registros
      na raiz.
    Retorna numero de PAJs concluidos nesta rodada.
    """
    if not PAJS_DIR.exists():
        return 0

    from datetime import datetime

    concluidos = 0
    for pasta in PAJS_DIR.iterdir():
        if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
            continue
        if pasta.name in pajs_na_caixa:
            continue  # ja marcado True no processamento

        meta_path = pasta / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if meta.get("em_caixa_atual") is False:
            continue  # ja concluido em sync anterior

        # PAJ adicionado via watchlist (busca explicita do defensor) que NUNCA
        # passou pela caixa nao deve ser concluido pela reconciliacao — ele
        # simplesmente nao esta no dominio da caixa. Sem essa excecao, qualquer
        # sync da caixa o marcaria concluido e apagaria anexos que o defensor
        # baixou manualmente via botao "Sincronizar". So entra no fluxo de
        # conclusao se um dia tiver sido visto na caixa (em_caixa_atual=True).
        if meta.get("via_watchlist") and meta.get("em_caixa_atual") is not True:
            continue

        # Transicao ativo -> concluido
        agora = datetime.now().isoformat(timespec="seconds")
        meta["em_caixa_atual"] = False
        meta["concluido_em"] = agora
        # Limpa campo legado se presente (compat com metadata.json antigos)
        meta.pop("arquivado_em", None)

        # Auto-delete dos anexos binarios (preservando OCR e registros)
        n_removidos, bytes_lib = _apagar_anexos_pasta(pasta, log)
        if n_removidos > 0:
            meta["n_anexos_removidos"] = meta.get("n_anexos_removidos", 0) + n_removidos
            hist = meta.get("anexos_removidos_em") or []
            hist.append(agora)
            meta["anexos_removidos_em"] = hist
            log(
                f"  [concluido] {pasta.name}: {n_removidos} anexo(s) removido(s) "
                f"({bytes_lib / 1024:.0f} KB liberados)"
            )
        else:
            log(f"  [concluido] {pasta.name}")

        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            concluidos += 1
        except Exception as e:
            log(f"  [warn] falha ao concluir {pasta.name}: {e}")

    return concluidos


async def atualizar_caixa(log_callback: Callable[[str], None]) -> dict:
    """Reconciliacao RAPIDA: le so' a listagem da caixa de entrada (1 pagina,
    sem abrir cada PAJ) e marca `em_caixa_atual=False` para os PAJs ja
    sincronizados em disco que nao aparecem mais nela — mesma reconciliacao
    de `rodar()`, mas sem o custo de visitar PAJ por PAJ.

    Usado pelo botao "Atualizar" do dashboard: NAO adiciona PAJs novos (isso
    continua sendo papel do "Sincronizar caixa" completo) — so' remove da
    lista quem saiu. Os dados/anexos ja baixados sao preservados (exceto pela
    limpeza automatica de anexos binarios que ja acontece em qualquer
    transicao ativo->concluido, igual ao sync completo).

    Retorna {"ok": bool, "total_caixa": int, "removidos": int, "mensagem": str}.
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    log("[atualizar] abrindo caixa de entrada...")
    try:
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {
            "ok": False,
            "total_caixa": 0,
            "removidos": 0,
            "mensagem": f"falha ao abrir caixa: {e}",
        }

    itens_raw = data.get("itens_tabela", []) or []
    pajs_na_caixa: set[str] = set()
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and it.get("paj"):
            pajs_na_caixa.add(_normalizar_paj(it["paj"]))

    log(f"[atualizar] caixa: {len(pajs_na_caixa)} PAJs")

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    log("[atualizar] reconciliando PAJs que saíram da caixa...")
    removidos = _reconciliar_concluidos(pajs_na_caixa, log)
    if removidos:
        log(f"[atualizar] {removidos} PAJ(s) saíram da caixa e foram removidos do dashboard")
    else:
        log("[atualizar] nenhum PAJ saiu da caixa — tudo em dia")

    return {
        "ok": True,
        "total_caixa": len(pajs_na_caixa),
        "removidos": removidos,
        "mensagem": "",
    }


async def rodar(
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
) -> dict:
    """Sincroniza caixa SISDPU com PAJs/<PAJ>/.

    log_callback: funcao que recebe uma linha de log por chamada.
    deve_cancelar: callback opcional consultado entre PAJs; se True,
        o laco abort e a funcao retorna o resumo parcial.
    baixar_anexos: se False, pula download/OCR dos anexos (sync rapido).
    rodar_ocr: se True, roda o OCR simples nos PDFs baixados (padrao False —
        so baixa; use "Melhorar OCR" na aba Anexos SISDPU pra texto de qualidade).
    Retorna dict com resumo: {"total_caixa": N, "processados": M, "erros": [...]}.
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    def _cancelado() -> bool:
        try:
            return bool(deve_cancelar()) if deve_cancelar else False
        except Exception:
            return False

    resumo: dict = {
        "total_caixa": 0,
        "processados": 0,
        "concluidos": 0,
        "erros": [],
        "cancelado": False,
    }
    pajs_na_caixa: set[str] = set()

    log("[sync] iniciando sincronização SISDPU")
    log(f"[sync] timeout global: {TIMEOUT_TOTAL}s, rate-limit: {RATE_LIMIT_SISDPU}s/PAJ")
    if not baixar_anexos:
        log("[sync] modo RÁPIDO — anexos NÃO serão baixados (só movs/metadata)")

    try:
        log("[sync] abrindo caixa de entrada...")
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        resumo["erros"].append(f"caixa: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    itens_raw = data.get("itens_tabela", []) or []
    itens: list[dict] = []
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it:
            itens.append(it)
    resumo["total_caixa"] = len(itens)
    log(f"[sync] caixa: {len(itens)} PAJs válidos")

    if not itens:
        log("[sync] nada a sincronizar — encerrando")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    for i, item in enumerate(itens, start=1):
        if _cancelado():
            log(f"[sync] CANCELADO pelo usuário após {i - 1}/{len(itens)} PAJs")
            resumo["cancelado"] = True
            break
        log(f"[{i}/{len(itens)}] {item.get('paj', '?')}")
        try:
            paj_norm, ok = await _processar_paj(
                item,
                log,
                deve_cancelar=_cancelado,
                baixar_anexos=baixar_anexos,
                rodar_ocr=rodar_ocr,
            )
            if paj_norm:
                pajs_na_caixa.add(paj_norm)
            if ok:
                resumo["processados"] += 1
        except Exception as e:
            log(f"  [EXC] {type(e).__name__}: {e}")
            resumo["erros"].append(f"{item.get('paj', '?')}: {e}")
        await asyncio.sleep(RATE_LIMIT_SISDPU)

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    # Se foi cancelado, pula reconciliacao (dados incompletos — nao queremos
    # concluir PAJs que ainda nao foram visitados)
    if resumo["cancelado"]:
        log("[sync] reconciliação SALTADA (sync cancelada — dados parciais)")
        return resumo

    # Reconciliacao: PAJs no disco fora da caixa sao marcados em_caixa_atual=False.
    log("[sync] reconciliando PAJs concluídos (fora da caixa)...")
    concluidos = _reconciliar_concluidos(pajs_na_caixa, log)
    resumo["concluidos"] = concluidos
    if concluidos:
        log(f"[sync] {concluidos} PAJ(s) marcados como concluídos")

    # Resumo de PAJs com anexos pendentes (overflow do limite de 30) e
    # PAJs com sincronizacao incompleta (cancelados mid-PAJ).
    pajs_overflow: list[dict] = []
    pajs_incompletos: list[dict] = []
    for paj_norm in pajs_na_caixa:
        meta_path = PAJS_DIR / paj_norm / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ov = meta.get("anexos_extras")
        if ov and isinstance(ov, dict):
            pajs_overflow.append(
                {
                    "paj_norm": paj_norm,
                    "total_no_sisdpu": ov.get("total_no_sisdpu"),
                    "baixados": ov.get("baixados"),
                    "data_min_baixada": ov.get("data_min_baixada"),
                    # data de instauracao real do PAJ — usada no log abaixo no lugar
                    # da `data_min_disponivel` (que costuma ser ficcional 0001-01-01).
                    "data_abertura": meta.get("data_abertura", ""),
                }
            )
        if meta.get("sync_incompleto"):
            pajs_incompletos.append(
                {
                    "paj_norm": paj_norm,
                    "motivo": meta.get("sync_incompleto_motivo", ""),
                    "em": meta.get("sync_incompleto_em", ""),
                }
            )

    if pajs_overflow:
        log("=" * 60)
        log(f"[sync] {len(pajs_overflow)} PAJ(s) com mais de {MAX_ANEXOS_POR_PAJ} anexos:")
        for p in pajs_overflow:
            data_inst_br = _iso_para_br(p.get("data_abertura", "")) or "?"
            data_min_br = _iso_para_br(p.get("data_min_baixada", "")) or "?"
            log(
                f"  - {p['paj_norm']}: {p['baixados']}/{p['total_no_sisdpu']} baixados "
                f"(mais antigo baixado: {data_min_br}; "
                f"instaurado em: {data_inst_br})"
            )
        log(
            "[sync] DICA: abra o PAJ → aba Anexos → 'Baixar mais antigos' "
            "para informar a data de corte e completar."
        )
    resumo["pajs_overflow"] = pajs_overflow

    if pajs_incompletos:
        log("=" * 60)
        log(
            f"[sync] {len(pajs_incompletos)} PAJ(s) com SINCRONIZACAO INCOMPLETA "
            "(elaboracao bloqueada ate ressincronizar):"
        )
        for p in pajs_incompletos:
            log(f"  - {p['paj_norm']}: {p['motivo']}")
        log(
            "[sync] DICA: abra cada PAJ e clique em 'Re-sincronizar agora' "
            "no banner vermelho do topo."
        )
    resumo["pajs_incompletos"] = pajs_incompletos

    log("=" * 60)
    log(
        f"[sync] FIM: {resumo['processados']}/{resumo['total_caixa']} processados, "
        f"{len(resumo['erros'])} erros"
    )

    # Marca este momento como "última sincronização da CAIXA INTEIRA". Usado
    # pelo dashboard como "ultima_execucao" — diferente dos mtimes dos
    # metadata.json individuais (que mudam tambem em sync de um PAJ unico,
    # em sync de anexos-desde-data, e na busca leve via watchlist). Soh este
    # marcador reflete uma varredura completa da caixa.
    with contextlib.suppress(Exception):
        from datetime import datetime as _dt2

        marcador = PAJS_DIR / ".ultima_sync_caixa.json"
        PAJS_DIR.mkdir(parents=True, exist_ok=True)
        marcador.write_text(
            json.dumps(
                {
                    "em": _dt2.now().isoformat(timespec="seconds"),
                    "modo": "completa" if baixar_anexos else "rapida",
                    "total_caixa": resumo.get("total_caixa", 0),
                    "processados": resumo.get("processados", 0),
                    "erros": len(resumo.get("erros", [])),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return resumo


async def rodar_anexos_desde_data(
    paj_identificador: str,
    data_inicio,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    rodar_ocr: bool = False,
) -> dict:
    """Baixa anexos de UM PAJ a partir de uma data de corte (inclusive).

    `data_inicio` e' um `datetime.date`. Baixa todos os anexos com
    `data >= data_inicio`, sem aplicar o limite padrao de 30. Os 17 que ja
    estiverem no disco sao detectados pelo dedup e pulados.

    Reuso do fluxo: login → caixa → abrir detalhamento do PAJ → baixar anexos.
    Nao mexe em movs/metadata textuais; so completa o conjunto de anexos.

    Retorna dict com {paj_norm, total_no_sisdpu, baixados, ocr_ok, novos,
    overflow_residual}.
    """
    import datetime as _dt

    if not isinstance(data_inicio, _dt.date):
        raise TypeError("data_inicio deve ser datetime.date")

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    log(f"[anexos-desde] alvo: {paj_identificador} (data_inicio={data_inicio.isoformat()})")

    # 1) Localiza o PAJ na caixa atual (mesmo modus operandi de rodar_paj_unico)
    log("[anexos-desde] abrindo caixa de entrada...")
    try:
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": f"caixa: {e}"}

    itens_raw = data.get("itens_tabela", []) or []
    alvo = _normalizar_paj(paj_identificador)
    item_alvo = None
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and _normalizar_paj(it.get("paj", "")) == alvo:
            item_alvo = it
            break

    if not item_alvo:
        log(f"[anexos-desde] PAJ {alvo} NÃO encontrado na caixa atual")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": "paj_nao_na_caixa"}

    # 2) Abre detalhamento (necessario antes de listar arquivos)
    paj_txt = item_alvo.get("paj", "")
    dec = _decompor_paj(paj_txt)
    if not dec:
        log(f"[anexos-desde] PAJ mal-formado: {paj_txt!r}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": "paj_mal_formado"}
    ano, unidade, numero = dec

    log(f"  [sisdpu] abrindo detalhamento {paj_txt}")
    try:
        det = await sisdpu_client.movimentacoes_paj(numero, ano, unidade)
    except Exception as e:
        log(f"  [ERRO] movimentacoes_paj: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": f"detalhamento: {e}"}

    if det.get("erro"):
        log(f"  [ERRO] {det['erro']}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {"erro": det["erro"]}

    pasta = PAJS_DIR / alvo

    # 2.5) Monta o mesmo mapa {id_do_arquivo -> seq_real} usado na sincronizacao
    # normal, mas lendo do metadata.json JA EXISTENTE (populado numa completa
    # anterior — e' assim que o overflow foi detectado em primeiro lugar).
    # Evita reprocessar a extracao por seq so pra completar anexos antigos.
    seq_por_file_id: dict[str, int] = {}
    meta_path_existente = pasta / "metadata.json"
    if meta_path_existente.exists():
        try:
            meta_existente = json.loads(meta_path_existente.read_text(encoding="utf-8"))
            movs_existentes = meta_existente.get("detalhes_sisdpu", {}).get("movimentacoes", []) or []
            for mov in movs_existentes:
                for doc in mov.get("documentos", []) or []:
                    file_id = sisdpu_client.extrair_id_arquivo(doc.get("urlDownload", ""))
                    if file_id:
                        seq_por_file_id[file_id] = mov.get("seq")
        except Exception as e:
            log(f"  [anexos-desde] aviso: nao foi possivel ler seqs do metadata existente: {e}")

    # 3) Baixa anexos com filtro por data (ignora limite)
    pasta.mkdir(parents=True, exist_ok=True)
    resultado_anexos = await _baixar_anexos_sisdpu(
        pasta / "Arquivos",
        log,
        limite=MAX_ANEXOS_POR_PAJ,  # ignorado quando filtro_data_min e' usado
        deve_cancelar=deve_cancelar,
        filtro_data_min=data_inicio,
        seq_por_file_id=seq_por_file_id,
        rodar_ocr=rodar_ocr,
    )

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    # 4) Atualiza metadata: pode reduzir/eliminar overflow
    meta_path = pasta / "metadata.json"
    overflow_residual = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ov_anterior = meta.get("anexos_extras", {}) or {}
            data_min_disponivel_str = ov_anterior.get("data_min_disponivel", "")
            try:
                data_min_disponivel = _dt.date.fromisoformat(data_min_disponivel_str)
            except (ValueError, TypeError):
                data_min_disponivel = None
            # Se o usuario escolheu uma data <= data_min_disponivel, agora todos
            # os anexos foram baixados — limpa o flag.
            if data_min_disponivel and data_inicio <= data_min_disponivel:
                meta.pop("anexos_extras", None)
            else:
                # Ainda restam anexos mais antigos que data_inicio. Atualiza
                # data_min_baixada para a nova data de corte do usuario.
                if "anexos_extras" in meta:
                    meta["anexos_extras"]["data_min_baixada"] = data_inicio.isoformat()
                    meta["anexos_extras"]["baixados"] = (
                        ov_anterior.get("baixados", 0) + resultado_anexos["baixados"]
                    )
                    overflow_residual = meta["anexos_extras"]
            meta["n_anexos_sisdpu"] = resultado_anexos["total_no_sisdpu"]
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            log(f"  [warn] falha ao atualizar metadata: {type(e).__name__}: {e}")

    log("=" * 60)
    log(
        f"[anexos-desde] FIM: {resultado_anexos['baixados']} anexo(s) baixado(s) "
        f"(de {resultado_anexos['total_no_sisdpu']} no SISDPU)"
    )
    return {
        "paj_norm": alvo,
        "total_no_sisdpu": resultado_anexos["total_no_sisdpu"],
        "baixados": resultado_anexos["baixados"],
        "ocr_ok": resultado_anexos["ocr_ok"],
        "overflow_residual": overflow_residual,
    }


async def rodar_baixar_documentos(
    paj_norm: str,
    ids: list[str],
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
) -> dict:
    """Baixa documentos ESPECIFICOS (por ID) de um PAJ ja sincronizado — sem
    ressincronizar nada. Le as URLs direto do metadata.json existente
    (populado pela extracao por seq de uma sincronizacao anterior, completa
    ou rapida) e baixa so o que foi pedido.

    Bem mais leve que uma sincronizacao completa: nao abre a caixa de
    entrada, nao reabre o detalhamento do PAJ, nao reextrai movimentacoes
    nem documentos — so login (reaproveitando sessao se ja estiver valida) +
    fetch dos arquivos pedidos. Usado pelos botoes "Baixar todos"/"Baixar
    selecionados" da coluna Documentos.

    `ids`: lista de IDs de arquivo (numero na URL de download do SISDPU —
    o mesmo usado no nome do arquivo local "seqNNN - ID - nome").

    Retorna dict: {baixados, erros, total, nao_encontrados, cancelado?}.
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        log(f"[baixar-docs] PAJ {paj_norm} nao encontrado (sem metadata.json)")
        return {"baixados": 0, "erros": ["PAJ nao encontrado"], "total": 0, "nao_encontrados": list(ids)}

    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[baixar-docs] erro ao ler metadata: {type(e).__name__}: {e}")
        return {"baixados": 0, "erros": [str(e)], "total": 0, "nao_encontrados": list(ids)}

    movs = metadata.get("detalhes_sisdpu", {}).get("movimentacoes", []) or []

    # Monta a lista de download + o mapa seq_por_file_id a partir do que ja
    # esta indexado no metadata — mesma fonte da coluna "Documentos" no painel,
    # entao nao precisa reabrir o SISDPU pra descobrir nada disso de novo.
    ids_pedidos = set(ids)
    lista_arquivos: list[dict] = []
    seq_por_file_id: dict[str, int] = {}
    ids_encontrados: set[str] = set()
    for mov in movs:
        seq = mov.get("seq")
        for doc in mov.get("documentos", []) or []:
            file_id = sisdpu_client.extrair_id_arquivo(doc.get("urlDownload", ""))
            if not file_id:
                continue
            seq_por_file_id[file_id] = seq
            if file_id in ids_pedidos:
                ids_encontrados.add(file_id)
                lista_arquivos.append({
                    "seq": seq,
                    "nomeArq": doc.get("nomeArquivo", "arquivo"),
                    "href": doc.get("urlDownload", ""),
                })

    nao_encontrados = [i for i in ids if i not in ids_encontrados]
    if nao_encontrados:
        log(f"[baixar-docs] aviso: {len(nao_encontrados)} id(s) nao encontrado(s) no metadata: {', '.join(nao_encontrados)}")

    if not lista_arquivos:
        log("[baixar-docs] nenhum documento valido pra baixar")
        return {"baixados": 0, "erros": [], "total": 0, "nao_encontrados": nao_encontrados}

    log(f"[baixar-docs] {len(lista_arquivos)} documento(s) selecionado(s) — entrando no SISDPU...")
    try:
        await sisdpu_client._ensure_logged_in()
    except Exception as e:
        log(f"[ERRO] login: {type(e).__name__}: {e}")
        return {"baixados": 0, "erros": [str(e)], "total": len(lista_arquivos), "nao_encontrados": nao_encontrados}

    if deve_cancelar is not None and deve_cancelar():
        log("[baixar-docs] cancelado antes do download")
        return {
            "baixados": 0,
            "erros": [],
            "total": len(lista_arquivos),
            "nao_encontrados": nao_encontrados,
            "cancelado": True,
        }

    resultado = await sisdpu_client.baixar_todos_arquivos(
        pasta / "Arquivos",
        lista_arquivos=lista_arquivos,
        log=log,
        seq_por_file_id=seq_por_file_id,
    )

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    resultado["nao_encontrados"] = nao_encontrados
    log("=" * 60)
    log(f"[baixar-docs] FIM: {resultado['baixados']}/{resultado['total']} documento(s) baixado(s)")
    return resultado


async def rodar_paj_unico(
    paj_identificador: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
) -> dict:
    """Sincroniza UM unico PAJ (util pra teste/re-sync pontual sem rodar a caixa inteira).

    `paj_identificador` aceita tanto '2021/044-00635' quanto 'PAJ-2021-044-00635'.

    Exige que o PAJ esteja na caixa de entrada atual (para localizar o link
    na tela da caixa e clicar nele).
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    resumo: dict = {"paj": "", "processado": False, "anexos": 0, "baixados": 0, "erros": []}

    # Normaliza identificador para o formato "2021/044-00635"
    alvo = paj_identificador.strip()
    if alvo.startswith("PAJ-"):
        resto = alvo[4:]  # '2021-044-00635'
        partes = resto.split("-", 1)  # ['2021', '044-00635']
        if len(partes) == 2:
            alvo = f"{partes[0]}/{partes[1]}"

    resumo["paj"] = alvo

    log(f"[sync-paj] alvo: {alvo}")

    try:
        log("[sync-paj] abrindo caixa de entrada...")
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        resumo["erros"].append(f"caixa: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    itens_raw = data.get("itens_tabela", []) or []
    item_alvo = None
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and it.get("paj") == alvo:
            item_alvo = it
            break

    if not item_alvo:
        log(f"[sync-paj] PAJ {alvo} NÃO encontrado na caixa atual")
        resumo["erros"].append("PAJ fora da caixa")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    if not baixar_anexos:
        log("[sync-paj] modo RÁPIDO — anexos NÃO serão baixados")

    try:
        paj_norm, ok = await _processar_paj(
            item_alvo,
            log,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
        )
        if ok:
            resumo["processado"] = True
            # Recupera contadores do metadata recem-gravado
            pasta = PAJS_DIR / paj_norm
            try:
                meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
                resumo["anexos"] = meta.get("n_anexos_sisdpu", 0)
            except Exception:
                pass
            # Conta arquivos em Arquivos/
            pasta_pecas = pasta / "Arquivos"
            if pasta_pecas.exists():
                resumo["baixados"] = sum(
                    1 for f in pasta_pecas.iterdir() if f.is_file() and f.suffix.lower() != ".txt"
                )
    except Exception as e:
        log(f"  [EXC] {type(e).__name__}: {e}")
        resumo["erros"].append(f"{alvo}: {e}")

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    log("=" * 60)
    log(
        f"[sync-paj] FIM: processado={resumo['processado']}, "
        f"anexos_listados={resumo['anexos']}, "
        f"arquivos_baixados={resumo['baixados']}, "
        f"erros={len(resumo['erros'])}"
    )
    return resumo


async def rodar_paj_via_busca_global(
    paj_identificador: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
) -> dict:
    """Sincroniza UM PAJ via o campo de pesquisa global do SIS-DPU.

    Diferente de `rodar_paj_unico`, NAO depende do PAJ estar na caixa de
    entrada do defensor — entra pela "Pesquisa Rapida" do header e cai
    direto no detalhamento do processo. Usado para PAJs da watchlist, que
    podem ter sido concluidos (fora da caixa) ou nunca ter passado por ela
    (cadastrados individualmente).

    Preservacao de metadata vs `rodar_paj_unico`:
      - `em_caixa_atual` — preservado do metadata anterior (a busca global
        nao responde se o PAJ ainda esta na caixa do defensor).
      - `etiqueta_sisdpu` — preservada da rodada anterior (campo de pesquisa
        nao expoe etiquetas da caixa).
      - `via_watchlist`, `concluido_em`, `n_anexos_removidos` — preservados.

    `paj_identificador` aceita 'PAJ-2026-044-00311' ou '2026/044-00311'.
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    resumo: dict = {"paj": "", "processado": False, "anexos": 0, "baixados": 0, "erros": []}

    alvo = paj_identificador.strip()
    if alvo.startswith("PAJ-"):
        resto = alvo[4:]
        partes = resto.split("-", 1)
        if len(partes) == 2:
            alvo = f"{partes[0]}/{partes[1]}"

    dec = _decompor_paj(alvo)
    if not dec:
        log(f"[busca-global] identificador invalido: {paj_identificador}")
        resumo["erros"].append("identificador invalido")
        return resumo
    ano, unidade, numero = dec
    paj_norm = _normalizar_paj(alvo)
    resumo["paj"] = alvo

    log(f"[busca-global] alvo: {alvo}")
    log("[busca-global] pesquisando no campo global do SIS-DPU...")

    try:
        det = await sisdpu_client.buscar_paj_global(numero, ano, unidade)
    except Exception as e:
        log(f"[FATAL] falha na busca global: {type(e).__name__}: {e}")
        resumo["erros"].append(f"busca: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    if det.get("erro"):
        log(f"[busca-global] sem resultado: {det['erro']}")
        resumo["erros"].append(det["erro"])
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return resumo

    if not baixar_anexos:
        log("[busca-global] modo RÁPIDO — anexos NÃO serão baixados")

    # Item sintetico — `etiqueta` ficaria nulo aqui (busca global nao expoe);
    # _processar_paj_pos_detalhamento (com via_busca_global=True) le a etiqueta
    # antiga do metadata.json existente, entao nao se perde a info entre syncs.
    item_sintetico = {"paj": alvo}

    try:
        _, ok = await _processar_paj_pos_detalhamento(
            item_sintetico,
            det,
            log,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
            via_busca_global=True,
        )
        if ok:
            resumo["processado"] = True
            pasta = PAJS_DIR / paj_norm
            try:
                meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
                resumo["anexos"] = meta.get("n_anexos_sisdpu", 0)
            except Exception:
                pass
            pasta_pecas = pasta / "Arquivos"
            if pasta_pecas.exists():
                resumo["baixados"] = sum(
                    1 for f in pasta_pecas.iterdir() if f.is_file() and f.suffix.lower() != ".txt"
                )
    except Exception as e:
        log(f"  [EXC] {type(e).__name__}: {e}")
        resumo["erros"].append(f"{alvo}: {e}")

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    log("=" * 60)
    log(
        f"[busca-global] FIM: processado={resumo['processado']}, "
        f"anexos_listados={resumo['anexos']}, "
        f"arquivos_baixados={resumo['baixados']}, "
        f"erros={len(resumo['erros'])}"
    )
    return resumo


async def rodar_watchlist(
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
) -> dict:
    """Sincroniza todos os PAJs ATIVOS da watchlist via busca global do SIS-DPU.

    Itera serialmente — entre PAJs, consulta `deve_cancelar` pra parar de
    forma limpa. Reusa `rodar_paj_via_busca_global` (cada PAJ refaz login
    com sessao fresca; e' caro mas garante estado limpo).

    PAJs encerrados (`status=encerrado`) sao ignorados — a watchlist mantem
    o registro pra historico, mas a sync so atualiza os ativos.
    """
    from services import watchlist_service

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    resumo: dict = {
        "total": 0,
        "processados": 0,
        "anexos_baixados": 0,
        "erros": [],
        "cancelado": False,
    }

    itens = [i for i in watchlist_service.listar() if i.get("status") == "ativo"]
    resumo["total"] = len(itens)

    if not itens:
        log("[sync-watchlist] nenhum PAJ ativo na watchlist — nada a fazer")
        return resumo

    log(f"[sync-watchlist] {len(itens)} PAJ(s) ativo(s) na watchlist")
    if not baixar_anexos:
        log("[sync-watchlist] modo RÁPIDO — anexos NÃO serão baixados")

    for idx, item in enumerate(itens, start=1):
        if deve_cancelar is not None and deve_cancelar():
            log(f"[sync-watchlist] CANCELADO apos {idx - 1} de {len(itens)}")
            resumo["cancelado"] = True
            break

        paj_norm = item.get("paj_norm", "")
        log("-" * 60)
        log(f"[sync-watchlist] ({idx}/{len(itens)}) {paj_norm}")

        try:
            r = await rodar_paj_via_busca_global(
                paj_norm,
                log_callback,
                deve_cancelar=deve_cancelar,
                baixar_anexos=baixar_anexos,
                rodar_ocr=rodar_ocr,
            )
            if r.get("processado"):
                resumo["processados"] += 1
                resumo["anexos_baixados"] += r.get("baixados", 0)
            else:
                erros_paj = r.get("erros", []) or ["nao processado"]
                resumo["erros"].append(f"{paj_norm}: {'; '.join(erros_paj)}")
        except Exception as e:
            log(f"[sync-watchlist] EXC em {paj_norm}: {type(e).__name__}: {e}")
            resumo["erros"].append(f"{paj_norm}: {type(e).__name__}: {e}")

    log("=" * 60)
    log(
        f"[sync-watchlist] FIM: processados={resumo['processados']} de "
        f"{resumo['total']}, anexos_baixados={resumo['anexos_baixados']}, "
        f"erros={len(resumo['erros'])}" + (" (cancelado)" if resumo["cancelado"] else "")
    )
    return resumo


async def buscar_resumo_via_busca(
    paj_identificador: str,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Busca um PAJ via campo de pesquisa global do SIS-DPU e grava um
    metadata.json minimo com os dados do cabecalho. NAO baixa anexos nem
    roda OCR — e' a versao "leve" usada quando o usuario adiciona um PAJ
    a watchlist e o sistema precisa popular a aba Resumo.

    O metadata recebe `via_watchlist: True` para que o dashboard possa
    esconder esse PAJ da listagem geral (so aparece na watchlist e via URL
    direta /paj/<id>). Se uma sync de caixa subsequente reencontrar o PAJ,
    `em_caixa_atual` sera setado e a flag perde o efeito de ocultacao.

    `paj_identificador` aceita 'PAJ-2026-044-00311' ou '2026/044-00311'.
    Retorna {paj_norm, ok, mensagem} — `ok=True` se metadata foi gravado.
    """

    def log(msg: str) -> None:
        if log_callback is not None:
            with contextlib.suppress(Exception):
                log_callback(msg)

    alvo = paj_identificador.strip()
    if alvo.startswith("PAJ-"):
        resto = alvo[4:]
        partes = resto.split("-", 1)
        if len(partes) == 2:
            alvo = f"{partes[0]}/{partes[1]}"

    paj_norm = _normalizar_paj(alvo)
    componentes = _decompor_paj(alvo)
    if not componentes:
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": f"identificador invalido: {paj_identificador}",
        }

    ano, unidade, numero = componentes
    log(f"[busca-paj] pesquisando {alvo} no SIS via campo global...")

    try:
        det = await sisdpu_client.buscar_paj_global(numero, ano, unidade)
    except Exception as e:
        log(f"[busca-paj] FALHA: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": f"erro ao acessar SIS-DPU: {type(e).__name__}: {e}",
        }

    if det.get("erro"):
        log(f"[busca-paj] sem resultado: {det['erro']}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": det["erro"],
        }

    # Item sintetico (PAJ nao estava na caixa, entao nao temos etiqueta/oficio
    # vindos de la — usamos so o numero do PAJ; o restante vem do detalhamento).
    item_sintetico = {"paj": alvo}
    texto_sisdpu = _montar_sisdpu_txt(item_sintetico, det)

    pasta = PAJS_DIR / paj_norm
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "sisdpu.txt").write_text(texto_sisdpu, encoding="utf-8")

    try:
        metadata = parser.montar_metadata(paj_norm, texto_sisdpu)
    except Exception as e:
        log(f"[busca-paj] ERRO parser: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            await sisdpu_client.fechar()
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": f"erro ao processar metadata: {e}",
        }

    metadata["sisdpu_raw"] = det

    # Se temos movimentacoes estruturadas do sisdpu_client, usa direto sem perda
    if det.get("movimentacoes"):
        mov_estruturadas = parser.parsear_movimentacoes_estruturadas(det["movimentacoes"])
        if "detalhes_sisdpu" not in metadata:
            metadata["detalhes_sisdpu"] = {}
        metadata["detalhes_sisdpu"]["movimentacoes"] = mov_estruturadas

    # Marca origem: PAJ nao veio da caixa atual, foi puxado por busca explicita
    # do defensor (via watchlist). Dashboard usa essa flag para esconder.
    metadata["via_watchlist"] = True
    # Nao seta em_caixa_atual — fica ausente. Se uma sync regular encontrar
    # esse PAJ na caixa depois, ela seta True e o dashboard volta a mostrar.

    (pasta / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    with contextlib.suppress(Exception):
        await sisdpu_client.fechar()

    log(f"[busca-paj] OK: {paj_norm} (assistido={metadata.get('assistido_caixa', '?')[:40]})")
    return {
        "paj_norm": paj_norm,
        "ok": True,
        "mensagem": "PAJ encontrado e dados do cabecalho gravados",
    }
