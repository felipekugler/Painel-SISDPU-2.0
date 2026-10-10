"""Rotas do item "Dossiê CadÚnico" (ver services.dossie_cadunico_service).

One-click por tipo (completo | folha | historico): CPF e nome vêm do PAJ; o login no CadÚnico é manual, na janela
que o painel abre."""

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from services import dossie_cadunico_service
from services.paj_service import PajNorm

router = APIRouter()


@router.post("/api/dossie-cadunico/iniciar/{paj_norm}/{tipo}")
async def dossie_cadunico_iniciar(paj_norm: PajNorm, tipo: str):
    """tipo: completo | folha | historico."""
    result = dossie_cadunico_service.iniciar(paj_norm, tipo)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/dossie-cadunico/status/{paj_norm}")
async def dossie_cadunico_status(paj_norm: PajNorm):
    return dossie_cadunico_service.get_status(paj_norm)


@router.post("/api/dossie-cadunico/juntar/{paj_norm}")
async def dossie_cadunico_juntar(paj_norm: PajNorm):
    """Rota async de propósito: a task roda no loop principal, o mesmo da
    sessão do SISDPU."""
    result = dossie_cadunico_service.juntar(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/dossie-cadunico/dispensar-juntada/{paj_norm}")
async def dossie_cadunico_dispensar_juntada(paj_norm: PajNorm):
    return dossie_cadunico_service.dispensar_juntada(paj_norm)


@router.get("/api/dossie-cadunico/arquivo/{paj_norm}")
async def dossie_cadunico_arquivo(paj_norm: PajNorm, nome: str):
    """Serve INLINE (nova aba) um PDF de `CadÚnico/` — botão "Visualizar" do
    modal de juntada."""
    p = dossie_cadunico_service.caminho_arquivo(paj_norm, nome)
    if not p:
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)
    return FileResponse(str(p), media_type="application/pdf", filename=p.name,
                        content_disposition_type="inline")
