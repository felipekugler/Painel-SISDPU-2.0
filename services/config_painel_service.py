"""Configurações persistidas do Painel — toggles controláveis pelo usuário via UI.

Armazenadas em painel_config.json na raiz do projeto. Ausência do arquivo equivale
a todos os defaults ligados (comportamento original do sistema).
"""

import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).parent.parent / "painel_config.json"
_CONFIG_FILE_STR = str(_CONFIG_FILE)

DEFAULTS: dict = {
    "download_narrativa": True,
}


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
    _CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def salvar_config(config: dict) -> dict:
    # Preserva chaves extras (ex.: preferências de tramitação) ao gravar os toggles.
    raw = _ler_raw()
    for k in DEFAULTS:
        raw[k] = bool(config.get(k, raw.get(k, DEFAULTS[k])))
    _salvar_raw(raw)
    return {k: raw[k] for k in DEFAULTS}


# --- Preferências de tramitação (caixas favoritas + prazo padrão) ---
GRUPOS_FAVORITOS_MAX = 3
PRAZO_TRAMITACAO_DEFAULT = 15


def get_prazo_tramitacao_padrao() -> int:
    try:
        v = int(_ler_raw().get("prazo_tramitacao_padrao", PRAZO_TRAMITACAO_DEFAULT))
    except Exception:
        v = PRAZO_TRAMITACAO_DEFAULT
    return min(365, max(1, v))


def set_prazo_tramitacao_padrao(dias: int) -> int:
    v = min(365, max(1, int(dias)))
    raw = _ler_raw()
    raw["prazo_tramitacao_padrao"] = v
    _salvar_raw(raw)
    return v


def get_grupos_favoritos() -> list[str]:
    g = _ler_raw().get("grupos_favoritos", [])
    return [str(x) for x in g][:GRUPOS_FAVORITOS_MAX] if isinstance(g, list) else []


def add_grupo_favorito(grupo: str) -> list[str]:
    grupo = (grupo or "").strip()
    favs = get_grupos_favoritos()
    if grupo and grupo not in favs and len(favs) < GRUPOS_FAVORITOS_MAX:
        favs.append(grupo)
        raw = _ler_raw()
        raw["grupos_favoritos"] = favs
        _salvar_raw(raw)
    return favs


def del_grupo_favorito(grupo: str) -> list[str]:
    favs = [g for g in get_grupos_favoritos() if g != (grupo or "").strip()]
    raw = _ler_raw()
    raw["grupos_favoritos"] = favs
    _salvar_raw(raw)
    return favs


# --- Valores padrão da aba "Arquivos SAT Central" ---
# Caixa de tramitação padrão, modo/quantidade de prazo, tempo entre consultas e
# espera após bloqueio anti-robô. Todos assumem o valor de fábrica quando ausentes
# (comportamento original do painel). É uma padronização ESPECÍFICA da aba: o nº de
# dias usa a chave própria `sat_prazo_dias`, independente do prazo geral de tramitação
# (`prazo_tramitacao_padrao`, editado em "Preferências de tramitação").
SAT_CAIXA_PADRAO_DEFAULT = "01 - PREV_DIVPREV"
SAT_PRAZO_MODO_DEFAULT = "sem_prazo"  # "sem_prazo" | "dias"
SAT_PRAZO_DIAS_DEFAULT = 15           # dias de prazo quando o modo é "dias"
SAT_TEMPO_ENTRE_DEFAULT = 5           # segundos entre consultas ao SAT
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
        "tempo_entre": min(60, max(0, entre)),
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


def limpar_dados_narrativa() -> int:
    """Limpa a narrativa em todos os PAJs: substitui sisdpu.txt e zera metadata.json.
    Retorna o número de PAJs alterados."""
    import re as _re
    import json as _json
    from config import PAJS_DIR as _PAJS_DIR
    placeholder_txt = "Narrativa: Teor da narrativa desabilitado no painel de Configuração"
    count = 0
    for paj_dir in _PAJS_DIR.iterdir():
        if not paj_dir.is_dir() or not paj_dir.name.startswith("PAJ-"):
            continue
        alterado = False
        # 1) sisdpu.txt — substitui linha "Narrativa: ..."
        sisdpu_txt = paj_dir / "sisdpu.txt"
        if os.path.isfile(str(sisdpu_txt)):
            try:
                texto = sisdpu_txt.read_text(encoding="utf-8")
                novo, n = _re.subn(
                    r"^Narrativa: .+$",
                    placeholder_txt,
                    texto,
                    flags=_re.MULTILINE,
                )
                if n:
                    sisdpu_txt.write_text(novo, encoding="utf-8")
                    alterado = True
            except Exception:
                pass
        # 2) metadata.json — zera campo narrativa e sisdpu_raw.narrativa (fonte da UI)
        meta_path = paj_dir / "metadata.json"
        if os.path.isfile(str(meta_path)):
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                changed = False
                if meta.get("narrativa"):
                    meta["narrativa"] = ""
                    changed = True
                sraw = meta.get("sisdpu_raw")
                if isinstance(sraw, dict) and sraw.get("narrativa"):
                    sraw["narrativa"] = ""
                    changed = True
                if changed:
                    meta_path.write_text(
                        _json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    alterado = True
            except Exception:
                pass
        if alterado:
            count += 1
    return count
