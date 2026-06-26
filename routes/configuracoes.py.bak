"""Rota de Configurações do Painel — toggles de comportamento da sincronização."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import config_painel_service

router = APIRouter()


@router.get("/configuracoes", response_class=HTMLResponse)
async def pagina_configuracoes(request: Request):
    config = config_painel_service.ler_config()
    tmpl = request.app.state.jinja.get_template("configuracoes.html")
    return HTMLResponse(tmpl.render(request=request, config=config))


@router.post("/api/configuracoes")
async def salvar_configuracoes(request: Request):
    body = await request.json()
    salvo = config_painel_service.salvar_config(body)
    limpeza: dict = {}
    if body.get("limpar_prazos"):
        limpeza["prazos"] = config_painel_service.limpar_dados_prazos()
    if body.get("limpar_narrativa"):
        limpeza["narrativa"] = config_painel_service.limpar_dados_narrativa()
    return JSONResponse({"ok": True, "config": salvo, "limpeza": limpeza})
