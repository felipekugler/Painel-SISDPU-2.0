"""Rotas do botão "Dossiê Seguro-Desemprego Web" (ver services.dossie_sd_service).

One-click: o único parâmetro é o tipo (completo | vinculos | requerimentos)."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from config import PAJS_DIR
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


@router.get("/api/dossie-sd/arquivo/{paj_norm}")
async def dossie_sd_arquivo(paj_norm: PajNorm, nome: str):
    """Serve INLINE (nova aba) um PDF de `Seguro Desemprego Web/` — botão
    "Visualizar" do modal de juntada. `nome` = basename (anti path-traversal)."""
    base = Path(nome).name
    p = PAJS_DIR / paj_norm / dossie_sd_service.SUBPASTA_SD / base
    if not (base.lower().endswith(".pdf") and p.is_file()):
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)
    return FileResponse(str(p), media_type="application/pdf", filename=base,
                        content_disposition_type="inline")
