"""Rotas do módulo genérico "Ações no SISDPU" (movimentar/tramitar/concluir),
reutilizável em qualquer página do painel — não depende da Retornos.

- GET  /movimentacoes-pendentes          — página: lista os rascunhos salvos.
- GET  /api/acoes-sisdpu/preferencias    — favoritas/frequentes de fases e caixas.
- GET  /api/acoes-sisdpu/pecas-ia/{paj_norm} — arquivos da subpasta "Peças IA" do PAJ.
- POST /api/acoes-sisdpu/sintese-paj/{paj_norm} — gera por IA a "SÍNTESE DA
  TRAMITAÇÃO DO PAJ" (botão ao lado de "Usar modelo").
- GET  /api/acoes-sisdpu/despachos/{paj_norm} — despachos gravados do PAJ
  (Ações com IA › Minutar Despacho; vários por PAJ).
- GET/DELETE /api/acoes-sisdpu/despacho/{paj_norm}/{id} — abre/exclui um despacho;
  POST /api/acoes-sisdpu/despacho/{paj_norm} grava (cria ou atualiza);
  POST .../{id}/lancado marca "Lançado no SISDPU".
- POST /api/acoes-sisdpu/despacho-ia/{paj_norm} — redige o despacho por IA a
  partir da orientação diretiva.
- POST /api/acoes-sisdpu/anexo-externo   — upload de um anexo externo (staging).
- POST /api/acoes-sisdpu/anexo-externo/remover — descarta um anexo externo staged.
- GET/POST/DELETE /api/acoes-sisdpu/rascunho/{paj_norm} — rascunho de movimentação.
- GET  /api/acoes-sisdpu/rascunhos       — todos os rascunhos salvos.
- GET  /api/acoes-sisdpu/executar        — SSE: executa mov→tram→conclui em um PAJ
  (conclui aceita `id_tramites` — lista JSON, 0/1/N trâmites da caixa — ou o par
  legado `id_tramite`+`concluir`).
- GET  /api/acoes-sisdpu/executar-fila   — SSE: executa uma FILA de movimentações em
  sequência (mesma sessão do SISDPU) — ver "Movimentações Pendentes".
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
    """Favoritas (dropdown) + frequentes (badges) de fases/caixas + despachos
    padrão cadastrados — do cache local (instantâneo, sem navegar no SISDPU).
    Mesma fonte da aba Configurações › SISDPU."""
    return JSONResponse({
        **config_painel_service.get_sisdpu_prefs(),
        "modelos_despacho": config_painel_service.get_sisdpu_modelos_despacho(),
    })


@router.get("/api/acoes-sisdpu/pecas-ia/{paj_norm}", response_class=JSONResponse)
async def api_pecas_ia(paj_norm: str):
    """Arquivos da subpasta "Peças IA" do PAJ (petições/peças geradas pela análise
    de pretensão) — alimenta o modal "Adicionar Petições IA"."""
    return JSONResponse({"ok": True, "itens": acoes_sisdpu_service.listar_pecas_ia(paj_norm)})


@router.post("/api/acoes-sisdpu/sintese-paj/{paj_norm}", response_class=JSONResponse)
async def api_sintese_paj(paj_norm: str):
    """Gera por IA a "SÍNTESE DA TRAMITAÇÃO DO PAJ" (texto corrido, a partir do
    PROMPT_MAX do PAJ) — botão "Gerar Síntese do PAJ" ao lado de "Usar modelo".
    Chamada bloqueante (pode levar dezenas de segundos), mas síncrona pro
    frontend — mesmo padrão de /api/valor-causa/{paj_norm}."""
    return JSONResponse(await acoes_sisdpu_service.gerar_sintese_paj(paj_norm))


@router.post("/api/acoes-sisdpu/despacho-ia/{paj_norm}", response_class=JSONResponse)
async def api_despacho_ia(paj_norm: str, request: Request):
    """Redige por IA o despacho a partir da orientação diretiva (Ações com IA ›
    Minutar Despacho › Despacho com IA). Bloqueante (dezenas de segundos)."""
    body = await request.json()
    return JSONResponse(await acoes_sisdpu_service.gerar_despacho_ia(paj_norm, (body or {}).get("orientacao", "")))


@router.get("/api/acoes-sisdpu/despachos/{paj_norm}", response_class=JSONResponse)
async def api_listar_despachos(paj_norm: str):
    """Despachos gravados do PAJ (lista abaixo dos subitens de Minutar Despacho)."""
    return JSONResponse({"ok": True, "itens": acoes_sisdpu_service.listar_despachos(paj_norm)})


@router.get("/api/acoes-sisdpu/despacho/{paj_norm}/{id_despacho}", response_class=JSONResponse)
async def api_obter_despacho(paj_norm: str, id_despacho: str):
    """Um despacho gravado, com o texto, para abrir e editar."""
    return JSONResponse({"ok": True, "despacho": acoes_sisdpu_service.obter_despacho(paj_norm, id_despacho)})


@router.post("/api/acoes-sisdpu/despacho/{paj_norm}", response_class=JSONResponse)
async def api_salvar_despacho(paj_norm: str, request: Request):
    """Grava o despacho — {tipo, texto} cria um novo; com {id} atualiza aquele."""
    b = (await request.json()) or {}
    res = acoes_sisdpu_service.salvar_despacho(paj_norm, b.get("tipo", ""), b.get("texto", ""), b.get("id", ""))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/acoes-sisdpu/despacho/{paj_norm}/{id_despacho}/lancado", response_class=JSONResponse)
async def api_despacho_lancado(paj_norm: str, id_despacho: str):
    """Marca o despacho como levado ao SISDPU (botão "Lançar no SISDPU")."""
    res = acoes_sisdpu_service.marcar_despacho_lancado(paj_norm, id_despacho)
    return JSONResponse(res, status_code=200 if res.get("ok") else 404)


@router.delete("/api/acoes-sisdpu/despacho/{paj_norm}/{id_despacho}", response_class=JSONResponse)
async def api_excluir_despacho(paj_norm: str, id_despacho: str):
    res = acoes_sisdpu_service.excluir_despacho(paj_norm, id_despacho)
    return JSONResponse(res, status_code=200 if res.get("ok") else 404)


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


@router.get("/api/acoes-sisdpu/rascunhos/count", response_class=JSONResponse)
async def api_rascunhos_count():
    """Contagem pro badge do submenu "Movimentações pendentes" — mesmo total
    exibido no cabeçalho da própria página, sem tocar em nada."""
    return {"count": len(acoes_sisdpu_service.listar_rascunhos())}


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
                           prazo_data: str, id_tramite: str, concluir: bool, id_tramites: list,
                           dry: bool, anexos: list, etapa: str = ""):
    async for ev in acoes_sisdpu_service.executar_stream(
        id_processo, paj=paj, paj_norm=paj_norm, fase=fase, descricao_mov=descricao_mov,
        descricao_mov_html=descricao_mov_html, grupo=grupo or None,
        descricao_tram=descricao_tram, prazo_dias=prazo, prazo_data=prazo_data,
        id_tramite=id_tramite or None, concluir=concluir, id_tramites=id_tramites,
        dry_run=dry, anexos=anexos, etapa=etapa,
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/acoes-sisdpu/executar")
async def executar(paj: str = "", id_processo: str = "", paj_norm: str = "", fase: str = "", descricao_mov: str = "",
                   descricao_mov_html: int = 0, grupo: str = "", descricao_tram: str = "", prazo: int = 0,
                   prazo_data: str = "", id_tramite: str = "", concluir: int = 0, id_tramites: str = "",
                   dry: int = 0, anexos: str = "", etapa: str = ""):
    """SSE: movimenta (fase + despacho da movimentação, opcionalmente com documentos do
    PAJ anexados — `anexos`, JSON de caminhos relativos, requer `paj_norm` — e HTML de
    verdade quando `descricao_mov_html=1`, escrito direto no CKEditor do despacho) →
    tramita (se grupo, com despacho próprio da tramitação — SEMPRE texto puro, aquele
    campo não é CKEditor) → conclui (se `id_tramites` — JSON de lista, ex.: '["123","456"]'
    — trouxer ao menos um id; OU, no par legado, se concluir=1 E id_tramite informado).
    Movimentar/tramitar/concluir são independentes entre si. Os dois despachos também
    são independentes e podem ficar em branco — o SISDPU aceita ambos vazios.

    `id_processo` agora é OPCIONAL: sem ele (PAJ ainda não sincronizado localmente —
    "Adicionar movimentação" em Movimentações Pendentes), `executar_stream` resolve o
    id automaticamente pelo número do PAJ (`paj`, obrigatório nesse caso) via busca
    global no SISDPU (`services/paj_link_service.resolver`) antes de prosseguir."""
    try:
        anexos_list = json.loads(anexos) if anexos else []
        if not isinstance(anexos_list, list):
            anexos_list = []
    except Exception:  # noqa: BLE001 — JSON malformado: trata como "nenhum anexo"
        anexos_list = []
    try:
        id_tramites_list = json.loads(id_tramites) if id_tramites else []
        if not isinstance(id_tramites_list, list):
            id_tramites_list = []
    except Exception:  # noqa: BLE001 — JSON malformado: trata como "nenhum selecionado"
        id_tramites_list = []
    return EventSourceResponse(_stream_executar(
        id_processo, paj, paj_norm, fase, descricao_mov, bool(int(descricao_mov_html)), grupo, descricao_tram,
        int(prazo), prazo_data, id_tramite, bool(int(concluir)), id_tramites_list, bool(int(dry)), anexos_list,
        etapa))


async def _stream_executar_fila(id_processo: str, paj: str, paj_norm: str, itens: list, dry: bool):
    async for ev in acoes_sisdpu_service.executar_fila_stream(
        id_processo, paj=paj, paj_norm=paj_norm, itens=itens, dry_run=dry,
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        elif ev.get("item_done"):
            yield {"event": "item_done", "data": json.dumps(
                {"index": ev.get("index"), "ok": ev.get("ok"), "resultado": ev.get("resultado", {})},
                ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/acoes-sisdpu/executar-fila")
async def executar_fila(paj: str = "", id_processo: str = "", paj_norm: str = "", itens: str = "", dry: int = 0):
    """SSE: executa uma FILA de movimentações (cada uma: mov→tram→conclui) em
    SEQUÊNCIA, na MESMA sessão do SISDPU (resolve `id_processo` uma única vez pra
    fila inteira — ver `executar_stream` sobre a resolução automática pelo número).

    `itens`: JSON de lista de dicts, cada um com as mesmas chaves de uma execução
    única (fase, descricao_mov, descricao_mov_html, grupo, descricao_tram, prazo,
    prazo_data, id_tramites, anexos) — sem o par legado id_tramite/concluir (a fila
    é um modelo novo, só usa a lista de trâmites por item).

    PARA no primeiro item que falhar. Emite um evento `item_done` extra
    ({index, ok, resultado}) depois de CADA item — o frontend usa isso pra ir
    removendo da fila persistida só os itens que realmente terminaram, conforme a
    execução avança, em vez de esperar o evento final `done` pra saber o que sobrou."""
    try:
        itens_list = json.loads(itens) if itens else []
        if not isinstance(itens_list, list):
            itens_list = []
    except Exception:  # noqa: BLE001 — JSON malformado: trata como "fila vazia"
        itens_list = []
    return EventSourceResponse(_stream_executar_fila(id_processo, paj, paj_norm, itens_list, bool(int(dry))))
