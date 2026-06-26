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


def salvar_config(config: dict) -> dict:
    merged = {k: bool(config.get(k, DEFAULTS[k])) for k in DEFAULTS}
    _CONFIG_FILE.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return merged


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
