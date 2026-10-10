"""Leitura AO VIVO pelo MCP do SIS-DPU 2.0 (etapa 2) — econômica, sem botão, sem widget.

Com a fonte de dados em "mcp" (Configurações › SISDPU › Fonte de dados) e o
painel conectado:

  - CAIXA (dashboard aberto e a cada INTERVALO_CAIXA_SEG): conferência barata
    (1 registro: total + trâmite mais recente). Só se mudou relê a lista e,
    para os PAJs com trâmite novo, a CAPA SEM HISTÓRICO (pretensão, assistido,
    decurso…) — ~1-4 registros por PAJ. PAJ que ninguém abre não custa mais.
  - PAJ ABERTO: 1ª vez, leitura completa (histórico + anexos com clipe); depois
    só as movimentações novas e os anexos delas + processos/contatos (~5-10
    registros). A página recarrega só se algo mudou.

Por que tão econômico: o servidor tem teto de 5000 REGISTROS lidos por pessoa
por dia (painel + uso do MCP pelo Claude somados) — descoberto em 10/10/2026,
quando as sincronizações de teste com histórico completo da caixa o estouraram.
O medidor (sisdpu2_mcp_client.consumo_hoje) freia o automático em
LIMITE_AUTOMATICO do teto e suspende tudo quando o servidor recusa.

Usa o MESMO lock da sincronização manual: com uma rodando, a leitura ao vivo é
pulada. O download dos PDFs NÃO entra aqui (MCP só entrega texto).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime

from config import PAJS_DIR
from ingestao import sincronizador
from ingestao import sisdpu2_fonte as fonte
from ingestao import sisdpu2_mcp_client as sis2
from services import config_painel_service, sync_service
from services.paj_service import invalidar_cache_listagem

log = logging.getLogger("ao_vivo")

MIN_INTERVALO_PAJ_SEG = 60       # reabrir o mesmo PAJ em menos de 1 min não relê
MIN_INTERVALO_CAIXA_SEG = 60     # dashboard aberto várias vezes seguidas
INTERVALO_CAIXA_SEG = 600        # conferência automática da caixa em segundo plano
LIMITE_AUTOMATICO = 0.70         # acima disso do teto diário, só leitura ao abrir PAJ
LIMITE_ABERTURA = 0.95           # acima disso, nada (reserva para o uso pelo chat)

_ultimo_paj: dict[str, float] = {}
_caixa: dict = {"ts": 0.0, "assinatura": None, "quando": None, "resultado": None}


def _fracao_consumida() -> float:
    c = sis2.consumo_hoje()
    if c["teto_atingido"]:
        return 1.0
    return c["registros"] / max(1, c["teto"])


def ativo() -> bool:
    return (config_painel_service.get_fonte_dados() == "mcp" and sis2.conectado()
            and not sis2.consumo_hoje()["teto_atingido"])


def _assinatura_paj(paj_norm: str) -> tuple:
    """O que, se mudar, justifica recarregar a página do PAJ."""
    try:
        m = json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ()
    movs = (m.get("detalhes_sisdpu") or {}).get("movimentacoes") or []
    return (len(movs), sum(len(mv.get("documentos") or []) for mv in movs),
            m.get("pretensao"), len(m.get("telefones") or []),
            tuple(sorted(t.get("id_tramite", "") for t in m.get("tramitacoes_caixa") or [])))


async def _com_lock(coro_factory):
    """Roda sob o lock da sincronização; None se já houver uma rodando."""
    lock = sync_service._sync_lock
    if lock.locked():
        return None
    await lock.acquire()  # sem await entre o teste e a aquisição: atômico no loop
    try:
        return await coro_factory()
    finally:
        lock.release()
        invalidar_cache_listagem()


def _paj_de(paj_norm: str) -> str:
    a, u, n = paj_norm.removeprefix("PAJ-").split("-")  # AAAA-UUU-NNNNN
    return f"{a}/{u}-{n}"


async def atualizar_paj(paj_norm: str) -> dict:
    if not ativo():
        return {"ok": False, "motivo": "inativo"}
    if _fracao_consumida() >= LIMITE_ABERTURA:
        return {"ok": False, "motivo": "teto_diario_quase_atingido"}
    if time.monotonic() - _ultimo_paj.get(paj_norm, 0) < MIN_INTERVALO_PAJ_SEG:
        return {"ok": True, "mudou": False, "motivo": "recente"}
    antes = _assinatura_paj(paj_norm)
    reg_antes = sis2.consumo_hoje()["registros"]
    linhas: list[str] = []

    async def _rodar():
        t1 = fonte.MODO.set("paj")
        t2 = fonte.CAIXA_EM_CACHE_SERVE.set(True)
        try:
            # Busca global: lê o PAJ esteja ou não na caixa, sem reler a caixa
            # (em_caixa_atual fica como a última conferência da caixa deixou).
            return await sincronizador.rodar_paj_via_busca_global(
                _paj_de(paj_norm), linhas.append, deve_cancelar=lambda: False,
                baixar_anexos=False, forcar_sessao=False, extrair_documentos=True,
                esperas_curtas=True, fora_da_caixa_confirmado=False)
        finally:
            fonte.CAIXA_EM_CACHE_SERVE.reset(t2)
            fonte.MODO.reset(t1)

    try:
        res = await _com_lock(_rodar)
    except Exception as e:  # noqa: BLE001
        log.warning("ao vivo PAJ %s falhou: %s", paj_norm, e)
        return {"ok": False, "motivo": f"{type(e).__name__}: {e}"}
    if res is None:
        return {"ok": False, "motivo": "sincronizacao_em_andamento"}
    erros = (res or {}).get("erros") or []
    if erros:
        return {"ok": False, "motivo": str(erros[0])[:300]}
    _ultimo_paj[paj_norm] = time.monotonic()
    depois = _assinatura_paj(paj_norm)
    return {"ok": True, "mudou": antes != depois, "movimentacoes": depois[:1],
            "registros_gastos": sis2.consumo_hoje()["registros"] - reg_antes}


async def atualizar_caixa(forcar: bool = False, automatico: bool = False) -> dict:
    if not ativo():
        return {"ok": False, "motivo": "inativo"}
    if automatico and _fracao_consumida() >= LIMITE_AUTOMATICO:
        return {"ok": False, "motivo": "automatico_pausado_pelo_consumo"}
    if _fracao_consumida() >= LIMITE_ABERTURA:
        return {"ok": False, "motivo": "teto_diario_quase_atingido"}
    if not forcar and time.monotonic() - _caixa["ts"] < MIN_INTERVALO_CAIXA_SEG:
        return {"ok": True, "mudou": False, "motivo": "recente", "quando": _caixa["quando"]}
    reg_antes = sis2.consumo_hoje()["registros"]
    try:
        assinatura = await asyncio.to_thread(fonte.assinatura_caixa)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "motivo": f"{type(e).__name__}: {e}"}
    _caixa["ts"] = time.monotonic()
    if assinatura == _caixa["assinatura"] and not forcar:
        _caixa["quando"] = datetime.now().isoformat(timespec="seconds")
        return {"ok": True, "mudou": False, "quando": _caixa["quando"],
                "registros_gastos": sis2.consumo_hoje()["registros"] - reg_antes}

    linhas: list[str] = []

    async def _rodar():
        fonte.invalidar_cache_caixa()
        t = fonte.MODO.set("caixa")
        try:
            # rodar() já PULA os PAJs sem trâmite novo; os demais leem só a capa.
            return await sincronizador.rodar(linhas.append, deve_cancelar=lambda: False,
                                             baixar_anexos=False, rodar_ocr=False,
                                             extrair_documentos=False)
        finally:
            fonte.MODO.reset(t)

    try:
        res = await _com_lock(_rodar)
    except Exception as e:  # noqa: BLE001
        log.warning("ao vivo caixa falhou: %s", e)
        return {"ok": False, "motivo": f"{type(e).__name__}: {e}"}
    if res is None:
        return {"ok": False, "motivo": "sincronizacao_em_andamento"}
    erros = (res or {}).get("erros") or []
    if not erros:
        _caixa["assinatura"] = assinatura  # com erro, a próxima conferência tenta de novo
    _caixa["quando"] = datetime.now().isoformat(timespec="seconds")
    _caixa["resultado"] = {k: res.get(k) for k in ("total_caixa", "processados", "pulados", "concluidos")}
    return {"ok": not erros, "mudou": True, "quando": _caixa["quando"], "resultado": _caixa["resultado"],
            "erros": erros[:5], "registros_gastos": sis2.consumo_hoje()["registros"] - reg_antes}


def estado() -> dict:
    c = sis2.consumo_hoje()
    return {"ativo": ativo(), "fonte": config_painel_service.get_fonte_dados(),
            "conectado": sis2.conectado(), "caixa_conferida_em": _caixa["quando"],
            "intervalo_caixa_min": INTERVALO_CAIXA_SEG // 60, "consumo_hoje": c,
            "automatico_pausado": _fracao_consumida() >= LIMITE_AUTOMATICO,
            "limite_automatico": LIMITE_AUTOMATICO}


async def laco_caixa() -> None:
    """Tarefa de fundo (startup do app): confere a caixa a cada INTERVALO_CAIXA_SEG
    enquanto a fonte for MCP, houver sessão e o consumo estiver abaixo de
    LIMITE_AUTOMATICO. Sessão vencida (401) apaga o token e o laço só observa."""
    await asyncio.sleep(30)
    while True:
        with contextlib.suppress(Exception):
            if ativo():
                await atualizar_caixa(automatico=True)
        await asyncio.sleep(INTERVALO_CAIXA_SEG)
