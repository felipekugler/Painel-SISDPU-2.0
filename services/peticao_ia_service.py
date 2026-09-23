"""Geração de petição via IA (Claude Code CLI, chamada única/"one-shot") pro
módulo "Ações no Eproc" — botão "Gerar petição com IA" acima da caixa de
texto da petição.

Fluxo:
  1) Recebe o(s) `prazo_id` selecionado(s) na tela ("Selecione o(s) prazo(s)
     a ser(em) fechado(s)") — são o universo do que a petição deve responder.
  2) Resolve cada prazo pro registro cru (`acoes_eproc_service.
     resolver_prazo_por_id`) e casa contra a triagem MCP do processo (mesmo
     critério de "mesmo dia de envio + mesmo nº de dias" já usado em
     `static/js/processo_judicial.js`) pra achar o `evento_referenciado` —
     o ATO que gerou a intimação (o que a petição precisa responder).
  3) Baixa o texto integral desse(s) ato(s) referenciado(s) (MCP, mesma
     função que a página "Consultar processo" usa).
  4) Monta um prompt e chama o Claude Code CLI em modo não-interativo (`-p`),
     instruindo-o a escrever/executar um script Python usando
     `peticao_base.py` (biblioteca de formatação DPU) e salvar o `.docx`.
  5) Converte o `.docx` gerado pra PDF (Word/COM — e-proc não aceita .docx
     como anexo). O docx+pdf ficam salvos na subpasta "Peças IA" do PAJ (não
     na raiz — pedido do usuário 2026-09-22: essa é a mesma pasta que
     "Adicionar Petições IA" já lê em Ações no Eproc E em Ações no SISDPU,
     então a petição gerada aqui já sai disponível pra juntar/registrar
     TAMBÉM no SISDPU). O PDF é ainda COPIADO pra pasta do RASCUNHO do PAJ
     (mesma pasta usada por "Adicionar Externo" — ver `acoes_eproc_service`),
     pra virar anexo da PETIÇÃO NO E-PROC via a MESMA rota que já serve
     arquivos de rascunho — os dois usos (Peças IA e rascunho) coexistem.

Estado em memória (`_status`), 1 geração por PAJ por vez — reinicia se chamado
de novo enquanto uma está rodando não é permitido (ver `iniciar`).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from services import acoes_eproc_service
from services.chat_service import CLAUDE_CMD

MODELOS_DIR = Path(r"C:\Claude\DPU\Modelos")
MODELO_EXEMPLO_DOCX = MODELOS_DIR / "Petição_MODELO_converted.docx"

# Regras de formatação/estilo EXATAS pedidas pelo usuário (verbatim) — além das
# já embutidas em peticao_base.py (que o prompt também instrui a seguir).
_REGRAS_FORMATO = """\
#FORMATO DA RESPOSTA
Petição jurídica, concisa e objetiva, em texto corrido.
Os parágrafos devem ser curtos, sem excesso de informações.

#ESTILO DE ESCRITA
Não utilize pessoalização no texto. Evite citar nomes de juízes, advogados, \
auxiliares do juízo como peritos e assistentes sociais, ou quaisquer atores \
do processo. Opte por citar sempre de maneira impessoal "o juízo decidiu \
que"; "a perícia concluiu..."; "o laudo pericial..."; "a avaliação social \
indicou que..." etc.
Ao criar parágrafos, depois de citar algum ato realizado no processo, \
prefira utilizar sujeito oculto nas sentenças seguintes para evitar \
repetições. Dessa forma, a escrita deverá ficar mais profissional e fluida.
Se for utilizar marcadores, adote o padrão numérico (1., 2., 3., etc.) com \
subtópicos (1.1, 1.2, 2.1, 2.2) e alíneas (a, b, c). Não utilize marcadores \
com bullets (•).
Não utilize travessões.
Não utilize faixas, linhas, separadores ou qualquer elemento html entre \
tópicos.
Não foque em detalhes irrelevantes para o caso (como dosagens de \
medicamentos, p.ex.), a não ser que sejam relevantes para explicar os \
fundamentos.

#FLUIDEZ E COESÃO TEXTUAL
Elaborar o texto de forma coesa e fluida, evitando estruturas rígidas ou \
mecânicas. Utilizar conectivos adequados para criar transições naturais \
entre os parágrafos.
Evitar repetições desnecessárias e manter o texto enxuto, preservando \
apenas as informações essenciais para a fundamentação do pedido.
"""

# paj_norm -> {status: idle|running|done|error, erro, arquivo, tamanho, iniciado_em}
_status: dict[str, dict] = {}
_lock = threading.Lock()


def _soData(s: str) -> str:
    m = re.search(r"(\d{2}/\d{2}/\d{4})", s or "")
    return m.group(1) if m else ""


def status(paj_norm: str) -> dict:
    return _status.get(paj_norm, {"status": "idle"})


def _nome_peticao_hoje() -> str:
    """Nome do arquivo (sem extensão): "Petição-DD-MM-AAAA" — mesmo nome pras
    duas vias (IA e "Incluir petição nos anexos") no mesmo dia, o que faz a
    segunda ATUALIZAR o mesmo arquivo/anexo em vez de criar um novo; num dia
    diferente, vira um arquivo novo (a data marca a versão)."""
    return f"Petição-{datetime.now().strftime('%d-%m-%Y')}"


def iniciar(paj_norm: str, processo: str, prazo_ids: list[str]) -> dict:
    """Valida e dispara a geração em background. Retorna {ok, status, erro?}."""
    prazo_ids = [p for p in (prazo_ids or []) if p]
    if not prazo_ids:
        return {"ok": False, "erro": "Selecione ao menos um prazo antes de gerar a petição com IA."}

    atual = _status.get(paj_norm)
    if atual and atual.get("status") == "running":
        return {"ok": True, "status": "running"}

    with _lock:
        _status[paj_norm] = {"status": "running", "erro": "", "iniciado_em": datetime.now().isoformat(timespec="seconds")}

    t = threading.Thread(target=_gerar_worker, args=(paj_norm, processo, prazo_ids), daemon=True)
    t.start()
    return {"ok": True, "status": "running"}


def _gerar_worker(paj_norm: str, processo: str, prazo_ids: list[str]) -> None:
    from config import PAJS_DIR

    try:
        atos = _coletar_atos_referenciados(prazo_ids)
        if not atos:
            _falhar(paj_norm, "Não foi possível identificar o(s) prazo(s) selecionado(s) "
                               "(cache desatualizado?). Sincronize a aba Prazos e tente de novo.")
            return

        # Gera na subpasta "Peças IA" do PAJ (docx E pdf ficam guardados ali —
        # pedido do usuário 2026-09-22: essa é a MESMA pasta que "Adicionar
        # Petições IA" já lê tanto em Ações no Eproc quanto em Ações no SISDPU
        # — ver acoes_eproc_service.listar_pecas_ia/acoes_sisdpu_service.
        # listar_pecas_ia —, então a petição gerada aqui passa a ficar
        # disponível pra juntar/registrar TAMBÉM no SISDPU, sem precisar
        # reanexar manualmente algo que já foi redigido aqui). O PDF depois é
        # COPIADO (não movido) pra pasta do rascunho, só pra poder ser servido
        # como anexo da petição (e-proc não aceita .docx).
        pasta_paj = PAJS_DIR / paj_norm / "Peças IA"
        pasta_paj.mkdir(parents=True, exist_ok=True)
        nome = _nome_peticao_hoje()
        docx_path = pasta_paj / f"{nome}.docx"
        pdf_path = pasta_paj / f"{nome}.pdf"
        with contextlib.suppress(Exception):
            if docx_path.exists():
                docx_path.unlink()
            if pdf_path.exists():
                pdf_path.unlink()

        prompt = _montar_prompt(paj_norm, atos, docx_path)

        # Prompt vai por STDIN, não como argumento de linha de comando: o
        # texto do ato referenciado facilmente passa de alguns KB, e o Windows
        # (cmd.exe, usado pra invocar o claude.CMD) tem um limite de ~8191
        # caracteres por linha de comando ("Linha de comando muito longa").
        cmd = [*CLAUDE_CMD, "-p", "--permission-mode", "bypassPermissions"]
        proc = subprocess.run(
            cmd, cwd=str(MODELOS_DIR), input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        if not docx_path.exists():
            saida = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            _falhar(paj_norm, f"A IA não gerou o arquivo .docx esperado. Saída: {saida[-1200:] or '(vazia)'}")
            return

        _docx_para_pdf(docx_path, pdf_path)
        if not pdf_path.exists():
            _falhar(paj_norm, "A petição foi redigida (.docx), mas a conversão pra PDF falhou "
                               "(verifique se o Microsoft Word está instalado nesta máquina).")
            return

        peticao_html = ""
        with contextlib.suppress(Exception):
            peticao_html = _docx_para_html(docx_path)

        pasta_rascunho = acoes_eproc_service._rascunho_arquivos_dir(paj_norm)
        pasta_rascunho.mkdir(parents=True, exist_ok=True)
        pdf_rascunho = pasta_rascunho / f"{nome}.pdf"
        shutil.copyfile(pdf_path, pdf_rascunho)

        with _lock:
            _status[paj_norm] = {
                "status": "done", "erro": "", "arquivo": f"{nome}.pdf",
                "tamanho": pdf_rascunho.stat().st_size, "peticao_html": peticao_html,
                "concluido_em": datetime.now().isoformat(timespec="seconds"),
            }
    except subprocess.TimeoutExpired:
        _falhar(paj_norm, "A IA excedeu o tempo limite (10 min) redigindo a petição.")
    except Exception as e:  # noqa: BLE001
        _falhar(paj_norm, f"{type(e).__name__}: {e}"[:500])


def _docx_para_html(docx_path: Path) -> str:
    """Extrai o texto do .docx gerado em HTML simples (parágrafos + negrito/
    itálico/sublinhado por run) — pra popular a caixa de texto da seção
    "Petição" (Quill aceita via `setHTML`/`dangerouslyPasteHTML`). Não tenta
    replicar a formatação visual completa do Word (recuo, alinhamento) — só o
    texto corrido, que é o que interessa nessa caixa."""
    import html as _html

    import docx as _docx

    doc = _docx.Document(str(docx_path))
    partes = []
    for p in doc.paragraphs:
        texto_runs = []
        for run in p.runs:
            txt = _html.escape(run.text)
            if not txt:
                continue
            if run.bold:
                txt = f"<strong>{txt}</strong>"
            if run.italic:
                txt = f"<em>{txt}</em>"
            if run.underline:
                txt = f"<u>{txt}</u>"
            texto_runs.append(txt)
        linha = "".join(texto_runs).strip()
        if linha:
            partes.append(f"<p>{linha}</p>")
    return "\n".join(partes)


def _falhar(paj_norm: str, erro: str) -> None:
    with _lock:
        _status[paj_norm] = {"status": "error", "erro": erro}


def _coletar_atos_referenciados(prazo_ids: list[str]) -> list[dict]:
    """Wrapper síncrono — roda tudo numa ÚNICA sessão de event loop (ver
    `_coletar_async`). Duas chamadas `asyncio.run()` separadas (triagem +
    download) fazem o cliente HTTP async (curl_cffi) perder estado entre elas
    — o download silenciosamente não vinha (texto vazio, sem erro)."""
    import asyncio

    return asyncio.run(_coletar_async(prazo_ids))


async def _coletar_async(prazo_ids: list[str]) -> list[dict]:
    """Pra cada prazo selecionado: resolve o registro cru, casa contra a
    triagem MCP do processo (mesmo critério dia+dias de
    `processo_judicial.js`) e baixa o texto do evento referenciado. Agrupa
    por processo pra só consultar a triagem UMA VEZ por processo distinto
    (normal ter só 1, mas o PAJ pode ter mais de um vinculado)."""
    from ingestao import eproc_mcp_client as mcp

    resolvidos = []
    for pid in prazo_ids:
        r = acoes_eproc_service.resolver_prazo_por_id(pid)
        if r:
            resolvidos.append(r)
    if not resolvidos:
        return []

    por_processo: dict[str, list[dict]] = {}
    for r in resolvidos:
        num = re.sub(r"\D", "", r["caso"].get("processo", ""))
        por_processo.setdefault(num, []).append(r)

    atos: list[dict] = []
    vistos_evento: set[str] = set()
    for numero, itens in por_processo.items():
        triagem = await mcp.triagem_processo(numero, instancia=1)
        intimacoes = [it for it in (triagem.get("intimacoes") or [])]
        usadas: set[int] = set()

        for item in itens:
            prazo, caso = item["prazo"], item["caso"]
            dia_p = _soData(prazo.get("data_envio", ""))
            try:
                dias_p = int(prazo.get("prazo_dias") or -1)
            except ValueError:
                dias_p = -1
            match_idx = -1
            for i, it in enumerate(intimacoes):
                if i in usadas:
                    continue
                # data_hora = expedição da PRÓPRIA intimação (casa com o "data_envio" do
                # Painel do Procurador) — NÃO enviado_em (sub-dado "Domicílio Judicial
                # Eletrônico: Enviado em...", momento distinto da entrega eletrônica,
                # sempre um pouco posterior à expedição do evento).
                dia_it = _soData(it.get("data_hora", ""))
                if not dia_p or not dia_it or dia_p != dia_it:
                    continue
                try:
                    dias_it = int(it.get("prazo_dias") or -1)
                except ValueError:
                    dias_it = -1
                if dias_p != -1 and dias_it != -1 and dias_p != dias_it:
                    continue
                match_idx = i
                break

            ref = None
            if match_idx != -1:
                usadas.add(match_idx)
                ref = intimacoes[match_idx].get("evento_referenciado")

            if ref and ref.get("numero") and ref["numero"] not in vistos_evento:
                vistos_evento.add(ref["numero"])
                texto = await _baixar_texto_evento(numero, ref["numero"])
                atos.append({
                    "processo": caso.get("processo", ""), "assunto": caso.get("assunto", ""),
                    "partes": caso.get("partes", ""),
                    "evento_numero": ref["numero"], "descricao": ref.get("descricao", ""),
                    "data_hora": ref.get("data_hora", ""), "texto": texto,
                })
            elif not ref:
                # Sem correspondência na triagem (fora da janela de eventos) —
                # ainda assim registra o que o Painel sabe, sem texto integral.
                atos.append({
                    "processo": caso.get("processo", ""), "assunto": caso.get("assunto", ""),
                    "partes": caso.get("partes", ""),
                    "evento_numero": "", "descricao": prazo.get("evento", ""),
                    "data_hora": prazo.get("data_envio", ""), "texto": "",
                })
    return atos


async def _baixar_texto_evento(numero: str, evento_numero: str) -> str:
    from ingestao import eproc_mcp_client as mcp

    res = await mcp.baixar_documentos_evento(numero, 1, [evento_numero])
    partes = []
    for item in res.get("baixados") or []:
        caminho = item.get("arquivo")
        if caminho and Path(caminho).exists():
            with contextlib.suppress(Exception):
                partes.append(Path(caminho).read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(partes)


def _contexto_paj(paj_norm: str) -> dict:
    """Lê metadata.json do PAJ: qualificação básica do assistido (nome + CPF)
    e as últimas 10 movimentações (SISDPU) em ordem cronológica — dá à IA o
    pano de fundo procedimental recente do PAJ, pra interpretar o ato
    referenciado dentro do contexto real do caso (não só o texto isolado do
    ato), em vez de só o nome do assistido."""
    from config import PAJS_DIR
    from services.paj_service import _strip_html

    meta = {}
    with contextlib.suppress(Exception):
        meta = json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))

    sisdpu_raw = meta.get("sisdpu_raw") or {}
    assistido = meta.get("assistido_caixa") or meta.get("assistido") or ""
    cpf = sisdpu_raw.get("cpf_assistido", "")
    processo = _numero_processo_unico(meta)

    movs_raw = (meta.get("detalhes_sisdpu") or {}).get("movimentacoes") or sisdpu_raw.get("movimentacoes") or []
    movs_ordenadas = sorted(movs_raw, key=lambda m: m.get("seq", 0))
    ultimas = movs_ordenadas[-10:]
    movimentacoes = [{
        "data": m.get("data_original") or m.get("data") or "",
        "categoria": m.get("movimentacao", ""),
        "descricao": _strip_html(m.get("descricao", ""))[:500],
    } for m in ultimas]

    return {"assistido": assistido, "cpf": cpf, "processo": processo, "movimentacoes": movimentacoes}


def _numero_processo_unico(meta: dict) -> str:
    """Número do processo judicial vinculado ao PAJ — só quando há EXATAMENTE
    UM (considera a lista `processos_judiciais` e o campo legado singular
    `processo_judicial`, mesma informação em formato mais antigo do migrador
    do SISDPU, normalizando pra dígitos antes de comparar pra não contar duas
    vezes o mesmo processo grafado com/sem pontuação). PAJ sem processo
    vinculado, ou com MAIS DE UM, devolve vazio de propósito — pedido do
    usuário 2026-09-16: "[PROCESSO] deve permanecer; nos casos de mais de um
    processo, pode trazer vazio" (não escolhe um arbitrariamente entre vários,
    mesmo risco de ambiguidade que já tinha derrubado o [LOCAL])."""
    candidatos: dict[str, str] = {}  # dígitos -> forma original (c/ pontuação)
    for p in (meta.get("processos_judiciais") or []):
        bruto = str(p.get("processo_judicial") or "").strip()
        digitos = re.sub(r"\D", "", bruto)
        if digitos:
            candidatos.setdefault(digitos, bruto)
    bruto_legado = str(meta.get("processo_judicial") or "").strip()
    digitos_legado = re.sub(r"\D", "", bruto_legado)
    if digitos_legado:
        candidatos.setdefault(digitos_legado, bruto_legado)
    if len(candidatos) != 1:
        return ""
    return next(iter(candidatos.values()))


def obter_qualificacao_principal(paj_norm: str) -> dict:
    """Nome + CPF do assistido PRINCIPAL do PAJ (ausência do ícone "Excluir" na
    tabela do SISDPU — já resolvido na própria sincronização, ver
    ingestao/sisdpu_client.py: assistidos_pf[].principal / cpf_assistido) + o
    número do processo judicial, SE houver exatamente um vinculado (ver
    `_numero_processo_unico` — vazio se não houver nenhum ou houver mais de
    um, de propósito). Reaproveita `_contexto_paj` — usado pelos placeholders
    [NOME]/[CPF]/[PROCESSO] dos "Modelos de petição" (Ações no Eproc) e
    "Despachos padrão" (Ações no SISDPU) — ver routes/acoes_eproc.py;
    [DATA] resolve no próprio front, não depende do PAJ.

    NÃO tem mais um [LOCAL] (removido 2026-09-16): `metadata.foro_detectado`
    (que alimentava esse placeholder) nunca foi um local geográfico de
    verdade — é a ÁREA TEMÁTICA da pretensão (ex.: "Previdenciário"), vinda
    de `derivar_foro()` em ingestao/parser.py ("primeira palavra da
    pretensão normalizada"). Bug real relatado pelo usuário: [LOCAL] vinha
    "Previdenciário" em vez do município de atuação ("Curitiba"). Não há
    hoje nenhum campo de cidade/comarca estruturado no painel pra alimentar
    isso corretamente (o único lugar com o nome da cidade é o texto livre
    `OFICIO_DESCRICAO` do .env) — decisão do usuário: remover o placeholder
    em vez de expor um dado errado ou de confiabilidade duvidosa. [PROCESSO]
    é diferente: quando ambíguo (0 ou 2+ processos), some vazio (sem quebrar
    nada, o usuário completa à mão) em vez de escolher um dado ERRADO — por
    isso o usuário pediu pra mantê-lo, ao contrário do [LOCAL] (que sempre
    vinha errado, nunca vazio).

    Endereço NÃO é resolvido aqui — a sincronização baixa endereços por
    assistido PF mas sem religar ao `idPessoa`/principal (ver
    ingestao/sisdpu_client._extrair_contatos_pf), então não há hoje um
    endereço confiável do principal pra expor como placeholder."""
    ctx = _contexto_paj(paj_norm)
    return {"nome": ctx["assistido"], "cpf": ctx["cpf"], "processo": ctx["processo"]}


def _montar_prompt(paj_norm: str, atos: list[dict], docx_destino: Path) -> str:
    ctx = _contexto_paj(paj_norm)

    blocos_ato = []
    for i, ato in enumerate(atos, start=1):
        blocos_ato.append(
            f"### Ato referenciado {i} (Evento {ato['evento_numero'] or '?'}, {ato['data_hora']})\n"
            f"Descrição do sistema: {ato['descricao'] or '(não informada)'}\n\n"
            + (f"Texto integral:\n```\n{ato['texto']}\n```\n" if ato["texto"]
               else "(Texto integral não pôde ser obtido automaticamente — trabalhe só com a "
                    "descrição acima e sinalize essa limitação no resumo, se houver.)\n")
        )

    processo = atos[0]["processo"] if atos else ""
    assunto = atos[0]["assunto"] if atos else ""
    partes = atos[0]["partes"] if atos else ""

    if ctx["movimentacoes"]:
        linhas_mov = "\n".join(
            f"- [{m['data']}] ({m['categoria']}) {m['descricao'] or '(sem descrição)'}"
            for m in ctx["movimentacoes"]
        )
        bloco_mov = (
            f"### Últimas movimentações do PAJ (mais recente por último, mais antiga primeiro)\n"
            f"Contexto procedimental recente — use pra entender o ESCOPO do que está sendo pedido "
            f"no ato referenciado (ex.: se já há decisão/encaminhamento tratando do mesmo assunto, "
            f"documento já juntado, etc.), não pra copiar literalmente:\n{linhas_mov}\n"
        )
    else:
        bloco_mov = "### Últimas movimentações do PAJ\n(Não disponíveis.)\n"

    return (
        f"Redija uma petição respondendo ao(s) ato(s) processual(is) abaixo.\n\n"
        "## Dados para qualificação, epígrafe e endereçamento\n"
        f"- Assistido(a): {ctx['assistido'] or '(ver processo)'}"
        + (f", CPF {ctx['cpf']}" if ctx["cpf"] else "") + "\n"
        f"- Processo judicial: {processo} — partes: {partes}\n"
        f"- Assunto: {assunto or '(não informado)'}\n"
        f"- PAJ (referência interna DPU, NÃO aparece pro juízo — só controle): {paj_norm}\n\n"
        + bloco_mov
        + "\n"
        + "\n".join(blocos_ato)
        + "\n---\n\n"
        + _REGRAS_FORMATO
        + "\n---\n\n"
        "## Como gerar o arquivo\n"
        f"Use a biblioteca `peticao_base.py` (já está em `{MODELOS_DIR}` — rode a partir dessa "
        "pasta, ou faça `sys.path.insert(0, r'" + str(MODELOS_DIR) + "')` antes do import). Ela "
        "já traz as REGRAS DE ESTILO obrigatórias da DPU no próprio docstring do módulo — leia-o "
        "primeiro (`import peticao_base; help(peticao_base)` ou abra o arquivo) e siga-as "
        "TAMBÉM, além das regras acima.\n\n"
        "Estrutura ESPERADA, NESTA ORDEM (adapte o conteúdo, mas siga a sequência de chamadas):\n"
        "1. `novo_documento()` para abrir o doc.\n"
        "2. `enderecamento(doc, texto_endereco, num_processo)` — ANTES da qualificação. "
        "`texto_endereco` é algo como 'EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A) FEDERAL' (ou o cargo "
        "cabível pela instância/classe do processo); `num_processo` é o número do processo "
        "informado acima. Esta função JÁ cuida de tudo sozinha: parágrafo em negrito/maiúsculo/"
        "justificado/sem recuo, os espaços em branco até a qualificação, e a linha 'Processo: "
        f"{processo}'. NÃO escreva você mesmo esse cabeçalho com `par()`/`r()` — chame só a função.\n"
        "3. Logo após o `enderecamento()`, ANTES da qualificação, insira uma linha adicional "
        "(mesmo padrão de `par()`/`r()`, alinhado à esquerda, sem recuo) com a referência do "
        f"PAJ: 'PAJ: {paj_norm}' — é controle interno da Defensoria, não precisa de destaque.\n"
        "4. `qualificacao(doc, nome_da_parte, nome_da_peca)` — nome_da_parte em maiúsculas "
        "(a função já faz isso), nome_da_peca escolhido por você conforme o que a petição "
        "efetivamente faz (ex.: 'PETIÇÃO', 'PETIÇÃO DE JUNTADA DE DOCUMENTOS', conforme cabível "
        "— nunca invente um nome que não corresponda ao conteúdo real).\n"
        "5. Parágrafos com `par()`/`r()` — texto corrido, conciso, seguindo as regras de estilo "
        "acima (impessoal, sem travessão, sem dois-pontos em frases, numeração 1./1.1/a) quando "
        "houver marcadores). Use as movimentações recentes do PAJ e o ato referenciado juntos pra "
        "entender o que realmente precisa ser dito — não repita o texto do ato, responda a ele.\n"
        "6. Se houver pedido, encerre o corpo com o requerimento (sem usar dois-pontos antes da "
        "lista).\n"
        "7. `assinatura(doc)` — chame-a UMA ÚNICA VEZ, IMEDIATAMENTE após o último parágrafo do "
        "pedido, SEM passar `termos`/`data` (o padrão já é o fecho fixado pelo Defensor: "
        "'Curitiba, <data de hoje por extenso>.' seguido do nome/cargo — SEM 'Termos em que pede "
        "deferimento'). NÃO escreva você mesmo nenhuma linha de fecho antes dela (nem 'Termos em "
        "que pede deferimento', nem 'Curitiba, ...data...', nem o nome/cargo do defensor) — "
        "`assinatura()` já gera TODAS essas linhas sozinha. Escrever essas linhas manualmente E "
        "chamar `assinatura()` duplica o fecho inteiro no documento final.\n"
        f"8. `salvar(doc, r'{docx_destino}')` — ESTE CAMINHO EXATO, sem alterar.\n\n"
        "Escreva e execute o script diretamente (você tem acesso a Bash/Write) — não pergunte "
        "nada, não peça confirmação, não mostre o texto antes de salvar. Ao final, apenas "
        "confirme em 1 linha que o arquivo foi salvo."
    )


def incluir_nos_anexos(paj_norm: str, peticao_html: str) -> dict:
    """"Incluir petição nos anexos" — pega o texto ATUAL da caixa "Petição"
    (já editado manualmente pelo usuário, ou o gerado pela IA sem alteração) e
    o formata no padrão DPU (`peticao_base.py`), sem chamar IA nenhuma — é uma
    transformação estrutural (HTML da Quill -> .docx -> PDF), rápida e
    síncrona. Nome do arquivo: "Petição-DD-MM-AAAA" (ver `_nome_peticao_hoje`)
    — no MESMO dia, sobrescreve o gerado pela IA (mesmo arquivo lógico, só
    reexportado); útil quando o texto precisou de ajuste manual depois de
    gerado. Salva na subpasta "Peças IA" do PAJ (mesmo motivo de
    `_gerar_worker` acima — fica disponível pra "Adicionar Petições IA" tanto
    em Ações no Eproc quanto em Ações no SISDPU)."""
    from config import PAJS_DIR

    texto_limpo = re.sub(r"<[^>]+>", "", peticao_html or "").strip()
    if not texto_limpo:
        return {"ok": False, "erro": "A caixa de Petição está vazia — escreva ou gere o texto antes de incluir nos anexos."}

    pasta_paj = PAJS_DIR / paj_norm / "Peças IA"
    pasta_paj.mkdir(parents=True, exist_ok=True)
    nome = _nome_peticao_hoje()
    docx_path = pasta_paj / f"{nome}.docx"
    pdf_path = pasta_paj / f"{nome}.pdf"

    try:
        _html_para_docx(peticao_html, docx_path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Falha ao formatar a petição: {type(e).__name__}: {e}"[:400]}

    with contextlib.suppress(Exception):
        if pdf_path.exists():
            pdf_path.unlink()
    _docx_para_pdf(docx_path, pdf_path)
    if not pdf_path.exists():
        return {"ok": False, "erro": "A petição foi formatada (.docx), mas a conversão pra PDF falhou "
                                      "(verifique se o Microsoft Word está instalado nesta máquina)."}

    pasta_rascunho = acoes_eproc_service._rascunho_arquivos_dir(paj_norm)
    pasta_rascunho.mkdir(parents=True, exist_ok=True)
    pdf_rascunho = pasta_rascunho / f"{nome}.pdf"
    shutil.copyfile(pdf_path, pdf_rascunho)

    return {"ok": True, "arquivo": f"{nome}.pdf", "tamanho": pdf_rascunho.stat().st_size}


# Paragráfos do bloco de assinatura, pra RETIRAR do texto vindo da caixa (se o
# usuário deixou/editou ali) e sempre reconstruir com `assinatura()` — garante
# a formatação certa (centralizado, negrito no nome) mesmo que o Quill não
# tenha preservado alinhamento (a extração _docx_para_html só guarda run-level
# bold/italic/underline, não parágrafo-level alignment).
_RE_LINHA_ASSINATURA = re.compile(
    r"^(termos em que pede deferimento\.?|.+,\s*data do protocolo eletr[ôo]nico\.?|"
    # Fecho com data por extenso já preenchida (ex.: "Curitiba, 18 de agosto
    # de 2026.") — usado por quem chama `_html_para_docx` com `data=` (ver
    # services/minuta_ia_service.py) em vez do placeholder de protocolo acima.
    r".+,\s*\d{1,2}\s+de\s+[a-zà-ÿ]+\s+de\s+\d{4}\.?|"
    r"felipe belache kugler|defensor p[uú]blico federal)$",
    re.IGNORECASE,
)

# Linhas de cabeçalho (endereçamento/autos/PAJ) — SEM recuo de 1ª linha,
# diferente de todo o resto do corpo (recuo só a partir da qualificação, ver
# `enderecamento()`/`qualificacao()` em peticao_base.py). Detectadas por
# CONTEÚDO (regex própria pra cada uma, não uma só) pelo mesmo motivo de
# `_RE_LINHA_ASSINATURA`: a extração `_docx_para_html` só guarda formatação
# por RUN (negrito/itálico/sublinhado), não por parágrafo (recuo/quebras em
# branco) — sem isso, "Gerar arquivo (DOC/PDF)" reconstruía essas linhas com
# o recuo padrão de `par()` e sem as quebras em branco antes delas, igual ao
# resto do corpo (bug real, 18/08/2026).
_RE_LINHA_ENDERECO = re.compile(r"^EXCELENT[ÍI]SSIMO.*$", re.IGNORECASE)
_RE_LINHA_AUTOS = re.compile(r"^Autos n[ºo°]\s.*$", re.IGNORECASE)
_RE_LINHA_PAJ = re.compile(r"^PAJ[:\s].*$", re.IGNORECASE)
_RE_LINHA_SEM_RECUO = re.compile(
    r"^(EXCELENT[ÍI]SSIMO.*|Autos n[ºo°]\s.*|PAJ[:\s].*)$",
    re.IGNORECASE,
)


def _html_para_docx(peticao_html: str, docx_path: Path, termos: bool = False, data: str | None = None) -> None:
    """HTML da Quill -> .docx no padrão DPU. Cada `<p>`/`<li>` vira um
    parágrafo justificado com recuo de 1ª linha (mesmo estilo de `par()`);
    `<strong>/<b>`, `<em>/<i>`, `<u>` viram negrito/itálico/sublinhado por
    run. O bloco de assinatura (se sobrou do texto original) é removido e
    sempre reconstruído do zero via `assinatura()`, pra garantir centralização
    e negrito corretos independente do que a extração/edição preservou.

    `termos`/`data`: repassados direto pra `assinatura()` — defaults (False/
    None) são o fecho padrão fixado pelo Defensor (18/08/2026): "Curitiba,
    <data de hoje por extenso>." + nome/cargo, SEM "Termos em que pede
    deferimento" e SEM o antigo placeholder "data do protocolo eletrônico"
    — removidos de todas as peças, sem exceção."""
    sys.path.insert(0, str(MODELOS_DIR))
    from bs4 import BeautifulSoup, NavigableString

    from peticao_base import Cm, FI, assinatura, blank, novo_documento, par, r as escrever_run, salvar

    soup = BeautifulSoup(peticao_html or "", "html.parser")
    blocos = soup.find_all(["p", "li"])
    if not blocos:
        # Sem nenhuma tag de bloco (textarea simples, sem Quill) — texto puro
        # com quebras de linha; separa por linha em vez de virar 1 parágrafo só.
        texto_bruto = soup.get_text("\n")
        blocos = [BeautifulSoup(f"<p>{linha}</p>", "html.parser") for linha in texto_bruto.split("\n") if linha.strip()]

    doc, _ = novo_documento()
    linha_anterior_era_paj = False
    linha_anterior_era_enderecamento = False
    for bloco in blocos:
        runs = _extrair_runs(bloco, NavigableString)
        texto_puro = "".join(t for t, _b, _i, _u in runs).strip()
        if not texto_puro:
            continue
        if _RE_LINHA_ASSINATURA.match(texto_puro):
            continue  # descartado — assinatura() reconstrói no final

        eh_autos = bool(_RE_LINHA_AUTOS.match(texto_puro))
        eh_paj = bool(_RE_LINHA_PAJ.match(texto_puro))
        eh_enderecamento = bool(_RE_LINHA_ENDERECO.match(texto_puro))
        sem_recuo = eh_autos or eh_paj or eh_enderecamento

        # 3 quebras em branco ANTES do número dos autos, e 3 ANTES do
        # parágrafo de qualificação (o primeiro parágrafo comum logo após a
        # linha do PAJ) — mesma regra do enderecamento()/prompt de geração,
        # perdida na extração pro texto editável (bug real, 18/08/2026).
        if eh_autos:
            for _ in range(3):
                blank(doc)
        if linha_anterior_era_paj and not sem_recuo:
            for _ in range(3):
                blank(doc)
        # 8 quebras em branco ("EXCELENTÍSSIMO..." direto pro corpo, SEM
        # linha de Autos/PAJ no meio — padrão de peças como a de impugnação
        # de laudo pericial, cuja #ESTRUTURA_DA_PETIÇÃO vai direto do
        # endereçamento pra qualificação). Mesmo total de `enderecamento()`
        # em peticao_base.py (3 antes do Autos + 5 depois = 8) — aqui não há
        # Autos/PAJ pra repartir, então os 8 vêm juntos. Só dispara quando a
        # linha seguinte NÃO for Autos/PAJ (senão essas regras acima já
        # cuidam do espaçamento delas, sem dobrar).
        if linha_anterior_era_enderecamento and not sem_recuo:
            for _ in range(8):
                blank(doc)
        linha_anterior_era_paj = eh_paj
        linha_anterior_era_enderecamento = eh_enderecamento

        p = par(doc, fi=(Cm(0) if sem_recuo else FI))
        for texto, bold, italic, underline in runs:
            if texto:
                escrever_run(p, texto, bold=bold, italic=italic, underline=underline)
    assinatura(doc, termos=termos, data=data)
    salvar(doc, str(docx_path))


def _extrair_runs(bloco, navigable_string_cls) -> list[tuple[str, bool, bool, bool]]:
    """Percorre os filhos de um `<p>`/`<li>` e devolve [(texto, bold, italic,
    underline), ...] — um item por trecho de formatação uniforme. Tags de
    formatação aninhadas (ex.: `<strong><em>x</em></strong>`) combinam os
    estilos; qualquer outra tag (span de cor, etc.) é tratada como texto puro
    (peticao_base não suporta cor arbitrária nas regras de estilo da DPU)."""
    runs: list[tuple[str, bool, bool, bool]] = []

    def visitar(no, bold, italic, underline):
        if isinstance(no, navigable_string_cls):
            if str(no):
                runs.append((str(no), bold, italic, underline))
            return
        nome = getattr(no, "name", "") or ""
        b = bold or nome in ("strong", "b")
        i = italic or nome in ("em", "i")
        u = underline or nome == "u"
        for filho in getattr(no, "children", []):
            visitar(filho, b, i, u)

    for filho in getattr(bloco, "children", []):
        visitar(filho, False, False, False)
    return runs


def _docx_para_pdf(docx_path: Path, pdf_path: Path) -> None:
    """Converte via Word/COM (mesmo padrão de calc_valor_causa.py). Precisa
    rodar em thread com COM inicializado — `_gerar_worker` já roda numa
    thread dedicada, então inicializamos aqui."""
    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    try:
        word = win32.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        try:
            doc = word.Documents.Open(str(docx_path.resolve()), ReadOnly=True)
            doc.SaveAs2(str(pdf_path.resolve()), FileFormat=17)
            doc.Close(SaveChanges=False)
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()
