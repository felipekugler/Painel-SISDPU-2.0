"""Rotas do módulo "Gerar Minuta" (aba Processo Judicial) — ver
`services.minuta_ia_service`. As duas abas do modal de seleção de documentos
reaproveitam endpoints que já existem (não duplicados aqui):
  - aba "Processo": GET /api/eproc-mcp/triagem (eventos do processo)
  - aba "PAJ": GET /api/paj/{paj_norm}/movimentacoes (anexos_sisdpu)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services import config_painel_service, minuta_ia_service

router = APIRouter()


@router.get("/api/minutas/tipos", response_class=JSONResponse)
async def api_minutas_tipos():
    """Catálogo de tipos de minuta pro dropdown "Gerar Minuta"."""
    return JSONResponse({"ok": True, "tipos": minuta_ia_service.listar_tipos()})


@router.get("/api/minutas/modelos-padrao", response_class=JSONResponse)
async def api_minutas_modelos_padrao():
    """Submenu "Petição padrão" do menu "Minutar Petição" — MESMO cadastro de
    "Modelos de petição" de Configurações › e-proc, sem duplicar nada."""
    return JSONResponse({"ok": True, "modelos": config_painel_service.get_eproc_modelos_peticao()})


@router.post("/api/minutas/usar-modelo/{paj_norm}", response_class=JSONResponse)
async def api_minutas_usar_modelo(paj_norm: str, request: Request):
    """"Petição padrão" — grava na caixa de edição o texto de um modelo já
    cadastrado (placeholders já resolvidos pelo front), sem chamar IA. Body:
    {peticao_html}."""
    body = await request.json()
    peticao_html = body.get("peticao_html") or ""
    res = minuta_ia_service.usar_modelo_padrao(paj_norm, peticao_html)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/minutas/gerar/{paj_norm}", response_class=JSONResponse)
async def api_minutas_gerar(paj_norm: str, request: Request):
    """Dispara a geração da minuta em background. Body:
    {processo, tipo_slug, documentos: [{origem:"processo", evento_numero} |
    {origem:"paj", caminho}], orientacao?}. `orientacao`: texto opcional da
    caixa "Orientação diretiva para análise da IA" (compartilhada com
    Analisar/Calcular Tempo de Contribuição/Calcular Qualidade de Segurado)."""
    body = await request.json()
    processo = (body.get("processo") or "").strip()
    tipo_slug = (body.get("tipo_slug") or "").strip()
    documentos = body.get("documentos") or []
    orientacao = (body.get("orientacao") or "").strip()
    res = minuta_ia_service.iniciar(paj_norm, processo, tipo_slug, documentos, orientacao)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/minutas/gerar/{paj_norm}/status", response_class=JSONResponse)
async def api_minutas_gerar_status(paj_norm: str):
    """Polling do status da geração em background (idle|running|done|error).
    Em "done", vem `peticao_html` — só o RASCUNHO da IA, ainda não salvo em
    arquivo nenhum (ver "Gerar arquivo (DOC/PDF)" abaixo)."""
    return JSONResponse(minuta_ia_service.status(paj_norm))


@router.post("/api/minutas/salvar-rascunho/{paj_norm}", response_class=JSONResponse)
async def api_minutas_salvar_rascunho(paj_norm: str, request: Request):
    """Botão "Gravar" da caixa de edição — persiste o texto ATUAL (sem gerar
    arquivo), pra não perder a edição manual num reload da página. Body:
    {peticao_html}."""
    body = await request.json()
    peticao_html = body.get("peticao_html") or ""
    res = minuta_ia_service.salvar_rascunho(paj_norm, peticao_html)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/minutas/formatar/{paj_norm}", response_class=JSONResponse)
async def api_minutas_formatar(paj_norm: str, request: Request):
    """"Gerar arquivo (DOC/PDF)" — formata o texto ATUAL da caixa (já revisado
    manualmente, ou o rascunho da IA sem alteração) no padrão DPU e salva em
    `Peças IA/`. Sem IA — síncrono, mas roda em thread (Word/COM bloqueia).
    Body: {tipo_slug, peticao_html}."""
    import asyncio

    body = await request.json()
    tipo_slug = (body.get("tipo_slug") or "").strip()
    peticao_html = body.get("peticao_html") or ""
    res = await asyncio.to_thread(minuta_ia_service.formatar_e_salvar, paj_norm, tipo_slug, peticao_html)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)
