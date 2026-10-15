"""Rota de detalhe do PAJ."""

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ingestao import sisdpu_movtram
from services import etapa_paj_service, lembretes_service
from services.download_completo_service import gerar_pdf_consolidado, verificar_sincronizacao_completa
from services.paj_service import (
    PajNorm,
    abrir_pasta_paj,
    concluir_tramites_caixa,
    definir_whatsapp_numeros,
    excluir_arquivo_paj,
    invalidar_cache_listagem,
    ler_paj,
    limpar_anexos_paj,
    registrar_pos_execucao_sisdpu,
)

router = APIRouter()


@router.get("/paj/{paj_norm}", response_class=HTMLResponse)
async def paj_detail(request: Request, paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    if not dados:
        # PAJ pedido (caixa de pesquisa do menu, minimenu "Abrir PAJ" antigo,
        # link/favorito) mas ainda sem pasta local — em vez do "PAJ não
        # encontrado" morto de antes, mostra uma tela de carregamento que
        # dispara sozinha a sincronização avulsa e recarrega quando terminar
        # (ver templates/paj_carregando.html + pajCarregandoApp() em app.js).
        # 200, não 404: é uma resposta válida (a tela em si carregou certo),
        # só ainda não tem os dados do PAJ.
        template = request.app.state.jinja.get_template("paj_carregando.html")
        html = template.render(request=request, paj_norm=paj_norm)
        return HTMLResponse(html)
    template = request.app.state.jinja.get_template("paj_detail.html")
    html = template.render(request=request, dados=dados, paj_norm=paj_norm,
                           etapa=etapa_paj_service.calcular(paj_norm),
                           etapas_manuais=etapa_paj_service.opcoes_manuais(),
                           etapas_todas=etapa_paj_service.opcoes_todas())
    return HTMLResponse(html)


@router.get("/api/paj/{paj_norm}/etapa", response_class=JSONResponse)
async def api_etapa(paj_norm: PajNorm):
    """Etapa atual do PAJ (manual = última movimentação "Etapa: …"; automática =
    deduzida das rotinas do painel) — ver services/etapa_paj_service."""
    return {"ok": True, "etapa": etapa_paj_service.calcular(paj_norm),
            "manuais": etapa_paj_service.opcoes_manuais(), "todas": etapa_paj_service.opcoes_todas()}


@router.post("/api/paj/{paj_norm}/etapa", response_class=JSONResponse)
async def api_registrar_etapa(paj_norm: PajNorm, payload: dict = Body(...)):
    """Grava a etapa escolhida pelo Defensor como movimentação "Etapa: …" no
    SIS-DPU 2.0 (com a ficha de confirmação). Body: {"etapa": "<chave>"}."""
    r = await etapa_paj_service.registrar(paj_norm, (payload or {}).get("etapa") or "")
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/paj/{paj_norm}/etapa/minuta-solicitada", response_class=JSONResponse)
async def api_etapa_minuta_solicitada(paj_norm: PajNorm):
    """Botão "Minutar Petição Inicial": a etapa passa a "Elaborando minuta de inicial"."""
    return etapa_paj_service.marcar_minuta_solicitada(paj_norm)


@router.get("/api/paj/{paj_norm}", response_class=JSONResponse)
async def api_paj(paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    if not dados:
        return JSONResponse({"erro": "PAJ não encontrado"}, status_code=404)
    # Remove prompt_max do JSON (muito grande) — tem endpoint proprio
    dados_resumo = {k: v for k, v in dados.items() if k != "prompt_max"}
    return dados_resumo


@router.get("/api/paj/{paj_norm}/movimentacoes", response_class=JSONResponse)
async def api_paj_movimentacoes(paj_norm: PajNorm):
    """Movimentações NORMALIZADAS do PAJ (unificadas, com documentos) + anexos, para a
    tabela COMPARTILHADA de movimentações (aba do PAJ e modal da Retornos usam a mesma).
    Retorna 404 se o PAJ ainda não foi sincronizado (sem metadata)."""
    dados = ler_paj(paj_norm)
    if not dados:
        return JSONResponse({"ok": False, "erro": "PAJ não sincronizado"}, status_code=404)
    return {
        "ok": True,
        "movimentacoes": dados.get("movimentacoes", []),
        "anexos_sisdpu": dados.get("anexos_sisdpu", []),
        "max_seq": dados.get("max_seq_mov", 0),
    }


@router.post("/api/paj/{paj_norm}/pos-execucao-sisdpu", response_class=JSONResponse)
async def api_pos_execucao_sisdpu(paj_norm: PajNorm, request: Request):
    """Chamado pelo frontend logo após o console genérico "Ações no SISDPU"
    (ou o atalho "Concluir PAJ da Caixa de Entrada" do cabeçalho) terminar de
    executar NA PÁGINA DO PAJ: registra no Histórico do painel e marca o PAJ
    como fora da caixa quando o trâmite foi concluído — mesmo efeito que
    `retornos_service.registrar_pos_execucao` tem pra Retornos. Body:
    {"resultado": {...payload do onExecutado...}}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — corpo vazio/malformado: nada a registrar
        body = {}
    resultado = body.get("resultado") or {}
    return JSONResponse(await registrar_pos_execucao_sisdpu(paj_norm, resultado))


@router.post("/api/paj/{paj_norm}/concluir-tramites", response_class=JSONResponse)
async def api_concluir_tramites(paj_norm: PajNorm, payload: dict = Body(...)):
    """Conclui um ou mais trâmites específicos da caixa de entrada deste PAJ
    (id_tramite individualizado — permite escolher exatamente quais, quando
    há mais de um pendente). Body: {"id_tramites": ["123", "456"]}."""
    ids = payload.get("id_tramites") if isinstance(payload, dict) else None
    resultado = await concluir_tramites_caixa(paj_norm, ids if isinstance(ids, list) else [])
    if not resultado.get("ok") and not resultado.get("concluidos"):
        return JSONResponse(resultado, status_code=400)
    return resultado


@router.post("/api/paj/{paj_norm}/atualizar-tramitacoes-caixa", response_class=JSONResponse)
async def api_atualizar_tramitacoes_caixa(paj_norm: PajNorm):
    """Atualiza `tramitacoes_caixa` deste PAJ sob demanda, lendo a caixa de
    entrada agora (não depende da sync completa da caixa ter rodado
    recentemente). Chamado pelo botão "Concluir PAJ da Caixa de Entrada"
    ANTES de decidir entre o caminho simples e o modal de escolha múltipla —
    ver `services.sync_service.atualizar_tramitacoes_paj` pro motivo."""
    from services.sync_service import atualizar_tramitacoes_paj
    return JSONResponse(await atualizar_tramitacoes_paj(paj_norm))


@router.get("/api/paj/{paj_norm}/tramitacoes", response_class=JSONResponse)
async def api_paj_tramitacoes(paj_norm: PajNorm, limite: int = 10):
    """Caixas onde o PAJ está aberto agora (mesma fonte usada pela Retornos —
    services/retornos_service.caixas_abertas) + últimas `limite` tramitações
    (histórico completo, concluídas ou não) — numa ÚNICA navegação ao SISDPU."""
    dados = ler_paj(paj_norm)
    id_processo = (dados or {}).get("id_processo") or ""
    if not id_processo:
        return {"ok": False, "abertas": [], "tramitacoes": [], "erro": "id_processo ausente (sincronize)."}
    # `ok=False` aqui significa "não foi possível confirmar" (conflito de sessão,
    # timeout...) — NUNCA interpretar abertas/tramitacoes vazias como "sem
    # tramitação" nesse caso (ver docstring de caixas_e_historico_paj).
    from ingestao import sisdpu2_fonte, sisdpu2_mcp_client
    if sisdpu2_fonte.fonte_mcp_ativa() and sisdpu2_mcp_client.conectado():
        return await sisdpu2_fonte.caixas_e_historico_paj(id_processo, limite)
    return await sisdpu_movtram.caixas_e_historico_paj(id_processo, limite)


@router.get("/api/paj/{paj_norm}/limpar-anexos/preview", response_class=JSONResponse)
async def limpar_anexos_preview(paj_norm: PajNorm):
    """Dry-run: lista o que seria apagado e mostra bloqueios de seguranca."""
    resultado = limpar_anexos_paj(paj_norm, dry_run=True)
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=404)
    return resultado


@router.post("/api/paj/{paj_norm}/limpar-anexos", response_class=JSONResponse)
async def limpar_anexos_executar(paj_norm: PajNorm, forcar: bool = False):
    """Executa a limpeza. Se forcar=True, ignora os bloqueios de seguranca."""
    resultado = limpar_anexos_paj(paj_norm, dry_run=False, forcar=forcar)
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=400)
    return resultado


@router.delete("/api/paj/{paj_norm}/arquivo", response_class=JSONResponse)
async def api_excluir_arquivo(paj_norm: PajNorm, caminho: str):
    """Exclui UM arquivo de peça/despacho da pasta do PAJ (ícone de lixeira
    das abas Outros > Peças/Despachos). `caminho` é relativo à pasta do PAJ
    (ex: 'peticao_inicial.docx' ou 'Peças IA/diagnostico.md') — vem direto do
    campo `caminho` que a própria listagem devolveu, então não precisa
    normalização no front."""
    resultado = excluir_arquivo_paj(paj_norm, caminho)
    if not resultado.get("ok"):
        status = 404 if "não encontrado" in resultado.get("erro", "") else 400
        return JSONResponse(resultado, status_code=status)
    return resultado


@router.get("/api/paj/{paj_norm}/sincronizacao-completa", response_class=JSONResponse)
async def api_sincronizacao_completa(paj_norm: PajNorm):
    """Usado pelo botao 'Baixar PAJ Completo' pra decidir, ANTES de gerar o
    PDF, se todos os documentos referenciados nas movimentacoes ja foram
    baixados localmente — evita gerar um 'completo' faltando anexo."""
    resultado = verificar_sincronizacao_completa(paj_norm)
    if resultado is None:
        return JSONResponse({"erro": "PAJ não encontrado"}, status_code=404)
    return resultado


@router.get("/api/paj/{paj_norm}/download-completo")
def api_download_completo(paj_norm: PajNorm):
    """Gera sob demanda (sem persistir em disco) um PDF unico com capa,
    historico de movimentacoes e todos os anexos do PAJ. Sincrono de proposito
    (roda em threadpool) — mesclar PDFs e' CPU-bound e nao deve travar o event
    loop."""
    pdf_bytes = gerar_pdf_consolidado(paj_norm)
    if pdf_bytes is None:
        return JSONResponse({"erro": "PAJ não encontrado"}, status_code=404)
    nome_arquivo = f"{paj_norm} - Download Completo.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


@router.post("/api/paj/{paj_norm}/melhorar-ocr", response_class=JSONResponse)
def api_melhorar_ocr(paj_norm: PajNorm):
    """Transcreve, via Sonnet (visao), os anexos do PAJ cujo OCR ficou fraco.

    Sincrono de proposito (FastAPI roda `def` em threadpool, sem travar o event
    loop): a transcricao pode demorar em autos grandes. Idempotente — pula o que
    ja foi transcrito. O gatilho automatico roda antes da elaboracao; este botao
    e' pra antecipar/forcar sob demanda. Import tardio evita ciclo no startup.
    """
    from services.ocr_llm_service import melhorar_ocr_paj

    return melhorar_ocr_paj(paj_norm)


@router.post("/api/paj/{paj_norm}/abrir-pasta", response_class=JSONResponse)
async def api_abrir_pasta(paj_norm: PajNorm):
    """Abre a pasta do PAJ no Explorer da maquina onde o painel roda (uso local)."""
    resultado = abrir_pasta_paj(paj_norm)
    if not resultado.get("ok"):
        return JSONResponse(resultado, status_code=404)
    return resultado


@router.post("/api/paj/{paj_norm}/whatsapp", response_class=JSONResponse)
async def api_definir_whatsapp(paj_norm: PajNorm, payload: dict = Body(...)):
    """Escolha manual de quais telefones (já extraídos do SISDPU) valem como
    WhatsApp — o SISDPU não tem esse dado. Body: {"numeros": ["(41)999...", ...]}."""
    numeros = payload.get("numeros") if isinstance(payload, dict) else None
    contatos = definir_whatsapp_numeros(paj_norm, numeros if isinstance(numeros, list) else [])
    if contatos is None:
        return JSONResponse({"ok": False, "erro": "PAJ não encontrado"}, status_code=404)
    return {"ok": True, "contatos": contatos}


# ---------------------------------------------------------------------------
# Lembretes — nos moldes da ferramenta "Lembretes" do e-proc (descrição + cor).
# Vide services/lembretes_service.py para o modelo de dados e a paleta.
# ---------------------------------------------------------------------------


@router.get("/api/paj/{paj_norm}/lembretes", response_class=JSONResponse)
async def api_lembretes_listar(paj_norm: PajNorm):
    return {"lembretes": lembretes_service.listar_lembretes(paj_norm)}


@router.post("/api/paj/{paj_norm}/lembretes", response_class=JSONResponse)
async def api_lembretes_criar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Cria um lembrete. Body: {"descricao": "...", "cor": "amarelo"}."""
    descricao = (payload.get("descricao") or "").strip() if isinstance(payload, dict) else ""
    cor = (payload.get("cor") or "").strip() if isinstance(payload, dict) else ""
    if not descricao:
        return JSONResponse({"ok": False, "erro": "Descrição é obrigatória"}, status_code=400)
    lembrete = lembretes_service.criar_lembrete(paj_norm, descricao, cor)
    if lembrete is None:
        return JSONResponse({"ok": False, "erro": "PAJ inválido"}, status_code=400)
    invalidar_cache_listagem()  # dashboard precisa refletir o novo lembrete
    return {"ok": True, "lembrete": lembrete}


@router.put("/api/paj/{paj_norm}/lembretes/{lembrete_id}", response_class=JSONResponse)
async def api_lembretes_atualizar(paj_norm: PajNorm, lembrete_id: str, payload: dict = Body(...)):
    """Altera um lembrete existente. Body: {"descricao": "...", "cor": "azul"}."""
    descricao = (payload.get("descricao") or "").strip() if isinstance(payload, dict) else ""
    cor = (payload.get("cor") or "").strip() if isinstance(payload, dict) else ""
    if not descricao:
        return JSONResponse({"ok": False, "erro": "Descrição é obrigatória"}, status_code=400)
    lembrete = lembretes_service.atualizar_lembrete(paj_norm, lembrete_id, descricao, cor)
    if lembrete is None:
        return JSONResponse({"ok": False, "erro": "Lembrete não encontrado"}, status_code=404)
    invalidar_cache_listagem()
    return {"ok": True, "lembrete": lembrete}


@router.delete("/api/paj/{paj_norm}/lembretes/{lembrete_id}", response_class=JSONResponse)
async def api_lembretes_remover(paj_norm: PajNorm, lembrete_id: str):
    ok = lembretes_service.remover_lembrete(paj_norm, lembrete_id)
    if not ok:
        return JSONResponse({"ok": False, "erro": "Lembrete não encontrado"}, status_code=404)
    invalidar_cache_listagem()
    return {"ok": True}
