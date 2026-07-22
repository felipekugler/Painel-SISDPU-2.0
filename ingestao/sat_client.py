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


def _porta_aberta(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def _pw_start():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _ensure_chromium() -> None:
    """Garante um Chromium persistente rodando com remote-debugging (processo separado).
    Lança-o apontando já para a consulta do SAT (o usuário loga na janela)."""
    if _porta_aberta(_CDP_PORT):
        return
    pw = await _pw_start()
    exe = pw.chromium.executable_path
    _PROFILE.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP → sobrevive ao painel
        flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        [exe,
         f"--remote-debugging-port={_CDP_PORT}",
         f"--user-data-dir={_PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=PDFViewerUpdate,PdfUnseasoned",
         SAT_CONSULTA],
        creationflags=flags, close_fds=True,
    )
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


# ---------------------------------------------------------------------------
# Download dos documentos (Fase 2). Etapa 2a: consulta por CPF + Grupo 1.
# ---------------------------------------------------------------------------
# Anti-bloqueio: nº de tentativas por documento e pausa base (× tentativa) entre elas.
_MAX_TENT = 3
_PAUSA_MS = 2500

# Grupo 1 = abrem direto com 1 clique em "Consultar". (rótulo no painel, prefixo do nome)
_GRUPO1 = [
    ("CNIS - Atividades do CNIS v.3", "CNIS - Atividades"),
    ("CNIS - Dados Cadastrais do CNIS", "CNIS - Dados Cadastrais"),
    ("CNIS - Elos do CNIS", "CNIS - Elos"),
    ("CNIS - Microfichas do CNIS v.3", "CNIS - Microfichas"),
    ("CNIS - Remunerações do CNIS v.3", "CNIS - Remuneracoes"),
    ("CNIS - Vínculos do CNIS v.3", "CNIS - Vinculos"),
    ("Declaração de Benefícios", "Declaracao de Beneficios"),
    ("Requerimentos do SIBE", "Requerimentos do SIBE"),
    ("Revisão de Benefício - Artigo 29", "Revisao de Beneficio - Artigo 29"),
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


async def baixar_cidadao_stream(cpf: str, nome: str, pasta_destino: str):
    """Consulta o CPF no SAT (usuário resolve o CAPTCHA na janela) e baixa o Grupo 1.
    Gera eventos SSE; termina com {'done':True,'resultado':{...}}. [Etapa 2a]"""
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
        for label, prefixo in _GRUPO1:
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
                # silêncio: nem aba nem aviso "não retornou dados" p/ este documento →
                # provável bloqueio anti-robô → pausa e tenta de novo
                if tentativa < _MAX_TENT:
                    yield reg(f"  … {prefixo}: sem resposta do SAT — pausando e repetindo "
                              f"({tentativa}/{_MAX_TENT})", "muted")
                    await page.wait_for_timeout(_PAUSA_MS * tentativa)
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
                pulados.append(f"{prefixo} (sem resposta após {_MAX_TENT} tentativas)")
                yield reg(f"  ✗ {prefixo}: sem resposta do SAT após {_MAX_TENT} tentativas "
                          f"(bloqueio?) — reveja manualmente", "erro")

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
