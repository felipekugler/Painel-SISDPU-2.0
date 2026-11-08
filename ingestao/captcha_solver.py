"""Resolução automática do CAPTCHA do SAT Central pelo serviço CapSolver (08/11/2026).

O CAPTCHA da consulta por CPF do SAT é um reCAPTCHA v2 padrão ("Não sou um robô",
api2 — NÃO Enterprise, sem callback), dentro do diálogo PrimeFaces
`idFormRecaptcha:dgCaptcha` (reconhecimento feito em 08/11/2026 na janela logada).

Fluxo (API HTTP do CapSolver, https://docs.capsolver.com):
  1. POST /createTask  {clientKey, task:{type:"ReCaptchaV2TaskProxyLess", websiteURL, websiteKey}}
  2. POST /getTaskResult {clientKey, taskId} a cada 3 s até status "ready"
     → solution.gRecaptchaResponse (o token)
  3. Quem chama coloca o token no <textarea name="g-recaptcha-response"> e clica "Enviar".

Só saem do painel: o endereço da página e a chave PÚBLICA do site (data-sitekey).
Nenhum CPF, nome, cookie ou dado de sessão é enviado.

A chave de API fica no COFRE do painel (sistema "capsolver": usuário = e-mail da
conta, senha = chave de API) — nunca em código ou config em texto puro.
"""

from __future__ import annotations

import asyncio
import time

import httpx

API = "https://api.capsolver.com"
_POLL_S = 3.0
_TIMEOUT_S = 120.0

# errorCode → mensagem em português (as demais caem no errorDescription)
_ERROS = {
    "ERROR_KEY_DENIED_ACCESS": "chave de API do CapSolver inválida",
    "ERROR_ZERO_BALANCE": "saldo do CapSolver zerado",
    "ERROR_CAPTCHA_UNSOLVABLE": "o CapSolver não conseguiu resolver este CAPTCHA",
    "ERROR_TASK_TIMEOUT": "o CapSolver não resolveu a tempo",
    "ERROR_KEY_TEMP_BLOCKED": "chave do CapSolver bloqueada temporariamente (5 min)",
    "ERROR_IP_BANNED": "IP bloqueado pelo CapSolver (10 min)",
    "ERROR_RATE_LIMIT": "limite de requisições do CapSolver atingido",
    "ERROR_SERVICE_UNAVALIABLE": "serviço do CapSolver indisponível",
    "ERROR_INVALID_TASK_DATA": "dados do CAPTCHA inválidos para o CapSolver",
}


def chave_api() -> str:
    """Chave de API do cofre ('' se o cofre estiver trancado ou sem cadastro)."""
    try:
        from services import cofre_service
        cred = cofre_service.get_credenciais("capsolver")
        return (cred[1] or "").strip() if cred else ""
    except Exception:  # noqa: BLE001
        return ""


def _erro(r: dict) -> str:
    cod = r.get("errorCode") or ""
    return _ERROS.get(cod) or (r.get("errorDescription") or cod or "erro desconhecido do CapSolver")


async def saldo(api_key: str | None = None) -> dict:
    """{ok, saldo} ou {ok: False, erro} — usado no botão "Testar chave / ver saldo"."""
    key = api_key or chave_api()
    if not key:
        return {"ok": False, "erro": "Chave de API do CapSolver não cadastrada no cofre (ou cofre trancado)."}
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            r = (await cli.post(f"{API}/getBalance", json={"clientKey": key})).json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Sem comunicação com o CapSolver: {e}"}
    if r.get("errorId"):
        return {"ok": False, "erro": _erro(r)}
    return {"ok": True, "saldo": r.get("balance")}


async def resolver_recaptcha_v2(website_url: str, website_key: str,
                                api_key: str | None = None,
                                timeout_s: float = _TIMEOUT_S) -> dict:
    """Pede ao CapSolver o token do reCAPTCHA v2. Retorna
    {ok: True, token, segundos} ou {ok: False, erro, segundos}."""
    t0 = time.monotonic()
    key = api_key or chave_api()
    if not key:
        return {"ok": False, "erro": "chave de API do CapSolver não disponível (cofre trancado ou sem cadastro)",
                "segundos": 0}
    tarefa = {"type": "ReCaptchaV2TaskProxyLess", "websiteURL": website_url, "websiteKey": website_key}
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = (await cli.post(f"{API}/createTask", json={"clientKey": key, "task": tarefa})).json()
            if r.get("errorId"):
                return {"ok": False, "erro": _erro(r), "segundos": round(time.monotonic() - t0)}
            task_id = r.get("taskId")
            sol = r.get("solution") if r.get("status") == "ready" else None
            while sol is None:
                if time.monotonic() - t0 > timeout_s:
                    return {"ok": False, "erro": "o CapSolver não resolveu a tempo",
                            "segundos": round(time.monotonic() - t0)}
                await asyncio.sleep(_POLL_S)
                r = (await cli.post(f"{API}/getTaskResult",
                                    json={"clientKey": key, "taskId": task_id})).json()
                if r.get("errorId"):
                    return {"ok": False, "erro": _erro(r), "segundos": round(time.monotonic() - t0)}
                if r.get("status") == "failed":
                    return {"ok": False, "erro": _erro(r) if r.get("errorCode") else "o CapSolver falhou",
                            "segundos": round(time.monotonic() - t0)}
                if r.get("status") == "ready":
                    sol = r.get("solution") or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"sem comunicação com o CapSolver: {e}",
                "segundos": round(time.monotonic() - t0)}
    token = (sol or {}).get("gRecaptchaResponse") or ""
    if not token:
        return {"ok": False, "erro": "o CapSolver não devolveu o token", "segundos": round(time.monotonic() - t0)}
    return {"ok": True, "token": token, "segundos": round(time.monotonic() - t0)}
