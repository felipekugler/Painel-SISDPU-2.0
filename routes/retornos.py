"""Rotas da feature "Retornos".

- GET  /retornos                        — página (tabela de atendimentos de retorno).
- GET  /api/retornos/lista              — registros já sincronizados (store local).
- GET  /api/retornos/sincronizar        — SSE: lê a caixa, filtra os retornos,
                                          detalha e analisa com a IA leve.
- POST /api/retornos/atualizar          — reconciliação rápida (sem SSE): só remove
                                          da lista quem já saiu da caixa.
- POST /api/retornos/{id_tramite}/sugestao — salva a edição manual da sugestão.
- GET  /api/retornos/{id_tramite}/ressincronizar-paj — refaz a leitura de UM PAJ
                                          no SISDPU (modo rápido) e atualiza o metadata.
- POST /api/retornos/{id_tramite}/pos-execucao — pós-processo da Retornos após o
                                          console genérico "Ações no SISDPU" executar
                                          (histórico do painel + remoção da lista).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import retornos_service

router = APIRouter()


@router.get("/retornos", response_class=HTMLResponse)
async def pagina_retornos(request: Request):
    template = request.app.state.jinja.get_template("retornos.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/retornos/lista", response_class=JSONResponse)
async def api_lista():
    return JSONResponse(retornos_service.listar())


@router.get("/api/retornos/count", response_class=JSONResponse)
async def api_retornos_count():
    """Contagem pro badge do submenu — mesmo total exibido no cabeçalho da própria
    página Retornos (itens.length lá), sem re-sincronizar nada."""
    dados = retornos_service.listar()
    return {"count": len(dados.get("itens", []) or [])}


async def _stream_sincronizar():
    async for ev in retornos_service.sincronizar_stream():
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/retornos/sincronizar")
async def sincronizar():
    """SSE: lê a caixa de entrada, filtra os PAJs com trâmite de 'Atendimento de
    Retorno', detalha cada um (movimentações + descrição integral) e dispara a IA
    leve (resumo + sugestão)."""
    return EventSourceResponse(_stream_sincronizar())


@router.post("/api/retornos/atualizar", response_class=JSONResponse)
async def api_atualizar():
    """Reconciliação rápida (sem SSE, poucos segundos): remove do painel os retornos
    cujo trâmite já saiu da caixa de entrada (concluídos/tramitados por fora). Não
    sincroniza PAJs novos nem reanalisa — isso é papel do botão "Sincronizar". Mesmo
    padrão do botão "Atualizar" do dashboard."""
    return JSONResponse(await retornos_service.atualizar_caixa())


@router.post("/api/retornos/{id_tramite}/sugestao", response_class=JSONResponse)
async def api_salvar_sugestao(id_tramite: str, request: Request):
    body = await request.json()
    return JSONResponse(retornos_service.salvar_sugestao(id_tramite, body.get("texto", "")))


@router.post("/api/retornos/{id_tramite}/reanalisar", response_class=JSONResponse)
async def api_reanalisar(id_tramite: str, request: Request):
    """Reanalisa o retorno com a IA; `prompt` (opcional) = instruções adicionais."""
    body = await request.json()
    return JSONResponse(await retornos_service.reanalisar(id_tramite, body.get("prompt", "")))


@router.get("/api/retornos/{id_tramite}/caixas-abertas", response_class=JSONResponse)
async def api_caixas_abertas(id_tramite: str):
    """Caixas/usuários onde o PAJ está aberto agora (badge acima dos cards)."""
    return JSONResponse(await retornos_service.caixas_abertas(id_tramite))


@router.post("/api/retornos/{id_tramite}/pos-execucao", response_class=JSONResponse)
async def api_pos_execucao(id_tramite: str, request: Request):
    """Chamado pelo frontend logo após o console genérico "Ações no SISDPU" concluir
    a execução (SSE de /api/acoes-sisdpu/executar): registra no Histórico do painel e
    remove o retorno da lista quando o trâmite foi concluído (efeitos EXCLUSIVOS da
    Retornos). Body: {"resultado": {...payload do onExecutado...}}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — corpo vazio/malformado: trata como "nada a registrar"
        body = {}
    resultado = body.get("resultado") or {}
    return JSONResponse(await retornos_service.registrar_pos_execucao(id_tramite, resultado))


async def _stream_ressincronizar_paj(id_tramite: str, completo: bool, ia: bool):
    async for ev in retornos_service.ressincronizar_paj_stream(id_tramite, completo=completo, ia=ia):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/retornos/{id_tramite}/ressincronizar-paj")
async def ressincronizar_paj(id_tramite: str, completo: int = 0, ia: int = 0):
    """Ressincroniza SÓ este PAJ — modo e IA escolhidos INDEPENDENTEMENTE no minimenu
    do botão "Sincronizar" de cada linha:

    `completo=0` (padrão, **leve**): sem baixar anexos/OCR nem indexar documentos,
    esperas curtas — só o núcleo do metadata (ofício, assistido, pretensão,
    movimentações). Serve pra conferir rapidamente se uma movimentação/tramitação/
    conclusão foi mesmo efetivada no SISDPU (ex.: após um "erro de conexão" no
    console Ações no SISDPU), sem pagar o custo da extração de documentos.

    `completo=1`: baixa e indexa os documentos das movimentações (igual à aba PAJs
    em "Sincronizar com anexos").

    `ia=1` (independente do modo): reanalisa com a IA (resumo/sugestão) ao final —
    o usuário liga isso só quando precisar de uma análise nova, evitando o custo em
    toda ressincronização leve de conferência.

    Em qualquer combinação, NUNCA é a sincronização pesada da aba PAJs propriamente dita."""
    return EventSourceResponse(_stream_ressincronizar_paj(id_tramite, completo=bool(completo), ia=bool(ia)))


