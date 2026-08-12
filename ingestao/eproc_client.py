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
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config

_log = logging.getLogger("eproc.client")

_PROFILE = Path(config.OFICIO_GERAL) / "_eproc_profile"

# --- TOTP (2FA) automático via a mesma seed já cadastrada pro MCP eproc-trf4 -
# O MCP (C:\Claude\MCP\eproc-trf4, ver auth.py) guarda uma seed TOTP base32 no
# Credential Manager do Windows pra resolver o 2FA sozinho no login HTTP dele.
# É o MESMO usuário do SO, então dá pra ler essa MESMA seed aqui (nunca
# duplicada/copiada) pra também resolver o 2FA quando ele aparecer no login
# pela JANELA do navegador — mesma conta, mesmo segredo.
_TOTP_CRED_KEY = "mcp-eproc-trf4"
_TOTP_ITEM_KEY = "totp_seed"


def _gerar_codigo_totp() -> str | None:
    """Código TOTP atual (6 dígitos), lido da seed do MCP eproc-trf4. Retorna
    None se a seed não estiver cadastrada ou keyring/pyotp faltarem — nesse
    caso o 2FA continua manual, como sempre foi."""
    try:
        import keyring
        import pyotp
    except ImportError:
        return None
    try:
        seed = keyring.get_password(_TOTP_CRED_KEY, _TOTP_ITEM_KEY)
        if not seed:
            return None
        return pyotp.TOTP(seed).now()
    except Exception:
        return None

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


async def _pw_reiniciar() -> None:
    """Descarta o processo-driver do Playwright (Node) e força reinício na
    próxima `_pw_start()`. Necessário porque um diálogo JS nativo do e-proc
    (alert/confirm) pode, por um bug do próprio driver, derrubar o processo
    Node inteiro com uma "unhandled promise rejection" — depois disso, o
    singleton `_pw` fica apontando pra um driver morto e TODA chamada futura
    de `connect_over_cdp` falha com "Connection closed while reading from the
    driver", mesmo com o Chromium (a janela do e-proc) saudável na porta CDP."""
    global _pw
    if _pw is not None:
        with contextlib.suppress(Exception):
            await _pw.stop()
        _pw = None


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
    try:
        pw = await _pw_start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception as exc:
        # Driver Node morto (ex.: crash por diálogo JS não tratado — ver
        # _pw_reiniciar). Reinicia o driver e tenta mais uma vez antes de
        # desistir; sem isso, o painel fica falhando em TODA chamada de
        # e-proc até ser reiniciado manualmente.
        if "Connection closed while reading from the driver" not in str(exc):
            raise
        _log.warning("driver do Playwright (e-proc) morreu — reiniciando: %s", exc)
        await _pw_reiniciar()
        pw = await _pw_start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    if not getattr(page, "_eproc_dialog_hook", False):
        # Handler explícito de diálogo: dispensa qualquer alert/confirm nativo
        # assim que ele abre. Sem isso, o Playwright cai no fallback "sem
        # listener" (auto-dismiss por timeout) — e se o diálogo já tiver
        # fechado por outro caminho nesse meio-tempo, o driver Node crasha
        # (bug conhecido do driver, "No dialog is showing").
        page.on("dialog", _dispensar_dialogo)
        page._eproc_dialog_hook = True
    return browser, page


async def _dispensar_dialogo(dialog) -> None:
    with contextlib.suppress(Exception):
        await dialog.dismiss()


async def _maximizar_janela(page) -> None:
    """Garante que a janela (SO) do Chromium esteja maximizada — `bring_to_front`
    do Playwright só ativa a ABA dentro do navegador, não restaura uma janela
    minimizada do SO (limitação conhecida do CDP em alto nível). Usa o
    protocolo CDP puro (`Browser.getWindowForTarget`/`setWindowBounds`).
    Chromium não aceita ir direto de "minimized" para "maximized" — por isso
    passa por "normal" primeiro. Silencioso em qualquer falha (best-effort;
    nunca deve quebrar o fluxo de abrir o processo)."""
    with contextlib.suppress(Exception):
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        window_id = info.get("windowId")
        if window_id is not None:
            with contextlib.suppress(Exception):
                await cdp.send("Browser.setWindowBounds",
                                {"windowId": window_id, "bounds": {"windowState": "normal"}})
            await cdp.send("Browser.setWindowBounds",
                            {"windowId": window_id, "bounds": {"windowState": "maximized"}})


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


# --- Auto-login: SSO Keycloak (eproc-sso) OU tela nativa do eproc ----------
# Duas telas possíveis quando a sessão expirou:
#  a) Keycloak (SSO expirado): campo #username, senha #password
#     (+ input[name=password]) e botão #kc-login.
#  b) Nativa do eproc (sessão totalmente encerrada / cookie do IdP perdido):
#     campo #txtUsuario; a senha tem DOIS campos irmãos — #pwdSenha (type=text,
#     name="", classe "masked", é o que aparece VISÍVEL na tela e o eye-icon
#     alterna) e o REAL input[name=pwdSenha] (type=password, display:none — é
#     o único com `name`, logo o único que de fato vai no submit). O visível
#     tem um listener JS de máscara ligado a digitação REAL (char a char) que
#     espelha cada tecla no oculto; setar `.value` inteiro de uma vez via script
#     confunde esse listener (ele substitui o valor inteiro por "●●●" sem
#     repassar nada pro campo oculto). Por isso a senha nativa exige digitação
#     simulada de verdade (Playwright `keyboard.type`), não um `.value=`.
#     Botão "Entrar" não tem id (achado pelo texto do <button>).
# Nenhuma tem CAPTCHA. Com o 2FA memorizado no perfil, após usuário/senha segue
# direto (Keycloak → seleção de perfil; nativa → área interna ou pedido de 2FA,
# que fica para o usuário completar manualmente na janela).

_JS_TEM_LOGIN = ("()=>!!document.querySelector("
                 "'#username, input[name=username], #txtUsuario, input[name=txtUsuario]')")

_JS_FILL_USUARIO = r"""(user)=>{
  const setV=(el,v)=>{ if(!el) return false; try{el.focus();}catch(e){}
    el.value=v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true})); return true; };
  const u=document.querySelector('#username, input[name=username], #txtUsuario, input[name=txtUsuario]');
  return setV(u, user);
}"""

# Campo de senha "simples" (Keycloak) — value-set direto funciona, sem máscara
# JS por trás. Não mexe no #pwdSenha nativo do eproc (esse exige digitação real,
# tratado à parte em `_preencher_login`).
_JS_FILL_SENHA_SIMPLES = r"""(pass)=>{
  const setV=(el,v)=>{ if(!el) return false; try{el.focus();}catch(e){}
    el.value=v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true})); return true; };
  let ok=false;
  document.querySelectorAll('#password, input[name=password]').forEach(el=>{ if(setV(el,pass)) ok=true; });
  return ok;
}"""

# Lê de volta o valor do campo REAL de senha (o oculto, name=pwdSenha, ou o do
# Keycloak) — usado pra CONFERIR se o fill/digitação realmente chegou lá.
_JS_LER_SENHA_REAL = ("()=>{const el=document.querySelector("
                      "'#password, input[name=password], input[name=pwdSenha]');"
                      "return el ? el.value : null;}")

_JS_SUBMIT_LOGIN = r"""()=>{
  const b=document.querySelector('#kc-login');
  if(b){ b.click(); return true; }
  const entrar=[...document.querySelectorAll('button')]
    .find(el=>/entrar/i.test((el.innerText||'').trim()));
  if(entrar){ entrar.click(); return true; }
  const f=document.querySelector('form');
  if(f){ f.submit(); return true; }
  return false;
}"""


async def _preencher_login(page, usuario: str, senha: str) -> bool:
    """Se a tela de login (Keycloak ou nativa do eproc) está presente, preenche e
    envia. Retorna True se enviou. No-op (False) se não houver campo de usuário."""
    tem = False
    with contextlib.suppress(Exception):
        tem = await page.evaluate(_JS_TEM_LOGIN)
    if not tem:
        return False

    with contextlib.suppress(Exception):
        await page.evaluate(_JS_FILL_USUARIO, usuario or "")

    ok = False
    for _ in range(3):
        # 1) campo "simples" (Keycloak) — value-set direto
        with contextlib.suppress(Exception):
            ok = await page.evaluate(_JS_FILL_SENHA_SIMPLES, senha or "")
        # 2) campo nativo do eproc (mascarado por JS) — precisa de digitação real
        if not ok:
            with contextlib.suppress(Exception):
                campo = page.locator("#pwdSenha")
                if await campo.count() > 0:
                    await campo.click()
                    await campo.press("Control+A")
                    await campo.press("Delete")
                    await page.keyboard.type(senha or "", delay=25)
                    ok = True
        await page.wait_for_timeout(300)
        atual = None
        with contextlib.suppress(Exception):
            atual = await page.evaluate(_JS_LER_SENHA_REAL)
        if ok and atual == (senha or ""):
            break
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


# --- 2FA (TOTP) — campo txtAcessoCodigo, ver auth.py do MCP pros nomes reais -
_JS_TEM_2FA = ("()=>!!document.querySelector("
               "'#txtAcessoCodigo, input[name=txtAcessoCodigo]')")

_JS_FILL_2FA = r"""(codigo)=>{
  const setV=(el,v)=>{ if(!el) return false; try{el.focus();}catch(e){}
    el.value=v;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true})); return true; };
  const campo=document.querySelector('#txtAcessoCodigo, input[name=txtAcessoCodigo]');
  const ok=setV(campo, codigo);
  // marca "confiar neste dispositivo" — reduz a chance de pedir 2FA de novo
  const chk=document.querySelector('#chkLiberarDispositivo, input[name=chkLiberarDispositivo]');
  if(chk && !chk.checked){ chk.checked=true; chk.dispatchEvent(new Event('change',{bubbles:true})); }
  return ok;
}"""

_JS_LER_2FA = ("()=>{const el=document.querySelector("
               "'#txtAcessoCodigo, input[name=txtAcessoCodigo]');"
               "return el ? el.value : null;}")

_JS_SUBMIT_2FA = r"""()=>{
  const b=[...document.querySelectorAll('button')]
    .find(el=>/confirmar|validar|entrar/i.test((el.innerText||'').trim()));
  if(b){ b.click(); return true; }
  const f=document.querySelector('form');
  if(f){ f.submit(); return true; }
  return false;
}"""


async def _preencher_2fa(page) -> bool:
    """Se a tela de 2FA (código TOTP) está presente, gera o código a partir da
    seed do MCP eproc-trf4 e preenche/envia sozinho. Retorna True se enviou.
    No-op (False) se não houver a tela ou a seed não estiver cadastrada —
    nesses casos o 2FA continua manual, como sempre foi."""
    tem = False
    with contextlib.suppress(Exception):
        tem = await page.evaluate(_JS_TEM_2FA)
    if not tem:
        return False
    codigo = _gerar_codigo_totp()
    if not codigo:
        return False

    ok = False
    for _ in range(3):
        with contextlib.suppress(Exception):
            ok = await page.evaluate(_JS_FILL_2FA, codigo)
        await page.wait_for_timeout(300)
        atual = None
        with contextlib.suppress(Exception):
            atual = await page.evaluate(_JS_LER_2FA)
        if ok and atual == codigo:
            break
        # código pode ter virado entre tentativas (janela de 30s do TOTP) —
        # gera de novo antes de reenviar, pra não submeter um código expirado.
        codigo = _gerar_codigo_totp() or codigo
    if not ok:
        return False
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_SUBMIT_2FA)
    for _ in range(20):  # ~10s aguardando sair da tela de 2FA
        await page.wait_for_timeout(500)
        with contextlib.suppress(Exception):
            if not await page.evaluate(_JS_TEM_2FA):
                break
    await page.wait_for_timeout(1000)
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

      1) tela de login (Keycloak ou nativa) → se `usuario`/`senha` vierem (cofre
         destrancado), preenche e envia; senão, para aqui (login manual na janela).
      2) 2FA → resolvido sozinho se a seed TOTP do MCP eproc-trf4 estiver
         cadastrada (ver `_gerar_codigo_totp`); senão, pedido quando não
         memorizado no perfil — o usuário resolve na janela.
      3) seleção de perfil → escolhe PROCURADOR automaticamente.

    Retorna o status final (logado?/url/msg)."""
    browser, page = await _connect(url)
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        await _maximizar_janela(page)
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

        # 2) 2FA — resolve sozinho se a tela aparecer e a seed estiver cadastrada
        with contextlib.suppress(Exception):
            await _preencher_2fa(page)

        # 3) seleção de perfil → PROCURADOR (no-op se já dentro ou na tela de senha)
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


# Clica o link <a> cujo texto (normalizado) casa EXATO com o alvo — usado para
# navegar por elementos REAIS da página (nunca montar as URLs com hash= por conta
# própria: são assinadas e de uso único por sessão, ver nota em eproc_intimacoes.py).
_JS_CLICAR_LINK_EXATO = r"""(alvo)=>{
  const norm=s=>(s||'').replace(/\s+/g,' ').trim();
  const el=[...document.querySelectorAll('a')].find(a=>norm(a.innerText)===alvo);
  if(el){el.click(); return true;} return false;
}"""

# Extrai a lista de eventos do popup "Listar Todos" (ou do autocomplete) do campo
# "Evento a ser lançado:" — pega o MAIOR <ul> visível da tela (o menu lateral e
# outros widgets também são <ul>, mas nunca chegam a ter tantos itens quanto o
# catálogo de eventos de uma classe judicial).
_JS_EXTRAIR_EVENTOS = r"""()=>{
  const uls=[...document.querySelectorAll('ul')].filter(u=>u.offsetParent && u.id!=='main-menu');
  let melhor=[];
  for(const u of uls){
    const itens=[...u.children].map(li=>(li.innerText||'').trim()).filter(Boolean);
    if(itens.length>melhor.length) melhor=itens;
  }
  return melhor;
}"""


async def _abrir_tela_peticionamento(page, numero: str) -> str:
    """Navega, numa página JÁ conectada, até a tela "Movimentar/Peticionar" de UM
    processo — busca rápida (real, gera hash assinado pelo servidor) → clica o link
    real "Movimentar/Peticionar". Devolve "" em sucesso, ou uma mensagem de erro."""
    await page.goto(EPROC_HOME, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(800)
    if not _status_dict(page.url)["logado"]:
        return ("Sessão do e-proc não está logada — faça login na janela do e-proc "
                "e tente de novo.")

    campo = page.locator("#txtNumProcessoPesquisaRapida:visible")
    await campo.click()
    await campo.fill(numero)
    await campo.press("Enter")
    await page.wait_for_load_state("domcontentloaded", timeout=20000)
    await page.wait_for_timeout(800)

    if not await page.evaluate(_JS_CLICAR_LINK_EXATO, "Movimentar/Peticionar"):
        return ("Processo não encontrado ou sem link 'Movimentar/Peticionar' "
                "(permissão? já arquivado?).")
    await page.wait_for_load_state("domcontentloaded", timeout=20000)
    await page.wait_for_timeout(800)
    return ""


async def _listar_via_botao_listar_todos(numero_processo: str, seletor_botao: str,
                                          rotulo_campo: str) -> dict:
    """Base comum de `listar_eventos_movimentacao`/`listar_tipos_documento`: abre a
    tela de peticionamento de `numero_processo` numa ABA NOVA (não mexe na aba que o
    usuário estiver usando), clica o botão/label "Listar Todos" indicado por
    `seletor_botao` e extrai a lista completa exibida. Fecha a aba ao final."""
    numero = re.sub(r"\D", "", numero_processo or "")
    if not numero:
        return {"ok": False, "erro": "Número de processo vazio."}
    browser, pagina_ref = await _connect()
    ctx = pagina_ref.context
    page = await ctx.new_page()
    try:
        erro = await _abrir_tela_peticionamento(page, numero)
        if erro:
            return {"ok": False, "erro": erro}

        listar = page.locator(seletor_botao)
        if await listar.count() == 0:
            return {"ok": False, "erro": f"Campo '{rotulo_campo}' não encontrado nesta tela."}
        await listar.click()
        await page.wait_for_timeout(1200)

        brutos = await page.evaluate(_JS_EXTRAIR_EVENTOS)
        itens = sorted({e for e in brutos if e and e != "[Desmarcar Todos]"})
        return {"ok": True, "itens": itens, "numero_processo": numero}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        with contextlib.suppress(Exception):
            await page.close()
        with contextlib.suppress(Exception):
            await browser.close()


async def listar_eventos_movimentacao(numero_processo: str) -> dict:
    """Lista os EVENTOS disponíveis para peticionamento/movimentação de UM processo
    — tela "Movimentar/Peticionar" → campo "Evento a ser lançado:" → link "Listar
    Todos". A lista depende da CLASSE JUDICIAL do processo (processos de tipos
    diferentes podem ter catálogos diferentes); por isso pede um processo de
    REFERÊNCIA em vez de uma lista fixa."""
    res = await _listar_via_botao_listar_todos(
        numero_processo, "#lblListarEvento", "Evento a ser lançado")
    if res.get("ok"):
        res["eventos"] = res.pop("itens")
    return res


async def listar_niveis_sigilo(numero_processo: str) -> dict:
    """Lista os NÍVEIS DE SIGILO disponíveis por documento — tela "Movimentar/
    Peticionar" → select `#selIdSigilo_1` (sigilo do 1º documento do formulário).
    Ao contrário de eventos/tipos, é um `<select>` estático (não popup "Listar
    Todos") — mesma tela, só lê as opções diretamente. Ainda assim pede um processo
    de referência para reaproveitar a navegação e por segurança (pode variar por
    tribunal/vara)."""
    numero = re.sub(r"\D", "", numero_processo or "")
    if not numero:
        return {"ok": False, "erro": "Número de processo vazio."}
    browser, pagina_ref = await _connect()
    ctx = pagina_ref.context
    page = await ctx.new_page()
    try:
        erro = await _abrir_tela_peticionamento(page, numero)
        if erro:
            return {"ok": False, "erro": erro}

        sel = page.locator("#selIdSigilo_1")
        if await sel.count() == 0:
            return {"ok": False, "erro": "Campo 'Sigilo' não encontrado nesta tela."}
        opcoes = await sel.evaluate("(el) => [...el.options].map(o => o.text.trim())")
        sigilos = [o for o in opcoes if o]
        return {"ok": True, "sigilos": sigilos, "numero_processo": numero}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        with contextlib.suppress(Exception):
            await page.close()
        with contextlib.suppress(Exception):
            await browser.close()


async def listar_tipos_documento(numero_processo: str) -> dict:
    """Lista os TIPOS DE DOCUMENTO disponíveis para classificar um documento anexado
    numa petição — tela "Movimentar/Peticionar" → bloco de Documentos → "Tipo:" →
    link "Listar todos" (`#lblListarTipo_1`, primeiro documento do formulário). Ao
    contrário dos eventos, este catálogo parece ser GENÉRICO (não depende da classe
    judicial), mas ainda assim pede um processo de referência — mesma tela."""
    res = await _listar_via_botao_listar_todos(
        numero_processo, "#lblListarTipo_1", "Tipo (documento)")
    if res.get("ok"):
        res["tipos"] = res.pop("itens")
    return res


async def abrir_processo(url: str) -> dict:
    """Abre a Consulta Processual do e-proc em uma ABA NOVA da janela (onde o
    usuário está logado), espelhando o clique no nº do processo dentro do eproc.

    O `url` vem capturado na sincronização (hash assinado, válido na sessão). Fechar
    a conexão CDP no fim NÃO fecha a aba — ela permanece aberta para o usuário."""
    if not url:
        return {"ok": False, "erro": "URL do processo vazia."}
    browser, page = await _connect()
    ctx = page.context
    try:
        nova = await ctx.new_page()
        with contextlib.suppress(Exception):
            await nova.goto(url, wait_until="domcontentloaded", timeout=60000)
            await nova.wait_for_timeout(400)
            await nova.bring_to_front()
        await _maximizar_janela(nova)
        return {"ok": True, "url": nova.url}
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def abrir_processo_por_numero(numero: str) -> dict:
    """Abre a Consulta Processual do e-proc pesquisando DIRETO pelo número, numa
    ABA NOVA da janela onde o usuário está logado — mesma "pesquisa rápida" usada
    por `_abrir_tela_peticionamento`, mas sem clicar em "Movimentar/Peticionar" no
    fim (aqui só queremos ver o processo, não peticionar).

    Diferente de `abrir_processo`, NÃO depende de um link/hash pré-capturado numa
    sincronização — funciona pra qualquer número, inclusive processos vinculados a
    um PAJ que nunca passaram pela sincronização de Prazos e-proc. Fechar a conexão
    CDP no fim NÃO fecha a aba — ela permanece aberta para o usuário."""
    numero_limpo = re.sub(r"\D", "", numero or "")
    if not numero_limpo:
        return {"ok": False, "erro": "Número de processo vazio."}
    browser, pagina_ref = await _connect()
    ctx = pagina_ref.context
    try:
        page = await ctx.new_page()
        await page.goto(EPROC_HOME, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(800)
        if not _status_dict(page.url)["logado"]:
            return {
                "ok": False,
                "erro": "Sessão do e-proc não está logada — faça login na janela do e-proc e tente de novo.",
            }
        campo = page.locator("#txtNumProcessoPesquisaRapida:visible")
        await campo.click()
        await campo.fill(numero_limpo)
        await campo.press("Enter")
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await page.wait_for_timeout(600)
        await page.bring_to_front()
        await _maximizar_janela(page)
        return {"ok": True, "url": page.url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
