"""Qual caixa de entrada o painel está lendo — separação de PAJs por caixa
(pedido do Defensor, 17/11/2026).

Por quê: o Defensor tem duas caixas no SIS-DPU 2.0 — a do 4º Ofício Previdenciário
de Curitiba (login felipe.kugler, unidade 29) e a da CCR Prev (login
felipe.kugler.ccr, unidade 43). O MCP lê a caixa de quem autorizou a sessão. Até
aqui o painel não sabia de qual caixa vinha cada PAJ: em 12/11/2026, ao conectar
com o login da CCR, a sincronização tratou os PAJs da 029 como "saíram da caixa"
e mandou os anexos deles para a Lixeira.

Como funciona agora:
  - `identificar()` pergunta ao MCP (auth_situacao) em nome de quem está a sessão
    e devolve a identidade da caixa: o LOGIN ("felipe.kugler",
    "felipe.kugler.ccr") — é exato e não depende do número dos PAJs.
  - cada PAJ visto na caixa ganha, no metadata.json, `caixas: [login, ...]`
    (pode estar em mais de uma);
  - a reconciliação só tira da caixa X os PAJs marcados com X
    (ingestao/sincronizador._reconciliar_concluidos);
  - as caixas já vistas ficam em dados/_caixas_conhecidas.json (rótulo, unidade,
    última leitura) — alimentam "Caixas habilitadas" da rotina do dossiê e o
    futuro seletor do dashboard.

Com a fonte legada (raspagem do SISDPU, sem MCP) não há como saber a caixa:
`identificar()` devolve None e a sincronização segue o comportamento antigo.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

_DADOS = Path(__file__).resolve().parents[1].parent / "dados"
_ARQ_CONHECIDAS = _DADOS / "_caixas_conhecidas.json"

# identidade por token: uma consulta ao MCP por sessão (cada chamada conta no teto
# diário do servidor). Trocar de login troca o token, e o cache se refaz sozinho.
_cache: dict = {"chave": None, "caixa": None}

_RE_SESSAO = re.compile(r"em nome de\s+(?P<nome>.+?)\s*\((?P<login>[^()\s]+)\),\s*unidade\s+(?P<unidade>\d+)", re.I)


def _chave_token() -> Optional[str]:
    from ingestao import sisdpu2_mcp_client as sis2
    tok = sis2._token()
    if not tok:
        return None
    bruto = str(tok.get("access_token") or tok.get("token") or json.dumps(tok, sort_keys=True))
    return hashlib.sha256(bruto.encode()).hexdigest()[:16]


def interpretar(texto: str) -> Optional[dict]:
    """'Sessao ativa em nome de (DEF) Fulano (fulano.ccr), unidade 43. ...' →
    {id, login, nome, unidade, rotulo}."""
    m = _RE_SESSAO.search(texto or "")
    if not m:
        return None
    login = m.group("login").strip().lower()
    unidade = m.group("unidade").zfill(3)[-3:]
    nome = m.group("nome").strip()
    return {"id": login, "login": login, "nome": nome, "unidade": unidade,
            "rotulo": f"{login} — unidade {unidade}"}


def _identificar_sync() -> Optional[dict]:
    from ingestao import sisdpu2_fonte
    from ingestao import sisdpu2_mcp_client as sis2

    if not sisdpu2_fonte.fonte_mcp_ativa() or not sis2.conectado():
        return None
    chave = _chave_token()
    if chave and _cache["chave"] == chave and _cache["caixa"]:
        return dict(_cache["caixa"])
    texto = sis2.chamar("auth_situacao")
    caixa = interpretar(texto if isinstance(texto, str) else json.dumps(texto, ensure_ascii=False))
    if caixa:
        _cache.update(chave=chave, caixa=caixa)
        registrar(caixa)
    return caixa


async def identificar() -> Optional[dict]:
    """Caixa da sessão MCP atual, ou None (fonte legada, sem conexão ou resposta
    ilegível). Nunca levanta."""
    try:
        return await asyncio.to_thread(_identificar_sync)
    except Exception:  # noqa: BLE001
        return None


def fonte_mcp() -> bool:
    from ingestao import sisdpu2_fonte
    with contextlib.suppress(Exception):
        return sisdpu2_fonte.fonte_mcp_ativa()
    return False


# ---------------------------------------------------------------- caixas conhecidas

def conhecidas() -> dict:
    with contextlib.suppress(Exception):
        dados = json.loads(_ARQ_CONHECIDAS.read_text(encoding="utf-8"))
        if isinstance(dados, dict):
            return dados
    return {}


def registrar(caixa: dict) -> None:
    dados = conhecidas()
    reg = dados.get(caixa["id"]) or {}
    reg.update({k: caixa[k] for k in ("login", "nome", "unidade", "rotulo")})
    reg["visto_em"] = datetime.now().isoformat(timespec="seconds")
    dados[caixa["id"]] = reg
    with contextlib.suppress(Exception):
        _ARQ_CONHECIDAS.parent.mkdir(parents=True, exist_ok=True)
        _ARQ_CONHECIDAS.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- marcação no metadata

def caixas_do_paj(meta: dict) -> list[str]:
    return [str(c) for c in (meta.get("caixas") or []) if c]


def marcar(meta: dict, caixa_id: Optional[str], anteriores: Optional[list] = None) -> None:
    """Acrescenta `caixa_id` às caixas do PAJ (mantendo as que ele já tinha)."""
    atuais = set(caixas_do_paj(meta)) | {str(c) for c in (anteriores or []) if c}
    if caixa_id:
        atuais.add(caixa_id)
    if atuais:
        meta["caixas"] = sorted(atuais)
