"""Rotas do botão "Calcular Valor da Causa" (ver
services.calcular_valor_causa_service)."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import calcular_valor_causa_service
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/calcular-valor-causa/peticoes/{paj_norm}")
async def calcular_valor_causa_peticoes(paj_norm: PajNorm):
    """Lista os arquivos de petição inicial encontrados em "Peças IA/" (nome
    ou conteúdo sugerindo petição inicial) para o modal de seleção — pode
    haver mais de um arquivo na pasta (ex.: a inicial e um recurso)."""
    return {"peticoes": calcular_valor_causa_service.listar_peticoes(paj_norm)}


@router.post("/api/calcular-valor-causa/iniciar/{paj_norm}")
async def calcular_valor_causa_iniciar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: {"arquivo": "Peças IA/01 - PI.txt"} — petição escolhida pelo
    Defensor no modal. Dispara só a extração (leitura) dos parâmetros; o
    cálculo em si só roda depois de /confirmar."""
    arquivo = (payload.get("arquivo") or "").strip()
    result = calcular_valor_causa_service.iniciar(paj_norm, arquivo)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/calcular-valor-causa/status/{paj_norm}")
async def calcular_valor_causa_status(paj_norm: PajNorm):
    return calcular_valor_causa_service.get_status(paj_norm)


@router.post("/api/calcular-valor-causa/cancelar/{paj_norm}")
async def calcular_valor_causa_cancelar(paj_norm: PajNorm):
    """Botão "Cancelar" do modal "Confira os parâmetros"."""
    result = calcular_valor_causa_service.cancelar(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.post("/api/calcular-valor-causa/fechar/{paj_norm}")
async def calcular_valor_causa_fechar(paj_norm: PajNorm):
    """Botão "Fechar" do modal de resultado — só reconhece a revisão."""
    result = calcular_valor_causa_service.fechar_revisao(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# Rota síncrona (def, não async def) — valor_causa_service.processar() é
# bloqueante no Word (win32com); o FastAPI roda em threadpool, igual
# routes/valor_causa.py.
@router.post("/api/calcular-valor-causa/confirmar/{paj_norm}")
def calcular_valor_causa_confirmar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: o objeto de parâmetros completo (extraído pela IA + editado pelo
    Defensor no modal "Confira os parâmetros"). Só dispara o cálculo
    determinístico se o mínimo exigido estiver presente — ver
    calcular_valor_causa_service._validar_minimo."""
    result = calcular_valor_causa_service.confirmar(paj_norm, payload or {})
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


# Também síncrona — mesmo motivo (Word/win32com).
@router.post("/api/calcular-valor-causa/inserir/{paj_norm}")
def calcular_valor_causa_inserir(paj_norm: PajNorm):
    """Botão "Inserir Valor da Causa na Inicial" do modal de resultado — grava
    uma CÓPIA da petição com o valor em negrito; nunca sobrescreve a
    original."""
    result = calcular_valor_causa_service.inserir(paj_norm)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)
