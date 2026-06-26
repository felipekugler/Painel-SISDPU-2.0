"""Rotas de análise IA de PAJ (start + polling de status)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.analise_ia_service import get_status, iniciar_analise
from services.paj_service import PajNorm

router = APIRouter()


@router.post("/api/analisar-paj/{paj_norm}")
async def analisar_paj_start(paj_norm: PajNorm):
    """Inicia análise IA do PAJ em background."""
    result = iniciar_analise(paj_norm)
    return JSONResponse(result)


@router.get("/api/analisar-paj/status/{paj_norm}")
async def analisar_paj_status(paj_norm: PajNorm):
    """Retorna status da análise + conteúdo (quando concluído)."""
    return get_status(paj_norm)
