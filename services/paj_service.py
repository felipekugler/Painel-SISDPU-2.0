"""Servico de leitura de PAJs do workspace Oficio Geral.

Cada PAJ fica em `PAJS_DIR/<PAJ-YYYY-044-XXXXX>/`, com pelo menos:
- `metadata.json` — gerado pelo script de migracao
- `sisdpu.txt`   — texto bruto do SISDPU
- `PROMPT_MAX.md` — gerado on-demand pelo prompt_builder
- arquivos gerados pelo Claude (despacho.txt, peticao.txt, .docx, .pdf, etc.)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Annotated

from fastapi import Path as FastApiPath

from config import PAJS_DIR, PECAS_FEITAS_DIR
from services import lembretes_service


# Type alias para parametros de rota — FastAPI valida o pattern e retorna 422
# com mensagem clara automaticamente. Use em toda rota que recebe paj_norm:
#     async def handler(paj_norm: PajNorm): ...
PAJ_NORM_REGEX = r"^PAJ-\d{4}-\d{3}-\d{5}$"
PajNorm = Annotated[
    str,
    FastApiPath(
        pattern=PAJ_NORM_REGEX,
        description="Identificador do PAJ no formato PAJ-YYYY-044-XXXXX",
    ),
]


IGNORAR = {
    "metadata.json",
    "PROMPT_MAX.md",
    "elaboracao.json",
    "historico.jsonl",
    "sisdpu.txt",
    "NOTAS.md",
    "analise_pretensao.json",
    "organizar_ajuizamento.json",
}

PREFIXOS_DESPACHO = ("despacho",)

PREFIXOS_PECA_JUDICIAL = (
    "peticao",
    "petição",
    "peca",
    "peça",
    "recurso",
    "agravo",
    "apelacao",
    "apelação",
    "embargos",
    "contestacao",
    "contestação",
    "manifestacao",
    "manifestação",
    "memoriais",
    "relatorio",
    "relatório",
    "oficio",
    "ofício",
)


def _ler_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalizar(texto: str) -> str:
    """Remove acentos e lowercase — pra comparar nomes."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


_RE_HTML_TAGS = re.compile(r"<[^>]+>")


def _strip_html(texto: str) -> str:
    """Remove tags HTML e normaliza espacos — usado para exibir descricoes de
    movimentacao (que vem em HTML) como texto simples no dashboard."""
    if not texto:
        return ""
    import html as _html

    limpo = _html.unescape(_RE_HTML_TAGS.sub(" ", texto))
    return re.sub(r"\s+", " ", limpo).strip()


def _e_pasta_paj(p: Path) -> bool:
    return p.is_dir() and p.name.startswith("PAJ-")


def _garantir_prompt_max(pasta: Path) -> None:
    """Gera PROMPT_MAX.md se faltar ou se estiver mais antigo que metadata.json."""
    try:
        from services.prompt_builder import gerar_prompt_max
    except ImportError:
        return

    prompt_path = pasta / "PROMPT_MAX.md"
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        return
    if prompt_path.exists() and prompt_path.stat().st_mtime >= meta_path.stat().st_mtime:
        return
    with contextlib.suppress(Exception):
        gerar_prompt_max(pasta.name)


MAX_CONCLUIDOS_VISIVEIS = 100


# Cache em memoria de listar_pajs() — dashboard chama isso a cada navegacao,
# entao vale evitar a varredura de ~309 stat() + 103 read JSON quando nada
# mudou. Invalida por TTL curto (5s) + assinatura do diretorio (mtime + count
# de entradas) para detectar criacao/remocao de pastas. Sync_service tambem
# chama invalidar_cache_listagem() explicitamente apos cada sync.
_LIST_CACHE_TTL_SEG = 5.0
_list_cache: dict = {"ts": 0.0, "key": None, "result": None}


def _dir_signature() -> tuple[float, int]:
    """Assinatura barata do estado de PAJS_DIR: (mtime, n_entries).

    mtime do diretorio muda quando arquivos sao adicionados/removidos. A
    contagem ajuda a pegar reorgs sem mudanca de mtime (alguns FS sao
    inconsistentes). Edicoes internas em metadata.json NAO invalidam — pra
    isso o TTL curto basta, e o sync chama invalidar_cache_listagem()."""
    if not PAJS_DIR.exists():
        return (0.0, 0)
    st = PAJS_DIR.stat()
    n = sum(1 for _ in PAJS_DIR.iterdir())
    return (st.st_mtime, n)


def invalidar_cache_listagem() -> None:
    """Zera o cache de listar_pajs(). Chamada pelo sync_service ao terminar."""
    _list_cache["result"] = None
    _list_cache["ts"] = 0.0
    _list_cache["key"] = None


def _ler_concluido_em(metadata: dict) -> str:
    """Le o timestamp em que o PAJ saiu da caixa (foi 'concluido').

    Aceita os dois nomes de campo: o atual `concluido_em` e o legado
    `arquivado_em` (de PAJs sincronizados antes do rename). Conforme cada
    PAJ for re-sincronizado, o metadata passa a usar so' o nome novo —
    leitura tolerante garante zero quebra para PAJs ainda nao atualizados.
    """
    return metadata.get("concluido_em") or metadata.get("arquivado_em", "")


def listar_pajs(incluir_concluidos: bool = False, excluir_submenu: bool = True) -> list[dict]:
    """Retorna lista resumida de PAJs pro dashboard.

    "Concluido" aqui significa apenas "fora da caixa de entrada" (o PAJ
    saiu da caixa, podendo ou nao estar formalmente arquivado no SIS).
    Por padrao retorna apenas PAJs em_caixa_atual (True ou campo ausente —
    legado sem o flag ainda conta como ativo ate a proxima sync). Quando
    incluir_concluidos=True, devolve tambem os que sairam da caixa,
    limitado aos MAX_CONCLUIDOS_VISIVEIS mais recentes (ordenados por
    concluido_em desc). PAJs mais antigos permanecem no disco (com
    registros/OCR) mas nao aparecem mais na lista.

    Resultado cacheado em memoria por _LIST_CACHE_TTL_SEG segundos (e ate o
    PAJS_DIR mudar de assinatura — vide _dir_signature)."""
    chave = ("incluir" if incluir_concluidos else "ativos",
             "sub0" if excluir_submenu else "sub1", *_dir_signature())
    agora = time.monotonic()
    if (
        _list_cache["result"] is not None
        and _list_cache["key"] == chave
        and agora - _list_cache["ts"] < _LIST_CACHE_TTL_SEG
    ):
        return _list_cache["result"]

    # Sigla padronizada do ofício (Tabela de Ofícios das Configurações).
    try:
        from services.config_painel_service import sigla_de_oficio as _sigla_of
    except Exception:  # noqa: BLE001
        def _sigla_of(x):
            return x

    ativos: list[dict] = []
    concluidos: list[dict] = []

    if not PAJS_DIR.exists():
        return []

    for pasta in sorted(PAJS_DIR.iterdir()):
        if not _e_pasta_paj(pasta):
            continue

        metadata = _ler_json(pasta / "metadata.json")
        if not metadata:
            continue

        # Filtro de concluidos: so exclui se o flag esta EXPLICITAMENTE False
        em_caixa = metadata.get("em_caixa_atual")
        esta_concluido = em_caixa is False
        if esta_concluido and not incluir_concluidos:
            continue

        # PAJs adicionados via watchlist (busca explicita pelo defensor) NAO
        # aparecem no dashboard — sao visiveis so na watchlist e via URL direta.
        # Excecao: se uma sync regular ja confirmou em_caixa_atual=True, e' um
        # PAJ "promovido" para a caixa e volta a aparecer normalmente.
        if metadata.get("via_watchlist") and em_caixa is not True:
            continue

        # PAJs que pertencem a um submenu especializado (Retornos/SAT) NÃO
        # aparecem no dashboard — só no submenu próprio (decisão do usuário).
        # Fail-safe: categoria ausente/"" → PAJ geral, aparece normalmente.
        if excluir_submenu and metadata.get("categoria_caixa"):
            continue

        paj_norm = pasta.name

        det = metadata.get("detalhes_sisdpu", {}) or {}
        movs = det.get("movimentacoes", []) or []
        # "Ultima" = mais recente. Usa data como criterio principal (robusto
        # tanto para seq real do SIS quanto para seq local de sisdpu.txt
        # antigos), com seq como desempate para movimentacoes do mesmo dia.
        ultima_mov = max(
            movs,
            key=lambda m: (m.get("data") or "", int(m.get("seq", 0) or 0)),
            default={},
        )
        max_seq_mov = max((int(m.get("seq", 0) or 0) for m in movs), default=0)

        # "Data" do dashboard = data do TRAMITE que esta na caixa de entrada.
        # Fonte: metadata.data_mov_caixa (gravado pelo sync a partir da linha da
        # caixa). Quando ele == data_abertura, e' o fallback antigo do parser (o
        # sisdpu.txt nao tinha movs) -> usa a data da ultima movimentacao real
        # ate um novo "Sincronizar caixa" gravar a data exata da linha da caixa.
        _dmc = metadata.get("data_mov_caixa", "")
        _data_caixa = (
            _dmc if (_dmc and _dmc != metadata.get("data_abertura", ""))
            else (ultima_mov.get("data") or _dmc)
        )

        # Contagem de pecas GERADAS pelo defensor/Claude (na raiz do PAJ)
        n_pecas_geradas = 0
        for f in pasta.iterdir():
            if not f.is_file() or f.name in IGNORAR:
                continue
            n_pecas_geradas += 1

        # Lembretes pessoais deste PAJ (LEMBRETES.json) — alimenta o icone com
        # tooltip na coluna PAJ do dashboard, no mesmo padrao do icone de
        # lembrete do e-proc usado em templates/prazos.html.
        lembretes_paj = lembretes_service.ler_lembretes_pasta(pasta)

        # Contagem de ANEXOS SISDPU (baixados pelo sync em Arquivos/, excluindo
        # os .txt de OCR — contamos so o arquivo-fonte, nao o OCR companion).
        pasta_pecas = pasta / "Arquivos"
        n_anexos_sisdpu = 0
        if pasta_pecas.exists() and pasta_pecas.is_dir():
            for f in pasta_pecas.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() == ".txt":
                    # e' o OCR companion — pula, ja contamos o PDF/original
                    continue
                n_anexos_sisdpu += 1

        item = {
            "paj": metadata.get("paj", paj_norm),
            "paj_norm": paj_norm,
            "assistido": metadata.get("assistido_caixa", ""),
            "oficio": metadata.get("oficio_caixa", ""),
            "oficio_sigla": _sigla_of(metadata.get("oficio_caixa", "")),
            "etiqueta": metadata.get("etiqueta_sisdpu", ""),
            "foro": metadata.get("foro_detectado", "?"),
            "classificacao": metadata.get("classificacao", "?"),
            "pretensaopura": metadata.get("pretensaopura", ""),
            "data_caixa": _data_caixa,
            # Descricao do tramite da caixa de entrada (coluna "Descricao" do
            # dashboard). Fonte primaria: desc_mov_caixa (gravado pelo sync a
            # partir da linha da caixa). Fallback ate o proximo sync: descricao
            # da ultima movimentacao, com HTML removido.
            "desc_caixa": (metadata.get("desc_mov_caixa") or "").strip()
            or _strip_html(ultima_mov.get("descricao") or "")[:300],
            "processo_judicial": metadata.get("processo_judicial", ""),
            "n_pecas": n_pecas_geradas,
            "n_anexos_sisdpu": n_anexos_sisdpu,
            "n_documentos_indexados": metadata.get("n_documentos_indexados", 0) or 0,
            "n_decisoes": 0,
            "ultima_preparacao": metadata.get("migrado_em", ""),
            "prazos_abertos": metadata.get("prazos_abertos", []),
            "ultima_mov_desc": (ultima_mov.get("descricao") or "")[:120],
            "ultima_mov_data": ultima_mov.get("data", ""),
            "status_sisdpu": (det.get("status_paj") or "").strip(),
            "em_caixa_atual": not esta_concluido,
            "concluido_em": _ler_concluido_em(metadata) if esta_concluido else "",
            "sync_incompleto": bool(metadata.get("sync_incompleto", False)),
            "sync_incompleto_motivo": metadata.get("sync_incompleto_motivo", ""),
            # Coluna "Análise" do dashboard — SIM se o diagnostico por
            # pretensao (BPC/BI) foi concluido dentro do painel
            # (analise_pretensao.json com status "done", vide
            # analise_pretensao_service). So conta o que roda pelo painel —
            # analise gerada por fora (skill manual) nao entra aqui.
            "tem_analise_ia": bool(
                isinstance(_ap := _ler_json(pasta / "analise_pretensao.json"), dict)
                and _ap.get("status") == "done"
            ),
            # Estado atual usado pelo frontend pra detectar novidades desde a
            # ultima visita (snapshot guardado em localStorage). Vide
            # static/js/app.js -> calcularNovidades().
            "max_seq_mov": max_seq_mov,
            # Lembretes — icone com tooltip na coluna PAJ (vide dashboard.html).
            "n_lembretes": len(lembretes_paj),
            "lembrete_itens": [
                {"texto": l.get("descricao", ""), "cor": l.get("cor", "amarelo")}
                for l in lembretes_paj
            ],
        }
        if esta_concluido:
            concluidos.append(item)
        else:
            ativos.append(item)

    # Cap: mostra so os MAX_CONCLUIDOS_VISIVEIS mais recentes (por concluido_em desc).
    # PAJs legados sem concluido_em caem no fim (sort por string vazia ordena antes).
    concluidos.sort(key=lambda p: p.get("concluido_em") or "", reverse=True)
    concluidos = concluidos[:MAX_CONCLUIDOS_VISIVEIS]

    resultado = ativos + concluidos
    _list_cache["ts"] = agora
    _list_cache["key"] = chave
    _list_cache["result"] = resultado
    return resultado


def tem_processo(item: dict) -> bool:
    """True se o PAJ tem processo judicial vinculado (numero CNJ no metadata).

    Usado pelo roteamento da triagem em lote: COM processo -> analisar-processo;
    SEM -> firac-triagem. Aceita tanto o item resumido de listar_pajs quanto um
    metadata bruto (ambos expoem `processo_judicial`).
    """
    return bool((item.get("processo_judicial") or "").strip())


def ler_paj(paj_norm: str) -> dict | None:
    """Retorna dados completos de um PAJ."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return None

    metadata = _ler_json(pasta / "metadata.json")
    if not metadata:
        return None

    _garantir_prompt_max(pasta)

    prompt_max_path = pasta / "PROMPT_MAX.md"
    prompt_max = prompt_max_path.read_text(encoding="utf-8") if prompt_max_path.exists() else ""

    # Categorizar arquivos na pasta do PAJ:
    #   despachos       — nomes comecando com "despacho"
    #   pecas_judiciais — demais arquivos gerados (peticao*, recurso*, .docx, .pdf, etc.)
    despachos: list[dict] = []
    pecas_judiciais: list[dict] = []

    for f in sorted(pasta.iterdir()):
        if not f.is_file() or f.name in IGNORAR:
            continue

        item = {
            "nome": f.name,
            "caminho": f.name,
            "tipo": f.suffix.lstrip(".").lower(),
            "tamanho": f.stat().st_size,
            "modificado": f.stat().st_mtime,
        }

        nome_norm = _normalizar(f.name)
        if nome_norm.startswith(PREFIXOS_DESPACHO):
            despachos.append(item)
        else:
            pecas_judiciais.append(item)

    # Subpasta "Peças IA": petições/peças geradas pela análise de pretensão
    # (services/analise_pretensao_service.py — skills de diagnóstico BPC/BI) são
    # salvas aqui, separadas dos demais arquivos da raiz. Entram na MESMA aba
    # "Peças" da página do PAJ — caminho relativo prefixado com a subpasta,
    # mesmo padrão de `Arquivos/` (anexos_sisdpu, abaixo).
    pasta_pecas_ia = pasta / "Peças IA"
    if pasta_pecas_ia.exists() and pasta_pecas_ia.is_dir():
        for f in sorted(pasta_pecas_ia.iterdir()):
            if not f.is_file() or f.name in IGNORAR:
                continue
            item = {
                "nome": f.name,
                "caminho": f"Peças IA/{f.name}",
                "tipo": f.suffix.lstrip(".").lower(),
                "tamanho": f.stat().st_size,
                "modificado": f.stat().st_mtime,
            }
            nome_norm = _normalizar(f.name)
            if nome_norm.startswith(PREFIXOS_DESPACHO):
                despachos.append(item)
            else:
                pecas_judiciais.append(item)

    det = metadata.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes", []) or []
    movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0))
    max_seq_mov = max((int(m.get("seq", 0) or 0) for m in movs), default=0)
    # Data da ULTIMA movimentacao real (das movs estruturadas). NAO usar
    # metadata.data_mov_caixa: no fluxo atual ele cai no fallback = data de
    # abertura (o parser do sisdpu.txt nao acha movs), o que fazia a pagina do
    # PAJ mostrar "Ult. movim." igual a "Data abertura".
    ultima_mov = max(
        movs,
        key=lambda m: (m.get("data") or "", int(m.get("seq", 0) or 0)),
        default={},
    )

    # Anexos SISDPU: arquivos em `Arquivos/` baixados pelo sincronizador.
    # O OCR gera um `.txt` companion com o mesmo stem — associamos pro frontend
    # poder exibir "abrir PDF" + "ler OCR" lado a lado.
    anexos_sisdpu: list[dict] = []
    pasta_pecas = pasta / "Arquivos"
    if pasta_pecas.exists() and pasta_pecas.is_dir():
        # Mapeia companheiros OCR por stem
        txts_ocr: dict[str, Path] = {}
        for f in pasta_pecas.iterdir():
            if f.is_file() and f.suffix.lower() == ".txt":
                txts_ocr[f.stem] = f

        for f in sorted(pasta_pecas.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() == ".txt":
                continue  # o .txt e' listado junto com seu arquivo-fonte
            ocr_companion = txts_ocr.get(f.stem)
            anexos_sisdpu.append(
                {
                    "nome": f.name,
                    # caminho relativo a pasta do PAJ (usado por /files/<paj>/<path>)
                    "caminho": f"Arquivos/{f.name}",
                    "tipo": f.suffix.lstrip(".").lower(),
                    "tamanho": f.stat().st_size,
                    "modificado": f.stat().st_mtime,
                    "tem_ocr": ocr_companion is not None and ocr_companion.stat().st_size > 0,
                    "caminho_ocr": f"Arquivos/{ocr_companion.name}" if ocr_companion else None,
                }
            )

    # Extrai narrativa de sisdpu_raw
    sisdpu_raw = metadata.get("sisdpu_raw", {}) or {}
    narrativa = sisdpu_raw.get("narrativa", "")

    # id_processo / id_tramite: extraidos da URL de detalhamento gravada pelo sync
    # (sisdpu_client.movimentacoes_paj grava "url" em sisdpu_raw), no formato
    # ".../detalhamentoProcesso.xhtml?idTramite=NNN&caixaEntrada=true&id=NNN&tp=D".
    # Permite acionar o modulo "Ações no SISDPU" (movimentar/tramitar/concluir)
    # sem precisar de uma busca ao vivo — mesmo padrao usado em sat_service.add_paj_manual.
    url_detalhe = sisdpu_raw.get("url", "") or ""
    m_id = re.search(r"[?&]id=(\d+)", url_detalhe)
    m_tram = re.search(r"[?&]idTramite=(\d+)", url_detalhe)
    id_processo = m_id.group(1) if m_id else ""
    id_tramite = m_tram.group(1) if m_tram else ""

    # Processos judiciais vinculados (para o módulo "Ações no e-proc") — mesma
    # extração usada pela aba Retornos, reaproveitada em vez de duplicada.
    try:
        from services.retornos_service import _extrair_processos
        processos_judiciais = _extrair_processos(metadata)
    except Exception:  # noqa: BLE001
        processos_judiciais = []

    # Flags para o botão "Organizar Documentos para Ajuizamento" (habilita só
    # depois que uma petição inicial existir na pasta do PAJ).
    try:
        from services.organizar_ajuizamento_service import (
            pasta_ajuizamento_existe,
            tem_peticao_inicial,
        )
        _tem_peticao = tem_peticao_inicial(paj_norm)
        _tem_ajuizamento = pasta_ajuizamento_existe(paj_norm)
    except Exception:  # noqa: BLE001
        _tem_peticao = False
        _tem_ajuizamento = False

    return {
        "metadata": metadata,
        "narrativa": narrativa,
        "prompt_max": prompt_max,
        "pecas": [],
        "pecas_por_categoria": [],
        "decisoes": [],
        "despachos": despachos,
        "pecas_judiciais": pecas_judiciais,
        "pecas_geradas": despachos + pecas_judiciais,
        "anexos_sisdpu": anexos_sisdpu,
        "movimentacoes": movs_sorted,
        "max_seq_mov": max_seq_mov,
        "ultima_mov_data": ultima_mov.get("data", ""),
        "n_anexos_sisdpu": len(anexos_sisdpu),
        "prazos_abertos": metadata.get("prazos_abertos", []),
        "id_processo": id_processo,
        "id_tramite": id_tramite,
        "processos_judiciais": processos_judiciais,
        "tem_peticao_inicial": _tem_peticao,
        "tem_pasta_ajuizamento": _tem_ajuizamento,
        "lembretes": lembretes_service.ler_lembretes_pasta(pasta),
        "pasta": str(pasta),
    }


def ler_arquivo(paj_norm: str, caminho_relativo: str) -> tuple[Path | None, str]:
    """Retorna (path_absoluto, content_type) de um arquivo do PAJ."""
    pasta = PAJS_DIR / paj_norm
    arquivo = pasta / caminho_relativo

    try:
        arquivo.resolve().relative_to(pasta.resolve())
    except ValueError:
        return None, ""

    if not arquivo.exists() or not arquivo.is_file():
        return None, ""

    ext = arquivo.suffix.lower()
    content_types = {
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    return arquivo, content_types.get(ext, "application/octet-stream")


def abrir_pasta_paj(paj_norm: str) -> dict:
    """Abre a pasta do PAJ no Explorer da maquina onde o painel esta rodando.

    So funciona porque o painel roda localmente, na mesma maquina do usuario
    (nao teria sentido num deploy remoto) — mesmo principio de um app desktop
    com botao 'mostrar no Explorer'.
    """
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return {"ok": False, "erro": "Pasta do PAJ não encontrada"}

    try:
        os.startfile(str(pasta))  # noqa: S606 — Windows-only, uso local/desktop intencional
    except Exception as e:
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def limpar_anexos_paj(
    paj_norm: str,
    dry_run: bool = True,
    forcar: bool = False,
) -> dict:
    """Prepara (dry_run=True) ou executa (dry_run=False) a limpeza dos anexos
    SISDPU de um PAJ.

    Politica: so apaga o binario original (PDF/docx/imagem). MANTEM:
      - .txt OCR companion (texto extraido fica permanente)
      - metadata.json, sisdpu.txt, PROMPT_MAX.md (dados estruturais)
      - qualquer arquivo fora de Arquivos/ (pecas geradas pelo Claude, etc)

    Safeguards (consultados via dry_run; execute requer `forcar=True` se
    algum seguranca falhar):
      1. Todos os anexos PDF/docx precisam ter .txt OCR nao-vazio ao lado
      2. PAJ precisa estar CONCLUIDO (em_caixa_atual=False — fora da caixa)
         — PAJ ativo requer forcar=True (o usuario clicou "tenho certeza")

    Retorno:
        {
          "ok": bool,               # operacao bem-sucedida
          "pode_limpar": bool,      # safeguards passaram automaticamente
          "motivos_bloqueio": [...],# motivos pelos quais nao pode limpar sem forcar
          "em_caixa_atual": bool,
          "arquivos_a_remover": [{nome, tamanho, tem_ocr}],
          "arquivos_preservados": [{nome, tamanho, motivo}],
          "removidos": N,           # 0 em dry_run
          "bytes_liberados": N,
        }
    """
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return {
            "ok": False,
            "erro": "PAJ nao encontrado",
            "arquivos_a_remover": [],
            "arquivos_preservados": [],
        }

    metadata = _ler_json(pasta / "metadata.json") or {}
    em_caixa = metadata.get("em_caixa_atual", True)  # default: ativo

    pasta_pecas = pasta / "Arquivos"
    arquivos_a_remover: list[dict] = []
    arquivos_preservados: list[dict] = []

    if pasta_pecas.exists() and pasta_pecas.is_dir():
        # Mapeia OCRs existentes por stem
        txts_ocr: dict[str, Path] = {}
        for f in pasta_pecas.iterdir():
            if f.is_file() and f.suffix.lower() == ".txt":
                txts_ocr[f.stem] = f

        for f in sorted(pasta_pecas.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() == ".txt":
                # Preserva TODOS os .txt (sao os OCRs)
                arquivos_preservados.append(
                    {
                        "nome": f.name,
                        "tamanho": f.stat().st_size,
                        "motivo": "OCR companion (preservado)",
                    }
                )
                continue
            ocr_companion = txts_ocr.get(f.stem)
            tem_ocr = ocr_companion is not None and ocr_companion.stat().st_size > 0
            arquivos_a_remover.append(
                {
                    "nome": f.name,
                    "caminho": f"Arquivos/{f.name}",
                    "tamanho": f.stat().st_size,
                    "tem_ocr": tem_ocr,
                }
            )

    # Avalia safeguards
    motivos_bloqueio: list[str] = []
    sem_ocr = [a for a in arquivos_a_remover if not a["tem_ocr"]]
    if sem_ocr:
        motivos_bloqueio.append(
            f"{len(sem_ocr)} arquivo(s) sem OCR — perder o original apagaria o conteudo "
            "(OCR ausente/vazio pode indicar Tesseract nao instalado ou arquivo nao-PDF)"
        )
    if em_caixa:
        motivos_bloqueio.append(
            "PAJ esta ATIVO na caixa (em_caixa_atual=True) — limpar cedo demais pode "
            "dificultar elaboracao de pecas; recomendado aguardar conclusao"
        )

    pode_limpar = len(motivos_bloqueio) == 0
    bytes_total = sum(a["tamanho"] for a in arquivos_a_remover)

    resultado = {
        "ok": True,
        "pode_limpar": pode_limpar,
        "motivos_bloqueio": motivos_bloqueio,
        "em_caixa_atual": em_caixa,
        "arquivos_a_remover": arquivos_a_remover,
        "arquivos_preservados": arquivos_preservados,
        "removidos": 0,
        "bytes_liberados": 0,
        "bytes_total_disponivel": bytes_total,
    }

    if dry_run:
        return resultado

    # Execucao: exige pode_limpar OU forcar
    if not pode_limpar and not forcar:
        resultado["ok"] = False
        resultado["erro"] = "bloqueado por safeguards — passe forcar=True para override"
        return resultado

    removidos = 0
    bytes_liberados = 0
    for a in arquivos_a_remover:
        caminho = pasta_pecas / a["nome"]
        try:
            tam = caminho.stat().st_size
            caminho.unlink()
            removidos += 1
            bytes_liberados += tam
        except Exception:
            pass

    resultado["removidos"] = removidos
    resultado["bytes_liberados"] = bytes_liberados

    # Atualiza n_anexos_sisdpu no metadata pra refletir que foram apagados
    # (mantem o valor historico num campo separado pra nao perder)
    if removidos > 0:
        metadata["n_anexos_removidos"] = metadata.get("n_anexos_removidos", 0) + removidos
        metadata["anexos_removidos_em"] = metadata.get("anexos_removidos_em", [])
        from datetime import datetime

        metadata["anexos_removidos_em"].append(datetime.now().isoformat(timespec="seconds"))
        with contextlib.suppress(Exception):
            (pasta / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    return resultado


def listar_pecas_assistido(assistido: str) -> list[dict]:
    """Lista arquivos em Pecas Feitas/ cujo nome contenha termos do assistido."""
    if not assistido or not PECAS_FEITAS_DIR.exists():
        return []

    tokens = [_normalizar(t) for t in re.split(r"[\s,]+", assistido) if len(t) >= 4]
    if not tokens:
        return []

    resultado: list[dict] = []
    for f in PECAS_FEITAS_DIR.iterdir():
        if not f.is_file():
            continue
        nome_norm = _normalizar(f.name)
        if any(tok in nome_norm for tok in tokens):
            resultado.append(
                {
                    "nome": f.name,
                    "caminho": str(f),
                    "tipo": f.suffix.lstrip(".").lower(),
                    "tamanho": f.stat().st_size,
                    "modificado": f.stat().st_mtime,
                }
            )
    resultado.sort(key=lambda x: x["modificado"], reverse=True)
    return resultado


# ---------------------------------------------------------------------------
# Notas pessoais por PAJ — NOTAS.md na pasta do PAJ
# ---------------------------------------------------------------------------


def _validar_paj_norm(paj_norm: str) -> Path | None:
    """Valida paj_norm e retorna a pasta (ou None se invalido)."""
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return None
    if not paj_norm.startswith("PAJ-"):
        return None
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not pasta.is_dir():
        return None
    return pasta


def ler_notas(paj_norm: str) -> str:
    """Retorna conteudo de NOTAS.md (string vazia se nao existe)."""
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return ""
    f = pasta / "NOTAS.md"
    if not f.exists():
        return ""
    try:
        return f.read_text(encoding="utf-8")
    except Exception:
        return ""


def salvar_notas(paj_norm: str, texto: str) -> bool:
    """Salva NOTAS.md. Retorna False se PAJ invalido."""
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return False
    try:
        (pasta / "NOTAS.md").write_text(texto or "", encoding="utf-8")
        return True
    except Exception:
        return False
