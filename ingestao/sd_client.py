"""Cliente do Seguro-Desemprego Web (MTE, sd.trabalho.gov.br) via Playwright —
janela VISÍVEL, processo separado conectado por CDP (mesmo padrão de
`ingestao.sat_client` / `ingestao.rpv_precatorio_client`).

Usado pelo botão "Dossiê Seguro-Desemprego Web" da página do PAJ (ver
`services.dossie_sd_service`). NÃO reimplementa a coleta: executa, na aba
logada, os PRÓPRIOS coletores JS das skills (`SD-DossieVinculos/sd_coletor.js`
e `SD-DossieRequerimentos/sd_req_coletor.js`, lidos do disco a cada execução)
— corrigir a skill corrige o painel.

Login: GERID/CAS do MTE (geridmte.dataprev.gov.br), MESMAS credenciais do SAT
Central (sistema "sat" do cofre) e MESMA seed TOTP (ver
`sat_client._gerar_codigo_totp`). A tela é o mesmo CAS stock do SAT (form
#fm1, username/password, token) — reaproveita `sat_client._preencher_login` e
`_preencher_2fa`. Cofre trancado/sem seed → login manual na janela (a coleta
segue sozinha quando o portal abrir).

Navegador próprio (porta/perfil exclusivos): o "Baixar CNIS do SAT" fecha o
Chromium do SAT ao terminar, o que derrubaria um dossiê em andamento se
compartilhassem o mesmo processo.

Os coletores terminam "baixando" o pacote JSON por um <a download> — aqui o
clique é interceptado (`_JS_HOOK_DOWNLOAD`) e o texto lido da própria página
em fatias, sem passar pela pasta Downloads."""

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

_PROFILE = Path(config.OFICIO_GERAL) / "_sd_profile"
_CDP_PORT = 9225  # SAT=9222, e-proc=9223, RPV=9224

SKILLS_DIR = Path.home() / ".claude" / "skills"
COLETOR_VINCULOS = SKILLS_DIR / "SD-DossieVinculos" / "sd_coletor.js"
COLETOR_REQUERIMENTOS = SKILLS_DIR / "SD-DossieRequerimentos" / "sd_req_coletor.js"
MONTADOR_VINCULOS = SKILLS_DIR / "SD-DossieVinculos" / "sd_dossie.py"
MONTADOR_REQUERIMENTOS = SKILLS_DIR / "SD-DossieRequerimentos" / "sd_req_dossie.py"
MONTADOR_UNIFICADO = SKILLS_DIR / "SD-Dossies" / "sd_dossie_unificado.py"  # "Completo"

SD_LOGIN_URL = ("https://geridmte.dataprev.gov.br/cas/login"
                "?service=https%3A%2F%2Fsd.mte.gov.br%2Fsd%2F")
SD_EXTRATO_URL = "https://sd.trabalho.gov.br/sd/paginas/extrato/consultar.jsf"

_sd_pid: int | None = None


class SessaoExpirada(Exception):
    pass


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
    """Fecha o Chromium do Seguro-Desemprego Web — chamado SEMPRE ao fim de
    cada dossiê (pedido do usuário, 2026-10-02) e no shutdown do painel."""
    global _sd_pid
    if not _porta_aberta(_CDP_PORT):
        _sd_pid = None
        return
    pid = _sd_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _sd_pid = None


async def _ensure_chromium(pw) -> None:
    global _sd_pid
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
         "--disable-extensions",  # mesmo motivo do sat_client (extensão Adobe)
         "about:blank"],
        creationflags=flags, close_fds=True,
    )
    _sd_pid = proc.pid
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)
            return
        time.sleep(0.5)


async def conectar():
    """(pw, browser, page) — feche com `desconectar(pw, browser)`.

    Uma instância do Playwright POR JOB, nunca guardada em global: cada job
    roda num `asyncio.run()` próprio (thread nova), e uma instância criada no
    loop de um job anterior (já encerrado) quebra o job seguinte com
    "connect_over_cdp: 'NoneType' object has no attribute 'send'" —
    reproduzido 2026-10-02 (1ª geração ok, 2ª falhava)."""
    pw = await async_playwright().start()
    try:
        await _ensure_chromium(pw)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return pw, browser, page


async def desconectar(pw, browser) -> None:
    """Fecha a conexão CDP (não o Chromium) e encerra o Playwright do job."""
    if browser is not None:
        with contextlib.suppress(Exception):
            await browser.close()
    if pw is not None:
        with contextlib.suppress(Exception):
            await pw.stop()


def _logado(url: str) -> bool:
    u = (url or "").lower()
    return (("sd.trabalho.gov.br" in u or "sd.mte.gov.br" in u)
            and "/cas/" not in u and "gerid" not in u)


def formatar_cpf(cpf: str) -> str:
    d = re.sub(r"\D", "", cpf or "")
    if len(d) != 11:
        return ""
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


async def garantir_login(page, usuario: str, senha: str, log, timeout_s: int = 240) -> bool:
    """Abre o CAS do MTE e loga: usuário/senha do cofre + 2FA pela seed. Sem
    credencial/seed, espera o login manual na janela (até `timeout_s`)."""
    with contextlib.suppress(Exception):
        await page.bring_to_front()
    with contextlib.suppress(Exception):
        await page.goto(SD_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)
    if _logado(page.url):
        return True
    if not (usuario and senha):
        log("aviso", "Cofre trancado ou sem credencial do SAT — faça o login na janela "
                     "do Seguro-Desemprego Web que abriu; a coleta continua sozinha.")
    login_enviado = False
    tentativas_2fa = 0
    aviso_manual = False
    ini = time.monotonic()
    while time.monotonic() - ini < timeout_s:
        if _logado(page.url):
            return True
        with contextlib.suppress(Exception):
            if (not login_enviado and usuario and senha
                    and await page.evaluate(sat_client._JS_TEM_LOGIN_GERID)):
                log("info", "Preenchendo login do GERID (credenciais do SAT)…")
                await sat_client._preencher_login(page, usuario, senha)
                login_enviado = True
                continue
            if tentativas_2fa < 3 and await page.evaluate(sat_client._JS_TEM_2FA_GERID):
                tentativas_2fa += 1
                if await sat_client._preencher_2fa(page):
                    log("info", "Código 2FA enviado (seed do SAT).")
                elif not aviso_manual:
                    aviso_manual = True
                    log("aviso", "Seed 2FA não cadastrada — digite o código na janela.")
                continue
        await page.wait_for_timeout(1000)
    return _logado(page.url)


# Intercepta o <a download="sd_dossie_*_pacote.json"> dos coletores: em vez de
# baixar, guarda o texto do pacote em window.__sdPacote (lido em fatias depois).
_JS_HOOK_DOWNLOAD = r"""()=>{
  window.__sdPacote = null;
  if (window.__sdHook) return true;
  window.__sdHook = true;
  const orig = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function(){
    if (this.download && /^sd_dossie_.*_pacote\.json$/.test(this.download)) {
      window.__sdPacote = 'PENDENTE';
      fetch(this.href).then(r => r.text())
        .then(t => { window.__sdPacote = t; }, e => { window.__sdPacote = 'ERRO_LEITURA'; });
      return;
    }
    return orig.apply(this, arguments);
  };
  return true;
}"""

_FATIA = 4_000_000


async def _ler_pacote(page) -> str:
    for _ in range(120):
        estado = await page.evaluate(
            "()=>{const p=window.__sdPacote; if(p===null||p===undefined) return 'NULO';"
            " if(p==='PENDENTE'||p==='ERRO_LEITURA') return p; return 'OK:'+p.length;}")
        if estado.startswith("OK:"):
            total = int(estado[3:])
            partes = []
            for i in range(0, total, _FATIA):
                partes.append(await page.evaluate(
                    "([i,n])=>window.__sdPacote.slice(i,i+n)", [i, _FATIA]))
            await page.evaluate("()=>{window.__sdPacote=null;}")
            return "".join(partes)
        if estado == "ERRO_LEITURA":
            raise RuntimeError("falha ao ler o pacote gerado pelo coletor")
        await page.wait_for_timeout(500)
    raise RuntimeError("o coletor não entregou o pacote")


def _como_funcao(js: str) -> str:
    """Arquivo do coletor (IIFE async, com comentários no topo) → função
    async para `page.evaluate` (que aguarda a Promise)."""
    corpo = js.strip().rstrip(";")
    return "async () => {\n const __r = (\n" + corpo + "\n);\n return await __r;\n}"


async def _rodar_coletor(page, js: str) -> str:
    await page.evaluate(_JS_HOOK_DOWNLOAD)
    res = str(await page.evaluate(_como_funcao(js)))
    if "SESSAO_EXPIRADA" in res:
        raise SessaoExpirada(res)
    if not res.startswith("OK"):
        raise RuntimeError(res[:300])
    return res


_JS_CONSULTAR_EXTRATO = r"""(cpf)=>{
  const s=document.getElementById('f:tipoPesquisa'); if(!s) return false;
  s.value='CPF'; s.dispatchEvent(new Event('change',{bubbles:true}));
  document.getElementById('f:txtNumCpf').value=cpf;
  document.getElementById('f:dataInicial').value='01/01/1950';
  const h=new Date(); document.getElementById('f:dataFinal').value=
    [String(h.getDate()).padStart(2,'0'),String(h.getMonth()+1).padStart(2,'0'),h.getFullYear()].join('/');
  setTimeout(()=>document.getElementById('f:consultar').click(),100); return true;
}"""

_JS_CONTAR_EXTRATO = r"""()=>{
  const as=[...document.querySelectorAll('#f a')];
  const v=as.filter(a=>/f:vinculos:\d+:/.test(a.getAttribute('onclick')||'')).length;
  const b=as.filter(a=>/f:beneficios:\d+:/.test(a.getAttribute('onclick')||'')).length;
  const exp=/sess[aã]o no servidor expirou/i.test(document.body ? document.body.innerText : '');
  return {titulo: document.title, pronto: document.readyState==='complete', v, b, exp};
}"""


async def coletar_vinculos(page, cpf_fmt: str) -> str:
    """Passos 2-3 do SD-DossieVinculos: consulta o extrato (desde 1950) e roda
    `sd_coletor.js`. Retorna o texto do pacote JSON."""
    await page.goto(SD_EXTRATO_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(800)
    if "/cas/" in page.url.lower():
        raise SessaoExpirada("redirecionado ao login")
    if not await page.evaluate(_JS_CONSULTAR_EXTRATO, cpf_fmt):
        raise RuntimeError("formulário do Extrato do Trabalhador (CNIS) não encontrado")
    # espera a tabela estabilizar (logo após o clique o título muda antes da tabela existir)
    anterior, estavel, c = None, 0, {}
    for _ in range(80):
        await page.wait_for_timeout(500)
        with contextlib.suppress(Exception):
            c = await page.evaluate(_JS_CONTAR_EXTRATO)
            if c.get("exp"):
                raise SessaoExpirada("sessão expirada no extrato")
            atual = (c["v"], c["b"])
            if ("Visualizar Extrato" in c["titulo"] and c["pronto"]
                    and atual == anterior and atual != (0, 0)):
                estavel += 1
                if estavel >= 3:
                    break
            else:
                estavel = 0
            anterior = atual
    if "Visualizar Extrato" not in (c.get("titulo") or ""):
        raise RuntimeError("o Extrato do Trabalhador não abriu para este CPF")
    js = COLETOR_VINCULOS.read_text(encoding="utf-8")
    await _rodar_coletor(page, js)
    return await _ler_pacote(page)


async def coletar_requerimentos(page, cpf_fmt: str) -> str:
    """Passo 2 do SD-DossieRequerimentos: roda `sd_req_coletor.js` numa página
    qualquer do portal logado. Retorna o texto do pacote JSON."""
    if not _logado(page.url) or "sd.trabalho.gov.br" not in page.url:
        await page.goto(SD_EXTRATO_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(800)
        if "/cas/" in page.url.lower():
            raise SessaoExpirada("redirecionado ao login")
    js = COLETOR_REQUERIMENTOS.read_text(encoding="utf-8").replace("__CPF__", cpf_fmt)
    await _rodar_coletor(page, js)
    return await _ler_pacote(page)
