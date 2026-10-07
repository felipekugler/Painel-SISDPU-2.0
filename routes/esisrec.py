"""Consulta de recurso administrativo no e-Sisrec (INSS/CRPS), disparada pela
caixa "e-Sisrec" do cabeçalho (ver ingestao.esisrec_client).

- GET /esisrec/consultar?numero=   → página do painel com os dados do processo
- GET /api/esisrec/consultar       → leitura headless (dados, partes, eventos)
- GET /api/esisrec/documento       → PDF de um documento do histórico (inline)
- GET /api/esisrec/extrato         → extrato do processo ("Gerar Relatório Andamento")
- GET /api/esisrec/abrir           → abre o processo na janela VISÍVEL do e-Sisrec
"""

import contextlib
import html
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ingestao import esisrec_client

router = APIRouter()


def _credenciais_sat() -> tuple[str, str]:
    with contextlib.suppress(Exception):
        from services import cofre_service
        creds = cofre_service.get_credenciais("sat")
        if creds:
            return creds
    return "", ""


@router.get("/esisrec/consultar", response_class=HTMLResponse)
async def pagina_esisrec_consultar(request: Request):
    template = request.app.state.jinja.get_template("esisrec_consultar.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    return HTMLResponse(template.render(request=request, ambiente=ambiente))


@router.get("/api/esisrec/consultar")
async def esisrec_consultar(numero: str):
    usuario, senha = _credenciais_sat()
    return JSONResponse(await esisrec_client.consultar(numero, usuario, senha))


@router.get("/api/esisrec/documento")
async def esisrec_documento(numero: str, evento: str, idx: int = 0, pagina: str = "1"):
    usuario, senha = _credenciais_sat()
    try:
        corpo, nome, ctype = await esisrec_client.baixar_documento(numero, evento, idx, pagina, usuario, senha)
    except esisrec_client.EsisrecErro as e:
        return HTMLResponse(f"<p style='font-family:sans-serif'>{html.escape(str(e))}</p>", status_code=502)
    return Response(corpo, media_type=ctype,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(nome)}"})


@router.get("/api/esisrec/extrato")
async def esisrec_extrato(numero: str):
    """Extrato do processo no CRPS ("Gerar Relatório Andamento" do e-Sisrec)."""
    usuario, senha = _credenciais_sat()
    try:
        corpo, nome, ctype = await esisrec_client.baixar_extrato(numero, usuario, senha)
    except esisrec_client.EsisrecErro as e:
        return HTMLResponse(f"<p style='font-family:sans-serif'>{html.escape(str(e))}</p>", status_code=502)
    return Response(corpo, media_type=ctype,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(nome)}"})


@router.get("/api/esisrec/abrir")
async def esisrec_abrir(numero: str):
    usuario, senha = _credenciais_sat()
    return JSONResponse(await esisrec_client.abrir_processo(numero, usuario, senha))
