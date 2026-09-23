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
    "calcular_cnis.json",
    "calcular_qs.json",
    "valor_causa.json",
}

# Extensoes que contam como "peça" de verdade na aba Peças da pagina do PAJ.
# Sem essa allowlist, QUALQUER arquivo solto na raiz da pasta do PAJ que nao
# estivesse em IGNORAR aparecia ali — inclusive arquivos internos de
# funcionamento (foto_assistido.jpg, LEMBRETES.json, .json intermediarios de
# skills como valor_causa/calculo_cnis, etc.), sem nenhuma relacao com peca
# judicial (pedido do usuario 2026-09-18).
EXTENSOES_PECA = {"pdf", "docx", "doc", "odt", "rtf", "txt"}

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


def _texto_fase_limpo(fases: str) -> str:
    """'· Arquivado. Desinteresse do assistido;' -> 'Arquivado. Desinteresse
    do assistido' — remove o marcador/bullet do início e o ';' final que o
    SISDPU usa nessa coluna (ver _ultima_fase_arquivamento)."""
    t = (fases or "").strip()
    t = re.sub(r"^\W+", "", t)  # bullet/marcador não-alfanumérico no início
    return t.rstrip(";").strip()


def _ultima_fase_arquivamento(movs: list[dict]) -> dict | None:
    """Última movimentação (maior seq) cuja fase começa com "Arquivado" —
    identifica motivo/seq/data do arquivamento pra exibir no banner "PAJ
    arquivado" do cabeçalho (ver templates/paj_detail.html). None se nenhuma
    movimentação tiver essa fase (PAJ nunca foi arquivado, ou foi
    desarquivado depois — nesse caso a fase mais recente é "Paj
    desarquivado", que não começa com "Arquivado" e não entra aqui)."""
    candidatas = []
    for m in movs or []:
        texto = _texto_fase_limpo(m.get("fases", ""))
        if texto.lower().startswith("arquivado"):
            candidatas.append((int(m.get("seq", 0) or 0), texto, m))
    if not candidatas:
        return None
    seq, texto, m = max(candidatas, key=lambda x: x[0])
    data_original = (m.get("data_original") or "").strip()
    data_curta = data_original.split(" ")[0] if data_original else (m.get("data") or "")
    return {"seq": seq, "fase": texto, "data": data_curta}


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

    # Badge "PI" (petição inicial minutada pela IA) da coluna Análise —
    # mesma checagem que já habilita o botão "Organizar Documentos para
    # Ajuizamento" na página do PAJ (ver services/organizar_ajuizamento_service).
    from services.organizar_ajuizamento_service import tem_peticao_inicial as _tem_peticao_inicial

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

        em_caixa = metadata.get("em_caixa_atual")

        # `tramitacoes_caixa` (lista, uma entrada por tramitação pendente na
        # caixa — geral, retorno OU sat, cada uma com `subtipo`, ver
        # ingestao.sincronizador._atualizar_tramitacoes_caixa) é AUTORITATIVA
        # quando presente: um PAJ pode ter 2+ tramitações simultâneas, e só sai
        # do dashboard quando NENHUMA restar (de qualquer subtipo — desde
        # 2026-09-19, quando a lista passou a incluir retorno/sat também, não
        # só geral). Supera em_caixa_atual/categoria_caixa (campos únicos,
        # sobrescritos a cada sync) pra PAJs já sincronizados com essa lógica.
        # PAJs ainda não resincronizados (campo ausente) caem no comportamento
        # antigo, abaixo. `em_caixa_atual` entra como segunda camada de
        # segurança (independente, setado a cada sync) contra qualquer
        # inconsistência entre as duas leituras de caixa usadas pelo
        # sincronizador.
        tramitacoes_caixa = metadata.get("tramitacoes_caixa")
        if tramitacoes_caixa is not None:
            esta_concluido = len(tramitacoes_caixa) == 0 and em_caixa is not True
            if esta_concluido and not incluir_concluidos:
                continue
        else:
            # Filtro de concluidos: so exclui se o flag esta EXPLICITAMENTE False
            esta_concluido = em_caixa is False
            if esta_concluido and not incluir_concluidos:
                continue

            # Decisão REVERTIDA 2026-09-18 (pedido do usuário): PAJs de
            # Retornos/SAT ficavam OMITIDOS da lista geral do dashboard, só
            # apareciam no submenu próprio — dava a impressão de que menos
            # PAJs tinham sido sincronizados do que de fato foram. Agora o
            # dashboard chama com `excluir_submenu=False` (ver routes/
            # dashboard.py) e esses PAJs aparecem nos dois lugares. O
            # parâmetro continua existindo (default `True`) pros OUTROS
            # chamadores desta função que não foram revisados nesse pedido
            # (ex.: services/chat_service.py, services/cota_service.py) —
            # não alterar o default aqui sem confirmar se eles precisam
            # continuar excluindo. Fail-safe: categoria ausente/"" → PAJ
            # geral, aparece normalmente independente do parâmetro.
            if excluir_submenu and metadata.get("categoria_caixa"):
                continue

        # PAJs adicionados via watchlist (busca explicita pelo defensor) NAO
        # aparecem no dashboard — sao visiveis so na watchlist e via URL direta.
        # Excecao: se uma sync regular ja confirmou em_caixa_atual=True, e' um
        # PAJ "promovido" para a caixa e volta a aparecer normalmente.
        if metadata.get("via_watchlist") and em_caixa is not True:
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

        # Contagem de pecas GERADAS pelo defensor/Claude (na raiz do PAJ) —
        # mesmo filtro por extensao da aba Peças (EXTENSOES_PECA), senão
        # arquivos internos (foto, LEMBRETES.json, calcular_cnis.json etc.)
        # inflavam essa contagem tambem (pedido do usuario 2026-09-18).
        n_pecas_geradas = 0
        for f in pasta.iterdir():
            if not f.is_file() or f.name in IGNORAR:
                continue
            if f.suffix.lstrip(".").lower() not in EXTENSOES_PECA:
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
            # Todas as tramitações GERAIS pendentes na caixa deste PAJ (pode
            # ser mais de uma — comum no SISDPU) — cada uma com seu próprio
            # id_tramite, pra concluir individualmente. Lista vazia = PAJ
            # ainda não resincronizado com essa lógica (ver `tramitacoes_caixa`
            # acima) ou realmente sem tramitação geral aberta.
            "tramitacoes_caixa": tramitacoes_caixa or [],
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
            # Badge "PI" da coluna Análise — já existe petição inicial
            # minutada pela IA na pasta (Peças IA/ ou raiz do PAJ, mesma
            # checagem do botão "Organizar Documentos para Ajuizamento").
            "tem_peticao_inicial": _tem_peticao_inicial(paj_norm),
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


_RE_DIGITOS = re.compile(r"\d+")
_TAMANHO_CHAVE = 12  # confirmado (usuário + amostra real): a chave de acesso do e-proc tem sempre 12 dígitos, ex. "352882565025"


def _extrair_chave_de_texto(texto: str) -> str:
    """Extrai a chave de acesso de um texto livre — sempre uma sequência de
    EXATOS 12 dígitos (ex.: "352882565025"), mesmo com rótulo/quebra de linha
    ao redor no campo `observacoes_processo` do PAJ — confirmado varrendo os
    PAJs reais: só o número ("737324295824"), com rótulo e quebra de linha
    ("Chave Processo:\\n300160906026"), ou rótulo com espaço
    ("Chave: 737324295824"). Filtra pelo tamanho exato em vez de "a sequência
    mais longa" — mais preciso, e ignora de propósito qualquer outra sequência
    de dígitos de tamanho diferente que apareça no texto (ex.: um ano)."""
    candidatos = [d for d in _RE_DIGITOS.findall(texto or "") if len(d) == _TAMANHO_CHAVE]
    return candidatos[0] if candidatos else ""


def buscar_chave_por_processo(numero_processo: str) -> dict | None:
    """Procura, em TODOS os PAJs, a chave de acesso cadastrada pro processo
    judicial informado — campo `observacoes_processo` de `processos_judiciais`
    no metadata.json (cadastrado manualmente na narrativa/instauração do PAJ,
    ex.: "Nº Processo: ...  Chave para Consulta  ...").

    Usado pelo botão "Atualizar com chave" da tela de consulta de processo —
    ação pontual e manual (não fica em loop nem em polling), por isso varre
    todos os PAJs sem cache: ~100 arquivos pequenos, custo desprezível numa
    chamada isolada.

    Retorna {"chave": "...", "paj_norm": "PAJ-AAAA-UUU-NNNNN", "paj": "AAAA/UUU-NNNNN"}
    do primeiro PAJ que tiver esse processo cadastrado com uma chave extraível,
    ou None se nenhum PAJ tiver essa chave (processo sem chave cadastrada, ou
    sem PAJ vinculado)."""
    alvo = re.sub(r"\D", "", numero_processo or "")
    if not alvo or not PAJS_DIR.exists():
        return None

    for pasta in PAJS_DIR.iterdir():
        if not _e_pasta_paj(pasta):
            continue
        metadata = _ler_json(pasta / "metadata.json")
        if not isinstance(metadata, dict):
            continue

        candidatos_proc = list(metadata.get("processos_judiciais") or [])
        # Também considera o par singular no nível raiz (mesma informação,
        # formato mais antigo/redundante do migrador do SISDPU).
        if metadata.get("processo_judicial"):
            candidatos_proc.append({
                "processo_judicial": metadata.get("processo_judicial"),
                "observacoes_processo": metadata.get("observacoes_processo", ""),
            })

        for proc in candidatos_proc:
            num_proc = re.sub(r"\D", "", str(proc.get("processo_judicial") or ""))
            if num_proc != alvo:
                continue
            chave = _extrair_chave_de_texto(proc.get("observacoes_processo") or "")
            if chave:
                return {"chave": chave, "paj_norm": pasta.name, "paj": metadata.get("paj", "")}
    return None


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

    # Arquivos gerados na raiz do PAJ (peticao*, recurso*, despacho*, .docx,
    # .pdf, etc.) — a aba "Despachos" foi removida (pedido do usuário
    # 2026-09-18, junto com o resto da seção "Outros"); os antigos arquivos
    # "despacho*" agora entram na mesma lista de peças, sem distinção.
    pecas_judiciais: list[dict] = []

    for f in sorted(pasta.iterdir()):
        if not f.is_file() or f.name in IGNORAR:
            continue
        tipo = f.suffix.lstrip(".").lower()
        if tipo not in EXTENSOES_PECA:
            continue

        _stat = f.stat()
        pecas_judiciais.append({
            "nome": f.name,
            "caminho": f.name,
            "tipo": tipo,
            "tamanho": _stat.st_size,
            "modificado": _stat.st_mtime,
        })

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
            tipo = f.suffix.lstrip(".").lower()
            if tipo not in EXTENSOES_PECA:
                continue
            _stat = f.stat()
            pecas_judiciais.append({
                "nome": f.name,
                "caminho": f"Peças IA/{f.name}",
                "tipo": tipo,
                "tamanho": _stat.st_size,
                "modificado": _stat.st_mtime,
            })

    det = metadata.get("detalhes_sisdpu", {}) or {}
    movs = det.get("movimentacoes", []) or []
    movs_sorted = sorted(movs, key=lambda m: int(m.get("seq", 0) or 0))
    max_seq_mov = max((int(m.get("seq", 0) or 0) for m in movs), default=0)
    arquivamento = _ultima_fase_arquivamento(movs)
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

    # Assistidos PF vinculados ao PAJ — normalmente 1 (o titular), mas PAJs
    # coletivos/familiares podem ter vários (ver sisdpu_client.py, extração da
    # tabela "Assistido(s) Pessoa(s) Física:"). Cada item já vem com
    # {idPessoa, nome, cpf, principal} — o principal é o único sem o ícone
    # "Excluir este assistido" no SISDPU. Usado pela seção "Assistidos" da
    # página do PAJ, que só aparece quando há mais de um.
    assistidos_pf = sisdpu_raw.get("assistidos_pf") or []

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
        "pecas_judiciais": pecas_judiciais,
        "pecas_geradas": pecas_judiciais,
        "anexos_sisdpu": anexos_sisdpu,
        "assistidos_pf": assistidos_pf,
        "movimentacoes": movs_sorted,
        "max_seq_mov": max_seq_mov,
        "arquivamento": arquivamento,
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


def definir_whatsapp_numeros(paj_norm: str, numeros: list[str]) -> dict | None:
    """Escolha MANUAL de quais telefones (já extraídos do SISDPU) valem como
    WhatsApp — o SISDPU não distingue isso, então é o único dado de Contatos
    que não vem da sincronização. Persiste em metadata.json (sobrevive a
    re-sincronizações — ver `_montar_contatos` em ingestao/sincronizador.py).

    Retorna o `contatos` atualizado, ou None se o PAJ não existir."""
    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        return None

    metadata = _ler_json(meta_path) or {}
    numeros_validos = [n.strip() for n in (numeros or []) if n and n.strip()]
    metadata["whatsapp_numeros"] = numeros_validos
    contatos = metadata.get("contatos") or {
        "app_sisdpu": False, "telefone": False, "whatsapp": False,
        "email": False, "endereco": False,
    }
    contatos["whatsapp"] = bool(numeros_validos)
    metadata["contatos"] = contatos

    meta_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return contatos


async def registrar_pos_execucao_sisdpu(paj_norm: str, resultado: dict) -> dict:
    """Efeitos após uma execução do console genérico "Ações no SISDPU"
    (`static/js/acoes_sisdpu.js`) NA PÁGINA DO PAJ — seja pelo próprio console
    (movimentar/tramitar/concluir) ou pelo atalho "Concluir PAJ da Caixa de
    Entrada" do cabeçalho (que roda o mesmo endpoint com concluir=1): registra
    a atividade no Histórico do painel e, quando o trâmite foi de fato
    concluído, marca o PAJ como fora da caixa (`em_caixa_atual=False`) — some
    do dashboard/caixa de entrada IMEDIATAMENTE, sem esperar a próxima
    sincronização.

    IMPORTANTE — PAJ com MAIS DE UM trâmite pendente na caixa (comum: o mesmo
    processo tramitado pra grupos/pessoas diferentes em momentos diferentes,
    cada um com seu próprio `id_tramite`): concluir_paj() só afeta o
    id_tramite ESPECÍFICO passado — nunca conclui "tudo daquele PAJ" de uma
    vez. Por isso, antes de marcar `em_caixa_atual=False`, confirmamos AO VIVO
    (`sisdpu_movtram.caixas_abertas_paj`) que não resta NENHUM outro trâmite
    aberto pra este PAJ — senão o PAJ sumiria do dashboard com pendência real
    ainda na caixa. Falha ao confirmar (sessão caiu, etc.) = não esconde (erra
    pro lado seguro: falso "ainda na caixa" é preferível a falso "concluído").

    Mesma lógica de `retornos_service.registrar_pos_execucao`, adaptada pra
    PAJs abertos direto pela própria página (não pelo store da aba Retornos).
    `resultado` é o payload enriquecido que `acoesSisdpuBlock.onExecutado`
    devolve (ok/status/concluido/tramitado_para/fase/descricao_mov_texto/...).

    `resultado["concluidos"]` (lista, NOVO): ids de trâmite concluídos agora OU já
    ausentes nesta execução — cobre a seleção de 1 a N tramitações da caixa direto
    no console "Ações no SISDPU" da página do PAJ. Sem essa lista (chamador legado,
    ex.: Retornos), cai no fallback singular `resultado["idTramite"]`.
    """
    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    # PAJ ainda não sincronizado localmente (fluxo de "Movimentações pendentes"
    # — ver services.acoes_sisdpu_service/paj_link_service): registra no
    # Histórico do painel MESMO ASSIM logo abaixo (não depende de metadata.json,
    # só de campos que já vêm no próprio `resultado`) — só o que depende de
    # fato da pasta em disco (marcar "fora da caixa", atualizar
    # tramitacoes_caixa) fica pulado, sem erro, até uma sincronização real
    # trazer o PAJ pra cá. Bug real 2026-09-14: nada era registrado nesse caso,
    # nem o que já dava pra registrar sem PAJ local nenhum.
    paj_sincronizado = meta_path.exists()

    status = (resultado or {}).get("status")
    ok = bool((resultado or {}).get("ok"))
    ja_ausente = status == "ja_ausente"
    if not (ok or ja_ausente):
        return {"ok": False, "detalhe": "Execução sem êxito — nada a registrar."}

    metadata = _ler_json(meta_path) or {}
    fase = ((resultado or {}).get("fase") or "").strip()
    concluido = bool((resultado or {}).get("concluido"))
    tram_caixa = (resultado or {}).get("tramitado_para") or ""
    # "descricao_mov_texto" (TEXTO PURO, sem tags) é preferida pro Histórico — a
    # movimentação pode ser HTML de verdade (editor rico Quill); os demais campos
    # só entram como fallback de compatibilidade.
    descricao_final = ((resultado or {}).get("descricao_mov_texto") or (resultado or {}).get("descricao_mov")
                        or (resultado or {}).get("descricao_tram") or "").strip()

    algo_efetivado = bool(concluido or tram_caixa or fase)
    if algo_efetivado:
        if concluido:
            atividade = "Conclusão do PAJ na Caixa de Entrada"
        elif tram_caixa:
            atividade = "Tramitação do PAJ na Caixa de Entrada"
        else:
            atividade = "Movimentação do PAJ na Caixa de Entrada"
        from services import historico_painel

        historico_painel.registrar({
            "paj": metadata.get("paj", ""), "paj_norm": paj_norm,
            "assistido": metadata.get("assistido_caixa", ""),
            "origem": "paj", "atividade": atividade,
            "movimentacao": fase or "—", "tramitado_para": tram_caixa,
            "concluido": concluido, "detalhe": descricao_final[:300],
        })

    saiu_da_caixa = False
    outros_tramites_abertos = 0
    # Sem metadata.json local não há tramitacoes_caixa nem "fora da caixa" pra
    # marcar de verdade — pula direto (o registro no Histórico acima já
    # aconteceu, independente disso). Uma sincronização real, mais tarde,
    # traz o estado correto da caixa pra este PAJ.
    if paj_sincronizado and (concluido or ja_ausente):
        # "concluidos" (NOVO — lista): ids concluídos agora OU já ausentes nesta
        # execução, devolvidos por acoes_sisdpu_service.executar_stream — cobre
        # tanto 1 quanto N trâmites selecionados de uma vez na página do PAJ.
        # "idTramite" (singular) é o fallback pro par legado (ex.: Retornos, cujo
        # trâmite fica fora de tramitacoes_caixa e nunca manda a lista).
        concluidos_raw = (resultado or {}).get("concluidos")
        if isinstance(concluidos_raw, list) and concluidos_raw:
            ids_concluidos = [str(i).strip() for i in concluidos_raw if str(i).strip()]
        else:
            id_tramite_concluido = str((resultado or {}).get("idTramite") or "").strip()
            ids_concluidos = [id_tramite_concluido] if id_tramite_concluido else []
        tramitacoes_caixa = metadata.get("tramitacoes_caixa")

        if tramitacoes_caixa is not None:
            # Modelo novo: a lista já é escopada à MINHA caixa (ver
            # ingestao.sincronizador._atualizar_tramitacoes_caixa) — remover os
            # ids concluídos dali é suficiente, sem precisar de nova consulta ao
            # vivo. Sem nenhum id informado (ex.: chamada antiga), não arrisca
            # remover o item errado — não decide.
            if ids_concluidos:
                await _dar_baixa_retorno_se_aplicavel(tramitacoes_caixa, ids_concluidos)
                ids_set = set(ids_concluidos)
                restantes = [
                    t for t in tramitacoes_caixa
                    if str(t.get("id_tramite") or "").strip() not in ids_set
                ]
                outros_tramites_abertos = len(restantes)
                metadata["tramitacoes_caixa"] = restantes
                # `tramitacoes_caixa` agora inclui geral+retorno+sat
                # (2026-09-19), então "nada restante" já significa mesmo fora
                # da caixa, de qualquer subtipo.
                if not restantes:
                    from datetime import datetime

                    metadata["em_caixa_atual"] = False
                    metadata["concluido_em"] = datetime.now().isoformat(timespec="seconds")
                    saiu_da_caixa = True
                meta_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                invalidar_cache_listagem()
        else:
            # Modelo antigo (PAJ ainda não passou por uma sync com a lista) —
            # confirma ao vivo se não resta nenhum outro trâmite (qualquer
            # grupo/pessoa, não só minha caixa) antes de esconder do dashboard.
            sisdpu_raw = metadata.get("sisdpu_raw", {}) or {}
            url_detalhe = sisdpu_raw.get("url", "") or ""
            m_id = re.search(r"[?&]id=(\d+)", url_detalhe)
            id_processo = m_id.group(1) if m_id else ""

            outros_abertos = None  # None = nao foi possivel confirmar (nao decide)
            if id_processo:
                from ingestao import sisdpu_movtram

                try:
                    outros_abertos = await sisdpu_movtram.caixas_abertas_paj(id_processo)
                except Exception:  # noqa: BLE001
                    outros_abertos = None

            if outros_abertos == []:
                from datetime import datetime

                metadata["em_caixa_atual"] = False
                metadata["concluido_em"] = datetime.now().isoformat(timespec="seconds")
                meta_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                invalidar_cache_listagem()
                saiu_da_caixa = True
            elif outros_abertos:
                outros_tramites_abertos = len(outros_abertos)

    return {
        "ok": True, "concluido": concluido, "saiu_da_caixa": saiu_da_caixa,
        "outros_tramites_abertos": outros_tramites_abertos,
    }


async def _dar_baixa_retorno_se_aplicavel(
    tramitacoes_caixa: list[dict] | None, ids_concluidos: list[str]
) -> None:
    """Reconciliação pedida pelo usuário 2026-09-19: desde que trâmites de
    Retorno/SAT passaram a aparecer em `tramitacoes_caixa` (junto com os
    gerais — ver `ingestao.sincronizador._atualizar_tramitacoes_caixa`) e
    podem ser concluídos por QUALQUER caminho genérico (pill do dashboard,
    "Concluir PAJ da Caixa de Entrada", módulo "Ações no SISDPU"), um
    trâmite de Retorno concluído por um desses caminhos — em vez do botão
    próprio da página Retornos — precisa "dar baixa" no store dela também,
    senão ela continua mostrando como pendente até a próxima sincronização
    manual da Retornos. `retornos_service.registrar_pos_execucao` já existe
    e já faz exatamente isso (é o mesmo hook que a própria página Retornos
    usa) — só precisamos chamá-lo aqui pra cada id concluído que for
    subtipo "retorno". Arquivos SAT não precisa de nada: sua lista é sempre
    lida ao vivo da caixa, sem store próprio — um trâmite concluído
    simplesmente não aparece mais lá na próxima vez que a página carregar."""
    if not tramitacoes_caixa or not ids_concluidos:
        return
    ids_set = set(ids_concluidos)
    retorno_ids = [
        t.get("id_tramite") for t in tramitacoes_caixa
        if t.get("subtipo") == "retorno" and t.get("id_tramite") in ids_set
    ]
    if not retorno_ids:
        return
    from services import retornos_service

    for rid in retorno_ids:
        with contextlib.suppress(Exception):
            await retornos_service.registrar_pos_execucao(rid, {"ok": True, "concluido": True})


async def concluir_tramites_caixa(paj_norm: str, id_tramites: list[str]) -> dict:
    """Conclui uma ou mais tramitações ESPECÍFICAS da caixa de entrada deste
    PAJ, cada uma pelo seu próprio `id_tramite` — chamado tanto pelo pill "N
    trâmites" do dashboard (conclusão individual) quanto pelo modal de escolha
    do botão "Concluir PAJ da Caixa de Entrada" quando há mais de uma
    pendente (o usuário marca quais quer concluir de uma vez).

    `movtram.concluir_paj()` já aceita uma lista de ids — aqui só orquestramos
    o registro no Histórico (uma entrada por trâmite efetivamente concluído),
    a atualização de `tramitacoes_caixa`/`em_caixa_atual`, e — desde
    2026-09-19, quando trâmites de Retorno/SAT passaram a poder ser
    concluídos por aqui também (não só pela página Retornos) — a
    reconciliação do store da Retornos pra ela "dar baixa" no que foi
    concluído externamente (ver `_dar_baixa_retorno_se_aplicavel` abaixo)."""
    pasta = PAJS_DIR / paj_norm
    meta_path = pasta / "metadata.json"
    if not meta_path.exists():
        return {"ok": False, "detalhe": "PAJ não encontrado."}

    ids = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "detalhe": "Nenhum trâmite selecionado."}

    from ingestao import sisdpu_movtram

    try:
        res = await sisdpu_movtram.concluir_paj(ids)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": f"{type(e).__name__}: {e}"}

    resultados = res.get("resultados") or {}
    if not resultados:
        return {"ok": False, "detalhe": res.get("detalhe", "Falha ao concluir — sem resultado do SISDPU.")}

    metadata = _ler_json(meta_path) or {}
    tramitacoes_caixa = metadata.get("tramitacoes_caixa")

    from services import historico_painel

    concluidos_ok: list[str] = []
    for i in ids:
        r = resultados.get(i, {})
        if r.get("concluido") or r.get("ja_ausente"):
            concluidos_ok.append(i)
            tramite_info = next(
                (t for t in (tramitacoes_caixa or []) if t.get("id_tramite") == i), {}
            )
            historico_painel.registrar({
                "paj": metadata.get("paj", ""), "paj_norm": paj_norm,
                "assistido": metadata.get("assistido_caixa", ""),
                "origem": "paj", "atividade": "Conclusão do PAJ na Caixa de Entrada",
                "movimentacao": "—", "tramitado_para": "",
                "concluido": True, "detalhe": (tramite_info.get("descricao") or "")[:300],
            })

    await _dar_baixa_retorno_se_aplicavel(tramitacoes_caixa, concluidos_ok)

    saiu_da_caixa = False
    outros_tramites_abertos = 0
    if tramitacoes_caixa is not None:
        restantes = [t for t in tramitacoes_caixa if t.get("id_tramite") not in concluidos_ok]
        outros_tramites_abertos = len(restantes)
        metadata["tramitacoes_caixa"] = restantes
        # `tramitacoes_caixa` agora inclui geral+retorno+sat (2026-09-19), então
        # "nada restante" já significa mesmo fora da caixa, de qualquer subtipo.
        if not restantes:
            from datetime import datetime

            metadata["em_caixa_atual"] = False
            metadata["concluido_em"] = datetime.now().isoformat(timespec="seconds")
            saiu_da_caixa = True
        meta_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        invalidar_cache_listagem()

    return {
        "ok": bool(concluidos_ok), "concluidos": concluidos_ok,
        "saiu_da_caixa": saiu_da_caixa, "outros_tramites_abertos": outros_tramites_abertos,
        "detalhe": res.get("detalhe", ""),
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


def excluir_arquivo_paj(paj_norm: str, caminho: str) -> dict:
    """Exclui UM arquivo de peça da pasta do PAJ (aba Peças — ícone de
    lixeira). `caminho` é relativo à pasta do PAJ, igual ao campo
    `item["caminho"]` que `ler_paj` devolve (arquivo na raiz ou em
    "Peças IA/...").

    Bloqueia: caminho fora da pasta do PAJ (path traversal), arquivos de
    sistema (`IGNORAR`) e qualquer coisa dentro de `Arquivos/` — os anexos do
    SISDPU têm fluxo próprio de limpeza (`limpar_anexos_paj`), com safeguards
    específicos (PAJ ativo na caixa, etc.) que não fazem sentido aqui.
    """
    pasta = (PAJS_DIR / paj_norm).resolve()
    if not pasta.exists() or not _e_pasta_paj(pasta):
        return {"ok": False, "erro": "PAJ não encontrado"}

    if not caminho or not caminho.strip():
        return {"ok": False, "erro": "caminho obrigatório"}

    alvo = (pasta / caminho).resolve()
    try:
        alvo.relative_to(pasta)
    except ValueError:
        return {"ok": False, "erro": "caminho inválido"}

    if not alvo.is_file():
        return {"ok": False, "erro": "arquivo não encontrado"}
    if alvo.name in IGNORAR:
        return {"ok": False, "erro": "arquivo protegido — não pode ser excluído por aqui"}
    if alvo.parent.name == "Arquivos":
        return {"ok": False, "erro": "anexos do SISDPU têm fluxo próprio (botão \"Limpar anexos\")"}

    try:
        alvo.unlink()
    except Exception as e:
        return {"ok": False, "erro": f"falha ao excluir: {type(e).__name__}: {e}"}

    invalidar_cache_listagem()
    return {"ok": True, "excluido": caminho}


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
