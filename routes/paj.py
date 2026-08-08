"""Rota de detalhe do PAJ."""

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from services import lembretes_service
from services.download_completo_service import gerar_pdf_consolidado, verificar_sincronizacao_completa
from services.paj_service import (
    PajNorm,
    abrir_pasta_paj,
    invalidar_cache_listagem,
    ler_paj,
    ler_notas,
    limpar_anexos_paj,
    salvar_notas,
)

router = APIRouter()


@router.get("/paj/{paj_norm}", response_class=HTMLResponse)
async def paj_detail(request: Request, paj_norm: PajNorm):
    dados = ler_paj(paj_norm)
    if not dados:
        return HTMLResponse("<h1>PAJ não encontrado</h1>", status_code=404)
    template = request.app.state.jinja.get_template("paj_detail.html")
    html = template.render(request=request, dados=dados, paj_norm=paj_norm)
    return HTMLResponse(html)


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


@router.get("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_ler(paj_norm: PajNorm):
    """Le NOTAS.md do PAJ (string vazia se nao existe)."""
    return {"paj_norm": paj_norm, "texto": ler_notas(paj_norm)}


@router.post("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_salvar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Salva NOTAS.md do PAJ. Body: {"texto": "..."}."""
    texto = payload.get("texto", "") if isinstance(payload, dict) else ""
    ok = salvar_notas(paj_norm, texto)
    if not ok:
        return JSONResponse({"ok": False, "erro": "PAJ inválido"}, status_code=400)
    return {"ok": True}


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
