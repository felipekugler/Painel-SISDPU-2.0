"""Worker de vida curta: manda um LOTE de arquivos/pastas para a Lixeira do
Windows, FORA do processo do servidor (23/10/2026 — ver
services/exclusao_suave.py).

Por que fora: o FortiEDR bloqueou o processo do painel (python.exe com socket
de rede) depois de exclusões em lote ("File Delete blocked") e, em seguida,
TODA gravação dele ("File Write blocked"). Aqui a exclusão roda num processo
separado, sem rede, de vida curta: se a proteção reagir, só este processo é
afetado — o painel continua gravando.

Como: uma ÚNICA chamada SHFileOperationW (FO_DELETE + FOF_ALLOWUNDO = Lixeira)
com todos os caminhos do lote — a mesma operação do Explorer ao "Excluir",
nunca arquivo a arquivo.

Uso: `python services/_lixeira_worker.py` com a lista JSON de caminhos na
entrada padrão. Saída (stdout): JSON {"codigo": int, "restantes": [...]}.
"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


_FO_DELETE = 0x0003
_FLAGS = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI


def para_lixeira(caminhos: list[str]) -> int:
    existentes = [c for c in caminhos if Path(c).exists()]
    if not existentes:
        return 0
    origem = "\0".join(existentes) + "\0"  # lista terminada em nulo duplo
    op = _SHFILEOPSTRUCTW(hwnd=None, wFunc=_FO_DELETE, pFrom=origem, pTo=None, fFlags=_FLAGS)
    codigo = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if not codigo and op.fAnyOperationsAborted:
        return -1
    return codigo


def main() -> int:
    try:
        caminhos = [str(c) for c in json.loads(sys.stdin.buffer.read().decode("utf-8") or "[]")]
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"codigo": -2, "erro": str(e), "restantes": []}))
        return 1
    codigo = para_lixeira(caminhos)
    restantes = [c for c in caminhos if Path(c).exists()]
    print(json.dumps({"codigo": codigo, "restantes": restantes}, ensure_ascii=False))
    return 0 if not restantes else 1


if __name__ == "__main__":
    sys.exit(main())
