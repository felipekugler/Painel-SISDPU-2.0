"""Rotas da feature "Retornos".

- GET  /retornos                        — página (tabela de atendimentos de retorno).
- GET  /api/retornos/lista              — registros já sincronizados (store local).
- GET  /api/retornos/sincronizar        — SSE: lê a caixa, filtra os retornos,
                                          detalha e analisa com a IA leve.
- POST /api/retornos/{id_tramite}/sugestao — salva a edição manual da sugestão.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import retornos_service

router = APIRouter()


@router.get("/retornos", response_class=HTMLResponse)
async def pagina_retornos(request: Request):
    template = request.app.state.jinja.get_template("retornos.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/retornos/lista", response_class=JSONResponse)
async def api_lista():
    return JSONResponse(retornos_service.listar())


async def _stream_sincronizar():
    async for ev in retornos_service.sincronizar_stream():
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/retornos/sincronizar")
async def sincronizar():
    """SSE: lê a caixa de entrada, filtra os PAJs com trâmite de 'Atendimento de
    Retorno', detalha cada um (movimentações + descrição integral) e dispara a IA
    leve (resumo + sugestão)."""
    return EventSourceResponse(_stream_sincronizar())


@router.post("/api/retornos/{id_tramite}/sugestao", response_class=JSONResponse)
async def api_salvar_sugestao(id_tramite: str, request: Request):
    body = await request.json()
    return JSONResponse(retornos_service.salvar_sugestao(id_tramite, body.get("texto", "")))
