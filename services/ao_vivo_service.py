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
from pathlib import Path
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
# mudou_em: quando a caixa foi relida de fato (pela tela OU pelo laço de fundo) —
# a tela aberta compara com o último que viu e recarrega a lista se mudou.
_caixa: dict = {"ts": 0.0, "assinatura": None, "quando": None, "resultado": None, "mudou_em": None}

# A assinatura da caixa sobrevive ao reinício do painel: sem isso, cada reinício
# relia a caixa INTEIRA na 1ª conferência (11/10/2026: ~2.700 registros do dia
# só em caixa_entrada, boa parte por reinícios).
_ARQ_ASSINATURA = Path(__file__).resolve().parents[1].parent / "dados" / "_mcp_sisdpu2_caixa.json"


def _carregar_assinatura() -> None:
    try:
        d = json.loads(_ARQ_ASSINATURA.read_text(encoding="utf-8"))
        a = d.get("assinatura")
        _caixa["assinatura"] = (int(a[0]), str(a[1])) if a else None
        _caixa["quando"] = d.get("quando")
    except Exception:  # noqa: BLE001 — sem arquivo: 1ª conferência relê, como antes
        pass


def _salvar_assinatura() -> None:
    try:
        _ARQ_ASSINATURA.parent.mkdir(parents=True, exist_ok=True)
        _ARQ_ASSINATURA.write_text(json.dumps({"assinatura": list(_caixa["assinatura"] or []) or None,
                                               "quando": _caixa["quando"]}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("não gravou a assinatura da caixa: %s", e)


_carregar_assinatura()


# Decisão do Defensor (11/10/2026): o SERVIDOR decide quando parar — o painel
# não freia pela própria estimativa (que ainda está sendo calibrada contra a
# recusa real). Só a recusa do servidor (teto_atingido) suspende as leituras.
# True volta a frear pela estimativa (LIMITE_AUTOMATICO / LIMITE_ABERTURA).
FREIO_PELA_ESTIMATIVA = False


def _fracao_consumida() -> float:
    c = sis2.consumo_hoje()
    if c["teto_atingido"]:
        return 1.0
    if not FREIO_PELA_ESTIMATIVA:
        return 0.0
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
            tuple(sorted(t.get("id_tramite", "") for t in m.get("tramitacoes_caixa") or [])),
            m.get("em_caixa_atual"))


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


async def atualizar_paj(paj_norm: str, forcar: bool = False) -> dict:
    """`forcar`: releitura pedida logo depois de uma gravação (ou pelo ícone da
    tabela) — ignora o intervalo mínimo entre leituras do mesmo PAJ."""
    if not ativo():
        return {"ok": False, "motivo": "inativo"}
    if _fracao_consumida() >= LIMITE_ABERTURA:
        return {"ok": False, "motivo": "teto_diario_quase_atingido"}
    if not forcar and time.monotonic() - _ultimo_paj.get(paj_norm, 0) < MIN_INTERVALO_PAJ_SEG:
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


# ---------------------------------------------------------------------------
# Anexos do PAJ aberto (download pelo MCP, sem freio de consumo)
# ---------------------------------------------------------------------------

_downloads: dict[str, dict] = {}  # paj_norm -> {rodando, inicio, resultado}


def _anexos_faltando(paj_norm: str) -> int:
    """Quantos anexos com id direto ainda não estão na pasta Arquivos (local, sem MCP)."""
    import re
    pasta = PAJS_DIR / paj_norm
    try:
        m = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    existentes = {p.name for p in (pasta / "Arquivos").glob("*")} if (pasta / "Arquivos").exists() else set()
    ids_locais = {re.match(r"seq\d+ - (\d+) - ", n).group(1) for n in existentes if re.match(r"seq\d+ - (\d+) - ", n)}
    faltam = 0
    for mv in (m.get("detalhes_sisdpu") or {}).get("movimentacoes") or []:
        for d in mv.get("documentos") or []:
            achado = re.search(r"/pdf/(\d+)/", d.get("urlDownload") or "")
            if achado and achado.group(1) not in ids_locais:
                faltam += 1
    return faltam


async def baixar_anexos_paj(paj_norm: str) -> dict:
    """Dispara, em segundo plano, o download dos anexos que faltam do PAJ —
    o mesmo caminho da sincronização completa (nomes, limite de anexos por PAJ,
    contagem no metadata), lendo os arquivos pelo MCP. Sem freio de consumo:
    baixar o que vai usar é trabalho do Defensor (decisão de 11/10/2026); só
    para se o próprio servidor recusar."""
    if config_painel_service.get_fonte_dados() != "mcp" or not sis2.conectado():
        return {"ok": False, "motivo": "inativo"}
    atual = _downloads.get(paj_norm)
    if atual and atual.get("rodando"):
        return {"ok": True, "iniciado": False, "motivo": "em_andamento"}
    faltam = _anexos_faltando(paj_norm)
    if not faltam:
        return {"ok": True, "iniciado": False, "faltam": 0}
    _downloads[paj_norm] = {"rodando": True, "inicio": datetime.now().isoformat(timespec="seconds"),
                            "faltam": faltam, "resultado": None}

    async def _tarefa():
        linhas: list[str] = []

        async def _rodar():
            t1 = fonte.MODO.set("paj")
            t2 = fonte.CAIXA_EM_CACHE_SERVE.set(True)
            try:
                return await sincronizador.rodar_paj_via_busca_global(
                    _paj_de(paj_norm), linhas.append, deve_cancelar=lambda: False,
                    baixar_anexos=True, rodar_ocr=False, forcar_sessao=False, extrair_documentos=True,
                    esperas_curtas=True, fora_da_caixa_confirmado=False)
            finally:
                fonte.CAIXA_EM_CACHE_SERVE.reset(t2)
                fonte.MODO.reset(t1)

        res = None
        try:
            # Espera a vez se houver outra leitura/sync (não pula: o Defensor quer os arquivos).
            for _ in range(120):
                res = await _com_lock(_rodar)
                if res is not None:
                    break
                await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001
            res = {"erros": [f"{type(e).__name__}: {e}"]}
        _downloads[paj_norm].update(rodando=False, resultado={
            "baixados": (res or {}).get("baixados", 0), "erros": ((res or {}).get("erros") or [])[:5]})

    asyncio.create_task(_tarefa())
    return {"ok": True, "iniciado": True, "faltam": faltam}


def interromper_anexos(paj_norm: str) -> dict:
    """Pedido do Defensor (toast "Interromper"): o download em andamento para
    antes do próximo arquivo. Os já baixados ficam."""
    atual = _downloads.get(paj_norm)
    if not atual or not atual.get("rodando"):
        return {"ok": True, "interrompido": False, "motivo": "nada_em_andamento"}
    fonte.INTERROMPER_DOWNLOAD.add(str(PAJS_DIR / paj_norm / "Arquivos"))
    atual["interrompido"] = True
    return {"ok": True, "interrompido": True}


def progresso_anexos(paj_norm: str) -> dict:
    est = dict(_downloads.get(paj_norm) or {})
    prog = fonte.PROGRESSO_DOWNLOAD.get(str(PAJS_DIR / paj_norm / "Arquivos")) or {}
    est["progresso"] = {k: prog.get(k) for k in ("total", "feitos", "baixados", "erros")}
    est["faltam_agora"] = _anexos_faltando(paj_norm)
    return est


async def atualizar_caixa(forcar: bool = False, automatico: bool = False) -> dict:
    if not ativo():
        return {"ok": False, "motivo": "inativo"}
    if automatico and _fracao_consumida() >= LIMITE_AUTOMATICO:
        return {"ok": False, "motivo": "automatico_pausado_pelo_consumo"}
    if _fracao_consumida() >= LIMITE_ABERTURA:
        return {"ok": False, "motivo": "teto_diario_quase_atingido"}
    if not forcar and time.monotonic() - _caixa["ts"] < MIN_INTERVALO_CAIXA_SEG:
        return {"ok": True, "mudou": False, "motivo": "recente", "quando": _caixa["quando"],
                "mudou_em": _caixa["mudou_em"]}
    reg_antes = sis2.consumo_hoje()["registros"]
    try:
        assinatura = await asyncio.to_thread(fonte.assinatura_caixa)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "motivo": f"{type(e).__name__}: {e}"}
    _caixa["ts"] = time.monotonic()
    if assinatura == _caixa["assinatura"] and not forcar:
        _caixa["quando"] = datetime.now().isoformat(timespec="seconds")
        return {"ok": True, "mudou": False, "quando": _caixa["quando"], "mudou_em": _caixa["mudou_em"],
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
    _caixa["mudou_em"] = _caixa["quando"]
    if not erros:
        _salvar_assinatura()
    _caixa["resultado"] = {k: res.get(k) for k in ("total_caixa", "processados", "pulados", "concluidos")}
    return {"ok": not erros, "mudou": True, "quando": _caixa["quando"], "mudou_em": _caixa["mudou_em"],
            "resultado": _caixa["resultado"],
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
