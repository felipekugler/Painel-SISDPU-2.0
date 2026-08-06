"""Rotas: cálculo do valor da causa (sub-passo da geração da petição inicial).

O cálculo roda automaticamente no fim do fluxo de PI (analise_pretensao_service),
mas estes endpoints permitem disparar/reprocessar manualmente e ler o último
resultado gravado (`valor_causa_result.json`).
"""

import json

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from config import PAJS_DIR
from services import valor_causa_service
from services.paj_service import PajNorm

router = APIRouter()


# Endpoint síncrono (def) — FastAPI roda em threadpool; a geração via Word
# (win32com) é bloqueante e curta, então não trava o event loop.
@router.post("/api/valor-causa/{paj_norm}")
def valor_causa_processar(paj_norm: PajNorm, payload: dict = Body(default={})):
    """Dispara o cálculo. Sem corpo: lê os parâmetros do `valor_causa.json`
    emitido pela skill. Com `params` no corpo: usa-os diretamente (override/teste)."""
    params = payload.get("params")
    result = valor_causa_service.processar(paj_norm, params=params)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/valor-causa/status/{paj_norm}")
def valor_causa_status(paj_norm: PajNorm):
    """Retorna o último resultado gravado, procurando em 'Peças IA/' e na raiz."""
    paj_dir = PAJS_DIR / paj_norm
    for c in (paj_dir / "Peças IA" / "valor_causa_result.json", paj_dir / "valor_causa_result.json"):
        if c.exists():
            try:
                return json.loads(c.read_text(encoding="utf-8"))
            except Exception:
                break
    return {"ok": False, "status": "idle"}
