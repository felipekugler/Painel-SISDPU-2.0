"""Rota de Configurações do Painel — toggles de comportamento da sincronização."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import config_painel_service
from services import credenciais_eproc_service as cred_eproc

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


# --- Valores padrão da aba "Arquivos SAT Central" ---
@router.get("/api/config/sat-central")
async def get_sat_central():
    return JSONResponse(config_painel_service.get_sat_central_defaults())


@router.post("/api/config/sat-central")
async def set_sat_central(request: Request):
    body = await request.json()
    defaults = config_painel_service.set_sat_central_defaults(body)
    return JSONResponse({"ok": True, "defaults": defaults})


# --- Acesso ao e-proc: cofre de credenciais cifrado por senha-mestra ---------
# Usado pela aba "Prazos e-proc" (login automático). As credenciais ficam
# cifradas em disco; a senha-mestra não é gravada. Escopado a esta feature —
# não tranca o resto do painel.
@router.get("/api/config/eproc/status", response_class=JSONResponse)
async def eproc_cred_status():
    return {"ok": True,
            "configurado": cred_eproc.configurado(),
            "destrancado": cred_eproc.destrancado(),
            "usuario": cred_eproc.get_usuario()}


@router.post("/api/config/eproc/credenciais", response_class=JSONResponse)
async def eproc_cred_salvar(payload: dict = Body(...)):
    """Define (primeira vez) ou troca as credenciais do e-proc + senha-mestra.
    Sobrescreve o cofre e já deixa a sessão destrancada."""
    p = payload or {}
    usuario = (p.get("usuario") or "").strip()
    senha = p.get("senha") or ""
    senha_mestra = p.get("senha_mestra") or ""
    confirma = p.get("senha_mestra_confirma") or ""
    if not usuario or not senha or not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Preencha usuário, senha do e-proc e senha-mestra."},
                            status_code=400)
    if len(senha_mestra) < 6:
        return JSONResponse({"ok": False, "erro": "A senha-mestra deve ter ao menos 6 caracteres."},
                            status_code=400)
    if senha_mestra != confirma:
        return JSONResponse({"ok": False, "erro": "A confirmação da senha-mestra não confere."},
                            status_code=400)
    try:
        cred_eproc.salvar(usuario, senha, senha_mestra)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return {"ok": True}


@router.post("/api/config/eproc/desbloquear", response_class=JSONResponse)
async def eproc_cred_desbloquear(payload: dict = Body(...)):
    senha_mestra = (payload or {}).get("senha_mestra", "")
    if not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Informe a senha-mestra."}, status_code=400)
    if cred_eproc.destrancar(senha_mestra):
        return {"ok": True, "usuario": cred_eproc.get_usuario()}
    return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)


@router.post("/api/config/eproc/bloquear", response_class=JSONResponse)
async def eproc_cred_bloquear():
    """Esquece as credenciais da memória (tranca de novo)."""
    cred_eproc.bloquear()
    return {"ok": True}


@router.delete("/api/config/eproc/credenciais", response_class=JSONResponse)
async def eproc_cred_remover():
    """Bloqueia e APAGA o cofre em disco (volta a exigir configuração)."""
    cred_eproc.remover()
    return {"ok": True}
