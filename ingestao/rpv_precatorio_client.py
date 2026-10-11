"""Cliente de consulta de pagamento de RPV/Precatório nos sites públicos dos bancos
depositários da Justiça Federal (CEF e, no futuro, BB) — via Playwright, janela
VISÍVEL e PERSISTENTE (mesmo padrão de `ingestao.sat_client`: processo Chromium
separado, conectado por CDP, sobrevive a reinícios do painel).

Diferença central em relação a `sat_client`/`eproc_client`: aqui NÃO há login nem
sessão de usuário — é uma consulta pública (agência/operação/conta/dv). O perfil
persistente existe só para acumular histórico de navegação real (cookies,
`user-data-dir`) — o site da CEF tem proteção anti-robô (ShieldSquare/PerimeterX,
confirmado em teste real 2026-09-22: a 1ª consulta caiu direto em
`validate.perfdrive.com`, tela "Precisamos fazer uma verificação de segurança") —
uma janela com histórico real de uso tende a ser menos suspeita que um contexto
descartável.

⚠️ Este módulo NUNCA tenta resolver esse desafio programaticamente (contornar
CAPTCHA/anti-robô é uma linha que este projeto não cruza, mesmo sendo tecnicamente
possível) — quando ele aparece, a consulta deste item é marcada `bloqueado` e a
janela FICA AQUERE ABERTA na tela do desafio; o Defensor resolve manualmente (é
uma verificação simples de "toque no quadro") e clica "Tentar de novo" na tela do
Painel — mesmo espírito de resolução manual já usado no login do SAT Central
(2FA/CAPTCHA) e no `resolver_acesso_integra` do e-proc.

**Ainda não validado contra um caso real** (só o formulário foi inspecionado ao
vivo, com dados de exemplo do próprio usuário — a consulta de fato caiu no
anti-robô antes de eu conseguir ver a tela de sucesso ou de "sem dados"). A
classificação de banner (`_classificar_banner`) é uma heurística genérica
(qualquer painel de mensagem RichFaces visível) que precisa ser confirmada nos
dois casos reais informados pelo usuário: 50349052120154047000 (CEF, 2 credores,
ambos pagos — deve abrir aba com PDF) e 50123274920244047000 (CEF, 3 credores,
nenhum pago — deve mostrar "Não há dados para consulta!")."""

from __future__ import annotations

import contextlib
import re
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from playwright.async_api import async_playwright

import config

_PROFILE = Path(config.OFICIO_GERAL) / "_rpv_profile"
_CDP_PORT = 9224 + config.CDP_OFFSET  # SAT=9222, e-proc=9223 — porta própria, coexiste com as duas

CEF_URL = (
    "https://depositojudicial.caixa.gov.br/sigsj_internet/"
    "levantamento-deposito-judicial/index.xhtml"
)

# Campos fixos do formulário da CEF (confirmados ao vivo, 2026-09-22) — Agência e
# Operação SEMPRE são estas, para qualquer RPV/precatório da Justiça Federal; só
# Conta+DV variam por credor (lidos do DEMTRANSF — ver services.consulta_rpv_service).
_CEF_AGENCIA_FIXA = "0652"
_CEF_OPERACAO_FIXA = "5"  # "005 - Depósitos Judiciais da Justiça Federal"

# ids reais do formulário (JSF/PrimeFaces — confirmados ao vivo 2026-09-22):
#   radio "Agência/Operação/Conta/DV": filtro4
#   Agência: j_id49:j_id51:agencia | Operação: j_id49:j_id51:operacaoFalse
#   Conta: j_id49:j_id51:conta | DV: j_id49:j_id51:dv
#   Botão consultar (rotulado "Consultar" na tela, id interno "btnBaixar"): j_id49:j_id51:btnBaixar
_SEL_RADIO = '[id="filtro4"]'
_SEL_AGENCIA = '[id="j_id49:j_id51:agencia"]'
_SEL_OPERACAO = '[id="j_id49:j_id51:operacaoFalse"]'
_SEL_CONTA = '[id="j_id49:j_id51:conta"]'
_SEL_DV = '[id="j_id49:j_id51:dv"]'
_SEL_CONSULTAR = '[id="j_id49:j_id51:btnBaixar"]'

_pw = None
_rpv_pid: int | None = None


def _porta_aberta(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_na_porta(port: int) -> int | None:
    with contextlib.suppress(Exception):
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                return int(line.split()[-1])
    return None


def fechar_navegador() -> None:
    """Fecha o Chromium desta consulta (shutdown do painel) — mesmo padrão de
    `sat_client.fechar_navegador`. Não roda em kill -9, então reinícios de dev
    preservam a janela (e um eventual desafio anti-robô em andamento nela)."""
    global _rpv_pid
    if not _porta_aberta(_CDP_PORT):
        _rpv_pid = None
        return
    pid = _rpv_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _rpv_pid = None


async def _pw_start():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _ensure_chromium() -> None:
    global _rpv_pid
    if _porta_aberta(_CDP_PORT):
        return
    pw = await _pw_start()
    exe = pw.chromium.executable_path
    _PROFILE.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [exe,
         f"--remote-debugging-port={_CDP_PORT}",
         f"--user-data-dir={_PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "about:blank"],
        creationflags=flags, close_fds=True,
    )
    _rpv_pid = proc.pid
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)
            return
        time.sleep(0.5)


async def _connect():
    await _ensure_chromium()
    pw = await _pw_start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return browser, page


def _norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


_MARCAS_SEM_DADOS = ("nao ha dados", "nao existem dados", "nao foram encontrados",
                     "nenhum registro")
_MARCAS_CAPTCHA = ("verificacao de seguranca", "nao e um robo", "shieldsquare",
                   "perfdrive")


def _classificar(url: str, banner_norm: str) -> str:
    """Retorna 'captcha' | 'sem_dados' | 'indisponivel' | None (nenhum sinal —
    quem chama continua esperando)."""
    u = (url or "").lower()
    if "perfdrive.com" in u or "validate." in u:
        return "captcha"
    if any(m in banner_norm for m in _MARCAS_CAPTCHA):
        return "captcha"
    if any(m in banner_norm for m in _MARCAS_SEM_DADOS):
        return "sem_dados"
    if banner_norm:
        return "indisponivel"
    return None


# Painel de mensagem RichFaces (modalPanel "modal") ou qualquer contêiner com id
# contendo "ensagem"/"messages" que esteja VISÍVEL na tela — heurística genérica
# (ver ressalva no topo do módulo: precisa validação contra os 2 casos reais).
_JS_BANNER = r"""()=>{
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return s.display!=='none' && s.visibility!=='hidden' && parseFloat(s.opacity||'1')>0 && r.width>5 && r.height>5; };
  const cands = [...document.querySelectorAll('[id*="essagem" i],[id*="essages" i],[id^="modal" i],.rf-mp-panel,.ui-dialog,.rich-messages')];
  for (const el of cands) { if (vis(el)) { const t=(el.innerText||'').trim(); if (t) return t; } }
  return '';
}"""


async def _baixar_pdf(ctx, url: str) -> bytes | None:
    try:
        resp = await ctx.request.get(url, timeout=60000)
        body = await resp.body()
        if body[:5] == b"%PDF-" and len(body) > 500:
            return body
    except Exception:  # noqa: BLE001
        pass
    return None


def _pdf_url(aba_url: str) -> str:
    from urllib.parse import parse_qs, unquote, urlparse
    if "pdfurl=" in (aba_url or ""):
        v = parse_qs(urlparse(aba_url).query).get("pdfurl", [""])[0]
        if v:
            return unquote(v)
    return aba_url


async def consultar_cef(conta: str, dv: str) -> dict:
    """Consulta o levantamento de depósito judicial na CEF pra uma Conta/DV
    (Agência e Operação são sempre as constantes fixas da Justiça Federal — ver
    `_CEF_AGENCIA_FIXA`/`_CEF_OPERACAO_FIXA`).

    Retorna um de:
      {"status": "pago", "pdf_bytes": b"..."}
      {"status": "sem_dados"}
      {"status": "captcha", "mensagem": "..."} — janela FICA na tela do desafio;
          resolva manualmente e chame de novo (mesma Conta/DV) depois.
      {"status": "indisponivel", "mensagem": "..."}
      {"status": "erro", "mensagem": "..."}
    """
    conta = re.sub(r"\D", "", conta or "")
    dv = re.sub(r"\D", "", dv or "")
    if not conta or not dv:
        return {"status": "erro", "mensagem": "Conta/DV inválidos (vazios)."}

    try:
        browser, page = await _connect()
    except Exception as e:  # noqa: BLE001
        return {"status": "erro", "mensagem": f"Falha ao abrir o navegador: {type(e).__name__}: {e}"[:300]}

    try:
        await page.goto(CEF_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector(_SEL_RADIO, timeout=15000)
        await page.click(_SEL_RADIO)
        await page.fill(_SEL_AGENCIA, _CEF_AGENCIA_FIXA)
        await page.select_option(_SEL_OPERACAO, _CEF_OPERACAO_FIXA)
        await page.fill(_SEL_CONTA, conta)
        await page.fill(_SEL_DV, dv)

        ctx = page.context
        antes = set(ctx.pages)
        await page.click(_SEL_CONSULTAR)

        desfecho, banner = None, ""
        for _ in range(30):  # ~15s
            await page.wait_for_timeout(500)
            novas = [p for p in ctx.pages if p not in antes]
            if novas:
                nova = novas[0]
                with contextlib.suppress(Exception):
                    await nova.wait_for_load_state("domcontentloaded", timeout=15000)
                pdf_bytes = await _baixar_pdf(ctx, _pdf_url(nova.url))
                with contextlib.suppress(Exception):
                    await nova.close()
                if pdf_bytes:
                    return {"status": "pago", "pdf_bytes": pdf_bytes}
                desfecho = "indisponivel"
                banner = "Abriu uma aba com o comprovante, mas não consegui baixar o PDF."
                break
            banner_raw = await page.evaluate(_JS_BANNER)
            cls = _classificar(page.url, _norm(banner_raw))
            if cls:
                desfecho, banner = cls, banner_raw
                break

        if desfecho == "sem_dados":
            return {"status": "sem_dados"}
        if desfecho == "captcha":
            return {
                "status": "captcha",
                "mensagem": "O site da CEF exigiu verificação de segurança (anti-robô). "
                            "Resolva manualmente na janela do navegador que abriu e clique "
                            "\"Tentar de novo\" no Painel.",
            }
        if desfecho == "indisponivel":
            return {"status": "indisponivel", "mensagem": banner or "Resposta não reconhecida do site da CEF."}
        return {"status": "indisponivel",
                "mensagem": "Nenhuma resposta do site da CEF em 15s (nem PDF, nem mensagem) — "
                            "pode ser bloqueio anti-robô silencioso. Tente de novo."}
    except Exception as e:  # noqa: BLE001
        return {"status": "erro", "mensagem": f"{type(e).__name__}: {e}"[:300]}
    finally:
        with contextlib.suppress(Exception):
            await browser.close()  # só a conexão CDP — o Chromium persistente continua


async def consultar_bb(conta: str, dv: str) -> dict:
    """BB ainda SEM caso real para aprender o formulário (o usuário confirmou: só
    tem exemplos da CEF até agora) — não tenta adivinhar campos num site
    bancário. Retorna sempre 'nao_suportado'; quando houver o primeiro caso real,
    este stub deve ser substituído pela automação de verdade, seguindo o mesmo
    padrão de `consultar_cef` acima."""
    return {
        "status": "nao_suportado",
        "mensagem": "Consulta automática ao Banco do Brasil ainda não implementada "
                    "(sem caso real para validar o formulário do site). Consulte "
                    "manualmente em https://www63.bb.com.br/portalbb/djo/id/resgate/"
                    "dadosResgate,802,4647,500828,0,1,1.bbx",
    }
