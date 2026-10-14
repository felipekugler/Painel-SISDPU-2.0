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
_CDP_PORT = 9226 + config.CDP_OFFSET  # SAT=9222, e-proc=9223, RPV=9224, SD=9225

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


# --- Consulta HEADLESS (dentro do painel) -----------------------------------
# Viabilidade confirmada ao vivo 2026-10-06 (protocolo 44233866826202613):
# login GERID com reCAPTCHA invisível passa headless (~10s), processo ~13s.
# Só LEITURA: nunca clica Salvar/Anexar/Alterar etc. Os únicos cliques são a
# busca rápida e o paginador do "Histórico de Eventos"; documentos são obtidos
# reenviando o formulário com o nome do botão btBaixarArquivo (o mesmo POST
# não-AJAX que o clique faria, sem abrir aba).
#
# Uma sessão headless por processo do painel, reaproveitada entre consultas e
# fechada após _OCIOSO_S sem uso. Navegador próprio, independente da janela
# visível (porta 9226).
import asyncio  # noqa: E402

_PROFILE_HEADLESS = Path(config.OFICIO_GERAL) / "_esisrec_headless_profile"
_OCIOSO_S = 15 * 60
_hl: dict = {}
_hl_lock = asyncio.Lock()


class EsisrecErro(Exception):
    pass


async def _avaliar(page, js: str, arg=None):
    """`page.evaluate` que tolera a página ainda navegando: o e-Sisrec (JSF)
    às vezes recarrega logo após a busca, e a leitura caía com "Execution
    context was destroyed" (visto ao vivo 2026-10-06)."""
    for tentativa in range(8):
        try:
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load", timeout=15000)
            return await (page.evaluate(js, arg) if arg is not None else page.evaluate(js))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if tentativa < 7 and ("context was destroyed" in msg or "navigat" in msg.lower()):
                await page.wait_for_timeout(700)
                continue
            raise


async def _fechar_headless() -> None:
    ctx, pw = _hl.pop("ctx", None), _hl.pop("pw", None)
    _hl.clear()
    if ctx is not None:
        with contextlib.suppress(Exception):
            await ctx.close()
    if pw is not None:
        with contextlib.suppress(Exception):
            await pw.stop()


async def _vigia_ociosidade() -> None:
    while _hl:
        await asyncio.sleep(60)
        if _hl and time.monotonic() - _hl.get("uso", 0) > _OCIOSO_S:
            async with _hl_lock:
                if _hl and time.monotonic() - _hl.get("uso", 0) > _OCIOSO_S:
                    await _fechar_headless()


async def _pagina_headless():
    if _hl.get("page") is not None and not _hl["page"].is_closed():
        return _hl["page"]
    await _fechar_headless()
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_HEADLESS), headless=True, accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"])
    except Exception:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    # O UA do headless contém "HeadlessChrome" — trocado pelo do Chrome comum
    # (o login tem reCAPTCHA invisível).
    with contextlib.suppress(Exception):
        ua = await page.evaluate("navigator.userAgent")
        if "HeadlessChrome" in ua:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride",
                           {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    _hl.update(pw=pw, ctx=ctx, page=page, numero=None, uso=time.monotonic())
    asyncio.get_running_loop().create_task(_vigia_ociosidade())
    return page


async def _garantir_sessao(page, usuario: str, senha: str) -> None:
    if not _logado(page.url):
        await page.goto(ESISREC_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
    if not await _garantir_login(page, usuario, senha, timeout_s=60):
        # Diz ONDE parou (captcha, senha recusada, 2FA…) em vez da frase genérica
        # — perfil novo do Painel 2.0 falhava sem pista (13/10/2026).
        tela, desafio = "", False
        with contextlib.suppress(Exception):
            tela = " ".join((await page.evaluate("document.body.innerText") or "").split())[:300]
        with contextlib.suppress(Exception):
            desafio = await page.evaluate("""() => [...document.querySelectorAll('iframe')]
                .some(f => /recaptcha\\/api2\\/bframe|hcaptcha/.test(f.src || '') && f.offsetParent !== null)""")
        raise EsisrecErro(
            "Não foi possível logar no e-Sisrec (GERID) com a credencial do SAT Central."
            + (" O GERID pediu o desafio do captcha (verificação de robô)." if desafio else "")
            + f" Página: {page.url}" + (f" — tela: {tela}" if tela else ""))


async def _buscar(page, numero: str, usuario: str, senha: str) -> None:
    """Busca rápida → página do processo. Refaz o login se a sessão caiu."""
    for tentativa in range(2):
        await _garantir_sessao(page, usuario, senha)
        achou = False
        for _ in range(20):
            with contextlib.suppress(Exception):
                achou = await page.evaluate(_JS_ACHAR_BUSCA)
            if achou:
                break
            await page.wait_for_timeout(500)
        if not achou:
            if tentativa == 0:  # sessão expirada no meio de uma página
                await page.goto(ESISREC_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                continue
            raise EsisrecErro("Caixa de pesquisa 'CPF / NB / Protocolo' não encontrada no e-Sisrec.")
        campo = page.locator("[data-esisrec-busca=campo]")
        await campo.fill(numero)
        await page.locator("[data-esisrec-busca=botao]").first.click()
        with contextlib.suppress(Exception):
            await page.wait_for_url(re.compile(r"/pages/processo/manter_processo"), timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(500)
        if "/pages/processo/manter_processo" in page.url:
            _hl["numero"] = numero
            return
        if not _logado(page.url) and tentativa == 0:
            continue
        texto = ""
        with contextlib.suppress(Exception):
            texto = (await page.evaluate("()=>document.body.innerText"))[:600]
        raise EsisrecErro("A busca não abriu um processo único no e-Sisrec. " + texto.strip())


_JS_LER_PROCESSO = r"""()=>{
  const el=id=>document.getElementById(id);
  const txt=id=>{const e=el(id); return e?(e.innerText||'').trim():'';};
  const limpa=s=>(s||'').replace(/ui-button/g,'').replace(/[ \t]+\n/g,'\n').trim();
  const titulo=[...document.querySelectorAll('.panel-title')].map(e=>e.innerText.trim())
    .find(x=>/Informações do Processo/i.test(x))||'';
  const motivos=[...new Set([...document.querySelectorAll('[id^="formProcesso:motivo"][id$="_label"]')]
    .map(e=>e.innerText.trim()).filter(Boolean))];
  const aps=el('formProcesso:apsResponsavel_input');
  const partes=[...document.querySelectorAll('[id="formProcesso:dtPartesProcesso_data"] > tr')]
    .map(tr=>[...tr.children].map(td=>limpa(td.innerText)))
    .filter(c=>c.length>=5 && c[0])
    .map(c=>({nome:c[0], documento:c[1], tipo:c[2], principal:c[3], representacao:c[4]}));
  const eventos=[...document.querySelectorAll('[id="formProcesso:dtEventosProcesso_data"] > tr[data-ri]')]
    .map(tr=>{
      const td=[...tr.children];
      const docs=td.length>=5 ? [...td[4].querySelectorAll('button[id$=btBaixarArquivo]')].map(b=>{
        const nomeEl=b.parentElement && b.parentElement.querySelector('[id$=descricaoTipoArquivo]');
        return {nome:(nomeEl?nomeEl.innerText:'').trim()||'Documento', botao:b.name};
      }) : [];
      return {n:limpa(td[0]&&td[0].innerText), data:limpa(td[1]&&td[1].innerText),
              descricao:limpa(td[2]&&td[2].innerText), usuario:limpa(td[3]&&td[3].innerText),
              documentos:docs};
    });
  const pag='[id="formProcesso:dtEventosProcesso_paginator_bottom"]';
  const prox=document.querySelector(pag+' .ui-paginator-next');
  const ativa=document.querySelector(pag+' .ui-paginator-page.ui-state-active');
  return {titulo, numero:(titulo.split(' - ').slice(1).join(' - ')||'').trim(),
          especie:txt('formProcesso:optEspecie'), motivos,
          aps:aps?aps.value.trim():'', orgao:txt('formProcesso:orgaoAtual'),
          correspondencia:txt('formProcesso:correspondencia_label'),
          partes, eventos, pagina:ativa?ativa.innerText.trim():'1',
          tem_proxima:!!(prox && !prox.classList.contains('ui-state-disabled'))};
}"""

_SEL_PAGINADOR = '[id="formProcesso:dtEventosProcesso_paginator_bottom"]'


async def _esperar_troca_pagina(page, primeiro_antes) -> dict:
    novo = {}
    for _ in range(40):
        await page.wait_for_timeout(250)
        novo = await _avaliar(page, _JS_LER_PROCESSO)
        if (novo["eventos"] or [{}])[0].get("n") != primeiro_antes:
            return novo
    raise EsisrecErro("O histórico de eventos não trocou de página.")


async def _ir_para_pagina_eventos(page, pagina: str) -> None:
    atual = await _avaliar(page, _JS_LER_PROCESSO)
    if atual["pagina"] == str(pagina):
        return
    primeiro = (atual["eventos"] or [{}])[0].get("n")
    await page.locator(f"{_SEL_PAGINADOR} .ui-paginator-page",
                       has_text=re.compile(rf"^{re.escape(str(pagina))}$")).first.click()
    await _esperar_troca_pagina(page, primeiro)


async def consultar(numero: str, usuario: str, senha: str) -> dict:
    """Lê o processo de recurso `numero` (protocolo/CPF/NB) — dados, partes e
    histórico de eventos completo (todas as páginas), com a referência
    (botão + página) de cada documento para `baixar_documento`."""
    numero_limpo = re.sub(r"\D", "", numero or "")
    if not numero_limpo:
        return {"ok": False, "erro": "Número vazio."}
    if not (usuario and senha):
        return {"ok": False, "erro": "Cofre trancado ou sem credencial do SAT Central — "
                                     "desbloqueie o cofre e consulte de novo."}
    async with _hl_lock:
        try:
            page = await _pagina_headless()
            _hl["uso"] = time.monotonic()
            await _buscar(page, numero_limpo, usuario, senha)
            dados = await _avaliar(page, _JS_LER_PROCESSO)
            eventos = [dict(e, pagina=dados["pagina"]) for e in dados["eventos"]]
            paginas = 1
            while dados["tem_proxima"] and paginas < 50:
                primeiro = (dados["eventos"] or [{}])[0].get("n")
                await page.locator(f"{_SEL_PAGINADOR} .ui-paginator-next").first.click()
                dados = await _esperar_troca_pagina(page, primeiro)
                paginas += 1
                eventos += [dict(e, pagina=dados["pagina"]) for e in dados["eventos"]]
            if paginas > 1:
                with contextlib.suppress(Exception):
                    await _ir_para_pagina_eventos(page, "1")
            for k in ("tem_proxima", "pagina"):
                dados.pop(k, None)
            dados.update(eventos=eventos, busca=numero_limpo, url=page.url)
            return {"ok": True, **dados}
        except EsisrecErro as e:
            return {"ok": False, "erro": str(e)}
        except Exception as e:  # noqa: BLE001
            await _fechar_headless()  # estado desconhecido → recomeça na próxima
            return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}


# Documento identificado pelo nº do EVENTO + posição do documento na linha —
# não pelo nome do botão: os ids JSF (j_idtNNN) mudam entre sessões (493 no
# diagnóstico, 513 na consulta seguinte, 2026-10-06).
_JS_FORM_DOWNLOAD = r"""([evento, idx])=>{
  const f=document.getElementById('formProcesso');
  if(!f) return {erro:'formulário do processo ausente', url:location.href};
  const rows=[...document.querySelectorAll('[id="formProcesso:dtEventosProcesso_data"] > tr[data-ri]')];
  const tr=rows.find(r=>r.children[0] && r.children[0].innerText.trim()===String(evento));
  if(!tr) return {erro:'evento não está nesta página', eventos:rows.map(r=>r.children[0].innerText.trim())};
  const b=[...tr.querySelectorAll('button[id$=btBaixarArquivo]')][idx];
  if(!b) return {erro:'documento '+idx+' ausente no evento '+evento};
  const p=new URLSearchParams();
  for(const [k,v] of new FormData(f)) if(typeof v==='string') p.append(k,v);
  p.append(b.name, b.name);
  return {action:new URL(f.getAttribute('action'), location.href).href, body:p.toString()};
}"""


async def baixar_documento(numero: str, evento: str, idx: int, pagina: str,
                           usuario: str, senha: str) -> tuple[bytes, str, str]:
    """(conteúdo, nome do arquivo, content-type) do `idx`-ésimo documento do
    evento `evento` do histórico. `pagina` é só o palpite inicial — se o
    evento não estiver nela (histórico mudou), percorre as demais."""
    numero_limpo = re.sub(r"\D", "", numero or "")
    if not re.fullmatch(r"\d{1,5}", str(evento or "")):
        raise EsisrecErro("Referência de documento inválida.")
    if not (usuario and senha):
        raise EsisrecErro("Cofre trancado ou sem credencial do SAT Central.")
    async with _hl_lock:
        try:
            page = await _pagina_headless()
            _hl["uso"] = time.monotonic()
            if _hl.get("numero") != numero_limpo or "/pages/processo/" not in page.url:
                await _buscar(page, numero_limpo, usuario, senha)
            with contextlib.suppress(EsisrecErro):
                await _ir_para_pagina_eventos(page, str(pagina or "1"))
            form = await _avaliar(page, _JS_FORM_DOWNLOAD, [str(evento), int(idx)])
            if form and "evento não está" in form.get("erro", ""):
                # percorre as páginas do histórico a partir da 1ª
                with contextlib.suppress(EsisrecErro):
                    await _ir_para_pagina_eventos(page, "1")
                for _ in range(50):
                    form = await _avaliar(page, _JS_FORM_DOWNLOAD, [str(evento), int(idx)])
                    if not form or "evento não está" not in form.get("erro", ""):
                        break
                    dados = await _avaliar(page, _JS_LER_PROCESSO)
                    if not dados["tem_proxima"]:
                        break
                    primeiro = (dados["eventos"] or [{}])[0].get("n")
                    await page.locator(f"{_SEL_PAGINADOR} .ui-paginator-next").first.click()
                    await _esperar_troca_pagina(page, primeiro)
            if not form or form.get("erro"):
                raise EsisrecErro("Documento não encontrado no e-Sisrec: " + str(form))
            resp = await page.request.post(
                form["action"], data=form["body"],
                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=120000)
            ctype = resp.headers.get("content-type", "application/octet-stream")
            corpo = await resp.body()
        except EsisrecErro:
            raise
        except Exception as e:  # noqa: BLE001
            await _fechar_headless()
            raise EsisrecErro(f"{type(e).__name__}: {e}"[:300]) from e
    if not resp.ok or ctype.startswith("text/html"):
        raise EsisrecErro(f"O e-Sisrec não devolveu o arquivo (HTTP {resp.status}, {ctype}).")
    nome = "documento"
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', resp.headers.get("content-disposition", ""))
    if m:
        nome = m.group(1).strip()
    return corpo, nome, ctype


# "Gerar Relatório Andamento" do e-Sisrec = extrato do processo no CRPS (PDF).
# Localizado pelo TEXTO exato do botão (fica na mesma barra de "Salvar", que
# nunca é tocado). Botão de submit comum → reenvia o formulário (como os
# documentos); botão AJAX → clica e captura o download/aba aberta.
_JS_BOTAO_EXTRATO = r"""()=>{
  const f=document.getElementById('formProcesso');
  const b=[...document.querySelectorAll('button, a, input[type=submit]')]
    .find(x=>/^\s*gerar\s+relat[óo]rio\s+(de\s+)?andamento\s*$/i.test(x.innerText||x.value||''));
  if(!f || !b) return null;
  b.setAttribute('data-esisrec-extrato','1');
  const oc=b.getAttribute('onclick')||'';
  const ajax=/PrimeFaces\.ab\(|mojarra\.ab\(|jsf\.ajax/.test(oc);
  if(ajax || !b.name) return {ajax:true};
  const p=new URLSearchParams();
  for(const [k,v] of new FormData(f)) if(typeof v==='string') p.append(k,v);
  p.append(b.name, b.name);
  return {ajax:false, action:new URL(f.getAttribute('action'), location.href).href, body:p.toString()};
}"""


async def baixar_extrato(numero: str, usuario: str, senha: str) -> tuple[bytes, str, str]:
    """(conteúdo, nome do arquivo, content-type) do relatório de andamento."""
    numero_limpo = re.sub(r"\D", "", numero or "")
    if not numero_limpo:
        raise EsisrecErro("Número vazio.")
    if not (usuario and senha):
        raise EsisrecErro("Cofre trancado ou sem credencial do SAT Central.")
    async with _hl_lock:
        try:
            page = await _pagina_headless()
            _hl["uso"] = time.monotonic()
            if _hl.get("numero") != numero_limpo or "/pages/processo/" not in page.url:
                await _buscar(page, numero_limpo, usuario, senha)
            info = await _avaliar(page, _JS_BOTAO_EXTRATO)
            if not info:
                raise EsisrecErro("Botão 'Gerar Relatório Andamento' não encontrado no e-Sisrec.")
            if not info["ajax"]:
                resp = await page.request.post(
                    info["action"], data=info["body"],
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=120000)
                ctype = resp.headers.get("content-type", "application/octet-stream")
                if not resp.ok or ctype.startswith("text/html"):
                    raise EsisrecErro(f"O e-Sisrec não devolveu o relatório (HTTP {resp.status}, {ctype}).")
                corpo = await resp.body()
                nome = "documento"
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
                              resp.headers.get("content-disposition", ""))
                if m:
                    nome = m.group(1).strip()
                return corpo, nome, ctype
            # AJAX: o relatório chega como download (na própria aba ou numa
            # aba nova que o headless converte em download).
            ctx = _hl["ctx"]
            fut: asyncio.Future = asyncio.get_running_loop().create_future()

            def _on_download(d):
                if not fut.done():
                    fut.set_result(d)

            def _on_page(p):
                p.on("download", _on_download)

            page.on("download", _on_download)
            ctx.on("page", _on_page)
            try:
                await page.locator("[data-esisrec-extrato='1']").first.click()
                download = await asyncio.wait_for(fut, timeout=90)
            except asyncio.TimeoutError:
                raise EsisrecErro("O e-Sisrec não gerou o relatório de andamento (tempo esgotado).") from None
            finally:
                page.remove_listener("download", _on_download)
                ctx.remove_listener("page", _on_page)
            caminho = await download.path()
            corpo = Path(caminho).read_bytes()
            nome = download.suggested_filename or "relatorio_andamento.pdf"
            for p in ctx.pages:  # fecha abas extras abertas pelo relatório
                if p is not page:
                    with contextlib.suppress(Exception):
                        await p.close()
            ctype = "application/pdf" if corpo[:5] == b"%PDF-" else "application/octet-stream"
            return corpo, nome, ctype
        except EsisrecErro:
            raise
        except Exception as e:  # noqa: BLE001
            await _fechar_headless()
            raise EsisrecErro(f"{type(e).__name__}: {e}"[:300]) from e


async def fechar_headless() -> None:
    async with _hl_lock:
        await _fechar_headless()
