"""Rotas do módulo "Ações no Eproc" (peticionamento no processo judicial):
rascunho (evento, prazos, petição, anexos), "Gerar petição com IA", e a execução
real no e-proc (`/executar/{paj_norm}` prepara evento+anexos na tela
"Movimentar/Peticionar"; `/executar/{paj_norm}/confirmar` protocola de fato,
clicando "Peticionar" — ver `services.acoes_eproc_service`)."""

from __future__ import annotations

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from services import acoes_eproc_service, config_painel_service, peticao_ia_service

router = APIRouter()


@router.get("/peticoes-pendentes", response_class=HTMLResponse)
async def pagina_peticoes_pendentes(request: Request):
    template = request.app.state.jinja.get_template("peticoes_pendentes.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)


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


# --- Rascunhos de petição ("Gravar" p/ peticionar mais tarde / "Petições pendentes") --

@router.get("/api/acoes-eproc/rascunhos", response_class=JSONResponse)
async def api_listar_rascunhos():
    """Todos os rascunhos salvos — alimenta a página "Petições pendentes"."""
    return JSONResponse({"ok": True, "itens": acoes_eproc_service.listar_rascunhos()})


@router.get("/api/acoes-eproc/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_obter_rascunho(paj_norm: str):
    """Rascunho salvo do PAJ, se houver — carregado automaticamente ao abrir o
    console "Ações no Eproc" na página do PAJ."""
    rascunho = acoes_eproc_service.obter_rascunho(paj_norm)
    return JSONResponse({"ok": True, "rascunho": rascunho})


@router.post("/api/acoes-eproc/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_salvar_rascunho(paj_norm: str, request: Request):
    """Grava/sobrescreve o rascunho de petição do PAJ (evento, prazos, petição,
    metadados dos anexos — os arquivos "Externo" já devem ter sido enviados
    antes via `POST .../upload`, ver `acoes_eproc.js:gravarRascunho`)."""
    body = await request.json()
    res = acoes_eproc_service.salvar_rascunho(paj_norm, body)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.delete("/api/acoes-eproc/rascunho/{paj_norm}", response_class=JSONResponse)
async def api_excluir_rascunho(paj_norm: str):
    """Descarta o rascunho — usado em "Petições pendentes" e depois que a
    petição for de fato protocolada no e-proc."""
    return JSONResponse(acoes_eproc_service.excluir_rascunho(paj_norm))


@router.post("/api/acoes-eproc/rascunho/{paj_norm}/upload", response_class=JSONResponse)
async def api_upload_arquivo_rascunho(paj_norm: str, arquivo: UploadFile = File(...)):
    """Envia os BYTES de um anexo "Externo" pro servidor, pra sobreviver ao
    "Gravar" — sem isso, o `File` do navegador se perde ao recarregar a página."""
    conteudo = await arquivo.read()
    res = await acoes_eproc_service.salvar_arquivo_rascunho(paj_norm, arquivo.filename or "arquivo", conteudo)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/acoes-eproc/rascunho/{paj_norm}/arquivo/{nome_arquivo}")
async def api_arquivo_rascunho(paj_norm: str, nome_arquivo: str):
    """Serve um anexo "Externo" já salvo no rascunho — usado pelo link
    "Visualizar" na tabela de anexos após um reload da página."""
    caminho = acoes_eproc_service.caminho_arquivo_rascunho(paj_norm, nome_arquivo)
    if not caminho:
        return JSONResponse({"ok": False, "erro": "Arquivo não encontrado."}, status_code=404)
    return FileResponse(caminho)


# --- "Gerar petição com IA" ---------------------------------------------------

@router.post("/api/acoes-eproc/gerar-peticao-ia/{paj_norm}", response_class=JSONResponse)
async def api_gerar_peticao_ia(paj_norm: str, request: Request):
    """Dispara a geração da petição via IA em background — responde ao(s)
    ato(s) referenciado(s) do(s) prazo(s) informado(s) (ver
    `services.peticao_ia_service`). Exige ao menos 1 prazo selecionado."""
    body = await request.json()
    processo = (body.get("processo") or "").strip()
    prazo_ids = body.get("prazo_ids") or []
    res = peticao_ia_service.iniciar(paj_norm, processo, prazo_ids)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/acoes-eproc/gerar-peticao-ia/{paj_norm}/status", response_class=JSONResponse)
async def api_gerar_peticao_ia_status(paj_norm: str):
    """Polling do status da geração em background (idle|running|done|error)."""
    return JSONResponse(peticao_ia_service.status(paj_norm))


@router.post("/api/acoes-eproc/incluir-peticao-anexo/{paj_norm}", response_class=JSONResponse)
async def api_incluir_peticao_anexo(paj_norm: str, request: Request):
    """"Incluir petição nos anexos" — formata o texto ATUAL da caixa de
    Petição (editado ou não) no padrão DPU e anexa como PDF. Sem chamada de
    IA — rápido e síncrono, mas roda em thread (conversão via Word/COM é
    bloqueante) pra não travar o event loop."""
    import asyncio

    body = await request.json()
    peticao_html = body.get("peticao_html") or ""
    res = await asyncio.to_thread(peticao_ia_service.incluir_nos_anexos, paj_norm, peticao_html)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


# --- "Executar no e-proc" (peticionamento real) -------------------------------

@router.get("/api/acoes-eproc/executar/{paj_norm}/resumo", response_class=JSONResponse)
async def api_resumo_execucao(paj_norm: str):
    """Resumo do que SERIA enviado (evento, processo, anexos com tipo/sigilo/
    caminho) a partir do rascunho salvo — usado pelo modal de confirmação antes
    de preparar de verdade no e-proc. Não toca no e-proc."""
    res = acoes_eproc_service.montar_resumo_execucao(paj_norm)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/acoes-eproc/executar/{paj_norm}", response_class=JSONResponse)
async def api_executar_peticionamento(paj_norm: str):
    """Prepara de verdade a tela "Movimentar/Peticionar" do e-proc a partir do
    rascunho salvo — evento + anexos, terminando em "Confirmar seleção de
    documentos". NÃO protocola (não clica "Peticionar") — ver
    `api_confirmar_peticionamento`, chamada só depois que o usuário revisar o
    resultado aqui e confirmar explicitamente."""
    res = await acoes_eproc_service.executar_peticionamento(paj_norm)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/acoes-eproc/executar/{paj_norm}/confirmar", response_class=JSONResponse)
async def api_confirmar_peticionamento(paj_norm: str, request: Request):
    """Clica "Peticionar" de fato na aba já preparada por `api_executar_peticionamento`
    — IRREVERSÍVEL, protocolo real na vara. Exige o `processo` (devolvido pela
    preparação) pra reencontrar a aba certa."""
    body = await request.json()
    processo = (body.get("processo") or "").strip()
    if not processo:
        return JSONResponse({"ok": False, "erro": "processo é obrigatório."}, status_code=400)
    res = await acoes_eproc_service.confirmar_peticionamento_real(paj_norm, processo)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/acoes-eproc/resumo-pos-peticionamento", response_class=JSONResponse)
async def api_resumo_pos_peticionamento(request: Request):
    """Depois de "Peticionar" confirmado com sucesso: gera (IA, rápido) o texto
    de descrição pra pré-preencher a movimentação no "Ações no SISDPU" — resumo
    objetivo da petição + documentos anexados + referência ao evento do e-proc.
    Não toca em arquivo nem no e-proc/SISDPU — só texto."""
    import asyncio

    body = await request.json()
    peticao_html = body.get("peticao_html") or ""
    anexos_nomes = body.get("anexos_nomes") or []
    evento_referencia = (body.get("evento_referencia") or "").strip()
    res = await asyncio.to_thread(
        peticao_ia_service.gerar_resumo_pos_peticionamento, peticao_html, anexos_nomes, evento_referencia)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)
