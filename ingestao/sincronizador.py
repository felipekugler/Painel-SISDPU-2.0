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
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable

from config import (
    MAX_ANEXOS_POR_PAJ,
    PAJS_DIR,
    RATE_LIMIT_SISDPU,
    TIMEOUT_OCR_POR_PAGINA_SEG,
    TIMEOUT_TOTAL,
)
from ingestao import ocr, parser, sisdpu_client, sisdpu_movtram
from ingestao.sisdpu2_fonte import ProxyFonte
import contextlib

# Fonte de dados trocável (Configurações › Fonte de dados): com "mcp", as
# leituras vêm do MCP do SIS-DPU 2.0 no MESMO formato da raspagem; downloads
# de PDF e o resto continuam no legado. Ver ingestao/sisdpu2_fonte.py.
sisdpu_client = ProxyFonte(sisdpu_client)
sisdpu_movtram = ProxyFonte(sisdpu_movtram)

# Worker de vida curta e SEM rede usado como 1ª tentativa de gravação abaixo —
# mesmo workaround provisório do FortiClient (ver services.intimacao_service.
# _gravar_via_subprocesso, 2026-08-28): o antivírus bloqueia o PROCESSO DO
# SERVIDOR (app.py, porta 8001 aberta) de gravar/substituir arquivo, mas um
# processo Python de vida curta sem socket de rede grava livremente na mesma
# pasta. Confirmado ao vivo 2026-10-05: sync avulso de PAJ-2025-029-01045
# (disparado por abrirPajPainel() ao clicar no número do PAJ na tabela de
# Intimações de Prazos) estourou `PermissionError` gravando sisdpu.txt mesmo
# com as retentativas abaixo já esgotadas — esse caminho nunca tinha sido
# migrado pro worker (só intimacao_service/intimacao_ia_service tinham sido).
_PROJETO_DIR = Path(__file__).resolve().parents[1]
_ARQUIVO_WORKER = _PROJETO_DIR / "services" / "_arquivo_worker.py"


def _gravar_via_subprocesso(destino: Path, texto: str) -> bool:
    """Devolve True em sucesso; False em qualquer falha (worker ausente, erro
    de execução, timeout) — o chamador cai pro método antigo (escrita direta
    + retentativas), então uma falha aqui nunca impede a gravação, só a torna
    mais lenta."""
    if not _ARQUIVO_WORKER.exists():
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(_ARQUIVO_WORKER), str(destino)],
            input=texto.encode("utf-8"), capture_output=True,
            cwd=str(_PROJETO_DIR), timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False

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


def _preservar_descricao_retorno_antiga(mov_estruturadas: list[dict], movs_antigas: list[dict]) -> None:
    """Preserva `descricao_retorno` de uma sincronizacao ANTERIOR pra
    movimentacoes que vieram vazias nesta sincronizacao — mutando
    `mov_estruturadas` in-place.

    `descricao_retorno` (o texto real de um "Atendimento de retorno") vem de
    uma segunda passagem no SISDPU que clica pra expandir cada linha de
    retorno e le o conteudo — sujeita a race condition quando o PAJ tem
    varias movimentacoes de retorno (AJAX em paralelo; ver `_wait_pf_ajax`
    em `sisdpu_client.py`). Sem isso, uma sincronizacao que tropecasse nessa
    race iria APAGAR um valor que uma sincronizacao anterior ja tinha
    capturado certo — mesmo padrao de preservacao ja usado acima pra
    `foto_assistido` e pra `whatsapp_numeros`."""
    antigas_por_seq = {
        str(mov_antiga.get("seq")): mov_antiga["descricao_retorno"]
        for mov_antiga in movs_antigas
        if mov_antiga.get("descricao_retorno")
    }
    for mov in mov_estruturadas:
        if not mov.get("descricao_retorno"):
            antiga = antigas_por_seq.get(str(mov.get("seq")))
            if antiga:
                mov["descricao_retorno"] = antiga


def _montar_contatos(det: dict, meta_antiga: dict) -> dict:
    """Monta o bloco `contatos` do metadata a partir do `det` fresco do SISDPU.

    App SISDPU / Telefone / E-mail / Endereço vêm direto da extração desta
    sincronização (sisdpu_client já visitou o Cadastro de Pessoa Física de
    cada assistido) — mas, se a extração desta rodada não devolver nada
    (instabilidade pontual, mesma classe de bug já vista em narrativa/foto),
    preserva o que já estava gravado em vez de apagar. WhatsApp é a ÚNICA
    exceção INTENCIONAL: o SISDPU não distingue "este telefone é WhatsApp" de
    um telefone comum — é uma escolha manual do usuário (ver
    `services/contatos_service.py` / endpoint de definir WhatsApp) — por isso
    `whatsapp_numeros` é lido do metadata ANTERIOR (preservado através de
    re-sincronizações) em vez de recalculado aqui.

    Retorna {"contatos": {...5 bool...}, "telefones": [...], "emails": [...],
    "enderecos": [...], "whatsapp_numeros": [...]} pronto pra gravar direto em
    `metadata` (chaves de topo, sobrevivem a `metadata["sisdpu_raw"] = det`).
    """
    telefones = det.get("telefones") or meta_antiga.get("telefones") or []
    emails = det.get("emails") or meta_antiga.get("emails") or []
    enderecos = det.get("enderecos") or meta_antiga.get("enderecos") or []
    whatsapp_numeros = meta_antiga.get("whatsapp_numeros") or []
    return {
        "contatos": {
            "app_sisdpu": bool(det.get("contato_app_sisdpu")),
            "telefone": bool(telefones),
            "whatsapp": bool(whatsapp_numeros),
            "email": bool(emails),
            "endereco": bool(enderecos),
        },
        "telefones": telefones,
        "emails": emails,
        "enderecos": enderecos,
        "whatsapp_numeros": whatsapp_numeros,
    }


async def _escrever_texto_com_retry(
    caminho: Path, texto: str, tentativas: int = 4, espera_inicial: float = 0.3
) -> None:
    """Grava texto num arquivo com retry/backoff — protege contra bloqueio
    TRANSITORIO do arquivo no Windows (antivirus escaneando um arquivo/pasta
    recem-criado, indexador, cliente de backup/nuvem), que pode disparar
    `PermissionError: [Errno 13]` mesmo sem NENHUMA outra execucao do painel
    disputando o arquivo — confirmado em producao: sync de um PAJ novo (pasta
    recem-criada por `pasta.mkdir()` linhas antes) falhou gravando sisdpu.txt
    com o lock global (`_sync_lock`) livre, ou seja, sem concorrencia real
    entre sincronizacoes — sinal de bloqueio externo passageiro, nao de uma
    segunda escrita simultanea.

    1ª tentativa é sempre via `_gravar_via_subprocesso` (worker sem rede, ver
    acima) — escapa do bloqueio PERSISTENTE do FortiClient no processo do
    servidor, que as retentativas abaixo (pensadas pra bloqueio transitorio)
    nunca resolviam sozinhas. Só cai pra escrita direta+retry se o worker
    falhar por qualquer outro motivo.

    `await asyncio.sleep` (nao `time.sleep`) entre tentativas — isso roda
    dentro do event loop do FastAPI/uvicorn, um sleep sincrono travaria TODAS
    as requisicoes do painel durante a espera."""
    if await asyncio.to_thread(_gravar_via_subprocesso, caminho, texto):
        return

    espera = espera_inicial
    for tentativa in range(1, tentativas + 1):
        try:
            caminho.write_text(texto, encoding="utf-8")
            return
        except PermissionError:
            if tentativa == tentativas:
                raise
            await asyncio.sleep(espera)
            espera *= 2


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
    movs: list[dict],
    deve_cancelar: Callable[[], bool] | None = None,
    filtro_data_min=None,
    seq_por_file_id: dict[str, int] | None = None,
    rodar_ocr: bool = False,
) -> dict:
    """Baixa os anexos do PAJ atual, a partir da relacao de documentos JA
    extraida por sequencial de movimentacao (`movs`, cada um com um campo
    `documentos`: [{nomeArquivo, urlDownload, commandLinkId, ...}]).

    ATE' 2026-10-09 esta funcao reabria o dialogo "Arquivos" do PAJ (uma
    LISTAGEM AGREGADA separada, via `sisdpu_client.listar_arquivos_paj()`)
    como segunda fonte dos documentos a baixar — uma segunda passagem pela
    MESMA informacao que a extracao por sequencial (`movs`, parametro desta
    funcao) ja fornece. Essa segunda passagem foi REMOVIDA (confirmado:
    a aba "Arquivos" e' so' uma compilacao do PAJ inteiro numa pagina so,
    nao ha' divergencia de cobertura possivel em relacao a extracao por
    sequencial) — alem de redundante, ela usava um "sequencial" MENOS
    confiavel pra documentos sem link de download direto (imagens/.docx via
    `<p:commandLink>` do PrimeFaces), e um ID derivado do HREF (sempre
    vazio pra esse tipo de documento) como chave de um mapa GLOBAL — dois
    bugs de colisao de ID ja confirmados ao vivo nessa segunda passagem
    (um documento de uma seq podia ser baixado usando o popup de OUTRA
    seq, por aquele mapa global colidir). A extracao por sequencial nao
    tem esse problema: o `seq` de cada documento vem direto da
    movimentacao dona dele.

    PRE-CONDICAO: nenhuma mais — nao abre pagina/dialogo nenhum (so' usa os
    dados ja' extraidos). O antigo requisito ("page no detalhamento do
    PAJ") so' se aplica indiretamente, atraves de quem populou `movs`.

    `seq_por_file_id`: repassado a `sisdpu_client.baixar_todos_arquivos` pra
    nomear o arquivo local com a seq REAL da movimentacao — so' usado hoje
    pros documentos com link de download direto. Documentos sem link direto
    ja' vem com o `seq` certo desde a origem (ver `baixar_todos_arquivos`) e
    ignoram este mapa.

    Comportamento:
      - Achata `movs` numa lista de 1 item por documento anexado.
      - Ordena os itens por DATA (da movimentacao-dona) decrescente.
      - Se `filtro_data_min` (date) for informado, baixa SO os com data >= filtro
        — sem aplicar `limite` (modo "completar a partir de uma data").
      - Caso contrario, aplica `limite`: baixa os `limite` mais recentes; se
        houver mais que isso, registra overflow no resultado.

    Retorna dict:
      {
        "total_no_sisdpu": int,         # total de documentos indexados
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

    # Achata `movs` numa lista de 1 item por documento — formato que
    # `sisdpu_client.baixar_todos_arquivos` ja espera (seq/data/nomeArq/
    # href/commandLinkId). A "data" de cada documento e' a da propria
    # movimentacao-dona (os documentos nao tem data individual) — usada so'
    # pra ordenacao/overflow/filtro-por-data, nao pra exibicao.
    lista: list[dict] = []
    for mov in movs:
        seq = mov.get("seq")
        data_mov = (mov.get("data_original") or "").split(" ")[0]
        for doc in mov.get("documentos") or []:
            nome_arq = (doc.get("nomeArquivo") or "").strip()
            if not nome_arq or nome_arq == "Nenhum arquivo encontrado.":
                continue
            lista.append({
                "seq": seq,
                "data": data_mov,
                "nomeArq": nome_arq,
                "href": doc.get("urlDownload", ""),
                "commandLinkId": doc.get("commandLinkId"),
            })

    total = len(lista)
    if total == 0:
        log("  [anexos] nenhum documento indexado pela extração por sequencial")
        return {"total_no_sisdpu": 0, "baixados": 0, "ocr_ok": 0, "overflow": None, "falhou": False}

    log(f"  [anexos] {total} documento(s) indexado(s) pela extração por sequencial")

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
    interrompido = False

    # Usa nova estrategia: fetch() + servidor local (mais robusto que Playwright clicks)
    log(f"  [anexos] baixando {len(lista)} arquivo(s) via fetch...")
    resultado_fetch = await sisdpu_client.baixar_todos_arquivos(
        pasta_pecas, lista_arquivos=lista, log=log, seq_por_file_id=seq_por_file_id
    )

    if resultado_fetch.get("total", 0) > 0:
        baixados = resultado_fetch.get("baixados", 0)
        pulados_fetch = resultado_fetch.get("pulados", 0)
        total = resultado_fetch['total']
        erros_count = len(resultado_fetch.get("erros", []))
        log("")
        log(
            f"✓ {baixados}/{total} arquivo(s) baixado(s), {pulados_fetch} já existente(s) "
            f"pulado(s)   /   {erros_count} erros"
        )

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
        det = {"erro": f"{type(e).__name__}: {e}"}

    if det.get("erro"):
        # Este item veio da PROPRIA listagem da caixa (lida ha poucos segundos
        # no mesmo `rodar()`) — "nao encontrado"/erro aqui e' sinal de sessao
        # ou pagina quebrada (ex.: erro de navegacao no PAJ anterior deixou a
        # page num estado inconsistente), nao de o PAJ ter realmente saido da
        # caixa. Forca reconexao (fecha navegador + refaz login do zero) e
        # tenta mais UMA vez antes de desistir deste PAJ — sem isso, um unico
        # erro de timing num PAJ cascateava "nao encontrado" pro resto inteiro
        # da rodada (visto em produção: item 1 falhou e os 47 seguintes
        # falharam em sequência, 0/48 processados).
        log(f"  [ERRO] {det['erro']} — reconectando e tentando novamente...")
        with contextlib.suppress(Exception):
            await sisdpu_client.reconectar()
        try:
            det = await sisdpu_client.movimentacoes_paj(
                numero, ano, unidade, esperas_curtas=esperas_curtas)
        except Exception as e2:
            log(f"  [ERRO] retry movimentacoes_paj {paj}: {type(e2).__name__}: {e2}")
            return (paj_norm, False)
        if det.get("erro"):
            log(f"  [ERRO] {det['erro']} (persistiu após reconexão)")
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


def _linha_caixa_das_tramitacoes(meta: dict, tramitacoes: list[dict]) -> None:
    """Descrição e data da coluna da caixa a partir do trâmite MAIS RECENTE
    pendente. Quem atualiza só a lista de trâmites (releitura dos trâmites
    do PAJ) precisa levar junto a linha da caixa — senão o PAJ aparecia na
    caixa sem descrição e com a data de uma movimentação antiga (16/10/2026,
    PAJ 2026/029-03459: trâmite "Minuta de inicial." de 13/10)."""
    def _quando(t: dict) -> str:
        d = parser.parse_data_br(t.get("data_envio") or "")
        h = re.search(r"\d{2}:\d{2}(:\d{2})?", t.get("data_envio") or "")
        return d + (h.group(0) if h else "")
    if not tramitacoes:
        return
    ult = max(tramitacoes, key=_quando)
    meta["desc_mov_caixa"] = (ult.get("descricao") or "").strip()[:500]
    data_iso = parser.parse_data_br(ult.get("data_envio") or "")
    if data_iso:
        meta["data_mov_caixa"] = data_iso


def _atualizar_linha_caixa(meta_path, item: dict) -> None:
    """PAJ pulado na sync da caixa (sem trâmite novo): mesmo assim grava a
    descrição e a data do trâmite exatamente como vieram na linha da caixa do
    SIS-DPU (colunas "Descrição"/"Data" do dashboard) — custo zero, a caixa já
    foi lida. Só escreve se mudou."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sem metadata, a próxima sync completa grava
        return
    mudou = False
    if "desc" in item or "descricao" in item:
        desc = ((item.get("desc") if "desc" in item else item.get("descricao")) or "").strip()[:500]
        if meta.get("desc_mov_caixa") != desc:
            meta["desc_mov_caixa"] = desc
            mudou = True
    data_iso = parser.parse_data_br(item.get("data") or "")
    if data_iso and meta.get("data_mov_caixa") != data_iso:
        meta["data_mov_caixa"] = data_iso
        mudou = True
    if mudou:
        try:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


async def _processar_paj_pos_detalhamento(
    item: dict,
    det: dict,
    log: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool = True,
    via_busca_global: bool = False,
    fora_da_caixa_confirmado: bool = False,
) -> tuple[str, bool]:
    """Etapa pos-detalhamento: a page do Playwright ja esta no detalhamento do
    PAJ (via clique na caixa ou via campo de pesquisa global) e `det` ja foi
    extraido. Grava sisdpu.txt/metadata.json, detecta prazos, baixa anexos.

    `via_busca_global=True`: por padrao NAO sobrescreve `em_caixa_atual`
    (preserva o valor anterior — a busca global, por si so, nao responde se o
    PAJ ainda esta na caixa do defensor), e preserva `etiqueta_sisdpu` do
    metadata antigo (o campo de pesquisa do SIS nao expoe a etiqueta da caixa).

    `fora_da_caixa_confirmado=True`: usado quando o CHAMADOR ja verificou a
    listagem da caixa segundos antes (caso do fallback de `rodar_paj_unico` —
    abriu a caixa, nao achou o PAJ, so' ENTAO caiu na busca global) e por isso
    sabe com certeza que o PAJ NAO esta mais na caixa. Nesse caso, sobrescreve
    `em_caixa_atual=False` (com `concluido_em`) em vez de preservar o valor
    antigo. Sem essa distincao, um PAJ que saiu da caixa MAS foi ressincronizado
    via "Sincronizar PAJ" (que cai na busca global por nao acha-lo na caixa)
    ficava com `em_caixa_atual=True` PRA SEMPRE (o valor antigo, de quando
    ainda estava la), reaparecendo indefinidamente no dashboard como se ainda
    estivesse na caixa — bug real observado em producao. Diferente do uso pela
    watchlist/`rodar_watchlist`, onde a busca global e' o UNICO caminho (nunca
    se checou a caixa) e preservar o valor antigo continua sendo o correto.
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
    await _escrever_texto_com_retry(pasta / "sisdpu.txt", texto_sisdpu)

    try:
        metadata = parser.montar_metadata(paj_norm, texto_sisdpu)
    except Exception as e:
        log(f"  [ERRO] parser metadata: {type(e).__name__}: {e}")
        metadata = {"paj": paj, "paj_norm": paj_norm, "erro_parser": str(e)}

    # Snapshot da estrutura bruta devolvida pelo SISDPU para diagnostico/posteridade
    metadata["sisdpu_raw"] = det

    # "Última sincronização" (templates/paj_detail.html) — timestamp real de
    # quando o PAINEL efetivamente conversou com o SISDPU sobre este PAJ,
    # atualizado em TODA sincronização que passa por aqui (caixa, avulsa via
    # busca global, watchlist). Diferente de `data_mov_caixa` (a data da
    # PRÓPRIA linha do trâmite na caixa — só existe/atualiza enquanto o PAJ
    # está ativamente na caixa): usar `data_mov_caixa` como "última
    # sincronização" era o bug relatado pelo usuário 2026-09-23 — um PAJ
    # concluído (fora da caixa, ex. PAJ-2025-029-03370) ou sincronizado via
    # busca global nunca atualiza `data_mov_caixa` de novo, então a UI
    # mostrava sempre a mesma data antiga mesmo após uma sync levíssima bem
    # sucedida.
    import datetime as _dt_sinc

    metadata["ultima_sincronizacao_em"] = _dt_sinc.datetime.now().isoformat(timespec="seconds")

    # Campos de `sisdpu_raw` cuja extracao e' fragil (dependem do layout/estado
    # da pagina de detalhamento no momento exato do evaluate() — ver
    # sisdpu_client.py, movimentacoes_paj/buscar_paj_global): se o SISDPU nao
    # devolveu algum deles NESTA sincronizacao, preserva o da sincronizacao
    # anterior em vez de apagar por uma falha pontual de extracao — mesmo
    # raciocinio ja aplicado a foto_assistido logo abaixo. Bug real observado
    # com narrativa: sync leve disparada logo apos uma movimentacao via "Acoes
    # no SISDPU" veio sem narrativa e apagou a que ja existia (2026-08-20,
    # PAJ-2026-029-03259) — auditoria subsequente achou o mesmo padrao de risco
    # nos demais campos abaixo, corrigidos juntos na mesma leva.
    sisdpu_raw_antigo = meta_antiga.get("sisdpu_raw") or {}
    for _campo in ("narrativa", "assistidos_pf", "audiencias", "representantes_legais"):
        if not metadata["sisdpu_raw"].get(_campo) and sisdpu_raw_antigo.get(_campo):
            metadata["sisdpu_raw"][_campo] = sisdpu_raw_antigo[_campo]

    # Assistido/Ofício da caixa: já têm dupla fonte (caixa + detalhamento, ver
    # `_montar_sisdpu_txt`), o que já reduz bastante o risco — mas se AMBAS
    # vierem vazias nesta rodada (ex.: via busca global, que não tem a linha
    # da caixa), cai pro valor anterior em vez de ficar em branco.
    # `assistido_prev` já era capturado no topo desta função, mas nunca tinha
    # sido de fato usado como fallback — fechando esse gap aqui.
    if not metadata.get("assistido_caixa") and assistido_prev:
        metadata["assistido_caixa"] = assistido_prev
    if not metadata.get("oficio_caixa") and meta_antiga.get("oficio_caixa"):
        metadata["oficio_caixa"] = meta_antiga["oficio_caixa"]

    # TODOS os processos judiciais vinculados ao PAJ — direto do `det` (sem reparse
    # de texto, evita perda de dados), mesmo padrão usado para movimentações abaixo.
    # `processo_judicial` (singular, em metadata via parser) continua existindo
    # para os consumidores antigos (compat), representando o ÚLTIMO da lista.
    # Preserva o vínculo anterior se esta rodada não devolveu nenhum (mesmo
    # raciocínio do bloco acima — sem isso, uma falha pontual de extração
    # "desvincularia" o processo judicial e desabilitaria Ações no e-proc /
    # Gerar Minuta até a próxima sync bem-sucedida).
    if det.get("processos_judiciais"):
        metadata["processos_judiciais"] = det["processos_judiciais"]
    elif meta_antiga.get("processos_judiciais"):
        metadata["processos_judiciais"] = meta_antiga["processos_judiciais"]

    # Foto do assistido — vem do `det` como data URI (extraida do <img> do
    # SISDPU). Roda em toda sincronizacao (rapida ou completa), ja que so'
    # requer o `det` do detalhamento, sem passo extra. Nem todo PAJ tem foto
    # cadastrada; quando o SISDPU nao devolve nesta sync, preserva o arquivo
    # de uma sincronizacao anterior em vez de apagar por uma falha pontual.
    foto_src = det.get("foto_assistido_src") or ""
    foto_path = pasta / "foto_assistido.jpg"
    if foto_src.startswith("data:image") and "," in foto_src:
        try:
            foto_path.write_bytes(base64.b64decode(foto_src.split(",", 1)[1]))
            metadata["foto_assistido"] = "foto_assistido.jpg"
        except Exception as e:
            log(f"  [aviso] falha ao salvar foto do assistido: {type(e).__name__}: {e}")
            if foto_path.exists():
                metadata["foto_assistido"] = "foto_assistido.jpg"
    elif foto_path.exists():
        metadata["foto_assistido"] = "foto_assistido.jpg"
    else:
        # Fonte SIS-DPU 2.0: a foto vem da ficha do assistido no MCP (22/10/2026,
        # services/foto_assistido_service.py) — uma consulta por PAJ, só se a
        # pasta ainda não tem foto; avatar genérico é descartado.
        with contextlib.suppress(Exception):
            from services import foto_assistido_service
            _rf = await asyncio.to_thread(foto_assistido_service.garantir_foto, pasta, det)
            if _rf.get("situacao") == "foto":
                metadata["foto_assistido"] = "foto_assistido.jpg"

    # Contatos (App SISDPU/Telefone/WhatsApp/E-mail/Endereço) — extraídos
    # diretamente do SISDPU nesta sincronização (leve ou completa); painel
    # autônomo, não depende de nenhum outro sistema. Ver `_montar_contatos`.
    metadata.update(_montar_contatos(det, meta_antiga))

    # Preenchido abaixo (se houver movimentacoes) e usado no bloco de anexos,
    # mais adiante, pra nomear os arquivos baixados com a seq real.
    seq_por_file_id: dict[str, int] = {}

    # Se temos movimentacoes estruturadas do sisdpu_client, usa direto sem perda
    # (evita parsing regex que perde campos como movimentacao, fases, usuario, anexo)
    if det.get("movimentacoes"):
        mov_estruturadas = parser.parsear_movimentacoes_estruturadas(det["movimentacoes"])
        _preservar_descricao_retorno_antiga(mov_estruturadas, movs_antigas)
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
        # Seqs cujo popup ja foi aberto e CONFIRMADO vazio (sem erro) em uma
        # sync anterior — sem isso, uma seq com icone clicavel mas 0
        # documentos era reprocessada em TODA sincronizacao pra sempre (o
        # dict acima so' cacheia seqs com documento, por ser `dict` vazio =
        # falsy). Persistido em metadata["seqs_sem_documento_confirmado"].
        seqs_sem_doc_antigas = {
            str(s) for s in (meta_antiga.get("seqs_sem_documento_confirmado") or [])
        }
        seqs_conhecidas_para_pular = list(documentos_conhecidos_por_seq.keys()) + list(seqs_sem_doc_antigas)
        # `extrair_documentos=False` (ex.: aba Retornos): PULA a extracao por seq
        # (cara — 1 popup por movimentacao com anexo), mas ainda preserva os
        # documentos ja conhecidos de sync anterior (nada e' perdido). Um sync
        # completo posterior reextrai as seqs que faltarem (custo incremental).
        if extrair_documentos:
            try:
                resultado_docs = await sisdpu_client.extrair_documentos_por_seq(
                    seqs_conhecidos=seqs_conhecidas_para_pular,
                    deve_cancelar=deve_cancelar,
                    log=log,
                )
            except Exception as e:
                log(f"  [docs] erro ao extrair documentos por seq: {type(e).__name__}: {e}")
                resultado_docs = {"falhou": True}
        else:
            log("  [docs] extracao por seq PULADA (sync rapida — documentos conhecidos preservados)")
            resultado_docs = {}

        # Sinaliza falha (contexto da pagina perdido/tabela nao encontrada —
        # ver extrair_documentos_por_seq) pra `_atualizar_tramitacoes_caixa`
        # NAO gravar este PAJ como "em dia": sem isso, a proxima sync completa
        # pularia pra sempre um PAJ cuja extracao de documentos falhou so'
        # porque a tramitacao da caixa nao mudou (visto em producao,
        # 2026-10-08 — PAJs 2026/029-00412 e 2023/029-01897).
        if resultado_docs.get("falhou"):
            metadata["docs_sync_falhou"] = True
        else:
            metadata.pop("docs_sync_falhou", None)

        docs_novos_por_seq = {
            str(mov_doc.get("seq")): mov_doc.get("documentos", [])
            for mov_doc in resultado_docs.get("movimentacoes_com_documentos", [])
        }

        # Acumula (com as antigas) as seqs recem-confirmadas sem documento
        # nesta rodada — persistido abaixo pra beneficiar a PROXIMA sync.
        seqs_sem_doc_novas = {
            str(s) for s in (resultado_docs.get("seqsVerificadosSemDocumento") or [])
        }
        seqs_sem_doc_total = seqs_sem_doc_antigas | seqs_sem_doc_novas
        if seqs_sem_doc_total:
            metadata["seqs_sem_documento_confirmado"] = sorted(seqs_sem_doc_total, key=lambda s: (len(s), s))

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
    elif movs_antigas:
        # SISDPU nao devolveu movimentacoes nesta rodada (extracao falhou —
        # todo PAJ tem ao menos a movimentacao de abertura, entao "vazio de
        # verdade" nao existe aqui) — preserva o historico inteiro da
        # sincronizacao anterior em vez de zera-lo. Mesma auditoria de
        # 2026-08-20 que corrigiu a narrativa: essa e' a perda mais grave
        # possivel desse padrao de bug (histórico inteiro, nao so' um campo).
        log(f"  [aviso] SISDPU nao devolveu movimentacoes nesta sincronizacao "
            f"— preservando {len(movs_antigas)} da sincronizacao anterior")
        if "detalhes_sisdpu" not in metadata:
            metadata["detalhes_sisdpu"] = {}
        metadata["detalhes_sisdpu"]["movimentacoes"] = movs_antigas
        if meta_antiga.get("n_documentos_indexados") is not None:
            metadata["n_documentos_indexados"] = meta_antiga["n_documentos_indexados"]
        if meta_antiga.get("seqs_sem_documento_confirmado"):
            metadata["seqs_sem_documento_confirmado"] = meta_antiga["seqs_sem_documento_confirmado"]

    if via_busca_global:
        if fora_da_caixa_confirmado:
            # Chamador (rodar_paj_unico) ja checou a caixa momentos antes e
            # confirmou que o PAJ nao esta la — nao preserva o `True` antigo
            # (ficaria "em caixa" pra sempre, mesmo tendo saido de verdade).
            if meta_antiga.get("em_caixa_atual") is not False:
                import datetime as _dt

                metadata["em_caixa_atual"] = False
                metadata["concluido_em"] = _dt.datetime.now().isoformat(timespec="seconds")
                metadata["tramitacoes_caixa"] = []
            else:
                metadata["em_caixa_atual"] = False
        else:
            # Busca global nao da informacao sobre estar na caixa atual —
            # preserva o que tinhamos (se houver). Caso do via_watchlist puro,
            # que nunca chegou a checar a caixa.
            if "em_caixa_atual" in meta_antiga:
                metadata["em_caixa_atual"] = meta_antiga["em_caixa_atual"]
        # Tambem nao tem acesso a etiqueta da caixa, entao mantemos a antiga.
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
        # Só o que o SIS-DPU enviou na linha da caixa. Item sem a linha (releitura
        # do PAJ pela busca global) mantém o valor já gravado — nunca outro texto.
        if "desc" in item or "descricao" in item:
            metadata["desc_mov_caixa"] = ((item.get("desc") if "desc" in item else item.get("descricao")) or "").strip()[:500]
        elif "desc_mov_caixa" in meta_antiga:
            metadata["desc_mov_caixa"] = meta_antiga["desc_mov_caixa"]
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

    # Releitura SEM download de anexos (ex.: abrir o PAJ — leitura ao vivo do
    # SIS-DPU 2.0): mantém o que só o download apura. Sem isso, abrir o PAJ
    # depois da sincronização completa apagava `n_anexos_sisdpu` e o "Baixar PAJ
    # Completo" passava a dizer "precisa de sincronização completa" com todos os
    # documentos na pasta (PAJ 2026/029-03051, 14/10/2026).
    if not baixar_anexos:
        for k in ("n_anexos_sisdpu", "anexos_extras", "anexos_sync_falhou"):
            if k in meta_antiga and k not in metadata:
                metadata[k] = meta_antiga[k]

    (pasta / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Anexos SISDPU: usa a relacao de documentos JA extraida por sequencial de
    # movimentacao acima (nao reabre mais a aba "Arquivos" — ver docstring de
    # `_baixar_anexos_sisdpu`).
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

        # A mesma lista que acabou de ser gravada em metadata.json acima
        # (`mov_estruturadas` no caminho normal, `movs_antigas` preservada
        # quando o SISDPU nao devolveu movimentacoes nesta rodada) — le de
        # volta do proprio `metadata` em vez de repetir a bifurcacao,
        # garantindo que os dois caminhos fiquem sempre em sincronia.
        movs_para_anexos = (metadata.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", []) or []

        resultado_anexos = await _baixar_anexos_sisdpu(
            pasta / "Arquivos",
            log,
            limite=MAX_ANEXOS_POR_PAJ,
            movs=movs_para_anexos,
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

        # Mesma logica do `docs_sync_falhou` acima: "0 anexos" por falha do
        # clique no botao "Arquivos" (ver _baixar_anexos_sisdpu) nao pode ser
        # confundido com "0 anexos confirmado" — senao a proxima sync completa
        # pula este PAJ pra sempre (tramitacao da caixa sem mudanca).
        if resultado_anexos.get("falhou"):
            metadata["anexos_sync_falhou"] = True
        else:
            metadata.pop("anexos_sync_falhou", None)

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


# Trâmites concluídos pelo painel nos últimos minutos: id_tramite -> monotonic.
# `rodar()` lê a caixa no INÍCIO e grava `tramitacoes_caixa` no FIM — um
# trâmite concluído no meio da rodada voltava a aparecer no dashboard (bug
# real 14/10/2026, PAJ 2026/029-02674: "Minuta de petição inicial." concluída
# às 13:57 e regravada como pendente pela sync que terminou às 14:01). Um
# id_tramite concluído nunca volta a ficar pendente, então basta filtrá-lo.
_CONCLUIDOS_RECENTES: dict[str, float] = {}
_CONCLUIDOS_JANELA_SEG = 3600


def registrar_tramites_concluidos(ids: list[str]) -> None:
    """Chamado por quem conclui trâmite pelo painel (services/paj_service)."""
    import time as _t

    agora = _t.monotonic()
    for i in ids or []:
        if str(i).strip():
            _CONCLUIDOS_RECENTES[str(i).strip()] = agora


def _ids_concluidos_recentes() -> set[str]:
    import time as _t

    limite = _t.monotonic() - _CONCLUIDOS_JANELA_SEG
    for i in [i for i, ts in _CONCLUIDOS_RECENTES.items() if ts < limite]:
        _CONCLUIDOS_RECENTES.pop(i, None)
    return set(_CONCLUIDOS_RECENTES)


async def _ler_tramitacoes_estruturadas_por_paj(
    log: Callable[[str], None],
) -> dict[str, list[dict]] | None:
    """Lê a caixa via `sisdpu_movtram.listar_caixa_estruturada()` (por linha,
    data-rk — tem `id_tramite` individualizado, diferente do scrape de texto
    solto usado por `sisdpu_client.caixa_de_entrada()`) e agrupa por PAJ.

    Retorna `None` em falha de leitura (nunca `{}` pra "falhou" — `{}` real
    só ocorre se a caixa estiver genuinamente vazia). Chamadores devem tratar
    `None` como "não sei o que está na caixa agora" e cair no caminho mais
    caro/seguro (processar normalmente), nunca assumir "sem mudança" sobre um
    `None` — mesmo espírito do guard em `sisdpu_client.caixa_de_entrada()`
    (ver docstring lá: um erro de leitura nunca pode parecer "vazio").

    Extraída de `_atualizar_tramitacoes_caixa` pra ser chamada 1x no início
    de `rodar()` e reaproveitada tanto pela decisão de pular PAJs sem
    tramitação nova (`id_tramite` inalterado) quanto pela gravação final —
    antes disso, `rodar()` fazia SUA PRÓPRIA leitura de texto solto da caixa
    E esta função fazia uma SEGUNDA leitura estruturada, no fim: duas idas à
    mesma página por sync completa.
    """
    try:
        res = await sisdpu_movtram.listar_caixa_estruturada()
    except Exception as e:  # noqa: BLE001
        log(f"  [tramitacoes] falha ao ler caixa estruturada: {type(e).__name__}: {e}")
        return None
    if not res.get("ok"):
        log(f"  [tramitacoes] {res.get('detalhe', 'não foi possível ler a caixa estruturada')}")
        return None

    return _agrupar_tramitacoes_por_paj(res.get("itens", []) or [])


def _agrupar_tramitacoes_por_paj(itens: list[dict]) -> dict[str, list[dict]]:
    """Linhas de `listar_caixa_estruturada()` → {paj_norm: [tramitação, ...]}
    no formato gravado em `tramitacoes_caixa`. Trâmites concluídos há pouco
    pelo painel ficam de fora (ver `registrar_tramites_concluidos`)."""
    from services import categorias_caixa

    recentes = _ids_concluidos_recentes()
    por_paj: dict[str, list[dict]] = {}
    for it in itens:
        paj_txt = (it.get("paj") or "").strip()
        if not paj_txt:
            continue
        if str(it.get("id_tramite") or "") in recentes:
            continue
        paj_norm = _normalizar_paj(paj_txt)
        categoria = categorias_caixa.classificar(it.get("descricao", ""), it.get("linha_texto", ""))
        por_paj.setdefault(paj_norm, []).append({
            "id_tramite": it.get("id_tramite", ""),
            "id_processo": it.get("id", ""),
            "descricao": it.get("descricao", ""),
            "data_envio": it.get("data_envio", ""),
            "remetente": it.get("remetente", ""),
            "prazo": it.get("prazo", ""),
            "subtipo": categoria or "geral",
        })
    return por_paj


async def _atualizar_tramitacoes_caixa(
    pajs_na_caixa: set[str],
    log: Callable[[str], None],
    por_paj_pronto: dict[str, list[dict]] | None = None,
) -> int:
    """Grava `tramitacoes_caixa` (lista) no metadata de cada PAJ ainda na caixa:
    TODAS as tramitações pendentes na caixa de entrada do usuário — geral,
    retorno OU sat, cada uma com `subtipo` ("geral"/"retorno"/"sat") e
    `id_tramite` individualizado — permite ao dashboard e ao módulo "Ações no
    SISDPU" mostrar/concluir qualquer uma (ver `services/paj_service`,
    `templates/dashboard.html`, `static/js/acoes_sisdpu.js`).

    Redesenhado 2026-09-19 (pedido do usuário): até então, trâmites de
    Retorno/SAT eram excluídos desta lista (tinham "fluxo próprio" nas
    páginas Retornos/Arquivos SAT) — isso causava dois problemas: (1) um PAJ
    só-retorno/só-sat "sumia" do dashboard geral quando a lista ficava vazia
    (corrigido antes com um campo `tem_pendencia_submenu`, agora superado por
    esta mudança), e (2) o módulo "Ações no SISDPU" não oferecia ESSES
    trâmites pra conclusão, obrigando o usuário a ir na página específica só
    pra isso. Agora TODOS aparecem em qualquer lugar que leia
    `tramitacoes_caixa`. A reconciliação com Retornos (dar baixa no store
    dela quando um trâmite de retorno é concluído por aqui, fora da própria
    página) fica a cargo de quem CONCLUI — ver
    `paj_service.concluir_tramites_caixa`/`registrar_pos_execucao_sisdpu`,
    que chamam `retornos_service.registrar_pos_execucao` pra isso. Arquivos
    SAT não precisa de nada — sua lista é sempre lida ao vivo da caixa, sem
    store próprio, então um trâmite concluído por aqui simplesmente não
    aparece mais lá na próxima vez que a página carregar.

    Comum o mesmo PAJ ter mais de uma tramitação pendente simultânea — antes,
    cada uma sobrescrevia a anterior no metadata (etiqueta/descrição/data);
    agora todas convivem lado a lado, sem conflito.

    Fonte: `sisdpu_movtram.listar_caixa_estruturada()` — a MESMA leitura por
    linha (data-rk) já usada por Retornos/SAT, com id_tramite, remetente e
    prazo por tramitação (o scrape mais simples usado no resto deste módulo
    só devolve texto solto, sem id por linha — não serve pra isto).

    Só toca PAJs que JÁ TÊM metadata.json (PAJ nunca sincronizado localmente
    não entra aqui — o laço principal cuida disso na primeira sincronização).

    `por_paj_pronto`: quando informado (ver `rodar()`), reaproveita uma
    leitura já feita no início da sync em vez de reler a caixa de novo —
    evita a dupla leitura que existia antes desta função ser dividida em
    `_ler_tramitacoes_estruturadas_por_paj` + esta.
    """
    if por_paj_pronto is not None:
        por_paj = por_paj_pronto
    else:
        por_paj = await _ler_tramitacoes_estruturadas_por_paj(log)
        if por_paj is None:
            return 0

    # PAJs a atualizar: todo PAJ ainda na caixa.
    recentes = _ids_concluidos_recentes()  # `por_paj_pronto` pode ser do início da rodada
    atualizados = 0
    pulados_por_falha = 0
    for paj_norm in pajs_na_caixa:
        meta_path = PAJS_DIR / paj_norm / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Falha de extracao de documentos/anexos NESTA rodada (ver
        # `anexos_sync_falhou`/`docs_sync_falhou` em `_processar_paj_pos_
        # detalhamento`) — NAO atualiza `tramitacoes_caixa` deste PAJ: se
        # atualizasse, a proxima sync (mesmo Completa) veria a tramitacao
        # "ja conhecida" e pularia o PAJ inteiro pra sempre, perpetuando a
        # falha pontual (bug real, visto em producao 2026-10-08 — PAJs
        # 2026/029-00412 e 2023/029-01897). Deixando o valor antigo, o
        # proximo sync da caixa volta a tentar processar este PAJ do zero.
        if meta.get("anexos_sync_falhou") or meta.get("docs_sync_falhou"):
            pulados_por_falha += 1
            continue
        novas = [t for t in por_paj.get(paj_norm, []) if str(t.get("id_tramite") or "") not in recentes]
        if meta.get("tramitacoes_caixa") == novas:
            continue  # sem mudança
        meta["tramitacoes_caixa"] = novas
        _linha_caixa_das_tramitacoes(meta, novas)
        meta.pop("tem_pendencia_submenu", None)  # campo antigo, superado por subtipo
        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            atualizados += 1
        except Exception as e:  # noqa: BLE001
            log(f"  [tramitacoes] falha ao gravar {paj_norm}: {type(e).__name__}: {e}")
    if atualizados:
        log(f"  [tramitacoes] {atualizados} PAJ(s) com tramitacoes_caixa atualizado")
    if pulados_por_falha:
        log(
            f"  [tramitacoes] {pulados_por_falha} PAJ(s) com falha de extração de "
            "documentos/anexos nesta rodada — NÃO marcados como em dia, serão "
            "reprocessados na próxima sincronização"
        )
    return atualizados


async def atualizar_tramitacoes_paj(
    paj_norm: str, log: Callable[[str], None], paj_ja_lido: bool = False
) -> list[dict]:
    """Atualiza `tramitacoes_caixa` de UM ÚNICO PAJ, sob demanda — mesma leitura
    de `_atualizar_tramitacoes_caixa` (a caixa inteira, o SISDPU não expõe
    consulta por PAJ), mas grava só este PAJ e devolve a lista fresca.

    Existe porque esse campo hoje só é atualizado pela sync COMPLETA da caixa
    (`rodar()`) — os caminhos avulsos (`rodar_paj_unico`, busca global,
    watchlist) nunca passam por `_atualizar_tramitacoes_caixa`, então um PAJ
    sincronizado só por esses caminhos fica com o campo `None` (nunca
    calculado, diferente de "sabidamente zero trâmites"). O botão "Concluir
    PAJ da Caixa de Entrada" (`services.paj_service`, `static/js/app.js`
    `concluirPajModalApp`) chama isto ANTES de decidir entre o caminho
    simples (0-1 trâmite) e o modal de escolha múltipla — sem isso, um PAJ
    com `tramitacoes_caixa=None` e MAIS de um trâmite pendente de verdade
    seria tratado como se tivesse só um, concluindo apenas o "trâmite de
    trabalho" e deixando os demais abertos sem o usuário saber (bug real,
    relatado em 2026-08-17 no PAJ-2024-029-01535).

    PAJ fora da caixa (não encontrado na leitura) grava `[]` — mesmo
    comportamento de `_atualizar_tramitacoes_caixa` pra PAJs sem tramitação
    geral pendente.
    """
    paj_norm = paj_norm.strip()
    from ingestao import sisdpu2_fonte

    if sisdpu2_fonte.fonte_mcp_ativa():
        return await atualizar_tramitacoes_paj_mcp(paj_norm, log, paj_ja_lido=paj_ja_lido)
    await _atualizar_tramitacoes_caixa({paj_norm}, log)

    meta_path = PAJS_DIR / paj_norm / "metadata.json"
    if not meta_path.exists():
        return []
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return meta.get("tramitacoes_caixa") or []


async def atualizar_tramitacoes_paj_mcp(
    paj_norm: str, log: Callable[[str], None], paj_ja_lido: bool
) -> list[dict]:
    """Fonte SIS-DPU 2.0: trâmites de UM PAJ pela busca da caixa (~1 registro
    por trâmite dele, em vez de reler a caixa inteira). Grava
    `tramitacoes_caixa` e acerta `em_caixa_atual` — com trâmite pendente o PAJ
    volta ao dashboard (inclusive um escondido cedo demais por uma conclusão
    feita com a lista desatualizada); sem nenhum, sai dele, como na conclusão
    pelo painel (services/paj_service), sem apagar anexos.

    `paj_ja_lido=False` (só os trâmites, sem reler o PAJ): trâmite novo marca
    `tramitacao_nao_lida`, que impede `rodar()` de pular o PAJ por "sem
    tramitação nova" — senão as movimentações que vieram com ele nunca
    seriam lidas. O campo some sozinho quando o PAJ é processado (o metadata
    é remontado do zero).

    Levanta RuntimeError se a leitura falhar — nunca grava "fora da caixa"
    por falha."""
    from ingestao import sisdpu2_fonte

    meta_path = PAJS_DIR / paj_norm / "metadata.json"
    if not meta_path.exists():
        return []
    paj = re.sub(r"^PAJ-(\d{4})-(\d{3})-(\d+)$", r"\1/\2-\3", paj_norm)
    res = await sisdpu2_fonte.tramites_caixa_do_paj(paj)
    if not res.get("ok"):
        raise RuntimeError(res.get("detalhe") or "não foi possível ler os trâmites do PAJ na caixa")
    novas = _agrupar_tramitacoes_por_paj(res["itens"]).get(paj_norm, [])

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    antes = meta.get("tramitacoes_caixa")
    ids_antes = {t.get("id_tramite") for t in (antes or [])}
    if not paj_ja_lido and any(t.get("id_tramite") not in ids_antes for t in novas):
        meta["tramitacao_nao_lida"] = True
    meta["tramitacoes_caixa"] = novas
    _linha_caixa_das_tramitacoes(meta, novas)
    if novas:
        if meta.get("em_caixa_atual") is not True:
            log(f"  [tramitacoes] {paj_norm}: trâmite pendente na caixa — volta ao dashboard")
        meta["em_caixa_atual"] = True
        meta.pop("concluido_em", None)
    elif meta.get("em_caixa_atual") is True:
        from datetime import datetime as _dt

        log(f"  [tramitacoes] {paj_norm}: nenhum trâmite pendente na caixa — sai do dashboard")
        meta["em_caixa_atual"] = False
        meta["concluido_em"] = _dt.now().isoformat(timespec="seconds")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return novas


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
        meta["tramitacoes_caixa"] = []  # saiu da caixa por completo — zero tramitacoes
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

    Retorna {"ok": bool, "total_caixa": int, "removidos": int, "novos_pendentes": int,
    "mensagem": str}. `novos_pendentes` conta PAJs na caixa sem pasta local ainda
    (aviso apenas — ver bloco abaixo).
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    log("[atualizar] abrindo caixa de entrada...")
    try:
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
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

    if not pajs_na_caixa:
        # Mesma salvaguarda de `rodar`: caixa vazia/ilegível NUNCA vira "todos
        # concluídos" (isso apaga os anexos de todos os PAJs ativos).
        log("[atualizar] caixa sem nenhum PAJ legível — reconciliação NÃO executada")
        return {"ok": False, "total_caixa": 0, "removidos": 0,
                "mensagem": "caixa vazia ou ilegível — nada foi marcado como concluído"}

    log("[atualizar] reconciliando PAJs que saíram da caixa...")
    removidos = _reconciliar_concluidos(pajs_na_caixa, log)
    if removidos:
        log(f"[atualizar] {removidos} PAJ(s) saíram da caixa e foram removidos do dashboard")
    else:
        log("[atualizar] nenhum PAJ saiu da caixa — tudo em dia")

    # PAJs que estao na caixa mas nunca tiveram pasta local criada — ainda nao
    # passaram por nenhuma sincronizacao (avulsa ou completa). So' um AVISO:
    # este endpoint (usado pelo botao "Atualizar", rapido) nao baixa PAJ
    # nenhum — isso continua sendo papel do "Sincronizar caixa" completo.
    # Persistido em marcador proprio pra sobreviver ao reload do dashboard
    # (mesmo padrao de `.ultima_sync_caixa.json`, gravado por `rodar()` acima);
    # o badge que le esse marcador (routes/dashboard.py) reconfere ao vivo se
    # cada PAJ listado aqui ainda esta sem pasta, e se auto-corrige quando uma
    # sincronizacao completa (ou avulsa) resolver algum deles nesse meio tempo.
    novos_pendentes = sorted(p for p in pajs_na_caixa if not (PAJS_DIR / p).exists())
    if novos_pendentes:
        log(f"[atualizar] {len(novos_pendentes)} PAJ(s) na caixa ainda não sincronizados")
    else:
        log("[atualizar] nenhum PAJ novo pendente de sincronização")

    with contextlib.suppress(Exception):
        from datetime import datetime as _dt_pend

        PAJS_DIR.mkdir(parents=True, exist_ok=True)
        (PAJS_DIR / ".pendentes_sync.json").write_text(
            json.dumps(
                {"em": _dt_pend.now().isoformat(timespec="seconds"), "pajs": novos_pendentes},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    return {
        "ok": True,
        "total_caixa": len(pajs_na_caixa),
        "removidos": removidos,
        "novos_pendentes": len(novos_pendentes),
        "mensagem": "",
    }


async def rodar(
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool | None = None,
) -> dict:
    """Sincroniza caixa SISDPU com PAJs/<PAJ>/.

    log_callback: funcao que recebe uma linha de log por chamada.
    deve_cancelar: callback opcional consultado entre PAJs; se True,
        o laco abort e a funcao retorna o resumo parcial.
    baixar_anexos: se False, pula download/OCR dos anexos (so' baixa quando
        True).
    extrair_documentos: se True, abre o popup de anexo de cada movimentacao
        pra investigar a relacao de documentos por seq, mesmo sem baixar.
        Se None (padrao), segue `baixar_anexos` (compat com chamadores que
        nao escolhem os dois separadamente). As 3 combinacoes usadas pelo
        botao "Sincronizar caixa" do dashboard (terminologia do projeto,
        pedido do usuario 2026-09-18):
          - LEVISSIMA: baixar_anexos=False, extrair_documentos=False — so'
            movs/metadata.
          - LEVE:      baixar_anexos=False, extrair_documentos=True — movs +
            relacao de documentos, sem baixar.
          - COMPLETA:  baixar_anexos=True,  extrair_documentos=True — movs +
            relacao de documentos + download.
    rodar_ocr: se True, roda o OCR simples nos PDFs baixados (padrao False —
        so baixa; use "Melhorar OCR" na aba Anexos SISDPU pra texto de qualidade).
    Retorna dict com resumo: {"total_caixa": N, "processados": M, "erros": [...]}.
    """
    if extrair_documentos is None:
        extrair_documentos = baixar_anexos

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
        "pulados": 0,
        "concluidos": 0,
        "erros": [],
        "cancelado": False,
    }
    pajs_na_caixa: set[str] = set()

    log("[sync] iniciando sincronização SISDPU")
    log(f"[sync] timeout global: {TIMEOUT_TOTAL}s, rate-limit: {RATE_LIMIT_SISDPU}s/PAJ")
    if not baixar_anexos and not extrair_documentos:
        log("[sync] modo LEVÍSSIMO — sem baixar anexos e sem investigar documentos por seq (só movs/metadata)")
    elif not baixar_anexos and extrair_documentos:
        log("[sync] modo LEVE — investiga a relação de documentos por seq mas não baixa anexos")

    try:
        log("[sync] abrindo caixa de entrada...")
        data = await sisdpu_client.caixa_de_entrada()
    except Exception as e:
        log(f"[FATAL] falha ao abrir caixa: {type(e).__name__}: {e}")
        resumo["erros"].append(f"caixa: {e}")
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
        return resumo

    # Leitura estruturada da caixa (id_tramite por linha) feita UMA VEZ aqui,
    # no início — reaproveitada tanto pra decidir quais PAJs pular abaixo
    # quanto pra gravar `tramitacoes_caixa` no final (ver a chamada de
    # `_atualizar_tramitacoes_caixa` mais abaixo). `None` = falha na leitura;
    # nesse caso NENHUM PAJ é pulado
    # nesta rodada (fail-safe — sem saber o que está na caixa agora, não há
    # base pra dizer "sem mudança").
    tramitacoes_estruturadas_por_paj = await _ler_tramitacoes_estruturadas_por_paj(log)
    if tramitacoes_estruturadas_por_paj is None:
        log("[sync] leitura estruturada da caixa falhou — nenhum PAJ será pulado por 'sem tramitação nova' nesta rodada")

    # Comum o mesmo PAJ ter mais de uma tramitação pendente na caixa (aparece
    # mais de uma vez em `itens`) — o detalhamento/anexos/movs são os MESMOS
    # PARA O PROCESSO independente de qual tramitação disparou, então
    # processamos por completo só na PRIMEIRA ocorrência; as demais só
    # precisam entrar em `pajs_na_caixa` (as tramitações individualizadas de
    # cada uma são gravadas depois, em bloco, por `_atualizar_tramitacoes_caixa`).
    vistos: set[str] = set()
    for i, item in enumerate(itens, start=1):
        if _cancelado():
            log(f"[sync] CANCELADO pelo usuário após {i - 1}/{len(itens)} PAJs")
            resumo["cancelado"] = True
            break
        paj_norm_item = _normalizar_paj(item.get("paj", "") or "")
        if paj_norm_item and paj_norm_item in vistos:
            log(f"[{i}/{len(itens)}] {item.get('paj', '?')} — outra tramitação do mesmo PAJ, já sincronizado nesta rodada")
            pajs_na_caixa.add(paj_norm_item)
            continue

        # Pula a reabertura cara do detalhamento quando o CONJUNTO de
        # id_tramite deste PAJ na caixa agora é EXATAMENTE o mesmo já
        # conhecido da última sync — ou seja, nenhuma tramitação nova
        # chegou pra ele desde então (nem sobrou uma antiga que já não
        # esteja mais lá). Comparação por ID (não por texto/data) porque
        # `id_tramite` só muda quando o SISDPU cria uma tramitação de fato
        # nova; é a mesma chave usada por `concluir_tramites_caixa`.
        #
        # Só pula quando TODAS as condições valem — cada uma existe pra
        # nunca pular por incerteza (fail-safe, mesmo raciocínio do guard em
        # sisdpu_client.caixa_de_entrada):
        #   1) a leitura estruturada desta rodada funcionou;
        #   2) e achou ESTE PAJ (se não achou, é uma divergência entre as
        #      duas leituras da caixa — processa por segurança);
        #   3) o PAJ já tinha sido sincronizado com o modelo de
        #      `tramitacoes_caixa` (PAJ legado nesse campo ainda em `None`
        #      nunca tem uma base confiável de comparação);
        #   4) o PAJ já estava marcado ativo (nunca pula um que "voltou" de
        #      concluído — precisa reabrir pra recalcular tudo);
        #   5) o conjunto de id_tramite bate INTEIRO, e não é vazio.
        pulou = False
        if paj_norm_item and tramitacoes_estruturadas_por_paj is not None:
            tram_atuais = tramitacoes_estruturadas_por_paj.get(paj_norm_item)
            if tram_atuais:
                ids_atuais = {t.get("id_tramite") for t in tram_atuais if t.get("id_tramite")}
                meta_path_item = PAJS_DIR / paj_norm_item / "metadata.json"
                if ids_atuais and meta_path_item.exists():
                    try:
                        meta_item = json.loads(meta_path_item.read_text(encoding="utf-8"))
                    except Exception:
                        meta_item = None
                    if (
                        meta_item is not None
                        and meta_item.get("em_caixa_atual") is True
                        and meta_item.get("tramitacoes_caixa") is not None
                        # trâmite novo gravado sem reler o PAJ (atualizar_tramitacoes_paj_mcp)
                        and not meta_item.get("tramitacao_nao_lida")
                    ):
                        ids_conhecidos = {
                            t.get("id_tramite")
                            for t in (meta_item.get("tramitacoes_caixa") or [])
                            if t.get("id_tramite")
                        }
                        if ids_conhecidos == ids_atuais:
                            pulou = True

        if pulou:
            _atualizar_linha_caixa(PAJS_DIR / paj_norm_item / "metadata.json", item)
            log(f"[{i}/{len(itens)}] {item.get('paj', '?')} — sem tramitação nova desde a última sync, pulado")
            pajs_na_caixa.add(paj_norm_item)
            vistos.add(paj_norm_item)
            resumo["pulados"] += 1
            continue

        log(f"[{i}/{len(itens)}] {item.get('paj', '?')}")
        try:
            paj_norm, ok = await _processar_paj(
                item,
                log,
                deve_cancelar=_cancelado,
                baixar_anexos=baixar_anexos,
                rodar_ocr=rodar_ocr,
                extrair_documentos=extrair_documentos,
            )
            if paj_norm:
                pajs_na_caixa.add(paj_norm)
                vistos.add(paj_norm)
            if ok:
                resumo["processados"] += 1
        except Exception as e:
            log(f"  [EXC] {type(e).__name__}: {e}")
            resumo["erros"].append(f"{item.get('paj', '?')}: {e}")
            # Está NA CAIXA, só a leitura falhou: sem isto a reconciliação o daria
            # por concluído e apagaria os anexos dele.
            pajs_na_caixa.add(paj_norm_item)
        await asyncio.sleep(RATE_LIMIT_SISDPU)

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

    # Tramitações individualizadas (id_tramite por linha) dos PAJs ainda na
    # caixa — permite ao dashboard mostrar/concluir cada uma separadamente
    # quando há mais de uma pro mesmo PAJ (comum no SISDPU).
    log("[sync] atualizando tramitações individualizadas da caixa...")
    with contextlib.suppress(Exception):
        await _atualizar_tramitacoes_caixa(
            pajs_na_caixa, log, por_paj_pronto=tramitacoes_estruturadas_por_paj
        )

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
        f"{resumo['pulados']} pulado(s) (sem tramitação nova), "
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

    Reuso do fluxo: login → abrir detalhamento do PAJ (caixa OU busca global,
    via `abrir_detalhamento_paj`) → baixar anexos. Funciona esteja o PAJ na
    caixa de entrada ou nao. Nao mexe em movs/metadata textuais; so completa
    o conjunto de anexos.

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

    alvo = _normalizar_paj(paj_identificador)
    dec = _decompor_paj(paj_identificador)
    if not dec:
        log(f"[anexos-desde] PAJ mal-formado: {paj_identificador!r}")
        return {"erro": "paj_mal_formado"}
    ano, unidade, numero = dec

    # Abre o detalhamento direto — funciona esteja o PAJ na caixa de entrada
    # ou nao (concluido, watchlist, incluido manualmente...). Antes exigia
    # localizar o item na listagem da caixa primeiro (`paj_nao_na_caixa`
    # abortava aqui); agora usa o mesmo fallback padrao das demais
    # funcionalidades (`abrir_detalhamento_paj`).
    log(f"  [sisdpu] abrindo detalhamento {alvo}")
    try:
        det = await sisdpu_client.abrir_detalhamento_paj(numero, ano, unidade)
    except Exception as e:
        log(f"  [ERRO] abrir_detalhamento_paj: {type(e).__name__}: {e}")
        return {"erro": f"detalhamento: {e}"}

    if det.get("erro"):
        log(f"  [ERRO] {det['erro']}")
        return {"erro": det["erro"]}

    pasta = PAJS_DIR / alvo

    # 2.5) Monta o mesmo mapa {id_do_arquivo -> seq_real} usado na sincronizacao
    # normal, mas lendo do metadata.json JA EXISTENTE (populado numa completa
    # anterior — e' assim que o overflow foi detectado em primeiro lugar).
    # Evita reprocessar a extracao por seq so pra completar anexos antigos.
    seq_por_file_id: dict[str, int] = {}
    movs_existentes: list[dict] = []
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

    # 3) Baixa anexos com filtro por data (ignora limite) — mesma relacao de
    # documentos por sequencial ja' lida acima (`movs_existentes`), sem
    # reabrir a aba "Arquivos" (ver docstring de `_baixar_anexos_sisdpu`).
    pasta.mkdir(parents=True, exist_ok=True)
    resultado_anexos = await _baixar_anexos_sisdpu(
        pasta / "Arquivos",
        log,
        limite=MAX_ANEXOS_POR_PAJ,  # ignorado quando filtro_data_min e' usado
        movs=movs_existentes,
        deve_cancelar=deve_cancelar,
        filtro_data_min=data_inicio,
        seq_por_file_id=seq_por_file_id,
        rodar_ocr=rodar_ocr,
    )

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
                    # so' presente pra documentos sem link direto (imagens via
                    # <p:commandLink> — ver extrair_documentos_movimentacoes.js);
                    # exige reabrir o detalhamento do PAJ pra clicar de verdade.
                    "commandLinkId": doc.get("commandLinkId"),
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

    # Documentos sem link direto (imagens via <p:commandLink> do PrimeFaces)
    # precisam clicar de verdade dentro do popup de documentos da seq — o que
    # exige estar no detalhamento do PAJ (nao so' logado). O fluxo normal
    # (URLs reais) nao precisa disso — so' abre o detalhamento quando ha' pelo
    # menos um item desse tipo no lote pedido.
    if any(item.get("commandLinkId") for item in lista_arquivos):
        dec = _decompor_paj(metadata.get("paj", ""))
        if dec:
            ano, unidade, numero = dec
            log("[baixar-docs] há documento(s) sem link direto — abrindo detalhamento do PAJ...")
            try:
                det = await sisdpu_client.abrir_detalhamento_paj(numero, ano, unidade, esperas_curtas=True)
                if det.get("erro"):
                    log(f"[baixar-docs] aviso: falha ao abrir detalhamento ({det['erro']}) — esses documentos vão falhar")
            except Exception as e:
                log(f"[baixar-docs] aviso: falha ao abrir detalhamento ({type(e).__name__}: {e}) — esses documentos vão falhar")

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

    resultado["nao_encontrados"] = nao_encontrados
    log("=" * 60)
    log(f"[baixar-docs] FIM: {resultado['baixados']}/{resultado['total']} documento(s) baixado(s)")
    return resultado


async def rodar_documentos_seq(
    paj_identificador: str,
    seq: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
) -> dict:
    """Extrai a relacao de documentos de UM seq especifico de um PAJ — sem
    tocar nas demais movimentacoes, sem baixar nada. Usado pelo minimenu do
    clipe de anexo na coluna "Documentos" (ver movimentacoes_tabela.html):
    quando o icone (`tem_anexo_icone`, ver sisdpu_client.py — extraido JA na
    sincronizacao levissima, sem custo) sinaliza que aquele seq PODE ter
    documento mas a relacao em si ainda nao foi obtida, chamar isso e' bem
    mais barato que uma sincronizacao leve/completa (que reprocessaria TODA
    seq nova, nao so' a pedida).

    Entra pela caixa de entrada (se o PAJ estiver la') ou pela busca global
    (mesmo fallback de `rodar_paj_unico`), so' pra reabrir o detalhamento do
    PAJ — e' pre-condicao de `extrair_documentos_por_seq`. Nao existe hoje um
    parametro de "so' este seq" nessa funcao (so' `seqs_conhecidos`, uma
    lista de EXCLUSAO) — o efeito de "so' processar 1 seq" e' obtido passando
    TODOS os seqs MENOS o pedido como "conhecidos", reaproveitando 100% do
    mecanismo ja testado (inclusive a protecao contra ler o popup do seq
    errado, ja corrigida ali antes).

    Persiste o resultado direto no metadata.json existente, tocando so' a
    movimentacao daquele seq (`documentos`, `tem_anexo_icone`) mais os
    contadores agregados (`n_documentos_indexados`, `seqs_sem_documento_
    confirmado`) — NAO reescreve o resto do PAJ (narrativa, contatos, outras
    movimentacoes ficam exatamente como estavam).

    Retorna {"ok": True, "seq": ..., "documentos": [...]} ou {"erro": "..."}.
    """

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    alvo = paj_identificador.strip()
    if alvo.startswith("PAJ-"):
        resto = alvo[4:]
        partes = resto.split("-", 1)
        if len(partes) == 2:
            alvo = f"{partes[0]}/{partes[1]}"
    dec = _decompor_paj(alvo)
    if not dec:
        return {"erro": f"identificador inválido: {paj_identificador}"}
    ano, unidade, numero = dec
    paj_norm = _normalizar_paj(alvo)
    seq_str = str(seq).strip()

    log(f"[doc-seq] alvo: {alvo} seq={seq_str}")

    try:
        data = await sisdpu_client.caixa_de_entrada()
        item_alvo = None
        for raw in data.get("itens_tabela", []) or []:
            it = sisdpu_client.parse_item_caixa(raw)
            if it and it.get("paj") == alvo:
                item_alvo = it
                break
        if item_alvo:
            det = await sisdpu_client.movimentacoes_paj(numero, ano, unidade, esperas_curtas=True)
        else:
            log(f"[doc-seq] PAJ {alvo} não está na caixa atual — seguindo pela busca global do SIS-DPU")
            det = await sisdpu_client.buscar_paj_global(
                numero, ano, unidade, forcar_sessao=False, esperas_curtas=True
            )
    except Exception as e:  # noqa: BLE001
        log(f"[FATAL] {type(e).__name__}: {e}")
        return {"erro": str(e)}

    if det.get("erro"):
        log(f"[doc-seq] {det['erro']}")
        return {"erro": det["erro"]}

    movs = det.get("movimentacoes") or []
    mov_alvo = next((m for m in movs if str(m.get("seq")) == seq_str), None)
    if mov_alvo is None:
        msg = f"seq {seq_str} não encontrado na tabela de movimentações"
        log(f"[doc-seq] {msg}")
        return {"erro": msg}
    if not mov_alvo.get("tem_anexo_icone"):
        log(f"[doc-seq] seq {seq_str} não tem ícone de anexo — nada a obter")
        return {"ok": True, "seq": seq_str, "documentos": []}

    if deve_cancelar is not None and deve_cancelar():
        log("[doc-seq] cancelado antes da extração")
        return {"erro": "cancelado"}

    todos_seqs = {str(m.get("seq")) for m in movs if m.get("seq") is not None}
    seqs_conhecidos = sorted(todos_seqs - {seq_str})
    resultado_docs = await sisdpu_client.extrair_documentos_por_seq(
        seqs_conhecidos=seqs_conhecidos,
        deve_cancelar=deve_cancelar,
        log=log,
    )
    if resultado_docs.get("erro"):
        log(f"[doc-seq] {resultado_docs['erro']}")
        return {"erro": resultado_docs["erro"]}

    documentos: list[dict] = []
    for mov_doc in resultado_docs.get("movimentacoes_com_documentos", []):
        if str(mov_doc.get("seq")) == seq_str:
            documentos = mov_doc.get("documentos", []) or []
            break
    sem_documento_confirmado = seq_str in {
        str(s) for s in (resultado_docs.get("seqsVerificadosSemDocumento") or [])
    }

    # Persiste so' este seq no metadata.json existente — nao toca no resto.
    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        log(f"[doc-seq] aviso: PAJ {paj_norm} sem metadata.json local — resultado não persistido")
        return {"ok": True, "seq": seq_str, "documentos": documentos}
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"[doc-seq] erro ao ler metadata: {type(e).__name__}: {e}")
        return {"ok": True, "seq": seq_str, "documentos": documentos}

    movs_meta = metadata.get("detalhes_sisdpu", {}).get("movimentacoes", []) or []
    mov_meta_alvo = next((m for m in movs_meta if str(m.get("seq")) == seq_str), None)
    if mov_meta_alvo is not None:
        n_delta = len(documentos) - len(mov_meta_alvo.get("documentos") or [])
        mov_meta_alvo["documentos"] = documentos
        mov_meta_alvo["tem_anexo_icone"] = True
        metadata["n_documentos_indexados"] = max(
            0, (metadata.get("n_documentos_indexados") or 0) + n_delta
        )
        if sem_documento_confirmado and not documentos:
            confirmados = {str(s) for s in (metadata.get("seqs_sem_documento_confirmado") or [])}
            confirmados.add(seq_str)
            metadata["seqs_sem_documento_confirmado"] = sorted(confirmados, key=lambda s: (len(s), s))
        try:
            meta_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log(f"[doc-seq] erro ao salvar metadata: {type(e).__name__}: {e}")
    else:
        log(f"[doc-seq] aviso: seq {seq_str} não encontrado no metadata local (tabela pode ter mudado) — resultado não persistido")

    log(f"[doc-seq] FIM: seq {seq_str} — {len(documentos)} documento(s)")
    return {"ok": True, "seq": seq_str, "documentos": documentos}


async def rodar_baixar_documentos_seq(
    paj_norm: str,
    seq: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
) -> dict:
    """Baixa os documentos de UM seq especifico — um clique so' pra "PAJ com
    fase de encerramento, so' preciso do que anexaram AGORA" (caso de uso
    real do usuario, ver PAJ 2026/029-02241 seq 22). Se a relacao de
    documentos daquele seq ainda nao for conhecida no metadata local, obtem
    primeiro (chamando `rodar_documentos_seq` acima) e ja encadeia pro
    download; se ja for conhecida (ex.: usuario ja clicou em "Obter relação
    de documentos" antes, ou uma sincronizacao leve anterior ja pegou esse
    seq), pula direto pro download.

    Reusa `rodar_baixar_documentos` (por ID de arquivo) pra a parte de
    download em si — mesmo mecanismo dos botoes "Baixar todos"/"Baixar
    selecionados" da coluna Documentos, so' que os IDs aqui vem do seq
    pedido em vez de uma selecao manual do usuario."""

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    seq_str = str(seq).strip()
    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        return {"erro": f"PAJ {paj_norm} não encontrado (sem metadata.json)"}

    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"erro": f"erro ao ler metadata: {e}"}

    movs_meta = metadata.get("detalhes_sisdpu", {}).get("movimentacoes", []) or []
    mov_meta_alvo = next((m for m in movs_meta if str(m.get("seq")) == seq_str), None)
    documentos = (mov_meta_alvo or {}).get("documentos") or []

    if not documentos:
        log(f"[baixar-doc-seq] relação de documentos da seq {seq_str} ainda não conhecida — obtendo...")
        # `metadata["paj"]` guarda o formato "AAAA/UUU-NNNNN" (aceito por
        # rodar_documentos_seq); paj_norm ("PAJ-AAAA-UUU-NNNNN") tambem e'
        # aceito, entao serve de fallback se o campo faltar por algum motivo.
        resultado_extracao = await rodar_documentos_seq(
            metadata.get("paj") or paj_norm,
            seq_str,
            log_callback,
            deve_cancelar=deve_cancelar,
        )
        if resultado_extracao.get("erro"):
            return resultado_extracao
        documentos = resultado_extracao.get("documentos") or []

    if not documentos:
        log(f"[baixar-doc-seq] seq {seq_str} não tem documento nenhum — nada a baixar")
        return {"baixados": 0, "erros": [], "total": 0, "nao_encontrados": []}

    ids = [
        sisdpu_client.extrair_id_arquivo(doc.get("urlDownload", ""))
        for doc in documentos
    ]
    ids = [i for i in ids if i]
    if not ids:
        log(f"[baixar-doc-seq] seq {seq_str} tem documento(s) sem ID de download reconhecível")
        return {"baixados": 0, "erros": [], "total": 0, "nao_encontrados": []}

    return await rodar_baixar_documentos(paj_norm, ids, log_callback, deve_cancelar=deve_cancelar)


async def rodar_paj_unico(
    paj_identificador: str,
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool = True,
    esperas_curtas: bool = False,
) -> dict:
    """Sincroniza UM unico PAJ (util pra teste/re-sync pontual sem rodar a caixa inteira).

    `paj_identificador` aceita tanto '2021/044-00635' quanto 'PAJ-2021-044-00635'.

    Entra PREFERENCIALMENTE pela caixa de entrada (clicando no link da tela da
    caixa) — esse caminho e' o unico que traz `etiqueta_sisdpu` e confirma
    `em_caixa_atual`. Se o PAJ NAO estiver na caixa (concluido, de outro
    defensor, watchlist...), NAO aborta: cai automaticamente para a busca
    global do SIS-DPU (`rodar_paj_via_busca_global`), reaproveitando a sessao
    ja aberta. Ou seja, sincronizar um PAJ avulso nunca fica preso ao escopo
    da caixa — esse escopo e' exclusivo da sync da caixa (`rodar`).

    `esperas_curtas=True` reduz as esperas fixas pos-navegacao. Combinado com
    `baixar_anexos=False` e' o modo "so movimentacoes" (icone de atualizar da
    tabela de movimentacoes): re-le a tabela inteira (o SISDPU nao oferece
    delta), mas mantem `extrair_documentos=True` para so' abrir o popup de
    anexo das movimentacoes REALMENTE novas — `extrair_documentos_por_seq`
    ja exclui da fila as seqs que ja tinham `documentos` no metadata anterior,
    entao o custo por clique fica proporcional so' ao que mudou desde a
    ultima sincronizacao.
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
        # Mesmo soluço transitório de rede/navegação do SISDPU tratado em
        # `rodar()` (ver `_processar_item_caixa` acima) — aqui a chamada e'
        # avulsa (um unico PAJ, sem o loop que a' fazia tentar o proximo item),
        # entao sem retry um net::ERR_ABORTED pontual matava a sincronizacao
        # inteira na certa (confirmado em producao, 2026-10-08: PAJ 2023/029-
        # 01897 e 2026/029-03247). Forca reconexao e tenta mais UMA vez antes
        # de desistir.
        log(f"[ERRO] falha ao abrir caixa: {type(e).__name__}: {e} — reconectando e tentando novamente...")
        with contextlib.suppress(Exception):
            await sisdpu_client.reconectar()
        try:
            data = await sisdpu_client.caixa_de_entrada()
        except Exception as e2:
            log(f"[FATAL] falha ao abrir caixa (persistiu após reconexão): {type(e2).__name__}: {e2}")
            resumo["erros"].append(f"caixa: {e2}")
            return resumo

    itens_raw = data.get("itens_tabela", []) or []
    item_alvo = None
    for raw in itens_raw:
        it = sisdpu_client.parse_item_caixa(raw)
        if it and it.get("paj") == alvo:
            item_alvo = it
            break

    if not item_alvo:
        # Fora da caixa nao e' erro: e' o caso normal de PAJ concluido, de
        # watchlist ou incluido manualmente. Segue pela pesquisa global do
        # SIS-DPU REAPROVEITANDO a sessao ja logada (forcar_sessao=False).
        # `fora_da_caixa_confirmado=True`: acabamos de checar a listagem da
        # caixa (logo acima) e o PAJ NAO estava la — diferente da watchlist,
        # aqui sabemos com certeza que ele saiu, entao `em_caixa_atual` deve
        # ser corrigido pra False (nao preservado do valor antigo, que pode
        # estar desatualizado de quando o PAJ ainda estava na caixa).
        log(f"[sync-paj] PAJ {alvo} não está na caixa atual — seguindo pela busca global do SIS-DPU")
        return await rodar_paj_via_busca_global(
            alvo,
            log_callback,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
            forcar_sessao=False,
            fora_da_caixa_confirmado=True,
            extrair_documentos=extrair_documentos,
            esperas_curtas=esperas_curtas,
        )

    if not baixar_anexos:
        log("[sync-paj] modo RÁPIDO — anexos NÃO serão baixados")

    try:
        paj_norm, ok = await _processar_paj(
            item_alvo,
            log,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
            extrair_documentos=extrair_documentos,
            esperas_curtas=esperas_curtas,
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
            # `tramitacoes_caixa` — GARANTE que fica atualizado independente da
            # forma de sincronização (pedido do usuário 2026-09-24: antes,
            # esse campo só era calculado pela sync COMPLETA da caixa `rodar()`,
            # deixando um PAJ sincronizado por um caminho avulso como este com
            # o campo `None`/desatualizado até alguém reconferir na unha — ver
            # `atualizar_tramitacoes_paj`). Best-effort: uma falha aqui não
            # derruba a sincronização do PAJ em si, só fica sem essa atualização
            # extra (o campo permanece no que já estava).
            try:
                await atualizar_tramitacoes_paj(paj_norm, log, paj_ja_lido=True)
            except Exception as e:  # noqa: BLE001
                log(f"  [tramitacoes] falha ao atualizar (best-effort): {type(e).__name__}: {e}")
    except Exception as e:
        log(f"  [EXC] {type(e).__name__}: {e}")
        resumo["erros"].append(f"{alvo}: {e}")

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
    forcar_sessao: bool = True,
    extrair_documentos: bool = True,
    esperas_curtas: bool = False,
    fora_da_caixa_confirmado: bool = False,
) -> dict:
    """Sincroniza UM PAJ via o campo de pesquisa global do SIS-DPU.

    Diferente de `rodar_paj_unico`, NAO depende do PAJ estar na caixa de
    entrada do defensor — entra pela "Pesquisa Rapida" do header e cai
    direto no detalhamento do processo. Usado para PAJs da watchlist, que
    podem ter sido concluidos (fora da caixa) ou nunca ter passado por ela
    (cadastrados individualmente).

    Preservacao de metadata vs `rodar_paj_unico`:
      - `em_caixa_atual` — por padrao preservado do metadata anterior (a busca
        global, por si so, nao responde se o PAJ ainda esta na caixa do
        defensor) — EXCETO se `fora_da_caixa_confirmado=True` (ver abaixo).
      - `etiqueta_sisdpu` — preservada da rodada anterior (campo de pesquisa
        nao expoe etiquetas da caixa).
      - `via_watchlist`, `concluido_em`, `n_anexos_removidos` — preservados
        (exceto `concluido_em`, tambem atualizado quando `fora_da_caixa_confirmado`
        confirma uma transicao True->False que ainda nao tinha sido registrada).

    `paj_identificador` aceita 'PAJ-2026-044-00311' ou '2026/044-00311'.

    `forcar_sessao=False` reaproveita a sessao Playwright ja aberta em vez de
    fechar e relogar do zero — usado pelo fallback de `rodar_paj_unico`, que
    ja abriu a caixa (e portanto ja esta logado).

    `fora_da_caixa_confirmado=True`: usado exclusivamente pelo fallback de
    `rodar_paj_unico` (que ACABOU de checar a caixa e nao achou o PAJ) — repassa
    ao `_processar_paj_pos_detalhamento` pra corrigir `em_caixa_atual` pra False
    em vez de preservar cegamente um `True` desatualizado. NAO usar em chamadas
    vindas da watchlist (`rodar_watchlist`), que nunca checaram a caixa.
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
        det = await sisdpu_client.buscar_paj_global(
            numero, ano, unidade, forcar_sessao=forcar_sessao, esperas_curtas=esperas_curtas)
    except Exception as e:
        log(f"[FATAL] falha na busca global: {type(e).__name__}: {e}")
        resumo["erros"].append(f"busca: {e}")
        return resumo

    if det.get("erro"):
        log(f"[busca-global] sem resultado: {det['erro']}")
        resumo["erros"].append(det["erro"])
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
            extrair_documentos=extrair_documentos,
            via_busca_global=True,
            fora_da_caixa_confirmado=fora_da_caixa_confirmado,
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
            # `tramitacoes_caixa` — mesma garantia de `rodar_paj_unico` (ver nota
            # lá): independe do PAJ estar de fato na caixa agora — se não
            # estiver (comum pra watchlist), grava `[]` corretamente
            # (`atualizar_tramitacoes_paj` já trata isso). Best-effort.
            try:
                await atualizar_tramitacoes_paj(paj_norm, log, paj_ja_lido=True)
            except Exception as e:  # noqa: BLE001
                log(f"  [tramitacoes] falha ao atualizar (best-effort): {type(e).__name__}: {e}")
    except Exception as e:
        log(f"  [EXC] {type(e).__name__}: {e}")
        resumo["erros"].append(f"{alvo}: {e}")

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


async def rodar_prazos_abertos(
    log_callback: Callable[[str], None],
    deve_cancelar: Callable[[], bool] | None = None,
) -> dict:
    """Sincronização LEVE (só movimentações, sem anexos) de todos os PAJs que
    aparecem na categoria "Processos com prazo em aberto" das intimações do
    e-proc (aba Prazos) — postura proativa: sempre que a caixa do SISDPU é
    sincronizada, os PAJs que têm um prazo correndo no e-proc já ficam com o
    detalhamento/movimentações atualizados no painel, sem precisar abrir cada
    um manualmente.

    Fonte da lista: `eproc_intimacoes.carregar()["secoes"]["prazo_aberto"]` —
    o que já foi lido na ÚLTIMA sincronização de intimações (não força uma
    releitura do e-proc aqui; isso é papel do botão "Sincronizar intimações"
    ou do auto-sync do início do painel). O campo `paj` de cada caso vem de
    texto livre digitado no lembrete do e-proc — pode estar vazio (sem
    lembrete) ou não bater com nada no SISDPU; ambos os casos são só um erro
    registrado no resumo, não interrompem os demais.

    Itera serialmente (mesmo motivo de `rodar_watchlist`: uma única sessão
    Playwright) via `rodar_paj_via_busca_global` com `baixar_anexos=False` —
    funciona mesmo se o PAJ já saiu da caixa de entrada (concluído).
    """
    from ingestao import eproc_intimacoes

    def log(msg: str) -> None:
        with contextlib.suppress(Exception):
            log_callback(msg)

    resumo: dict = {"total": 0, "processados": 0, "erros": [], "cancelado": False}

    # Leitura VISÍVEL (baixa local aplicada, ver `services.prazo_baixa_
    # service`) — um PAJ cujo único prazo já foi fechado dentro do painel
    # não entra mais nesta lista sem precisar de nova sincronização.
    dados_intimacoes = eproc_intimacoes.carregar_visivel_todos()  # TRF4 + JMU
    casos = (dados_intimacoes.get("secoes") or {}).get("prazo_aberto") or []

    # Dedupe por paj_norm preservando ordem — o mesmo PAJ pode ter varios
    # prazos/processos na mesma categoria.
    vistos: set[str] = set()
    pajs_norm: list[str] = []
    for c in casos:
        paj_exibicao = (c.get("paj") or "").strip()
        if not paj_exibicao:
            continue
        dec = _decompor_paj(paj_exibicao)
        if not dec:
            resumo["erros"].append(f"{paj_exibicao}: número de PAJ inválido (lembrete mal formatado)")
            continue
        paj_norm = _normalizar_paj(paj_exibicao)
        if paj_norm in vistos:
            continue
        vistos.add(paj_norm)
        pajs_norm.append(paj_norm)

    resumo["total"] = len(pajs_norm)

    if not pajs_norm:
        log("[prazos-abertos] nenhum PAJ com prazo em aberto identificado nas intimações — nada a fazer")
        return resumo

    log(f"[prazos-abertos] {len(pajs_norm)} PAJ(s) com prazo em aberto (sincronização leve, sem anexos)")

    for idx, paj_norm in enumerate(pajs_norm, start=1):
        if deve_cancelar is not None and deve_cancelar():
            log(f"[prazos-abertos] CANCELADO após {idx - 1} de {len(pajs_norm)}")
            resumo["cancelado"] = True
            break

        log("-" * 60)
        log(f"[prazos-abertos] ({idx}/{len(pajs_norm)}) {paj_norm}")

        try:
            r = await rodar_paj_via_busca_global(
                paj_norm,
                log_callback,
                deve_cancelar=deve_cancelar,
                baixar_anexos=False,
                extrair_documentos=True,
                esperas_curtas=True,
            )
            if r.get("processado"):
                resumo["processados"] += 1
            else:
                erros_paj = r.get("erros", []) or ["não processado"]
                resumo["erros"].append(f"{paj_norm}: {'; '.join(erros_paj)}")
        except Exception as e:
            log(f"[prazos-abertos] EXC em {paj_norm}: {type(e).__name__}: {e}")
            resumo["erros"].append(f"{paj_norm}: {type(e).__name__}: {e}")

    log("=" * 60)
    log(
        f"[prazos-abertos] FIM: processados={resumo['processados']} de "
        f"{resumo['total']}, erros={len(resumo['erros'])}"
        + (" (cancelado)" if resumo["cancelado"] else "")
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
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": f"erro ao acessar SIS-DPU: {type(e).__name__}: {e}",
        }

    if det.get("erro"):
        log(f"[busca-paj] sem resultado: {det['erro']}")
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
    await _escrever_texto_com_retry(pasta / "sisdpu.txt", texto_sisdpu)

    # Metadata anterior (se este PAJ ja tinha sido sincronizado antes por outro
    # caminho) — usado so' pra preservar a escolha manual de WhatsApp, que nao
    # vem do SISDPU (ver `_montar_contatos`).
    meta_antiga: dict = {}
    with contextlib.suppress(Exception):
        meta_path_antiga = pasta / "metadata.json"
        if meta_path_antiga.exists():
            meta_antiga = json.loads(meta_path_antiga.read_text(encoding="utf-8"))

    try:
        metadata = parser.montar_metadata(paj_norm, texto_sisdpu)
    except Exception as e:
        log(f"[busca-paj] ERRO parser: {type(e).__name__}: {e}")
        return {
            "paj_norm": paj_norm,
            "ok": False,
            "mensagem": f"erro ao processar metadata: {e}",
        }

    metadata["sisdpu_raw"] = det

    # Campos frageis de sisdpu_raw: preserva o da sincronizacao anterior se
    # esta nao devolveu (mesmo raciocinio/auditoria de
    # _processar_paj_pos_detalhamento, ver comentario lá).
    sisdpu_raw_antigo = meta_antiga.get("sisdpu_raw") or {}
    for _campo in ("narrativa", "assistidos_pf", "audiencias", "representantes_legais"):
        if not metadata["sisdpu_raw"].get(_campo) and sisdpu_raw_antigo.get(_campo):
            metadata["sisdpu_raw"][_campo] = sisdpu_raw_antigo[_campo]

    # Assistido/Ofício: aqui (busca global "resumo") não há linha da caixa
    # nenhuma — só o detalhamento. Se vier vazio, cai pro valor anterior.
    if not metadata.get("assistido_caixa") and meta_antiga.get("assistido_caixa"):
        metadata["assistido_caixa"] = meta_antiga["assistido_caixa"]
    if not metadata.get("oficio_caixa") and meta_antiga.get("oficio_caixa"):
        metadata["oficio_caixa"] = meta_antiga["oficio_caixa"]

    # Vínculo com processo judicial — mesma proteção de
    # _processar_paj_pos_detalhamento.
    if det.get("processos_judiciais"):
        metadata["processos_judiciais"] = det["processos_judiciais"]
    elif meta_antiga.get("processos_judiciais"):
        metadata["processos_judiciais"] = meta_antiga["processos_judiciais"]

    metadata.update(_montar_contatos(det, meta_antiga))

    # Se temos movimentacoes estruturadas do sisdpu_client, usa direto sem perda
    movs_antigas = (meta_antiga.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", []) or []
    if det.get("movimentacoes"):
        mov_estruturadas = parser.parsear_movimentacoes_estruturadas(det["movimentacoes"])
        _preservar_descricao_retorno_antiga(mov_estruturadas, movs_antigas)
        if "detalhes_sisdpu" not in metadata:
            metadata["detalhes_sisdpu"] = {}
        metadata["detalhes_sisdpu"]["movimentacoes"] = mov_estruturadas
    elif movs_antigas:
        # Mesma proteção de _processar_paj_pos_detalhamento — nunca zerar o
        # histórico inteiro por uma falha pontual de extração.
        log(f"[busca-paj] SISDPU nao devolveu movimentacoes — preservando "
            f"{len(movs_antigas)} da sincronizacao anterior")
        if "detalhes_sisdpu" not in metadata:
            metadata["detalhes_sisdpu"] = {}
        metadata["detalhes_sisdpu"]["movimentacoes"] = movs_antigas

    # Foto do assistido (mesma logica de `_processar_paj_pos_detalhamento`).
    foto_src = det.get("foto_assistido_src") or ""
    foto_path = pasta / "foto_assistido.jpg"
    if foto_src.startswith("data:image") and "," in foto_src:
        try:
            foto_path.write_bytes(base64.b64decode(foto_src.split(",", 1)[1]))
            metadata["foto_assistido"] = "foto_assistido.jpg"
        except Exception as e:
            log(f"[busca-paj] aviso: falha ao salvar foto do assistido: {type(e).__name__}: {e}")
            if foto_path.exists():
                metadata["foto_assistido"] = "foto_assistido.jpg"
    elif foto_path.exists():
        metadata["foto_assistido"] = "foto_assistido.jpg"
    else:
        # Fonte SIS-DPU 2.0: a foto vem da ficha do assistido no MCP (22/10/2026,
        # services/foto_assistido_service.py) — uma consulta por PAJ, só se a
        # pasta ainda não tem foto; avatar genérico é descartado.
        with contextlib.suppress(Exception):
            from services import foto_assistido_service
            _rf = await asyncio.to_thread(foto_assistido_service.garantir_foto, pasta, det)
            if _rf.get("situacao") == "foto":
                metadata["foto_assistido"] = "foto_assistido.jpg"

    # Marca origem: PAJ nao veio da caixa atual, foi puxado por busca explicita
    # do defensor (via watchlist). Dashboard usa essa flag para esconder.
    metadata["via_watchlist"] = True
    # Nao seta em_caixa_atual — fica ausente. Se uma sync regular encontrar
    # esse PAJ na caixa depois, ela seta True e o dashboard volta a mostrar.

    (pasta / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    log(f"[busca-paj] OK: {paj_norm} (assistido={metadata.get('assistido_caixa', '?')[:40]})")
    return {
        "paj_norm": paj_norm,
        "ok": True,
        "mensagem": "PAJ encontrado e dados do cabecalho gravados",
    }
