"""Página "Consultar processo" (menu E-proc).

Busca um processo pelo número dos autos (NPU) e mostra a íntegra estruturada
(cabeçalho + timeline de eventos + intimações) trazida pelo `eproc-trf4` via
`ingestao.eproc_mcp_client` — sem navegador, sem depender da janela do e-proc
(`ingestao.eproc_client`, usada em `routes/prazos.py`) estar aberta.

A busca de fato roda no browser do usuário contra `/api/eproc-mcp/triagem`
(ver `routes/eproc_mcp.py`); esta rota só serve a página.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/eproc/consultar", response_class=HTMLResponse)
async def pagina_eproc_consultar(request: Request):
    template = request.app.state.jinja.get_template("eproc_consultar.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)
