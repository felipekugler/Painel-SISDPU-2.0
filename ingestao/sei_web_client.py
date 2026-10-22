"""Janela do SEI controlada pelo painel (22/10/2026) — "Abrir no SEI" /
"Abrir para assinar" dos ofícios (partials/oficio_sei.html).

Antes, o botão era um link comum: abria o SEI no navegador de uso do Defensor,
que pedia o login a cada vez. Agora o documento abre numa janela PRÓPRIA do SEI
(Chromium separado, porta CDP dedicada — mesmo padrão do e-Sisrec/e-proc) e,
se cair na tela de login do SIP, o painel preenche usuário, senha e órgão (DPU)
com as credenciais do SISDPU no cofre (mesmo login de rede da DPU) e entra. O
SEI guarda o destino durante o login e cai direto no documento.

A ASSINATURA continua sendo ato do Defensor, na própria janela: o painel não
toca na senha da assinatura. Cofre trancado → a janela abre na tela de login,
para o login manual. A janela fica aberta; fechada só no shutdown do painel."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config

_PROFILE = Path(config.OFICIO_GERAL) / "_sei_profile"
_CDP_PORT = 9228 + config.CDP_OFFSET  # SAT=9222, e-proc=9223, RPV=9224, SD=9225, e-Sisrec=9226, CadÚnico=9227

SEI_WEB = "https://sei.dpu.def.br/sei/"
ORGAO_LOGIN = "DPU-CTB"  # "Órgão" do login do SIP (unidade de Curitiba; existe também "DPU")

_sei_pid: int | None = None


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
    """Fecha o Chromium do SEI — só no shutdown do painel."""
    global _sei_pid
    if not _porta_aberta(_CDP_PORT):
        _sei_pid = None
        return
    pid = _sei_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _sei_pid = None


async def _ensure_chromium(pw) -> None:
    global _sei_pid
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
         "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
         "--start-maximized",
         "about:blank"],
        creationflags=flags, close_fds=True,
    )
    _sei_pid = proc.pid
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)
            return
        time.sleep(0.5)


def _no_login(url: str) -> bool:
    return "/sip/login.php" in (url or "").lower()


def _no_sei(url: str) -> bool:
    u = (url or "").lower()
    return "sei.dpu.def.br/sei/" in u and not _no_login(u)


# O campo de senha do SIP usa o MaskedPassword do framework Infra (o mesmo do
# login nativo do e-proc): o #pwdSenha visível mostra "●" e a senha real fica
# num campo com name="pwdSenha". Preenchimento direto é desfeito; só a
# digitação de verdade alimenta o campo real.
_JS_SENHA_REAL = """() => { const c = [...document.querySelectorAll('input[name=pwdSenha]')];
    const real = c.find(e => e.type === 'hidden') || c[c.length - 1];
    return real ? real.value : null; }"""


async def _entrar(page, usuario: str, senha: str) -> bool:
    """Tela de login do SIP: usuário, senha, órgão (DPU-CTB) e "Acessar".
    Espera a página assentar (um script de carga devolve o foco ao usuário
    depois de 500 ms), preenche o usuário, DIGITA a senha no campo mascarado e
    confere a senha real, o usuário e o órgão antes de clicar em Acessar. Sem
    conferir, não envia: a tela fica para o Defensor completar. True se
    enviou."""
    with contextlib.suppress(Exception):
        await page.wait_for_selector("#pwdSenha", timeout=15000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load", timeout=15000)
    await page.wait_for_timeout(900)

    ok = False
    for _ in range(3):
        with contextlib.suppress(Exception):
            await page.locator("#txtUsuario").fill(usuario)
            await page.select_option("#selOrgao", label=ORGAO_LOGIN)
            await page.wait_for_timeout(600)  # o foco automático da página já passou
            campo = page.locator("#pwdSenha").first
            await campo.click()
            await campo.press("Control+A")
            await campo.press("Delete")
            await page.keyboard.type(senha, delay=25)
            await page.wait_for_timeout(300)
            ok = (await page.evaluate(_JS_SENHA_REAL) == senha
                  and await page.locator("#txtUsuario").input_value() == usuario
                  and await page.evaluate("() => document.querySelector('#selOrgao')"
                                          ".selectedOptions[0].textContent.trim()") == ORGAO_LOGIN)
        if ok:
            break
        await page.wait_for_timeout(700)
    if not ok:
        return False
    with contextlib.suppress(Exception):
        await page.click("#sbmAcessar")
    with contextlib.suppress(Exception):
        await page.wait_for_url(lambda u: not _no_login(u), timeout=30000)
    return True


async def _a_frente(page) -> None:
    """Janela MAXIMIZADA e à frente das outras (22/10/2026: abria minimizada
    ou atrás do navegador do painel). `bring_to_front` só ativa a aba; o
    Windows não deixa um processo em segundo plano roubar o foco — passar por
    "minimizada" e voltar a "maximizada" faz a janela reaparecer por cima."""
    with contextlib.suppress(Exception):
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        wid = info.get("windowId")
        if wid is not None:
            for estado in ("minimized", "normal", "maximized"):
                with contextlib.suppress(Exception):
                    await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": estado}})
                await page.wait_for_timeout(150)
    with contextlib.suppress(Exception):
        await page.bring_to_front()


async def abrir_documento(url: str, usuario: str = "", senha: str = "") -> dict:
    """Abre `url` (link de documento do SEI) na janela do SEI, entrando sozinho
    se cair no login e houver credenciais. A janela fica aberta e à frente."""
    if not (url or "").startswith(SEI_WEB):
        return {"ok": False, "erro": "Endereço que não é do SEI da DPU."}
    pw = await async_playwright().start()
    browser = None
    try:
        await _ensure_chromium(pw)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # Uma aba só: a do SEI/SIP já aberta ou a em branco inicial.
        page = (next((p for p in ctx.pages if "dpu.def.br" in (p.url or "")), None)
                or next((p for p in ctx.pages if (p.url or "") in ("about:blank", "")), None)
                or await ctx.new_page())
        await _a_frente(page)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        entrou = False
        if _no_login(page.url):
            if not (usuario and senha):
                await _a_frente(page)
                return {"ok": True, "login_manual": True, "url": page.url}
            entrou = await _entrar(page, usuario, senha)
            if _no_login(page.url):
                await _a_frente(page)
                return {"ok": True, "login_manual": True, "url": page.url,
                        "aviso": "O SEI não aceitou o login automático — confira a janela do SEI."}
            # O SEI costuma guardar o destino; se não guardou, vai ao documento.
            if "id_documento=" in url and "id_documento=" not in (page.url or ""):
                with contextlib.suppress(Exception):
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await _a_frente(page)
        return {"ok": True, "login_automatico": entrou, "url": page.url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()
