"""Aprovação humana das gravações no SIS-DPU 2.0 (MCP) — o "modal padrão".

O servidor do MCP só grava depois que o Defensor lê a FICHA INTEGRAL que ele
devolve no preparo e responde que sim. A TI aceitou como resposta o clique em
"Confirmar" num modal do painel que mostra a ficha inteira (10/10/2026).

Como funciona: a gravação (ingestao/sisdpu2_movtram.py) prepara, chama
`solicitar()` e PAUSA. A ficha fica em `_pendentes`; o static/js/ficha_mcp.js,
carregado em todas as páginas, a encontra e abre o modal. O clique responde
via `responder()` e a gravação segue (ou é abandonada). Assim TODA rotina que
grava — console de ações, conjunta, SAT, dossiês, CRPS, RPV, mera ciência —
passa pelo mesmo modal, sem ficha "pendente para depois" (decisão do Defensor,
11/10/2026): sem resposta até a ficha vencer, a gravação NÃO acontece.

Aprovação antecipada (movimentação conjunta): o resumo mostra a ficha antes
de executar o e-proc; ao confirmar, a ficha aprovada é registrada aqui e,
quando a gravação chega, um preparo com a MESMA ficha (conteúdo idêntico) é
liberado sem perguntar de novo. Se mudou qualquer coisa — ou a aprovação
venceu — o modal aparece de novo.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from datetime import datetime
from typing import Optional

VALIDADE_PADRAO_SEG = 540          # o token do servidor vale ~10 min
VALIDADE_ANTECIPADA_SEG = 1800     # aprovação dada no resumo da conjunta

RESPOSTA_CLIQUE = "sim — confirmo (Defensor clicou em 'Confirmar gravação' no Painel SISDPU 2.0 após ler a ficha integral)"

_pendentes: dict[str, dict] = {}
_antecipadas: list[dict] = []


def ficha_para_exibir(ficha: str) -> str:
    """A ficha como vai ao Defensor: tudo o que será gravado, sem o token e sem
    as instruções que o servidor dirige ao assistente de IA."""
    linhas = []
    for linha in (ficha or "").splitlines():
        if re.match(r"\s*(Token de confirma|APRESENTE ESTA FICHA|Espere a RESPOSTA)", linha, re.I):
            break
        linhas.append(linha)
    return "\n".join(linhas).strip()


def _normalizar(ficha: str) -> str:
    return re.sub(r"\s+", " ", ficha_para_exibir(ficha)).strip()


def _validade_seg(ficha: str) -> float:
    m = re.search(r"V[áa]lido at[ée]:\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", ficha or "")
    if m:
        try:
            ate = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M")
            return max(30.0, (ate - datetime.now()).total_seconds() - 20)
        except ValueError:
            pass
    return VALIDADE_PADRAO_SEG


async def solicitar(tipo: str, paj: str, ficha: str, contexto: Optional[dict] = None) -> Optional[str]:
    """Espera o Defensor aprovar a ficha. Devolve a resposta (texto) ou None
    (recusou, ou a ficha venceu sem resposta)."""
    agora = time.time()
    norm = _normalizar(ficha)
    for a in list(_antecipadas):
        if a["ate"] < agora:
            _antecipadas.remove(a)
        elif a["paj"] == paj and a["tipo"] == tipo and a["ficha"] == norm:
            _antecipadas.remove(a)
            return a["resposta"]

    validade = _validade_seg(ficha)
    ident = secrets.token_hex(8)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pendentes[ident] = {"id": ident, "tipo": tipo, "paj": paj, "ficha": ficha_para_exibir(ficha),
                         "contexto": contexto or {}, "criado": agora, "expira": agora + validade,
                         "future": fut}
    try:
        return await asyncio.wait_for(fut, timeout=validade)
    except asyncio.TimeoutError:
        return None
    finally:
        _pendentes.pop(ident, None)


def responder(ident: str, aprovado: bool) -> bool:
    p = _pendentes.get(ident)
    if not p or p["future"].done():
        return False
    p["future"].set_result(RESPOSTA_CLIQUE if aprovado else None)
    return True


def listar_pendentes() -> list[dict]:
    agora = time.time()
    return [{k: v for k, v in p.items() if k != "future"} | {"restam_seg": int(p["expira"] - agora)}
            for p in sorted(_pendentes.values(), key=lambda x: x["criado"])]


def registrar_antecipadas(fichas: list[dict]) -> int:
    """fichas: [{tipo, paj, ficha}] que o Defensor aprovou no resumo."""
    ate = time.time() + VALIDADE_ANTECIPADA_SEG
    for f in fichas:
        _antecipadas.append({"tipo": f["tipo"], "paj": f["paj"], "ficha": _normalizar(f["ficha"]),
                             "resposta": RESPOSTA_CLIQUE, "ate": ate})
    return len(fichas)


# Coleta de fichas sem gravar (dry-run): usada para montar o resumo da conjunta.
_coletando: dict[str, list] = {}


def coletor_ativo(chave: str) -> Optional[list]:
    return _coletando.get(chave)


def iniciar_coleta(chave: str) -> list:
    _coletando[chave] = []
    return _coletando[chave]


def encerrar_coleta(chave: str) -> list:
    return _coletando.pop(chave, [])
