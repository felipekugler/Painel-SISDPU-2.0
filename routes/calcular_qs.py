"""Rotas do botão "Calcular Qualidade de Segurado" (ver
services.calcular_qs_service).

Mesmo padrão arquitetural de routes/calcular_cnis.py (skill própria, sessão
real do Claude Code CLI, modal de seleção de arquivo) — a diferença é que
aqui a skill (`AnáliseQS`, generalizada em 2026-09-21 para aceitar qualquer
"data-alvo"/fato gerador) só precisa de 4 fatos simples do CNIS (última
competência, tipo de filiação, total de contribuições, desemprego), não do
cálculo completo de tempo de contribuição/carência — por isso é mais rápida
e não usa a mesma sessão do botão "Calcular Tempo de Contribuição"."""

import json

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import calcular_qs_service
from services.calcular_cnis_service import listar_arquivos_cnis
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/calcular-qs/arquivos/{paj_norm}")
async def calcular_qs_arquivos(paj_norm: PajNorm):
    """Reaproveita o mesmo detector de arquivos CNIS do botão "Calcular Tempo
    de Contribuição" (services.calcular_cnis_service.listar_arquivos_cnis) —
    mesmo critério de arquivo nativo/escaneado, mesma ordenação por data de
    emissão."""
    return {"arquivos": listar_arquivos_cnis(paj_norm)}


@router.post("/api/calcular-qs/iniciar/{paj_norm}")
async def calcular_qs_iniciar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: {"arquivo": "Arquivos/CNIS.pdf", "tipo_beneficio": "pensao_morte",
    "data_alvo": "DD/MM/AAAA", "data_alvo_fonte": "Certidão de óbito",
    "idade_instituidor": "", "orientacao": ""} — tudo informado objetivamente
    pelo Defensor no modal (mesmo princípio de calcular_cnis.py: tipo de
    benefício e data-alvo nunca são inferidos/pesquisados pela IA, só o que
    vem do próprio CNIS). `orientacao` (opcional): mesmo texto da caixa
    "Orientação diretiva para análise da IA" do botão "Analisar" (ver
    paj_detail.html) — integrada aqui a pedido do usuário, 2026-09-21."""
    arquivo = (payload.get("arquivo") or "").strip()
    tipo_beneficio = (payload.get("tipo_beneficio") or "").strip()
    data_alvo_rotulo = (payload.get("data_alvo_rotulo") or "").strip()
    data_alvo = (payload.get("data_alvo") or "").strip()
    data_alvo_fonte = (payload.get("data_alvo_fonte") or "").strip()
    idade_instituidor = (payload.get("idade_instituidor") or "").strip()
    orientacao = (payload.get("orientacao") or "").strip()
    result = calcular_qs_service.iniciar(
        paj_norm,
        arquivo,
        tipo_beneficio,
        data_alvo_rotulo,
        data_alvo,
        data_alvo_fonte,
        idade_instituidor,
        orientacao,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/calcular-qs/status/{paj_norm}")
async def calcular_qs_status(paj_norm: PajNorm):
    return calcular_qs_service.get_status(paj_norm)
