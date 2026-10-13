"""Automação do e-proc para a PETIÇÃO INICIAL de benefício por incapacidade
(fluxo ensinado ao vivo pelo Defensor em 2026-10-08 — ver
`Estrutura de Construção/APRENDIZADO_PETICAO_INICIAL_EPROC.md` para cada
seletor e regra).

Duas etapas, mesmo espírito de `eproc_client.preparar_peticionamento` /
`confirmar_peticionamento`:

1. `preparar(plano)` — abre "Petição Inicial" numa ABA NOVA da janela do
   e-proc e percorre o assistente (Informações do processo → Assuntos →
   Partes → Informações Adicionais), PARANDO na última etapa, antes de
   "Finalizar". Nada é distribuído. A aba fica aberta pro Defensor conferir.
2. `ajuizar()` — reencontra essa aba, clica "Finalizar" e confirma o modal
   de conferência ("Confirmar ajuizamento") — DISTRIBUIÇÃO REAL,
   irreversível — e devolve nº do processo + chave de consulta.

O fluxo do BI é específico: o INSS já vem fixo como réu e o assistente tem 4
etapas. Cada etapa é reconhecida pelo TÍTULO da página ("Peticionamento
Eletrônico (N de M) - <nome>"); se a página não for a esperada (outra
pretensão, mudança no e-proc), a automação PARA com erro em vez de adivinhar.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import datetime
from pathlib import Path

import config
from ingestao import eproc_client as ec

_log = logging.getLogger("eproc.peticao_inicial")

# Acima disto, o upload vai 1 arquivo por vez (ver nota em eproc_client sobre o
# lote travar a transferência CDP com ~40MB+).
_LIMITE_LOTE_BYTES = 25 * 1024 * 1024

_ACAO_ETAPA4 = "processo_cadastrar_4"

# Mesmo `_JS_CLICAR_LINK_EXATO` do eproc_client, mas por textContent (o menu
# lateral pode estar recolhido — innerText de elemento oculto é vazio).
_JS_CLICAR_MENU = r"""(alvo)=>{
  // Ignora os nomes de ícone que o e-proc põe dentro do link (ex.: "Petição
  // Inicial arrow_drop_down" — menu virou suspenso, visto em 13/10/2026).
  const norm=s=>(s||'').replace(/\b[a-z]+(?:_[a-z]+)+\b/g,' ').replace(/\s+/g,' ').trim();
  const el=[...document.querySelectorAll('a')].find(a=>norm(a.textContent)===alvo);
  if(el){el.click(); return true;} return false;
}"""


def _n(s: str) -> str:
    return ec._normalizar_texto_autocompletar(s)


def _log_passo(passos: list, msg: str) -> None:
    _log.info(msg)
    passos.append({"hora": datetime.now().strftime("%H:%M:%S"), "msg": msg})


async def _titulo(page) -> str:
    with contextlib.suppress(Exception):
        return await page.title()
    return ""


async def _esperar_titulo(page, trecho: str, timeout_ms: int = 30000) -> bool:
    decorrido = 0
    while decorrido < timeout_ms:
        if trecho.lower() in (await _titulo(page)).lower():
            return True
        await page.wait_for_timeout(500)
        decorrido += 500
    return False


async def _selecionar_por_rotulo(page, seletor: str, rotulo: str) -> bool:
    """`<select>` comum: escolhe a opção cujo texto normalizado == rótulo (ou
    começa com ele) e dispara change. Retorna False se não houver."""
    valor = await page.evaluate(
        """([sel, alvo]) => {
            const norm = s => (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/\\s+/g,' ').trim().toLowerCase();
            const el = document.querySelector(sel); if (!el) return null;
            const a = norm(alvo);
            const o = [...el.options].find(o => norm(o.text) === a) || [...el.options].find(o => norm(o.text).startsWith(a));
            return o ? o.value : null;
        }""", [seletor, rotulo])
    if valor is None:
        return False
    await page.locator(seletor).select_option(value=valor)
    return True


async def _digitar(page, seletor: str, texto: str) -> None:
    """Digitação REAL (campos com máscara do e-proc — CPF, CEP, valor,
    telefone — só formatam com eventos de tecla)."""
    campo = page.locator(seletor)
    await campo.click()
    await campo.fill("")
    await campo.type(texto, delay=25)


async def _limpar_documentos_pendentes(page, passos: list) -> dict:
    """Sobras de uma preparação anterior abandonada ficam na tabela
    "Documentos selecionados e ainda não utilizados" e seriam juntadas de novo
    — remove todas (cada exclusão abre um confirm() nativo que precisa ser
    aceito)."""
    removidos = 0
    async with ec._com_dialogo_aceito(page):
        while removidos < 100:
            links = page.locator("a.btnExcluirDocumento")
            antes = await links.count()
            if antes == 0:
                break
            await links.first.click()
            for _ in range(20):
                await page.wait_for_timeout(400)
                if await page.locator("a.btnExcluirDocumento").count() < antes:
                    break
            else:
                return {"ok": False, "erro": "Não consegui remover documentos pendentes de uma preparação anterior."}
            removidos += 1
    if removidos:
        _log_passo(passos, f"{removidos} documento(s) pendente(s) de preparação anterior removido(s).")
    return {"ok": True}


async def _upload_e_tipos(page, documentos: list[dict], passos: list) -> str:
    caminhos = [Path(d["caminho_abs"]) for d in documentos]
    for c in caminhos:
        if not c.exists():
            return f"Arquivo não encontrado: {c}"
    total = sum(c.stat().st_size for c in caminhos)

    if total <= _LIMITE_LOTE_BYTES:
        await page.locator("input.qq-upload-input").first.set_input_files([str(c) for c in caminhos])
        decorrido = 0
        while decorrido < 240000:
            classes = await page.evaluate(
                "() => [...document.querySelectorAll('.qq-upload-list li')].map(li => li.className)")
            if any("qq-upload-fail" in c for c in classes):
                return "O e-proc rejeitou o envio de um dos arquivos."
            if len(classes) >= len(caminhos) and all("qq-upload-success" in c for c in classes):
                break
            await page.wait_for_timeout(500)
            decorrido += 500
        else:
            return "Tempo excedido enviando os arquivos ao e-proc."
        _log_passo(passos, f"{len(caminhos)} arquivo(s) enviados em lote ({total // 1024} KB).")
    else:
        for i, c in enumerate(caminhos, start=1):
            res = await ec._upload_documento(page, "", i, c)
            if not res.get("ok"):
                return res.get("erro") or f"Falha no envio de {c.name}."
            if i < len(caminhos):
                await page.locator("#lblAdicionarDocumento").click()
                await page.wait_for_timeout(700)
        _log_passo(passos, f"{len(caminhos)} arquivo(s) enviados um a um ({total // 1024 // 1024} MB).")

    for i, d in enumerate(documentos, start=1):
        if not await ec._selecionar_autocomplete(page, f"txtTipo_{i}", f"divInfraAjaxtxtTipo_{i}", d["tipo"]):
            return f'Tipo "{d["tipo"]}" não encontrado no catálogo do e-proc (documento {i}: {caminhos[i - 1].name}).'
        with contextlib.suppress(Exception):
            await page.locator(f"#selIdSigilo_{i}").select_option(value="0")
    # confere os ids ocultos (o autocomplete só "pega" com clique real)
    vazios = await page.evaluate(
        "(n) => [...Array(n)].map((_,i)=>i+1).filter(i => !(document.getElementById('selTipoArquivo_'+i)||{}).value)",
        len(documentos))
    if vazios:
        return f"Tipo não registrado pelo e-proc no(s) documento(s) {vazios}."
    _log_passo(passos, "Tipos de documento preenchidos.")

    await page.locator("#btnEnviarArquivos").click()
    for _ in range(60):
        if await page.locator("#selLocalidadeJudicial").is_visible():
            break
        await page.wait_for_timeout(500)
    else:
        return "Os campos de endereçamento não apareceram após \"Confirmar seleção de documentos\"."
    n_tabela = await page.evaluate(
        "() => [...document.querySelectorAll('table tr')].filter(tr => tr.cells.length > 5 && /\\.pdf/i.test(tr.innerText)).length")
    if n_tabela != len(documentos):
        return (f"A tabela de documentos do e-proc tem {n_tabela} arquivo(s), mas foram enviados {len(documentos)} "
                "(sobras de uma preparação anterior?) — remova os excedentes na janela do e-proc e prepare de novo.")
    _log_passo(passos, f"{n_tabela} documentos armazenados no e-proc.")
    return ""


async def _etapa1_enderecamento(page, d: dict, passos: list) -> str:
    if not await _selecionar_por_rotulo(page, "#selLocalidadeJudicial", d["localidade"]):
        return f'Localidade "{d["localidade"]}" não disponível.'
    await page.wait_for_timeout(2500)
    if not await _selecionar_por_rotulo(page, "#selRitoProcesso", d["rito"]):
        return f'Rito "{d["rito"]}" não disponível.'
    await page.wait_for_timeout(2500)
    if not await _selecionar_por_rotulo(page, "#selIdGrupoCompetencia", d.get("area") or "Previdenciária"):
        return "Área Previdenciária não disponível."
    await page.wait_for_timeout(2500)
    # Escolher a classe RECARREGA a página inteira (aviso do próprio e-proc).
    if not await _selecionar_por_rotulo(page, "#selIdClasseJudicial", d["classe"]):
        return f'Classe "{d["classe"]}" não disponível para esse rito/área.'
    await page.wait_for_timeout(1500)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
    for _ in range(40):
        ok = await page.evaluate("""(c) => {
            const el = document.getElementById('selIdClasseJudicial');
            const v = document.getElementById('txtValorCausa');
            return !!(el && v && el.selectedOptions[0] && el.selectedOptions[0].text.trim().toUpperCase() === c.toUpperCase());
        }""", d["classe"])
        if ok:
            break
        await page.wait_for_timeout(500)
    else:
        return "A classe não ficou selecionada após o recarregamento da página."
    with contextlib.suppress(Exception):
        await _selecionar_por_rotulo(page, "#selIdSigilo", d.get("sigilo") or "Sem Sigilo (Nível 0)")

    digitos = re.sub(r"\D", "", d["valor_causa"] or "")
    if not digitos:
        return "Valor da causa vazio."
    await _digitar(page, "#txtValorCausa", digitos)
    await page.keyboard.press("Tab")
    valor_tela = await page.locator("#txtValorCausa").input_value()
    if re.sub(r"\D", "", valor_tela) != digitos:
        return f'Valor da causa ficou "{valor_tela}" na tela (esperado {d["valor_causa"]}).'
    await page.locator("#chkNaoAplicaValor").set_checked(False)
    await page.locator("#chkRenunciaValor").set_checked(bool(d.get("renuncia_60sm")))
    _log_passo(passos, f'Endereçamento: {d["localidade"]} · {d["rito"]} · {d["classe"]} · R$ {valor_tela}'
                       + (" · renúncia > 60 SM" if d.get("renuncia_60sm") else ""))

    await page.locator("#btnSalvar:visible").last.click()
    if not await _esperar_titulo(page, "Assuntos"):
        return f'Não avançou para "Assuntos" (página atual: {await _titulo(page)}).'
    return ""


async def _etapa2_assunto(page, d: dict, passos: list) -> str:
    for _ in range(30):
        if await page.locator("#tblSugestaoAssuntos tr").count():
            break
        await page.wait_for_timeout(500)
    sugestoes = await page.evaluate(
        "() => [...document.querySelectorAll('#tblSugestaoAssuntos tr')].map(tr => tr.innerText.trim())")
    alvo = _n(d.get("assunto") or "")
    idx = next((i for i, s in enumerate(sugestoes) if _n(s).startswith(alvo)), -1)
    if idx < 0:
        return ("O assunto \"" + (d.get("assunto") or "") + "\" não está entre as sugestões da IA do e-proc: "
                + " | ".join(sugestoes[:5]) + ". Escolha o assunto manualmente.")
    linha = page.locator("#tblSugestaoAssuntos tr").nth(idx)
    await linha.locator("img[src*='transportar']").first.click()
    await page.wait_for_timeout(2500)
    _log_passo(passos, f'Assunto principal: {sugestoes[idx]} (sugestão nº {idx + 1} da IA do e-proc).')
    await page.locator("#sbmProcessoEtapa2:visible").first.click()
    if not await _esperar_titulo(page, "Partes"):
        return f'Não avançou para "Partes" (página atual: {await _titulo(page)}).'
    return ""


async def _botao_consultar(page, n: int):
    return page.locator('input[type=button][value="Consultar"]:visible').nth(n)


async def _cadastrar_pessoa(page, a: dict, passos: list) -> str:
    """Modal "Cadastro de Pessoa Física" (iframe #ifrSubFrm) — só o necessário:
    sexo, estado civil, raça/etnia, endereço (CEP + lupa + número/complemento
    → Incluir) e telefone (→ Incluir); depois Salvar."""
    fr = page.frame_locator("#ifrSubFrm")
    frame = next((f for f in page.frames if f.name == "ifrSubFrm" or "ifrSubFrm" in (f.name or "")), None)
    if frame is None:
        el = await page.query_selector("#ifrSubFrm")
        frame = await el.content_frame() if el else None
    if frame is None:
        return "Modal de cadastro de pessoa não encontrado."

    async def sel(sid, rotulo):
        return await _selecionar_por_rotulo(frame, sid, rotulo)

    sexo_atual = await frame.evaluate("() => (document.getElementById('selSexo')||{}).value || ''")
    if (not sexo_atual or sexo_atual in ("null", "0")) and a.get("sexo"):
        await sel("#selSexo", a["sexo"])
    if not await sel("#selEstCivil", a.get("estado_civil") or "Não Informado"):
        await sel("#selEstCivil", "Não Informado")
    with contextlib.suppress(Exception):
        await sel("#selTipoEtnia", "Não declarado")

    if a.get("cep"):
        await fr.locator("#txtCep").click()
        await fr.locator("#txtCep").fill("")
        await fr.locator("#txtCep").type(a["cep"], delay=25)
        await fr.locator("a#txtImage").click()
        await page.wait_for_timeout(3000)
        if a.get("numero"):
            await fr.locator("#txtEndNum").fill(a["numero"])
        if a.get("complemento"):
            await fr.locator("#txtEndComp").fill(a["complemento"])
        await fr.locator("#btnIncEnd").click()
        await page.wait_for_timeout(1500)
    if a.get("telefone"):
        await sel("#selTipoCont", "Telefone")
        await fr.locator("#txtContato").click()
        await fr.locator("#txtContato").type(_formatar_tel(a["telefone"]), delay=20)
        await fr.locator("#btnIncCont").click()
        await page.wait_for_timeout(1500)

    await fr.locator("button:has-text('Salvar'), input[value='Salvar']").first.click()
    for _ in range(30):
        await page.wait_for_timeout(500)
        if not await page.locator("#ifrSubFrm").is_visible():
            break
    else:
        return "O cadastro de pessoa física não foi salvo (modal continua aberto) — confira na janela do e-proc."
    _log_passo(passos, "Autor(a) cadastrado(a) no e-proc (CPF não constava da base).")
    return ""


def _formatar_tel(d: str) -> str:
    d = re.sub(r"\D", "", d or "")
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return d


async def _etapa3_partes(page, d: dict, passos: list) -> str:
    if not await page.locator("#txtNumeroBeneficio").count():
        return ("A página de Partes não é a de benefício por incapacidade (sem campo NB) — "
                "este botão só cobre o fluxo de BI por enquanto.")
    a = d["autor"]
    dialogos: list[str] = []
    ouvinte = lambda dlg: dialogos.append(dlg.message)  # noqa: E731 — só registra; quem dispensa é o hook único
    page.on("dialog", ouvinte)
    try:
        await _digitar(page, "#txtAutorCpf", a["cpf"])
        await (await _botao_consultar(page, 0)).click()
        await page.wait_for_timeout(4000)
        if await page.locator("#ifrSubFrm").is_visible():
            erro = await _cadastrar_pessoa(page, a, passos)
            if erro:
                return erro
            await (await _botao_consultar(page, 0)).click()
            await page.wait_for_timeout(3000)
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("dialog", ouvinte)
    nome = await page.locator("#txtAutorNome").input_value()
    if not nome.strip():
        return "O e-proc não reconheceu o CPF do(a) autor(a)" + (f" (aviso: {'; '.join(dialogos)})" if dialogos else "") + "."
    _log_passo(passos, f"Autor(a) reconhecido(a): {nome}.")

    await _digitar(page, "#txtAutorCep", a["cep"])
    await (await _botao_consultar(page, 1)).click()
    await page.wait_for_timeout(2500)

    if a.get("telefone"):
        await _digitar(page, "#txtAutorTelefone", a["telefone"])
    if a.get("email"):
        await page.locator("#txtAutorEmail").fill(a["email"])
    for cid in ("chkAutorTelefoneWhatsapp", "chkContatoAlternativo", "chkAdvogadoTelefoneWhatsapp"):
        with contextlib.suppress(Exception):
            await page.locator(f"#{cid}").set_checked(False)
    # Radios DEPOIS do Consultar (ele reseta justiça gratuita/representante).
    for rid in ("rdoJusticaGratuitaSim", "rdoPossuiRepresentanteNao", "rdoAutorInternacaoHospNao",
                "rdoAutorPresoNao", "rdoRetencaoHonorarioContratualNao"):
        await page.locator(f"#{rid}").check()

    await _digitar(page, "#txtNumeroBeneficio", d["nb"])

    escolha = await page.evaluate(r"""(alvos) => {
        const norm = s => (s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').trim().toUpperCase();
        const el = document.getElementById('selEspecialidade');
        const cands = [...el.options].filter(o => o.value && alvos.some(a => norm(o.text).startsWith(norm(a))));
        if (!cands.length) return null;
        const data = o => { const m = /(\d{2})\/(\d{2})\/(\d{4})/.exec(o.text); return m ? new Date(+m[3], +m[2]-1, +m[1]).getTime() : Infinity; };
        cands.sort((x, y) => data(x) - data(y));
        const o = cands[0];
        el.value = o.value;
        if (window.jQuery && jQuery(el).selectpicker) jQuery(el).selectpicker('refresh');
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return o.text;
    }""", d.get("especialidades") or [])
    if not escolha:
        return "Especialidade da perícia não encontrada entre as opções do e-proc: " + ", ".join(d.get("especialidades") or ["(nenhuma informada)"]) + "."

    await page.evaluate("""() => {
        const el = document.getElementById('selProcuracaoPoderes'); if (!el) return;
        [...el.options].forEach(o => o.selected = true);
        if (window.jQuery && jQuery(el).selectpicker) jQuery(el).selectpicker('refresh');
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }""")

    conf = await page.evaluate("""() => {
        const g = id => document.getElementById(id);
        return {jg: g('rdoJusticaGratuitaSim').checked, nb: g('txtNumeroBeneficio').value,
                loc: g('txtAutorLocalidade').value, tel: g('txtAutorTelefone').value,
                pod: [...g('selProcuracaoPoderes').selectedOptions].length};
    }""")
    if not conf["jg"] or re.sub(r"\D", "", conf["nb"]) != d["nb"]:
        return "Conferência da página de Partes falhou (justiça gratuita/NB não ficaram como esperado)."
    _log_passo(passos, f'Partes: CEP → {conf["loc"]} · tel {conf["tel"]} · NB {conf["nb"]} · {escolha} · '
                       f'{conf["pod"]} poder(es) da procuração · justiça gratuita Sim.')

    await page.locator("button:visible", has_text="Próxima").last.click()
    if not await _esperar_titulo(page, "Informações Adicionais"):
        return f'Não avançou para "Informações Adicionais" (página atual: {await _titulo(page)}).'
    return ""


_CHECKBOXES_ETAPA4 = ["chkDoencaGrave", "chkCautAntecTutela", "chkIntervMinisterioPublico", "chkDeficiente",
                      "chkCrianca", "chkPessoaComTEA", "chkPessoaLei14289", "chkJuizoDigital",
                      "chkDesinteresseTA", "chkAtendeReqLei8213_91", "chkDemandaComplexa"]


async def _etapa4_informacoes(page, d: dict, passos: list) -> str:
    for cid in _CHECKBOXES_ETAPA4:
        loc = page.locator(f"#{cid}")
        if not await loc.count() or not await loc.is_enabled():
            continue
        marcar = (cid == "chkAtendeReqLei8213_91") or (cid == "chkCautAntecTutela" and bool(d.get("tutela")))
        await loc.set_checked(marcar)
    _log_passo(passos, "Informações adicionais: art. 129-A mantido"
                       + (" · Liminar/Antecipação de Tutela marcada" if d.get("tutela") else "") + ".")
    return ""


def _abas_etapa4(ctx) -> list:
    return [p for p in ctx.pages if _ACAO_ETAPA4 in (p.url or "")]


async def preparar(plano: dict) -> dict:
    """`plano` = {"documentos": [{"caminho_abs", "tipo"}...], "dados": {...}}
    (ver services.peticao_inicial_service.montar_plano + ajustes do modal).
    Para na etapa "Informações Adicionais", sem Finalizar."""
    passos: list = []
    async with ec._browser_lock:
        browser, pagina_ref = await ec._connect()
        ctx = pagina_ref.context
        # no máximo 1 preparação aberta (evita reencontrar a aba errada no ajuizar)
        for p in _abas_etapa4(ctx):
            with contextlib.suppress(Exception):
                await p.close()
        page = await ctx.new_page()
        try:
            # Painel do Procurador pelo link assinado (o menu completo, com
            # "Petição Inicial", não está na tela de erro de `controlador.php`
            # puro); sessão caída = entra sozinho (credenciais do MCP do e-proc).
            await ec._ir_para_inicio(page, 1000)
            if not ec._status_dict(page.url)["logado"] and await ec._garantir_login(page):
                await ec._ir_para_inicio(page, 1000)
            if not ec._status_dict(page.url)["logado"]:
                return {"ok": False, "passos": passos,
                        "erro": "Sessão do e-proc não está logada — clique no selo do e-proc (topo) para entrar e tente de novo."}
            if not await page.evaluate(_JS_CLICAR_MENU, "Petição Inicial"):
                return {"ok": False, "passos": passos, "erro": 'Menu "Petição Inicial" não encontrado no e-proc.'}
            if not await _esperar_titulo(page, "Informações do processo"):
                return {"ok": False, "passos": passos, "erro": f"Tela inicial do peticionamento não abriu ({await _titulo(page)})."}
            _log_passo(passos, "Tela \"Petição Inicial\" aberta.")

            ia = page.locator("#btnPredicao")
            if await ia.count() and not await ia.is_checked():
                await ia.check()
            _log_passo(passos, "Apoio por Inteligência Artificial ligado.")

            res = await _limpar_documentos_pendentes(page, passos)
            if not res["ok"]:
                return {"ok": False, "passos": passos, "erro": res["erro"]}

            d = plano["dados"]
            for etapa in (lambda: _upload_e_tipos(page, plano["documentos"], passos),
                          lambda: _etapa1_enderecamento(page, d, passos),
                          lambda: _etapa2_assunto(page, d, passos),
                          lambda: _etapa3_partes(page, d, passos),
                          lambda: _etapa4_informacoes(page, d, passos)):
                erro = await etapa()
                if erro:
                    return {"ok": False, "passos": passos, "erro": erro, "etapa": await _titulo(page)}

            with contextlib.suppress(Exception):
                await page.bring_to_front()
                await ec._maximizar_janela(page)
            _log_passo(passos, "Petição preparada para conferência — parada antes de \"Finalizar\".")
            return {"ok": True, "passos": passos, "etapa": await _titulo(page)}
        except Exception as e:  # noqa: BLE001
            _log.exception("preparar petição inicial")
            return {"ok": False, "passos": passos, "erro": f"{type(e).__name__}: {e}"[:400],
                    "etapa": await _titulo(page)}
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def ajuizar() -> dict:
    """Finalizar + "Confirmar ajuizamento" na aba preparada — DISTRIBUI o
    processo (irreversível). Devolve nº/chave/classe/magistrado."""
    async with ec._browser_lock:
        browser, pagina_ref = await ec._connect()
        try:
            abas = _abas_etapa4(pagina_ref.context)
            if not abas:
                return {"ok": False, "erro": "Aba da petição preparada não encontrada — prepare de novo."}
            if len(abas) > 1:
                return {"ok": False, "erro": f"{len(abas)} abas de petição inicial abertas — feche as extras e prepare de novo."}
            page = abas[0]
            await page.bring_to_front()
            await page.locator("#btnSalvar:visible").first.click()

            frame_conf = None
            for _ in range(40):
                await page.wait_for_timeout(500)
                for f in page.frames:
                    with contextlib.suppress(Exception):
                        if await f.locator("#sbmConfirmar").count():
                            frame_conf = f
                            break
                if frame_conf:
                    break
            if not frame_conf:
                return {"ok": False, "erro": "O modal de conferência não abriu após \"Finalizar\" — confira a janela do e-proc."}

            async with ec._com_dialogo_aceito(page):
                await frame_conf.locator("#sbmConfirmar").click()
                for _ in range(120):
                    await page.wait_for_timeout(500)
                    if "processo_finalizado" in (page.url or ""):
                        break
            if "processo_finalizado" not in (page.url or ""):
                return {"ok": False, "erro": "Não chegou à tela \"Processo distribuído\" — NÃO repita sem conferir no e-proc se o processo foi distribuído.",
                        "url": page.url}
            await page.wait_for_timeout(800)
            dados = await page.evaluate("""() => {
                const t = id => ((document.getElementById(id)||{}).innerText||'').trim();
                const txt = document.body.innerText;
                const pega = (rot) => { const m = new RegExp(rot + '\\\\s*\\\\n\\\\s*([^\\\\n]+)').exec(txt); return m ? m[1].trim() : ''; };
                return {numero: t('lblDesNumProcesso'), chave: t('lblDesChaveConsulta'),
                        classe: pega('Classe'), magistrado: pega('Magistrado')};
            }""")
            if not dados.get("numero"):
                return {"ok": False, "erro": "Processo distribuído, mas não consegui ler o número na tela — copie do e-proc.", **dados}
            return {"ok": True, **dados}
        except Exception as e:  # noqa: BLE001
            _log.exception("ajuizar petição inicial")
            return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:400] + " — confira no e-proc se o processo foi distribuído antes de repetir."}
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


def resolver_documentos(paj_norm: str, arquivos: list[dict]) -> list[dict]:
    """[{caminho: "Ajuizamento/x.pdf", tipo}] → com caminho absoluto, restrito
    à pasta do PAJ."""
    base = (Path(config.PAJS_DIR) / paj_norm).resolve()
    docs = []
    for a in arquivos:
        p = (base / a["caminho"]).resolve()
        if base not in p.parents:
            raise ValueError(f"Caminho fora do PAJ: {a['caminho']}")
        docs.append({"caminho_abs": str(p), "tipo": a["tipo"]})
    return docs
