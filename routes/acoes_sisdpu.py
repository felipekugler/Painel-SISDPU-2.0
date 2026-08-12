"""Rotas do módulo genérico "Ações no SISDPU" (movimentar/tramitar/concluir),
reutilizável em qualquer página do painel — não depende da Retornos.

- GET  /movimentacoes-pendentes          — página: lista os rascunhos salvos.
- GET  /api/acoes-sisdpu/preferencias    — favoritas/frequentes de fases e caixas.
- POST /api/acoes-sisdpu/anexo-externo   — upload de um anexo externo (staging).
- POST /api/acoes-sisdpu/anexo-externo/remover — descarta um anexo externo staged.
- GET/POST/DELETE /api/acoes-sisdpu/rascunho/{paj_norm} — rascunho de movimentação.
- GET  /api/acoes-sisdpu/rascunhos       — todos os rascunhos salvos.
- GET  /api/acoes-sisdpu/executar        — SSE: executa mov→tram→conclui em um PAJ.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import acoes_sisdpu_service, config_painel_service

router = APIRouter()


@router.get("/movimentacoes-pendentes", response_class=HTMLResponse)
async def pagina_movimentacoes_pendentes(request: Request):
    template = request.app.state.jinja.get_template("movimentacoes_pendentes.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)


@router.get("/api/acoes-sisdpu/preferencias", response_class=JSONResponse)
async def api_preferencias():
    """Favoritas (dropdown) + frequentes (badges) de fases/caixas — do cache local
    (instantâneo, sem navegar no SISDPU). Mesma fonte da aba Configurações › SISDPU."""
    return JSONResponse(config_painel_service.get_sisdpu_prefs())


@router.post("/api/acoes-sisdpu/anexo-externo", response_class=JSONResponse)
async def api_anexo_externo_upload(paj_norm: str = Form(...), arquivo: UploadFile | None = None):
    """Recebe um arquivo do computador do usuário (upload) e o guarda numa pasta
    temporária dentro da pasta do PAJ — só assim a automação (Playwright) consegue
    anexá-lo depois a uma movimentação (precisa estar em disco no servidor)."""
    if arquivo is None:
        return JSONResponse({"ok": False, "detalhe": "Nenhum arquivo enviado."}, status_code=400)
    conteudo = await arquivo.read()
    return JSONResponse(acoes_sisdpu_service.salvar_anexo_externo(paj_norm, arquivo.filename or "", conteudo))


@router.post("/api/acoes-sisdpu/anexo-externo/remover", response_class=JSONResponse)
async def api_anexo_externo_remover(paj_norm: str = Form(...), ref: str = Form(...)):
    """Descarta um anexo externo staged ANTES de executar (usuário desistiu dele) —
    evita lixo acumulando na pasta temporária."""
    return JSONResponse(acoes_sisdpu_service.remover_anexo_externo(paj_norm, ref))


# --- Rascunhos de movimentação ("Gravar" p/ executar mais tarde) --------------

@router.get("/api/acoes-sisdpu/rascunhos", response_class=JSONResponse)
async def api_listar_rascunhos():
    """Todos os rascunhos salvos — alimenta a página "Movimentações pendentes"."""
    return JSONResponse({"ok": True, "itens": acoes_sisdpu_service.listar_rascunhos()})


@router.get("/api/acoes-sisdpu/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_obter_rascunho(paj_norm: str):
    """Rascunho salvo do PAJ, se houver — carregado automaticamente ao abrir o
    console "Ações no SISDPU" (inclusive o gravado automaticamente por "Ações no
    Eproc" depois de um Peticionar real confirmado)."""
    return JSONResponse({"ok": True, "rascunho": acoes_sisdpu_service.obter_rascunho(paj_norm)})


@router.post("/api/acoes-sisdpu/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_salvar_rascunho(paj_norm: str, request: Request):
    """Grava/sobrescreve o rascunho de movimentação do PAJ."""
    body = await request.json()
    res = acoes_sisdpu_service.salvar_rascunho(paj_norm, body)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.delete("/api/acoes-sisdpu/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_excluir_rascunho(paj_norm: str):
    """Descarta o rascunho — usado depois que a movimentação for de fato executada
    com sucesso, ou quando o usuário desistir manualmente."""
    return JSONResponse(acoes_sisdpu_service.excluir_rascunho(paj_norm))


async def _stream_executar(id_processo: str, paj: str, paj_norm: str, fase: str, descricao_mov: str,
                           descricao_mov_html: bool, grupo: str, descricao_tram: str, prazo: int,
                           prazo_data: str, id_tramite: str, concluir: bool, dry: bool, anexos: list):
    async for ev in acoes_sisdpu_service.executar_stream(
        id_processo, paj=paj, paj_norm=paj_norm, fase=fase, descricao_mov=descricao_mov,
        descricao_mov_html=descricao_mov_html, grupo=grupo or None,
        descricao_tram=descricao_tram, prazo_dias=prazo, prazo_data=prazo_data,
        id_tramite=id_tramite or None, concluir=concluir, dry_run=dry, anexos=anexos,
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/acoes-sisdpu/executar")
async def executar(id_processo: str, paj: str = "", paj_norm: str = "", fase: str = "", descricao_mov: str = "",
                   descricao_mov_html: int = 0, grupo: str = "", descricao_tram: str = "", prazo: int = 0,
                   prazo_data: str = "", id_tramite: str = "", concluir: int = 0, dry: int = 0, anexos: str = ""):
    """SSE: movimenta (fase + despacho da movimentação, opcionalmente com documentos do
    PAJ anexados — `anexos`, JSON de caminhos relativos, requer `paj_norm` — e HTML de
    verdade quando `descricao_mov_html=1`, escrito direto no CKEditor do despacho) →
    tramita (se grupo, com despacho próprio da tramitação — SEMPRE texto puro, aquele
    campo não é CKEditor) → conclui (se concluir=1 E id_tramite informado — sem
    id_tramite não há trâmite específico a concluir). Os dois despachos são
    independentes e podem ficar em branco — o SISDPU aceita ambos vazios."""
    try:
        anexos_list = json.loads(anexos) if anexos else []
        if not isinstance(anexos_list, list):
            anexos_list = []
    except Exception:  # noqa: BLE001 — JSON malformado: trata como "nenhum anexo"
        anexos_list = []
    return EventSourceResponse(_stream_executar(
        id_processo, paj, paj_norm, fase, descricao_mov, bool(int(descricao_mov_html)), grupo, descricao_tram,
        int(prazo), prazo_data, id_tramite, bool(int(concluir)), bool(int(dry)), anexos_list))
