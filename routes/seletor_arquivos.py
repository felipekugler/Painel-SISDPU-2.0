"""Seletor de arquivos nativo aberto na pasta do PAJ (ver services/seletor_arquivos_service.py).

- POST /api/seletor-arquivos/escolher/{paj_norm} — abre a janela "Abrir" do Windows
- GET  /api/seletor-arquivos/arquivo/{token}      — conteúdo de um arquivo escolhido
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from services import seletor_arquivos_service

router = APIRouter()


@router.post("/api/seletor-arquivos/escolher/{paj_norm}")
async def seletor_escolher(paj_norm: str, titulo: str = ""):
    return JSONResponse(await seletor_arquivos_service.escolher(paj_norm, titulo))


@router.get("/api/seletor-arquivos/arquivo/{token}")
async def seletor_arquivo(token: str):
    p = seletor_arquivos_service.caminho(token)
    if not p:
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)
    return FileResponse(str(p), filename=p.name)
