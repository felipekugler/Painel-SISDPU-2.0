"""Rota de sincronizacao da caixa SISDPU via SSE."""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from services.sync_service import (
    esta_rodando,
    rodar_sync,
    rodar_sync_paj,
    rodar_sync_anexos_desde_data,
    rodar_sync_baixar_documentos,
    rodar_sync_watchlist,
    solicitar_cancelamento,
)


router = APIRouter(prefix="/api")


async def _stream(baixar_anexos: bool, rodar_ocr: bool):
    async for linha in rodar_sync(baixar_anexos=baixar_anexos, rodar_ocr=rodar_ocr):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronização finalizada"}


async def _stream_paj(paj_identificador: str, baixar_anexos: bool, rodar_ocr: bool):
    async for linha in rodar_sync_paj(paj_identificador, baixar_anexos=baixar_anexos, rodar_ocr=rodar_ocr):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronização do PAJ finalizada"}


async def _stream_anexos_desde(paj_identificador: str, data_inicio: _dt.date, rodar_ocr: bool):
    async for linha in rodar_sync_anexos_desde_data(paj_identificador, data_inicio, rodar_ocr=rodar_ocr):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Download de anexos finalizado"}


async def _stream_watchlist(baixar_anexos: bool, rodar_ocr: bool):
    async for linha in rodar_sync_watchlist(baixar_anexos=baixar_anexos, rodar_ocr=rodar_ocr):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Sincronização da watchlist finalizada"}


async def _stream_baixar_documentos(paj_norm: str, ids: list[str]):
    async for linha in rodar_sync_baixar_documentos(paj_norm, ids):
        yield {"event": "log", "data": linha.rstrip("\n")}
    yield {"event": "done", "data": "Download de documentos finalizado"}


@router.get("/sync")
async def sync(anexos: int = 1, ocr: int = 0):
    """Sincroniza a caixa SISDPU.

    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado —
    so baixa; use "Melhorar OCR" na aba Anexos SISDPU pra texto de qualidade).
    """
    return EventSourceResponse(_stream(baixar_anexos=bool(anexos), rodar_ocr=bool(ocr)))


@router.get("/sync/paj/{paj_norm}")
async def sync_paj(paj_norm: str, anexos: int = 1, ocr: int = 0):
    """Sincroniza apenas UM PAJ (uso de teste / refresh pontual).

    `paj_norm` aceita 'PAJ-2021-044-00635' ou '2021/044-00635' (URL-encoded).
    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado).
    """
    return EventSourceResponse(_stream_paj(paj_norm, baixar_anexos=bool(anexos), rodar_ocr=bool(ocr)))


@router.get("/sync/paj/{paj_norm}/anexos-desde")
async def sync_anexos_desde(paj_norm: str, data: str = "", ocr: int = 0):
    """Baixa anexos de um PAJ a partir de uma data de corte (inclusive).

    Usado quando o sync padrao detecta overflow (PAJ tem >MAX_ANEXOS_POR_PAJ
    anexos) e o usuario quer completar a partir de uma data especifica.

    Query: ?data=YYYY-MM-DD (obrigatorio). ?ocr=1 roda o OCR simples (padrao: desabilitado).
    """
    try:
        data_inicio = _dt.date.fromisoformat(data)
    except (ValueError, TypeError):
        return JSONResponse(
            {"erro": "parametro 'data' invalido — use formato YYYY-MM-DD"},
            status_code=400,
        )
    return EventSourceResponse(_stream_anexos_desde(paj_norm, data_inicio, rodar_ocr=bool(ocr)))


@router.get("/sync/watchlist")
async def sync_watchlist(anexos: int = 1, ocr: int = 0):
    """Sincroniza todos os PAJs ativos da watchlist via busca global do SIS-DPU.

    Entra pela "Pesquisa Rapida" do header — funciona mesmo se o PAJ ja
    saiu da caixa de entrada (concluido).

    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado).
    """
    return EventSourceResponse(_stream_watchlist(baixar_anexos=bool(anexos), rodar_ocr=bool(ocr)))


@router.get("/sync/paj/{paj_norm}/baixar-documentos")
async def sync_baixar_documentos(paj_norm: str, ids: str = ""):
    """Baixa documentos ESPECIFICOS (por ID) de um PAJ ja sincronizado.

    Nao ressincroniza nada — le as URLs direto do metadata.json existente
    (populado pela extracao por seq de uma sincronizacao anterior). Usado
    pelos botoes "Baixar todos"/"Baixar selecionados" da coluna Documentos.

    Query: ?ids=41893728,41893730 (obrigatorio, separados por virgula — o
    numero na URL de download do SISDPU, igual ao usado no nome do arquivo
    local "seqNNN - ID - nome").
    """
    lista_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not lista_ids:
        return JSONResponse({"erro": "parametro 'ids' obrigatorio"}, status_code=400)
    return EventSourceResponse(_stream_baixar_documentos(paj_norm, lista_ids))


@router.post("/sync/cancel")
async def sync_cancel():
    """Sinaliza o worker de sync atual para parar na proxima oportunidade.

    O sincronizador consulta o flag entre PAJs e aborta o laco de forma limpa
    (sem concluir PAJs por engano). Se nao houver sync rodando, retorna 404.
    """
    if not esta_rodando():
        return {"ok": False, "motivo": "nenhuma sync em andamento"}
    sinalizado = solicitar_cancelamento()
    return {"ok": sinalizado}
