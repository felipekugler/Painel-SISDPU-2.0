"""Rotas do Relatório de Ações Judiciais Anteriores (Ações com IA › Consultas).
Ver services/acoes_anteriores_service.py."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services import acoes_anteriores_service as svc

router = APIRouter()


@router.post("/api/paj/{paj_norm}/acoes-anteriores/consultar", response_class=JSONResponse)
async def api_consultar(paj_norm: str):
    """Busca no e-proc (MCP) pelo CPF do assistido e devolve a PRÉVIA do relatório."""
    try:
        r = await svc.consultar(paj_norm)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/paj/{paj_norm}/acoes-anteriores/movimentar", response_class=JSONResponse)
async def api_movimentar(paj_norm: str, request: Request):
    """Juntada de documento no SISDPU com o relatório em HTML. Body: {html}.
    Aguarda a aprovação da ficha no modal padrão do SIS-DPU 2.0."""
    body = await request.json()
    try:
        r = await svc.movimentar(paj_norm, str(body.get("html") or ""))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "detalhe": f"{type(e).__name__}: {e}"}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)
