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


# --- Preferências de tramitação (usadas pela aba "Arquivos SAT") ---
@router.get("/api/config/tramitacao")
async def get_pref_tramitacao():
    return JSONResponse({
        "prazo": config_painel_service.get_prazo_tramitacao_padrao(),
        "favoritos": config_painel_service.get_grupos_favoritos(),
        "max_favoritos": config_painel_service.GRUPOS_FAVORITOS_MAX,
    })


@router.post("/api/config/prazo-tramitacao")
async def set_prazo_tramitacao(request: Request):
    body = await request.json()
    v = config_painel_service.set_prazo_tramitacao_padrao(body.get("dias", 15))
    return JSONResponse({"ok": True, "prazo": v})


@router.post("/api/config/grupos-favoritos")
async def add_grupo_favorito(request: Request):
    body = await request.json()
    favs = config_painel_service.add_grupo_favorito(body.get("grupo", ""))
    return JSONResponse({"ok": True, "favoritos": favs})


@router.post("/api/config/grupos-favoritos/del")
async def del_grupo_favorito(request: Request):
    body = await request.json()
    favs = config_painel_service.del_grupo_favorito(body.get("grupo", ""))
    return JSONResponse({"ok": True, "favoritos": favs})
