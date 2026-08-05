"""Rotas do módulo genérico "Ações no SISDPU" (movimentar/tramitar/concluir),
reutilizável em qualquer página do painel — não depende da Retornos.

- GET /api/acoes-sisdpu/preferencias — favoritas/frequentes de fases e caixas.
- GET /api/acoes-sisdpu/executar     — SSE: executa mov→tram→conclui em um PAJ.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import acoes_sisdpu_service, config_painel_service

router = APIRouter()


@router.get("/api/acoes-sisdpu/preferencias", response_class=JSONResponse)
async def api_preferencias():
    """Favoritas (dropdown) + frequentes (badges) de fases/caixas — do cache local
    (instantâneo, sem navegar no SISDPU). Mesma fonte da aba Configurações › SISDPU."""
    return JSONResponse(config_painel_service.get_sisdpu_prefs())


async def _stream_executar(id_processo: str, paj: str, fase: str, descricao: str,
                           grupo: str, prazo: int, prazo_data: str, id_tramite: str,
                           concluir: bool, dry: bool):
    async for ev in acoes_sisdpu_service.executar_stream(
        id_processo, paj=paj, fase=fase, descricao=descricao, grupo=grupo or None,
        prazo_dias=prazo, prazo_data=prazo_data, id_tramite=id_tramite or None,
        concluir=concluir, dry_run=dry,
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/acoes-sisdpu/executar")
async def executar(id_processo: str, paj: str = "", fase: str = "", descricao: str = "",
                   grupo: str = "", prazo: int = 0, prazo_data: str = "",
                   id_tramite: str = "", concluir: int = 0, dry: int = 0):
    """SSE: movimenta (fase + despacho) → tramita (se grupo) → conclui (se concluir=1 E
    id_tramite informado — sem id_tramite não há trâmite específico a concluir)."""
    return EventSourceResponse(_stream_executar(
        id_processo, paj, fase, descricao, grupo, int(prazo), prazo_data,
        id_tramite, bool(int(concluir)), bool(int(dry))))
