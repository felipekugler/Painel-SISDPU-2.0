"""Conexão do painel com o MCP do SIS-DPU 2.0 (OAuth) — ver ingestao/sisdpu2_mcp_client.py."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from ingestao import sisdpu2_mcp_client as sis2

router = APIRouter()


@router.get("/api/sisdpu2/status", response_class=JSONResponse)
async def api_sisdpu2_status():
    """Estado da conexão. Com token, confirma no servidor de quem é a sessão."""
    if not sis2.conectado():
        return {"conectado": False}
    try:
        quem = await run_in_threadpool(sis2.chamar, "auth_situacao")
    except sis2.Sisdpu2LoginNecessario:
        return {"conectado": False}
    except Exception as e:  # noqa: BLE001
        return {"conectado": True, "erro": str(e), "minutos": sis2.minutos_restantes()}
    return {"conectado": True, "sessao": quem, "minutos": sis2.minutos_restantes()}


@router.get("/api/sisdpu2/fonte", response_class=JSONResponse)
async def api_sisdpu2_fonte():
    from services import config_painel_service
    return {"fonte": config_painel_service.get_fonte_dados()}


@router.post("/api/sisdpu2/fonte", response_class=JSONResponse)
async def api_sisdpu2_fonte_salvar(request: Request):
    from services import config_painel_service
    body = await request.json()
    try:
        return {"ok": True, "fonte": config_painel_service.set_fonte_dados(str(body.get("fonte") or ""))}
    except ValueError as e:
        return {"ok": False, "erro": str(e)}


@router.get("/api/sisdpu2/login", response_class=JSONResponse)
async def api_sisdpu2_login():
    try:
        return {"ok": True, "url": await run_in_threadpool(sis2.iniciar_login)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Não foi possível iniciar o login no SIS-DPU 2.0: {e}"}


@router.post("/api/sisdpu2/desconectar", response_class=JSONResponse)
async def api_sisdpu2_desconectar():
    await run_in_threadpool(sis2.desconectar)
    return {"ok": True}


_PAGINA_RETORNO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Painel SISDPU 2.0 — SIS-DPU 2.0</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#f5f5f5}}div{{background:#fff;padding:32px 40px;border-radius:12px;
box-shadow:0 2px 12px #0002;text-align:center;max-width:460px}}h1{{font-size:20px;margin:0 0 8px}}</style>
</head><body><div><h1>{titulo}</h1><p>{texto}</p></div>
<script>try{{window.opener&&window.opener.postMessage('sisdpu2-login-{evento}','*')}}catch(e){{}}
setTimeout(function(){{window.close()}},{fechar});</script></body></html>"""


@router.get("/api/sisdpu2/oauth/callback", response_class=HTMLResponse)
async def api_sisdpu2_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SIS-DPU 2.0 não concluído",
            texto=f"Motivo: {error or 'sem código de autorização'}.", evento="erro", fechar=15000))
    try:
        await run_in_threadpool(sis2.concluir_login, code, state)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SIS-DPU 2.0 não concluído", texto=str(e), evento="erro", fechar=15000))
    return HTMLResponse(_PAGINA_RETORNO.format(
        titulo="✅ Painel conectado ao SIS-DPU 2.0", texto="Pode fechar esta aba e voltar ao painel.",
        evento="ok", fechar=2500))
