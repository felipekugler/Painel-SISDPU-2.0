"""Janela do SEI controlada pelo painel — "Abrir no SEI" / "Abrir para
assinar" dos ofícios (ver ingestao.sei_web_client)."""

import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ingestao import sei_web_client

router = APIRouter()


def _credenciais_sisdpu() -> tuple[str, str]:
    """O SEI usa o mesmo login de rede da DPU que o SISDPU (decisão do
    Defensor, 22/10/2026): credenciais "sisdpu" do cofre, se destrancado."""
    with contextlib.suppress(Exception):
        from services import cofre_service
        creds = cofre_service.get_credenciais("sisdpu")
        if creds:
            return creds
    return "", ""


@router.post("/api/sei/abrir-documento", response_class=JSONResponse)
async def api_sei_abrir_documento(request: Request):
    """Corpo: {url}. Abre o documento na janela do SEI, entrando sozinho se
    preciso."""
    b = await request.json()
    usuario, senha = _credenciais_sisdpu()
    return JSONResponse(await sei_web_client.abrir_documento(b.get("url") or "", usuario, senha))
