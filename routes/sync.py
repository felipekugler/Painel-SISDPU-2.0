"""Rota de sincronizacao da caixa SISDPU via SSE.

MODELO (desde 2026-08-28): as rotas de sincronizacao de longa duracao (as que
antes abriam a conexao SSE diretamente pra INICIAR o trabalho) agora fazem
duas coisas separadas:

1. `POST /sync/...` inicia a sincronizacao em segundo plano (retorna na hora,
   `{"ok": true}` ou erro 409 se ja houver uma rodando/faltar credenciais) —
   ver `services.sync_service.iniciar_em_segundo_plano`.
2. `GET /sync/stream` observa o log da sincronizacao ATUAL (a que estiver
   rodando, de qualquer origem) — pode ser aberto, fechado e reaberto de
   QUALQUER pagina do painel sem afetar a sincronizacao em si.

Isso substitui o modelo antigo, onde abrir o EventSource ERA o que iniciava
o trabalho — fechar a conexao (trocar de pagina, fechar a aba) cancelava a
sincronizacao no meio. Ver [[painel-sisdpu-sync-flutuante]] se essa memoria
existir, ou o changelog do painel.
"""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import sync_service
from services.sync_service import (
    atualizar_caixa,
    esta_rodando,
    solicitar_cancelamento,
    status_atual,
)

router = APIRouter(prefix="/api")


async def _iniciar_ou_erro(coro) -> JSONResponse:
    """Roda uma chamada `sync_service.rodar_sync_*(...)` (que so' DISPARA a
    sincronizacao e retorna na hora) e traduz falha de inicio (ja tem uma
    rodando, ou faltam credenciais) em erro HTTP 409 — nunca chega a criar
    sessao nenhuma nesse caso, entao nao ha nada pra `/sync/stream` observar."""
    try:
        await coro
    except RuntimeError as e:
        return JSONResponse({"ok": False, "mensagem": str(e)}, status_code=409)
    return JSONResponse({"ok": True})


async def _stream_acompanhar():
    """Observador GENÉRICO — funciona pra QUALQUER sincronizacao iniciada via
    `sync_service.iniciar_em_segundo_plano`, independente de qual rota a
    disparou. Repete o log acumulado (reconectando de outra pagina, por
    exemplo) e segue em tempo real; ao terminar, emite `done` com o
    resultado final."""
    async for linha in sync_service.acompanhar():
        yield {"event": "log", "data": linha.rstrip("\n")}
    status = status_atual()
    yield {
        "event": "done",
        "data": status.get("titulo") or "Sincronização finalizada",
    }


@router.get("/sync/stream")
async def sync_stream():
    """Observa o log da sincronizacao ATUALMENTE em andamento (ou a mais
    recente, se ja terminou) — chamado por QUALQUER pagina do painel ao
    carregar, se `sync_service.status_atual()` indicar que ha algo rodando
    (widget flutuante reconecta sozinho). Fechar esta conexao NAO cancela
    nada."""
    return EventSourceResponse(_stream_acompanhar())


@router.get("/sync/status")
async def sync_status():
    """Snapshot: {rodando, titulo, meta, log, done, ok}. Usado pelo
    front-end ao carregar QUALQUER pagina pra decidir se mostra o widget
    flutuante ja aberto (uma sincronizacao sobreviveu a uma navegacao)."""
    return JSONResponse(status_atual())


@router.post("/sync")
async def sync(anexos: int = 1, documentos: int = -1, ocr: int = 0):
    """Inicia a sincronizacao da caixa SISDPU em segundo plano.

    ?anexos=0 => nao baixa/OCR os anexos.
    ?documentos=0/1 => forca investigar (ou nao) a relacao de documentos por
    seq, independente de `anexos` — permite o nivel LEVE (documentos=1 com
    anexos=0). Omitido (-1, padrao) => segue `anexos` (LEVISSIMA se
    anexos=0, COMPLETA se anexos=1).
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado —
    so baixa; use "Melhorar OCR" na aba Anexos SISDPU pra texto de qualidade).
    """
    extrair_documentos = None if documentos == -1 else bool(documentos)
    return await _iniciar_ou_erro(
        sync_service.rodar_sync(
            baixar_anexos=bool(anexos),
            rodar_ocr=bool(ocr),
            extrair_documentos=extrair_documentos,
        )
    )


@router.post("/sync/paj/{paj_norm}")
async def sync_paj(paj_norm: str, anexos: int = 1, documentos: int = -1, ocr: int = 0):
    """Inicia a sincronizacao de apenas UM PAJ em segundo plano (uso de
    teste / refresh pontual).

    `paj_norm` aceita 'PAJ-2021-044-00635' ou '2021/044-00635' (URL-encoded).
    ?anexos=0 => nao baixa/OCR os anexos.
    ?documentos=0/1 => forca investigar (ou nao) a relacao de documentos por
    seq, independente de `anexos` — mesmas 3 hipoteses (levissima/leve/
    completa) do botao "Sincronizar caixa" do dashboard. Omitido (-1,
    padrao) => segue `anexos`.
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado).
    """
    extrair_documentos = None if documentos == -1 else bool(documentos)
    return await _iniciar_ou_erro(
        sync_service.rodar_sync_paj(
            paj_norm,
            baixar_anexos=bool(anexos),
            rodar_ocr=bool(ocr),
            extrair_documentos=extrair_documentos,
        )
    )


@router.post("/sync/paj/{paj_norm}/movimentacoes")
async def sync_paj_movimentacoes(paj_norm: str):
    """Inicia, em segundo plano, a sincronização EXCLUSIVA e INCREMENTAL das
    movimentações de um PAJ — sem baixar anexos (PDFs). Disparada pelo ícone
    de atualizar da tabela de movimentações (aba do PAJ ou modal da
    Retornos). Bem mais rápida que /api/sync/paj/{paj_norm}: relê a tabela
    de movimentações inteira (o SISDPU não expõe delta), mas só extrai os
    documentos anexados das movimentações que ainda não eram conhecidas —
    as demais mantêm os documentos já indexados em syncs anteriores, sem
    reabrir popup nenhum.

    `paj_norm` aceita 'PAJ-2021-044-00635' ou '2021/044-00635' (URL-encoded).
    """
    return await _iniciar_ou_erro(sync_service.rodar_sync_movimentacoes(paj_norm))


@router.post("/sync/paj/{paj_norm}/anexos-desde")
async def sync_anexos_desde(paj_norm: str, data: str = "", ocr: int = 0):
    """Inicia, em segundo plano, o download de anexos de um PAJ a partir de
    uma data de corte (inclusive).

    Usado quando o sync padrao detecta overflow (PAJ tem >MAX_ANEXOS_POR_PAJ
    anexos) e o usuario quer completar a partir de uma data especifica.

    Query: ?data=YYYY-MM-DD (obrigatorio). ?ocr=1 roda o OCR simples (padrao: desabilitado).
    """
    try:
        data_inicio = _dt.date.fromisoformat(data)
    except (ValueError, TypeError):
        return JSONResponse(
            {"erro": "parametro 'data' invalido — use formato YYYY-MM-DD"},
            status_code=400,
        )
    return await _iniciar_ou_erro(
        sync_service.rodar_sync_anexos_desde_data(paj_norm, data_inicio, rodar_ocr=bool(ocr))
    )


@router.post("/sync/watchlist")
async def sync_watchlist(anexos: int = 1, ocr: int = 0):
    """Inicia, em segundo plano, a sincronização de todos os PAJs ativos da
    watchlist via busca global do SIS-DPU.

    Entra pela "Pesquisa Rapida" do header — funciona mesmo se o PAJ ja
    saiu da caixa de entrada (concluido).

    ?anexos=0 => modo rapido (pula download/OCR dos anexos).
    ?ocr=1 => roda o OCR simples nos PDFs baixados (padrao: desabilitado).
    """
    return await _iniciar_ou_erro(
        sync_service.rodar_sync_watchlist(baixar_anexos=bool(anexos), rodar_ocr=bool(ocr))
    )


@router.post("/sync/prazos-abertos")
async def sync_prazos_abertos():
    """Inicia, em segundo plano, a sincronização LEVE (sem anexos) de todos
    os PAJs com prazo em aberto nas intimações do e-proc (aba Prazos) —
    disparada em sequência pelo front-end logo após "Sincronizar caixa"
    (postura proativa, não precisa abrir cada PAJ manualmente).

    Fonte: última sincronização de intimações já salva (não força releitura
    do e-proc — isso é papel do botão "Sincronizar intimações").
    """
    return await _iniciar_ou_erro(sync_service.rodar_sync_prazos_abertos())


@router.post("/sync/paj/{paj_norm}/baixar-documentos")
async def sync_baixar_documentos(paj_norm: str, ids: str = ""):
    """Inicia, em segundo plano, o download de documentos ESPECIFICOS (por
    ID) de um PAJ ja sincronizado.

    Nao ressincroniza nada — le as URLs direto do metadata.json existente
    (populado pela extracao por seq de uma sincronizacao anterior). Usado
    pelos botoes "Baixar todos"/"Baixar selecionados" da coluna Documentos.

    Query: ?ids=41893728,41893730 (obrigatorio, separados por virgula — o
    numero na URL de download do SISDPU, igual ao usado no nome do arquivo
    local "seqNNN - ID - nome").
    """
    lista_ids = [i.strip() for i in ids.split(",") if i.strip()]
    if not lista_ids:
        return JSONResponse({"erro": "parametro 'ids' obrigatorio"}, status_code=400)
    return await _iniciar_ou_erro(sync_service.rodar_sync_baixar_documentos(paj_norm, lista_ids))


@router.post("/sync/paj/{paj_norm}/seq/{seq}/documentos")
async def sync_documentos_seq(paj_norm: str, seq: str):
    """Inicia, em segundo plano, a extração da relação de documentos de UM
    seq específico (sem baixar nada) — minimenu do clipe de anexo na coluna
    "Documentos" (ver movimentacoes_tabela.html), quando o ícone
    (`tem_anexo_icone`) sinaliza que aquele seq pode ter documento mas a
    relação ainda não foi obtida. Bem mais barato que "Atualizar
    movimentações" (que reprocessaria toda seq nova, não só a pedida)."""
    return await _iniciar_ou_erro(sync_service.rodar_sync_documentos_seq(paj_norm, seq))


@router.post("/sync/paj/{paj_norm}/seq/{seq}/baixar-documentos")
async def sync_baixar_documentos_seq(paj_norm: str, seq: str):
    """Inicia, em segundo plano, o download dos documentos de UM seq
    específico — obtém a relação primeiro se ainda não for conhecida (mesmo
    passo da rota acima) e já encadeia para o download. Segunda opção do
    minimenu do clipe de anexo ("Baixar documentos")."""
    return await _iniciar_ou_erro(sync_service.rodar_sync_baixar_documentos_seq(paj_norm, seq))


@router.post("/sync/atualizar-caixa")
async def sync_atualizar_caixa():
    """Le a caixa de entrada (rapido — 1 pagina, sem abrir cada PAJ) e remove
    do dashboard os PAJs ja sincronizados que saíram dela.

    Diferente de /api/sync: NAO processa PAJs novos, so' reconcilia quem
    saiu. Usado pelo botao "Atualizar" do dashboard. Sem SSE, sem segundo
    plano — responde direto com o resultado (poucos segundos).
    """
    resultado = await atualizar_caixa()
    return JSONResponse(resultado)


@router.post("/sync/cancel")
async def sync_cancel():
    """Sinaliza o worker de sync atual para parar na proxima oportunidade.

    O sincronizador consulta o flag entre PAJs e aborta o laco de forma limpa
    (sem concluir PAJs por engano). Se nao houver sync rodando, retorna 404.
    """
    if not esta_rodando():
        return {"ok": False, "motivo": "nenhuma sync em andamento"}
    sinalizado = solicitar_cancelamento()
    return {"ok": sinalizado}
