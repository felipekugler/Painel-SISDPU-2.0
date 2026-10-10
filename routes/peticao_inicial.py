"""Rotas do botão "Preparar Petição Inicial no eproc" (fluxo de benefício por
incapacidade) — ver services.peticao_inicial_service (plano, só leitura) e
ingestao.eproc_peticao_inicial (automação do e-proc).

Pós-ajuizamento (gerenciamento + movimentação "Petição inicial" no SISDPU) NÃO
mora aqui: o front reaproveita os módulos já existentes
(/api/acoes-eproc/gerenciar e o console "Ações no SISDPU" da página do PAJ)."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ingestao import eproc_peticao_inicial
from services import peticao_inicial_service
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/peticao-inicial/plano/{paj_norm}")
async def plano(paj_norm: PajNorm):
    res = peticao_inicial_service.montar_plano(paj_norm)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/peticao-inicial/preparar/{paj_norm}")
async def preparar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: {"arquivos": [{"caminho": "Ajuizamento/x.pdf", "tipo": "..."}],
    "dados": {...dados do plano, ajustados no modal}}. Para antes de Finalizar."""
    if peticao_inicial_service.inicial_ajuizada(paj_norm) and not payload.get("novo_ajuizamento"):
        return JSONResponse({"ok": False, "erro": "A petição inicial deste PAJ já foi ajuizada — marque \"Trata-se de novo ajuizamento\" para prosseguir."},
                            status_code=400)
    arquivos = payload.get("arquivos") or []
    dados = payload.get("dados") or {}
    if not arquivos:
        return JSONResponse({"ok": False, "erro": "Nenhum arquivo selecionado."}, status_code=400)
    faltando = [c for c in ("valor_causa", "nb") if not str(dados.get(c) or "").strip()]
    if not (dados.get("autor") or {}).get("cpf"):
        faltando.append("CPF do autor")
    if not dados.get("especialidades"):
        faltando.append("especialidade da perícia")
    if faltando:
        return JSONResponse({"ok": False, "erro": "Campos obrigatórios vazios: " + ", ".join(faltando) + "."},
                            status_code=400)
    try:
        docs = eproc_peticao_inicial.resolver_documentos(paj_norm, arquivos)
    except ValueError as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    res = await eproc_peticao_inicial.preparar({"documentos": docs, "dados": dados})
    return JSONResponse(res)


@router.post("/api/peticao-inicial/ajuizar/{paj_norm}")
async def ajuizar(paj_norm: PajNorm, payload: dict = Body(default={})):
    """DISTRIBUI o processo (Finalizar + Confirmar ajuizamento) — irreversível;
    o front só chama depois do clique explícito em "Prosseguir com ajuizamento"."""
    if peticao_inicial_service.inicial_ajuizada(paj_norm) and not payload.get("novo_ajuizamento"):
        return JSONResponse({"ok": False, "erro": "A petição inicial deste PAJ já consta como ajuizada e o novo ajuizamento não foi confirmado — nada foi feito."})
    res = await eproc_peticao_inicial.ajuizar()
    if res.get("ok"):
        try:
            peticao_inicial_service.registrar_ajuizamento(paj_norm, res)
        except Exception:  # noqa: BLE001 — marcador é conveniência; o ajuizamento já ocorreu
            pass
        from services import historico_painel
        historico_painel.registrar({
            "paj": payload.get("paj_label", ""), "paj_norm": paj_norm,
            "origem": "eproc", "atividade": "Ajuizamento de petição inicial (e-proc)",
            "movimentacao": res.get("classe") or "Petição inicial", "tramitado_para": "",
            "concluido": False,
            "detalhe": f"Processo {res.get('numero')} — chave {res.get('chave')}"[:300],
        })
    return JSONResponse(res)
