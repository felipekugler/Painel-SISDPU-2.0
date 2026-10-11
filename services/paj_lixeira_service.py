"""PAJ arquivado → pasta do PAJ para a Lixeira do Windows (libera espaço em
disco; dá para restaurar pela Lixeira até ela ser esvaziada).

Decisão do Defensor (11/10/2026): ao gravar pelo painel uma movimentação cuja
fase começa com "Arquivado" — as 22 fases do SIS-DPU 2.0, inclusive
"Arquivado provisoriamente. …" —, a pasta INTEIRA do PAJ vai para a Lixeira:
documentos baixados do SISDPU e o que o painel gerou (Peças IA, análises,
cálculos, conversas). "Paj desarquivado" e "Dpgf cientificado. Arquivamento
homologado." não disparam.
"""

from __future__ import annotations

import ctypes
import logging
import re
from ctypes import wintypes
from pathlib import Path

from config import PAJS_DIR

log = logging.getLogger("paj_lixeira")

_RE_PAJ_NORM = re.compile(r"^PAJ-\d{4}-\d{3}-\d+$")


def fase_de_arquivamento(fase: str) -> bool:
    return (fase or "").strip().lower().startswith("arquivado")


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


_FO_DELETE = 0x0003
_FOF_SILENT, _FOF_NOCONFIRMATION, _FOF_ALLOWUNDO, _FOF_NOERRORUI = 0x0004, 0x0010, 0x0040, 0x0400


def _para_lixeira(caminho: Path) -> int:
    """SHFileOperationW com FOF_ALLOWUNDO = vai para a Lixeira (nunca exclusão
    definitiva). Devolve o código do Windows (0 = sucesso)."""
    op = _SHFILEOPSTRUCTW(hwnd=None, wFunc=_FO_DELETE, pFrom=str(caminho) + "\0", pTo=None,
                          fFlags=_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI)
    codigo = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if not codigo and op.fAnyOperationsAborted:
        return -1
    return codigo


def enviar_paj_para_lixeira(paj_norm: str) -> dict:
    """Manda a pasta do PAJ para a Lixeira. Só aceita PAJ-AAAA-UUU-NNNNN dentro
    da pasta de PAJs do painel (nunca outro caminho)."""
    if not _RE_PAJ_NORM.match(paj_norm or ""):
        return {"ok": False, "detalhe": f"PAJ inválido: {paj_norm!r}"}
    raiz = Path(PAJS_DIR).resolve()
    pasta = (raiz / paj_norm).resolve()
    if pasta.parent != raiz:
        return {"ok": False, "detalhe": "Caminho fora da pasta de PAJs — nada apagado."}
    if not pasta.is_dir():
        return {"ok": True, "detalhe": "Pasta do PAJ já não existia em disco.", "ja_ausente": True}
    tamanho = sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())
    codigo = _para_lixeira(pasta)
    if codigo or pasta.exists():
        log.warning("PAJ %s: Lixeira falhou (código %s)", paj_norm, codigo)
        return {"ok": False, "detalhe": f"Não foi possível enviar a pasta do PAJ para a Lixeira (código {codigo})."}
    log.info("PAJ %s arquivado: pasta enviada para a Lixeira (%.1f MB)", paj_norm, tamanho / 1048576)
    return {"ok": True, "detalhe": f"Pasta do PAJ enviada para a Lixeira ({tamanho / 1048576:.1f} MB).",
            "bytes": tamanho}
