"""Rotas do "Histórico" (conclusões de caixa de entrada efetivadas dentro do
painel — retornos, SAT, etc.) e de "Análises e Iniciais" (subitem da Caixa de
Entrada — casos com análise/petição inicial da IA aguardando revisão do
Defensor; antes era a aba "Análises (IA)" do Histórico, removida em
2026-09-30 a pedido do usuário)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import historico, historico_painel

router = APIRouter()


@router.get("/historico", response_class=HTMLResponse)
async def pagina_historico(request: Request):
    template = request.app.state.jinja.get_template("historico.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/historico/lista", response_class=JSONResponse)
async def api_lista():
    return JSONResponse({"registros": historico_painel.listar()})


@router.get("/analises-iniciais", response_class=HTMLResponse)
async def pagina_analises_iniciais(request: Request):
    template = request.app.state.jinja.get_template("analises_iniciais.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/analises-iniciais/lista", response_class=JSONResponse)
async def api_analises_iniciais_lista():
    return JSONResponse({"registros": historico.listar_analises_iniciais_pendentes()})


@router.get("/api/analises-iniciais/em-processamento", response_class=JSONResponse)
async def api_analises_iniciais_em_processamento():
    """Tabela "Em processamento" — ver `historico.listar_em_processamento`."""
    return JSONResponse({"registros": historico.listar_em_processamento()})


@router.get("/api/analises-iniciais/count", response_class=JSONResponse)
async def api_analises_iniciais_count():
    return JSONResponse({"count": len(historico.listar_analises_iniciais_pendentes())})


@router.post("/api/analises-iniciais/concluir/{paj_norm}", response_class=JSONResponse)
async def api_analises_iniciais_concluir(paj_norm: str, payload: dict = Body(default={})):
    """Marca o caso como tratado — some da lista e registra a conclusão no
    Histórico (ver `historico.concluir_analise_inicial`). Body opcional
    {paj, assistido, atividade} — o front já tem esses dados no registro
    sendo concluído, evita reler do disco pra só repetir o que já exibia."""
    ok = historico.concluir_analise_inicial(
        paj_norm,
        paj=(payload.get("paj") or ""),
        assistido=(payload.get("assistido") or ""),
        atividade=(payload.get("atividade") or ""),
    )
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400)
