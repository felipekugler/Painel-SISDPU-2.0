"""Intimações do e-proc da Justiça Militar da União (JMU) na aba Prazos.

Pedido do Defensor (17/10/2026): os prazos da JMU entram na MESMA tabela de
Prazos do TRF4, com filtro "Todos · TRF4 · JMU", selo "JMU" e a cor da linha
pela classe, como o próprio e-proc da JMU pinta (`css/cor-capa.php` dele).

Leitura pelo MCP da JMU (`C:\\Claude\\MCP\\eproc-jmu`, cópia do eproc-trf4
apontada para eproc1g.stm.jus.br, credenciais na chave `mcp-eproc-jmu` do
Gerenciador de Credenciais) — num processo à parte (`eproc_jmu_leitor.py`),
porque os módulos dos dois MCPs têm os mesmos nomes.

Os casos da JMU ficam num arquivo PRÓPRIO (`dados/_eproc_intimacoes_jmu.json`),
fora do cache do TRF4: agenda, triagem de intimações, retornos, vínculo
processo-PAJ e as ações automáticas (abrir prazo, ciência, peticionar) usam o
e-proc do TRF4 e não sabem tratar um processo da JMU. Só a aba Prazos junta os
dois (`mesclar`). Ações automáticas na JMU: etapa futura.

Procurador: na JMU o login é o identificador do representante no e-proc
(ex.: "DPUKUGLER") — mesmo padrão do TRF4 ("DPU551"); ver `meu_identificador`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

JMU_DIR = Path("C:/Claude/MCP/eproc-jmu")
CREDENCIAL = "mcp-eproc-jmu"
TRIBUNAL = "JMU"
URL_ENTRAR = "https://eproc1g.stm.jus.br/eproc_1g_prod/externo_controlador.php?acao=principal"

_STORE = Path(__file__).resolve().parents[1].parent / "dados" / "_eproc_intimacoes_jmu.json"
_LEITOR = Path(__file__).resolve().parent / "eproc_jmu_leitor.py"
_TIMEOUT_SEG = 300

def eh_processo_jmu(numero: str) -> bool:
    """Número CNJ NNNNNNN-DD.AAAA.J.TR.OOOO — segmento J = 7 é a Justiça
    Militar da União."""
    d = re.sub(r"\D", "", numero or "")
    return len(d) == 20 and d[13] == "7"


def meu_identificador() -> str:
    """Usuário do login do MCP da JMU — é o identificador do procurador no
    e-proc da JMU (ex.: "DPUKUGLER"). Vazio se não configurado."""
    with contextlib.suppress(Exception):
        import keyring
        return (keyring.get_password(CREDENCIAL, "usuario") or "").strip()
    return ""


def configurado() -> bool:
    return JMU_DIR.exists() and bool(meu_identificador())


# ---- armazenamento (próprio, separado do TRF4) -------------------------------

def carregar() -> dict:
    with contextlib.suppress(Exception):
        if _STORE.exists():
            return json.loads(_STORE.read_text(encoding="utf-8"))
    return {"sincronizado_em": "", "resumo": [], "secoes": {}}


def _marcar(secoes: dict, tribunal: str = TRIBUNAL, instancia: int = 1) -> dict:
    """Selo/tribunal de cada caso pelo padrão de `ingestao.tribunais`."""
    from ingestao import tribunais
    for casos in (secoes or {}).values():
        for c in casos or []:
            tribunais.marcar(c, tribunal, instancia)
    return secoes


def _salvar(res: dict) -> dict:
    """Grava a leitura nova, preservando `primeira_vez`/campos locais por processo
    (mesma ideia do `eproc_intimacoes.salvar` do TRF4)."""
    anterior = carregar()
    idx = {c.get("processo"): c for cs in (anterior.get("secoes") or {}).values() for c in cs or []}
    agora = dt.datetime.now().isoformat(timespec="seconds")
    secoes = _marcar(res.get("secoes") or {})
    for casos in secoes.values():
        for c in casos:
            ant = idx.get(c.get("processo"))
            c["primeira_vez"] = (ant or {}).get("primeira_vez", agora)
            c["novo"] = ant is None
            for campo in ("tratado", "obs"):
                if ant and campo in ant:
                    c[campo] = ant[campo]
    payload = {"sincronizado_em": agora, "resumo": res.get("resumo") or [], "secoes": secoes}
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ---- leitura -----------------------------------------------------------------

async def ler_intimacoes() -> dict:
    """Roda o leitor da JMU num processo à parte. Nunca lança."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(carregar().get("secoes") or {}, f, ensure_ascii=False)
        anteriores = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_LEITOR), anteriores,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SEG)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return {"ok": False, "erro": f"e-proc da JMU não respondeu em {_TIMEOUT_SEG}s."}
        linhas = [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
        if not linhas:
            return {"ok": False, "erro": "Leitura da JMU sem resposta: "
                    + err.decode("utf-8", "replace").strip()[-300:]}
        return json.loads(linhas[-1])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        with contextlib.suppress(OSError):
            Path(anteriores).unlink()


_EXEC = Path(__file__).resolve().parent / "eproc_jmu_exec.py"


async def executar_mcp(funcao: str, kwargs: dict, timeout: float = _TIMEOUT_SEG) -> object:
    """Roda `eproc_mcp_client.<funcao>(**kwargs)` com o MCP da JMU, num processo
    à parte (`eproc_jmu_exec.py`) — é por aqui que as funções do e-proc
    (consulta processual, triagem, documentos, acesso à íntegra, lembretes)
    valem também para processos da JMU (segmento 7 do número). Erro → dict
    com "erro" (mesmo formato das funções)."""
    if not configurado():
        return {"erro": "MCP do e-proc da JMU não configurado (credenciais em "
                        f"'{CREDENCIAL}' — ver C:\\Claude\\MCP\\eproc-jmu)."}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_EXEC), funcao,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps(kwargs, ensure_ascii=False).encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return {"erro": f"e-proc da JMU não respondeu em {int(timeout)}s."}
        linhas = [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
        if not linhas:
            return {"erro": "e-proc da JMU sem resposta: " + err.decode("utf-8", "replace").strip()[-300:]}
        return json.loads(linhas[-1])
    except Exception as e:  # noqa: BLE001
        return {"erro": f"e-proc da JMU: {type(e).__name__}: {e}"[:300]}


_lock = asyncio.Lock()


async def sincronizar() -> dict:
    """Lê a JMU e grava. Falha → mantém a última leitura e devolve o motivo."""
    if not configurado():
        return {"ok": False, "configurado": False}
    async with _lock:
        res = await ler_intimacoes()
        if not res.get("ok"):
            return {"ok": False, "erro": res.get("erro") or "falha desconhecida"}
        payload = _salvar(res)
        return {"ok": True, **payload, **({"aviso": res["aviso"]} if res.get("aviso") else {})}


# ---- junção com o TRF4 (só para a aba Prazos) ----------------------------------

def mesclar(dados_trf4: dict, jmu: dict | None = None, erro_jmu: str = "") -> dict:
    """Devolve `dados_trf4` com os casos da JMU somados às mesmas categorias,
    cada caso marcado pelo padrão de `ingestao.tribunais` (tribunal, instância,
    selo — os do TRF4 vêm do painel da 1ª instância: selo "JFPR"),
    `resumo_tribunal` {TRF4: resumo, JMU: resumo} e `tribunais` (botões do
    filtro da tela)."""
    from ingestao import tribunais
    jmu = jmu if jmu is not None else carregar()
    secoes = {k: list(v or []) for k, v in _marcar(dados_trf4.get("secoes") or {}, "TRF4", 1).items()}
    for chave, casos in _marcar(jmu.get("secoes") or {}).items():
        secoes.setdefault(chave, []).extend(casos)
    presentes = {c.get("tribunal") for cs in secoes.values() for c in cs}

    res_trf4 = dados_trf4.get("resumo") or []
    res_jmu = jmu.get("resumo") or []
    base = res_trf4 or [{**r, "quantidade": 0} for r in res_jmu]
    qtd_jmu = {r.get("chave"): int(r.get("quantidade") or 0) for r in res_jmu}
    resumo = [{**r, "quantidade": int(r.get("quantidade") or 0) + qtd_jmu.get(r.get("chave"), 0)} for r in base]

    out = {**dados_trf4, "secoes": secoes, "resumo": resumo,
           "resumo_tribunal": {"TRF4": res_trf4, "JMU": res_jmu},
           "tribunais": tribunais.para_tela(presentes | {"TRF4"} | ({"JMU"} if configurado() else set())),
           "jmu": {"configurado": configurado(), "sincronizado_em": jmu.get("sincronizado_em", ""),
                   "url_entrar": URL_ENTRAR, **({"erro": erro_jmu} if erro_jmu else {})}}
    return out
