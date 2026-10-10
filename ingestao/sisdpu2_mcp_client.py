"""Cliente do MCP do SIS-DPU 2.0 (https://mcp-sisdpu2.dpu.def.br/mcp) — HTTP puro, sem IA.

Mesmo desenho do `sei_mcp_client` (servidor REMOTO da DPU, JSON-RPC sobre
"streamable HTTP" com `httpx`, OAuth 2.1 com DCR + PKCE e login AD na página
oficial — a senha NUNCA passa pelo painel). O retorno do login cai em
`/api/sisdpu2/oauth/callback` (routes/sisdpu2_mcp.py).

Achados da avaliação real (09-10/10/2026) que moldaram este módulo:
  - O SIS-DPU 2.0 compartilha o BANCO do SISDPU legado: idProcesso = o `id=` da
    URL legada, idTramite = o `data-rk` da caixa, id de arquivo = o id de
    `/sisdpu/rest/download/pdf/{id}/{nome}`. Nada de tabela de conversão.
  - A sessão vale UMA JORNADA (~8 h) e NÃO se renova: vencida, 401 → novo login.
  - Há teto de chamadas por minuto: a recusa diz "teto de N por minuto"; o
    manual do servidor manda esperar e repetir a MESMA consulta.
  - 401 com CLIENTE_NAO_CREDENCIADO / INTEGRACAO_* = chave do servidor suspensa
    pelo Master (não é problema de login do Defensor).
  - Toda escrita é par preparar/confirmar, UM PAJ por vez, com a ficha aprovada
    pelo Defensor. Nesta fase (leitura) o cliente RECUSA qualquer ferramenta
    fora de `_TOOLS_LEITURA` — a escrita entra depois, com o modal de ficha.
  - Não há download binário: `documento_texto` devolve só o TEXTO (com OCR).
    O PDF original continua vindo do legado, pelo mesmo id de arquivo.
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
from pathlib import Path
from typing import Any, Optional

import httpx

from config import PAINEL_PORT

MCP_BASE = "https://mcp-sisdpu2.dpu.def.br"
MCP_URL = MCP_BASE + "/mcp"
REDIRECT_URI = f"http://127.0.0.1:{PAINEL_PORT}/api/sisdpu2/oauth/callback"

# O client_id do DCR fica preso ao redirect_uri (porta): entrada própria por porta.
_KEYRING_SERVICE = "painel-sisdpu-mcp-sisdpu2" + ("" if PAINEL_PORT == 8001 else f"-{PAINEL_PORT}")
_CHUNK = 900  # caracteres por pedaço no Credential Manager (limite ~1280)

# Fase 1: SÓ LEITURA. Qualquer outra ferramenta é recusada aqui, antes de sair.
_TOOLS_LEITURA = {
    "auth_situacao", "diagnostico", "manual",
    "caixa_entrada", "caixa_grupos", "caixa_perfil", "caixa_processos_judiciais",
    "processo_resolver", "processo_pesquisar",
    "paj_capa", "paj_movimentacoes", "paj_partes", "paj_representantes",
    "paj_processos_judiciais", "paj_retornos", "paj_onde_esta", "paj_apensos",
    "paj_anotacoes", "paj_documentos_juntada", "narrativa_do_paj", "pretensao_do_paj",
    "movimentacao_arquivos", "movimentacao_ler", "movimentacao_historico",
    "documento_metadados", "documento_texto", "documentos_do_processo",
    "documentos_do_assistido", "assistido_ficha",
    "tramite_historico", "tramite_detalhe", "tramites_concluidos_do_paj",
    "decurso_do_paj", "decurso_caixa", "prazos", "prazos_da_caixa",
    "audiencias_do_paj", "audiencias_do_paj_futuras", "agenda_audiencias",
    "tag_dos_pajs", "marcacao_do_paj", "ultimos_trabalhados", "pajs_parados",
}

_RE_TETO = re.compile(r"teto de (\d+) (?:leituras )?por minuto", re.IGNORECASE)
# "esta pessoa ja leu 5033 registros hoje por este modulo, e o teto e 5000"
_RE_TETO_DIA = re.compile(r"ja leu (\d+) registros hoje.*?teto e (\d+)", re.IGNORECASE | re.DOTALL)

# Teto diário do servidor (achado em 10/10/2026): 5000 REGISTROS lidos por
# pessoa por dia, somando o painel e o uso do MCP pelo Claude. Conta registros
# (cada item de lista, cada movimentação do histórico), não chamadas.
TETO_DIARIO = 5000


class Sisdpu2LoginNecessario(RuntimeError):
    """Sem token válido — o Defensor precisa (re)fazer o login com o usuário de rede."""


class Sisdpu2Erro(RuntimeError):
    """Erro devolvido pelo servidor MCP do SIS-DPU 2.0 (ou recusa dele)."""


class Sisdpu2TetoDiario(Sisdpu2Erro):
    """O servidor recusou: teto diário de registros desta pessoa atingido."""


# ---------------------------------------------------------------------------
# Medidor de consumo (estatística diária; também freia a leitura automática)
# ---------------------------------------------------------------------------

_ARQ_CONSUMO = Path(__file__).resolve().parents[1].parent / "dados" / "_mcp_sisdpu2_consumo.json"
_lock_consumo = threading.Lock()


def _hoje() -> str:
    return time.strftime("%Y-%m-%d")


def _ler_consumo() -> dict:
    try:
        return json.loads(_ARQ_CONSUMO.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _contar_registros(obj: Any) -> int:
    """ESTIMATIVA do que o servidor conta: lista = 1 por item; objeto = 1 mais
    os itens das listas de objetos que ele traz (ex.: histórico da capa)."""
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        return 1 + sum(len(v) for v in obj.values()
                       if isinstance(v, list) and v and isinstance(v[0], dict))
    return 1


def _registrar_consumo(nome: str, registros: int, teto_servidor: Optional[tuple[int, int]] = None) -> None:
    with _lock_consumo:
        dados = _ler_consumo()
        dia = dados.setdefault(_hoje(), {"chamadas": 0, "registros": 0, "por_ferramenta": {}})
        dia["chamadas"] += 1
        dia["registros"] += registros
        f = dia["por_ferramenta"].setdefault(nome, {"chamadas": 0, "registros": 0})
        f["chamadas"] += 1
        f["registros"] += registros
        if teto_servidor:
            dia["teto_atingido"] = True
            dia["registros_servidor"], dia["teto_servidor"] = teto_servidor
        for velho in sorted(dados)[:-90]:  # guarda ~3 meses
            dados.pop(velho, None)
        try:
            _ARQ_CONSUMO.parent.mkdir(parents=True, exist_ok=True)
            _ARQ_CONSUMO.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def consumo_hoje() -> dict:
    dia = _ler_consumo().get(_hoje(), {})
    return {"data": _hoje(), "chamadas": dia.get("chamadas", 0), "registros": dia.get("registros", 0),
            "teto": dia.get("teto_servidor", TETO_DIARIO), "teto_atingido": bool(dia.get("teto_atingido")),
            "registros_servidor": dia.get("registros_servidor"), "por_ferramenta": dia.get("por_ferramenta", {})}


def historico_consumo(dias: int = 30) -> list[dict]:
    dados = _ler_consumo()
    return [{"data": d, "chamadas": v.get("chamadas", 0), "registros": v.get("registros", 0),
             "teto_atingido": bool(v.get("teto_atingido"))} for d, v in sorted(dados.items(), reverse=True)[:dias]]


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
        return "".join(kr.get_password(_KEYRING_SERVICE, f"{nome}.{i}") or "" for i in range(int(n)))
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
        "client_name": "Painel SISDPU 2.0 (DPU Curitiba)",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    }, timeout=30)
    if r.status_code >= 400:
        raise Sisdpu2Erro(f"O servidor recusou o registro do painel (HTTP {r.status_code}): {r.text[:300]}")
    cid = r.json()["client_id"]
    _salvar_segredo("client_id", cid)
    return cid


def iniciar_login() -> str:
    """Devolve a URL da página de login do MCP (abrir numa aba do navegador)."""
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
        raise Sisdpu2Erro("Login expirado ou inválido — clique em 'Conectar ao SIS-DPU 2.0' de novo.")
    r = httpx.post(MCP_BASE + "/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": pend["client_id"], "code_verifier": pend["verifier"], "resource": MCP_URL,
    }, timeout=30)
    if r.status_code >= 400:
        if "client" in r.text.lower():
            # registro do cliente perdido/revogado no servidor: refaz no próximo login
            _apagar_segredo("client_id")
        raise Sisdpu2Erro(f"Falha ao obter token (HTTP {r.status_code}): {r.text[:300]}")
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


def minutos_restantes() -> Optional[int]:
    """Minutos até o token vencer (pelo expires_in do servidor), ou None."""
    tok = _token()
    if not tok or not tok.get("expira_em"):
        return None
    return max(0, int((float(tok["expira_em"]) - time.time()) // 60))


# ---------------------------------------------------------------------------
# Sessão MCP (JSON-RPC sobre streamable HTTP)
# ---------------------------------------------------------------------------

class _SessaoPerdida(Exception):
    pass


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
            raise Sisdpu2LoginNecessario("Painel não está conectado ao SIS-DPU 2.0.")
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
            corpo = r.text[:300]
            if "CLIENTE_NAO_CREDENCIADO" in corpo or "INTEGRACAO_" in corpo:
                raise Sisdpu2Erro("O acesso do painel ao MCP do SIS-DPU 2.0 está suspenso no servidor "
                                  f"(não é problema do seu login): {corpo}")
            _apagar_segredo("token")
            self.session_id = None
            raise Sisdpu2LoginNecessario("A sessão no SIS-DPU 2.0 venceu — conecte de novo.")
        if r.status_code == 404 and self.session_id:
            self.session_id = None
            raise _SessaoPerdida()
        if r.status_code >= 400:
            raise Sisdpu2Erro(f"MCP do SIS-DPU 2.0 respondeu HTTP {r.status_code}: {r.text[:300]}")
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
            "clientInfo": {"name": "painel-sisdpu-2.0", "version": "2.0"},
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
                        raise Sisdpu2Erro("Sessão MCP perdida duas vezes seguidas.")
            raise Sisdpu2Erro("inalcançável")


_sessao = _Sessao()


def _ler_resposta(r: httpx.Response, req_id: int) -> dict:
    """Aceita resposta JSON direta ou SSE (text/event-stream)."""
    if "text/event-stream" in r.headers.get("content-type", ""):
        for linha in r.text.splitlines():
            if linha.startswith("data:"):
                bruto = linha[5:].strip()
                if not bruto:
                    continue
                msg = json.loads(bruto)
                if msg.get("id") == req_id:
                    return msg
        raise Sisdpu2Erro("Resposta SSE do MCP sem a mensagem esperada.")
    return r.json()


def chamar(nome: str, args: Optional[dict] = None, timeout: float = 90) -> Any:
    """Chama uma ferramenta de LEITURA e devolve o JSON (ou o texto) do 1º bloco.

    Teto por minuto: espera e repete a MESMA chamada (até 3 vezes), como manda
    o manual do servidor. Ferramenta fora da lista de leitura: recusada aqui."""
    if nome not in _TOOLS_LEITURA:
        raise Sisdpu2Erro(f"'{nome}' não é ferramenta de leitura liberada nesta fase do painel.")
    args = args or {}
    for tentativa in range(4):
        msg = _sessao.tools_call(nome, args, timeout=timeout)
        if "error" in msg:
            raise Sisdpu2Erro(f"{nome}: {msg['error'].get('message', msg['error'])}")
        res = msg.get("result") or {}
        blocos = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        texto = blocos[0] if blocos else ""
        if res.get("isError"):
            dia = _RE_TETO_DIA.search(texto)
            if dia:
                _registrar_consumo(nome, 0, (int(dia.group(1)), int(dia.group(2))))
                raise Sisdpu2TetoDiario(texto[:800])
            _registrar_consumo(nome, 0)
            if _RE_TETO.search(texto) and tentativa < 3:
                time.sleep(20 * (tentativa + 1))
                continue
            raise Sisdpu2Erro(texto[:800] or f"{nome}: erro sem mensagem")
        try:
            valor = json.loads(texto)
        except ValueError:
            valor = texto
        _registrar_consumo(nome, _contar_registros(valor))
        return valor
    raise Sisdpu2Erro(f"{nome}: teto de chamadas do servidor persistiu após novas tentativas.")


def url_download_legado(id_arquivo: int, nome: str) -> str:
    """URL de download do PDF no SISDPU LEGADO para um id de arquivo do SIS 2
    (mesmo banco, mesmo id). Exige sessão do legado para baixar."""
    return ("https://sisdpu.dpu.def.br/sisdpu/rest/download/pdf/"
            f"{id_arquivo}/{urllib.parse.quote(nome)}")
