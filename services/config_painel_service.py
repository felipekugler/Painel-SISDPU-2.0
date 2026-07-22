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
    "download_prazos": True,
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


def limpar_dados_prazos() -> int:
    """Apaga o JSONL de prazos pendentes. Retorna o número de registros removidos."""
    from config import PAJS_DIR as _PAJS_DIR
    jsonl = _PAJS_DIR / ".pendentes_calendar.jsonl"
    if not os.path.isfile(str(jsonl)):
        return 0
    try:
        linhas = [l for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        count = len(linhas)
        jsonl.unlink()
        return count
    except Exception:
        return 0


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
