"""Rotas do "Minutar Ofício" (SEI) — ver services/oficio_sei_service.py e
ingestao/sei_mcp_client.py."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from ingestao import sei_mcp_client as sei
from services import oficio_sei_service as svc

router = APIRouter()


@router.get("/api/oficio-sei/{paj_norm}/status", response_class=JSONResponse)
async def api_oficio_status(paj_norm: str):
    return svc.status(paj_norm)


@router.post("/api/oficio-sei/{paj_norm}/redigir", response_class=JSONResponse)
async def api_oficio_redigir(paj_norm: str, request: Request):
    body = await request.json()
    try:
        return svc.iniciar_redacao(paj_norm, body.get("orientacao", ""))
    except FileNotFoundError as e:
        return {"ok": False, "erro": str(e)}


@router.post("/api/oficio-sei/{paj_norm}/rascunho", response_class=JSONResponse)
async def api_oficio_rascunho(paj_norm: str, request: Request):
    body = await request.json()
    try:
        return svc.salvar_rascunho(paj_norm, body.get("rascunho") or {})
    except FileNotFoundError as e:
        return {"ok": False, "erro": str(e)}


@router.post("/api/oficio-sei/{paj_norm}/descartar", response_class=JSONResponse)
async def api_oficio_descartar(paj_norm: str):
    return svc.descartar_rascunho(paj_norm)


@router.post("/api/oficio-sei/{paj_norm}/criar", response_class=JSONResponse)
async def api_oficio_criar(paj_norm: str, request: Request):
    body = await request.json()
    if not body.get("confirmado"):
        return {"ok": False, "erro": "Criação no SEI exige confirmação explícita."}
    try:
        return await run_in_threadpool(svc.criar_no_sei, paj_norm, body.get("rascunho") or {})
    except FileNotFoundError as e:
        return {"ok": False, "erro": str(e)}


# --- Conexão do painel com o SEI (OAuth do MCP-SEI) ---

@router.get("/api/sei/status", response_class=JSONResponse)
async def api_sei_status():
    return {"conectado": sei.conectado()}


@router.get("/api/sei/login", response_class=JSONResponse)
async def api_sei_login():
    try:
        return {"ok": True, "url": await run_in_threadpool(sei.iniciar_login)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Não foi possível iniciar o login no SEI: {e}"}


@router.post("/api/sei/desconectar", response_class=JSONResponse)
async def api_sei_desconectar():
    await run_in_threadpool(sei.desconectar)
    return {"ok": True}


_PAGINA_RETORNO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Painel SISDPU — SEI</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#f5f5f5}}div{{background:#fff;padding:32px 40px;border-radius:12px;
box-shadow:0 2px 12px #0002;text-align:center;max-width:460px}}h1{{font-size:20px;margin:0 0 8px}}</style>
</head><body><div><h1>{titulo}</h1><p>{texto}</p></div>
<script>try{{window.opener&&window.opener.postMessage('sei-login-{evento}','*')}}catch(e){{}}
setTimeout(function(){{window.close()}},{fechar});</script></body></html>"""


@router.get("/api/sei/oauth/callback", response_class=HTMLResponse)
async def api_sei_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SEI não concluído", texto=f"Motivo: {error or 'sem código de autorização'}.",
            evento="erro", fechar=15000))
    try:
        await run_in_threadpool(sei.concluir_login, code, state)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SEI não concluído", texto=str(e), evento="erro", fechar=15000))
    return HTMLResponse(_PAGINA_RETORNO.format(
        titulo="✅ Painel conectado ao SEI", texto="Pode fechar esta aba e voltar ao painel.",
        evento="ok", fechar=2500))
