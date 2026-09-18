"""Rotas do botão "Calcular Tempo de Contribuição" (ver
services.calcular_cnis_service)."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import calcular_cnis_service
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/calcular-cnis/arquivos/{paj_norm}")
async def calcular_cnis_arquivos(paj_norm: PajNorm):
    """Lista os arquivos de CNIS encontrados em Arquivos/ (nome contém
    'cnis') para o modal de seleção — pode haver mais de um, extraídos em
    datas diferentes."""
    return {"arquivos": calcular_cnis_service.listar_arquivos_cnis(paj_norm)}


@router.post("/api/calcular-cnis/iniciar/{paj_norm}")
async def calcular_cnis_iniciar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: {"arquivo": "Arquivos/CNIS.pdf", "modo": "simples"|"completo"}
    — caminho relativo e modo escolhidos pelo Defensor no modal (modo só é
    perguntado quando o arquivo detectado é um CNIS completo)."""
    arquivo = (payload.get("arquivo") or "").strip()
    modo = (payload.get("modo") or "simples").strip()
    result = calcular_cnis_service.iniciar(paj_norm, arquivo, modo)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/calcular-cnis/status/{paj_norm}")
async def calcular_cnis_status(paj_norm: PajNorm):
    return calcular_cnis_service.get_status(paj_norm)
