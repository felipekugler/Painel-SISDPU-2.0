"""Rotas do botão "Dossiê Seguro-Desemprego Web" (ver services.dossie_sd_service).

One-click: o único parâmetro é o tipo (completo | vinculos | requerimentos)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import dossie_sd_service
from services.paj_service import PajNorm

router = APIRouter()


@router.post("/api/dossie-sd/iniciar/{paj_norm}/{tipo}")
async def dossie_sd_iniciar(paj_norm: PajNorm, tipo: str):
    result = dossie_sd_service.iniciar(paj_norm, tipo)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/dossie-sd/status/{paj_norm}")
async def dossie_sd_status(paj_norm: PajNorm):
    return dossie_sd_service.get_status(paj_norm)


@router.post("/api/dossie-sd/juntar/{paj_norm}")
async def dossie_sd_juntar(paj_norm: PajNorm):
    """Junta ao SISDPU o(s) PDF(s) do dossiê recém-gerado. Rota async de
    propósito: a task roda no loop principal, o mesmo da sessão do SISDPU."""
    result = dossie_sd_service.juntar(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/dossie-sd/dispensar-juntada/{paj_norm}")
async def dossie_sd_dispensar_juntada(paj_norm: PajNorm):
    return dossie_sd_service.dispensar_juntada(paj_norm)
