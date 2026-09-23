"""Rotas do botão "Calcular Tempo de Contribuição" (ver
services.calcular_cnis_service)."""

import contextlib
import json

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from config import PAJS_DIR
from services import calcular_cnis_service
from services.paj_service import PajNorm

router = APIRouter()


@router.get("/api/calcular-cnis/arquivos/{paj_norm}")
async def calcular_cnis_arquivos(paj_norm: PajNorm):
    """Lista os arquivos de CNIS encontrados em Arquivos/ (nome contém
    'cnis') para o modal de seleção — pode haver mais de um, extraídos em
    datas diferentes."""
    return {"arquivos": calcular_cnis_service.listar_arquivos_cnis(paj_norm)}


@router.post("/api/calcular-cnis/iniciar/{paj_norm}")
async def calcular_cnis_iniciar(paj_norm: PajNorm, payload: dict = Body(...)):
    """Body: {"arquivo": "Arquivos/CNIS.pdf", "modo": "simples"|"completo",
    "sexo": "Masculino"|"Feminino"|"", "der": "DD/MM/AAAA"|"",
    "orientacao": ""} — arquivo e modo escolhidos pelo Defensor no modal
    (modo só é perguntado quando o arquivo detectado é um CNIS completo);
    sexo e DER são informados objetivamente pelo Defensor nos campos do
    modal, nunca pesquisados pela IA (pedido do usuário, 2026-09-20) —
    vazios significam "não informado". `orientacao` (opcional): mesmo texto
    da caixa "Orientação diretiva para análise da IA" do botão "Analisar"
    (ver paj_detail.html) — integrada aqui a pedido do usuário, 2026-09-21."""
    arquivo = (payload.get("arquivo") or "").strip()
    modo = (payload.get("modo") or "simples").strip()
    sexo = (payload.get("sexo") or "").strip()
    der = (payload.get("der") or "").strip()
    orientacao = (payload.get("orientacao") or "").strip()
    result = calcular_cnis_service.iniciar(paj_norm, arquivo, modo, sexo, der, orientacao)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@router.get("/api/calcular-cnis/status/{paj_norm}")
async def calcular_cnis_status(paj_norm: PajNorm):
    return calcular_cnis_service.get_status(paj_norm)


async def _stream_baixar_sat(paj_norm: str):
    """Baixa SÓ "CNIS - Vínculos" e "CNIS - Remunerações" (keys do catálogo
    em `ingestao.sat_client.TIPOS_DOCUMENTOS`) direto para `Arquivos/` deste
    PAJ — não para a subpasta "Arquivos SAT" da feature de juntada (aqui não
    disparamos movimentação/tramitação, só queremos os 2 PDFs mais atuais
    disponíveis pro cálculo). Mesmo motor da aba "Arquivos SAT"
    (`sat_client.baixar_cidadao_stream`, ver routes/sat.py `_stream_baixar`
    para o precedente) — usuário resolve login/CAPTCHA na janela do SAT que
    `/api/sat/abrir` já abre (chamado pelo próprio `calcular_cnis.js` antes
    de abrir esta stream).

    Ao final (sucesso, erro ou CPF ausente), FECHA a janela/sessão do SAT
    (`sat_client.fechar_navegador()`) — pedido do usuário (2026-09-20): este
    botão é uma ação pontual e não deve deixar sessão órfã do SAT aberta.
    Diferente da aba "Arquivos SAT" (fluxo de várias etapas, onde o usuário
    pode querer manter a janela aberta entre downloads), aqui é sempre um
    disparo único — fechar sempre é seguro."""
    from ingestao import sat_client

    try:
        cpf, nome = calcular_cnis_service.dados_assistido(paj_norm)
        if not cpf:
            yield {
                "event": "log",
                "data": json.dumps(
                    {"level": "erro", "msg": f"CPF do assistido não encontrado no PAJ {paj_norm}."},
                    ensure_ascii=False,
                ),
            }
            yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": "CPF não encontrado"}, ensure_ascii=False)}
            return

        pasta = str(PAJS_DIR / paj_norm / "Arquivos")
        try:
            async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, tipos=["cnis_vinculos", "cnis_remuneracoes"]
            ):
                if ev.get("done"):
                    yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
                else:
                    yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
        except Exception as e:  # noqa: BLE001
            yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
            yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}
    finally:
        # NÃO fazer `yield` aqui: o cliente já fechou o EventSource ao
        # receber o 'done' emitido acima, e tentar mandar mais um evento
        # numa conexão já encerrada pode levantar dentro do próprio `yield`
        # (PEP 525) e abortar este `finally` ANTES de chegar no fechamento
        # do navegador — por isso só a chamada direta, sem stream.
        with contextlib.suppress(Exception):
            sat_client.fechar_navegador()


@router.get("/api/calcular-cnis/baixar-sat/{paj_norm}")
async def calcular_cnis_baixar_sat(paj_norm: PajNorm):
    """SSE: baixa o CNIS (Vínculos + Remunerações) mais atual do SAT direto
    para `Arquivos/` deste PAJ — botão "Baixar CNIS Atualizado do SAT" no
    modal de seleção."""
    return EventSourceResponse(_stream_baixar_sat(paj_norm))
