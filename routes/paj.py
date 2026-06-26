"""Rota de detalhe do PAJ."""

import os
import subprocess

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import PAJS_DIR
from services.paj_service import (
    PajNorm,
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


@router.get("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_ler(paj_norm: PajNorm):
    """Le NOTAS.md do PAJ (string vazia se nao existe)."""
    return {"paj_norm": paj_norm, "texto": ler_notas(paj_norm)}


@router.post("/api/paj/{paj_norm}/abrir-pasta", response_class=JSONResponse)
async def abrir_pasta_paj(paj_norm: PajNorm):
    """Abre a pasta do PAJ no Explorer."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return JSONResponse({"ok": False, "erro": f"Pasta não encontrada: {pasta}"}, status_code=404)
    if os.name == "nt":
        os.startfile(str(pasta))
    else:
        subprocess.Popen(["xdg-open", str(pasta)])
    return {"ok": True}


@router.post("/api/paj/{paj_norm}/notas", response_class=JSONResponse)
async def api_notas_salvar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Salva NOTAS.md do PAJ. Body: {"texto": "..."}."""
    texto = payload.get("texto", "") if isinstance(payload, dict) else ""
    ok = salvar_notas(paj_norm, texto)
    if not ok:
        return JSONResponse({"ok": False, "erro": "PAJ inválido"}, status_code=400)
    return {"ok": True}
