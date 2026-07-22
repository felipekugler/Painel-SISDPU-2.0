"""Rotas de segurança: encerramento de painel, bloqueio, etc."""

import os
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
PID_FILE = BASE_DIR / ".server.pid"


def _encerrar_servidor() -> None:
    """Remove o PID file e derruba o processo do servidor. Roda como BackgroundTask,
    ou seja, DEPOIS que a resposta HTTP já foi enviada ao navegador (que então exibe a
    tela final de 'painel encerrado'). Usa os._exit para um desligamento imediato e
    confiável no Windows — a porta é reaproveitada na próxima subida (app.py faz
    _cleanup_port + espera antes de escutar)."""
    try:
        if PID_FILE.exists():
            pid_file.unlink(missing_ok=True)
    except Exception:
        pass
    time.sleep(0.4)  # garante que a resposta terminou de sair antes de matar o processo
    os._exit(0)


@router.post("/api/encerrar", response_class=JSONResponse)
async def api_encerrar():
    """Encerra o painel: fecha o navegador e desliga o servidor.

    O desligamento é agendado como BackgroundTask, que só executa APÓS a resposta ser
    enviada — assim o navegador recebe {ok:true} e mostra a tela final antes do
    processo cair. Os dados já salvos permanecem em disco."""
    detalhe = ""
    return JSONResponse(
        {"ok": True, "detalhe": detalhe},
        background=BackgroundTask(_encerrar_servidor),
    )
