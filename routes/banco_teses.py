"""Página "Banco de teses" (19/10/2026) — ver services/banco_teses_service.py."""

import asyncio

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from services import banco_teses_service as bt

router = APIRouter()


@router.get("/banco-teses", response_class=HTMLResponse)
async def pagina_banco_teses(request: Request):
    template = request.app.state.jinja.get_template("banco_teses.html")
    return HTMLResponse(template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {})))


@router.get("/api/banco-teses", response_class=JSONResponse)
async def api_listar():
    return {"ok": True, "teses": bt.listar(), "areas": bt.AREAS}


@router.post("/api/banco-teses", response_class=JSONResponse)
async def api_salvar(request: Request):
    try:
        tese = bt.salvar(await request.json())
    except ValueError as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    return {"ok": True, "tese": tese}


@router.post("/api/banco-teses/excluir-varias", response_class=JSONResponse)
async def api_excluir_varias(request: Request):
    """Body: {"ids": [...]} — as teses listadas na página (com os filtros)."""
    body = await request.json()
    return {"ok": True, "excluidas": bt.excluir_varios(body.get("ids") or [])}


@router.delete("/api/banco-teses/{tese_id}", response_class=JSONResponse)
async def api_excluir(tese_id: str):
    return {"ok": bt.excluir(tese_id)}


@router.post("/api/banco-teses/importar-teses-dpu", response_class=JSONResponse)
async def api_importar():
    return {"ok": True, **(await asyncio.to_thread(bt.importar_teses_dpu))}


@router.post("/api/banco-teses/ler-arquivo", response_class=JSONResponse)
async def api_ler_arquivo(arquivo: UploadFile = File(...)):
    """Importação individual: lê o .docx/.pdf e devolve {titulo, texto_html} para
    o formulário de nova tese (nada é gravado até o Defensor salvar)."""
    conteudo = await arquivo.read()
    try:
        lido = await asyncio.to_thread(bt.ler_arquivo, arquivo.filename or "", conteudo)
    except ValueError as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": f"Não foi possível ler o arquivo ({type(e).__name__})."},
                            status_code=400)
    return {"ok": True, **lido, "origem": f"arquivo:{arquivo.filename or ''}"}


@router.post("/api/banco-teses/importar-arquivos", response_class=JSONResponse)
async def api_importar_arquivos(arquivos: list[UploadFile] = File(...)):
    """Importação em bloco (arquivos de uma pasta): cada .docx/.pdf vira uma tese
    INATIVA para revisar e ativar."""
    lidos = [(a.filename or "", await a.read()) for a in arquivos]
    return {"ok": True, **(await asyncio.to_thread(bt.importar_arquivos, lidos))}
