"""Cliente do SAT Central (INSS/Dataprev) via Playwright — janela VISÍVEL (headed) e
PERSISTENTE (processo separado, conectado por CDP).

Por que CDP e não `launch_persistent_context`: se o navegador fosse filho do processo
do painel, reiniciar o painel (comum ao atualizar `.py`) mataria o navegador e
derrubaria a sessão do SAT (cookies de sessão do GERID). Aqui o Chromium é lançado
como **processo independente** com `--remote-debugging-port`; o painel apenas
**conecta** via `connect_over_cdp`. Assim a sessão sobrevive a reinícios do painel — o
usuário loga uma vez e segue usando.

O login (usuário/senha/2FA) e o CAPTCHA são feitos pelo usuário NA JANELA.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config
from ingestao import sat_utils

_log = logging.getLogger("sat.client")

_PROFILE = Path(config.OFICIO_GERAL) / "_sat_profile"
SAT_CONSULTA = (
    "https://consultas.inss.gov.br/satcentral/pages/consultaCidadao/consultaCidadao.xhtml"
)
_CDP_PORT = 9222

_pw = None
_sat_pid: int | None = None  # PID do Chromium do SAT (p/ fechá-lo no shutdown do painel)


def _porta_aberta(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_na_porta(port: int) -> int | None:
    """PID do processo LISTENING na porta (via netstat) — cobre o caso de o Chromium ter
    sobrevivido a um restart do painel (PID lançado desconhecido)."""
    with contextlib.suppress(Exception):
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                return int(line.split()[-1])
    return None


def fechar_navegador() -> None:
    """Fecha o Chromium do SAT (chamado no SHUTDOWN do painel). Mata a árvore de processos
    pelo PID lançado ou, se desconhecido, pelo dono da porta CDP. Não roda em kill -9 do
    painel (só em encerramento gracioso), então reinícios de dev preservam a sessão."""
    global _sat_pid
    if not _porta_aberta(_CDP_PORT):
        _sat_pid = None
        return
    pid = _sat_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _sat_pid = None


async def _pw_start():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _ensure_chromium() -> None:
    """Garante um Chromium persistente rodando com remote-debugging (processo separado).
    Lança-o apontando já para a consulta do SAT (o usuário loga na janela)."""
    global _sat_pid
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
         SAT_CONSULTA],
        creationflags=flags, close_fds=True,
    )
    _sat_pid = proc.pid  # p/ fechar o navegador no shutdown do painel
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)  # respiro para o CDP subir de fato
            return
        time.sleep(0.5)


async def _connect():
    """Conecta ao Chromium persistente e devolve (browser, page). Chame `browser.close()`
    ao fim — fecha só a CONEXÃO CDP, não o Chromium."""
    await _ensure_chromium()
    pw = await _pw_start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return browser, page


def _status_dict(url: str) -> dict:
    u = (url or "").lower()
    logado = ("satcentral" in u) and ("cas/login" not in u) and ("geridinss" not in u)
    return {
        "logado": logado,
        "url": url,
        "msg": ("Sessão pronta no SAT — pode baixar." if logado
                else "Faça o login na janela do SAT (CPF, senha e código 2FA)."),
    }


async def abrir() -> dict:
    """Abre/traz à frente a janela do SAT e navega para a consulta (login se preciso)."""
    browser, page = await _connect()
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        with contextlib.suppress(Exception):
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def status() -> dict:
    """Verifica se a sessão está ativa navegando para a consulta (deixa a tela pronta)."""
    browser, page = await _connect()
    try:
        with contextlib.suppress(Exception):
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def peek() -> dict:
    """Status LEVE p/ polling automático: só LÊ a URL das abas abertas — NÃO navega e
    NÃO abre o Chromium (se a janela estiver fechada, retorna 'não conectado'). Assim
    o selo atualiza sozinho sem recarregar a janela nem atrapalhar login/CAPTCHA."""
    if not _porta_aberta(_CDP_PORT):
        return {"logado": False, "url": "",
                "msg": "Janela do SAT fechada — clique em 'Abrir SAT / Login'."}
    try:
        pw = await _pw_start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception:  # noqa: BLE001
        return {"logado": False, "url": "", "msg": "Janela do SAT indisponível."}
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        pages = list(ctx.pages) if ctx else []
        for p in pages:  # logado se ALGUMA aba está no satcentral (fora do login)
            d = _status_dict(p.url or "")
            if d["logado"]:
                return d
        return _status_dict(pages[0].url if pages else "")
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


# ---------------------------------------------------------------------------
# Download dos documentos (Fase 2). Etapa 2a: consulta por CPF + Grupo 1.
# ---------------------------------------------------------------------------
# Anti-bloqueio: quando o SAT dá SILÊNCIO (nem abre PDF/modal, nem mostra aviso EXPRESSO
# de documento inexistente), é (quase) certeza de proteção ANTIROBÔ. Para os documentos
# que SEMPRE existem (flag `garantido`) o silêncio = antirobô com CERTEZA. Recuperação:
# pausar (45s FIXO, sem progressão — decisão do usuário 2026-07-21) e re-tentar SÓ o doc.
_MAX_TENT = 3
_PAUSA_BLOQUEIO_MS = 45000  # 45s fixos ao detectar bloqueio antirobô (era 30s)

# Ritmo entre consultas: (1) delay APÓS cada consulta (campo "Tempo entre consultas",
# padrão 5s) + (2) a cada _LOTE_N consultas, uma pausa EXTRA de _LOTE_PAUSA_MS. É este
# ritmo (e não esperas fixas ad-hoc) que governa as transições entre docs/grupos/modais.
_RITMO_MS = 5000
_LOTE_N = 5
_LOTE_PAUSA_MS = 10000
_consultas = 0  # contador de consultas do download corrente (reset no início de cada um)


async def _ritmo(page) -> None:
    """Conta 1 consulta e aplica o ritmo: delay por consulta + pausa a cada _LOTE_N."""
    global _consultas
    _consultas += 1
    if _RITMO_MS > 0:
        await page.wait_for_timeout(_RITMO_MS)
    if _LOTE_N > 0 and _LOTE_PAUSA_MS > 0 and _consultas % _LOTE_N == 0:
        await page.wait_for_timeout(_LOTE_PAUSA_MS)


# Grupo 1 = abrem direto com 1 clique em "Consultar". (rótulo, prefixo do nome, GARANTIDO).
# `garantido`=True → o doc SEMPRE existe; silêncio ao consultar = antirobô com certeza.
_GRUPO1 = [
    ("CNIS - Atividades do CNIS v.3", "CNIS - Atividades", False),
    ("CNIS - Dados Cadastrais do CNIS", "CNIS - Dados Cadastrais", True),
    ("CNIS - Elos do CNIS", "CNIS - Elos", False),
    ("CNIS - Microfichas do CNIS v.3", "CNIS - Microfichas", False),
    ("CNIS - Remunerações do CNIS v.3", "CNIS - Remuneracoes", True),
    ("CNIS - Vínculos do CNIS v.3", "CNIS - Vinculos", True),
    ("Declaração de Benefícios", "Declaracao de Beneficios", True),
    ("Requerimentos do SIBE", "Requerimentos do SIBE", False),
    ("Revisão de Benefício - Artigo 29", "Revisao de Beneficio - Artigo 29", False),
]

# Clique no "Consultar" pelo rótulo (comparação NORMALIZADA — sem acento, espaços
# colapsados, minúsculo — o innerText do td tem espaçamento/acentos variáveis).
_JS_CLK = r"""(label)=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const alvo=norm(label);
  const btns=[...document.querySelectorAll('button[type=submit]')].filter(b=>norm(b.innerText||b.value)==='consultar');
  const btn=btns.find(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td&&norm(td.innerText)===alvo;});
  if(btn){btn.click(); return true;} return false;
}"""

_JS_BANNER = r"""()=>{
  const m=[...document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')]
    .map(e=>(e.innerText||'').trim()).filter(Boolean);
  return m.join(' | ');
}"""

# Marcação de banners para detectar mensagem NOVA (não residual). Guarda o texto atual
# de cada mensagem em data-sat-seen; uma mensagem é "nova" quando o nó ainda não foi
# marcado OU seu texto mudou desde a marcação (o PrimeFaces re-renderiza .ui-messages a
# cada AJAX, criando nós novos → funciona mesmo p/ textos idênticos consecutivos).
_JS_MARCAR_BANNERS = r"""()=>{
  document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')
    .forEach(e=>{ e.dataset.satSeen=(e.innerText||'').trim(); });
}"""

_JS_BANNER_NOVO = r"""()=>{
  const els=[...document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')];
  const novas=[];
  for(const e of els){ const t=(e.innerText||'').trim(); if(t && e.dataset.satSeen!==t){ novas.push(t); } e.dataset.satSeen=t; }
  return novas.join(' | ');
}"""

# Painel de Consultas RENDERIZADO: >=5 botões "Consultar" cujo td (rótulo) já tem texto.
_JS_TEM_CONSULTAR = r"""()=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const n=[...document.querySelectorAll('button[type=submit]')]
    .filter(b=>norm(b.innerText||b.value)==='consultar')
    .filter(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td && td.innerText.trim().length>3;}).length;
  return n>=5;
}"""


def _pdf_url(aba_url: str) -> str:
    """URL http real do PDF: o visualizador (Adobe/Chrome) traz a URL em `pdfurl=`."""
    from urllib.parse import urlparse, parse_qs, unquote
    if "pdfurl=" in (aba_url or ""):
        v = parse_qs(urlparse(aba_url).query).get("pdfurl", [""])[0]
        if v:
            return unquote(v)
    if (aba_url or "").startswith("http"):
        return aba_url
    return "https://consultas.inss.gov.br/satcentral/downloads"


async def _baixar_pdf(ctx, url: str, caminho: str) -> bool:
    """Baixa os bytes do PDF exibido na aba /downloads (usa os cookies da sessão)."""
    try:
        resp = await ctx.request.get(url, timeout=60000)
        body = await resp.body()
        if body[:5] == b"%PDF-" and len(body) > 500:
            Path(caminho).write_bytes(body)
            return True
        _log.warning("sat pdf inválido (%s bytes) em %s", len(body), url)
    except Exception as e:  # noqa: BLE001
        _log.warning("sat baixar pdf %s: %r", url, e)
    return False


def _ev(msg: str, level: str = "info") -> dict:
    return {"level": level, "msg": msg}


def _norm(s: str) -> str:
    import re as _re
    import unicodedata as _u
    s = "".join(c for c in _u.normalize("NFD", s or "") if _u.category(c) != "Mn")
    return _re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# Grupo 2 (documentos que abrem em MODAL) e PAT. Portados da skill
# SAT-DownloadArquivos (helpers dlg/linhasModal/abrirPdfLinha/btnModal/campoModal).
# ---------------------------------------------------------------------------
# O SAT pré-carrega ~14 modais PrimeFaces na mesma página, quase todos INVISÍVEIS
# até clicar o "Consultar" correspondente. SEMPRE operar só no dialog VISÍVEL
# (display/visibility ok, opacity>0, rect>0) e ler as linhas ACHATADAS
# (cells.join(' | ')) — a extração aninhada mascara o NB como "[BLOCKED: JWT token]".
_JS_DLG_FRAG = r"""
const _norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
const _dlg=()=>{const vis=[...document.querySelectorAll('.ui-dialog')].filter(d=>{const s=getComputedStyle(d);const r=d.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&parseFloat(s.opacity||'1')>0&&r.width>5&&r.height>5;});return vis.length?vis[vis.length-1]:null;};
const _dlgc=()=>{const d=_dlg();return d?(d.querySelector('.ui-dialog-content')||d):null;};
const _linhas=()=>{const c=_dlgc();if(!c)return[];return [...c.querySelectorAll('tbody tr')].filter(tr=>tr.querySelector('td')).map(tr=>[...tr.querySelectorAll('td')].map(td=>(td.innerText||'').replace(/\s+/g,' ').trim()).join(' | '));};
"""

# Consultar por rótulo, tolerante: exato e (fallback) "contém".
_JS_CLK_CONTEM = r"""(label)=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const alvo=norm(label);
  const btns=[...document.querySelectorAll('button[type=submit]')].filter(b=>norm(b.innerText||b.value)==='consultar');
  const btn=btns.find(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td&&norm(td.innerText).includes(alvo);});
  if(btn){btn.click(); return true;} return false;
}"""

_JS_DIALOG_VIS = "()=>{" + _JS_DLG_FRAG + " return !!_dlg();}"
_JS_LINHAS_MODAL = "()=>{" + _JS_DLG_FRAG + " return _linhas();}"

# Clica o "Abrir PDF" da linha n do dialog visível (mesmo índice de _linhas).
_JS_ABRIR_PDF_LINHA = "(n)=>{" + _JS_DLG_FRAG + r"""
  const c=_dlgc(); if(!c) return false;
  const trs=[...c.querySelectorAll('tbody tr')].filter(tr=>tr.querySelector('td'));
  const tr=trs[n]; if(!tr) return false;
  let b=[...tr.querySelectorAll('button,a,input[type=button],input[type=submit]')]
    .find(x=>_norm((x.innerText||'')+' '+(x.value||'')+' '+(x.title||'')).includes('abrir pdf'));
  if(!b) b=tr.querySelector('button,a,input[type=button],input[type=submit]');
  if(b){b.click(); return true;} return false;
}"""

# Clica um botão do dialog visível cujo texto casa a regex (OK/Consultar/Confirmar…).
_JS_BTN_MODAL = "(re)=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const rx=new RegExp(re,'i');
  const b=[...d.querySelectorAll('button,a,input[type=button],input[type=submit]')]
    .filter(x=>x.offsetParent!==null)
    .find(x=>rx.test(_norm((x.innerText||'')+' '+(x.value||''))));
  if(b){b.click(); return true;} return false;
}"""

# Preenche o input de texto de índice idx do dialog visível (HISCRE: 0=De, 1=até).
_JS_CAMPO_MODAL = "(a)=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const idx=a[0], valor=a[1];
  const ins=[...d.querySelectorAll('input')].filter(i=>i.offsetParent!==null
     && (i.type==='text'||i.type===''||i.type==null) && !i.readOnly && !i.disabled);
  const el=ins[idx]; if(!el) return false;
  el.focus(); el.value=valor;
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
  return true;
}"""

# Fecha o dialog visível pelo "x" da barra de título.
_JS_FECHAR_MODAL = "()=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const x=d.querySelector('.ui-dialog-titlebar-close');
  if(x){x.click(); return true;} return false;
}"""

# Avança uma página no paginador PrimeFaces do dialog visível (PAT).
_JS_PAG_PROX = "()=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const nx=d.querySelector('.ui-paginator-next');
  if(nx && !nx.classList.contains('ui-state-disabled')){nx.click(); return true;}
  return false;
}"""


# Situações conhecidas (para deduzir a coluna "Situação" das linhas achatadas).
_SIT_TOKENS = ("ativo", "cessado", "indeferido", "deferido", "concedido", "suspenso",
               "cancelado", "ativa", "cessada", "indeferida", "deferida",
               "em manutencao", "emitido", "concluido")


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|") if c.strip()]


def _cells_dados(row: str) -> list[str]:
    """Células de dados, sem a célula do botão 'Abrir PDF' (última coluna das tabelas)."""
    return [c for c in _cells(row) if _norm(c) != "abrir pdf"]


def _achar_nb_row(row: str) -> str:
    m = re.search(r"\d{3}\.\d{3}\.\d{3}-\d(?!\d)", row)
    if m:
        return m.group(0)
    m = re.search(r"(?<!\d)(\d{10})(?!\d)", row)
    if m:
        r = m.group(1)
        return f"{r[0:3]}.{r[3:6]}.{r[6:9]}-{r[9]}"
    return ""


def _achar_situacao(row: str) -> str:
    n = _norm(row)
    for t in _SIT_TOKENS:
        if t in n:
            return t.upper()
    cells = _cells_dados(row)
    return cells[-1].upper() if cells else ""


def _achar_especie(row: str) -> str:
    """Célula alfabética mais longa que não seja NB/data/situação (usada no CRER)."""
    cand = []
    for c in _cells_dados(row):
        if not any(ch.isalpha() for ch in c):
            continue
        if re.search(r"\d{3}\.\d{3}", c) or re.fullmatch(r"[\d/.\- ]+", c):
            continue
        if _norm(c) in _SIT_TOKENS:
            continue
        cand.append(c)
    return max(cand, key=len) if cand else ""


async def _baixar_aba(ctx, nova, caminho: str) -> bool:
    """Carrega a aba do visualizador, baixa os bytes do PDF e fecha a aba."""
    with contextlib.suppress(Exception):
        await nova.wait_for_load_state("domcontentloaded", timeout=15000)
    ok = await _baixar_pdf(ctx, _pdf_url(nova.url), caminho)
    with contextlib.suppress(Exception):
        await nova.close()
    return ok


async def _clicar_abre_aba(page, ctx, fn_click, out, reg, nome_doc="documento",
                           alvo_norm="", garantido=False):
    """Executa fn_click() (async → bool de clicou) e aguarda um dos 3 desfechos:
    'aba' (abriu o PDF), 'sem_dados' (banner que MENCIONA o documento) ou 'bloqueio'
    (SILÊNCIO = antirobô → pausa e re-tenta). Gerador: yield-a os avisos ao vivo e
    grava o resultado em out['nova']/out['desfecho']."""
    nova, desfecho = None, "bloqueio"
    antes_ini = set(ctx.pages)
    for tentativa in range(1, _MAX_TENT + 1):
        antes = set(ctx.pages)
        await page.evaluate(_JS_MARCAR_BANNERS)  # marca banners RESIDUAIS como vistos
        clicou = await fn_click()
        if clicou:
            for _ in range(16):  # ~8s
                await page.wait_for_timeout(500)
                novas = [p for p in ctx.pages if p not in antes]
                if novas:  # abriu o PDF numa aba nova → prioridade sobre banner
                    nova, desfecho = novas[0], "aba"
                    break
                # "sem_dados" só p/ mensagem NOVA (não residual) que — havendo alvo —
                # menciona o documento. Assim um banner residual de outra linha não faz
                # falso "não retornou dados" (bug: a aba abria e ficava órfã).
                banner = _norm(await page.evaluate(_JS_BANNER_NOVO))
                if "nao retorn" in banner and (not alvo_norm or alvo_norm in banner):
                    desfecho = "sem_dados"
                    break
            if desfecho in ("aba", "sem_dados"):
                break
        # SILÊNCIO (nem aba nem aviso) → proteção antirobô ativada
        if tentativa < _MAX_TENT:
            cert = "" if garantido else " (provável)"
            yield reg(f"  🛡 proteção antirobô{cert} ativada em {nome_doc} — aguardando "
                      f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
            await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)
    # fecha abas órfãs (algum PDF que abriu mas não virou desfecho 'aba')
    if desfecho != "aba":
        for p in list(ctx.pages):
            if p not in antes_ini:
                with contextlib.suppress(Exception):
                    await p.close()
    out["nova"], out["desfecho"] = nova, desfecho
    await _ritmo(page)  # ritmo (delay por consulta + pausa de lote)


async def _sem_modal(page) -> bool:
    return not await page.evaluate(_JS_DIALOG_VIS)


async def _fechar_modal(page) -> None:
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_FECHAR_MODAL)
    for _ in range(8):  # aguarda o fade-out (opacity) sumir
        await page.wait_for_timeout(300)
        if await _sem_modal(page):
            return


async def _abrir_modal(page, label, out, reg, nome_doc="documento",
                       alvo_norm="", garantido=False):
    """Clica o 'Consultar' do rótulo e espera abrir o MODAL. Gerador: yield-a avisos ao
    vivo e grava out['estado'] = 'modal' | 'sem_dados' | 'bloqueio'."""
    # garante que nenhum modal antigo esteja visível antes de clicar
    for _ in range(6):
        if await _sem_modal(page):
            break
        await _fechar_modal(page)
    out["estado"] = "bloqueio"
    for tentativa in range(1, _MAX_TENT + 1):
        banner_antes = _norm(await page.evaluate(_JS_BANNER))
        clicou = await page.evaluate(_JS_CLK, label)
        if not clicou:
            clicou = await page.evaluate(_JS_CLK_CONTEM, label)
        if clicou:
            for _ in range(20):  # ~8s
                await page.wait_for_timeout(400)
                if await page.evaluate(_JS_DIALOG_VIS):
                    out["estado"] = "modal"
                    await _ritmo(page)  # ritmo após abrir o modal (é uma consulta)
                    return
                banner = _norm(await page.evaluate(_JS_BANNER))
                # sem_dados = banner NOVO que (se houver alvo) menciona o documento.
                if ("nao retorn" in banner and banner != banner_antes
                        and (not alvo_norm or alvo_norm in banner)):
                    out["estado"] = "sem_dados"
                    await _ritmo(page)
                    return
        # SILÊNCIO → proteção antirobô
        if tentativa < _MAX_TENT:
            cert = "" if garantido else " (provável)"
            yield reg(f"  🛡 proteção antirobô{cert} ativada em {nome_doc} — aguardando "
                      f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
            await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)


# Grupo 2 = abrem em MODAL. tipo "tabela": linhas com "Abrir PDF"; "hiscre": form de datas.
_GRUPO2 = [
    {"label": "Carta de Concessão", "tipo": "tabela", "nome": "CCON", "titulo": "carta"},
    {"label": "Comunicação de Resultado de Requerimento - CRER", "tipo": "tabela",
     "nome": "CRER", "titulo": "crer", "especie": True},
    {"label": "Histórico de Créditos", "tipo": "hiscre", "nome": "HISCRE",
     "titulo": "credito", "garantido": True},
    {"label": "Histórico de Empréstimos Consignados", "tipo": "tabela",
     "nome": "HISCONSIGNADOS", "titulo": "emprestimo"},
    {"label": "Laudo Médico", "tipo": "tabela", "nome": "LAUDO MEDICO", "titulo": "laudo"},
]


async def _baixar_modal_tabela(page, ctx, cfg, nome, pasta_destino, salvos, pulados, reg):
    """Modais-tabela (CCON/CRER/Consignados/Laudo): 'Abrir PDF' por linha de benefício."""
    linhas = await page.evaluate(_JS_LINHAS_MODAL)
    idxs = [i for i, l in enumerate(linhas) if _achar_nb_row(l)]
    if not idxs:
        pulados.append(f"{cfg['nome']} (sem linhas)")
        yield reg(f"  – {cfg['label']}: sem benefícios na tabela", "muted")
        return
    alvo = _norm(cfg.get("titulo") or cfg["label"])
    for i in idxs:
        row = linhas[i]
        nb = _achar_nb_row(row)
        sit = _achar_situacao(row)
        # Consignados: não faz sentido consultar benefício INDEFERIDO — pular (decisão
        # do usuário 2026-07-21) para poupar consultas e não disparar o antirobô à toa.
        if cfg["nome"] == "HISCONSIGNADOS" and "INDEFER" in sit:
            yield reg(f"  – HISCONSIGNADOS {nb}: benefício {sit} — não consultado", "muted")
            continue
        if cfg["nome"] == "CCON":
            base = f"CCON {nb} {sit}"
        elif cfg["nome"] == "CRER":
            esp = sat_utils.abreviar(_achar_especie(row))
            base = f"CRER {esp} {nb} {sit}"
        elif cfg["nome"] == "HISCONSIGNADOS":
            base = f"HISCONSIGNADOS {nb}"
        elif cfg["nome"] == "LAUDO MEDICO":
            base = f"LAUDO MEDICO {nb}"
        else:
            base = f"{cfg['nome']} {nb}"
        nome_arq = sat_utils.nome_final(base) + ".pdf"
        out = {}
        async for ev in _clicar_abre_aba(
                page, ctx, (lambda idx=i: page.evaluate(_JS_ABRIR_PDF_LINHA, idx)),
                out, reg, nome_doc=base, alvo_norm=alvo, garantido=cfg.get("garantido", False)):
            yield ev
        nova, desf = out["nova"], out["desfecho"]
        if desf == "aba" and nova:
            ok = await _baixar_aba(ctx, nova, os.path.join(pasta_destino, nome_arq))
            if ok:
                salvos.append(nome_arq)
                yield reg(f"  ✓ {nome_arq}", "ok")
            else:
                pulados.append(f"{nome_arq} (falha no download)")
                yield reg(f"  ✗ {nome_arq}: falha no download", "erro")
        elif desf == "sem_dados":
            pulados.append(f"{base} (sem dados)")
            yield reg(f"  – {base}: não retornou dados", "muted")
        else:
            pulados.append(f"{base} (antirobô — não recuperado)")
            yield reg(f"  ✗ {base}: proteção antirobô — não recuperado após {_MAX_TENT - 1} "
                      f"esperas de {_PAUSA_BLOQUEIO_MS // 1000}s", "erro")


async def _baixar_hiscre(page, ctx, pasta_destino, salvos, pulados, reg):
    """HISCRE: preenche De = (mês atual − 5 anos) e até = mês atual (MM/AAAA) e confirma."""
    from datetime import date
    hoje = date.today()
    de = f"{hoje.month:02d}/{hoje.year - 5}"
    ate = f"{hoje.month:02d}/{hoje.year}"
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_CAMPO_MODAL, [0, de])
        await page.wait_for_timeout(250)
        await page.evaluate(_JS_CAMPO_MODAL, [1, ate])
        await page.wait_for_timeout(250)
    nome_arq = "HISCRE 5 anos.pdf"
    out = {}
    async for ev in _clicar_abre_aba(
            page, ctx,
            (lambda: page.evaluate(_JS_BTN_MODAL, r"^(ok|consultar|abrir pdf|confirmar)$")),
            out, reg, nome_doc="HISCRE", alvo_norm="credito", garantido=True):
        yield ev
    nova, desf = out["nova"], out["desfecho"]
    if desf == "aba" and nova:
        ok = await _baixar_aba(ctx, nova, os.path.join(pasta_destino, nome_arq))
        if ok:
            salvos.append(nome_arq)
            yield reg(f"  ✓ {nome_arq} (De {de} até {ate})", "ok")
        else:
            pulados.append("HISCRE (falha no download)")
            yield reg("  ✗ HISCRE: falha no download", "erro")
    elif desf == "sem_dados":
        pulados.append("HISCRE (sem dados)")
        yield reg("  – HISCRE: não retornou dados", "muted")
    else:
        pulados.append("HISCRE (antirobô — não recuperado)")
        yield reg("  ✗ HISCRE: proteção antirobô — não recuperado (doc sempre existe)", "erro")


async def _grupo2_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
    """Baixa os documentos do Grupo 2 (cada um abre em modal)."""
    yield reg("Baixando documentos do Grupo 2 (modais)…", "novo")
    for cfg in _GRUPO2:
        alvo = _norm(cfg.get("titulo") or cfg["label"])
        garantido = cfg.get("garantido", False)
        out = {}
        async for ev in _abrir_modal(page, cfg["label"], out, reg, nome_doc=cfg["label"],
                                     alvo_norm=alvo, garantido=garantido):
            yield ev
        estado = out["estado"]
        if estado == "sem_dados":
            pulados.append(f"{cfg['nome']} (sem dados)")
            yield reg(f"  – {cfg['label']}: não retornou dados", "muted")
            continue
        if estado == "bloqueio":
            pulados.append(f"{cfg['nome']} (antirobô — não recuperado)")
            yield reg(f"  ✗ {cfg['label']}: proteção antirobô — não recuperado", "erro")
            continue
        if cfg["tipo"] == "hiscre":
            async for ev in _baixar_hiscre(page, ctx, pasta_destino, salvos, pulados, reg):
                yield ev
        else:
            async for ev in _baixar_modal_tabela(
                    page, ctx, cfg, nome, pasta_destino, salvos, pulados, reg):
                yield ev
        await _fechar_modal(page)


async def _pat_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
    """PAT / 2ª Via dos Processos: baixa todas as tarefas (todas as páginas) numa pasta
    temp e deduplica por (PROTOCOLO DE REQUERIMENTO interno + páginas), mantendo a
    PRINCIPAL e nomeando PROCADM (sat_utils.processar_pat)."""
    yield reg("Baixando processos administrativos (PAT / 2ª Via)…", "novo")
    out = {}
    async for ev in _abrir_modal(page, "Tarefas PAT", out, reg, nome_doc="Tarefas PAT",
                                 alvo_norm="tarefa", garantido=True):
        yield ev
    estado = out["estado"]
    if estado != "modal":  # rótulo alternativo
        out = {}
        async for ev in _abrir_modal(page, "Tarefas", out, reg, nome_doc="Tarefas PAT",
                                     alvo_norm="tarefa", garantido=True):
            yield ev
        estado = out["estado"]
    if estado == "sem_dados":
        pulados.append("PAT (sem dados)")
        yield reg("  – PAT: não retornou dados", "muted")
        return
    if estado != "modal":
        pulados.append("PAT (antirobô — não recuperado)")
        yield reg("  ✗ PAT: modal não abriu — proteção antirobô (doc sempre existe)", "erro")
        return

    tmp = Path(pasta_destino) / "_pat_tmp"
    with contextlib.suppress(Exception):
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    mapa: dict[str, dict] = {}
    seq = 0
    pagina = 1
    while True:
        linhas = await page.evaluate(_JS_LINHAS_MODAL)
        for i, row in enumerate(linhas):
            cells = _cells_dados(row)
            prot_i = next((k for k, c in enumerate(cells) if re.fullmatch(r"\d{6,}", c)), None)
            if prot_i is None:
                continue  # cabeçalho / linha "nenhum registro"
            prot = cells[prot_i]
            servico = cells[prot_i + 1] if prot_i + 1 < len(cells) else ""
            out = {}
            async for ev in _clicar_abre_aba(
                    page, ctx, (lambda idx=i: page.evaluate(_JS_ABRIR_PDF_LINHA, idx)),
                    out, reg, nome_doc=f"tarefa {prot}", garantido=True):
                yield ev
            nova, desf = out["nova"], out["desfecho"]
            if desf == "aba" and nova:
                seq += 1
                tmpnome = f"pat_{seq:03d}.pdf"
                ok = await _baixar_aba(ctx, nova, str(tmp / tmpnome))
                if ok:
                    mapa[tmpnome] = {"servico": servico, "protocolo": prot}
                    yield reg(f"  ↓ tarefa {prot} baixada", "muted")
                else:
                    yield reg(f"  ✗ tarefa {prot}: falha no download", "erro")
            elif desf == "sem_dados":
                yield reg(f"  – tarefa {prot}: sem dados", "muted")
            else:
                yield reg(f"  ✗ tarefa {prot}: proteção antirobô — não recuperado", "erro")
        avancou = await page.evaluate(_JS_PAG_PROX)
        if not avancou:
            break
        pagina += 1
        await _ritmo(page)  # avançar de página é uma consulta — ritmo governa a transição
        if pagina > 50:  # trava de segurança
            yield reg("  ⚠ PAT: >50 páginas — interrompido por segurança", "erro")
            break
    await _fechar_modal(page)

    if not mapa:
        pulados.append("PAT (sem tarefas)")
        yield reg("  – PAT: nenhuma tarefa encontrada", "muted")
    else:
        try:
            mant, desc = sat_utils.processar_pat(str(tmp), pasta_destino, mapa)
            yield reg(f"  PAT: {len(mapa)} tarefa(s) baixada(s) → {len(mant)} principal(is) "
                      f"mantido(s), {len(desc)} descartada(s) (subtarefa/duplicata)", "novo")
            for _f, nm in mant:
                salvos.append(nm)
                yield reg(f"  ✓ {nm}", "ok")
            for _a, motivo in desc:
                yield reg(f"  🗑 {motivo}", "muted")
        except Exception as e:  # noqa: BLE001
            pulados.append(f"PAT (dedup falhou: {e})")
            yield reg(f"  ✗ PAT: deduplicação/nomeação falhou: {e}", "erro")
    with contextlib.suppress(Exception):
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


async def baixar_cidadao_stream(cpf: str, nome: str, pasta_destino: str,
                                delay_s: float = 5.0, bloqueio_s: float = 45.0):
    """Consulta o CPF no SAT (usuário resolve o CAPTCHA na janela) e baixa o Grupo 1
    (1 clique), o Grupo 2 (modais: CCON, CRER, HISCRE, HISCONSIGNADOS, LAUDO MEDICO) e
    o PAT (2ª Via dos Processos, com deduplicação). `delay_s` = ritmo preventivo entre
    consultas (padrão 5s; 0 desliga). `bloqueio_s` = espera ao detectar antirobô
    (padrão 45s). Gera eventos SSE; termina com {'done':True,'resultado':{...}}."""
    global _RITMO_MS, _PAUSA_BLOQUEIO_MS, _consultas
    _RITMO_MS = max(0, int(float(delay_s) * 1000))
    _PAUSA_BLOQUEIO_MS = max(0, int(float(bloqueio_s) * 1000))
    _consultas = 0  # zera o contador de lote (pausa a cada _LOTE_N)
    Path(pasta_destino).mkdir(parents=True, exist_ok=True)
    salvos: list[str] = []
    pulados: list[str] = []
    log_msgs: list[dict] = []

    def reg(msg: str, level: str = "info") -> dict:
        e = _ev(msg, level)
        log_msgs.append(e)
        return e

    browser, page = await _connect()
    ctx = page.context
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1000)
        if not _status_dict(page.url)["logado"]:
            yield {"done": True, "resultado": {"ok": False,
                   "detalhe": "Sessão do SAT expirada — clique em 'Abrir SAT / Login' e refaça o login."}}
            return

        # --- consulta por CPF ---
        dig = "".join(c for c in cpf if c.isdigit())
        yield reg(f"Consultando o CPF {cpf}…", "novo")
        try:
            await page.click('[id="formFiltrosPF:txtCpf"]', timeout=10000)
            await page.fill('[id="formFiltrosPF:txtCpf"]', "")
            await page.type('[id="formFiltrosPF:txtCpf"]', dig, delay=25)
            await page.click('[id="formFiltrosPF:btnConsultarPF"]', timeout=10000)
        except Exception as e:  # noqa: BLE001
            yield {"done": True, "resultado": {"ok": False, "detalhe": f"Falha ao iniciar a consulta: {e}"}}
            return
        yield reg("⚠ Resolva o CAPTCHA na JANELA do SAT e clique em 'Enviar'. Aguardando… (até 3 min)", "info")
        try:
            await page.wait_for_function(_JS_TEM_CONSULTAR, timeout=180000)
        except Exception:
            yield {"done": True, "resultado": {"ok": False,
                   "detalhe": "Não detectei os dados do cidadão (CAPTCHA não resolvido a tempo?)."}}
            return
        await page.wait_for_timeout(800)
        yield reg("Cidadão localizado. Baixando os documentos (Grupo 1)…", "ok")

        # --- Grupo 1 ---
        for label, prefixo, garantido in _GRUPO1:
            alvo_norm = _norm(label)
            nova, desfecho = None, "bloqueio"
            # Três desfechos por clique: "aba" (abriu o PDF) · "sem_dados" (banner que
            # MENCIONA o documento) · "bloqueio" (silêncio anti-robô → re-tentar).
            for tentativa in range(1, _MAX_TENT + 1):
                antes = set(ctx.pages)
                clicou = await page.evaluate(_JS_CLK, label)
                if clicou:
                    for _ in range(16):  # aguarda ~8s por aba ou banner do documento
                        await page.wait_for_timeout(500)
                        novas = [p for p in ctx.pages if p not in antes]
                        if novas:
                            nova, desfecho = novas[0], "aba"
                            break
                        banner = _norm(await page.evaluate(_JS_BANNER))
                        if "nao retorn" in banner and alvo_norm in banner:
                            desfecho = "sem_dados"
                            break
                if desfecho in ("aba", "sem_dados"):
                    break
                # SILÊNCIO: nem aba nem aviso "não retornou dados" p/ este documento →
                # proteção antirobô ativada → pausa (fixo) e re-tenta
                if tentativa < _MAX_TENT:
                    cert = "" if garantido else " (provável)"
                    yield reg(f"  🛡 proteção antirobô{cert} ativada em {prefixo} — aguardando "
                              f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
                    await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)
            if desfecho == "aba" and nova:
                with contextlib.suppress(Exception):
                    await nova.wait_for_load_state("domcontentloaded", timeout=15000)
                nome_arq = sat_utils.nome_final(f"{prefixo} {nome}") + ".pdf"
                ok = await _baixar_pdf(ctx, _pdf_url(nova.url), os.path.join(pasta_destino, nome_arq))
                with contextlib.suppress(Exception):
                    await nova.close()
                if ok:
                    salvos.append(nome_arq)
                    yield reg(f"  ✓ {nome_arq}", "ok")
                else:
                    pulados.append(f"{prefixo} (falha no download)")
                    yield reg(f"  ✗ {prefixo}: falha no download", "erro")
            elif desfecho == "sem_dados":
                pulados.append(f"{prefixo} (sem dados)")
                yield reg(f"  – {prefixo}: não retornou dados", "muted")
            else:  # bloqueio persistente após as tentativas
                pulados.append(f"{prefixo} (antirobô — não recuperado)")
                yield reg(f"  ✗ {prefixo}: proteção antirobô — não recuperado após "
                          f"{_MAX_TENT - 1} esperas de {_PAUSA_BLOQUEIO_MS // 1000}s — "
                          f"reveja manualmente", "erro")
            await _ritmo(page)  # ritmo (delay por consulta + pausa de lote)

        # --- Grupo 2 (modais) ---
        with contextlib.suppress(Exception):
            await _fechar_modal(page)
        async for ev in _grupo2_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
            yield ev

        # --- PAT (2ª Via dos Processos) ---
        with contextlib.suppress(Exception):
            await _fechar_modal(page)
        async for ev in _pat_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
            yield ev

        det = f"{len(salvos)} documento(s) baixado(s)."
        yield {"done": True, "resultado": {"ok": True, "detalhe": det,
               "salvos": salvos, "pulados": pulados, "pasta": pasta_destino}}
    finally:
        # grava o LOG do download na pasta do PAJ (disponível na coluna "Log" da aba)
        with contextlib.suppress(Exception):
            import json as _json
            from datetime import datetime as _dt
            Path(pasta_destino, "_log_download.json").write_text(
                _json.dumps({"ts": _dt.now().isoformat(timespec="seconds"),
                             "cpf": cpf, "nome": nome, "salvos": salvos,
                             "pulados": pulados, "eventos": log_msgs},
                            ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(Exception):
            await browser.close()
