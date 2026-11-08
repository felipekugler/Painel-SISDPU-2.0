"""PAJ arquivado → pasta do PAJ para a Lixeira do Windows (libera espaço em
disco; dá para restaurar pela Lixeira até ela ser esvaziada).

Decisão do Defensor (11/10/2026): ao gravar pelo painel uma movimentação cuja
fase começa com "Arquivado" — as 22 fases do SIS-DPU 2.0, inclusive
"Arquivado provisoriamente. …" —, a pasta INTEIRA do PAJ vai para a Lixeira:
documentos baixados do SISDPU e o que o painel gerou (Peças IA, análises,
cálculos, conversas). "Paj desarquivado" e "Dpgf cientificado. Arquivamento
homologado." não disparam.

Limpeza POSTERGADA (06/11/2026, pedido do Defensor): o arquivamento só AGENDA a
Lixeira para 2 dias depois — prazo para correções e conferências, com a página
do PAJ continuando a abrir normalmente. `laco()` (iniciado no startup do app)
confere a agenda a cada hora e, vencido o prazo, manda a pasta para a Lixeira se
o PAJ continua arquivado. "Paj desarquivado" gravado pelo painel cancela o
agendamento; um desarquivamento feito fora do painel também cancela, desde que
o PAJ tenha sido relido depois (status deixou de ser "Arquivado").
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import re
import threading
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from config import PAJS_DIR

log = logging.getLogger("paj_lixeira")

_RE_PAJ_NORM = re.compile(r"^PAJ-\d{4}-\d{3}-\d+$")

PRAZO_LIXEIRA = timedelta(days=2)
_AGENDA = Path(__file__).resolve().parents[1].parent / "dados" / "_lixeira_agendada.json"
_INTERVALO_LACO_S = 3600
_lock = threading.Lock()


def fase_de_arquivamento(fase: str) -> bool:
    return (fase or "").strip().lower().startswith("arquivado")


def fase_de_desarquivamento(fase: str) -> bool:
    return (fase or "").strip().lower().startswith("paj desarquivado")


# --------------------------------------------------------------------------
# Agenda (dados/_lixeira_agendada.json: {paj_norm: {"arquivado_em", "lixeira_em"}})
# --------------------------------------------------------------------------

def _ler_agenda() -> dict:
    try:
        return json.loads(_AGENDA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar_agenda(agenda: dict) -> None:
    _AGENDA.parent.mkdir(parents=True, exist_ok=True)
    tmp = _AGENDA.with_suffix(".tmp")
    tmp.write_text(json.dumps(agenda, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_AGENDA)


def agendar(paj_norm: str) -> dict:
    """PAJ arquivado agora → pasta para a Lixeira daqui a 2 dias. Reagendar
    (novo arquivamento) recomeça o prazo."""
    if not _RE_PAJ_NORM.match(paj_norm or ""):
        return {"ok": False, "detalhe": f"PAJ inválido: {paj_norm!r}"}
    agora = datetime.now().replace(microsecond=0)
    quando = agora + PRAZO_LIXEIRA
    with _lock:
        agenda = _ler_agenda()
        agenda[paj_norm] = {"arquivado_em": agora.isoformat(), "lixeira_em": quando.isoformat()}
        _gravar_agenda(agenda)
    log.info("PAJ %s arquivado: pasta irá para a Lixeira em %s", paj_norm, quando.isoformat())
    return {"ok": True, "lixeira_em": quando.isoformat(),
            "detalhe": f"A pasta do PAJ irá para a Lixeira em {quando:%d/%m/%Y às %H:%M} "
                       "(2 dias para correções e conferências)."}


def cancelar(paj_norm: str, motivo: str = "") -> bool:
    with _lock:
        agenda = _ler_agenda()
        if agenda.pop(paj_norm, None) is None:
            return False
        _gravar_agenda(agenda)
    log.info("PAJ %s: envio à Lixeira cancelado%s", paj_norm, f" ({motivo})" if motivo else "")
    return True


def agendado(paj_norm: str) -> dict | None:
    return _ler_agenda().get(paj_norm)


def _ainda_arquivado(paj_norm: str, arquivado_em: str) -> bool:
    """Status local do PAJ. Só considera DESARQUIVADO se o PAJ foi relido depois
    do arquivamento e o status deixou de ser "Arquivado" — sem releitura, vale o
    arquivamento que o próprio painel gravou."""
    try:
        meta = json.loads((Path(PAJS_DIR) / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    status = ((meta.get("detalhes_sisdpu") or {}).get("status_paj") or "").strip()
    relido_depois = (meta.get("ultima_sincronizacao_em") or "") > (arquivado_em or "")
    return status == "Arquivado" or not relido_depois


def processar_vencidos(agora: datetime | None = None) -> list[dict]:
    """Manda para a Lixeira as pastas cujo prazo venceu (e que continuam arquivadas)."""
    agora = agora or datetime.now()
    feitos = []
    for paj_norm, item in list(_ler_agenda().items()):
        if (item.get("lixeira_em") or "") > agora.isoformat():
            continue
        if not _ainda_arquivado(paj_norm, item.get("arquivado_em") or ""):
            cancelar(paj_norm, "PAJ não está mais arquivado")
            feitos.append({"paj_norm": paj_norm, "acao": "cancelado"})
            continue
        r = enviar_paj_para_lixeira(paj_norm)
        if r.get("ok"):
            cancelar(paj_norm, "prazo vencido, pasta enviada")
        feitos.append({"paj_norm": paj_norm, "acao": "lixeira", **r})
    return feitos


async def laco() -> None:
    """Confere a agenda ao subir o painel e depois a cada hora."""
    while True:
        try:
            await asyncio.to_thread(processar_vencidos)
        except Exception:  # noqa: BLE001
            log.exception("falha ao processar a Lixeira agendada")
        await asyncio.sleep(_INTERVALO_LACO_S)


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
