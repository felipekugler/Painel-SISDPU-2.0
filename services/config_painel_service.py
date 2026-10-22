"""Configurações persistidas do Painel — toggles controláveis pelo usuário via UI.

Armazenadas em painel_config.json na raiz do projeto. Ausência do arquivo equivale
a todos os defaults ligados (comportamento original do sistema).
"""

import contextlib
import json
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path

_CONFIG_FILE = Path(__file__).parent.parent / "painel_config.json"
_CONFIG_FILE_STR = str(_CONFIG_FILE)

DEFAULTS: dict = {}


def ler_config() -> dict:
    # Usa os.path.isfile (não Path.exists) para evitar falso-negativo dentro de
    # contextos async no Python 3.14/Windows — mesmo problema do headless shell.
    if os.path.isfile(_CONFIG_FILE_STR):
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            pass
    return dict(DEFAULTS)


def _ler_raw() -> dict:
    """Lê o JSON cru (preserva TODAS as chaves, inclusive as fora de DEFAULTS)."""
    if os.path.isfile(_CONFIG_FILE_STR):
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _salvar_raw(data: dict) -> None:
    """Grava o config de forma ATÔMICA (arquivo temp + os.replace) com algumas tentativas.
    Evita o `PermissionError`/sharing-violation intermitente no Windows quando o JSON está
    aberto/lido por outro processo (editor, outra requisição, antivírus)."""
    import time
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = str(_CONFIG_FILE) + ".tmp"
    ultimo_erro: Exception | None = None
    for _ in range(6):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(txt)
            os.replace(tmp, _CONFIG_FILE_STR)  # troca atômica
            return
        except PermissionError as e:  # sharing violation transitória → espera e retenta
            ultimo_erro = e
            time.sleep(0.2)
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            raise
    # esgotou as tentativas atômicas: tenta escrita direta como último recurso
    try:
        _CONFIG_FILE.write_text(txt, encoding="utf-8")
    except Exception:
        if ultimo_erro:
            raise ultimo_erro
        raise


def salvar_config(config: dict) -> dict:
    # Preserva chaves extras (ex.: preferências de tramitação) ao gravar os toggles.
    raw = _ler_raw()
    for k in DEFAULTS:
        raw[k] = bool(config.get(k, raw.get(k, DEFAULTS[k])))
    _salvar_raw(raw)
    return {k: raw[k] for k in DEFAULTS}


# --- Valores padrão da aba "Arquivos SAT Central" ---
# Caixa de tramitação padrão, modo/quantidade de prazo, tempo entre consultas e
# espera após bloqueio anti-robô. Todos assumem o valor de fábrica quando ausentes
# (comportamento original do painel). O nº de dias usa a chave própria
# `sat_prazo_dias`, específica desta aba.
SAT_CAIXA_PADRAO_DEFAULT = "01 - PREV_DIVPREV"
SAT_PRAZO_MODO_DEFAULT = "sem_prazo"  # "sem_prazo" | "dias"
SAT_PRAZO_DIAS_DEFAULT = 15           # dias de prazo quando o modo é "dias"
SAT_TEMPO_ENTRE_DEFAULT = 5           # segundos entre consultas ao SAT (mínimo)
SAT_TEMPO_ENTRE_MAX_DEFAULT = 15      # máximo — cada pausa é sorteada entre mínimo e máximo
SAT_ESPERA_BLOQUEIO_DEFAULT = 45      # segundos de espera ao detectar antirobô
SAT_COMPILAR_DEFAULT = True           # compilar por tipo (CCON/CRER/Laudos) quando > limiar
SAT_COMPILAR_LIMIAR_DEFAULT = 3       # compilar só quando houver MAIS de N docs do tipo


def get_sat_central_defaults() -> dict:
    """Defaults invocados pelos elementos da aba Arquivos SAT Central."""
    raw = _ler_raw()
    caixa = str(raw.get("sat_caixa_padrao") or "").strip() or SAT_CAIXA_PADRAO_DEFAULT
    modo = raw.get("sat_prazo_modo")
    if modo not in ("sem_prazo", "dias"):
        modo = SAT_PRAZO_MODO_DEFAULT
    try:
        dias = int(raw.get("sat_prazo_dias", SAT_PRAZO_DIAS_DEFAULT))
    except Exception:
        dias = SAT_PRAZO_DIAS_DEFAULT
    try:
        entre = int(raw.get("sat_tempo_entre", SAT_TEMPO_ENTRE_DEFAULT))
    except Exception:
        entre = SAT_TEMPO_ENTRE_DEFAULT
    entre = min(60, max(0, entre))
    try:
        entre_max = int(raw.get("sat_tempo_entre_max", SAT_TEMPO_ENTRE_MAX_DEFAULT))
    except Exception:
        entre_max = SAT_TEMPO_ENTRE_MAX_DEFAULT
    try:
        bloqueio = int(raw.get("sat_espera_bloqueio", SAT_ESPERA_BLOQUEIO_DEFAULT))
    except Exception:
        bloqueio = SAT_ESPERA_BLOQUEIO_DEFAULT
    compilar = raw.get("sat_compilar", SAT_COMPILAR_DEFAULT)
    try:
        limiar = int(raw.get("sat_compilar_limiar", SAT_COMPILAR_LIMIAR_DEFAULT))
    except Exception:
        limiar = SAT_COMPILAR_LIMIAR_DEFAULT
    return {
        "caixa": caixa,
        "prazo_modo": modo,
        "prazo_dias": min(365, max(1, dias)),
        "tempo_entre": entre,
        "tempo_entre_max": min(60, max(entre, entre_max)),
        "espera_bloqueio": min(300, max(0, bloqueio)),
        "compilar": bool(compilar),
        "compilar_limiar": min(50, max(1, limiar)),
    }


def set_sat_central_defaults(d: dict) -> dict:
    """Grava apenas as chaves presentes em `d`. Retorna os defaults resultantes."""
    raw = _ler_raw()
    if "caixa" in d:
        raw["sat_caixa_padrao"] = str(d.get("caixa") or "").strip() or SAT_CAIXA_PADRAO_DEFAULT
    if "prazo_modo" in d:
        raw["sat_prazo_modo"] = "dias" if d.get("prazo_modo") == "dias" else "sem_prazo"
    if "prazo_dias" in d:
        try:
            raw["sat_prazo_dias"] = min(365, max(1, int(d.get("prazo_dias"))))
        except Exception:
            pass
    if "tempo_entre" in d:
        try:
            raw["sat_tempo_entre"] = min(60, max(0, int(d.get("tempo_entre"))))
        except Exception:
            pass
    if "tempo_entre_max" in d:
        try:
            raw["sat_tempo_entre_max"] = min(60, max(0, int(d.get("tempo_entre_max"))))
        except Exception:
            pass
    if "espera_bloqueio" in d:
        try:
            raw["sat_espera_bloqueio"] = min(300, max(0, int(d.get("espera_bloqueio"))))
        except Exception:
            pass
    if "compilar" in d:
        raw["sat_compilar"] = bool(d.get("compilar"))
    if "compilar_limiar" in d:
        try:
            raw["sat_compilar_limiar"] = min(50, max(1, int(d.get("compilar_limiar"))))
        except Exception:
            pass
    _salvar_raw(raw)
    return get_sat_central_defaults()


# --- Espécies de PROCESSO ADMINISTRATIVO (PAT) a IGNORAR (não baixar) ---
# Lista configurável de nomes de "Serviço" do PAT irrelevantes; comparação por substring
# normalizada (sem acento). Semeada com 3 exemplos quando a chave nunca foi gravada.
SAT_PAT_IGNORAR_DEFAULT = [
    "Alterar Local ou Forma de Pagamento",
    "Atualizar Cadastro e/ou Benefício",
    "Bloquear/Desbloquear Benefício para Empréstimo Consignado",
]


def get_sat_pat_ignorar() -> list[str]:
    v = _ler_raw().get("sat_pat_ignorar")
    if isinstance(v, list):
        return [str(x) for x in v]
    return list(SAT_PAT_IGNORAR_DEFAULT)  # nunca configurado → sementes


def set_sat_pat_ignorar(itens: list[str]) -> list[str]:
    raw = _ler_raw()
    limpos: list[str] = []
    for x in (itens or []):
        s = str(x).strip()
        if s and s not in limpos:
            limpos.append(s)
    raw["sat_pat_ignorar"] = limpos
    _salvar_raw(raw)
    return limpos


# --- Tipos de documentos do SAT a baixar, POR PRETENSÃO ---
# Guarda `sat_tipos_por_pretensao` = {pretensao: [keys]} e `sat_tipos_padrao` = [keys]
# (aplicado às pretensões sem config). Padrão ausente = TODOS os tipos.
def get_sat_tipos_padrao() -> list[str] | None:
    v = _ler_raw().get("sat_tipos_padrao")
    return [str(x) for x in v] if isinstance(v, list) else None


def get_sat_tipos_por_pretensao() -> dict:
    v = _ler_raw().get("sat_tipos_por_pretensao", {})
    if not isinstance(v, dict):
        return {}
    return {str(k): [str(x) for x in (val or [])] for k, val in v.items()}


def get_sat_tipos_instituidor() -> list[str] | None:
    """Keys do que baixar do INSTITUIDOR (benefícios derivados). None = usar default."""
    v = _ler_raw().get("sat_tipos_instituidor")
    return [str(x) for x in v] if isinstance(v, list) else None


def set_sat_tipos(pretensao: str, keys: list[str]) -> dict:
    """Salva as keys de uma pretensão; '' ou '__padrao__' = PADRÃO; '__instituidor__' =
    seleção do instituidor. Retorna {padrao, por_pretensao, instituidor}."""
    raw = _ler_raw()
    keys = [str(k) for k in (keys or [])]
    pretensao = (pretensao or "").strip()
    if pretensao == "__instituidor__":
        raw["sat_tipos_instituidor"] = keys
    elif pretensao in ("", "__padrao__"):
        raw["sat_tipos_padrao"] = keys
    else:
        d = raw.get("sat_tipos_por_pretensao")
        if not isinstance(d, dict):
            d = {}
        d[pretensao] = keys
        raw["sat_tipos_por_pretensao"] = d
    _salvar_raw(raw)
    return {"padrao": raw.get("sat_tipos_padrao"),
            "por_pretensao": raw.get("sat_tipos_por_pretensao", {}),
            "instituidor": raw.get("sat_tipos_instituidor")}


def resolver_sat_tipos(pretensao: str, todos: list[str]) -> list[str]:
    """Keys a baixar p/ a pretensão: específica > padrão > todos. Lista vazia
    configurada (usuário desmarcou tudo) é respeitada."""
    pretensao = (pretensao or "").strip()
    por = get_sat_tipos_por_pretensao()
    if pretensao and pretensao in por:
        return por[pretensao]
    pad = get_sat_tipos_padrao()
    return pad if pad is not None else list(todos)


# --- Preferências SISDPU: fases de movimentação e caixas de tramitação ---
# Listas COMPLETAS importadas do SISDPU (raramente mudam) + curadoria do usuário:
#   favoritas -> preenchem o DROPDOWN da console de despacho (Retornos);
#   frequentes -> aparecem como BADGES de clique rápido.
def _lst(raw: dict, k: str) -> list[str]:
    v = raw.get(k)
    return [str(x) for x in v] if isinstance(v, list) else []


FONTES_DADOS = ("sisdpu", "mcp")


def get_fonte_dados() -> str:
    """De onde a sincronização LÊ o SISDPU: "sisdpu" (raspagem da tela do
    legado, padrão) ou "mcp" (MCP do SIS-DPU 2.0, mesmo banco). Ver
    ingestao/sisdpu2_fonte.py."""
    v = str(_ler_raw().get("fonte_dados") or "sisdpu")
    return v if v in FONTES_DADOS else "sisdpu"


def set_fonte_dados(valor: str) -> str:
    if valor not in FONTES_DADOS:
        raise ValueError(f"fonte de dados inválida: {valor!r}")
    raw = _ler_raw()
    raw["fonte_dados"] = valor
    _salvar_raw(raw)
    return valor


MCP_DOWNLOAD_ULTIMAS_MOVS_PADRAO = 5


def get_mcp_download_ultimas_movs() -> int:
    """Download AUTOMÁTICO dos anexos ao abrir o PAJ (fonte SIS-DPU 2.0): só os
    das N movimentações mais recentes (Configurações › Fonte de dados). Cada
    pedaço de arquivo conta no teto diário de leitura do servidor (16/10/2026)."""
    try:
        n = int(_ler_raw().get("mcp_download_ultimas_movs", MCP_DOWNLOAD_ULTIMAS_MOVS_PADRAO))
    except (TypeError, ValueError):
        n = MCP_DOWNLOAD_ULTIMAS_MOVS_PADRAO
    return max(0, min(n, 999))


def set_mcp_download_ultimas_movs(valor) -> int:
    n = max(0, min(int(valor), 999))
    raw = _ler_raw()
    raw["mcp_download_ultimas_movs"] = n
    _salvar_raw(raw)
    return n


def get_sisdpu_prefs() -> dict:
    """Todas as listas SISDPU (importadas + favoritas + frequentes)."""
    raw = _ler_raw()
    return {
        "fases_todas": _lst(raw, "sisdpu_fases_todas"),
        "caixas_todas": _lst(raw, "sisdpu_caixas_todas"),
        "fases_favoritas": _lst(raw, "sisdpu_fases_favoritas"),
        "caixas_favoritas": _lst(raw, "sisdpu_caixas_favoritas"),
        "fases_frequentes": _lst(raw, "sisdpu_fases_frequentes"),
        "caixas_frequentes": _lst(raw, "sisdpu_caixas_frequentes"),
        # opção pré-selecionada nos dropdowns; caixa_padrao "" = Sem tramitação
        "fase_padrao": str(raw.get("sisdpu_fase_padrao") or ""),
        "caixa_padrao": str(raw.get("sisdpu_caixa_padrao") or ""),
    }


def set_sisdpu_listas_todas(fases: list[str], caixas: list[str]) -> dict:
    """Grava as listas COMPLETAS importadas do SISDPU (mantém a curadoria existente,
    apenas descartando favoritas/frequentes que sumiram da lista importada)."""
    raw = _ler_raw()
    fases = [str(x) for x in (fases or [])]
    caixas = [str(x) for x in (caixas or [])]
    raw["sisdpu_fases_todas"] = fases
    raw["sisdpu_caixas_todas"] = caixas
    for k, universo in (("sisdpu_fases_favoritas", fases), ("sisdpu_fases_frequentes", fases),
                        ("sisdpu_caixas_favoritas", caixas), ("sisdpu_caixas_frequentes", caixas)):
        if isinstance(raw.get(k), list) and universo:
            raw[k] = [x for x in _lst(raw, k) if x in universo]
    _salvar_raw(raw)
    return get_sisdpu_prefs()


def set_sisdpu_prefs(d: dict) -> dict:
    """Grava as listas de curadoria presentes em `d` (favoritas/frequentes)."""
    raw = _ler_raw()
    for chave in ("fases_favoritas", "caixas_favoritas", "fases_frequentes", "caixas_frequentes"):
        if chave in d and isinstance(d[chave], list):
            raw["sisdpu_" + chave] = [str(x) for x in d[chave]]
    if "fase_padrao" in d:
        raw["sisdpu_fase_padrao"] = str(d.get("fase_padrao") or "")
    if "caixa_padrao" in d:
        raw["sisdpu_caixa_padrao"] = str(d.get("caixa_padrao") or "")
    _salvar_raw(raw)
    return get_sisdpu_prefs()


def get_eproc_eventos_prefs() -> dict:
    """Lista de eventos do e-proc — importada (ao vivo, por processo de referência) +
    favoritos (dropdown) + frequentes (badges) para a seção "Ações no Eproc"."""
    raw = _ler_raw()
    return {
        "eventos_todos": _lst(raw, "eproc_eventos_todos"),
        "eventos_favoritos": _lst(raw, "eproc_eventos_favoritos"),
        "eventos_frequentes": _lst(raw, "eproc_eventos_frequentes"),
        "evento_padrao": str(raw.get("eproc_evento_padrao") or ""),
        "processo_referencia": str(raw.get("eproc_processo_referencia") or ""),
    }


def set_eproc_eventos_todos(eventos: list[str], processo_referencia: str = "") -> dict:
    """Grava a lista COMPLETA de eventos importada do e-proc (mantém a curadoria
    existente, só descartando favoritos/frequentes que sumiram da lista nova)."""
    raw = _ler_raw()
    eventos = [str(x) for x in (eventos or [])]
    raw["eproc_eventos_todos"] = eventos
    if processo_referencia:
        raw["eproc_processo_referencia"] = str(processo_referencia)
    for k in ("eproc_eventos_favoritos", "eproc_eventos_frequentes"):
        if isinstance(raw.get(k), list) and eventos:
            raw[k] = [x for x in _lst(raw, k) if x in eventos]
    _salvar_raw(raw)
    return get_eproc_eventos_prefs()


def set_eproc_eventos_prefs(d: dict) -> dict:
    """Grava a curadoria (favoritos/frequentes/padrão) presente em `d`."""
    raw = _ler_raw()
    for chave in ("eventos_favoritos", "eventos_frequentes"):
        if chave in d and isinstance(d[chave], list):
            raw["eproc_" + chave] = [str(x) for x in d[chave]]
    if "evento_padrao" in d:
        raw["eproc_evento_padrao"] = str(d.get("evento_padrao") or "")
    _salvar_raw(raw)
    return get_eproc_eventos_prefs()


def get_eproc_tipos_doc_prefs() -> dict:
    """Lista de tipos de documento do e-proc — importada + favoritos (dropdown) +
    frequentes (badges), usada para classificar cada anexo antes de peticionar."""
    raw = _ler_raw()
    return {
        "tipos_todos": _lst(raw, "eproc_tipos_doc_todos"),
        "tipos_favoritos": _lst(raw, "eproc_tipos_doc_favoritos"),
        "tipos_frequentes": _lst(raw, "eproc_tipos_doc_frequentes"),
        "tipo_padrao": str(raw.get("eproc_tipo_doc_padrao") or ""),
        "processo_referencia": str(raw.get("eproc_processo_referencia") or ""),
    }


def set_eproc_tipos_doc_todos(tipos: list[str], processo_referencia: str = "") -> dict:
    """Grava a lista COMPLETA de tipos de documento importada do e-proc (mantém a
    curadoria existente, só descartando favoritos/frequentes que sumiram da lista nova)."""
    raw = _ler_raw()
    tipos = [str(x) for x in (tipos or [])]
    raw["eproc_tipos_doc_todos"] = tipos
    if processo_referencia:
        raw["eproc_processo_referencia"] = str(processo_referencia)
    for k in ("eproc_tipos_doc_favoritos", "eproc_tipos_doc_frequentes"):
        if isinstance(raw.get(k), list) and tipos:
            raw[k] = [x for x in _lst(raw, k) if x in tipos]
    _salvar_raw(raw)
    return get_eproc_tipos_doc_prefs()


def set_eproc_tipos_doc_prefs(d: dict) -> dict:
    """Grava a curadoria (favoritos/frequentes/padrão) presente em `d`."""
    raw = _ler_raw()
    mapa_chaves = {"tipos_favoritos": "eproc_tipos_doc_favoritos",
                   "tipos_frequentes": "eproc_tipos_doc_frequentes"}
    for chave, chave_raw in mapa_chaves.items():
        if chave in d and isinstance(d[chave], list):
            raw[chave_raw] = [str(x) for x in d[chave]]
    if "tipo_padrao" in d:
        raw["eproc_tipo_doc_padrao"] = str(d.get("tipo_padrao") or "")
    _salvar_raw(raw)
    return get_eproc_tipos_doc_prefs()


# Sigilo por documento: lista curta e estável (3 níveis no e-proc) — sem
# favoritos/frequentes (não faz sentido "curar" 3 itens), só a lista importada +
# o valor padrão de cada novo anexo (usuário pediu "Sem Sigilo" como padrão).
def get_eproc_sigilo_prefs() -> dict:
    raw = _ler_raw()
    return {
        "sigilos_todos": _lst(raw, "eproc_sigilos_todos"),
        "sigilo_padrao": str(raw.get("eproc_sigilo_padrao") or ""),
    }


def set_eproc_sigilo_padrao(valor: str) -> dict:
    """Grava o nível de sigilo padrão para novos anexos (usuário escolhe em Configurações)."""
    raw = _ler_raw()
    raw["eproc_sigilo_padrao"] = str(valor or "")
    _salvar_raw(raw)
    return get_eproc_sigilo_prefs()


def set_eproc_sigilos_todos(sigilos: list[str], processo_referencia: str = "") -> dict:
    """Grava a lista importada do e-proc. Se ainda não houver padrão definido (ou o
    padrão salvo sumiu da lista nova), escolhe automaticamente o item que contém
    "sem sigilo" (ou o primeiro da lista, se nenhum casar)."""
    raw = _ler_raw()
    sigilos = [str(x) for x in (sigilos or [])]
    raw["eproc_sigilos_todos"] = sigilos
    if processo_referencia:
        raw["eproc_processo_referencia"] = str(processo_referencia)
    padrao_atual = str(raw.get("eproc_sigilo_padrao") or "")
    if sigilos and padrao_atual not in sigilos:
        sem_sigilo = next((s for s in sigilos if "sem sigilo" in s.lower()), sigilos[0])
        raw["eproc_sigilo_padrao"] = sem_sigilo
    _salvar_raw(raw)
    return get_eproc_sigilo_prefs()


# Procurador/unidade destino do Gerenciamento de Processo (substabelecimento):
# lista institucional completa tem centenas de nomes — mesmo padrão de
# favoritos (dropdown) + frequentes (badges) usado em eventos/tipos de
# documento, mas cada item é {"valor": <code interno>, "rotulo": <nome
# exibido>} (o "valor" é o que precisa ir pro <select> do e-proc, não dá pra
# guardar só o rótulo como nas listas de string simples acima).
def get_eproc_procuradores_prefs() -> dict:
    """Lista de procuradores/unidades destino do Gerenciamento de Processo —
    importada (ao vivo, por processo de referência) + favoritos (dropdown) +
    frequentes (badges), mesmo padrão de eventos/tipos de documento."""
    raw = _ler_raw()
    return {
        "procuradores_todos": raw.get("eproc_procuradores_todos") or [],
        "procuradores_favoritos": raw.get("eproc_procuradores_favoritos") or [],
        "procuradores_frequentes": raw.get("eproc_procuradores_frequentes") or [],
        "procurador_padrao": raw.get("eproc_procurador_padrao") or None,
    }


def set_eproc_procuradores_todos(procuradores: list[dict], processo_referencia: str = "") -> dict:
    """Grava a lista COMPLETA importada do e-proc (mantém a curadoria existente,
    só descartando favoritos/frequentes/padrão cujo `valor` sumiu da lista nova).

    O próprio dropdown do e-proc lista alguns destinos DUAS VEZES (mesmo
    `valor`/código, ex. uma unidade "fixada" no topo e de novo na ordem
    alfabética) — sem deduplicar aqui, o `:key="p.valor"` do Alpine no `x-for`
    (Configurações e no bloco "Gerenciamento do processo") colide e a lista
    inteira deixa de renderizar (bug real 2026-08-17: 249 importados, grade
    vazia). Mantém a 1ª ocorrência de cada `valor`."""
    raw = _ler_raw()
    vistos: set[str] = set()
    procuradores_dedup: list[dict] = []
    for p in (procuradores or []):
        valor = str(p.get("valor") or "")
        if not valor or valor in vistos:
            continue
        vistos.add(valor)
        procuradores_dedup.append({"valor": valor, "rotulo": str(p.get("rotulo") or "")})
    procuradores = procuradores_dedup
    raw["eproc_procuradores_todos"] = procuradores
    if processo_referencia:
        raw["eproc_processo_referencia"] = str(processo_referencia)
    valores = {p["valor"] for p in procuradores}
    for k in ("eproc_procuradores_favoritos", "eproc_procuradores_frequentes"):
        atual = raw.get(k)
        if isinstance(atual, list) and procuradores:
            raw[k] = [p for p in atual if isinstance(p, dict) and p.get("valor") in valores]
    padrao = raw.get("eproc_procurador_padrao")
    if isinstance(padrao, dict) and procuradores and padrao.get("valor") not in valores:
        raw["eproc_procurador_padrao"] = None
    _salvar_raw(raw)
    return get_eproc_procuradores_prefs()


def set_eproc_procuradores_prefs(d: dict) -> dict:
    """Grava a curadoria (favoritos/frequentes/padrão) presente em `d`."""
    raw = _ler_raw()
    for chave in ("procuradores_favoritos", "procuradores_frequentes"):
        if chave in d and isinstance(d[chave], list):
            raw["eproc_" + chave] = [
                {"valor": str(p.get("valor") or ""), "rotulo": str(p.get("rotulo") or "")}
                for p in d[chave] if isinstance(p, dict) and p.get("valor")
            ]
    if "procurador_padrao" in d:
        p = d.get("procurador_padrao")
        raw["eproc_procurador_padrao"] = (
            {"valor": str(p.get("valor") or ""), "rotulo": str(p.get("rotulo") or "")}
            if isinstance(p, dict) and p.get("valor") else None
        )
    _salvar_raw(raw)
    return get_eproc_procuradores_prefs()


# --- Modelos de texto padrão (Ações no Eproc / Ações no SISDPU) ------------
# Textos padrão reutilizáveis — "Modelos de petição" (peticionamento no e-proc:
# aceitação de acordo, juntada de documento, ciência de RPV/precatório etc.) e
# "Despachos padrão" (movimentação no SISDPU) — mesmo formato {id, nome, texto},
# cadastrados manualmente pelo usuário (não vem de importação nenhuma) — CRUD
# simples, lista completa substituída a cada "Salvar" (curadoria feita inteira
# no front). Placeholders no texto ([NOME]/[CPF]/[PROCESSO]/[PAJ]/[LOCAL]/
# [DATA]) são resolvidos no FRONT na hora de usar (ver static/js/acoes_eproc.js
# e acoes_sisdpu.js), não aqui — aqui é só armazenamento.
def _chave_ordenacao_nome(item: dict) -> str:
    """Ordem alfabética tolerante a acento/caixa (ex.: "Ação" perto de
    "Acordo", não jogado pro fim por causa do "ç") — mesmo espírito de
    `_norm_oficio_key` acima, aqui só pra ordenar, não pra casar chave."""
    s = unicodedata.normalize("NFKD", str(item.get("nome") or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _get_modelos_texto(chave: str) -> list[dict]:
    raw = _ler_raw()
    itens = raw.get(chave) or []
    # Ordem alfabética por nome (pedido do usuário 2026-09-23) — tanto pra
    # listagem em Configurações quanto pro dropdown "Usar modelo"/"Usar
    # despacho" (Ações no Eproc/SISDPU), que consome a MESMA lista.
    return sorted(itens, key=_chave_ordenacao_nome)


def _set_modelos_texto(chave: str, modelos: list[dict]) -> list[dict]:
    """Substitui a lista COMPLETA de modelos — gera `id` pros itens novos
    (vêm do front sem id, ou com id vazio); descarta item sem nome. Devolve
    já em ordem alfabética (ver `_get_modelos_texto`) — o front usa o
    retorno direto pra atualizar a tela sem precisar recarregar."""
    raw = _ler_raw()
    limpos: list[dict] = []
    for m in modelos or []:
        if not isinstance(m, dict):
            continue
        nome = str(m.get("nome") or "").strip()
        if not nome:
            continue
        limpos.append({
            "id": str(m.get("id") or "").strip() or uuid.uuid4().hex[:8],
            "nome": nome,
            "texto": str(m.get("texto") or ""),
        })
    limpos.sort(key=_chave_ordenacao_nome)
    raw[chave] = limpos
    _salvar_raw(raw)
    return limpos


def get_eproc_modelos_peticao() -> list[dict]:
    return _get_modelos_texto("eproc_modelos_peticao")


def set_eproc_modelos_peticao(modelos: list[dict]) -> list[dict]:
    return _set_modelos_texto("eproc_modelos_peticao", modelos)


def get_sisdpu_modelos_despacho() -> list[dict]:
    return _get_modelos_texto("sisdpu_modelos_despacho")


def set_sisdpu_modelos_despacho(modelos: list[dict]) -> list[dict]:
    return _set_modelos_texto("sisdpu_modelos_despacho", modelos)


# --- "Petição com IA" (menu "Minutar Petição" da página do PAJ) — tipos
# cadastrados pelo usuário, cada um com uma instrução livre pra IA (ANTES
# disso, pedido do usuário 2026-10-02, cada tipo era um dict fixo no código
# apontando pra uma pasta de arquivos .md em teses-dpu — só eu conseguia
# criar um tipo novo). Dois jeitos de proposital diferentes de
# "Petição Padrão"/"Despachos padrão" acima:
#   1. Ordem de INSERÇÃO importa (é a ordem do menu) — não ordena por nome.
#   2. Além do texto, cada tipo pode ter arquivo(s) de referência fixos
#      (upload real, ver salvar_arquivo_referencia_minuta_ia abaixo) — por
#      isso não reaproveita `_get_modelos_texto`/`_set_modelos_texto`
#      (pensados só pra texto, {id,nome,texto}).
_CHAVE_MINUTAS_IA = "minutas_ia_tipos"

# Mesma convenção de `dados/` na raiz do PAINEL SISDPU (fora do workspace do
# Ofício Geral) usada por `acoes_eproc_service.RASCUNHOS_DIR` — aqui pasta
# por TIPO (não por PAJ), guardando os arquivos de referência fixos dele.
MINUTAS_IA_ARQUIVOS_DIR = Path(__file__).resolve().parent.parent.parent / "dados" / "minutas_ia_arquivos"


def minutas_ia_tipos_cadastrado() -> bool:
    """True se o cadastro já foi inicializado alguma vez (mesmo que o usuário
    tenha apagado todos os tipos depois) — distingue "nunca migrado" (deve
    semear os 3 tipos que já existiam embutidos em código, ver
    `minuta_ia_service._semear_tipos_padrao`) de "usuário esvaziou de
    propósito" (não deve semear de novo)."""
    return _CHAVE_MINUTAS_IA in _ler_raw()


def get_minutas_ia_tipos() -> list[dict]:
    """Ordem de INSERÇÃO (é a ordem do submenu "Petição com IA")."""
    return _ler_raw().get(_CHAVE_MINUTAS_IA) or []


def set_minutas_ia_tipos(tipos: list[dict]) -> list[dict]:
    """Substitui a lista COMPLETA — gera `id` pros itens novos (vêm do front
    sem id, ou com id vazio), descarta item sem `label`. Remove do disco a
    pasta de arquivos de referência de qualquer tipo que tenha SAÍDO da
    lista (evita lixo órfão)."""
    raw = _ler_raw()
    antigos_ids = {t.get("id") for t in (raw.get(_CHAVE_MINUTAS_IA) or [])}

    limpos: list[dict] = []
    for t in tipos or []:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()
        if not label:
            continue
        limpos.append({
            "id": str(t.get("id") or "").strip() or uuid.uuid4().hex[:8],
            "label": label,
            # Texto livre (ex.: "Gerais", "BI", "BPC", "Recursos") — só pra
            # AGRUPAR visualmente no submenu "Petição com IA" e na tela de
            # Configurações; vazio = sem categoria (pedido do usuário,
            # 2026-10-08). A ORDEM de exibição (categorias e itens dentro
            # delas) continua sendo a ordem de INSERÇÃO nesta lista — mover
            # um item sobe/desce ele na lista (ver front, botões ▲/▼).
            "categoria": str(t.get("categoria") or "").strip(),
            "instrucao": str(t.get("instrucao") or ""),
            # Vazio = usa o padrão do sistema ("peça curta, 3-6 parágrafos,
            # sem tópicos numerados") — ver minuta_ia_service._montar_prompt.
            # Só precisa preencher quando a peça exigir outra estrutura (ex.:
            # Contrarrazões de Recurso, mais substancial).
            "instrucao_estilo": str(t.get("instrucao_estilo") or ""),
            "documentos_sugeridos": str(t.get("documentos_sugeridos") or ""),
            "documentos_obrigatorios": bool(t.get("documentos_obrigatorios", True)),
            "arquivos_referencia": [
                {"nome": str(a.get("nome") or "")}
                for a in (t.get("arquivos_referencia") or [])
                if isinstance(a, dict) and a.get("nome")
            ],
        })

    novos_ids = {t["id"] for t in limpos}
    for removido in antigos_ids - novos_ids:
        if removido:
            with contextlib.suppress(Exception):
                shutil.rmtree(_minutas_ia_arquivo_dir(removido), ignore_errors=True)

    raw[_CHAVE_MINUTAS_IA] = limpos
    _salvar_raw(raw)
    return limpos


def _minutas_ia_arquivo_dir(tipo_id: str) -> Path:
    return MINUTAS_IA_ARQUIVOS_DIR / tipo_id


def salvar_arquivo_referencia_minuta_ia(tipo_id: str, nome_original: str, conteudo: bytes) -> dict:
    """Grava os BYTES de um arquivo de referência fixo do tipo, e já atualiza
    o cadastro (`arquivos_referencia`) — mesmo padrão de
    `acoes_eproc_service.salvar_arquivo_rascunho` (dedup por sufixo
    numérico), só que por TIPO em vez de por PAJ."""
    raw = _ler_raw()
    tipos = raw.get(_CHAVE_MINUTAS_IA) or []
    tipo = next((t for t in tipos if t.get("id") == tipo_id), None)
    if tipo is None:
        return {"ok": False, "erro": "Tipo de petição com IA não encontrado."}

    pasta = _minutas_ia_arquivo_dir(tipo_id)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^\w\-. ]", "_", nome_original or "arquivo")
    destino = pasta / nome
    if destino.exists():
        base, ext = destino.stem, destino.suffix
        i = 2
        while (pasta / f"{base}_{i}{ext}").exists():
            i += 1
        destino = pasta / f"{base}_{i}{ext}"
    destino.write_bytes(conteudo)

    tipo.setdefault("arquivos_referencia", []).append({"nome": destino.name})
    _salvar_raw(raw)
    return {"ok": True, "nome_arquivo": destino.name, "tamanho": len(conteudo)}


def remover_arquivo_referencia_minuta_ia(tipo_id: str, nome_arquivo: str) -> bool:
    raw = _ler_raw()
    tipos = raw.get(_CHAVE_MINUTAS_IA) or []
    tipo = next((t for t in tipos if t.get("id") == tipo_id), None)
    if tipo is None:
        return False

    antes = tipo.get("arquivos_referencia") or []
    depois = [a for a in antes if a.get("nome") != nome_arquivo]
    if len(depois) == len(antes):
        return False
    tipo["arquivos_referencia"] = depois
    _salvar_raw(raw)

    caminho = caminho_arquivo_referencia_minuta_ia(tipo_id, nome_arquivo)
    if caminho:
        with contextlib.suppress(Exception):
            caminho.unlink()
    return True


def caminho_arquivo_referencia_minuta_ia(tipo_id: str, nome_arquivo: str) -> Path | None:
    """Resolve o caminho garantindo que fique DENTRO da pasta do tipo (mesmo
    cuidado de `acoes_eproc_service.caminho_arquivo_rascunho` — nome vem de
    path param da URL)."""
    pasta = _minutas_ia_arquivo_dir(tipo_id).resolve()
    p = (pasta / (nome_arquivo or "")).resolve()
    if pasta not in p.parents and p != pasta:
        return None
    return p if p.exists() and p.is_file() else None


# --- Identificação do Ofício/Defensor (aba Geral das Configurações) ---
# 4 campos livres — Ofício, Unidade da DPU, Nome do defensor, Cargo do
# defensor — usados em 2 lugares: (1) compõem o texto sob "Painel SISDPU" na
# sidebar (ver base.html/app.py), substituindo o antigo OFICIO_DESCRICAO fixo
# no .env; (2) alimentam os placeholders [OFICIO]/[UNIDADE]/[DEFENSOR]/
# [CARGO] dos "Modelos de petição" (Ações no Eproc) e "despachos padrão"
# (Ações no SISDPU) — ver static/js/acoes_eproc.js/acoes_sisdpu.js. Pedido
# do usuário 2026-09-18.
#
# NÃO confundir com `_OFICIOS_SIGLAS_DEFAULT` acima: aquela é uma tabela
# nome->sigla usada pra ABREVIAR o ofício de CADA PAJ (podem ser vários,
# de qualquer unidade) nas listagens; esta aqui é um dado ÚNICO — a
# identificação do Defensor/ofício deste painel — sem relação uma com a
# outra além de descreverem, por coincidência, o mesmo tipo de informação.
_IDENTIFICACAO_ORGAO_CAMPOS = ("oficio", "unidade", "defensor_nome", "defensor_cargo")


def get_identificacao_orgao() -> dict:
    """{"oficio", "unidade", "defensor_nome", "defensor_cargo"} — todos string,
    "" se nunca configurado. Se o painel NUNCA foi configurado por aqui (chave
    ausente do painel_config.json) e existir o antigo `OFICIO_DESCRICAO` no
    .env, faz um best-effort de migração: tenta separar em "Ofício - Unidade"
    (formato usado até 2026-09-18, ex.: "4º Ofício Previdenciário - DPU
    Curitiba") só para PRÉ-PREENCHER a tela na primeira vez — não grava nada
    sozinho, o usuário confirma salvando."""
    v = _ler_raw().get("identificacao_orgao")
    if isinstance(v, dict):
        return {c: str(v.get(c) or "") for c in _IDENTIFICACAO_ORGAO_CAMPOS}

    oficio, unidade = "", ""
    with contextlib.suppress(Exception):
        import config as _config

        legado = (getattr(_config, "OFICIO_DESCRICAO", "") or "").strip()
        if legado and legado != "Defensoria Pública da União":
            partes = legado.split(" - ", 1)
            oficio = partes[0].strip()
            unidade = partes[1].strip() if len(partes) > 1 else ""
    return {"oficio": oficio, "unidade": unidade, "defensor_nome": "", "defensor_cargo": ""}


def set_identificacao_orgao(dados: dict) -> dict:
    limpo = {c: str((dados or {}).get(c) or "").strip() for c in _IDENTIFICACAO_ORGAO_CAMPOS}
    raw = _ler_raw()
    raw["identificacao_orgao"] = limpo
    _salvar_raw(raw)
    return limpo


# --- Tabela de Ofícios: nome completo -> SIGLA (padronização de exibição) ---
# Exibida no lugar do nome completo do ofício em todo o painel (dashboard, PAJ,
# Retornos). Editável na aba SISDPU das Configurações. O casamento nome->sigla é
# TOLERANTE (ver _norm_oficio_key): ignora acentos, caixa, zero à esquerda e o
# prefixo de área "B."/"C."/"E.". Semeada com os ofícios da DPU Curitiba.
_OFICIOS_SIGLAS_DEFAULT = [
    {"nome": "B. 01º OFÍCIO CÍVEL", "sigla": "CIV1"},
    {"nome": "B. 02º OFÍCIO CÍVEL", "sigla": "CIV2"},
    {"nome": "B. 03º OFÍCIO CÍVEL", "sigla": "CIV3"},
    {"nome": "B. 04º OFÍCIO CÍVEL", "sigla": "CIV4"},
    {"nome": "B. 05º OFÍCIO CÍVEL", "sigla": "CIV5"},
    {"nome": "C. 01º OFÍCIO CRIMINAL", "sigla": "CRIM1"},
    {"nome": "C. 02º OFÍCIO CRIMINAL", "sigla": "CRIM2"},
    {"nome": "C. 03º OFÍCIO CRIMINAL", "sigla": "CRIM3"},
    {"nome": "C. 04º OFÍCIO CRIMINAL", "sigla": "CRIM4"},
    {"nome": "C. 05º OFÍCIO CRIMINAL", "sigla": "CRIM5"},
    {"nome": "E. 01º OFÍCIO PREVIDENCIÁRIO", "sigla": "PREV1"},
    {"nome": "E. 02º OFÍCIO PREVIDENCIÁRIO", "sigla": "PREV2"},
    {"nome": "E. 03º OFÍCIO PREVIDENCIÁRIO", "sigla": "PREV3"},
    {"nome": "E. 04º OFÍCIO PREVIDENCIÁRIO", "sigla": "PREV4"},
]


def _norm_oficio_key(s: str) -> str:
    """Chave normalizada para casar nome de ofício de forma tolerante."""
    s = str(s or "")
    if re.search(r"anterior\s*:", s, re.IGNORECASE):
        return ""  # "Anterior: ..." não é o ofício atual
    s = s.split("|")[0]                        # texto antes de "|"
    s = re.sub(r"^\s*[A-Za-z]\.\s*", "", s)    # remove prefixo de área "E. "/"B. "/"C. "
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()  # sem acento, maiúsculo
    s = re.sub(r"[^A-Z0-9]+", " ", s)          # 'º', pontuação -> espaço
    s = re.sub(r"\b0+(\d)", r"\1", s)          # zero à esquerda: "03" -> "3"
    return re.sub(r"\s+", " ", s).strip()


def get_oficios_siglas() -> list[dict]:
    """Lista [{nome, sigla}] — a salva pelo usuário, ou os defaults se nunca configurada."""
    v = _ler_raw().get("oficios_siglas")
    if isinstance(v, list):
        out = []
        for it in v:
            if isinstance(it, dict):
                nome = str(it.get("nome", "")).strip()
                sigla = str(it.get("sigla", "")).strip()
                if nome and sigla:
                    out.append({"nome": nome, "sigla": sigla})
        if out:
            return out
    return [dict(x) for x in _OFICIOS_SIGLAS_DEFAULT]


def set_oficios_siglas(itens: list[dict]) -> list[dict]:
    """Grava a tabela de ofícios (dedup por nome normalizado). Lista vazia = volta
    aos defaults (get devolve as sementes). Retorna a lista resultante."""
    raw = _ler_raw()
    limpos: list[dict] = []
    vistos: set[str] = set()
    for it in (itens or []):
        if not isinstance(it, dict):
            continue
        nome = str(it.get("nome", "")).strip()
        sigla = str(it.get("sigla", "")).strip()
        if not nome or not sigla:
            continue
        k = _norm_oficio_key(nome)
        if k in vistos:
            continue
        vistos.add(k)
        limpos.append({"nome": nome, "sigla": sigla})
    raw["oficios_siglas"] = limpos
    _salvar_raw(raw)
    return get_oficios_siglas()


# --- Retenção do cache de documentos do e-proc (página "Consultar processo") ---
# Arquivos em dados/eproc_docs/<npu>/ são só CACHE — sempre re-baixáveis do
# e-proc — mas sem limpeza cresceriam pra sempre. Configurável para o usuário
# ajustar conforme a necessidade; default segue o mesmo espírito da rotação de
# logs (TimedRotatingFileHandler, 7 dias) só que mais generoso pro caso de uso.
EPROC_DOCS_RETENCAO_DIAS_DEFAULT = 30


def get_eproc_docs_retencao_dias() -> int:
    raw = _ler_raw()
    try:
        dias = int(raw.get("eproc_docs_retencao_dias", EPROC_DOCS_RETENCAO_DIAS_DEFAULT))
    except Exception:
        dias = EPROC_DOCS_RETENCAO_DIAS_DEFAULT
    return min(3650, max(1, dias))


def set_eproc_docs_retencao_dias(dias) -> int:
    try:
        dias = min(3650, max(1, int(dias)))
    except Exception:
        dias = EPROC_DOCS_RETENCAO_DIAS_DEFAULT
    raw = _ler_raw()
    raw["eproc_docs_retencao_dias"] = dias
    _salvar_raw(raw)
    return dias


# --- Modelo/esforço do Claude usado na ELABORAÇÃO (botões "Elaborar"/"Analisar") ---
# Editável em Configurações › Acesso › "Conexão com o Claude", sem precisar editar
# arquivo nem reiniciar o painel (chat_service resolve isto A CADA sessão nova, não
# faz cache). get_elaboracao_prefs() SEMPRE devolve um valor CONCRETO (nunca "") —
# se o usuário nunca escolheu nada na tela, resolve pro valor de fábrica embutido em
# config.py (ELABORACAO_MODELO/ELABORACAO_EFFORT) — assim a tela mostra direto a
# opção realmente em uso, sem precisar de um item "padrão" à parte na lista.
_EFFORTS_VALIDOS = ("low", "medium", "high", "xhigh", "max")


def get_elaboracao_prefs() -> dict:
    raw = _ler_raw()
    modelo = str(raw.get("elaboracao_modelo") or "").strip()
    effort = str(raw.get("elaboracao_effort") or "").strip().lower()
    if not modelo or effort not in _EFFORTS_VALIDOS:
        import config as _config

        modelo = modelo or (_config.ELABORACAO_MODELO or "opus[1m]")
        effort = effort if effort in _EFFORTS_VALIDOS else (_config.ELABORACAO_EFFORT or "xhigh")
    return {"modelo": modelo, "effort": effort}


def set_elaboracao_prefs(d: dict) -> dict:
    raw = _ler_raw()
    if "modelo" in d:
        raw["elaboracao_modelo"] = str(d.get("modelo") or "").strip()
    if "effort" in d:
        effort = str(d.get("effort") or "").strip().lower()
        if effort in _EFFORTS_VALIDOS:
            raw["elaboracao_effort"] = effort
    _salvar_raw(raw)
    return get_elaboracao_prefs()


def sigla_de_oficio(raw: str) -> str:
    """Converte um nome de ofício (cru, como vem do SISDPU) na SIGLA correspondente.
    Sem correspondência → devolve uma versão limpa do nome (sem prefixo de área/'|')."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    key = _norm_oficio_key(raw)
    if key:
        for it in get_oficios_siglas():
            if _norm_oficio_key(it.get("nome", "")) == key:
                return it.get("sigla", "") or raw
    limpo = re.sub(r"^\s*[A-Za-z]\.\s*", "", raw.split("|")[0]).strip()
    return limpo or raw
