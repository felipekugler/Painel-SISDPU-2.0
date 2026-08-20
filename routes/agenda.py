"""Rota da página "Agenda" — calendário e tabela unificando audiências
(SISDPU, já capturadas por PAJ), prazos judiciais finais (e-proc, já
sincronizados em "/prazos") e compromissos/afastamentos cadastrados
manualmente. Ver `services/agenda_service.py` para o agregador.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from services import agenda_service

router = APIRouter()


@router.get("/agenda", response_class=HTMLResponse)
async def pagina_agenda(request: Request):
    template = request.app.state.jinja.get_template("agenda.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    return HTMLResponse(template.render(request=request, ambiente=ambiente))


@router.get("/api/agenda/eventos", response_class=JSONResponse)
async def api_listar_eventos(inicio: str | None = None, fim: str | None = None):
    return {"eventos": agenda_service.listar_eventos(inicio, fim)}


class CompromissoIn(BaseModel):
    titulo: str = Field(..., min_length=1, max_length=200)
    descricao: str = Field("", max_length=2000)
    data_inicio: str = Field(..., min_length=8, max_length=10)
    hora_inicio: str | None = Field(None, max_length=5)
    paj_norm: str = Field("", max_length=20)
    processo: str = Field("", max_length=30)
    status: str = Field("agendado", max_length=30)


class CompromissoUpdateIn(BaseModel):
    titulo: str | None = Field(None, max_length=200)
    descricao: str | None = Field(None, max_length=2000)
    data_inicio: str | None = Field(None, max_length=10)
    hora_inicio: str | None = Field(None, max_length=5)
    paj_norm: str | None = Field(None, max_length=20)
    processo: str | None = Field(None, max_length=30)
    status: str | None = Field(None, max_length=30)


@router.post("/api/agenda/compromissos", response_class=JSONResponse)
async def api_criar_compromisso(payload: CompromissoIn):
    item = agenda_service.criar_compromisso(**payload.model_dump())
    return {"ok": True, "item": item}


@router.put("/api/agenda/compromissos/{compromisso_id}", response_class=JSONResponse)
async def api_atualizar_compromisso(compromisso_id: str, payload: CompromissoUpdateIn):
    item = agenda_service.atualizar_compromisso(compromisso_id, **payload.model_dump())
    if item is None:
        raise HTTPException(status_code=404, detail="compromisso não encontrado")
    return {"ok": True, "item": item}


@router.delete("/api/agenda/compromissos/{compromisso_id}", response_class=JSONResponse)
async def api_remover_compromisso(compromisso_id: str):
    if not agenda_service.remover_compromisso(compromisso_id):
        raise HTTPException(status_code=404, detail="compromisso não encontrado")
    return {"ok": True}


class AfastamentoIn(BaseModel):
    tipo_afastamento: str = Field(..., max_length=30)
    data_inicio: str = Field(..., min_length=8, max_length=10)
    data_fim: str = Field(..., min_length=8, max_length=10)
    titulo: str = Field("", max_length=200)
    descricao: str = Field("", max_length=2000)


class AfastamentoUpdateIn(BaseModel):
    tipo_afastamento: str | None = Field(None, max_length=30)
    data_inicio: str | None = Field(None, max_length=10)
    data_fim: str | None = Field(None, max_length=10)
    titulo: str | None = Field(None, max_length=200)
    descricao: str | None = Field(None, max_length=2000)


@router.post("/api/agenda/afastamentos", response_class=JSONResponse)
async def api_criar_afastamento(payload: AfastamentoIn):
    item = agenda_service.criar_afastamento(**payload.model_dump())
    return {"ok": True, "item": item}


@router.put("/api/agenda/afastamentos/{afastamento_id}", response_class=JSONResponse)
async def api_atualizar_afastamento(afastamento_id: str, payload: AfastamentoUpdateIn):
    item = agenda_service.atualizar_afastamento(afastamento_id, **payload.model_dump())
    if item is None:
        raise HTTPException(status_code=404, detail="afastamento não encontrado")
    return {"ok": True, "item": item}


@router.delete("/api/agenda/afastamentos/{afastamento_id}", response_class=JSONResponse)
async def api_remover_afastamento(afastamento_id: str):
    if not agenda_service.remover_afastamento(afastamento_id):
        raise HTTPException(status_code=404, detail="afastamento não encontrado")
    return {"ok": True}
