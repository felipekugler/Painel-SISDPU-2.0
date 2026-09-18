"""Rota do dashboard — lista PAJs do workspace Oficio Geral."""

import contextlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import PAJS_DIR
from services.paj_service import listar_pajs

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    template = request.app.state.jinja.get_template("dashboard.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(
        request=request,
        ambiente=ambiente,
    )
    return HTMLResponse(html)


@router.get("/api/pajs/count", response_class=JSONResponse)
async def api_pajs_count():
    """Retorna a contagem de PAJs na caixa atual.

    `excluir_submenu=False`: PAJs de Retornos/SAT contam aqui também (pedido
    do usuário 2026-09-18 — ver `api_pajs` abaixo para o motivo completo);
    sem isso, o badge do submenu "PAJs" (sidebar) contaria menos PAJs do que
    a própria listagem que ele representa."""
    pajs = listar_pajs(incluir_concluidos=False, excluir_submenu=False)
    return {"count": len(pajs)}


@router.get("/api/pajs", response_class=JSONResponse)
async def api_pajs():
    """Lista PAJs pro dashboard — sempre apenas os em_caixa_atual.

    O toggle "Mostrar concluídos" (e os parametros `concluidos`/`arquivados`
    que ele usava) foi removido em 2026-09-18 (pedido do usuario — nunca
    usado, e forcava uma 2a varredura completa de PAJS_DIR a cada load do
    dashboard so' para calcular o badge "(+N)", sem se beneficiar do cache
    de `listar_pajs` por este ser um unico slot por chave).

    `excluir_submenu=False` (pedido do usuario 2026-09-18): PAJs
    classificados como Retornos/SAT (`metadata.categoria_caixa`) TAMBEM
    aparecem aqui, alem de continuarem aparecendo nos submenus proprios —
    antes ficavam OMITIDOS da lista geral (decisao antiga documentada em
    `paj_service.listar_pajs`), o que dava a impressao de que menos PAJs
    tinham sido sincronizados do que realmente foram.
    """
    pajs = listar_pajs(incluir_concluidos=False, excluir_submenu=False)

    # "ultima_execucao" reflete a ultima vez que a CAIXA INTEIRA foi
    # sincronizada (rapida ou completa). Le do marcador gravado pelo
    # sincronizador.rodar(). Sync de um PAJ unico, busca leve via watchlist
    # ou download de anexos-desde-data NAO atualizam esse campo —
    # propositalmente, para que o dashboard nao "pareça" mais fresco do que
    # realmente está. Fallback para o mtime mais recente apenas se o
    # marcador ainda nao existir (workspace antigo, primeiro uso).
    ultima_execucao = ""
    marcador = PAJS_DIR / ".ultima_sync_caixa.json"
    if marcador.exists():
        with contextlib.suppress(Exception):
            dados = json.loads(marcador.read_text(encoding="utf-8"))
            ultima_execucao = dados.get("em", "")
    if not ultima_execucao:
        # Fallback de compatibilidade: workspace sem marcador (nunca rodou
        # sync nesta versao do painel). Pega o mtime mais recente como
        # aproximacao.
        try:
            mtimes = [p.stat().st_mtime for p in PAJS_DIR.glob("PAJ-*/metadata.json")]
            if mtimes:
                import datetime as _dt

                ultima_execucao = _dt.datetime.fromtimestamp(max(mtimes)).isoformat()
        except Exception:
            pass

    return {
        "pajs": pajs,
        "ultima_execucao": ultima_execucao,
    }
