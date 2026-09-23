"""Rotas do "Histórico" — conclusões de caixa de entrada efetivadas dentro do
painel (retornos, SAT, etc.) e análises/petições disparadas por IA."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import historico, historico_painel

router = APIRouter()


@router.get("/historico", response_class=HTMLResponse)
async def pagina_historico(request: Request):
    template = request.app.state.jinja.get_template("historico.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/historico/lista", response_class=JSONResponse)
async def api_lista():
    return JSONResponse({"registros": historico_painel.listar()})


@router.get("/api/historico/analises", response_class=JSONResponse)
async def api_analises():
    """Aba "Análises (IA)" — ver `services.historico.listar_analises`."""
    return JSONResponse({"registros": historico.listar_analises()})
