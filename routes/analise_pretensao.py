"""Rotas: análise de PAJ por pretensão (diagnóstico via skill dedicada, ex.:
BPC/LOAS) e organização de documentos para ajuizamento."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import analise_pretensao_service, organizar_ajuizamento_service
from services.paj_service import PajNorm

router = APIRouter()


@router.post("/api/analise-pretensao/{tipo}/start/{paj_norm}")
async def analise_pretensao_start(tipo: str, paj_norm: PajNorm):
    result = analise_pretensao_service.iniciar(paj_norm, tipo)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/analise-pretensao/status/{paj_norm}")
async def analise_pretensao_status(paj_norm: PajNorm):
    return analise_pretensao_service.get_status(paj_norm)


@router.get("/api/analise-pretensao/status-todos")
async def analise_pretensao_status_todos():
    """Status (em memoria) de todas as analises por pretensao em andamento —
    consumido pelo dashboard (polling) pra exibir o spinner na coluna
    'Análise IA' enquanto o diagnostico roda em background."""
    return analise_pretensao_service.get_status_todos()


@router.post("/api/analise-pretensao/reenviar/{paj_norm}")
async def analise_pretensao_reenviar(paj_norm: PajNorm, payload: dict = Body(...)):
    mensagem = (payload.get("mensagem") or "").strip()
    result = analise_pretensao_service.reenviar(paj_norm, mensagem)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/organizar-ajuizamento/{paj_norm}")
async def organizar_ajuizamento_start(paj_norm: PajNorm, payload: dict = Body(default={})):
    forcar = bool(payload.get("forcar"))
    result = organizar_ajuizamento_service.iniciar(paj_norm, forcar=forcar)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/organizar-ajuizamento/status/{paj_norm}")
async def organizar_ajuizamento_status(paj_norm: PajNorm):
    return organizar_ajuizamento_service.get_status(paj_norm)
