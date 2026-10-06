"""Cliente do e-Sisrec (recursos administrativos do INSS / CRPS) via Playwright —
janela VISÍVEL, processo separado conectado por CDP (mesmo padrão de
`ingestao.sd_client` / `ingestao.eproc_client`).

Usado pela caixa "Consultar e-Sisrec" do cabeçalho (ao lado das buscas de PAJ e
processo): abre o processo de recurso numa ABA NOVA da janela do e-Sisrec, para
o Defensor ver — não lê nem grava nada no painel.

Login: GERID/CAS do INSS (geridinss.dataprev.gov.br), MESMAS credenciais do SAT
Central (sistema "sat" do cofre). Sem 2FA neste CAS (informado pelo usuário
2026-10-06), mas o tratamento de 2FA do `sd_client` é mantido por segurança
caso passe a ser exigido. Cofre trancado → login manual na janela.

Pesquisa: caixa "CPF / NB / Protocolo" no canto superior direito + botão de
busca rápida, já logado.

Ao contrário do SD (que fecha o navegador ao fim de cada dossiê), a janela
fica ABERTA — o objetivo é justamente o usuário ver o processo. Fechada só no
shutdown do painel (`fechar_navegador`)."""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config
from ingestao import sat_client

_PROFILE = Path(config.OFICIO_GERAL) / "_esisrec_profile"
_CDP_PORT = 9226  # SAT=9222, e-proc=9223, RPV=9224, SD=9225

ESISREC_HOME = "https://esisrec.inss.gov.br/esisrec/"
# Porta de entrada informada pelo usuário (2026-10-06): o CAS do GERID-INSS já
# com o serviço do e-Sisrec — logado, redireciona direto para o sistema.
ESISREC_LOGIN_URL = ("https://geridinss.dataprev.gov.br/cas/login"
                     "?service=https%3A%2F%2Fesisrec.inss.gov.br%2Fesisrec%2F")

_esisrec_pid: int | None = None


def _porta_aberta(port: int) -> bool:
    import socket

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
    """Fecha o Chromium do e-Sisrec — só no shutdown do painel."""
    global _esisrec_pid
    if not _porta_aberta(_CDP_PORT):
        _esisrec_pid = None
        return
    pid = _esisrec_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _esisrec_pid = None


async def _ensure_chromium(pw) -> None:
    global _esisrec_pid
    if _porta_aberta(_CDP_PORT):
        return
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
         "--disable-extensions",
         "about:blank"],
        creationflags=flags, close_fds=True,
    )
    _esisrec_pid = proc.pid
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)
            return
        time.sleep(0.5)


def _logado(url: str) -> bool:
    u = (url or "").lower()
    return "esisrec.inss.gov.br" in u and "/cas/" not in u and "gerid" not in u


async def _garantir_login(page, usuario: str, senha: str, timeout_s: int = 180) -> bool:
    """`page` já navegou para o e-Sisrec; se caiu no CAS, preenche com as
    credenciais do SAT (cofre) — ou espera o login manual na janela."""
    if _logado(page.url):
        return True
    login_enviado = False
    tentativas_2fa = 0
    ini = time.monotonic()
    while time.monotonic() - ini < timeout_s:
        if _logado(page.url):
            return True
        with contextlib.suppress(Exception):
            if (not login_enviado and usuario and senha
                    and await page.evaluate(sat_client._JS_TEM_LOGIN_GERID)):
                await sat_client._preencher_login(page, usuario, senha)
                login_enviado = True
                continue
            if tentativas_2fa < 3 and await page.evaluate(sat_client._JS_TEM_2FA_GERID):
                tentativas_2fa += 1
                await sat_client._preencher_2fa(page)
                continue
        await page.wait_for_timeout(1000)
    return _logado(page.url)


# Caixa "CPF / NB / Protocolo" do cabeçalho do e-Sisrec + botão de busca rápida
# ao lado (o primeiro botão/link clicável no mesmo contêiner do campo).
_JS_ACHAR_BUSCA = r"""()=>{
  const ok=el=>el && el.offsetParent!==null;
  const ins=[...document.querySelectorAll('input[type=text], input:not([type]), input[type=search]')].filter(ok);
  const campo=ins.find(i=>/cpf.*nb|protocolo/i.test((i.placeholder||'')+' '+(i.title||'')+' '+(i.getAttribute('aria-label')||'')));
  if(!campo) return false;
  document.querySelectorAll('[data-esisrec-busca]').forEach(e=>e.removeAttribute('data-esisrec-busca'));
  campo.setAttribute('data-esisrec-busca','campo');
  let c=campo.parentElement;
  for(let n=0; c && n<4; n++, c=c.parentElement){
    const b=[...c.querySelectorAll('button, a, input[type=submit], input[type=image]')].filter(ok)
      .find(x=>x!==campo);
    if(b){ b.setAttribute('data-esisrec-busca','botao'); break; }
  }
  return true;
}"""


async def abrir_processo(numero: str, usuario: str = "", senha: str = "",
                         espera_login_s: int = 180) -> dict:
    """Abre o processo de recurso `numero` (protocolo/CPF/NB) na janela do
    e-Sisrec, logando antes se preciso (credenciais do SAT ou login manual em
    até `espera_login_s`). A aba fica aberta."""
    numero_limpo = re.sub(r"\D", "", numero or "")
    if not numero_limpo:
        return {"ok": False, "erro": "Número vazio."}
    pw = await async_playwright().start()
    browser = None
    try:
        await _ensure_chromium(pw)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Reaproveita UMA aba: a do e-Sisrec/GERID já aberta (sem recarregar a
        # tela de login, onde o usuário pode estar digitando) ou a em branco
        # inicial. Nunca abre uma aba nova a cada pesquisa enquanto não logado
        # (o teste de 2026-10-06 empilhou ~12 abas de login assim).
        page = (next((p for p in ctx.pages if "esisrec" in p.url or "geridinss" in p.url), None)
                or next((p for p in ctx.pages if p.url in ("about:blank", "")), None)
                or await ctx.new_page())
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        await _maximizar(page)
        if "/cas/" not in page.url:
            await page.goto(ESISREC_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
        # Cofre trancado → espera o login manual na janela e segue sozinho.
        if not await _garantir_login(page, usuario, senha, timeout_s=espera_login_s):
            return {"ok": False, "erro": "Não houve login no e-Sisrec (GERID) — faça o login na "
                                         "janela do e-Sisrec e pesquise de novo."}
        await page.wait_for_load_state("domcontentloaded", timeout=30000)

        achou = False
        for _ in range(20):
            with contextlib.suppress(Exception):
                achou = await page.evaluate(_JS_ACHAR_BUSCA)
            if achou:
                break
            await page.wait_for_timeout(500)
        if not achou:
            return {"ok": False, "erro": "Caixa de pesquisa 'CPF / NB / Protocolo' não encontrada no e-Sisrec.",
                    "url": page.url}
        campo = page.locator("[data-esisrec-busca=campo]")
        await campo.click()
        await campo.fill(numero_limpo)
        botao = page.locator("[data-esisrec-busca=botao]")
        if await botao.count():
            await botao.first.click()
        else:
            await campo.press("Enter")
        # A busca rápida navega (JSF) para pages/processo/manter_processo.xhtml
        # — confirmado ao vivo 2026-10-06; sem esperar, a URL devolvida ainda
        # era a do painel de entrada.
        with contextlib.suppress(Exception):
            await page.wait_for_url(re.compile(r"/pages/processo/"), timeout=30000)
        await page.wait_for_timeout(500)
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        return {"ok": True, "url": page.url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()


async def _maximizar(page) -> None:
    from ingestao import eproc_client

    await eproc_client._maximizar_janela(page)
