"""Módulo genérico "Ações no SISDPU": console de despacho (movimentar → tramitar →
concluir) reutilizável em QUALQUER página do painel — não depende da Retornos nem de
nenhum store específico. O chamador informa o `id_processo` do SISDPU e o texto do
despacho; quando houver um trâmite específico da caixa de entrada a concluir (ex.: um
Atendimento de Retorno), informa também `id_tramite` — sem ele, a etapa de conclusão é
ignorada (nada a concluir).

Extraído de `retornos_service.py` (que hoje é o único consumidor, e passou a delegar
a execução para cá mantendo seus efeitos exclusivos — histórico e remoção do próprio
store). Futuras páginas (PAJ detail, Watchlist etc.) podem chamar este módulo direto.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import AsyncIterator

from ingestao import sisdpu_client
from ingestao import sisdpu_movtram as movtram


def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _normalizar(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _iso_to_br(s: str) -> str:
    """'2026-08-15' -> '15/08/2026'. Aceita já-BR ou vazio (devolve intacto)."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def _parece_problema_sessao(resultado, excecao: Exception | None) -> bool:
    """True quando a falha indica SESSÃO indisponível (conflito, deslogado, cofre
    bloqueado) — casos em que vale reconectar e tentar de novo, em vez de desistir."""
    if excecao is not None:
        return True
    if isinstance(resultado, dict) and not resultado.get("ok"):
        det = _normalizar(str(resultado.get("detalhe") or ""))
        return any(t in det for t in ("conflito de sessao", "sessao", "login", "cofre bloqueado"))
    return False


async def com_auto_reconexao(chamada):
    """Executa `chamada()` (coroutine factory); se falhar por sessão indisponível,
    RECONECTA automaticamente (login fresco no SISDPU) e tenta a MESMA ação mais uma
    vez. Retorna (resultado, excecao, eventos_sse) — eventos_sse já prontos para `yield`."""
    eventos: list[dict] = []
    try:
        resultado = await chamada()
        excecao = None
    except Exception as e:  # noqa: BLE001
        resultado, excecao = None, e

    if _parece_problema_sessao(resultado, excecao):
        eventos.append(_ev("muted", "Sessão do SISDPU indisponível — reconectando automaticamente…"))
        try:
            rec_conn = await sisdpu_client.reconectar()
        except Exception as e:  # noqa: BLE001
            rec_conn = {"ok": False, "detalhe": str(e)[:300]}
        eventos.append(_ev("ok" if rec_conn.get("ok") else "erro",
                           f"Reconexão automática: {rec_conn.get('detalhe', '')}"))
        if rec_conn.get("ok"):
            try:
                resultado = await chamada()
                excecao = None
            except Exception as e:  # noqa: BLE001
                resultado, excecao = None, e
    return resultado, excecao, eventos


async def executar_stream(
    id_processo: str,
    paj: str = "",
    fase: str = "",
    descricao_mov: str = "",
    grupo: str | None = None,
    descricao_tram: str = "",
    prazo_dias: int = 0,
    prazo_data: str = "",
    id_tramite: str | None = None,
    concluir: bool = False,
    dry_run: bool = False,
) -> AsyncIterator[dict]:
    """Executa mov→tram→conclui (cada etapa opcional) em UM PAJ do SISDPU identificado
    por `id_processo`. Emite eventos SSE-like: {level,msg} de log; evento final
    {done: True, resultado: {...}}. `concluir` só tem efeito quando `id_tramite`
    também for informado (é o trâmite específico que sai da caixa de entrada).
    `descricao_mov` e `descricao_tram` são independentes — o SISDPU aceita ambas
    (ou qualquer uma delas) em branco, então não há validação de obrigatoriedade aqui."""
    fase = (fase or "").strip()
    descricao_mov = (descricao_mov or "").strip()
    descricao_tram = (descricao_tram or "").strip()
    if not id_processo:
        yield _ev("erro", "id_processo ausente.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "id_processo ausente."}}
        return

    concluir_efetivo = bool(concluir and id_tramite)
    if not fase and not grupo and not concluir_efetivo:
        yield _ev("erro", "Selecione ao menos uma ação: movimentar, tramitar ou concluir.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Nenhuma ação selecionada."}}
        return

    # prazo: data explícita (calendário) tem precedência sobre dias
    prazo_data_final = None
    if grupo:
        if prazo_data:
            prazo_data_final = _iso_to_br(prazo_data)
        elif prazo_dias and int(prazo_dias) > 0:
            prazo_data_final = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")

    pajs = [{"id": id_processo, "numero": paj}]
    pref = "[dry-run] " if dry_run else ""
    label = paj or id_processo
    yield _ev("novo", f"{pref}Executando {label}…")

    # 1) MOVIMENTAR (opcional — carrega o despacho como descrição da movimentação)
    mv: dict = {}
    if fase:
        yield _ev("info", f"Movimentando (fase: {fase})…")
        mv, exc, eventos = await com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, descricao_mov, fase, [], "Documentos do Assistido", dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na movimentação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Verifique a conexão do SISDPU (selo/Reconectar) e tente de novo.")
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if mv.get("ok") else "erro", f"Movimentação: {mv.get('detalhe', '')}")
        if not mv.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
            return

    # 2) TRAMITAR (opcional)
    tram_caixa = ""
    if grupo:
        prazo_txt = f"prazo até {prazo_data_final}" if prazo_data_final else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})…")
        tr, exc, eventos = await com_auto_reconexao(
            lambda: movtram.tramitar_paj(pajs, grupo=grupo, descricao=descricao_tram,
                                         prazo_data=prazo_data_final, dry_run=dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na tramitação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "A movimentação já foi registrada; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if tr.get("ok") else "erro", f"Tramitação: {tr.get('detalhe', '')}")
        if not tr.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
            return
        tram_caixa = grupo

    # 3) CONCLUIR (opcional — só quando id_tramite foi informado)
    concluido = False
    ja_ausente = False
    if concluir_efetivo:
        yield _ev("info", "Concluindo o trâmite…")
        cc, exc, eventos = await com_auto_reconexao(lambda: movtram.concluir_paj([id_tramite], dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na conclusão ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Mov./tramitação já podem ter sido registradas; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        if cc.get("status") == "ja_ausente":
            yield _ev("muted", f"Conclusão: {cc.get('detalhe', '')}")
            ja_ausente = True
        else:
            yield _ev("ok" if cc.get("ok") else "erro", f"Conclusão: {cc.get('detalhe', '')}")
            if not cc.get("ok"):
                yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": cc.get("detalhe", "")}}
                return
            concluido = not dry_run

    if ja_ausente:
        yield _ev("muted", f"{label} — o trâmite já não estava na caixa antes desta "
                           "execução (provável conclusão manual anterior).")
        yield {"done": True, "resultado": {"ok": False, "status": "ja_ausente", "concluido": False,
                                           "tramitado_para": tram_caixa,
                                           "detalhe": "Trâmite já estava ausente antes desta execução — "
                                                     "nada foi concluído agora."}}
        return

    yield _ev("ok", f"{label} — executado com sucesso.")
    yield {"done": True, "resultado": {"ok": True, "concluido": concluido,
                                       "tramitado_para": tram_caixa, "mov_seq": mv.get("mov_seq")}}
