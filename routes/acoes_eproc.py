"""Rotas do módulo "Ações no Eproc" (peticionamento no processo judicial) — EM
CONSTRUÇÃO. Por ora só a listagem de prazos abertos/pendentes está implementada;
evento a lançar, petição, anexos e execução real do protocolo ficam para quando a
tela de peticionamento do e-proc for inspecionada e desenvolvida."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from services import acoes_eproc_service, config_painel_service

router = APIRouter()


@router.get("/api/acoes-eproc/preferencias", response_class=JSONResponse)
async def api_preferencias():
    """Favoritos (dropdown) + frequentes (badges) de eventos e de tipos de documento
    — do cache local (instantâneo). Curadoria feita em Configurações › e-proc."""
    return JSONResponse({
        **config_painel_service.get_eproc_eventos_prefs(),
        **config_painel_service.get_eproc_tipos_doc_prefs(),
        **config_painel_service.get_eproc_sigilo_prefs(),
    })


@router.get("/api/acoes-eproc/prazos", response_class=JSONResponse)
async def api_prazos(processo: list[str] = Query(default=[])):
    """Prazos abertos/pendentes (cache local de Prazos e-proc) dos processos
    informados — alimenta o checkbox "Selecione o(s) prazo(s) a ser(em) fechado(s)"."""
    return JSONResponse({"ok": True, "itens": acoes_eproc_service.listar_prazos(processo)})


@router.post("/api/acoes-eproc/comprimir-anexo", response_class=JSONResponse)
async def api_comprimir_anexo(request: Request):
    """Comprime/divide um anexo PDF de um PAJ (reaproveita a skill global
    PDF-Comprimir_Dividir) para caber no limite de tamanho do e-proc. O(s) resultado(s)
    ficam gravados na pasta `Arquivos/` do PAJ (NÃO em metadata.json — `anexos_sisdpu`
    é recalculado ao vivo dali) e já aparecem selecionáveis em "Adicionar do SISDPU"."""
    body = await request.json()
    paj_norm = (body.get("paj_norm") or "").strip()
    caminho = (body.get("caminho") or "").strip()
    if not paj_norm or not caminho:
        return JSONResponse({"ok": False, "erro": "paj_norm e caminho são obrigatórios."}, status_code=400)
    res = await acoes_eproc_service.comprimir_anexo(paj_norm, caminho)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)
