"""Caixa "Consultar e-Sisrec" do cabeçalho (ver ingestao.esisrec_client):
abre o processo de recurso administrativo numa aba da janela do e-Sisrec."""

import contextlib

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ingestao import esisrec_client

router = APIRouter()


@router.get("/api/esisrec/abrir")
async def esisrec_abrir(numero: str):
    usuario, senha = "", ""
    with contextlib.suppress(Exception):
        from services import cofre_service
        creds = cofre_service.get_credenciais("sat")
        if creds:
            usuario, senha = creds
    return JSONResponse(await esisrec_client.abrir_processo(numero, usuario, senha))
