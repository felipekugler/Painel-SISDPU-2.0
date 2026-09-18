"""Rotas da página "Movimentar Eproc + SISDPU" — peticionar/lançar evento no
processo judicial E registrar a movimentação no SISDPU numa única execução,
cada lado com seus próprios parâmetros definidos lado a lado (ver
templates/movimentar_conjunto.html). Só resolve PAJ<->processo aqui
(services.paj_processo_link_service) — a gravação/execução em si reaproveita
100% das rotas já existentes de "Ações no Eproc" e "Ações no SISDPU"."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import intimacao_service, paj_processo_link_service

router = APIRouter()


@router.get("/movimentar-conjunto", response_class=HTMLResponse)
async def pagina_movimentar_conjunto(request: Request):
    template = request.app.state.jinja.get_template("movimentar_conjunto.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)


@router.get("/api/movimentar-conjunto/resolver-pajs", response_class=JSONResponse)
async def api_resolver_pajs(processo: str):
    """PAJ(s) candidato(s) pra este número de processo (0, 1 ou mais — mais de
    1 é uma ambiguidade real, a tela mostra um dropdown de escolha)."""
    return JSONResponse({"candidatos": paj_processo_link_service.resolver_pajs_de_processo(processo)})


@router.get("/api/movimentar-conjunto/resolver-processos", response_class=JSONResponse)
async def api_resolver_processos(paj: str):
    """Processo(s) candidato(s) pra este PAJ (0, 1 ou mais). Aceita `paj` tanto
    em "AAAA/UUU-NNNNN" quanto já normalizado ("PAJ-AAAA-UUU-NNNNN") — o campo
    de entrada da tela aceita o formato que o usuário digitar."""
    paj_norm = intimacao_service.paj_para_norm(paj) or ""
    return JSONResponse({"candidatos": paj_processo_link_service.resolver_processos_de_paj(paj_norm)})
