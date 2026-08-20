"""Cliente SISDPU via Playwright headless (async).

Portado de dpu-workspace-main/dpuscript/mcp_servers/sisdpu/client.py.
Mantido como funcoes modulo-level com estado global de browser/page para
simplificar uso; concorrencia de sincronizacao e' serializada por um
asyncio.Lock em services/sync_service.py.

Credenciais vem das env vars SISDPU_USERNAME e SISDPU_PASSWORD (carregadas do
.env pelo config.py).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeoutError
import contextlib


SISDPU_URL = "https://sisdpu.dpu.def.br/sisdpu"

# Logger dedicado — os logs `[busca-paj]` vao para logs/app.log (handler
# configurado em app.py com rotacao diaria). Tambem aparecem no console
# quando o painel e' iniciado num terminal visivel.
_log = logging.getLogger("sisdpu.busca")

# Diretorio onde despejamos screenshot + HTML quando a busca via campo global
# falha — facilita o diagnostico (DOM real do SISDPU vs nossa heuristica).
_DEBUG_DIR = Path(__file__).resolve().parents[1] / "logs" / "sisdpu_debug"

# Retencao do diretorio de debug: pra nao acumular indefinidamente (cada dump
# tem screenshot+HTML+body, ~1MB), mantem soh os N mais recentes. Suficiente
# pra investigar a janela ativa de problemas sem inflar o repositorio.
_DEBUG_DIR_RETER = 20


def _purgar_debug_dir_antigos(reter: int = _DEBUG_DIR_RETER) -> None:
    """Remove subpastas mais antigas de _DEBUG_DIR, mantendo apenas as `reter`
    mais recentes (por mtime). Best-effort — qualquer erro e' suprimido pra
    nao quebrar o fluxo principal."""
    if not _DEBUG_DIR.exists():
        return
    with contextlib.suppress(Exception):
        import shutil

        subpastas = [p for p in _DEBUG_DIR.iterdir() if p.is_dir()]
        if len(subpastas) <= reter:
            return
        subpastas.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for antigo in subpastas[reter:]:
            with contextlib.suppress(Exception):
                shutil.rmtree(antigo)


class CredenciaisInvalidas(Exception):
    """SISDPU rejeitou usuario/senha no login."""

    pass


_playwright: Any = None
_browser: Browser | None = None
_page: Page | None = None


# JS que checa se o PrimeFaces terminou todos os AJAX pendentes — usado via
# `page.wait_for_function` (que ja' faz o polling com timeout), entao so'
# precisa devolver um booleano, sem Promise/setTimeout proprio.
_WAIT_PF_AJAX = """
() => (
    typeof PrimeFaces === 'undefined'
    || !PrimeFaces.ajax
    || !PrimeFaces.ajax.Queue
    || PrimeFaces.ajax.Queue.isEmpty()
)
"""


def _sel(element_id: str) -> str:
    """Gera seletor CSS para IDs JSF (que contem ':')."""
    return f'[id="{element_id}"]'


async def _get_page() -> Page:
    """Retorna a page Playwright, criando browser se necessario.

    O Chromium e' iniciado com flags que FORCAM download de PDFs em vez de
    abrir o preview interno — importante pro `listar_arquivos_paj`, ja que
    queremos capturar o arquivo bruto via evento `download`, nao previsualizar.
    """
    global _playwright, _browser, _page
    if _page is not None:
        try:
            # Acessa atributo so para verificar que a page ainda esta viva;
            # se foi fechada, levanta exception e caimos no reset abaixo.
            _ = _page.url
            return _page
        except Exception:
            _page = None
            _browser = None

    if _playwright is None:
        _playwright = await async_playwright().start()

    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            # Desabilita o PDF viewer interno do Chromium: PDFs viram download
            "--disable-features=PDFViewerUpdate,PdfUnseasoned",
        ],
    )
    context = await _browser.new_context(
        viewport={"width": 1280, "height": 1024},
        accept_downloads=True,
    )
    _page = await context.new_page()
    return _page


async def _wait_pf_ajax(page: Page, timeout: int = 10000) -> None:
    """Espera o PrimeFaces terminar AJAX pendente, com timeout de seguranca.

    BUG HISTORICO (corrigido aqui): a versao anterior chamava
    `page.evaluate(_WAIT_PF_AJAX, timeout=timeout)` — mas `Page.evaluate()`
    do Playwright NAO tem parametro `timeout` (a assinatura e' so'
    `evaluate(expression, arg=None)`). Isso levantava `TypeError` em TODA
    chamada, sempre engolido pelo `except` abaixo, que sempre caia no
    fallback fixo de 1 segundo — ou seja, esta funcao NUNCA de fato
    consultava `PrimeFaces.ajax.Queue.isEmpty()`; so' esperava 1s cego,
    independente do `timeout` pedido (5000/10000/15000ms nos varios call
    sites).

    Na pratica isso corrompia silenciosamente a "segunda passagem" que
    extrai `descricao_retorno` de PAJs com "Atendimento de retorno": o
    passo 1 clica em TODOS os togglers de expansao de uma vez (AJAX em
    paralelo do PrimeFaces), o passo 2 deveria dar tempo de todos
    assentarem antes do passo 3 varrer o DOM — mas so' esperava 1s fixo.
    Em PAJs com varios retornos, os ultimos cliques disparados no loop
    (que caem nos seqs mais antigos, mais abaixo na tabela) muitas vezes
    ainda estavam pendentes quando o DOM era lido — o campo ficava vazio
    (`descricao_retorno: ''`) sem erro nenhum, sem log nenhum. Confirmado
    empiricamente: num PAJ com 13 "Atendimento de retorno", os 2 ultimos
    da lista ficaram vazios, os outros 11 vieram certos.

    `page.wait_for_function` e' a API correta do Playwright pra isso: ela
    mesma faz o polling (com timeout de verdade), entao `_WAIT_PF_AJAX`
    virou uma funcao booleana simples em vez de uma Promise com
    setTimeout proprio."""
    try:
        await page.wait_for_function(_WAIT_PF_AJAX, timeout=timeout)
    except Exception:
        await page.wait_for_timeout(1000)


def _obter_credenciais() -> tuple[str, str]:
    """Credenciais do SISDPU: primeiro do COFRE cifrado (se destrancado); senão,
    fallback para o .env (SISDPU_USERNAME/PASSWORD) durante a transição. Levanta
    RuntimeError com mensagem clara se nenhuma fonte tiver credenciais."""
    with contextlib.suppress(Exception):
        from services import cofre_service
        creds = cofre_service.get_credenciais("sisdpu")
        if creds:
            return creds
    user = os.environ.get("SISDPU_USERNAME", "")
    senha = os.environ.get("SISDPU_PASSWORD", "")
    if user and senha:
        return user, senha
    # Nenhuma fonte disponível — distingue "cofre bloqueado" de "nada cadastrado".
    with contextlib.suppress(Exception):
        from services import cofre_service
        if cofre_service.configurado() and not cofre_service.destrancado():
            raise RuntimeError(
                "Cofre de credenciais bloqueado — desbloqueie com a senha-mestra "
                "para acessar o SISDPU.")
    raise RuntimeError(
        "Credenciais do SISDPU não configuradas — cadastre-as no cofre "
        "(Configurações) ou defina SISDPU_USERNAME/PASSWORD no .env.")


async def _login() -> None:
    """Faz login no SISDPU."""
    user, senha = _obter_credenciais()
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/login.xhtml", wait_until="domcontentloaded")
    await page.wait_for_selector(_sel("frmLogin:epaj_input_usuario"), timeout=15000)
    await page.fill(_sel("frmLogin:epaj_input_usuario"), user)
    await page.fill(_sel("frmLogin:epaj_input_senha"), senha)
    await page.evaluate('PrimeFaces.ab({s:"frmLogin:loginButton",f:"frmLogin"})')
    try:
        await page.wait_for_url("**/caixaEntrada**", timeout=15000)
    except PWTimeoutError:
        # Nao chegou na caixa — checar se SISDPU recusou as credenciais.
        # PrimeFaces exibe erros em .ui-messages-error, .mensagem-erro, ou
        # um <div class="ui-growl-message"> com texto de usuario/senha.
        msg_recusa = await page.evaluate(
            """() => {
                const seletores = [
                    '.ui-messages-error-detail',
                    '.ui-messages-error',
                    '.mensagem-erro',
                    '.ui-growl-message',
                    '#frmLogin .ui-message-error',
                ];
                for (const s of seletores) {
                    const el = document.querySelector(s);
                    if (el && el.innerText && el.innerText.trim()) {
                        return el.innerText.trim();
                    }
                }
                // fallback: varrer body por frase caracteristica
                const body = (document.body.innerText || '').toLowerCase();
                const pistas = [
                    'usu\u00e1rio ou senha', 'usuario ou senha',
                    'senha incorreta', 'senha inv\u00e1lida', 'senha invalida',
                    'credenciais inv\u00e1lidas', 'credenciais invalidas',
                    'login inv\u00e1lido', 'login invalido',
                ];
                for (const p of pistas) {
                    const i = body.indexOf(p);
                    if (i >= 0) return body.substr(Math.max(0, i - 20), 120);
                }
                return null;
            }"""
        )
        if msg_recusa:
            with contextlib.suppress(Exception):
                from services import conexao_service
                conexao_service.marcar_erro("O SISDPU recusou o usuário/senha.")
            raise CredenciaisInvalidas(msg_recusa[:200]) from None
        # Nao foi login fail explicito — repropaga como timeout.
        raise
    # Login OK — sessão saudável; limpa qualquer conflito/erro anterior.
    with contextlib.suppress(Exception):
        from services import conexao_service
        conexao_service.marcar_conectado()


async def _ensure_logged_in() -> None:
    """Garante que estamos logados. Refaz login se sessao expirou."""
    global _browser, _page
    page = await _get_page()
    try:
        url = page.url
        if "login" in url or not url.startswith("http"):
            await _login()
            return
        await page.goto(
            f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        await page.wait_for_timeout(500)
        if "login" in page.url:
            await _login()
    except Exception:
        try:
            if _browser:
                await _browser.close()
        except Exception:
            pass
        _browser = None
        _page = None
        await _login()


async def caixa_de_entrada() -> dict:
    """Navega para a caixa de entrada e extrai os itens listados."""
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(2000)
    await _wait_pf_ajax(page, timeout=10000)

    body = await page.inner_text("body")

    table_data = await page.evaluate(
        """
        () => {
            const tables = document.querySelectorAll(
                '.ui-datatable-data tr, .ui-datalist-content .ui-datalist-item'
            );
            if (tables.length > 0) {
                return Array.from(tables).map(tr => tr.innerText.trim())
                    .filter(t => t.length > 0);
            }
            const allTr = document.querySelectorAll('table tbody tr');
            return Array.from(allTr).map(tr => tr.innerText.trim())
                .filter(t => t.length > 0);
        }
        """
    )

    return {
        "url": page.url,
        "itens_tabela": table_data or [],
        "texto_pagina": body[:5000],
    }


def parse_item_caixa(item_raw: str) -> dict | None:
    """Extrai {paj, oficio, assistido, data, desc, etiqueta} de uma linha da caixa.

    Aceita etiquetas SISDPU (rotulos livres que o Defensor aplica na caixa, tipo
    "Aleg finais", "Urgente", etc.) presentes como prefixo antes do PAJ. O PAJ e'
    capturado com re.search (posicional), e o texto anterior vira 'etiqueta'.

    Portado de preparar_pajs.py._parse_item.
    """
    parts = [p.strip() for p in item_raw.split("\t")]
    header = parts[0] if parts else ""
    m = re.search(r"(\d{4}/\d{3}-\d+)\s*\n?\(?\s*(.*?)\s*\)?$", header, re.DOTALL)
    if not m:
        return None
    paj = m.group(1).strip()
    oficio = m.group(2).replace("E.", "").strip()
    # Etiqueta = texto ANTES do PAJ (se houver). Strip pra limpar espacos/newlines.
    etiqueta = header[: m.start(1)].strip()
    assistido = parts[1] if len(parts) > 1 else ""
    data = parts[2] if len(parts) > 2 else ""
    descricao = parts[-1] if len(parts) >= 5 else ""
    return {
        "paj": paj,
        "oficio": oficio[:120],
        "assistido": assistido[:120],
        "data": data,
        "desc": descricao,
        "etiqueta": etiqueta[:80],
    }


# JS extraido do CADASTRO DE PESSOA FISICA de cada assistido (pagina separada
# do detalhamento — cadastroAssistidoPessoaFisicaCombotoes.xhtml): telefones
# (com observacao), emails e enderecos. Os valores ficam em <input>/<select>
# (formulario), entao lemos .value / opcao selecionada — nao textContent.
#  - Contatos: em #tabelaContatos. Cada contato tem um input `pessoaContatoDescricao`
#    (o numero/email) cujo label ("Telefone:"/"Email:"/"Fax:") indica o tipo; a
#    observacao e' o input irmao `...pessoaContatoDescricaoTelefone` de mesmo indice.
#  - Enderecos: inputs/selects `pessoaEnderecoTipo/Logradouro/Numero/Complemento/
#    Bairro/Cep` + selects `enderecoUF`/`enderecoMunicipio`, agrupados pelo indice `:N:`.
_JS_CADASTRO_PF = r"""
() => {
    const val = (el) => el.tagName === "SELECT"
        ? (((el.options[el.selectedIndex] || {}).text) || "").replace(/\s+/g, " ").trim()
        : (el.value || "").replace(/\s+/g, " ").trim();

    const telefones = [], emails = [], enderecos = [];

    const tc = document.getElementById("tabelaContatos");
    if (tc) {
        const inputs = Array.from(tc.querySelectorAll("input"));
        for (const el of inputs) {
            const name = el.name || el.id || "";
            const m = name.match(/:(\d+):pessoaContatoDescricao$/);
            if (!m) continue;
            const valor = val(el);
            if (!valor) continue;
            let tipo = "";
            if (el.id) {
                const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (l) tipo = l.textContent.trim();
            }
            const idx = m[1];
            let obs = "";
            const o = inputs.find(x =>
                new RegExp(":" + idx + ":pessoaContatoDescricaoTelefone$").test(x.name || x.id || ""));
            if (o) obs = val(o);
            if (/email/i.test(tipo)) emails.push(valor);
            else if (/telefone/i.test(tipo)) telefones.push({ numero: valor, obs: obs });
            // Fax e outros tipos sao ignorados
        }
    }

    const chaves = ["pessoaEnderecoTipo","pessoaEnderecoLogradouro","pessoaEnderecoNumero",
                    "pessoaEnderecoComplemento","pessoaEnderecoBairro","enderecoMunicipio",
                    "enderecoUF","pessoaEnderecoCep"];
    const porIdx = {};
    for (const el of document.querySelectorAll("input,select")) {
        const name = el.name || el.id || "";
        for (const k of chaves) {
            const mm = name.match(new RegExp(":(\\d+):" + k + "$"));
            if (mm) { (porIdx[mm[1]] = porIdx[mm[1]] || {})[k] = val(el); break; }
        }
    }
    for (const idx of Object.keys(porIdx)) {
        const e = porIdx[idx];
        if (!(e.pessoaEnderecoLogradouro || e.pessoaEnderecoBairro || e.pessoaEnderecoCep)) continue;
        enderecos.push({
            tipo: e.pessoaEnderecoTipo || "", logradouro: e.pessoaEnderecoLogradouro || "",
            numero: e.pessoaEnderecoNumero || "", complemento: e.pessoaEnderecoComplemento || "",
            bairro: e.pessoaEnderecoBairro || "", municipio: e.enderecoMunicipio || "",
            uf: e.enderecoUF || "", cep: e.pessoaEnderecoCep || ""
        });
    }

    return { telefones, emails, enderecos };
}
"""


async def _extrair_contatos_pf(page: Page, id_processo: str, assistidos_pf: list[dict]) -> dict:
    """Visita o Cadastro de Pessoa Fisica de cada assistido PF vinculado ao PAJ
    (pagina separada do detalhamento) e extrai telefones/emails/enderecos.

    ATENCAO: navega a `page` pra fora do detalhamento — quem chama e' responsavel
    por trazer a `page` de volta pro detalhamento depois (anexos/documentos,
    chamados logo apos movimentacoes_paj/buscar_paj_global retornarem, dependem
    da page estar la).
    """
    telefones: list[dict] = []
    emails: list[str] = []
    enderecos: list[dict] = []
    for a in assistidos_pf or []:
        idp = a.get("idPessoa")
        if not idp:
            continue
        try:
            await page.goto(
                f"{SISDPU_URL}/pages/atendimento/cadastroAssistidoPessoaFisicaCombotoes.xhtml"
                f"?idProcesso={id_processo}&idPessoa={idp}",
                wait_until="domcontentloaded",
            )
            await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(600)
            cad = await page.evaluate(_JS_CADASTRO_PF)
        except Exception as e:  # noqa: BLE001
            _log.warning("contatos: cadastro PF idPessoa=%s falhou: %s", idp, e)
            continue
        telefones.extend(cad.get("telefones", []) or [])
        emails.extend(cad.get("emails", []) or [])
        enderecos.extend(cad.get("enderecos", []) or [])
    return {"telefones": telefones, "emails": emails, "enderecos": enderecos}


async def _extrair_contatos_e_voltar(page: Page, dados: dict) -> None:
    """Roda `_extrair_contatos_pf` (se houver assistidos_pf) e SEMPRE traz a
    `page` de volta pro detalhamento antes de retornar — passo final de
    movimentacoes_paj/buscar_paj_global, chamado so' depois de toda a extracao
    que depende do detalhamento ja ter terminado.

    Modifica `dados` in-place (telefones/emails/enderecos).
    """
    assistidos_pf = dados.get("assistidos_pf") or []
    url_detalhe = dados.get("url", "")
    if not assistidos_pf or not url_detalhe:
        return
    id_match = re.search(r"[?&]id=(\d+)", url_detalhe)
    if not id_match:
        return
    try:
        cad = await _extrair_contatos_pf(page, id_match.group(1), assistidos_pf)
        dados["telefones"] = cad["telefones"]
        dados["emails"] = cad["emails"]
        dados["enderecos"] = cad["enderecos"]
    finally:
        with contextlib.suppress(Exception):
            await page.goto(url_detalhe, wait_until="domcontentloaded")
            await _wait_pf_ajax(page, timeout=10000)


async def movimentacoes_paj(numero: str, ano: str, unidade: str = "44",
                            esperas_curtas: bool = False) -> dict:
    """Retorna dados completos do PAJ incluindo MOVIMENTACOES com URLs de peca.

    Fluxo: Caixa de Entrada -> clica no TEXTO DO PAJ -> Detalhamento do Processo.

    `esperas_curtas=True` (opt-in) reduz as esperas fixas pós-navegacao — usado pela
    aba Retornos, que so' le movimentacoes/narrativa (nao baixa anexos). O DEFAULT
    (`False`) preserva a temporizacao original usada pelo Sincronizador (que depende
    dos buffers para extrair as 500 movs e abrir os popups de anexo)."""
    _t_goto = 600 if esperas_curtas else 2000
    _t_click = 900 if esperas_curtas else 3000
    await _ensure_logged_in()
    page = await _get_page()

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(_t_goto)
    await _wait_pf_ajax(page, timeout=10000)

    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"

    clicked = await page.evaluate(
        """
        (pajTexto) => {
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes(pajTexto)) { a.click(); return true; }
            }
            return false;
        }
        """,
        paj_texto,
    )

    if not clicked:
        clicked = await page.evaluate(
            """
            (numero) => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes(numero)) { a.click(); return true; }
                }
                return false;
            }
            """,
            numero,
        )

    if not clicked:
        return {"erro": f"PAJ {paj_texto} não encontrado na caixa de entrada"}

    await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(_t_click)

    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            const assistidoMatch = body.match(
                /Assistido\\(s\\)[\\t\\s]+([^\\n]+)/
            );
            if (assistidoMatch) r.assistido = assistidoMatch[1].trim();

            // Extrai CPF da tabela "Assistido(s) Pessoa(s) Física:"
            // Seletor: primeira linha de dados, terceira célula (coluna CPF)
            const cpfCell = document.querySelector(
                '#detalhamentoForm\\\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)'
            );
            if (cpfCell) {
                const cpfText = cpfCell.textContent.trim().split('\\n')[0];
                if (cpfText && /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfText)) {
                    r.cpf_assistido = cpfText;
                }
            }

            // Foto do assistido — o SISDPU embute a imagem direto como data URI
            // base64 num <img id="detalhamentoForm:fotoAssistido">. Nem todo PAJ
            // tem foto cadastrada (o elemento so existe quando ha uma).
            const fotoImg = document.querySelector('#detalhamentoForm\\\\:fotoAssistido');
            if (fotoImg && fotoImg.src && fotoImg.src.startsWith('data:image')) {
                r.foto_assistido_src = fotoImg.src;
            }

            // App SISDPU — o cabecalho tem o <span id="detalhamentoForm:lnkNotificarAssistido">
            // que fica VAZIO quando o assistido nao aderiu ao app, e contem um <a>
            // (icone, title "Este PAJ possui acompanhamento pelo App") quando aderiu.
            // Reforcamos com a frase de adesao que aparece nas movimentacoes.
            const spanApp = document.getElementById('detalhamentoForm:lnkNotificarAssistido');
            const temLinkApp = !!(spanApp && spanApp.querySelector('a'));
            const temFraseApp = /Ades[aã]o ao Termo de Responsabilidade do Aplicativo do SISDPU/i.test(body);
            r.contato_app_sisdpu = temLinkApp || temFraseApp;

            // Assistidos PF — uma linha por pessoa vinculada ao PAJ (tabela
            // "Assistido(s) Pessoa(s) Física:"). Colunas: [toggle, Nome, CPF, RG,
            // ações]. O assistido PRINCIPAL é o ÚNICO sem o ícone "Excluir este
            // assistido" na última célula (os secundários têm esse ícone; o
            // principal só tem "Criar um novo PAJ para esse assistido") — não dá
            // pra assumir que é sempre a primeira linha do DOM (já vimos PAJ real
            // onde o principal aparece na 2ª linha). idPessoa tambem sai daqui —
            // usado depois pra extrair telefone/email/endereco (pagina separada).
            const tblPF = document.getElementById('detalhamentoForm:dataTableListaAssistidosPF');
            if (tblPF) {
                const assistidosPf = [];
                for (const tr of tblPF.querySelectorAll('tbody tr')) {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 3) continue;
                    const linkPessoa = cells[1]?.querySelector('a[href*="cadastroAssistidoPessoaFisica"]');
                    const href = linkPessoa?.getAttribute('href') || '';
                    const mp = href.match(/idPessoa=(\\d+)/);
                    if (!mp) continue;
                    // "(N PAJ'S)" e' um contador de outros PAJs da mesma pessoa
                    // que o SISDPU concatena no link — nao faz parte do nome.
                    let nome = (linkPessoa.textContent || '').trim();
                    nome = nome.replace(/\\s*\\(\\d+\\s*PAJ'?S\\)\\s*$/i, '').trim();
                    const cpfTexto = (cells[2]?.textContent || '').trim().split('\\n')[0].trim();
                    const cpf = /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfTexto) ? cpfTexto : '';
                    const ultimaCel = cells[cells.length - 1];
                    const temExcluir = !!(ultimaCel && ultimaCel.querySelector('a[title*="Excluir"], img[alt="Excluir"]'));
                    assistidosPf.push({ idPessoa: mp[1], nome, cpf, principal: !temExcluir });
                }
                if (assistidosPf.length) {
                    r.assistidos_pf = assistidosPf;
                    // Corrige r.cpf_assistido (abaixo, so' olhava a 1a linha do DOM,
                    // nem sempre o principal) pelo CPF do assistido principal de fato.
                    const principalPf = assistidosPf.find(a => a.principal) || assistidosPf[0];
                    if (principalPf.cpf) r.cpf_assistido = principalPf.cpf;
                }
            }

            const pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            }

            const oficioMatch = body.match(/Oficio[\\t\\s]+([^\\n]+)/) ||
                                body.match(/Ofício[\\t\\s]+([^\\n]+)/);
            if (oficioMatch) r.oficio = oficioMatch[1].trim();

            // Pretensao — formato "Pretensão  Area >> Subarea >> ..."
            const pretensaoMatch = body.match(
                /Pretens[ãa]o[\\t\\s]+([^\\n]+)/
            );
            if (pretensaoMatch) r.pretensao = pretensaoMatch[1].trim();

            // Data de abertura
            const aberturaMatch = body.match(
                /(?:Data\\s+de\\s+Abertura|Dt\\.?\\s*Abertura)[\\t\\s]+([^\\n]+)/i
            );
            if (aberturaMatch) r.data_abertura = aberturaMatch[1].trim();

            // Extração de Processo(s) Judicial(is) — TODOS os vinculados ao PAJ (a
            // tabela "Processo Judicial" pode ter mais de uma linha de dados).
            // IMPORTANTE: Todos os campos capturam DADOS COMPLETOS SEM LIMITE DE TAMANHO
            const extrairProcessosJudiciais = () => {
              const processos = [];

              // Buscar tabela de "Processo Judicial"
              const tabelas = document.querySelectorAll('table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro');
              let tabelaProcesso = null;

              for (const tabela of tabelas) {
                const th = tabela.querySelector('th');
                if (th && th.textContent.includes('Processo Judicial')) {
                  tabelaProcesso = tabela;
                  break;
                }
              }

              if (!tabelaProcesso) return processos;

              const linhas = tabelaProcesso.querySelectorAll('tbody tr');
              let atual = null;  // processo em construção — recebe a próxima linha "Observações:", se houver

              for (let i = 0; i < linhas.length; i++) {
                const linha = linhas[i];
                const celulas = linha.querySelectorAll('td');

                // Linha de DADOS (3+ colunas, sem "Observações") — INICIA um novo processo
                if (celulas.length >= 3 && !linha.textContent.includes('Observações:')) {
                  const spanProc = celulas[0]?.querySelector('span[style*="font-weight"]');
                  const numero = spanProc?.textContent.trim() || '';
                  if (!numero) { atual = null; continue; }  // linha sem número — não é um processo válido

                  const spanJuizo = celulas[1]?.querySelector('span[id*="juizo"]');
                  const spanData = celulas[2]?.querySelector('span');
                  atual = {
                    processo_judicial: numero,
                    juizo: spanJuizo?.textContent.trim() || null,
                    data_inclusao: spanData?.textContent.trim() || null,
                    observacoes_processo: null,
                    validacao: { sucesso: false, motivo: '' },
                  };
                  processos.push(atual);
                }

                // Linha de OBSERVAÇÕES — pertence ao processo da linha de dados ANTERIOR
                if (linha.textContent.includes('Observações:') && atual) {
                  const celulasObs = linha.querySelectorAll('td');
                  if (celulasObs.length >= 2) {
                    atual.observacoes_processo = celulasObs[1]?.textContent.trim() || null;
                  }
                }
              }

              return processos;
            };

            // Audiências cadastradas no PAJ — tabela dedicada (mesmas classes visuais
            // da tabela "Processo Judicial"), identificada pelo container fixo
            // `#detalhamentoForm:listaAudiencias`. Estrutura observada: 1ª linha =
            // título "Audiências" + link "Inserir Nova"; 2ª linha = cabeçalho de
            // colunas "Data / Hora | Local | Observação"; da 3ª linha em diante, uma
            // linha por audiência (última célula é o ícone de excluir — não é dado).
            // PAJ sem NENHUMA audiência cadastrada não tem nem a linha de cabeçalho
            // de colunas — só a linha de título (por isso o corte em `linhas.length < 3`).
            const extrairAudiencias = () => {
              const audiencias = [];
              const container = document.querySelector('#detalhamentoForm\\\\:listaAudiencias');
              if (!container) return audiencias;
              const tabela = container.querySelector('table');
              if (!tabela) return audiencias;
              const linhas = tabela.querySelectorAll('tbody tr');
              if (linhas.length < 3) return audiencias;

              // Local/Observação vêm como texto livre (às vezes com o link da sala
              // virtual colado como texto puro, não como <a href>) — normaliza a
              // indentação/whitespace da formatação do SISDPU, preservando as
              // quebras de linha reais (relevante pra distinguir sala/descrição/link).
              const limpa = (txt) => (txt || '').split('\\n').map((l) => l.trim()).filter(Boolean).join('\\n');

              for (let i = 2; i < linhas.length; i++) {
                const celulas = linhas[i].querySelectorAll('td');
                if (celulas.length < 3) continue;
                const dataHora = (celulas[0]?.textContent || '').trim();
                if (!dataHora) continue;
                audiencias.push({
                  data_hora: dataHora,
                  local: limpa(celulas[1]?.textContent),
                  observacao: limpa(celulas[2]?.textContent),
                });
              }
              return audiencias;
            };

            // Tabela de movimentacoes do SISDPU (EXTRAIR ANTES da validação cruzada)
            // ESTRUTURA DA TABELA (indices das células <td>):
            // 0: Seq. (numero sequencial ex: "1", "2", "3")
            // 1: Anexo (icone de clipe se houver arquivo)
            // 2: Data/Hora (formato "DD/MM/YYYY HH:MM:SS")
            // 3: Movimentação (tipo de movimentacao ex: "MOVIMENTAÇÃO MANUAL")
            // 4: Fases (descricao da fase ex: "° Decurso de prazo;")
            // 5: Descrição (texto completo da movimentacao)
            // 6: (vazio/checkbox)
            // 7: Usuário (nome do usuario que fez a movimentacao)
            // 8+: (colunas adicionais)
            // CRÍTICO: NÃO INVERTER cells[3] e cells[4] — isso quebra o painel
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 8) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const anexo = (cells[1]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.innerHTML || '';
                    const usuario = (cells[7]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || fases)) {
                        const urls = textoCompleto.match(/https?:\\/\\/[^\\s]+/g) || [];
                        const links_decisao = urls.filter(u =>
                            u.includes('stj.jus.br') ||
                            u.includes('stf.jus.br') ||
                            u.includes('portal.stf') ||
                            u.includes('processo.stj') ||
                            u.includes('eproc') ||
                            u.includes('downloadPeca') ||
                            u.includes('documento') ||
                            u.includes('decisao') ||
                            u.endsWith('.pdf')
                        );

                        // descricao_retorno será extraído em segunda passagem (após cliques)
                        let descricao_retorno = '';

                        movs.push({
                            seq: seq,
                            anexo: anexo,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto,
                            links_decisao: links_decisao,
                            usuario: usuario,
                            descricao_retorno: descricao_retorno
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 500);

            // AGORA fazer a extração de TODOS os Processos Judiciais com validação
            // cruzada individual (após movimentações já estarem carregadas)
            const processosJudiciais = extrairProcessosJudiciais();

            // Validação cruzada contra movimentações — UMA por processo (casa pelo
            // NÚMERO na descrição, não só a primeira "Cadastramento" encontrada —
            // necessário quando há mais de um processo/cadastramento no PAJ).
            //
            // "Cadastramento de processo judicial" e' uma FASE da movimentacao, nao
            // o tipo dela — a movimentacao em si costuma ser "Remessa ao Gabinete"
            // (ou outra). Checar so' `mov.movimentacao` NUNCA batia (bug: 0/17
            // validacoes bem-sucedidas nos PAJs existentes, sempre cai no motivo
            // "Movimentacao de cadastramento nao encontrada", mesmo quando a
            // movimentacao existe). Checa os dois campos por robustez.
            for (const p of processosJudiciais) {
              if (p.processo_judicial && p.data_inclusao) {
                const movCadastro = movs.find(mov =>
                  (mov.fases?.includes('Cadastramento de processo judicial') ||
                   mov.movimentacao?.includes('Cadastramento de processo judicial')) &&
                  (mov.descricao || '').includes(p.processo_judicial)
                );

                if (movCadastro) {
                  const descricao = movCadastro.descricao || '';
                  // `descricao` e' innerHTML cru (ex.: "...: 50687...(fecha tag)") —
                  // [\\S]+ pegava a tag de fechamento colada no numero (sem espaco
                  // entre eles) e nunca batia com `p.processo_judicial` (que e' so'
                  // digitos). \\d+ isola so' o numero.
                  const numeroMatch = descricao.match(/Número do Processo Judicial[:\\s]+(\\d+)/i);
                  const numeroNaMov = numeroMatch ? numeroMatch[1] : '';
                  const dataMov = movCadastro.data.substring(0, 10); // DD/MM/YYYY

                  if (numeroNaMov === p.processo_judicial && dataMov === p.data_inclusao) {
                    p.validacao.sucesso = true;
                    p.validacao.motivo = 'Validação cruzada bem-sucedida';
                  } else {
                    p.validacao.motivo = 'Dados não coincidem com movimentação';
                  }
                } else {
                  p.validacao.motivo = 'Movimentação de cadastramento não encontrada';
                }
              } else {
                p.validacao.motivo = 'Dados incompletos (sem número ou data)';
              }
            }

            // Armazenar dados extraídos — campos SINGULARES preservados (compat com
            // consumidores existentes: ÚLTIMO processo da tabela, mesmo comportamento
            // de antes) + a LISTA COMPLETA e fiel de todos os processos vinculados.
            const ultimoProcesso = processosJudiciais.length
                ? processosJudiciais[processosJudiciais.length - 1] : null;
            r.processo_judicial = ultimoProcesso ? ultimoProcesso.processo_judicial : null;
            r.juizo = ultimoProcesso ? ultimoProcesso.juizo : null;
            r.data_inclusao = ultimoProcesso ? ultimoProcesso.data_inclusao : null;
            r.observacoes_processo = ultimoProcesso ? ultimoProcesso.observacoes_processo : null;
            r.validacao_processo = ultimoProcesso ? ultimoProcesso.validacao : { sucesso: false, motivo: '' };
            r.processos_judiciais = processosJudiciais;
            r.audiencias = extrairAudiencias();

            // Extração da Narrativa
            const extrairNarrativa = () => {
              let narrativa = null;

              // Buscar tabelas na página
              const tabelas = document.querySelectorAll('table.epaj_sistema_formulario_tabela');

              for (const tabela of tabelas) {
                // Buscar header com "Narrativa"
                const headers = tabela.querySelectorAll('th.epaj_sistema_formulario_tabela_th_titulo');
                let temNarrativa = false;

                for (const th of headers) {
                  if (th.textContent.includes('Narrativa')) {
                    temNarrativa = true;
                    break;
                  }
                }

                if (temNarrativa) {
                  // Encontrou a tabela de Narrativa
                  // Procurar o conteúdo após "Editar Narrativa"
                  const linhas = tabela.querySelectorAll('tbody tr');

                  for (let i = 0; i < linhas.length; i++) {
                    const linha = linhas[i];
                    const textoLinha = linha.textContent;

                    // Procurar por "Editar Narrativa"
                    if (textoLinha.includes('Editar Narrativa')) {
                      // A narrativa está na próxima linha ou na célula seguinte
                      const proxLinha = linhas[i + 1];
                      if (proxLinha) {
                        // Extrai HTML para preservar formatação (negrito, itálicos, parágrafos, etc.)
                        narrativa = proxLinha.innerHTML.trim();
                        break;
                      }
                    } else if (i > 0 && textoLinha && !textoLinha.includes('Editar')) {
                      // Se encontrou texto após "Editar Narrativa", é a narrativa com formatação HTML
                      narrativa = linha.innerHTML.trim();
                      break;
                    }
                  }

                  if (narrativa) break;
                }
              }

              return narrativa;
            };

            const narrativa = extrairNarrativa();
            if (narrativa) r.narrativa = narrativa;

            return r;
        }
        """
    )

    if not dados:
        dados = {}
    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    todas_urls: list[str] = []
    for m in dados.get("movimentacoes", []) or []:
        for u in m.get("links_decisao", []) or []:
            if u not in todas_urls:
                todas_urls.append(u)
    if todas_urls:
        dados["urls_decisoes"] = todas_urls

    # ===== SEGUNDA PASSAGEM: Extrair descricao_retorno para "Atendimento de retorno" =====
    movimentacoes = dados.get("movimentacoes", []) or []

    # 1. Encontrar e clicar em TODOS os toggles de "Atendimento de retorno"
    await page.evaluate("""
        () => {
            const allTrs = document.querySelectorAll('table tbody tr');
            for (const tr of allTrs) {
                const cells = tr.querySelectorAll('td');
                const fases = (cells[4]?.textContent || '').trim();

                if (fases.includes('Atendimento de retorno')) {
                    const toggler = tr.querySelector('.ui-row-toggler');
                    if (toggler && toggler.getAttribute('aria-expanded') !== 'true') {
                        toggler.click();
                    }
                }
            }
        }
    """)

    # 2. Aguardar que as requisições AJAX completem
    await _wait_pf_ajax(page, timeout=5000)

    # 3. Extrair seq -> conteúdo direto em JavaScript
    mapa_conteudos = await page.evaluate("""
        () => {
            const resultado = {};
            const allTrs = document.querySelectorAll('table tbody tr');

            for (let i = 0; i < allTrs.length; i++) {
                const tr = allTrs[i];
                const cells = tr.querySelectorAll('td');
                const seq = (cells[0]?.textContent || '').trim();
                const fases = (cells[4]?.textContent || '').trim();

                // Se esta TR tem "Atendimento de retorno" E de fato tem o icone de
                // expansao PROPRIO dela (nem toda "Atendimento de retorno" tem — há
                // variante sem campo expansivel). Sem essa checagem, o loop abaixo
                // "vazava" pra frente e pegava o conteudo expandido de uma OUTRA
                // TR de retorno mais adiante (bug real: seq sem expansao propria
                // ficava com o descricao_retorno de um seq vizinho, sobrescrevendo
                // por engano a descricao correta na exibicao — ver
                // movimentacoes_tabela.html, que prioriza descricao_retorno sobre
                // descricao pra "Atendimento de retorno").
                const temToggler = !!tr.querySelector('.ui-row-toggler');
                if (fases.includes('Atendimento de retorno') && temToggler) {
                    // Procurar pela próxima TR (pode haver várias)
                    for (let j = i + 1; j < Math.min(i + 5, allTrs.length); j++) {
                        const proximaTr = allTrs[j];

                        // Se encontrou uma TR expandida
                        if (proximaTr.classList.contains('ui-expanded-row-content')) {
                            const td = proximaTr.querySelector('td');
                            if (td) {
                                resultado[seq] = td.textContent.trim();
                            }
                            break;
                        }
                    }
                }
            }

            return resultado;
        }
    """)

    # 4. Aplicar os conteúdos extraídos às movimentações
    for mov in movimentacoes:
        if "Atendimento de retorno" in mov.get("fases", ""):
            seq = str(mov.get("seq", "")).strip()

            # Debug: testar diferentes formatos de chave
            encontrou = False
            for chave_mapa in mapa_conteudos.keys():
                if str(chave_mapa).strip() == seq:
                    mov["descricao_retorno"] = mapa_conteudos[chave_mapa]
                    encontrou = True
                    break

            # Fallback: tentar direto
            if not encontrou and seq in mapa_conteudos:
                mov["descricao_retorno"] = mapa_conteudos[seq]

    # Contatos (telefone/email/endereco) — ultimo passo: exige navegar pro
    # Cadastro de Pessoa Fisica de cada assistido (pagina separada) e volta
    # pro detalhamento em seguida (anexos/documentos, chamados logo apos esta
    # funcao retornar, dependem da page estar de volta la). Pulado no caminho
    # rapido da aba Retornos (esperas_curtas) — so' le pra exibicao, nao grava
    # metadata.json, e o custo extra de navegacao nao compensa ali.
    if not esperas_curtas:
        with contextlib.suppress(Exception):
            await _extrair_contatos_e_voltar(page, dados)

    return dados


# JS que dispara a busca rapida do SISDPU. Descobertas chave (vistas no
# page.html do debug):
#  - O botao tem onmouseup="buscarProcesso(event)" (funcao JS customizada).
#  - O resultado abre em POPUP via redirecionar(url, idProcesso) ->
#    window.open(...). Por isso a URL da janela atual nunca muda.
#  - O ano/unidade da busca vem dos hidden inputs anoAtualParaPesquisaRapida
#    e codigoUnidadeParaPesquisaRapida (defaults para a unidade do usuario).
#    Para buscar PAJ de outro ano/unidade temos que alterar esses hidden.
_JS_PESQUISAR_PAJ = """
(args) => {
    const { termo, ano, unidade } = args;
    const debug = {};

    // ----- 1) Hidden inputs ano/unidade — sobrescreve para o PAJ desejado -----
    const anoHidden = document.getElementById('anoAtualParaPesquisaRapida');
    const unidHidden = document.getElementById('codigoUnidadeParaPesquisaRapida');
    if (anoHidden) {
        debug.anoAntes = anoHidden.value;
        anoHidden.value = ano;
        debug.anoDepois = anoHidden.value;
    }
    if (unidHidden) {
        debug.unidAntes = unidHidden.value;
        unidHidden.value = unidade;
        debug.unidDepois = unidHidden.value;
    }

    // ----- 2) Input do numero -----
    const input = document.getElementById('processoPesquisaRapidaNumeroProcesso');
    if (!input) return { ok: false, erro: 'input processoPesquisaRapidaNumeroProcesso nao encontrado', debug };
    debug.inputId = input.id;

    const btn = document.getElementById('botaoPesquisarPesquisaRapida');
    if (!btn) return { ok: false, erro: 'botao botaoPesquisarPesquisaRapida nao encontrado', debug };
    debug.btnId = btn.id;

    input.focus();
    input.value = termo;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));

    // ----- 3) Aciona buscarProcesso(event) — o handler do onmouseup -----
    try {
        if (typeof buscarProcesso === 'function') {
            const ev = new MouseEvent('mouseup', { bubbles: true, cancelable: true });
            Object.defineProperty(ev, 'target', { value: btn, writable: false });
            buscarProcesso(ev);
            debug.metodo = 'buscarProcesso(event)';
            return { ok: true, debug };
        }
        debug.buscarProcesso_disponivel = false;
    } catch (e) {
        debug.busca_erro = e.message;
    }

    // Fallback: dispara mouseup real no botao
    try {
        btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
        debug.metodo = (debug.metodo || '') + '+mouseup';
    } catch (e) {
        debug.mouseup_erro = e.message;
    }

    return { ok: true, debug };
}
"""


async def _dump_debug_paj(
    page: Page,
    paj_texto: str,
    motivo: str,
    extras: dict | None = None,
    extra_pages: dict[str, Page] | None = None,
) -> Path:
    """Salva screenshot + HTML + body.innerText + info da pagina atual em
    logs/sisdpu_debug/ para diagnostico. Usado tanto em falhas quanto em
    sucessos durante a fase de afinacao dos seletores. Devolve a pasta criada.

    `extra_pages={"popup": page2, ...}` dumpa tambem essas paginas extras com
    sufixo no nome do arquivo (popup_screenshot.png, popup_body.txt, etc.) —
    util pra capturar popups que foram abertos mas nao bateram a verificacao.
    """
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = paj_texto.replace("/", "-").replace(" ", "_")
    pasta = _DEBUG_DIR / f"{ts}_{slug}"
    pasta.mkdir(parents=True, exist_ok=True)

    # Purga subpastas antigas pra evitar acumulo (cada dump pesa ~1MB).
    _purgar_debug_dir_antigos()

    with contextlib.suppress(Exception):
        await page.screenshot(path=str(pasta / "screenshot.png"), full_page=True)
    with contextlib.suppress(Exception):
        html = await page.content()
        (pasta / "page.html").write_text(html, encoding="utf-8")
    with contextlib.suppress(Exception):
        texto = await page.inner_text("body")
        (pasta / "body.txt").write_text(texto, encoding="utf-8")

    info = [
        f"timestamp: {ts}",
        f"paj_texto: {paj_texto}",
        f"motivo: {motivo}",
        f"url: {page.url}",
    ]
    if extras:
        for k, v in extras.items():
            info.append(f"{k}: {v}")

    if extra_pages:
        for nome, pg in extra_pages.items():
            with contextlib.suppress(Exception):
                await pg.screenshot(path=str(pasta / f"{nome}_screenshot.png"), full_page=True)
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_page.html").write_text(await pg.content(), encoding="utf-8")
            with contextlib.suppress(Exception):
                (pasta / f"{nome}_body.txt").write_text(
                    await pg.inner_text("body"), encoding="utf-8"
                )
            with contextlib.suppress(Exception):
                info.append(f"{nome}_url: {pg.url}")

    (pasta / "info.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
    return pasta


async def buscar_paj_global(
    numero: str,
    ano: str,
    unidade: str = "44",
    forcar_sessao: bool = True,
    esperas_curtas: bool = False,
    somente_url: bool = False,
) -> dict:
    """Busca um PAJ via campo de pesquisa global do SISDPU (header da caixa).

    Diferente de `movimentacoes_paj`, esta funcao NAO depende do PAJ estar na
    caixa de entrada do defensor — usa o campo "Pesquisar" que aceita qualquer
    PAJ da unidade. Extrai os mesmos dados de cabecalho e movimentacoes.

    `forcar_sessao` (padrao True): fecha o navegador e refaz o login do zero
    (estado 100% limpo, porem LENTO). Passe False para REAPROVEITAR a sessao ja
    aberta — `_ensure_logged_in` verifica a autenticacao e so refaz o login se a
    sessao tiver expirado (muito mais rapido; usado pela inclusao manual de PAJ).

    Retorna `{"erro": "..."}` se a busca falhar. Em caso de falha, salva
    screenshot + HTML + info em `logs/sisdpu_debug/` para diagnostico.
    """
    paj_texto = f"{ano}/{unidade.zfill(3)}-{numero}"
    _log.info("INICIO termo='%s' forcar_sessao=%s", paj_texto, forcar_sessao)

    # Autenticacao: com forcar_sessao=True, fecha e reloga do zero (estado limpo,
    # lento). Com False, apenas garante login (reaproveita a sessao viva; so reloga
    # se expirou) — checagem de auth + login automatico, sem o custo de reabrir tudo.
    if forcar_sessao:
        _log.info("forcando nova sessao (fechar+login)...")
        with contextlib.suppress(Exception):
            await fechar()
    await _ensure_logged_in()
    page = await _get_page()
    _log.info("logado, url pos-login: %s", page.url)

    await page.goto(
        f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(1500)
    await _wait_pf_ajax(page, timeout=10000)
    _log.info("caixa carregada url=%s", page.url)

    # ---------- Verificacoes de sessao viva ----------
    # 1) URL nao caiu pra tela de login (redirect silencioso por sessao expirada).
    if "login" in page.url.lower():
        msg = "apos login, ainda na tela de login — sessao nao autenticada"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(page, paj_texto, msg)
            _log.warning("FIM(login-nao-efetivo) debug salvo em: %s", pasta)
        return {"erro": msg}

    # 2) Estamos de fato na area logada: precisa ter o botao "Pesquisar" do
    #    header (visivel em todas as telas autenticadas) E o titulo da unidade
    #    ("Unidade DPU - ..."). Se nao aparecerem, a sessao esta "morta"
    #    (logado mas sem direitos / popup bloqueando / pagina parcial).
    sessao_viva = await page.evaluate(
        """() => {
            const corpo = (document.body.innerText || '').toLowerCase();
            const temUnidade = corpo.includes('unidade dpu');
            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            let temBtnPesquisar = false;
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim().toLowerCase();
                if (txt === 'pesquisar' || txt === 'buscar' || txt.includes('pesquis')) {
                    temBtnPesquisar = true; break;
                }
            }
            return {
                ok: temUnidade && temBtnPesquisar,
                temUnidade, temBtnPesquisar,
                trecho_inicio_body: corpo.substring(0, 200)
            };
        }"""
    )
    _log.info("sessao_viva=%s", sessao_viva)
    if not sessao_viva.get("ok"):
        msg = (
            f"sessao SIS-DPU nao confirmada como ativa — "
            f"temUnidade={sessao_viva.get('temUnidade')} "
            f"temBtnPesquisar={sessao_viva.get('temBtnPesquisar')}"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"sessao": sessao_viva},
            )
            _log.warning("FIM(sessao-morta) debug salvo em: %s", pasta)
        return {"erro": msg}

    # Diagnostico inicial — lista os inputs de texto e botoes com texto
    # "Pesquisar"/"Buscar" presentes na pagina. Util pra entender se a
    # heuristica esta achando o elemento errado ou nada.
    inventario = await page.evaluate(
        """() => {
            const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                id: el.id || '',
                type: el.type || '',
                name: el.name || '',
                placeholder: el.placeholder || '',
                ariaLabel: el.getAttribute('aria-label') || ''
            })).filter(i => i.type === 'text' || i.type === '' || i.type === 'search');

            const cands = document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], input[type="image"], a, span[role="button"]'
            );
            const botoes = [];
            for (const el of cands) {
                const txt = ((el.textContent || el.value || el.title || el.getAttribute('aria-label') || '') + '').trim();
                if (txt.toLowerCase().includes('pesquis') || txt.toLowerCase().includes('buscar')) {
                    botoes.push({ tag: el.tagName, id: el.id || '', texto: txt.substring(0, 40) });
                }
            }
            return {
                n_inputs_texto: inputs.length,
                inputs_amostra: inputs.slice(0, 12),
                botoes_pesquisar: botoes.slice(0, 8),
                titulo: document.title || ''
            };
        }"""
    )
    _log.info("inventario da pagina: %s", inventario)

    # Regra confirmada pelo usuario: o campo de pesquisa SEMPRE exige o numero
    # completo do PAJ (12 digitos no total: 4 ano + 3 unidade + 5 numero).
    # Caracteres especiais (`/` e `-`) sao opcionais. Pesquisar so pelo numero
    # final (ex: '00090' ou '90') NAO funciona — o SIS-DPU exige ano+unidade
    # tambem. Por isso so' tentamos as 2 formas validas:
    #   1) canonico com separadores  — `2024/003-00512`
    #   2) 12 digitos puros          — `202400300512`
    numero_z = numero.zfill(5)
    unidade_z = unidade.zfill(3)
    tentativas = [
        f"{ano}/{unidade_z}-{numero_z}",  # 2024/003-00512 (formato PAJ canonico)
        f"{ano}{unidade_z}{numero_z}",  # 202400300512 (12 digitos puros)
    ]
    vistos: set[str] = set()
    tentativas = [t for t in tentativas if not (t in vistos or vistos.add(t))]

    # Page do detalhamento — pode ser a propria janela atual (se buscarProcesso
    # navegar) ou um POPUP novo (descobrimos no markup que redirecionar() faz
    # window.open). Comeca como None — a primeira tentativa que abrir popup
    # OU navegar a janela atual sera capturada.
    page_detalhe: Page | None = None
    sucesso = False
    erro_ultimo = ""
    debug_acumulado: list[dict] = []
    # Regex que confirma "estou na tela de detalhamento do PAJ". O SIS-DPU
    # tem mais de uma variante visual de cabecalho — aceitamos qualquer pista:
    #  - "AAAA/UUU-NNNNN - PAJ STATUS" (formato compacto, presente em algumas paginas)
    #  - "Numero do PAJ\tAAAA/UUU-NNNNN" (formato tabular, mais comum na tela atual)
    #  - "Detalhamento do Processo" (titulo do breadcrumb — pista robusta)
    re_pag_detalhe = re.compile(
        r"\d{4}/\d{3}-\d+\s*-\s*PAJ\s"
        r"|N[úu]mero\s+do\s+PAJ\s+\d{4}/\d{3}-\d+"
        r"|Detalhamento\s+do\s+Processo"
    )
    contexto = page.context

    for termo in tentativas:
        _log.info("tentando termo='%s'", termo)

        # Escuta um novo "page" (popup) sendo aberto no contexto. Se a
        # buscarProcesso navegar na janela atual, esse expect_page nao captura
        # nada e cai no fallback abaixo.
        popup_capturado: Page | None = None
        try:
            async with contexto.expect_page(timeout=12000) as popup_info:
                res = await page.evaluate(
                    _JS_PESQUISAR_PAJ,
                    {"termo": termo, "ano": ano, "unidade": unidade_z},
                )
                if not isinstance(res, dict) or not res.get("ok"):
                    erro_ultimo = (res or {}).get("erro", "falha ao localizar campo de pesquisa")
                    debug_acumulado.append({"termo": termo, "res": res})
                    _log.warning("  FALHA: %s debug=%s", erro_ultimo, res)
                    if "campo" in erro_ultimo or "botao" in erro_ultimo:
                        # nao adianta tentar outros termos
                        raise StopIteration
            popup_capturado = await popup_info.value
            _log.info("  popup capturado url=%s", popup_capturado.url)
        except StopIteration:
            break
        except PWTimeoutError:
            _log.info("  nenhum popup aberto neste termo (timeout do expect_page)")
        except Exception as e:
            _log.warning("  erro ao esperar popup: %s: %s", type(e).__name__, e)

        # Se capturou popup, estrategia em 2 etapas:
        #
        # ETAPA 1 — tentar usar o popup direto. Espera load + polling do regex.
        # ETAPA 2 (fallback) — pega URL do popup e NAVEGA a janela principal
        # pra ela. Isso funciona porque `redirecionar(url, idProcesso)` no
        # SIS-DPU e' so' `window.open(url + idProcesso)` — sem state JSF, e a
        # URL e' totalmente stateless (so' precisa de cookie de sessao).
        # Como a janela principal ja tem PrimeFaces carregado da caixa, o load
        # completa de forma previsivel.
        # JS que detecta "estou na tela de detalhamento do PAJ" — aceita 3
        # padroes de cabecalho que o SIS-DPU usa em telas diferentes (mesmos
        # padroes do regex Python `re_pag_detalhe` acima). Compartilhado entre
        # ETAPA 1 (popup) e ETAPA 2 (janela principal pos-goto).
        js_detect_header = r"""() => {
            const txt = (document.body && document.body.innerText) || '';
            return /\d{4}\/\d{3}-\d+\s*-\s*PAJ\s/.test(txt)
                || /N[úu]mero\s+do\s+PAJ\s+\d{4}\/\d{3}-\d+/.test(txt)
                || /Detalhamento\s+do\s+Processo/.test(txt);
        }"""

        if popup_capturado is not None:
            popup_url_inicial = popup_capturado.url
            with contextlib.suppress(Exception):
                await popup_capturado.wait_for_load_state(
                    "domcontentloaded",
                    timeout=15000,
                )

            tem_header = False
            # ETAPA 1: tentar ler do popup (10s)
            with contextlib.suppress(PWTimeoutError, Exception):
                await popup_capturado.wait_for_function(
                    js_detect_header,
                    timeout=10000,
                    polling=250,
                )
                tem_header = True

            if tem_header:
                # Popup funcionou — usa ele direto
                with contextlib.suppress(Exception):
                    await _wait_pf_ajax(popup_capturado, timeout=10000)
                await popup_capturado.wait_for_timeout(500)
                _log.info(
                    "  popup OK url=%s",
                    popup_capturado.url,
                )
                page_detalhe = popup_capturado
                sucesso = True
                break

            # ETAPA 2: fallback — navega janela principal pra popup_url
            popup_url_final = popup_capturado.url
            url_alvo = (
                popup_url_final if "detalhamentoProcesso" in popup_url_final else popup_url_inicial
            )
            _log.info(
                "  popup nao renderizou em 10s — fallback: navegar janela principal pra %s",
                url_alvo,
            )

            # Fecha o popup antes de mexer na janela principal
            with contextlib.suppress(Exception):
                await popup_capturado.close()

            if not url_alvo or "detalhamentoProcesso" not in url_alvo:
                erro_ultimo = f"popup '{termo}' nao renderizou e URL inesperada: {url_alvo!r}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            try:
                await page.goto(url_alvo, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                _log.warning("  fallback goto falhou: %s: %s", type(e).__name__, e)
                erro_ultimo = f"popup '{termo}' + fallback navegacao falhou: {e}"
                debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
                continue

            with contextlib.suppress(Exception):
                await _wait_pf_ajax(page, timeout=15000)
            # Polling do cabecalho na janela principal — 15s
            # Usa o mesmo `js_detect_header` (3 padroes aceitos)
            with contextlib.suppress(PWTimeoutError, Exception):
                await page.wait_for_function(
                    js_detect_header,
                    timeout=15000,
                    polling=250,
                )
                tem_header = True
            await page.wait_for_timeout(500)

            if tem_header:
                _log.info(
                    "  fallback OK — janela principal carregou detalhamento de %s",
                    page.url,
                )
                page_detalhe = page
                sucesso = True
                break

            # Mesmo o fallback falhou — dumpa tudo pra diagnostico
            with contextlib.suppress(Exception):
                pasta_dbg = await _dump_debug_paj(
                    page,
                    paj_texto,
                    f"popup '{termo}' nao renderizou + fallback nao carregou cabecalho",
                    extras={
                        "termo": termo,
                        "popup_url_inicial": popup_url_inicial,
                        "popup_url_final": popup_url_final,
                        "url_alvo": url_alvo,
                        "page_url_pos_goto": page.url,
                    },
                )
                _log.warning("  dump salvo em %s", pasta_dbg)

            erro_ultimo = (
                f"popup '{termo}' nao renderizou e fallback (goto {url_alvo}) "
                f"tambem nao mostrou cabecalho"
            )
            debug_acumulado.append({"termo": termo, "popup_url": url_alvo})
            # Volta pra caixa antes de proxima tentativa
            with contextlib.suppress(Exception):
                await page.goto(
                    f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                    wait_until="domcontentloaded",
                )
                await _wait_pf_ajax(page, timeout=10000)
            continue

        # Fallback: a janela atual pode ter navegado para o detalhamento
        # (caso raro, mas defensivo). Da uns segundos pro PrimeFaces AJAX.
        with contextlib.suppress(Exception):
            await _wait_pf_ajax(page, timeout=15000)
        await page.wait_for_timeout(2000)
        body = ""
        with contextlib.suppress(Exception):
            body = await page.inner_text("body")
        tem_header_detalhe = bool(re_pag_detalhe.search(body))
        _log.info(
            "  pos-pesquisa (janela atual) url=%s tem_header_detalhe=%s",
            page.url,
            tem_header_detalhe,
        )
        if tem_header_detalhe:
            page_detalhe = page
            sucesso = True
            break
        erro_ultimo = (
            f"pesquisa por '{termo}' nao abriu popup nem navegou — "
            f"PAJ pode nao existir nessa unidade/ano"
        )
        debug_acumulado.append({"termo": termo, "url": page.url})

        # Volta pra caixa antes da proxima tentativa
        with contextlib.suppress(Exception):
            await page.goto(
                f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                wait_until="domcontentloaded",
            )
            await _wait_pf_ajax(page, timeout=10000)

    if not sucesso or page_detalhe is None:
        msg = erro_ultimo or "PAJ nao encontrado via pesquisa global"
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"tentativas": debug_acumulado},
            )
            _log.warning("FIM(falha) debug salvo em: %s", pasta)
        return {"erro": msg}

    # A partir daqui a extracao roda em `page_detalhe`, que pode ser a janela
    # atual OU um popup aberto por window.open. Substituimos a referencia
    # `page` localmente para que o resto do codigo continue limpo.
    page = page_detalhe

    # Caso o popup tenha sido capturado, promove ele a "page global" — assim
    # operacoes posteriores que dependem de `_get_page()` (ex: listar_arquivos_paj
    # → baixar anexos) atuam no detalhamento do PAJ, e nao na caixa de entrada
    # antiga. Sem isso, sincronizar um PAJ da watchlist via busca global nao
    # conseguiria baixar anexos. Quando `page_detalhe` ja e' a propria `_page`
    # (busca navegou a janela atual em vez de abrir popup), a atribuicao e' no-op.
    global _page
    _page = page_detalhe

    # Atalho: quem só quer a URL de detalhe (ex.: montar link para abrir o PAJ no
    # navegador do usuário) retorna aqui, ANTES da extração pesada de cabeçalho e
    # movimentações. A URL do `detalhamentoProcesso` é stateless (só precisa do
    # cookie de sessão), então funciona em qualquer navegador logado no SISDPU.
    if somente_url:
        _log.info("FIM(somente_url) url=%s", page_detalhe.url)
        return {"url": page_detalhe.url, "paj": paj_texto}

    # Extrai os campos do cabecalho do detalhamento. Estrategia hibrida:
    # (1) DOM-based para label/valor (mais robusto que regex no innerText)
    # (2) regex residual para padroes posicionais (numero CNJ, linha PAJ).
    # (3) filtros para descartar valores que sao claramente cabecalhos de tabela.
    dados = await page.evaluate(
        """
        () => {
            const r = {};
            const body = document.body.innerText;

            // ---------- Helper: encontra valor adjacente a um label ----------
            // O regex inclui o ':' ASCII E o U+FF1A (fullwidth colon) porque labels
            // do SISDPU aparecem com qualquer um dos dois, dependendo da fonte.
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim()
                                          .toLowerCase().replace(/[:：]$/, '');

            // Lixo conhecido — cabecalhos de tabelas do SISDPU que aparecem
            // proximos a labels e foram capturados por engano antes.
            const LIXO_TABELA = /^(data de envio|remetente|prazo|descri[çc][ãa]o|tipo|status|data|seq|usu[áa]rio|fases|movimenta[çc][ãa]o|seq\\.?)\\b/i;

            function valorParaLabel(rotulos) {
                const alvos = rotulos.map(norm);
                // Exclui o MENU DE NAVEGAÇÃO do topo (ul.epaj_info_menu_lista, presente
                // em toda pagina do SISDPU) — ele tem itens de configuracao com o MESMO
                // texto de varios labels que procuramos aqui (ex: um item de menu chamado
                // literalmente "Ofício", que leva a uma tela administrativa de cadastro de
                // oficios, nada a ver com o Oficio Responsavel DESTE PAJ). Sem essa
                // exclusao, o proximo <li> do menu (ex: "Documento/Pretensão", outro item
                // do MESMO menu) era capturado como se fosse o valor do campo — bug real,
                // gravava "Documento/Pretensão" como oficio_caixa em ~38 PAJs sincronizados
                // via busca global (rodar_paj_via_busca_global), relatado em 2026-08-19.
                const candidatos = Array.from(document.querySelectorAll(
                    'td, th, label, span, dt, dd, li, b, strong, div, p'
                )).filter((el) => !el.closest('.epaj_info_menu_lista'));
                for (const el of candidatos) {
                    const txt = norm(el.textContent || '');
                    if (!alvos.includes(txt)) continue;

                    // 1) Proximo sibling com texto util.
                    let next = el.nextElementSibling;
                    while (next) {
                        const v = (next.textContent || '').trim();
                        if (v) {
                            const linha = v.split('\\n')[0].trim();
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                        next = next.nextElementSibling;
                    }

                    // 2) Pai contendo label e valor juntos no innerText.
                    const pai = el.parentElement;
                    if (pai) {
                        const fullPai = (pai.textContent || '').trim();
                        const idx = fullPai.toLowerCase().indexOf(txt);
                        if (idx >= 0) {
                            const depois = fullPai.substring(idx + (el.textContent || '').length).trim();
                            const linha = depois.split('\\n')[0].trim().replace(/^[:\\-\\s]+/, '');
                            if (linha && !LIXO_TABELA.test(linha) && linha.length < 250) {
                                return linha;
                            }
                        }
                    }
                }
                return '';
            }

            // ---------- Campos do cabecalho (todos os que aparecem na aba Resumo) ----------
            r.assistido = valorParaLabel(['Assistido(s)', 'Assistido', 'Assistidos']);

            // Extrai CPF da tabela "Assistido(s) Pessoa(s) Física:"
            // Seletor: primeira linha de dados, terceira célula (coluna CPF)
            const cpfCell = document.querySelector(
                '#detalhamentoForm\\\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)'
            );
            if (cpfCell) {
                const cpfText = cpfCell.textContent.trim().split('\\n')[0];
                if (cpfText && /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfText)) {
                    r.cpf_assistido = cpfText;
                }
            }

            // Foto do assistido — o SISDPU embute a imagem direto como data URI
            // base64 num <img id="detalhamentoForm:fotoAssistido">. Nem todo PAJ
            // tem foto cadastrada (o elemento so existe quando ha uma).
            const fotoImg = document.querySelector('#detalhamentoForm\\\\:fotoAssistido');
            if (fotoImg && fotoImg.src && fotoImg.src.startsWith('data:image')) {
                r.foto_assistido_src = fotoImg.src;
            }

            // App SISDPU / Assistidos PF — mesma logica de movimentacoes_paj (ver lá
            // o comentário completo).
            const spanApp = document.getElementById('detalhamentoForm:lnkNotificarAssistido');
            const temLinkApp = !!(spanApp && spanApp.querySelector('a'));
            const temFraseApp = /Ades[aã]o ao Termo de Responsabilidade do Aplicativo do SISDPU/i.test(body);
            r.contato_app_sisdpu = temLinkApp || temFraseApp;

            // Mesma logica enriquecida de movimentacoes_paj (ver lá o comentário
            // completo) — nome/CPF por linha + deteccao do principal pelo icone
            // de excluir ausente, nao so o idPessoa.
            const tblPF = document.getElementById('detalhamentoForm:dataTableListaAssistidosPF');
            if (tblPF) {
                const assistidosPf = [];
                for (const tr of tblPF.querySelectorAll('tbody tr')) {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 3) continue;
                    const linkPessoa = cells[1]?.querySelector('a[href*="cadastroAssistidoPessoaFisica"]');
                    const href = linkPessoa?.getAttribute('href') || '';
                    const mp = href.match(/idPessoa=(\\d+)/);
                    if (!mp) continue;
                    let nome = (linkPessoa.textContent || '').trim();
                    nome = nome.replace(/\\s*\\(\\d+\\s*PAJ'?S\\)\\s*$/i, '').trim();
                    const cpfTexto = (cells[2]?.textContent || '').trim().split('\\n')[0].trim();
                    const cpf = /\\d{3}\\.\\d{3}\\.\\d{3}-\\d{2}/.test(cpfTexto) ? cpfTexto : '';
                    const ultimaCel = cells[cells.length - 1];
                    const temExcluir = !!(ultimaCel && ultimaCel.querySelector('a[title*="Excluir"], img[alt="Excluir"]'));
                    assistidosPf.push({ idPessoa: mp[1], nome, cpf, principal: !temExcluir });
                }
                if (assistidosPf.length) {
                    r.assistidos_pf = assistidosPf;
                    const principalPf = assistidosPf.find(a => a.principal) || assistidosPf[0];
                    if (principalPf.cpf) r.cpf_assistido = principalPf.cpf;
                }
            }

            r.pretensao = valorParaLabel(['Pretensão', 'Pretensao']);
            r.data_abertura = valorParaLabel([
                'Data de Abertura', 'Dt. Abertura', 'Dt Abertura', 'Dt.Abertura', 'Abertura'
            ]);
            r.oficio = valorParaLabel([
                'Ofício', 'Oficio', 'Ofício Responsável', 'Oficio Responsavel',
                'Ofício responsável', 'Ofício atual'
            ]);
            // `juizo`/`processo_judicial`/`processos_judiciais` NAO usam mais
            // valorParaLabel aqui — essa busca generica por texto de label
            // (usada pros demais campos acima) nao enxerga a tabela real de
            // "Processo Judicial" (colunas, sem label solto "Juízo:" no DOM),
            // entao vinha vazia ou pegava texto de outro rotulo por coincidencia
            // de layout. A extracao por tabela (mesma logica de
            // movimentacoes_paj.extrairProcessosJudiciais) fica mais abaixo,
            // apos `movs` — preenche processo_judicial/juizo/data_inclusao/
            // observacoes_processo (onde mora a "chave" de acesso, ver
            // services.paj_service.buscar_chave_por_processo)/processos_judiciais
            // igual ao caminho da caixa. Sem essa tabela, PAJs sincronizados
            // pela busca global (fora da caixa) ficavam com juizo/chave ausentes
            // ou errados mesmo tendo "Processo Judicial" cadastrado.
            r.foro_detalhado = valorParaLabel([
                'Foro Detalhado', 'Foro detalhado', 'Foro'
            ]);
            r.decurso = valorParaLabel([
                'Decurso', 'Decurso agendado', 'Decurso Agendado'
            ]);

            // ---------- PAJ + status — duas variantes da tela ----------
            //  a) Formato compacto: "AAAA/UUU-NNNNN - PAJ STATUS"
            //  b) Formato tabular:  "Número do PAJ\tAAAA/UUU-NNNNN"  (nesta tela,
            //     o status vem em outra linha, ex: "Urgente" / "Tutela Coletiva".
            //     Deixamos status vazio se nao houver formato compacto.)
            let pajMatch = body.match(/(\\d{4}\\/\\d{3}-\\d+)\\s*-\\s*PAJ\\s+([^\\n]+)/);
            if (pajMatch) {
                r.paj = pajMatch[1];
                r.status_paj = pajMatch[2].trim();
            } else {
                pajMatch = body.match(/N[úu]mero\\s+do\\s+PAJ[\\s\\t]+(\\d{4}\\/\\d{3}-\\d+)/);
                if (pajMatch) {
                    r.paj = pajMatch[1];
                    r.status_paj = '';
                }
            }

            // ---------- Movimentacoes (tabela; mesma logica de antes) ----------
            const movs = [];
            const movRows = document.querySelectorAll('table tbody tr');
            for (const tr of movRows) {
                const cells = tr.querySelectorAll('td');
                if (cells.length >= 8) {
                    const seq = (cells[0]?.textContent || '').trim();
                    const anexo = (cells[1]?.textContent || '').trim();
                    const dataHora = (cells[2]?.textContent || '').trim();
                    const movimentacao = (cells[3]?.textContent || '').trim();
                    const fases = (cells[4]?.textContent || '').trim();
                    const textoCompleto = cells[5]?.innerHTML || '';
                    const usuario = (cells[6]?.textContent || '').trim();

                    if (seq && /^\\d{1,4}$/.test(seq)
                        && (textoCompleto.trim() || fases)) {

                        // descricao_retorno será extraído em segunda passagem (após cliques)
                        let descricao_retorno = '';

                        movs.push({
                            seq: seq,
                            anexo: anexo,
                            data: dataHora,
                            movimentacao: movimentacao,
                            fases: fases,
                            descricao: textoCompleto,
                            links_decisao: [],
                            usuario: usuario,
                            descricao_retorno: descricao_retorno
                        });
                    }
                }
            }
            if (movs.length > 0) r.movimentacoes = movs.slice(0, 500);

            // ---------- Processo(s) Judicial(is) — MESMA logica de
            // movimentacoes_paj.extrairProcessosJudiciais (ver lá o comentário
            // completo). Duplicada aqui (nao compartilhada — cada chamada de
            // page.evaluate() e' uma string isolada) porque a busca global
            // tambem precisa dos dados REAIS da tabela (juizo, observacoes com
            // a chave de acesso), nao so' do numero do processo. ----------
            const extrairProcessosJudiciais = () => {
              const processos = [];
              const tabelas = document.querySelectorAll('table.epaj_sistema_formulario_tabela.epaj_sistema_formulario_tabela_filtro');
              let tabelaProcesso = null;
              for (const tabela of tabelas) {
                const th = tabela.querySelector('th');
                if (th && th.textContent.includes('Processo Judicial')) { tabelaProcesso = tabela; break; }
              }
              if (!tabelaProcesso) return processos;

              const linhas = tabelaProcesso.querySelectorAll('tbody tr');
              let atual = null;
              for (let i = 0; i < linhas.length; i++) {
                const linha = linhas[i];
                const celulas = linha.querySelectorAll('td');
                if (celulas.length >= 3 && !linha.textContent.includes('Observações:')) {
                  const spanProc = celulas[0]?.querySelector('span[style*="font-weight"]');
                  const numero = spanProc?.textContent.trim() || '';
                  if (!numero) { atual = null; continue; }
                  const spanJuizo = celulas[1]?.querySelector('span[id*="juizo"]');
                  const spanData = celulas[2]?.querySelector('span');
                  atual = {
                    processo_judicial: numero,
                    juizo: spanJuizo?.textContent.trim() || null,
                    data_inclusao: spanData?.textContent.trim() || null,
                    observacoes_processo: null,
                    validacao: { sucesso: false, motivo: '' },
                  };
                  processos.push(atual);
                }
                if (linha.textContent.includes('Observações:') && atual) {
                  const celulasObs = linha.querySelectorAll('td');
                  if (celulasObs.length >= 2) {
                    atual.observacoes_processo = celulasObs[1]?.textContent.trim() || null;
                  }
                }
              }
              return processos;
            };

            const processosJudiciais = extrairProcessosJudiciais();
            for (const p of processosJudiciais) {
              if (p.processo_judicial && p.data_inclusao) {
                const movCadastro = movs.find(mov =>
                  (mov.fases?.includes('Cadastramento de processo judicial') ||
                   mov.movimentacao?.includes('Cadastramento de processo judicial')) &&
                  (mov.descricao || '').includes(p.processo_judicial)
                );
                if (movCadastro) {
                  const descricao = movCadastro.descricao || '';
                  const numeroMatch = descricao.match(/Número do Processo Judicial[:\\s]+(\\d+)/i);
                  const numeroNaMov = numeroMatch ? numeroMatch[1] : '';
                  const dataMov = movCadastro.data.substring(0, 10);
                  if (numeroNaMov === p.processo_judicial && dataMov === p.data_inclusao) {
                    p.validacao.sucesso = true;
                    p.validacao.motivo = 'Validação cruzada bem-sucedida';
                  } else {
                    p.validacao.motivo = 'Dados não coincidem com movimentação';
                  }
                } else {
                  p.validacao.motivo = 'Movimentação de cadastramento não encontrada';
                }
              } else {
                p.validacao.motivo = 'Dados incompletos (sem número ou data)';
              }
            }

            const ultimoProcesso = processosJudiciais.length
                ? processosJudiciais[processosJudiciais.length - 1] : null;
            if (ultimoProcesso) {
              r.processo_judicial = ultimoProcesso.processo_judicial;
              r.juizo = ultimoProcesso.juizo;
              r.data_inclusao = ultimoProcesso.data_inclusao;
              r.observacoes_processo = ultimoProcesso.observacoes_processo;
              r.validacao_processo = ultimoProcesso.validacao;
              r.processos_judiciais = processosJudiciais;
            }
            // Sem `else`/fallback por regex aqui de proposito: um regex de
            // padrao CNJ solto sobre `body` (texto de TODA a pagina, inclusive
            // descricoes de movimentacao/despacho) pega qualquer numero de
            // processo MENCIONADO em prosa — comum em despachos que citam
            // "coisa julgada"/"acao anterior"/processo de OUTRO PAJ da mesma
            // pessoa — e nao necessariamente o processo REALMENTE cadastrado
            // neste PAJ. Bug real observado em producao: PAJ sem nenhum
            // processo ajuizado aparecia com o numero de um processo ANTIGO
            // (ja baixado, de outro PAJ) so' porque uma analise previa citava
            // esse numero como "processo em nome da assistida" localizado no
            // e-proc. Preferimos deixar `processo_judicial` vazio (fiel a
            // realidade) a arriscar mostrar um numero ERRADO — mais perigoso
            // que simplesmente nao mostrar nada.

            // ---------- Audiências (mesma logica de movimentacoes_paj — ver lá o
            // comentário completo sobre a estrutura da tabela) ----------
            (() => {
                const container = document.querySelector('#detalhamentoForm\\\\:listaAudiencias');
                const tabela = container ? container.querySelector('table') : null;
                const linhas = tabela ? tabela.querySelectorAll('tbody tr') : [];
                if (linhas.length < 3) return;
                const limpa = (txt) => (txt || '').split('\\n').map((l) => l.trim()).filter(Boolean).join('\\n');
                const audiencias = [];
                for (let i = 2; i < linhas.length; i++) {
                    const celulas = linhas[i].querySelectorAll('td');
                    if (celulas.length < 3) continue;
                    const dataHora = (celulas[0]?.textContent || '').trim();
                    if (!dataHora) continue;
                    audiencias.push({
                        data_hora: dataHora,
                        local: limpa(celulas[1]?.textContent),
                        observacao: limpa(celulas[2]?.textContent),
                    });
                }
                if (audiencias.length > 0) r.audiencias = audiencias;
            })();

            // ---------- Filtro final: descarta qualquer campo que tenha vazado lixo ----------
            for (const k of ['assistido', 'pretensao', 'oficio', 'juizo', 'foro_detalhado', 'decurso', 'data_abertura']) {
                if (r[k] && LIXO_TABELA.test(r[k])) {
                    r[k] = '';
                }
            }

            return r;
        }
        """
    )

    if not dados:
        dados = {}

    n_movs = len(dados.get("movimentacoes", []) or [])
    _log.info(
        "extracao: assistido=%s paj=%s movs=%d",
        bool(dados.get("assistido")),
        dados.get("paj", "?"),
        n_movs,
    )

    # Validacao rigorosa: precisa do `paj` extraido pelo regex
    # "AAAA/UUU-NNNNN - PAJ STATUS", que so' aparece na tela de detalhamento.
    # Sem ele, ainda estamos na caixa ou em pagina inesperada.
    if not dados.get("paj"):
        msg = (
            "extracao falhou — cabecalho do PAJ nao encontrado na pagina "
            "(provavelmente nao estamos na tela de detalhamento)"
        )
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                msg,
                extras={"dados_parciais": {k: v for k, v in dados.items() if k != "movimentacoes"}},
            )
            _log.warning("FIM(sem-paj) debug salvo em: %s", pasta)
        return {"erro": msg}

    dados["paj_numero"] = numero
    dados["paj_ano"] = ano
    dados["paj_unidade"] = unidade
    dados["url"] = page.url

    # ===== SEGUNDA PASSAGEM: Extrair descricao_retorno para "Atendimento de retorno" =====
    movimentacoes = dados.get("movimentacoes", []) or []

    # 1. Encontrar e clicar em TODOS os toggles de "Atendimento de retorno"
    await page.evaluate("""
        () => {
            const allTrs = document.querySelectorAll('table tbody tr');
            for (const tr of allTrs) {
                const cells = tr.querySelectorAll('td');
                const fases = (cells[4]?.textContent || '').trim();

                if (fases.includes('Atendimento de retorno')) {
                    const toggler = tr.querySelector('.ui-row-toggler');
                    if (toggler && toggler.getAttribute('aria-expanded') !== 'true') {
                        toggler.click();
                    }
                }
            }
        }
    """)

    # 2. Aguardar que as requisições AJAX completem
    await _wait_pf_ajax(page, timeout=5000)

    # 3. Extrair seq -> conteúdo direto em JavaScript
    mapa_conteudos = await page.evaluate("""
        () => {
            const resultado = {};
            const allTrs = document.querySelectorAll('table tbody tr');

            for (let i = 0; i < allTrs.length; i++) {
                const tr = allTrs[i];
                const cells = tr.querySelectorAll('td');
                const seq = (cells[0]?.textContent || '').trim();
                const fases = (cells[4]?.textContent || '').trim();

                // Se esta TR tem "Atendimento de retorno" E de fato tem o icone de
                // expansao PROPRIO dela (nem toda "Atendimento de retorno" tem — há
                // variante sem campo expansivel). Sem essa checagem, o loop abaixo
                // "vazava" pra frente e pegava o conteudo expandido de uma OUTRA
                // TR de retorno mais adiante (bug real: seq sem expansao propria
                // ficava com o descricao_retorno de um seq vizinho, sobrescrevendo
                // por engano a descricao correta na exibicao — ver
                // movimentacoes_tabela.html, que prioriza descricao_retorno sobre
                // descricao pra "Atendimento de retorno").
                const temToggler = !!tr.querySelector('.ui-row-toggler');
                if (fases.includes('Atendimento de retorno') && temToggler) {
                    // Procurar pela próxima TR (pode haver várias)
                    for (let j = i + 1; j < Math.min(i + 5, allTrs.length); j++) {
                        const proximaTr = allTrs[j];

                        // Se encontrou uma TR expandida
                        if (proximaTr.classList.contains('ui-expanded-row-content')) {
                            const td = proximaTr.querySelector('td');
                            if (td) {
                                resultado[seq] = td.textContent.trim();
                            }
                            break;
                        }
                    }
                }
            }

            return resultado;
        }
    """)

    # 4. Aplicar os conteúdos extraídos às movimentações
    for mov in movimentacoes:
        if "Atendimento de retorno" in mov.get("fases", ""):
            seq = str(mov.get("seq", "")).strip()

            # Debug: testar diferentes formatos de chave
            encontrou = False
            for chave_mapa in mapa_conteudos.keys():
                if str(chave_mapa).strip() == seq:
                    mov["descricao_retorno"] = mapa_conteudos[chave_mapa]
                    encontrou = True
                    break

            # Fallback: tentar direto
            if not encontrou and seq in mapa_conteudos:
                mov["descricao_retorno"] = mapa_conteudos[seq]

    # Narrativa (HTML do painel) — extraida da MESMA pagina de detalhamento, sem
    # re-navegar. Evita uma 2a navegacao (detalhes_paj) na aba Retornos.
    with contextlib.suppress(Exception):
        narr = await page.evaluate(
            r"""() => {
                const n = document.querySelector('#detalhamentoForm\:narrativaPanel');
                return n ? n.innerHTML : '';
            }"""
        )
        if narr:
            dados["narrativa"] = narr

    # Dump tambem em caso de sucesso durante esta fase de afinacao — ajuda
    # diagnosticar campos vazios sem precisar retentar. Pode ser desligado
    # depois que a extracao estiver estavel. No caminho rapido (Retornos), pulamos
    # este dump de disco por PAJ.
    if not esperas_curtas:
        with contextlib.suppress(Exception):
            pasta = await _dump_debug_paj(
                page,
                paj_texto,
                "sucesso (dump pra afinacao)",
                extras={
                    "campos_extraidos": {
                        k: v for k, v in dados.items() if k not in ("movimentacoes", "sisdpu_raw")
                    },
                },
            )
            _log.info("dump de sucesso salvo em: %s", pasta)

    # Contatos (telefone/email/endereco) — mesma logica de movimentacoes_paj
    # (ver lá o comentário completo); pulado no caminho rapido (esperas_curtas).
    if not esperas_curtas:
        with contextlib.suppress(Exception):
            await _extrair_contatos_e_voltar(page, dados)

    _log.info("FIM(ok) %s", paj_texto)
    return dados


async def baixar_arquivo(url: str, destino) -> bool:
    """Baixa arquivo reaproveitando a sessao autenticada (cookies) do SISDPU.

    Retorna True se o arquivo foi gravado com tamanho > 0.
    """
    try:
        import httpx
    except ImportError:
        return False

    page = await _get_page()
    context = page.context
    cookies = await context.cookies()
    cookie_jar = {c["name"]: c["value"] for c in cookies}

    try:
        async with httpx.AsyncClient(
            cookies=cookie_jar,
            timeout=60,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/127.0 Safari/537.36"
                ),
                "Accept": "application/pdf,*/*;q=0.8",
            },
        ) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            if not resp.content:
                return False
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(resp.content)
            return destino.stat().st_size > 0
    except Exception:
        return False


_EXTRACAO_DOCS_JS_PATH = Path(__file__).resolve().parent / "extrair_documentos_movimentacoes.js"


async def extrair_documentos_por_seq(
    seqs_conhecidos: list[str] | None = None,
    deve_cancelar: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Extrai, por seq de movimentacao, os documentos anexados (via o icone de
    "Anexo" de cada linha) do PAJ atualmente aberto.

    PRE-CONDICAO: a page ja deve estar no detalhamento do PAJ (chamar
    `movimentacoes_paj(...)` antes) — mesma pre-condicao de `listar_arquivos_paj`.

    `seqs_conhecidos`: seqs (str) que ja possuem `documentos` no metadata da
    sincronizacao anterior — sao excluidas da fila, entao so processamos seqs
    novas desde a ultima sync (mantem o custo proporcional ao que mudou).

    Pilota o script `extrair_documentos_movimentacoes.js` (copia adaptada em
    `ingestao/`, NAO o original em C:\\Claude\\Scripts\\SISDPU\\) passo a passo:
    INIT (monta a fila) -> N chamadas de __extracaoProximo() (uma por seq,
    clica/le/fecha o popup) -> __extracaoFinalizar() (consolida o resultado).

    Retorna dict: {paj, assistido, movimentacoes_com_documentos: [{seq,
    dataHora, movimentacao, documentos: [{tipoArquivo, descricao, nomeArquivo,
    urlDownload}]}], total_documentos, seqsPuladas, seqsVerificadosSemDocumento,
    tabela_markdown}. `seqsVerificadosSemDocumento` sao seqs cujo popup abriu e
    fechou com sucesso mas nao tinha nenhum documento — o chamador (sincronizador)
    persiste essa lista em metadata pra nao reabrir esses popups nas proximas
    syncs (sem isso, so' seqs COM documento eram cacheadas como "conhecidas").
    Em caso de erro ou tabela nao encontrada, retorna {"erro": "..."}.
    """

    def _log(msg: str) -> None:
        if log:
            with contextlib.suppress(Exception):
                log(msg)

    page = await _get_page()

    js_source = _EXTRACAO_DOCS_JS_PATH.read_text(encoding="utf-8")
    # Envolve o arquivo inteiro numa arrow function explicita — mesmo padrao
    # usado em todo o resto deste modulo (ex: listar_arquivos_paj). Passar o
    # conteudo bruto (que comeca com um comentario /** */) faz o Playwright
    # nao reconhecer a string como "function-like", o que pode fazer as
    # atribuicoes a `window.*` nao "pegarem" do jeito esperado entre chamadas
    # seguintes de evaluate().
    await page.evaluate("(src) => { (0, eval)(src); }", js_source)

    async def _finalizar_seguro() -> dict:
        tem_estado = await page.evaluate("() => !!window.__extracaoEstado")
        if not tem_estado:
            _log("  [docs] estado da extracao perdido (dialogo/pagina mudou) — resultado descartado")
            return {"movimentacoes_com_documentos": [], "total_documentos": 0}
        return await page.evaluate("window.__extracaoFinalizar()")

    init = await page.evaluate(
        "(seqs) => window.__extracaoInit(seqs)", list(seqs_conhecidos or [])
    )

    if init.get("erro"):
        _log(f"  [docs] {init['erro']}")
        return {"erro": init["erro"], "movimentacoes_com_documentos": [], "total_documentos": 0}

    total_fila = init.get("totalFila", 0)
    puladas = init.get("puladas", 0)
    if puladas:
        _log(f"  [docs] {puladas} seq(s) ja conhecida(s) — pulando")
    if total_fila == 0:
        _log("  [docs] nenhuma seq nova com anexo para extrair")
        return await _finalizar_seguro()

    _log(f"  [docs] extraindo documentos de {total_fila} seq(s) nova(s)...")

    try:
        while True:
            if deve_cancelar is not None and deve_cancelar():
                _log("  [docs] CANCELADO pelo usuario")
                break
            r = await page.evaluate("window.__extracaoProximo()")
            if r.get("fim"):
                break
            progresso = r.get("progresso", "?")
            erro = r.get("erro")
            if erro:
                _log(f"    [docs] seq {r.get('seq')}: {erro} ({progresso})")
            else:
                n_docs = len(r.get("documentos", []))
                _log(f"    [docs] seq {r.get('seq')}: {n_docs} doc(s) ({progresso})")
    except Exception as e:
        _log(f"  [docs] erro durante extracao: {type(e).__name__}: {e}")
        # Garante que nao fica popup aberto atrapalhando o proximo passo (ex:
        # listar_arquivos_paj logo em seguida, no bloco de anexos).
        with contextlib.suppress(Exception):
            await page.evaluate(
                "() => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))"
            )

    return await _finalizar_seguro()


async def listar_arquivos_paj(debug: bool = False) -> dict:
    """Clica no botao "Arquivos" do PAJ atualmente aberto e extrai a lista.

    PRE-CONDICAO: a page ja deve estar no detalhamento do PAJ (chamar
    `movimentacoes_paj(...)` antes). Nao navega — so interage com a pagina atual.

    Retorna dict:
        {
            "ok": bool,
            "itens": [{tipo, descricao, arquivo, href, sem_link_direto, row_index,
                       todas_celulas}, ...],
            "diag": {...}   # sempre presente; use pra debug quando itens == []
        }
    `diag` inclui: candidatos_botao_arquivos, clicou, dialogs_visiveis,
    dialog_escolhido, total_linhas, linhas_filtradas_por_texto, html_snippet.

    `sem_link_direto=True`: o link "Visualizar" dessa linha e' um `<p:commandLink>`
    do PrimeFaces (href="#" cru, sem URL de download real) — tipicamente .doc/.docx
    e imagens "Documentos do Assistido". `href` vem vazio nesse caso (nao mais
    preenchido com uma URL fabricada que so' aponta pra propria pagina). Quem
    consome esta lista (`baixar_todos_arquivos`) usa `row_index` + clique real
    (`baixar_anexo_por_indice`) para esses, em vez de tentar um GET.
    """
    page = await _get_page()

    # 1) Inventariar candidatos ao botao "Arquivos" ANTES de clicar
    diag: dict = {}
    diag["candidatos_botao_arquivos"] = await page.evaluate(
        """
        () => {
            const out = [];
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt && /arquivo/i.test(txt) && txt.length < 40) {
                    out.push({
                        tag: el.tagName,
                        texto: txt,
                        id: el.id || '',
                        classes: el.className || '',
                        onclick_has: !!el.getAttribute('onclick'),
                    });
                }
            }
            return out;
        }
        """
    )

    # 2) Clica no link "Arquivos" — agora mais tolerante
    clicked = await page.evaluate(
        """
        () => {
            const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
            // Preferencia: match EXATO por "Arquivos" depois de trim
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (txt === 'Arquivos' || txt === 'Arquivo(s)') {
                    el.click();
                    return 'exato';
                }
            }
            // Fallback: contem "Arquivos" (mas nao "Arquivo anexo" de movimentacao)
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/^Arquivos?$/i.test(txt) || /^Arquivos\\s*\\(\\d+\\)$/.test(txt)) {
                    el.click();
                    return 'regex';
                }
            }
            // Fallback extremo: qualquer elemento com "Arquivo" curto
            for (const el of els) {
                const txt = (el.textContent || '').trim();
                if (/arquivo/i.test(txt) && txt.length < 15) {
                    el.click();
                    return 'fuzzy:' + txt;
                }
            }
            return '';
        }
        """
    )
    diag["clicou"] = clicked or "NADA"
    if not clicked:
        return {"ok": False, "itens": [], "diag": diag}

    # Aguarda AJAX + eventual navegacao (o link "Arquivos" navega para outra tela).
    # Tenta networkidle primeiro; se falhar, cai pra wait_pf_ajax + sleep.
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        await _wait_pf_ajax(page, timeout=15000)
    await page.wait_for_timeout(1500)

    # 3) Inventariar dialogs REALMENTE visiveis (filtra aria-hidden=true)
    diag["dialogs_visiveis"] = await page.evaluate(
        """
        () => {
            const dialogs = document.querySelectorAll('.ui-dialog');
            return Array.from(dialogs)
                .filter(d => {
                    if (d.getAttribute('aria-hidden') === 'true') return false;
                    if (d.classList.contains('ui-overlay-hidden')) return false;
                    const st = window.getComputedStyle(d);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    return true;
                })
                .map(d => ({
                    id: d.id || '',
                    titulo: (d.querySelector('.ui-dialog-title')?.textContent || '').trim(),
                    linhas_tabela: d.querySelectorAll('table tbody tr').length,
                }));
        }
        """
    )

    # Registra URL pra saber se navegou
    with contextlib.suppress(Exception):
        diag["url_apos_click"] = page.url

    # 4) Extrai linhas — a pagina de Arquivos do SISDPU tem MULTIPLAS tabelas,
    # uma por categoria ("Juntada de documento", "Concluso ao defensor",
    # "Oficio/Carta/Comunicacao expedida", etc.). Iteramos TODAS as tabelas
    # visiveis com botoes "Visualizar" em ordem do DOM e concatenamos as linhas
    # com indice global. A funcao baixar_anexo_por_indice usa a MESMA estrategia
    # de flatten para que `row_index` seja consistente.
    extrato = await page.evaluate(
        """
        () => {
            const diag = {estrategia: '', total_linhas: 0, linhas_filtradas: 0,
                          tabelas_analisadas: 0, tabelas_com_visualizar: 0,
                          categorias: []};

            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                        if (cur.classList && (
                            cur.classList.contains('ui-overlay-hidden') ||
                            cur.classList.contains('ui-hidden-container')
                        )) {
                            if (cur.getAttribute('aria-hidden') === 'true') return false;
                        }
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            // Tenta extrair o rotulo da categoria de uma tabela: o texto curto
            // mais proximo (header/legend/div) que aparece imediatamente antes.
            const detectaCategoria = (tbl) => {
                let prev = tbl.previousElementSibling;
                let tentativas = 0;
                while (prev && tentativas < 6) {
                    const t = (prev.textContent || '').trim();
                    if (t && t.length < 100 && t.length > 2 && !/^(Seq\\.|Nenhum registro)/i.test(t)) {
                        return t.split('\\n')[0].trim().slice(0, 80);
                    }
                    prev = prev.previousElementSibling;
                    tentativas++;
                }
                // Fallback: legend de fieldset ancestral
                let p = tbl.parentElement;
                while (p) {
                    const lg = p.querySelector(':scope > legend, :scope > .ui-fieldset-legend');
                    if (lg) {
                        const t = (lg.textContent || '').trim();
                        if (t) return t.slice(0, 80);
                    }
                    p = p.parentElement;
                }
                return '';
            };

            const tables = document.querySelectorAll('table');
            diag.tabelas_analisadas = tables.length;

            // Coleta tabelas visiveis com links "Visualizar"
            const uteis = [];
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }
            diag.tabelas_com_visualizar = uteis.length;

            if (uteis.length === 0) {
                // Fallback: tabelas visiveis cujos headers mencionem "arquivo"
                for (const tbl of tables) {
                    const ths = Array.from(tbl.querySelectorAll('th')).map(th => (th.textContent || '').toLowerCase());
                    if (!/arquivo|documento/.test(ths.join('|'))) continue;
                    if (!tbl.querySelector('tbody tr')) continue;
                    if (!isVisivel(tbl)) continue;
                    uteis.push(tbl);
                }
                if (uteis.length > 0) diag.estrategia = 'tabela_por_header';
            } else {
                diag.estrategia = uteis.length > 1
                    ? `multi_tabela_visualizar (${uteis.length})`
                    : 'tabela_com_visualizar';
            }

            if (uteis.length === 0) {
                diag.erro = 'nenhuma_tabela_util_encontrada';
                return {resultado: [], diag};
            }

            // Flatten: percorre todas as tabelas em ordem do DOM e concatena as
            // linhas com indice global crescente. A categoria de cada tabela e'
            // anexada a cada linha — util pra naming/diagnostico.
            const resultado = [];
            let idx = 0;
            const snippets = [];
            for (const tbl of uteis) {
                const categoria = detectaCategoria(tbl);
                diag.categorias.push({categoria: categoria, linhas: 0});
                const catIdx = diag.categorias.length - 1;
                const rows = tbl.querySelectorAll('tbody tr');
                diag.total_linhas += rows.length;
                for (const tr of rows) {
                    if (tr.classList.contains('ui-widget-header')) continue;
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 5) continue;  // Precisa das 5 primeiras colunas no mínimo
                    const textos = Array.from(cells).map(c => (c.textContent || '').trim());
                    // Primeira coluna DEVE ser um número válido (Seq.)
                    const seq = textos[0] || '';
                    if (!seq || !/^\\d+$/.test(seq)) continue;
                    const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                    let temVis = false;
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { temVis = true; break; }
                    }
                    if (!temVis) continue;
                    // Extrai nome do arquivo DIRETAMENTE do link "Visualizar" (fonte de verdade)
                    const linkVis = tr.querySelector('a, button, span[role="button"]');
                    let nome_arquivo = '';
                    let href = '';
                    let semLinkDireto = false;
                    if (linkVis) {
                        // Tenta extrair em ordem de confiabilidade:
                        // 1. Atributo download (HTML5)
                        // 2. Atributo data-filename (customizado)
                        // 3. Href com padrão comuns (ex: /download/123)
                        const downloadAttr = linkVis.getAttribute('download');
                        const dataNome = linkVis.getAttribute('data-filename');
                        // getAttribute (RAW, nunca .href resolvido) — ver mesmo padrao e
                        // mesmo motivo em extrair_documentos_movimentacoes.js: alguns
                        // anexos (imagens "Documentos do Assistido", peticoes .doc/.docx)
                        // sao <p:commandLink> do PrimeFaces SEM URL de download nenhuma —
                        // href="#" cru, clique dispara um POST do formulario via JS. Se
                        // resolvido por linkVis.href, "#" vira a URL da PROPRIA pagina
                        // (nunca um erro de sintaxe), entao so' getAttribute cru pega isso.
                        let hrefAttr = linkVis.getAttribute('href') || '';

                        if (hrefAttr === '#') {
                            // NAO sintetiza URL nenhuma pra esse caso — antes isso virava
                            // `window.location.origin + '/sisdpu/#'`, uma URL "absoluta"
                            // com cara de valida que na verdade so' aponta pra propria
                            // pagina atual. Um GET nela sempre devolve HTML (a propria
                            // tela), nunca o arquivo — silenciosamente tratado como link
                            // "real" e falhando so' mais tarde, sem sinal nenhum de que
                            // esse doc precisava de clique de verdade (nao GET).
                            semLinkDireto = true;
                        } else {
                            // Torna href absoluto se for relativo
                            if (hrefAttr && !hrefAttr.startsWith('http')) {
                                hrefAttr = window.location.origin + (hrefAttr.startsWith('/') ? '' : '/sisdpu/') + hrefAttr;
                            }
                            href = hrefAttr;
                        }

                        if (downloadAttr) nome_arquivo = downloadAttr;
                        else if (dataNome) nome_arquivo = dataNome;
                        else if (href && /\\.(pdf|docx?|xlsx?|txt|zip|jpg|png|gif)$/i.test(href)) {
                            // Se href tem extensão, usar último segmento como nome
                            nome_arquivo = href.split('/').pop();
                        }
                    }
                    // Fallback para coluna "Arquivo" se não conseguir extrair do link
                    if (!nome_arquivo) nome_arquivo = textos[4] || '';

                    resultado.push({
                        row_index: idx++,
                        seq: textos[0] || '',
                        data: textos[1] || '',
                        descricao: textos[2] || '',
                        tipo_arquivo: textos[3] || '',
                        arquivo: nome_arquivo,
                        href: href,
                        sem_link_direto: semLinkDireto,
                        categoria: categoria,
                        todas_celulas: textos,
                    });
                    diag.categorias[catIdx].linhas++;
                }
                try { snippets.push((tbl.outerHTML || '').slice(0, 800)); } catch (e) {}
            }
            // Remove duplicatas exatas (seq + arquivo idênticos)
            const vistos = new Set();
            const resultadoDeduplic = [];
            for (const item of resultado) {
                const chave = item.seq + '|' + item.arquivo;
                if (!vistos.has(chave)) {
                    vistos.add(chave);
                    resultadoDeduplic.push(item);
                }
            }
            const n_removidas = resultado.length - resultadoDeduplic.length;
            if (n_removidas > 0) diag.linhas_removidas = n_removidas;

            // Recalcula row_index sequencial (0, 1, 2, ...) após desduplicação
            for (let i = 0; i < resultadoDeduplic.length; i++) {
                resultadoDeduplic[i].row_index = i;
            }

            diag.linhas_filtradas = resultadoDeduplic.length;
            diag.html_snippet = snippets.join('\\n---\\n').slice(0, 2000);
            return {resultado: resultadoDeduplic, diag};
        }
        """
    )

    diag.update(extrato.get("diag", {}))
    itens = extrato.get("resultado", []) or []
    return {"ok": True, "itens": itens, "diag": diag}


_JS_MARCAR_POR_INDICE = """
(idx) => {
    const isVisivel = (el) => {
        let cur = el;
        while (cur) {
            if (cur.nodeType === 1) {
                if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
            }
            cur = cur.parentElement;
        }
        try {
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return false;
        } catch (e) {}
        return true;
    };
    const tables = document.querySelectorAll('table');
    const uteis = [];
    for (const tbl of tables) {
        const rows = tbl.querySelectorAll('tbody tr');
        if (rows.length === 0) continue;
        // Filtra tabelas que sao placeholders "Nenhum registro"
        let todosVazios = true;
        for (const r of rows) {
            const cells = r.querySelectorAll('td');
            if (cells.length === 0) continue;
            const textos = Array.from(cells).map(c => (c.textContent || '').trim());
            if (!/nenhum\\s+registro/i.test(textos.join(' '))) {
                todosVazios = false;
                break;
            }
        }
        if (todosVazios) continue;

        let comVis = 0;
        for (const r of rows) {
            const alvos = r.querySelectorAll('a, button, span[role="button"]');
            for (const a of alvos) {
                if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
            }
        }
        if (comVis === 0) continue;
        if (!isVisivel(tbl)) continue;
        uteis.push(tbl);
    }
    if (uteis.length === 0) return false;
    const linhas = [];
    for (const tbl of uteis) {
        const rows = Array.from(tbl.querySelectorAll('tbody tr'))
            .filter(tr => {
                if (tr.classList.contains('ui-widget-header')) return false;
                if (tr.querySelectorAll('td').length === 0) return false;
                const cells = tr.querySelectorAll('td');
                const textos = Array.from(cells).map(c => (c.textContent || '').trim());
                const seq = (textos[0] || '').trim();
                if (!seq || /nenhum|cabeçalho|conclus[aã]o|juntada|oficio/i.test(seq)) return false;
                const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                for (const a of alvos) {
                    if (/visualizar/i.test((a.textContent || '').trim())) return true;
                }
                return false;
            });
        for (const r of rows) linhas.push(r);
    }
    if (idx >= linhas.length) return false;
    const row = linhas[idx];
    const alvos = row.querySelectorAll('a, button, span[role="button"]');
    for (const el of alvos) {
        if (/visualizar/i.test((el.textContent || '').trim())) {
            el.setAttribute('data-dpu-pick', '1');
            return true;
        }
    }
    return false;
}
"""

_JS_MARCAR_POR_SEQ = """
(seq) => {
    // Busca a linha pela coluna Seq. (primeira coluna) em TODAS as tabelas.
    // Mais robusto que posicao quando a pagina e' atualizada entre downloads.
    const tables = document.querySelectorAll('table');
    for (const tbl of tables) {
        for (const tr of tbl.querySelectorAll('tbody tr')) {
            const cells = tr.querySelectorAll('td');
            if (cells.length === 0) continue;
            if ((cells[0].textContent || '').trim() !== seq) continue;
            const alvos = tr.querySelectorAll('a, button, span[role="button"]');
            for (const a of alvos) {
                if (/visualizar/i.test((a.textContent || '').trim())) {
                    a.setAttribute('data-dpu-pick', '1');
                    return true;
                }
            }
        }
    }
    return false;
}
"""

_JS_CLICAR_ARQUIVOS = """
() => {
    const els = Array.from(document.querySelectorAll('a, button, span[role="button"], .ui-button'));
    for (const el of els) {
        const txt = (el.textContent || '').trim();
        if (txt === 'Arquivos' || txt === 'Arquivo(s)') { el.click(); return 'exato'; }
    }
    for (const el of els) {
        const txt = (el.textContent || '').trim();
        if (/^Arquivos?$/i.test(txt) || /^Arquivos\\s*\\(\\d+\\)$/.test(txt)) { el.click(); return 'regex'; }
    }
    return '';
}
"""

# Extensoes conhecidas para deteccao via nome de arquivo (ultima extensao valida).
_EXTS_ARQUIVO = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ".txt", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".csv", ".xml", ".html", ".htm",
    ".odt", ".ods", ".odp", ".rtf",
}


def extrair_id_arquivo(url: str) -> str:
    """Extrai o ID do documento a partir da URL/marcador de download do SISDPU.

    Ex: '.../rest/download/pdf/42296157/Nome.pdf' -> '42296157'.
    Esse ID pertence ao ARQUIVO (nao a seq/listagem) e e' o mesmo em qualquer
    mecanismo que o descubra (listar_arquivos_paj ou extracao por seq de
    movimentacao) — usado como chave de juncao para nomear os arquivos
    baixados de forma que o frontend consiga casar o link da coluna
    "Documentos" com a copia local ja baixada, sem depender do nome do
    arquivo (que pode se repetir, ex: "email.pdf").

    Documentos sem link de download direto (anexos tipo imagem servidos via
    <p:commandLink> do PrimeFaces — ver extrair_documentos_movimentacoes.js)
    chegam aqui como o marcador sintetico 'sisdpu-commandlink:<id-do-elemento>'
    em vez de uma URL — devolve o id do elemento sanitizado (':' nao e' valido
    em nome de arquivo do Windows) em vez de tentar casar a regex de URL.
    """
    if url and url.startswith("sisdpu-commandlink:"):
        bruto = url[len("sisdpu-commandlink:"):]
        return re.sub(r"[^A-Za-z0-9_-]", "_", bruto) if bruto else ""
    m = re.search(r"/(\d+)/[^/]*$", url or "")
    return m.group(1) if m else ""


def _ext_de_nome_arquivo(nome: str) -> str:
    """Extrai extensao do FINAL do nome de arquivo validando contra lista conhecida.

    Usa rfind('.') para pegar sempre o ULTIMO ponto — evita interpretar pontos
    no meio do nome (ex: '1234-56.789.4.01.2345') como extensao.
    Retorna '' se nao reconhecida.
    """
    if not nome:
        return ""
    lower = nome.lower().strip()
    last = lower.rfind(".")
    if last < 0 or last == len(lower) - 1:
        return ""
    ext = lower[last:]
    return ext if ext in _EXTS_ARQUIVO else ""


async def extrair_lista_arquivos_paj() -> list[dict]:
    """Extrai a lista de arquivos do PAJ atualmente aberto via JavaScript.

    Itera todas as tabelas visiveis e retorna uma lista de documentos com:
    {seq, data, nomeArq, href}

    Estrategia identica a SISDPU-AnalisarPAJ2Cat skill.
    """
    page = await _get_page()

    resultado = await page.evaluate(
        """
        () => {
            const docs = [];
            const isVisivel = (el) => {
                let cur = el;
                while (cur) {
                    if (cur.nodeType === 1) {
                        if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                    }
                    cur = cur.parentElement;
                }
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                } catch (e) {}
                return true;
            };

            const tables = document.querySelectorAll('table');
            const uteis = [];

            // Coleta tabelas visiveis com links "Visualizar"
            for (const tbl of tables) {
                const rows = tbl.querySelectorAll('tbody tr');
                if (rows.length === 0) continue;
                let comVis = 0;
                for (const r of rows) {
                    const alvos = r.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) { comVis++; break; }
                    }
                }
                if (comVis === 0) continue;
                if (!isVisivel(tbl)) continue;
                uteis.push(tbl);
            }

            // Itera todas as tabelas uteis e coleta arquivos
            for (const tbl of uteis) {
                const rows = tbl.querySelectorAll('tbody tr');
                for (const tr of rows) {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 5) continue;

                    const seq = (cells[0]?.textContent || '').trim();
                    if (!seq || !/^\\d+$/.test(seq)) continue;

                    const data = (cells[1]?.textContent || '').trim();

                    // Nome do arquivo vem da coluna cells[4] (coluna "Arquivo")
                    const nomeArq = (cells[4]?.textContent || '').trim().split('\\n')[0].trim();

                    // Link "Visualizar" — usar getAttribute('href') em vez de .href
                    // para evitar parametros JSF que quebram downloads
                    let href = '';
                    const alvos = tr.querySelectorAll('a, button, span[role="button"]');
                    for (const a of alvos) {
                        if (/visualizar/i.test((a.textContent || '').trim())) {
                            href = a.getAttribute('href') || '';
                            break;
                        }
                    }

                    if (nomeArq && href) {
                        docs.push({
                            seq: seq,
                            data: data,
                            nomeArq: nomeArq,
                            href: href
                        });
                    }
                }
            }

            return docs;
        }
        """
    )

    return resultado or []


async def _abrir_popup_documentos_seq(page: Page, seq: str) -> bool:
    """Clica no icone de anexo da linha `seq` da tabela de movimentacoes e
    espera o popup de documentos ('Arquivo' no cabecalho) abrir e ficar
    visivel. PRE-CONDICAO: a page ja esta no detalhamento do PAJ
    (movimentacoes_paj ja rodou). Retorna True se o popup abriu."""
    clicou = await page.evaluate(
        """
        (seqAlvo) => {
            const tables = document.querySelectorAll('table');
            let tableMov = null;
            for (const t of tables) {
                const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
                    .map(h => h.textContent.trim()).filter(Boolean);
                if (headers.includes('Seq.') && headers.includes('Data/Hora')) { tableMov = t; break; }
            }
            if (!tableMov) return false;
            const rows = tableMov.querySelectorAll('tbody tr');
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (!cells.length) continue;
                if ((cells[0]?.textContent || '').trim() !== String(seqAlvo)) continue;
                const clickable = cells[1]?.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');
                if (!clickable) return false;
                clickable.click();
                return true;
            }
            return false;
        }
        """,
        seq,
    )
    if not clicou:
        return False
    try:
        await page.wait_for_function(
            """
            () => {
                const tables = document.querySelectorAll('table');
                for (let j = tables.length - 1; j >= Math.max(0, tables.length - 40); j--) {
                    const headers = Array.from(tables[j].querySelectorAll('thead th, tbody tr:first-child td'))
                        .map(h => h.textContent.trim()).filter(Boolean);
                    if (headers.includes('Arquivo') && tables[j].offsetParent !== null) return true;
                }
                return false;
            }
            """,
            timeout=10000,
        )
    except PWTimeoutError:
        return False
    return True


async def _fechar_popup_documentos(page: Page) -> None:
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)


async def baixar_documento_via_commandlink(
    seq: str,
    command_link_id: str,
    destino: Path,
) -> Path | None:
    """Baixa UM documento que nao tem link de download direto — os anexos
    tipo imagem (jpeg/png) "Documentos do Assistido" vem como <p:commandLink>
    do PrimeFaces (href="#" + onclick que faz um POST do formulario inteiro),
    entao um `httpx.get(href)` simples nao funciona (baixa a propria pagina,
    nao o arquivo). Aqui clicamos DE VERDADE no elemento (via id, ver
    extrair_documentos_movimentacoes.js) e capturamos o evento `download` do
    Playwright — mesmo padrao ja usado por `baixar_anexo_por_indice`
    (deprecado), so' que localizando o elemento pelo id em vez de por indice
    de linha.

    PRE-CONDICAO: a page ja esta no detalhamento do PAJ (movimentacoes_paj
    ja rodou) — abre e fecha o popup da seq sozinho.
    """
    page = await _get_page()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    if not await _abrir_popup_documentos_seq(page, seq):
        return None

    try:
        async with page.expect_event("download", timeout=30000) as dl_info:
            await page.click(_sel(command_link_id))
        download = await dl_info.value
        await download.save_as(str(destino))
    except Exception:
        await _fechar_popup_documentos(page)
        return None

    await _fechar_popup_documentos(page)

    if destino.exists() and destino.stat().st_size > 0:
        return destino
    return None


async def baixar_todos_arquivos(
    pasta_destino: Path,
    lista_arquivos: list[dict] | None = None,
    log: Callable[[str], None] | None = None,
    seq_por_file_id: dict[str, int] | None = None,
) -> dict:
    """Baixa todos os arquivos do PAJ via JavaScript fetch + local server.

    Se lista_arquivos não for fornecida, extrai via listar_arquivos_paj().
    Caso contrário, usa a lista ja fornecida (cada item deve ter: arquivo, seq, data, href ou as keys esperadas).

    `seq_por_file_id`: mapa {id_do_arquivo (str) -> seq_real (int)} vindo da
    extracao por seq de movimentacao (`extrair_documentos_por_seq`). Usado
    pra nomear o arquivo com a seq REAL da movimentacao — se o id do arquivo
    (extraido do href) nao estiver no mapa, cai no `seq` fornecido pela lista
    de origem (listar_arquivos_paj, que e' so um indice de listagem).

    Estrategia:
    1. Extrai lista de arquivos (ou usa fornecida)
    2. Inicia local server (download_server.py) na porta 8765
    3. Para cada arquivo: fetch() + Base64 + POST ao servidor local
    4. Servidor salva arquivo em disco
    5. Mata o processo do servidor

    Retorna {baixados: int, erros: list, total: int}
    """
    import subprocess
    import asyncio

    def _log(msg: str) -> None:
        if log:
            with contextlib.suppress(Exception):
                log(msg)

    pasta_destino = Path(pasta_destino)
    pasta_destino.mkdir(parents=True, exist_ok=True)

    # 1) Extrai lista de arquivos
    _log("[fetch] preparando downloads...")
    if lista_arquivos is None:
        _log("[fetch] extraindo lista de arquivos...")
        resultado = await listar_arquivos_paj()
        docs = resultado.get("itens", []) if isinstance(resultado, dict) else (resultado or [])
    else:
        docs = lista_arquivos

    # Normaliza para formato esperado pelo fetch (precisa de: seq, data, nomeArq, href)
    # Converte da lista de listar_arquivos_paj que tem: seq, data, descricao, tipo_arquivo, arquivo
    docs_normalizados = []
    for doc in docs:
        seq = doc.get("seq", "?")
        data = doc.get("data", "")
        # Nome do arquivo: tenta 'arquivo' primeiro (de listar_arquivos_paj), depois 'nomeArq' (de extrair_lista_arquivos_paj)
        nome_arq = doc.get("arquivo") or doc.get("nomeArq", "arquivo")
        # href: tenta 'href' direto ou reconstrói... deixa vazio se não houver
        href = doc.get("href", "")

        docs_normalizados.append({
            "seq": seq,
            "data": data,
            "nomeArq": nome_arq,
            "href": href,
            # so' presente quando href e' o marcador sintetico 'sisdpu-commandlink:...'
            # (ver extrair_documentos_movimentacoes.js) — usado pra clicar de
            # verdade no elemento em vez de tentar um GET que nao existe.
            "commandLinkId": doc.get("commandLinkId"),
            # Sinalizado por listar_arquivos_paj() quando o link "Visualizar" e'
            # um <p:commandLink> do PrimeFaces (href="#" cru) — mesma situacao
            # do commandLinkId acima, so' que vinda da tela "Arquivos" (que nao
            # expoe um id de elemento estavel, so' a posicao na tabela).
            "sem_link_direto": bool(doc.get("sem_link_direto")),
            "row_index": doc.get("row_index"),
        })

    docs = docs_normalizados
    _log(f"[fetch] {len(docs)} arquivo(s) encontrado(s)")

    if not docs:
        return {"baixados": 0, "erros": [], "total": 0}

    # 2) Inicia servidor local
    #
    # HISTORICO: cada chamada desta funcao subia um download_server.py NOVO e
    # o matava no `finally` ao final — um ciclo spawn+kill POR REQUISICAO. Isso
    # cria uma janela de corrida real: se dois downloads acontecerem em rapida
    # sucessao (ou o kill anterior ainda nao liberou a porta), o `Popen` daqui
    # ainda "sobe" (o processo abre), mas falha o bind da porta 8765 e MORRE
    # sozinho pouco depois — sem que este codigo jamais soubesse disso, porque
    # o sleep(1) fixo assumia sucesso e o stderr do processo (onde o traceback
    # do bind ia parar) nunca era lido. Resultado: todo POST subsequente dava
    # erro de conexao (servidor morto), sync tentado de novo minutos depois
    # (porta ja livre) e funcionava — exatamente o padrao "erro, repete, ok"
    # relatado em produção. Corrigido com: (1) probe de saude ANTES de tentar
    # subir um novo processo — se algo ja estiver respondendo em 8765 (de uma
    # requisicao concorrente, ou um processo anterior que nao terminou a
    # tempo), REAPROVEITA em vez de tentar subir outro (que so' falharia); (2)
    # apos o Popen, poll ATIVO (nao sleep fixo) confirmando resposta real; (3)
    # se nunca responder, LE o stderr do processo pra saber o motivo real em
    # vez de deixar a inconsistencia estourar so' nos POSTs individuais.
    async def _servidor_saudavel() -> bool:
        try:
            async with httpx.AsyncClient(timeout=0.5) as probe:
                r = await probe.options("http://127.0.0.1:8765")
                return r.status_code < 500
        except Exception:
            return False

    import httpx

    _log("[fetch] verificando servidor local (porta 8765)...")
    proc = None  # so' fica setado se ESTA chamada tiver spawnado (unico caso em que ela deve matar no finally)

    if await _servidor_saudavel():
        _log("[fetch] servidor local já ativo (reaproveitando)")
    else:
        _log("[fetch] iniciando servidor local na porta 8765...")

        # Tenta em ordem: projeto local, depois PREV4
        server_path = Path(__file__).resolve().parents[1] / ".claude" / "download_server.py"
        if not server_path.exists():
            server_path = Path("C:/Claude/DPU/PREV4/.claude/download_server.py")

        if not server_path.exists():
            _log(f"[fetch] ERRO: servidor {server_path} não encontrado")
            return {"baixados": 0, "erros": ["servidor download_server.py não encontrado"], "total": len(docs)}

        try:
            # Inicia o servidor como background process (Windows)
            # Usa cwd do diretório do projeto para que paths relativos funcionem
            cwd = Path(__file__).resolve().parents[1].parent

            proc = subprocess.Popen(
                ["python", str(server_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
            )

            # Poll ativo em vez de sleep fixo — sai assim que responder (rapido
            # no caso comum) e da' tempo real de sobra no caso lento, em vez de
            # um sleep(1) que tanto pode ser curto demais (maquina lenta) quanto
            # desperdicar tempo (servidor sobe em 100ms na maioria das vezes).
            subiu = False
            for _ in range(20):  # ate ~4s (20 x 200ms)
                if proc.poll() is not None:
                    break  # processo ja morreu — nao insiste
                if await _servidor_saudavel():
                    subiu = True
                    break
                await asyncio.sleep(0.2)

            if not subiu:
                # Motivo real do erro (ex.: porta ja em uso por outro processo
                # que o probe acima nao pegou por timing) — antes ficava
                # invisivel: o stderr do subprocess nunca era lido.
                motivo = "timeout aguardando resposta"
                if proc.poll() is not None:
                    with contextlib.suppress(Exception):
                        _, stderr_bytes = proc.communicate(timeout=1)
                        if stderr_bytes:
                            motivo = stderr_bytes.decode("utf-8", errors="replace").strip().splitlines()[-1][:200]
                _log(f"[fetch] ✗ servidor local não respondeu ({motivo})")
                with contextlib.suppress(Exception):
                    proc.terminate()
                return {"baixados": 0, "erros": [f"servidor local não respondeu: {motivo}"], "total": len(docs)}
        except Exception as e:
            _log(f"[fetch] ✗ falha ao iniciar servidor: {type(e).__name__}")
            if proc:
                with contextlib.suppress(Exception):
                    proc.terminate()
            return {"baixados": 0, "erros": [f"falha ao iniciar servidor"], "total": len(docs)}

    page = await _get_page()
    context = page.context
    baixados = 0
    erros = []

    try:
        # 3) Download via Playwright (usa cookies da session autenticada) + POST ao servidor
        # (httpx ja importado acima, no probe de saude do servidor local)
        import base64

        # Extrai cookies do contexto Playwright para passar ao httpx
        cookies_list = await context.cookies()
        cookies_dict = {c["name"]: c["value"] for c in cookies_list}

        for idx, doc in enumerate(docs, start=1):
            seq = doc.get("seq", "?")
            nomeArq = doc.get("nomeArq", "arquivo")
            href = doc.get("href", "")

            # Formata nome para POST: seqNNN - ID - NOME
            # O ID (extraido do href) e' a chave estavel que o frontend usa
            # pra casar o link da coluna "Documentos" com este arquivo local
            # — nao depende do nome (que pode se repetir, ex: "email.pdf").
            # A seq usa a REAL (do mapa por id), com 3 digitos pra ordenar
            # certo na pasta quando passar de 100 seqs.
            file_id = extrair_id_arquivo(href)
            seq_real = (seq_por_file_id or {}).get(file_id, seq)
            try:
                seq_fmt = f"{int(seq_real):03d}"
            except (TypeError, ValueError):
                seq_fmt = str(seq_real)

            nome_para_post = f"seq{seq_fmt} - {file_id or '0'} - {nomeArq}"

            _log(f"[{idx}/{len(docs)}]   {nomeArq}")

            # Documentos sem link de download direto (imagens via <p:commandLink>
            # do PrimeFaces — ver extrair_documentos_movimentacoes.js) precisam
            # de um clique de verdade + captura do evento `download`, nao um
            # GET simples (que so' baixaria a propria pagina). Bifurca ANTES do
            # fluxo httpx+servidor local de baixo, que so' serve pra URLs reais.
            command_link_id = doc.get("commandLinkId")
            if href.startswith("sisdpu-commandlink:") and command_link_id:
                destino_path = pasta_destino / nome_para_post
                resultado_click = await baixar_documento_via_commandlink(
                    str(seq_real), command_link_id, destino_path
                )
                if resultado_click:
                    tamanho = resultado_click.stat().st_size
                    tam_str = (
                        f"{tamanho / 1024:.1f} KB" if tamanho < 1024 * 1024
                        else f"{tamanho / (1024 * 1024):.1f} MB"
                    )
                    _log(f"               ... Concluído    ✓ {tam_str} (clique)")
                    baixados += 1
                else:
                    _log(f"               ... ERRO     ✗ falha ao clicar/capturar download")
                    erros.append(f"{nomeArq}: falha no download via clique (sem link direto)")
                continue

            # Mesma situacao do commandLinkId acima (anexo sem URL de download
            # real — tipicamente .doc/.docx e imagens "Documentos do
            # Assistido"), so' que sinalizada por `listar_arquivos_paj()` (tela
            # "Arquivos", que expoe `row_index` em vez de um id de elemento
            # estavel). ANTES disso, o codigo tentava um GET na URL fabricada
            # (window.location.origin + '/sisdpu/#'), que sempre volta HTML da
            # propria pagina — 100% reproduzivel pra TODO .doc/.docx/imagem
            # sem link direto, nao uma falha intermitente.
            row_index = doc.get("row_index")
            if doc.get("sem_link_direto") and row_index is not None:
                destino_path = pasta_destino / nome_para_post
                resultado_click = await baixar_anexo_por_indice(
                    row_index, destino_path, seq=str(seq_real)
                )
                if resultado_click:
                    tamanho = resultado_click.stat().st_size
                    tam_str = (
                        f"{tamanho / 1024:.1f} KB" if tamanho < 1024 * 1024
                        else f"{tamanho / (1024 * 1024):.1f} MB"
                    )
                    _log(f"               ... Concluído    ✓ {tam_str} (clique)")
                    baixados += 1
                else:
                    _log(f"               ... ERRO     ✗ falha ao clicar/capturar download")
                    erros.append(f"{nomeArq}: falha no download via clique (sem link direto)")
                continue

            # Se href estiver vazio, trata como erro
            if not href:
                _log(f"               ... ERRO     ✗ href vazio")
                erros.append(f"{nomeArq}: href vazio")
                continue

            try:
                # Fetch do arquivo via Playwright (reutiliza cookies autenticados)
                pasta_abs = str(pasta_destino).replace("\\", "/")

                # Usa httpx síncrono com cookies do contexto do Playwright
                async with httpx.AsyncClient(cookies=cookies_dict, verify=False) as client:
                    # Fetch arquivo
                    resp = await client.get(href, follow_redirects=True, timeout=30.0)
                    if resp.status_code != 200:
                        _log(f"               ... ERRO     ✗ HTTP {resp.status_code}")
                        erros.append(f"{nomeArq}: HTTP {resp.status_code}")
                        continue

                    conteudo = resp.content
                    tamanho = len(conteudo)

                    if tamanho == 0:
                        _log(f"               ... ERRO     ✗ vazio")
                        erros.append(f"{nomeArq}: arquivo vazio")
                        continue

                    # Proteção: alguns anexos (imagens via <p:commandLink> do
                    # PrimeFaces, sem link de download direto — ver
                    # extrair_documentos_movimentacoes.js) tem `href` quebrado
                    # que aponta pra propria PAGINA do SISDPU em vez do
                    # arquivo; um GET nela devolve HTTP 200 com HTML (a tela
                    # de shell/login), nao o binario esperado — sem essa
                    # checagem isso era salvo silenciosamente com a extensão
                    # do nome esperado (ex: .jpeg), virando um arquivo
                    # corrompido no disco. Detecta pelo Content-Type OU pela
                    # assinatura dos primeiros bytes e recusa a gravar.
                    ext_esperada = Path(nomeArq).suffix.lower()
                    content_type = resp.headers.get("content-type", "").lower()
                    parece_html = "text/html" in content_type or conteudo[:100].lstrip().lower().startswith(
                        (b"<!doctype", b"<html", b"<?xml")
                    )
                    if parece_html and ext_esperada not in (".html", ".htm", ".xml"):
                        _log(f"               ... ERRO     ✗ recebeu HTML em vez do arquivo (sem link de download direto)")
                        erros.append(f"{nomeArq}: href não é um link de download direto (recebeu página HTML)")
                        continue

                    # Formata tamanho em KB/MB
                    if tamanho < 1024 * 1024:
                        tam_str = f"{tamanho / 1024:.1f} KB"
                    else:
                        tam_str = f"{tamanho / (1024 * 1024):.1f} MB"

                    # Converte para Base64
                    b64_data = base64.b64encode(conteudo).decode("ascii")

                    # POST ao servidor local
                    try:
                        post_resp = await client.post(
                            "http://127.0.0.1:8765",
                            json={
                                "filename": nome_para_post,
                                "data": b64_data,
                                "target": pasta_abs,
                            },
                            timeout=10.0,
                        )
                        if post_resp.status_code == 200:
                            _log(f"               ... Concluído    ✓ {tam_str}")
                            baixados += 1
                        else:
                            _log(f"               ... ERRO     ✗ POST {post_resp.status_code}")
                            erros.append(f"{nomeArq}: POST HTTP {post_resp.status_code}")
                    except Exception as e:
                        _log(f"               ... ERRO     ✗ {type(e).__name__}")
                        erros.append(f"{nomeArq}: POST {type(e).__name__}")

            except Exception as e:
                _log(f"               ... ERRO     ✗ {type(e).__name__}")
                erros.append(f"{nomeArq}: {type(e).__name__}")

    finally:
        # 4) Mata o servidor
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    if baixados == len(docs):
        _log(f"[fetch] ✓ {baixados} arquivo(s) baixado(s) com sucesso")
    else:
        _log(f"[fetch] ⚠ {baixados}/{len(docs)} arquivo(s) baixado(s) — {len(erros)} erro(s)")
    return {
        "baixados": baixados,
        "erros": erros,
        "total": len(docs),
    }


async def baixar_anexo_por_indice(
    row_index: int,
    destino,
    seq: str = "",
    ext_hint: str = "",
):
    """Clica em "Visualizar" da linha `row_index` (na tela "Arquivos" ja aberta
    por `listar_arquivos_paj()`) e captura o `download` do Playwright.

    NAO esta mais deprecada: e' o fallback ATIVO usado por `baixar_todos_arquivos`
    para os anexos sinalizados com `sem_link_direto=True` — tipicamente .doc/.docx
    e imagens "Documentos do Assistido" que sao `<p:commandLink>` do PrimeFaces
    (href="#", sem URL de download real). Um `httpx.get()` nesses nunca funciona
    (so' baixa a propria pagina); so' o clique real + captura do evento `download`
    consegue. Analogo a `baixar_documento_via_commandlink`, mas localizando o
    elemento por posicao na tabela em vez de por id (a tela "Arquivos" nao
    expoe um id de elemento estavel do jeito que o popup por-seq expoe).
    """
    from pathlib import Path as _Path

    destino = _Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    page = await _get_page()

    found = await page.evaluate(_JS_MARCAR_POR_INDICE, row_index)
    metodo = "indice"

    if not found and seq:
        metodo = "seq-fallback"
        with contextlib.suppress(Exception):
            await page.evaluate(_JS_CLICAR_ARQUIVOS)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                await _wait_pf_ajax(page, timeout=10000)
            await page.wait_for_timeout(1000)
        found = await page.evaluate(_JS_MARCAR_POR_SEQ, seq)

    if not found:
        return None

    def _limpar_marker():
        with contextlib.suppress(Exception):
            return page.evaluate(
                "() => document.querySelectorAll('[data-dpu-pick]')"
                ".forEach(el => el.removeAttribute('data-dpu-pick'))"
            )

    try:
        async with page.expect_event("download", timeout=60000) as dl_info:
            await page.click('[data-dpu-pick="1"]')
        download = await dl_info.value

        sugerido = (download.suggested_filename or "").strip()
        ext_sugerida = _Path(sugerido).suffix.lower() if sugerido else ""
        if not ext_sugerida or ext_sugerida not in _EXTS_ARQUIVO:
            ext_sugerida = ext_hint if ext_hint else ".bin"

        atual_suffix = destino.suffix.lower()
        if atual_suffix and atual_suffix in _EXTS_ARQUIVO:
            destino_final = destino
        else:
            destino_final = destino.with_suffix(ext_sugerida)

        await download.save_as(str(destino_final))
    except Exception as e:
        await _limpar_marker()
        return None

    await _limpar_marker()

    if destino_final.exists() and destino_final.stat().st_size > 0:
        return destino_final
    return None


async def fechar_dialogo_arquivos() -> None:
    """Fecha o dialogo 'Arquivos' se estiver aberto (para nao interferir em outro PAJ)."""
    page = await _get_page()
    try:
        await page.evaluate(
            """
            () => {
                const dialogs = document.querySelectorAll('.ui-dialog:not(.ui-overlay-hidden)');
                for (const d of dialogs) {
                    const titulo = d.querySelector('.ui-dialog-title');
                    if (titulo && /arquivo/i.test(titulo.textContent || '')) {
                        const btn = d.querySelector('.ui-dialog-titlebar-close');
                        if (btn) btn.click();
                    }
                }
            }
            """
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def fechar() -> None:
    """Fecha o navegador headless."""
    global _playwright, _browser, _page
    if _browser:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
        _page = None
    if _playwright:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None


async def reconectar() -> dict:
    """Força uma RECONEXÃO no SISDPU — recupera de conflito de sessão / sessão
    derrubada por outro login. Fecha o navegador headless atual e refaz o login
    com as credenciais do cofre (ou do .env, no modo legado). Ao criar uma sessão
    nova, ela "assume a vez", derrubando a sessão concorrente do mesmo usuário.
    Retorna {ok, status, detalhe}."""
    global _browser, _page
    with contextlib.suppress(Exception):
        if _browser:
            await _browser.close()
    _browser = None
    _page = None
    try:
        await _login()
        page = await _get_page()
        if "login" in (page.url or "").lower():
            return {"ok": False, "status": "credenciais",
                    "detalhe": "O SISDPU recusou o usuário/senha ao reconectar. "
                               "Confira as credenciais cadastradas no cofre "
                               "(Configurações)."}
    except CredenciaisInvalidas as e:
        return {"ok": False, "status": "credenciais",
                "detalhe": f"O SISDPU recusou o usuário/senha ao reconectar: {e}"}
    except RuntimeError as e:
        return {"ok": False, "status": "indisponivel", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        with contextlib.suppress(Exception):
            from services import conexao_service
            conexao_service.marcar_erro(str(e))
        return {"ok": False, "status": "erro",
                "detalhe": f"Não foi possível reconectar ({type(e).__name__}: {e})."}
    return {"ok": True, "status": "ok",
            "detalhe": "Reconectado ao SISDPU — sessão do painel reativada."}


if __name__ == "__main__":
    import asyncio
    import json

    async def _smoke() -> None:
        # Carrega .env do projeto para expor SISDPU_USERNAME/PASSWORD
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")

        try:
            data = await caixa_de_entrada()
            itens = data.get("itens_tabela", [])[:5]
            print(json.dumps(itens, ensure_ascii=False, indent=2))
            print(f"(+{len(data.get('itens_tabela', [])) - len(itens)} itens)")
        finally:
            await fechar()

    asyncio.run(_smoke())
