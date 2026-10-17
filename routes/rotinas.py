"""Configurações › Rotinas automáticas (16/10/2026) — ver
services/rotina_dossie_sd_service.py."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services import rotina_dossie_sd_service as rotina

router = APIRouter()


@router.get("/api/rotinas/dossie-sd/estado", response_class=JSONResponse)
async def api_rotina_dossie_sd_estado():
    return {"ok": True, "estado": rotina.estado(), "candidatos": rotina.candidatos(),
            "pretensoes_conhecidas": rotina.pretensoes_conhecidas()}


@router.post("/api/rotinas/dossie-sd/config", response_class=JSONResponse)
async def api_rotina_dossie_sd_config(request: Request):
    body = await request.json()
    return {"ok": True, "config": rotina.set_config(body or {})}


@router.get("/api/rotinas/dossie-sd/historico", response_class=JSONResponse)
async def api_rotina_dossie_sd_historico(limite: int = 200):
    return {"ok": True, "historico": rotina.historico(limite)}


@router.post("/api/rotinas/dossie-sd/executar", response_class=JSONResponse)
async def api_rotina_dossie_sd_executar():
    if not rotina.get_config()["ativa"]:
        return {"ok": False, "erro": "Ative a rotina antes de executar."}
    rotina.disparar(forcar=True)  # pedido explícito: roda mesmo em fim de semana
    return {"ok": True}


@router.post("/api/rotinas/dossie-sd/reprocessar/{paj_norm}", response_class=JSONResponse)
async def api_rotina_dossie_sd_reprocessar(paj_norm: str):
    return rotina.reprocessar(paj_norm)
