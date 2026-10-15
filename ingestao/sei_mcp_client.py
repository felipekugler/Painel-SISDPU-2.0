"""Cliente do MCP-SEI da DPU (https://mcp-sei.dpu.def.br/mcp) — HTTP puro, sem IA.

Diferente do e-proc (`eproc_mcp_client`, que importa o MCP local como
biblioteca), o MCP-SEI é um servidor REMOTO da STI: aqui o painel fala o
protocolo MCP direto (JSON-RPC sobre "streamable HTTP") com `httpx`. Nenhum
modelo de IA fica no meio da gravação: quem decide O QUE vai pro SEI é o
Defensor (texto revisado na tela); este módulo só executa a chamada exata.

Autenticação: OAuth 2.1 do próprio servidor (Dynamic Client Registration +
PKCE), com login LDAP na página oficial da DPU — a senha NUNCA passa pelo
painel. O retorno do login cai em `/api/sei/oauth/callback` (routes/oficio_sei.py)
no próprio painel (127.0.0.1:8001). O token (Bearer, ~30 dias) fica no Windows
Credential Manager (keyring), em pedaços — o Credential Manager limita o
tamanho de cada segredo.

Achados do piloto real (28/09/2026) que moldaram este módulo:
  - Toda resposta do servidor vem em 2 blocos de conteúdo: o 1º é o JSON puro,
    o último é um "carimbo" ⟦mcp-sei·VERIFICAÇÃO … esta resposta é de: <tool>
    <ids> …⟧. Há um bug conhecido de infraestrutura que às vezes devolve a
    resposta de OUTRA requisição — por isso `chamar()` confere o carimbo e
    rejeita resposta cruzada (ver `_conferir_carimbo`).
  - O SEI de produção às vezes responde 302 transitório ("Falha transitória
    do SEI") — leituras são repetidas aqui; GRAVAÇÕES NUNCA são repetidas às
    cegas (podem ter sido executadas).
  - `listar_documentos(start=…)` conta PÁGINAS de `limit`, não documentos.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Optional

import httpx

from config import PAINEL_PORT

MCP_BASE = "https://mcp-sei.dpu.def.br"
MCP_URL = MCP_BASE + "/mcp"
REDIRECT_URI = f"http://127.0.0.1:{PAINEL_PORT}/api/sei/oauth/callback"
SEI_WEB = "https://sei.dpu.def.br/sei/controlador.php"

# O client_id do DCR fica preso ao redirect_uri (e portanto a porta): uma
# instancia em outra porta (Painel SISDPU 2.0) guarda o registro e o token em
# entrada propria, sem reaproveitar nem sobrescrever os da porta 8001.
_KEYRING_SERVICE = "painel-sisdpu-mcp-sei" + ("" if PAINEL_PORT == 8001 else f"-{PAINEL_PORT}")
_CHUNK = 900  # caracteres por pedaço no Credential Manager (limite ~1280)

# Tools de GRAVAÇÃO — nunca repetidas automaticamente em falha transitória.
_TOOLS_ESCRITA = {
    "criar_minuta", "alterar_minuta", "alterar_documento_interno",
    "adicionar_ao_bloco", "gerar_bloco",
}


class SeiLoginNecessario(RuntimeError):
    """Sem token válido — o Defensor precisa (re)fazer o login DPU."""


class SeiErro(RuntimeError):
    """Erro devolvido pelo servidor MCP-SEI / SEI."""


# ---------------------------------------------------------------------------
# Armazenamento do token (keyring, em pedaços)
# ---------------------------------------------------------------------------

def _kr():
    import keyring
    return keyring


def _salvar_segredo(nome: str, valor: str) -> None:
    kr = _kr()
    partes = [valor[i:i + _CHUNK] for i in range(0, len(valor), _CHUNK)] or [""]
    kr.set_password(_KEYRING_SERVICE, f"{nome}.n", str(len(partes)))
    for i, p in enumerate(partes):
        kr.set_password(_KEYRING_SERVICE, f"{nome}.{i}", p)


def _ler_segredo(nome: str) -> Optional[str]:
    kr = _kr()
    try:
        n = kr.get_password(_KEYRING_SERVICE, f"{nome}.n")
        if not n:
            return None
        partes = [kr.get_password(_KEYRING_SERVICE, f"{nome}.{i}") or "" for i in range(int(n))]
        return "".join(partes)
    except Exception:  # noqa: BLE001
        return None


def _apagar_segredo(nome: str) -> None:
    kr = _kr()
    try:
        n = int(kr.get_password(_KEYRING_SERVICE, f"{nome}.n") or 0)
    except Exception:  # noqa: BLE001
        n = 0
    for chave in [f"{nome}.n", *[f"{nome}.{i}" for i in range(n)]]:
        try:
            kr.delete_password(_KEYRING_SERVICE, chave)
        except Exception:  # noqa: BLE001
            pass


def _token() -> Optional[dict]:
    bruto = _ler_segredo("token")
    if not bruto:
        return None
    try:
        tok = json.loads(bruto)
    except ValueError:
        return None
    if tok.get("expira_em") and time.time() > float(tok["expira_em"]) - 60:
        return None
    return tok


# ---------------------------------------------------------------------------
# OAuth (DCR + PKCE)
# ---------------------------------------------------------------------------

_pendentes: dict[str, dict] = {}  # state -> {verifier, client_id, criado}
_lock_oauth = threading.Lock()


def _client_id() -> str:
    cid = _ler_segredo("client_id")
    if cid:
        return cid
    r = httpx.post(MCP_BASE + "/oauth/register", json={
        "client_name": "Painel SISDPU (DPU Curitiba)",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    }, timeout=30)
    r.raise_for_status()
    cid = r.json()["client_id"]
    _salvar_segredo("client_id", cid)
    return cid


def iniciar_login() -> str:
    """Devolve a URL da página de login DPU (abrir numa aba do navegador)."""
    cid = _client_id()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    with _lock_oauth:
        agora = time.time()
        for s in [s for s, v in _pendentes.items() if agora - v["criado"] > 900]:
            _pendentes.pop(s, None)
        _pendentes[state] = {"verifier": verifier, "client_id": cid, "criado": agora}
    qs = urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT_URI,
        "scope": "mcp", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": MCP_URL,
    })
    return f"{MCP_BASE}/oauth/authorize?{qs}"


def concluir_login(code: str, state: str) -> None:
    with _lock_oauth:
        pend = _pendentes.pop(state, None)
    if not pend:
        raise SeiErro("Login expirado ou inválido — clique em 'Conectar ao SEI' de novo.")
    r = httpx.post(MCP_BASE + "/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": pend["client_id"], "code_verifier": pend["verifier"], "resource": MCP_URL,
    }, timeout=30)
    if r.status_code >= 400:
        raise SeiErro(f"Falha ao obter token do MCP-SEI (HTTP {r.status_code}): {r.text[:300]}")
    j = r.json()
    tok = {"access_token": j["access_token"]}
    if j.get("expires_in"):
        tok["expira_em"] = time.time() + float(j["expires_in"])
    _salvar_segredo("token", json.dumps(tok))
    _sessao.resetar()


def desconectar() -> None:
    _apagar_segredo("token")
    _sessao.resetar()


def conectado() -> bool:
    return _token() is not None


def dias_restantes() -> Optional[int]:
    tok = _token()
    if not tok or not tok.get("expira_em"):
        return None
    return max(0, int((float(tok["expira_em"]) - time.time()) // 86400))


def verificar() -> dict:
    """Conexão de VERDADE: abre uma sessão MCP com o token guardado (o servidor
    responde 401 se ele tiver sido revogado/expirado — aí o token é apagado).
    {conectado, dias_restantes, erro}."""
    if not conectado():
        return {"conectado": False, "dias_restantes": None, "erro": ""}
    try:
        with _sessao.lock:
            _sessao._inicializar()
        return {"conectado": True, "dias_restantes": dias_restantes(), "erro": ""}
    except SeiLoginNecessario:
        return {"conectado": False, "dias_restantes": None, "erro": "O login no SEI expirou."}
    except Exception as e:  # noqa: BLE001 — rede/servidor: não afirma que caiu
        return {"conectado": True, "dias_restantes": dias_restantes(),
                "erro": f"Não foi possível confirmar a conexão com o SEI agora: {e}"[:300]}


# ---------------------------------------------------------------------------
# Sessão MCP (JSON-RPC sobre streamable HTTP)
# ---------------------------------------------------------------------------

class _Sessao:
    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self._id = 0
        self.lock = threading.RLock()

    def resetar(self) -> None:
        with self.lock:
            self.session_id = None

    def _headers(self) -> dict:
        tok = _token()
        if not tok:
            raise SeiLoginNecessario("Painel não está conectado ao SEI.")
        h = {
            "Authorization": f"Bearer {tok['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": "2025-06-18",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload: dict, timeout: float = 90) -> Optional[dict]:
        r = httpx.post(MCP_URL, headers=self._headers(), json=payload, timeout=timeout)
        if r.status_code == 401:
            _apagar_segredo("token")
            self.session_id = None
            raise SeiLoginNecessario("Login no SEI expirou — conecte de novo.")
        if r.status_code == 404 and self.session_id:
            self.session_id = None
            raise _SessaoPerdida()
        if r.status_code >= 400:
            raise SeiErro(f"MCP-SEI respondeu HTTP {r.status_code}: {r.text[:300]}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if "id" not in payload:  # notificação — sem corpo
            return None
        return _ler_resposta(r, payload["id"])

    def _inicializar(self) -> None:
        self.session_id = None
        self._id += 1
        self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "painel-sisdpu", "version": "1.0"},
        }}, timeout=30)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30)

    def tools_call(self, nome: str, args: dict, timeout: float = 90) -> dict:
        with self.lock:
            for tentativa in range(2):
                if not self.session_id:
                    self._inicializar()
                self._id += 1
                try:
                    return self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                       "params": {"name": nome, "arguments": args}}, timeout=timeout)
                except _SessaoPerdida:
                    if tentativa:
                        raise SeiErro("Sessão MCP-SEI perdida duas vezes seguidas.")
            raise SeiErro("inalcançável")


class _SessaoPerdida(Exception):
    pass


_sessao = _Sessao()


def _ler_resposta(r: httpx.Response, req_id: int) -> dict:
    """Aceita resposta JSON direta ou SSE (text/event-stream)."""
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for linha in r.text.splitlines():
            if linha.startswith("data:"):
                with_json = linha[5:].strip()
                if not with_json:
                    continue
                msg = json.loads(with_json)
                if msg.get("id") == req_id:
                    return msg
        raise SeiErro("Resposta SSE do MCP-SEI sem a mensagem esperada.")
    return r.json()


_RE_CARIMBO = re.compile(r"esta resposta é de:\s*([a-z_]+)([^·⟧]*)")


def _conferir_carimbo(nome: str, args: dict, blocos: list[str]) -> None:
    """Rejeita resposta cruzada (bug conhecido de reuso de conexão na borda)."""
    carimbo = next((b for b in reversed(blocos) if "mcp-sei·VERIFICAÇÃO" in b), "")
    if not carimbo:
        return  # servidor sem carimbo — nada a conferir
    m = _RE_CARIMBO.search(carimbo)
    if not m:
        return
    if m.group(1) != nome:
        raise _RespostaCruzada(f"carimbo de '{m.group(1)}' em chamada de '{nome}'")
    ids = m.group(2)
    for chave in ("numero_processo", "documento_id", "id_documento"):
        esperado = args.get(chave)
        achado = re.search(chave + r"=(\S+)", ids)
        if esperado and achado and achado.group(1) != str(esperado):
            raise _RespostaCruzada(f"{chave}={achado.group(1)} ≠ {esperado}")


class _RespostaCruzada(Exception):
    pass


def chamar(nome: str, args: Optional[dict] = None, timeout: float = 90) -> Any:
    """Chama uma tool do MCP-SEI e devolve o JSON do 1º bloco (já decodificado).

    Leituras: repete 1x em resposta cruzada e 1x em falha transitória do SEI.
    Gravações: NUNCA repete — levanta erro dizendo para conferir no SEI."""
    args = args or {}
    escrita = nome in _TOOLS_ESCRITA
    ultima_falha = ""
    for tentativa in range(1 if escrita else 3):
        msg = _sessao.tools_call(nome, args, timeout=timeout)
        if "error" in msg:
            raise SeiErro(f"{nome}: {msg['error'].get('message', msg['error'])}")
        res = msg.get("result") or {}
        blocos = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        try:
            _conferir_carimbo(nome, args, blocos)
        except _RespostaCruzada as e:
            if escrita:
                raise SeiErro(f"Resposta do SEI veio trocada ({e}) — a gravação PODE ter ocorrido. "
                              "Confira no processo antes de tentar de novo.")
            ultima_falha = f"resposta cruzada ({e})"
            time.sleep(1.5)
            continue
        texto = blocos[0] if blocos else ""
        if res.get("isError"):
            transitoria = "Falha transitória" in texto or "302" in texto or "corpo VAZIO" in texto
            if transitoria and not escrita and tentativa < 2:
                ultima_falha = texto[:300]
                time.sleep(2)
                continue
            raise SeiErro(texto[:800] or f"{nome}: erro sem mensagem")
        # O servidor às vezes anexa o carimbo no MESMO bloco do JSON — corta.
        corte = texto.find("⟦mcp-sei")
        if corte > 0:
            texto = texto[:corte]
        try:
            return json.loads(texto)
        except ValueError:
            return texto
    raise SeiErro(f"{nome}: falhou após novas tentativas — {ultima_falha}")


def link_documento(id_procedimento: str, id_documento: str) -> str:
    """Abre o processo já com o documento selecionado (validado no piloto:
    o SEI guarda o destino durante o login e cai direto no documento)."""
    return (f"{SEI_WEB}?acao=procedimento_trabalhar&id_procedimento={id_procedimento}"
            f"&id_documento={id_documento}")
