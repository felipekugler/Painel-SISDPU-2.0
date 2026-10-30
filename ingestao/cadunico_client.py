"""Cliente do Cadastro Único (CAIXA, www.cadastrounico.caixa.gov.br/cadun) via
Playwright — janela VISÍVEL, processo separado conectado por CDP (mesmo padrão
de `ingestao.sd_client`).

Usado pelo item "Dossiê CadÚnico" do menu Consultas da página do PAJ (ver
`services.dossie_cadunico_service`). Estrutura do site mapeada ao vivo em
2026-10-09:

- Tudo é AJAX (jQuery 3.6): cada clique troca o conteúdo de `#recebe_miolo`;
  mensagens do sistema aparecem em `#L_aviso`.
- Busca: menu "Buscar Família" → `#idTipoBusca` (1=Código Familiar, 2=CPF,
  3=NIS; o `change` jQuery troca o campo visível) → `#numeroCPF` →
  `#botaoBuscar`. Resultado: uma linha por FAMÍLIA (um CPF pode estar em mais
  de uma), cada uma com `onclick="cadunChamadaFamilia(cod, dv, 'buscaFamiliaForm')"`
  → página "Visualizar Família".
- Os documentos são PDFs OFICIAIS servidos por servlets GET ligados à família
  aberta NA SESSÃO (sem parâmetros): `ImprimirFolhaResumoServlet`,
  `ImprimirFormularioPrincipalServlet` (que já embute os suplementares
  preenchidos — conferido: as 2 últimas páginas do Principal == Suplementar
  1), `ImprimirFormularioSuplementar{1,2,3}Servlet` (devolvem o formulário EM
  BRANCO quando não preenchidos), `ImprimirMotivoExclusaoPessoaServlet` (do
  integrante excluído cuja tela de motivo está aberta) e
  `ImprimirHistoricoMovimentacoesFamiliaServlet?camposHistoricoSelecionados=<ids>`
  (depois de CONSULTAR o histórico no período). Um servlet às vezes devolve
  uma página HTML de "Erro Interno" com status 200 — por isso todo download é
  conferido pelo `%PDF`.

Login (Login CAIXA, Keycloak em login.caixa.gov.br, em etapas): CPF → senha →
código enviado por e-mail → 2FA. Automatizado com o que estiver no COFRE
(sistema "cadunico": CPF, senha, e-mail do código e, quando cadastrada, a seed
TOTP) — o código do e-mail é lido pelo MCP do Microsoft 365
(`services.codigo_email_service`). Qualquer etapa que não dê para resolver
sozinho (cofre trancado, sem seed, e-mail não lido, tela desconhecida, erro do
site) fica para o Defensor completar na janela, como antes — a coleta segue
sozinha quando a tela "Buscar Família" aparecer. Ao fim de cada dossiê a
sessão é encerrada (`sair` → botão "Sair") e a janela é fechada."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright

import config

_PROFILE = Path(config.OFICIO_GERAL) / "_cadunico_profile"
_CDP_PORT = 9227 + config.CDP_OFFSET  # SAT=9222, e-proc=9223, RPV=9224, SD=9225, e-Sisrec=9226

CADUN_URL = "https://www.cadastrounico.caixa.gov.br/cadun/abrirAplicacao.do"

_cad_pid: int | None = None


class CadunicoErro(Exception):
    pass


class SessaoExpirada(CadunicoErro):
    pass


# --------------------------------------------------------------------------
# Navegador
# --------------------------------------------------------------------------

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
    """Fecha o Chromium do CadÚnico — ao fim de cada dossiê (depois de `sair`)
    e no shutdown do painel."""
    global _cad_pid
    if not _porta_aberta(_CDP_PORT):
        _cad_pid = None
        return
    pid = _cad_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _cad_pid = None


async def _ensure_chromium(pw) -> None:
    global _cad_pid
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
    _cad_pid = proc.pid
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)
            return
        time.sleep(0.5)


async def conectar():
    """(pw, browser, page) — feche com `desconectar(pw, browser)`. Uma
    instância do Playwright POR JOB (ver `sd_client.conectar`)."""
    pw = await async_playwright().start()
    try:
        await _ensure_chromium(pw)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception:
        with contextlib.suppress(Exception):
            await pw.stop()
        raise
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = None
    for p in ctx.pages:  # reaproveita a aba já logada, se houver
        if "cadastrounico.caixa.gov.br" in (p.url or ""):
            page = p
            break
    if page is None:
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


# --------------------------------------------------------------------------
# Login (manual) e espera de estados
# --------------------------------------------------------------------------

_JS_LOGADO = r"""()=>!!(document.getElementById('botaoSair') && document.getElementById('recebe_miolo'))"""


async def _logado(page) -> bool:
    if "cadastrounico.caixa.gov.br/cadun" not in (page.url or ""):
        return False
    with contextlib.suppress(Exception):
        return bool(await page.evaluate(_JS_LOGADO))
    return False


# Etapa visível do Login CAIXA (mapeada ao vivo em 2026-10-30 só até a 1ª tela:
# `#username` = CPF + `#button-submit` "Próximo"; as seguintes são reconhecidas
# pelo campo visível e pelo texto da tela). Só conta o que está VISÍVEL.
_JS_ETAPA_LOGIN = r"""()=>{
  if(!/login\.caixa\.gov\.br/.test(location.host)) return {etapa:'fora'};
  const vis=e=>!!e && e.offsetParent!==null && !e.disabled;
  const txt=(document.body.innerText||'').replace(/\s+/g,' ');
  const erro=[...document.querySelectorAll('.alert-error,.kc-feedback-text,#input-error,.pf-m-danger,[role=alert]')]
     .filter(vis).map(e=>e.innerText.trim()).filter(Boolean).join(' | ');
  if(vis(document.getElementById('username'))) return {etapa:'cpf', erro};
  if([...document.querySelectorAll('input[type=password]')].some(vis)) return {etapa:'senha', erro};
  // escolha do meio de recebimento (2026-10-30): opções com o e-mail/telefone
  // mascarados + botão "Receber código"
  if([...document.querySelectorAll('button,a,input[type=submit]')].some(e=>vis(e)
     && /receber\s+c[óo]digo/i.test(e.innerText||e.value||''))) return {etapa:'canal', erro};
  const campo=[...document.querySelectorAll('input[type=text],input[type=tel],input[type=number],input:not([type])')].find(vis);
  if(campo){
    if(/autenticador|aplicativo|authenticator|\b2FA\b|\bOTP\b/i.test(txt) && !/enviad[oa].{0,40}e-?mail/i.test(txt))
      return {etapa:'totp', erro};
    // tela real (2026-10-30): "Validação de Login — insira o código de validação recebido"
    if(/c[óo]digo de valida[çc][ãa]o|(e-?mail).{0,80}c[óo]digo|c[óo]digo.{0,80}e-?mail/i.test(txt))
      return {etapa:'codigo_email', erro};
  }
  return {etapa:'desconhecida', erro, texto: txt.slice(0,200)};
}"""

_JS_PREENCHER_E_ENVIAR = r"""([seletor, valor])=>{
  const vis=e=>!!e && e.offsetParent!==null && !e.disabled;
  const campo=[...document.querySelectorAll(seletor)].find(vis);
  if(!campo) return 'campo não encontrado';
  campo.focus();
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(campo, valor);
  ['input','change','keyup'].forEach(t=>campo.dispatchEvent(new Event(t,{bubbles:true})));
  const btn=[document.getElementById('button-submit'), ...document.querySelectorAll('button[type=submit],input[type=submit]')].find(vis);
  if(btn) btn.click(); else if(campo.form) campo.form.requestSubmit();
  return 'ok';
}"""

_JS_ESCOLHER_EMAIL = r"""()=>{
  const vis=e=>!!e && e.offsetParent!==null;
  // a opção de e-mail é a que mostra um endereço mascarado (tem "@"; o domínio
  // exibido pode ser o antigo, ex. @dpu.gov.br) — pega o menor elemento visível com "@"
  const ops=[...document.querySelectorAll('label,li,div,span,p,button,a')]
     .filter(e=>vis(e) && /@/.test(e.innerText||'') && (e.innerText||'').length<150)
     .sort((x,y)=>x.innerText.length-y.innerText.length);
  const alvo=ops[0];
  if(!alvo) return 'opção de e-mail não encontrada';
  const box=alvo.closest('label,li,div')||alvo;
  const radio=box.querySelector('input[type=radio],input[type=checkbox]')
     || (alvo.htmlFor && document.getElementById(alvo.htmlFor));
  if(radio){ radio.checked=true; radio.dispatchEvent(new Event('change',{bubbles:true})); radio.click(); }
  else alvo.click();
  const btn=[...document.querySelectorAll('button,a,input[type=submit]')].find(e=>vis(e) && !e.disabled
     && /receber\s+c[óo]digo/i.test(e.innerText||e.value||''));
  if(!btn) return 'botão Receber código não encontrado';
  btn.click();
  return 'ok';
}"""

_SEL_CAMPO_CODIGO = "input[type=text],input[type=tel],input[type=number],input:not([type])"


async def _login_automatico(page, log, timeout_s: int = 900) -> bool:
    """Acompanha o Login CAIXA até logar (ou `timeout_s`), resolvendo cada etapa
    com o cofre. Etapa que não der para resolver sozinho fica para o Defensor
    na janela, mas o laço CONTINUA vigiando: as etapas seguintes (ex.: senha e
    2FA depois de um código digitado à mão) seguem automáticas."""
    from datetime import datetime

    from services import cofre_service

    cred = cofre_service.get_credenciais("cadunico")
    if not cred:
        log("aviso", "Credenciais do CadÚnico indisponíveis (cofre trancado ou sem cadastro em "
                     "Configurações › Cofre) — faça o login na janela.")
        cpf = senha = ""
    else:
        cpf, senha = re.sub(r"\D", "", cred[0]), cred[1]
    email = cofre_service.get_extra("cadunico", "email")
    seed = cofre_service.get_extra("cadunico", "totp_seed")

    enviado_em = datetime.now().astimezone()  # último envio — base da busca do e-mail
    anterior = ""            # etapa vista na volta anterior
    tentativas = 0           # ações nesta visita à etapa
    manual: set[str] = set() # etapas desta visita deixadas para o Defensor
    ini = time.monotonic()
    while time.monotonic() - ini < timeout_s:
        if await _logado(page):
            return True
        estado = None
        with contextlib.suppress(Exception):
            estado = await page.evaluate(_JS_ETAPA_LOGIN)
        etapa = (estado or {}).get("etapa") or "carregando"
        if etapa in ("fora", "carregando"):
            await page.wait_for_timeout(1500)
            continue
        if etapa != anterior:
            anterior, tentativas = etapa, 0
            manual.clear()
            if etapa == "desconhecida":
                log("aviso", "Tela do Login CAIXA não reconhecida — resolva na janela; o resto "
                             f"segue automático. Texto da tela: {estado.get('texto', '')}")
        if etapa == "desconhecida" or etapa in manual:
            await page.wait_for_timeout(1500)
            continue

        def deixar_manual(msg: str) -> None:
            log("aviso", msg)
            manual.add(etapa)

        if estado.get("erro") and tentativas:
            deixar_manual(f"Login CAIXA recusou: {estado['erro']} — resolva na janela.")
            continue
        if tentativas >= 2:  # a mesma etapa voltou sem mensagem = algo recusado
            deixar_manual(f"Login CAIXA não passou da etapa '{etapa}' — resolva na janela.")
            continue
        tentativas += 1

        r = "ok"
        if etapa in ("cpf", "senha") and not cred:
            manual.add(etapa)
            continue
        if etapa == "cpf":
            r = await page.evaluate(_JS_PREENCHER_E_ENVIAR, ["#username", cpf])
            log("info", "Login CAIXA: CPF informado.")
        elif etapa == "senha":
            r = await page.evaluate(_JS_PREENCHER_E_ENVIAR, ["input[type=password]", senha])
            log("info", "Login CAIXA: senha informada (cofre).")
        elif etapa == "canal":
            enviado_em = datetime.now().astimezone()
            r = await page.evaluate(_JS_ESCOLHER_EMAIL)
            log("info", "Login CAIXA: envio do código por e-mail solicitado.")
        elif etapa == "codigo_email":
            if not email:
                deixar_manual("Sem e-mail do código no cofre — digite o código na janela.")
                continue
            log("info", f"Login CAIXA: buscando o código no e-mail {email} (Microsoft 365)…")
            from services import codigo_email_service
            codigo = await codigo_email_service.buscar_codigo(email, enviado_em, log)
            if await _logado(page):
                return True
            with contextlib.suppress(Exception):
                if (await page.evaluate(_JS_ETAPA_LOGIN)).get("etapa") != "codigo_email":
                    continue  # o Defensor digitou enquanto a busca rodava
            if not codigo:
                deixar_manual("Não consegui ler o código do e-mail — digite-o na janela.")
                continue
            r = await page.evaluate(_JS_PREENCHER_E_ENVIAR, [_SEL_CAMPO_CODIGO, codigo])
            log("info", f"Login CAIXA: código do e-mail informado ({codigo}).")
        else:  # totp
            if not seed:
                deixar_manual("Seed do 2FA não cadastrada no cofre — digite o código do "
                              "autenticador na janela.")
                continue
            import pyotp
            r = await page.evaluate(_JS_PREENCHER_E_ENVIAR, [_SEL_CAMPO_CODIGO, pyotp.TOTP(seed).now()])
            log("info", "Login CAIXA: código do 2FA informado (seed do cofre).")
        if r != "ok":
            deixar_manual(f"Login CAIXA, etapa '{etapa}': {r} — resolva na janela.")
            continue
        await page.wait_for_timeout(2500)
    return await _logado(page)


async def sair(page) -> bool:
    """Encerra a sessão: clica em "Sair" (canto superior direito, `#botaoSair`
    → sairAplicacao.do). True se a página deixou de estar logada."""
    if not await _logado(page):
        return True
    page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
    with contextlib.suppress(Exception):
        await page.click("#botaoSair", timeout=10000)
    for _ in range(20):
        await page.wait_for_timeout(500)
        if not await _logado(page):
            return True
    return False


async def garantir_login(page, log, timeout_s: int = 900) -> bool:
    """Abre o CadÚnico; se não estiver logado, faz o login com o cofre
    (`_login_automatico`) e, no que faltar, traz a janela para frente e espera
    o Defensor completar à mão."""
    if await _logado(page):
        return True
    with contextlib.suppress(Exception):
        await page.goto(CADUN_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
    if await _logado(page):
        return True
    with contextlib.suppress(Exception):
        await page.bring_to_front()
    try:
        if await _login_automatico(page, log, timeout_s):
            return True
    except Exception as e:  # noqa: BLE001 — automação é best-effort; o manual continua valendo
        log("aviso", f"Login automático interrompido ({type(e).__name__}: {e}).")
    if await _logado(page):
        return True
    log("aviso", "Faça o login na janela do CadÚnico que abriu (o que faltar entre CPF, senha, "
                 "código do e-mail e 2FA) — a coleta continua sozinha depois.")
    ini = time.monotonic()
    while time.monotonic() - ini < timeout_s:
        await page.wait_for_timeout(2000)
        if await _logado(page):
            return True
    return await _logado(page)


async def _esperar(page, js: str, timeout_s: float = 60, arg=None):
    """Polling de uma condição JS (retorno truthy). Levanta SessaoExpirada se a
    página saiu do CadÚnico logado no meio do caminho."""
    ini = time.monotonic()
    while time.monotonic() - ini < timeout_s:
        with contextlib.suppress(Exception):
            r = await page.evaluate(js, arg)
            if r:
                return r
        if not await _logado(page):
            raise SessaoExpirada("sessão do CadÚnico encerrada")
        await page.wait_for_timeout(500)
    return None


async def _aviso(page) -> str:
    with contextlib.suppress(Exception):
        return (await page.evaluate(
            "()=>{const a=document.getElementById('L_aviso');return a?a.innerText.trim():''}")) or ""
    return ""


# --------------------------------------------------------------------------
# Busca por CPF ou por Código Familiar
# --------------------------------------------------------------------------

_JS_ABRIR_BUSCA = r"""()=>{
  const a=[...document.querySelectorAll('a')].find(e=>(e.innerText||'').trim().toLowerCase()==='buscar família');
  if(!a) return false; a.click(); return true;
}"""

_JS_FORM_BUSCA_PRONTO = r"""()=>!!document.getElementById('idTipoBusca') && !!document.getElementById('botaoBuscar')"""

_JS_BUSCAR_CPF = r"""(cpf)=>{
  const s=document.getElementById('idTipoBusca');
  s.value='2'; s.dispatchEvent(new Event('change',{bubbles:true}));
  const c=document.getElementById('numeroCPF');
  if(!c || c.offsetParent===null) return 'campo CPF não apareceu';
  c.value=cpf; c.dispatchEvent(new Event('change',{bubbles:true}));
  document.getElementById('botaoBuscar').click();
  return 'ok';
}"""

# Busca por Código Familiar (tipo 1). O campo que aparece não foi mapeado ao
# vivo: preenche o(s) campo(s) de texto visíveis do formulário — um só recebe
# os 11 dígitos; dois recebem código (9) + dígito verificador (2).
_JS_BUSCAR_CODIGO = r"""(cod)=>{
  const s=document.getElementById('idTipoBusca');
  // a opção é escolhida pelo TEXTO: o value "1" não é o Código Familiar (2026-10-30)
  const op=[...s.options].find(o=>/c[óo]d(igo|\.)?\s*familiar/i.test(o.text));
  if(!op) return 'opção Código Familiar não existe no tipo de busca: '+[...s.options].map(o=>o.value+'='+o.text).join(', ');
  s.value=op.value; s.dispatchEvent(new Event('change',{bubbles:true}));
  if(window.jQuery) jQuery(s).trigger('change');
  const form=s.form||document.getElementById('recebe_miolo');
  const vis=e=>e.offsetParent!==null && !e.disabled && !e.readOnly;
  const textos=[...form.querySelectorAll('input[type=text],input:not([type])')].filter(vis);
  const proprios=textos.filter(e=>/fam|cod/i.test((e.id||'')+' '+(e.name||'')) && !/nis|pessoa|cpf/i.test((e.id||'')+' '+(e.name||'')));
  const campos=proprios.length?proprios:textos;
  if(!campos.length) return 'campo do Código Familiar não apareceu';
  const set=(c,v)=>{c.value=v; ['input','change','keyup','blur'].forEach(t=>c.dispatchEvent(new Event(t,{bubbles:true})));};
  // campo único com máscara "999999999-99" (maxlength 12) recebe o hífen
  if(campos.length>=2){ set(campos[0],cod.slice(0,-2)); set(campos[1],cod.slice(-2)); }
  else set(campos[0], campos[0].maxLength===12 ? cod.slice(0,-2)+'-'+cod.slice(-2) : cod);
  document.getElementById('botaoBuscar').click();
  return 'ok';
}"""

# diagnóstico da busca por código: campos visíveis do formulário e valores
_JS_CAMPOS_BUSCA = r"""()=>{
  const s=document.getElementById('idTipoBusca'); const form=(s&&s.form)||document.getElementById('recebe_miolo');
  if(!form) return 'sem formulário';
  return 'opções: '+(s?[...s.options].map(o=>o.value+'='+o.text).join(', '):'?')+' | '+[...form.querySelectorAll('input,select')].filter(e=>e.offsetParent!==null && e.type!=='hidden')
    .map(e=>`${e.id||e.name}[${e.type},max=${e.maxLength}]=${e.value}`).join('; ');
}"""

# linhas de resultado: uma por família
_JS_RESULTADO = r"""()=>{
  const trs=[...document.querySelectorAll('#recebe_miolo tr[onclick]')]
     .filter(tr=>/cadunChamadaFamilia/.test(tr.getAttribute('onclick')||''));
  if(!trs.length) return null;
  const head=[...document.querySelectorAll('#recebe_miolo th')].map(th=>th.innerText.trim().replace(/\s+/g,' '));
  return trs.map(tr=>{
    const m=(tr.getAttribute('onclick')||'').match(/cadunChamadaFamilia\(\s*'?(\d+)'?\s*,\s*'?(\d+)'?/);
    const cel=[...tr.cells].map(td=>td.innerText.trim().replace(/\s+/g,' '));
    const campos={}; head.slice(-cel.length).forEach((h,i)=>{campos[h]=cel[i];});
    return {codigo:m?m[1]:'', dv:m?m[2]:'', celulas:cel, campos};
  });
}"""


async def _ir_para_busca(page) -> None:
    if not await page.evaluate(_JS_ABRIR_BUSCA):
        await page.goto(CADUN_URL, wait_until="domcontentloaded", timeout=60000)
        if not await _logado(page):
            raise SessaoExpirada("redirecionado ao login")
        await page.evaluate(_JS_ABRIR_BUSCA)
    if not await _esperar(page, _JS_FORM_BUSCA_PRONTO, 30):
        raise CadunicoErro("a tela Buscar Família não abriu")


async def buscar_familias(page, cpf: str, codigo_familiar: str = "") -> list[dict]:
    """Lista as famílias em que o CPF está cadastrado (pode ser vazia). Com
    `codigo_familiar` (11 dígitos, com o DV), busca por ele em vez do CPF."""
    cod = re.sub(r"\D", "", codigo_familiar or "")
    await _ir_para_busca(page)
    if cod:
        r = await page.evaluate(_JS_BUSCAR_CODIGO, cod)
    else:
        r = await page.evaluate(_JS_BUSCAR_CPF, re.sub(r"\D", "", cpf or ""))
    if r != "ok":
        raise CadunicoErro(r)
    # espera resultado (lista OU a própria família) ou mensagem do sistema
    achou = await _esperar(
        page, "()=>{ if((" + _JS_RESULTADO + ")()) return 'res';"
              " if((" + _JS_NA_FAMILIA + ")()) return 'familia';"
              " const a=document.getElementById('L_aviso');"
              " return (a && a.innerText.trim()) ? 'aviso' : null }", 60)
    if achou == "res":
        return await page.evaluate(_JS_RESULTADO) or []
    if achou == "familia":  # a busca por código pode cair direto na família
        return [{"codigo": cod[:-2], "dv": cod[-2:], "celulas": [], "campos": {}, "direto": True}]
    aviso = await _aviso(page)
    if cod:  # busca por código: devolve o diagnóstico (campo não mapeado ao vivo)
        campos = ""
        with contextlib.suppress(Exception):
            campos = await page.evaluate(_JS_CAMPOS_BUSCA)
        raise CadunicoErro(f"busca pelo Código Familiar {cod} sem resultado — aviso do sistema: "
                           f"{aviso or '(nenhum)'} | campos: {campos}")
    if re.search(r"(?i)n[ãa]o\s+(foi|foram)\s+encontrad|nenhum|n[ãa]o\s+existe|inexistente", aviso):
        return []
    raise CadunicoErro(aviso or "a busca não retornou resultado nem mensagem")


async def abrir_familia(page, cpf: str, codigo: str, dv: str, por_codigo: bool = False) -> dict:
    """Refaz a busca (o form da lista é o que leva à família) e abre a
    família. Retorna os "Dados da Família" e a lista de integrantes."""
    res = await buscar_familias(page, cpf, codigo + dv if por_codigo else "")
    if res and res[0].get("direto"):
        return await page.evaluate(_JS_DADOS_FAMILIA)
    ok = await page.evaluate(
        "([c,d])=>{ if(typeof cadunChamadaFamilia!=='function') return false;"
        " cadunChamadaFamilia(c,d,'buscaFamiliaForm'); return true; }", [codigo, dv])
    if not ok:
        raise CadunicoErro("não foi possível abrir a família")
    if not await _esperar(page, _JS_NA_FAMILIA, 60):
        raise CadunicoErro(await _aviso(page) or "a página Visualizar Família não abriu")
    return await page.evaluate(_JS_DADOS_FAMILIA)


_JS_NA_FAMILIA = r"""()=>{
  const m=document.getElementById('recebe_miolo'); if(!m) return false;
  // família EXCLUÍDA não tem o bloco "Imprimir ou salvar formulário" (2026-10-30)
  return /Visualizar Fam[ií]lia/.test(m.innerText)
    && /Imprimir ou salvar formul|C[óo]digo familiar:/i.test(m.innerText);
}"""

_JS_DADOS_FAMILIA = r"""()=>{
  const m=document.getElementById('recebe_miolo');
  const txt=m.innerText;
  const g=(re)=>{const x=txt.match(re);return x?x[1].trim():''};
  const integrantes=[]; const vistos=new Set();
  [...m.querySelectorAll('[onclick]')].forEach(e=>{
    const oc=e.getAttribute('onclick')||'';
    if(!/cadunSelecionaPessoaVisualizacao|cadunExibeMotivoExlusaoPessoa/.test(oc)) return;
    const tr=e.closest('tr'); if(!tr) return;
    const cel=[...tr.cells].map(td=>td.innerText.trim().replace(/\s+/g,' '));
    const k=cel.join('|'); if(vistos.has(k)) return; vistos.add(k);
    integrantes.push({celulas:cel, excluido:/EXCLU[ÍI]DO/i.test(tr.innerText)});
  });
  return {
    codigo_familiar: g(/C[óo]digo familiar:\s*([\d\-]+)/i),
    data_inclusao: g(/Data da Inclus[ãa]o da fam[ií]lia:\s*([\d\/]+)/i),
    data_atualizacao: g(/Data de Atualiza[çc][ãa]o:\s*([\d\/]+)/i),
    estado_cadastral: g(/Estado cadastral da fam[ií]lia:\s*([^\n]+)/i),
    renda_per_capita: g(/Renda per capita:\s*([^\n]+)/i),
    integrantes,
  };
}"""


# --------------------------------------------------------------------------
# Downloads dos PDFs (sempre conferidos por %PDF)
# --------------------------------------------------------------------------

_JS_BAIXAR = r"""async (url)=>{
  const r=await fetch(url,{credentials:'include'});
  const b=new Uint8Array(await r.arrayBuffer());
  let s=''; for(let i=0;i<b.length;i+=0x8000) s+=String.fromCharCode.apply(null,b.subarray(i,i+0x8000));
  return {status:r.status, ct:r.headers.get('content-type')||'', b64:btoa(s)};
}"""


async def baixar_pdf(page, servlet: str) -> bytes | None:
    """GET de um servlet de impressão. None se não vier um PDF (página de
    erro interno, sessão caída etc.)."""
    r = await page.evaluate(_JS_BAIXAR, "/cadun/" + servlet)
    corpo = base64.b64decode(r.get("b64") or "")
    if corpo[:5] != b"%PDF-":
        return None
    return corpo


SERVLET_FOLHA_RESUMO = "ImprimirFolhaResumoServlet"
SERVLET_PRINCIPAL = "ImprimirFormularioPrincipalServlet"
SERVLET_SUPLEMENTAR = "ImprimirFormularioSuplementar{n}Servlet"


# tela "Consulta de Motivo de Exclusão da Pessoa": tabela da pessoa (Nome, NIS,
# ...) + tabela "Motivo da Exclusão | Data da Exclusão | CPF do Operador"
_JS_DADOS_EXCLUSAO = r"""()=>{
  const tabs=[...document.querySelectorAll('#recebe_miolo table')];
  const linha=(t,cab)=>{
    if(!t) return {};
    const h=[...t.rows].find(r=>cab.test(r.innerText));
    const d=[...t.rows].find(r=>r!==h && r.cells.length>=(h?h.cells.length:2) && !cab.test(r.innerText));
    if(!h||!d) return {};
    const o={}; [...h.cells].forEach((c,i)=>{o[c.innerText.trim().replace(/\s+/g,' ')]=(d.cells[i]?d.cells[i].innerText.trim().replace(/\s+/g,' '):'');});
    return o;
  };
  const p=linha(tabs.find(t=>/Nome da Pessoa/i.test(t.innerText) && /Informa[çc][õo]es da Exclus/i.test(t.innerText)), /Nome da Pessoa/i);
  const m=linha(tabs.find(t=>/Data da Exclus/i.test(t.innerText) && !/Nome da Pessoa/i.test(t.innerText)), /Data da Exclus/i);
  const g=(o,re)=>{const k=Object.keys(o).find(k=>re.test(k)); return k?o[k]:'';};
  return {nome:g(p,/^Nome/i), nis:g(p,/^NIS/i), motivo:g(m,/^Motivo/i), data:g(m,/^Data/i), operador:g(m,/Operador/i)};
}"""


async def baixar_motivos_exclusao(page, com_pdf: bool = True) -> list[dict]:
    """Para cada integrante EXCLUÍDO: abre a tela do motivo, lê nome/NIS/
    motivo/data, baixa o PDF oficial (se `com_pdf`) e volta à Visualizar
    Família. [{nome, nis, motivo, data, operador, pdf}] (`pdf` None se não
    emitido/pedido)."""
    alvos = await page.evaluate(
        "()=>{const v=new Set();return [...document.querySelectorAll('#recebe_miolo [onclick]')]"
        ".map(e=>e.getAttribute('onclick')||'').filter(o=>/cadunExibeMotivoExlusaoPessoa/.test(o)"
        "&&!v.has(o)&&v.add(o))}")
    saida: list[dict] = []
    for oc in alvos or []:
        clicou = await page.evaluate(
            "(oc)=>{const e=[...document.querySelectorAll('#recebe_miolo [onclick]')]"
            ".find(x=>x.getAttribute('onclick')===oc); if(!e) return false; e.click(); return true}", oc)
        if not clicou:
            continue
        if not await _esperar(page, "()=>/Motivo da Exclus/i.test("
                                    "(document.getElementById('recebe_miolo')||{}).innerText||'')", 40):
            raise CadunicoErro("a tela do motivo de exclusão não abriu")
        info = await page.evaluate(_JS_DADOS_EXCLUSAO) or {}
        info["pdf"] = (await baixar_pdf(page, "ImprimirMotivoExclusaoPessoaServlet")
                       if com_pdf else None)
        saida.append(info)
        await _voltar(page)
        if not await _esperar(page, _JS_NA_FAMILIA, 40):
            raise CadunicoErro("não voltou para a Visualizar Família após o motivo de exclusão")
    return saida


async def _voltar(page) -> None:
    await page.evaluate(
        "()=>{const e=[...document.querySelectorAll('#recebe_miolo *')]"
        ".find(x=>/^VOLTAR$/.test((x.innerText||x.value||'').trim()) && (x.getAttribute('onclick')"
        "|| (window.jQuery && (jQuery._data(x,'events')||{}).click)));"
        " if(e) e.click(); return !!e}")


_JS_ABRIR_HISTORICO = r"""()=>{
  const a=[...document.querySelectorAll('#recebe_miolo [onclick]')]
     .find(e=>/consultaHistorico\(/.test(e.getAttribute('onclick')||'') && /Hist[óo]rico da Fam/i.test(e.innerText||''));
  if(!a) return false; a.click(); return true;
}"""

_JS_CONSULTAR_HISTORICO = r"""([di,mi,ai,df,mf,af])=>{
  const set=(n,v)=>{const e=document.getElementsByName(n)[0]; if(!e) throw new Error('campo '+n); e.value=v;};
  set('dia_data_inicio',di);set('mes_data_inicio',mi);set('ano_data_inicio',ai);
  set('dia_data_fim',df);set('mes_data_fim',mf);set('ano_data_fim',af);
  const t=document.getElementById('idTodos'); if(t && !t.checked) t.click();
  const b=[...document.querySelectorAll('#recebe_miolo [onclick]')]
     .find(e=>/^CONSULTAR$/.test((e.innerText||e.value||'').trim()));
  if(!b) return 'botão CONSULTAR não encontrado';
  b.click(); return 'ok';
}"""

_JS_HISTORICO_PRONTO = r"""()=>{
  if(document.getElementById('hist_tabela')) return 'tabela';
  const a=document.getElementById('L_aviso');
  return (a && a.innerText.trim()) ? 'aviso' : null;
}"""


async def baixar_historico_familia(page, data_inicio: date) -> bytes | None:
    """Abre o Histórico da Família, consulta de `data_inicio` até hoje com
    TODOS os campos e baixa o PDF oficial (botão IMPRIMIR). None se não houver
    movimentação no período."""
    if not await page.evaluate(_JS_ABRIR_HISTORICO):
        raise CadunicoErro("link Histórico da Família não encontrado")
    if not await _esperar(page, "()=>!!document.getElementById('idTodos')", 40):
        raise CadunicoErro("a tela do Histórico da Família não abriu")
    hoje = date.today()
    r = await page.evaluate(_JS_CONSULTAR_HISTORICO, [
        f"{data_inicio.day:02d}", f"{data_inicio.month:02d}", f"{data_inicio.year}",
        f"{hoje.day:02d}", f"{hoje.month:02d}", f"{hoje.year}"])
    if r != "ok":
        raise CadunicoErro(r)
    estado = await _esperar(page, _JS_HISTORICO_PRONTO, 120)
    if estado != "tabela":
        return None
    campos = await page.evaluate(
        "()=>{const f=document.getElementById('historicoMovimentacoesFamiliaForm');"
        " return f? [...f.querySelectorAll('input[name=camposHistoricoSelecionados]')]"
        ".filter(c=>c.checked && c.value && c.value!=='#').map(c=>c.value).join(',') : ''}")
    if not campos:
        raise CadunicoErro("nenhum campo do histórico selecionado")
    return await baixar_pdf(page, "ImprimirHistoricoMovimentacoesFamiliaServlet"
                                  "?camposHistoricoSelecionados=" + campos)
