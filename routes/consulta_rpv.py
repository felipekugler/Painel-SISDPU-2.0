"""Rotas do botão "Consulta RPV/Precatório" (ver services.consulta_rpv_service).

Sem corpo de requisição — a rotina é ONE-CLICK, todos os dados vêm do PAJ e do
processo judicial vinculado (pedido explícito do usuário, 2026-09-22)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services import consulta_rpv_service
from services.paj_service import PajNorm

router = APIRouter()


@router.post("/api/consulta-rpv/iniciar/{paj_norm}")
async def consulta_rpv_iniciar(paj_norm: PajNorm):
    result = consulta_rpv_service.iniciar(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/consulta-rpv/status/{paj_norm}")
async def consulta_rpv_status(paj_norm: PajNorm):
    return consulta_rpv_service.get_status(paj_norm)
