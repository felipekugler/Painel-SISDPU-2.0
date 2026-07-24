"""Cliente do e-proc (Justiça Federal — JFPR/TRF4) via Playwright — janela VISÍVEL
(headed) e PERSISTENTE (processo separado, conectado por CDP).

Espelha a camada de SESSÃO do `ingestao.sat_client`: o Chromium sobe como
**processo independente** com `--remote-debugging-port`; o painel apenas
**conecta** via `connect_over_cdp`. Assim a sessão do e-proc sobrevive a
reinícios do painel (comuns ao editar `.py`) — o usuário loga uma vez e segue.

O login (usuário/senha/2FA) é feito pelo usuário NA JANELA. Aqui só orquestramos
abrir a janela e verificar se a sessão está ativa.

IMPORTANTE: usa uma porta CDP e um perfil PRÓPRIOS (distintos do SAT), para as
duas janelas coexistirem sem conflito.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config

_log = logging.getLogger("eproc.client")

_PROFILE = Path(config.OFICIO_GERAL) / "_eproc_profile"

# Sistema alvo padrão: JFPR (1ª instância / Paraná). SSO é compartilhado com o
# TRF4, então logar aqui autentica os dois; para alternar, basta navegar.
EPROC_JFPR = "https://eproc.jfpr.jus.br/eprocV2/"
EPROC_TRF4 = "https://eproc.trf4.jus.br/"
EPROC_HOME = EPROC_JFPR

# Perfil a selecionar na tela "Seleção de perfil" do SSO (entrar_sso). O painel de
# prazos do defensor é o "Painel do Procurador" → perfil PROCURADOR (não o de
# gestão "GERENTE PROCURADORIA"). Comparação por texto EXATO normalizado.
PERFIL_PADRAO = "PROCURADOR"

# Clica o botão de perfil cujo texto casa EXATAMENTE (normalizado) o alvo. Restringe
# aos itens da lista de perfis (id "tr0", "tr1", …) para não pegar outros controles.
_JS_CLICAR_PERFIL = r"""(alvo)=>{
  const norm=s=>(s||'').replace(/\s+/g,' ').trim().toUpperCase();
  const els=[...document.querySelectorAll('[id^="tr"]')]
    .filter(e=>/^tr\d+$/.test(e.id));
  const el=els.find(e=>norm(e.innerText)===norm(alvo));
  if(el){el.click(); return true;} return false;
}"""

# Porta CDP DEDICADA ao e-proc (o SAT usa a 9222).
_CDP_PORT = 9223

_pw = None
_eproc_pid: int | None = None  # PID do Chromium do e-proc (p/ fechá-lo no shutdown)


def _porta_aberta(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_na_porta(port: int) -> int | None:
    """PID do processo LISTENING na porta (via netstat) — cobre o caso de o
    Chromium ter sobrevivido a um restart do painel (PID lançado desconhecido)."""
    with contextlib.suppress(Exception):
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                return int(line.split()[-1])
    return None


def fechar_navegador() -> None:
    """Fecha o Chromium do e-proc (chamado no SHUTDOWN do painel). Mata a árvore
    de processos pelo PID lançado ou, se desconhecido, pelo dono da porta CDP. Não
    roda em kill -9 do painel (só em encerramento gracioso), então reinícios de dev
    preservam a sessão."""
    global _eproc_pid
    if not _porta_aberta(_CDP_PORT):
        _eproc_pid = None
        return
    pid = _eproc_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _eproc_pid = None


async def _pw_start():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _ensure_chromium(url: str = EPROC_HOME) -> None:
    """Garante um Chromium persistente rodando com remote-debugging (processo
    separado). Lança-o apontando já para o e-proc (o usuário loga na janela)."""
    global _eproc_pid
    if _porta_aberta(_CDP_PORT):
        return
    pw = await _pw_start()
    exe = pw.chromium.executable_path
    _PROFILE.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP → sobrevive ao painel
        flags = 0x00000008 | 0x00000200
    proc = subprocess.Popen(
        [exe,
         f"--remote-debugging-port={_CDP_PORT}",
         f"--user-data-dir={_PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=PDFViewerUpdate,PdfUnseasoned",
         url],
        creationflags=flags, close_fds=True,
    )
    _eproc_pid = proc.pid  # p/ fechar o navegador no shutdown do painel
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)  # respiro para o CDP subir de fato
            return
        time.sleep(0.5)


async def _connect(url: str = EPROC_HOME):
    """Conecta ao Chromium persistente e devolve (browser, page). Chame
    `browser.close()` ao fim — fecha só a CONEXÃO CDP, não o Chromium."""
    await _ensure_chromium(url)
    pw = await _pw_start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return browser, page


def _em_sso(u: str) -> bool:
    """True se a URL é da área EXTERNA/SSO: tela de login ou seleção de perfil.

    Atenção: `externo_controlador.php` CONTÉM o substring `controlador.php`, então a
    checagem de "logado" precisa excluir explicitamente a área externa — senão a
    tela de seleção de perfil seria confundida com sessão ativa."""
    return (("externo_controlador" in u) or ("entrar_sso" in u)
            or ("eproc-sso" in u) or ("/login" in u))


def _status_dict(url: str) -> dict:
    """Deduz se a sessão do e-proc está ativa a partir da URL.

    Logado: a navegação chega ao `controlador.php` INTERNO (área autenticada) e NÃO
    está na área externa/SSO. A tela de "Seleção de perfil" (entrar_sso) conta como
    NÃO logado — ainda falta escolher o perfil (feito automaticamente por `abrir()`).
    """
    u = (url or "").lower()
    logado = ("controlador.php" in u) and not _em_sso(u)
    return {
        "logado": logado,
        "url": url,
        "msg": ("Sessão pronta no e-proc." if logado
                else "Faça o login na janela do e-proc (usuário, senha e 2FA)."),
    }


# --- Auto-login no SSO Keycloak (eproc-sso) --------------------------------
# A tela de login do e-proc é um Keycloak: campo #username, senha #password
# (+ input[name=password]) e botão #kc-login. Sem CAPTCHA. Só cai aqui quando a
# sessão SSO expirou; com o 2FA memorizado no perfil, após usuário/senha o SSO
# não repede o 2FA e segue direto para a seleção de perfil.

_JS_TEM_LOGIN = "()=>!!document.querySelector('#username, input[name=username]')"

# Preenche usuário/senha via DOM (set value + eventos) — cobre o campo de senha
# visível (id=password) e o oculto (name=password) do tema do Keycloak.
_JS_FILL_LOGIN = r"""(a)=>{
  const [user, pass] = a;
  const setV=(el,v)=>{ if(!el) return false; try{el.focus();}catch(e){}
    el.value=v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true})); return true; };
  const u=document.querySelector('#username, input[name=username]');
  const oku=setV(u,user);
  let okp=false;
  document.querySelectorAll('#password, input[name=password]').forEach(el=>{ if(setV(el,pass)) okp=true; });
  return oku && okp;
}"""

_JS_SUBMIT_LOGIN = r"""()=>{
  const b=document.querySelector('#kc-login');
  if(b){ b.click(); return true; }
  const f=document.querySelector('form');
  if(f){ f.submit(); return true; }
  return false;
}"""


async def _preencher_login(page, usuario: str, senha: str) -> bool:
    """Se a tela de login (Keycloak) está presente, preenche e envia. Retorna True
    se enviou. No-op (False) se não houver campo de usuário."""
    tem = False
    with contextlib.suppress(Exception):
        tem = await page.evaluate(_JS_TEM_LOGIN)
    if not tem:
        return False
    ok = False
    with contextlib.suppress(Exception):
        ok = await page.evaluate(_JS_FILL_LOGIN, [usuario or "", senha or ""])
    if not ok:
        return False
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_SUBMIT_LOGIN)
    # aguarda o form de login sumir (redirecionamento pós-login)
    for _ in range(30):  # ~15s
        await page.wait_for_timeout(500)
        with contextlib.suppress(Exception):
            if not await page.evaluate(_JS_TEM_LOGIN):
                break
    await page.wait_for_timeout(1200)
    return True


async def _selecionar_perfil(page, perfil: str = PERFIL_PADRAO) -> bool:
    """Se a página está na "Seleção de perfil" (entrar_sso), clica o perfil alvo e
    aguarda cair na área interna. Retorna True se, ao fim, a sessão está logada.

    Funciona quando o 2FA está memorizado e a sessão SSO segue viva: o "login" se
    reduz a escolher o perfil. Se a sessão SSO expirou (tela de usuário/senha), não
    há botão de perfil — retorna False e o usuário loga manualmente na janela."""
    if not _em_sso((page.url or "").lower()):
        return _status_dict(page.url)["logado"]
    clicou = False
    with contextlib.suppress(Exception):
        clicou = await page.evaluate(_JS_CLICAR_PERFIL, perfil)
    if not clicou:
        return False
    for _ in range(20):  # ~10s aguardando a navegação p/ a área interna
        await page.wait_for_timeout(500)
        if _status_dict(page.url)["logado"]:
            return True
    return _status_dict(page.url)["logado"]


async def abrir(url: str = EPROC_HOME, usuario: str = "", senha: str = "") -> dict:
    """Abre/traz à frente a janela do e-proc e tenta ENTRAR sozinho, em cadeia:

      1) tela de login (Keycloak) → se `usuario`/`senha` vierem (cofre destrancado),
         preenche e envia; senão, para aqui (login manual na janela).
      2) 2FA → pulado quando memorizado no perfil; se pedir, o usuário resolve na janela.
      3) seleção de perfil → escolhe PROCURADOR automaticamente.

    Retorna o status final (logado?/url/msg)."""
    browser, page = await _connect(url)
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        with contextlib.suppress(Exception):
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)

        # 1) login Keycloak (se a sessão SSO expirou) — só com credenciais do cofre
        if usuario and senha:
            for _ in range(16):  # ~8s aguardando o form de login aparecer/estabilizar
                if _status_dict(page.url)["logado"]:
                    break
                u = (page.url or "").lower()
                if ("externo_controlador" in u) or ("entrar_sso" in u):
                    break  # já passou o login → é seleção de perfil
                tem_login = False
                with contextlib.suppress(Exception):
                    tem_login = await page.evaluate(_JS_TEM_LOGIN)
                if tem_login:
                    await _preencher_login(page, usuario, senha)
                    break
                await page.wait_for_timeout(500)

        # 2) seleção de perfil → PROCURADOR (no-op se já dentro ou na tela de senha)
        with contextlib.suppress(Exception):
            await _selecionar_perfil(page)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def status(url: str = EPROC_HOME) -> dict:
    """Verifica se a sessão está ativa navegando para o e-proc (deixa a tela
    pronta)."""
    browser, page = await _connect(url)
    try:
        with contextlib.suppress(Exception):
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def peek() -> dict:
    """Status LEVE p/ polling automático: só LÊ a URL das abas abertas — NÃO
    navega e NÃO abre o Chromium (se a janela estiver fechada, retorna 'não
    conectado'). Assim o selo atualiza sozinho sem recarregar a janela nem
    atrapalhar login/2FA."""
    if not _porta_aberta(_CDP_PORT):
        return {"logado": False, "url": "",
                "msg": "Janela do e-proc fechada — clique em 'Abrir e-proc / Login'."}
    try:
        pw = await _pw_start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception:  # noqa: BLE001
        return {"logado": False, "url": "", "msg": "Janela do e-proc indisponível."}
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        pages = list(ctx.pages) if ctx else []
        for p in pages:  # logado se ALGUMA aba está na área autenticada
            d = _status_dict(p.url or "")
            if d["logado"]:
                return d
        return _status_dict(pages[0].url if pages else "")
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
