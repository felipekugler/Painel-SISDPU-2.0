"""Status de conexão do Claude CLI (o mesmo binário usado por chat_service,
analise_pretensao_service, peticao_ia_service etc.) — pro selo da topbar e pra
seção "Conexão com o Claude" em Configurações › Acesso.

Mesmo padrão do PAINEL COMUNICACOES (`analise_service.claude_instalado`/
`testar_conexao`), com uma diferença: aqui não há toggle "Modo Simples/Modo
IA" (o Claude é usado por praticamente todo o painel), então `disponivel()`
não decide "ligado/desligado" por configuração — só reporta o que se sabe
sem precisar chamar o Claude.

Duas checagens, de custo bem diferente:
- `disponivel()` — LEVE, sem subprocess: só `shutil.which` (binário no PATH) +
  leitura best-effort de `~/.claude/.credentials.json` pra saber se o
  refresh token OAuth já venceu. É só uma heurística local (não confirma nada
  no servidor da Anthropic) — usada pro polling do selo, pra não disparar um
  subprocess a cada 15s. Motivo de existir: o caso real que motivou este
  módulo (24/08/2026, PAJ 2026/029-02500) foi um "Failed to authenticate:
  OAuth session expired and could not be refreshed" que só apareceu quando o
  Defensor clicou em Analisar — com essa heurística, o selo já teria virado
  "warn" antes disso, porque `refreshTokenExpiresAt` já estava no passado.
- `testar_conexao()` — PESADA, um `claude -p` real (chamada mínima, ~1
  token de resposta) — única forma de confirmar de verdade. Usada só quando
  o usuário clica em "Testar conexão" (selo ou tela de Configurações).

Nunca dispara login automatizado: se `testar_conexao()` detectar sessão não
autenticada, a única ação é orientar o Defensor a rodar `claude auth login`
(ou `claude`) num terminal — o login em si é sempre feito fora do painel,
mesmo padrão do cofre de credenciais e do PAINEL COMUNICACOES.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from services import cota_service
from services.chat_service import CLAUDE_CMD

_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"

# Timeout do teste real — generoso porque, além do round-trip da API, o
# `claude` ainda inicializa (lê CLAUDE.md, MCPs etc.) antes de responder.
_TIMEOUT_TESTE = 60

_PALAVRAS_NAO_AUTENTICADO = (
    "authenticat",  # authenticate / authentication (en)
    "oauth",
    "not logged",
    "unauthor",
    "api key",
    "/login",
    "log in",
)


def claude_instalado() -> bool:
    return bool(shutil.which("claude"))


def _sessao_localmente_valida() -> bool | None:
    """True/False se der pra avaliar pela credencial local; None se não for
    possível dizer nada (arquivo ausente/corrompido, ou outro método de auth
    em uso — ex.: ANTHROPIC_API_KEY). Nunca lê/expõe o valor dos tokens."""
    try:
        dados = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
        exp_ms = dados.get("claudeAiOauth", {}).get("refreshTokenExpiresAt")
        if not isinstance(exp_ms, (int, float)):
            return None
        return (exp_ms / 1000) > time.time()
    except Exception:
        return None


def disponivel() -> dict:
    """Status leve (sem subprocess) pro polling do selo."""
    instalado = claude_instalado()
    sessao_valida = _sessao_localmente_valida() if instalado else None
    return {"instalado": instalado, "sessao_valida": sessao_valida}


def testar_conexao() -> dict:
    """Chamada mínima e real ao Claude CLI pra confirmar a conexão. Síncrona/
    bloqueante (subprocess) — chamar de dentro de `asyncio.to_thread` nas
    rotas, nunca direto num handler async."""
    if not claude_instalado():
        return {
            "ok": False,
            "estado": "nao_instalado",
            "detalhe": "Claude CLI não encontrado no PATH desta máquina.",
        }

    cmd = [*CLAUDE_CMD, "-p", "--output-format", "json", "--model", "sonnet"]
    try:
        proc = subprocess.run(
            cmd,
            input="Responda apenas com a palavra OK.",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_TESTE,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "estado": "erro",
            "detalhe": f"Sem resposta em {_TIMEOUT_TESTE}s (timeout).",
        }
    except Exception as e:
        return {"ok": False, "estado": "erro", "detalhe": f"{type(e).__name__}: {e}"}

    # `claude -p --output-format json` devolve um único objeto JSON mesmo em
    # falha (ex.: erro de autenticação) — só cai pro texto bruto se, por
    # algum motivo, a saída não vier em JSON.
    texto = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    is_error = proc.returncode != 0
    with contextlib.suppress(Exception):
        payload = json.loads(proc.stdout or "")
        texto = payload.get("result") or texto
        is_error = bool(payload.get("is_error")) or is_error

    if not is_error:
        return {"ok": True, "estado": "ok", "detalhe": "Claude conectado e respondendo normalmente."}

    if cota_service.texto_indica_cota(texto):
        return {
            "ok": False,
            "estado": "cota",
            "detalhe": "Limite de uso do Claude atingido — aguarde a renovação da cota (ver banner do Dashboard).",
        }

    texto_lower = texto.lower()
    if any(p in texto_lower for p in _PALAVRAS_NAO_AUTENTICADO):
        return {
            "ok": False,
            "estado": "nao_autenticado",
            "detalhe": (
                "O Claude está instalado, mas a sessão não está conectada. Abra um "
                "terminal e rode `claude auth login` (ou apenas `claude`, e faça "
                "login uma vez) — o painel nunca pede sua senha."
            ),
        }

    return {"ok": False, "estado": "erro", "detalhe": texto[:300] or "Falha desconhecida ao testar a conexão."}
