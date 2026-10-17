"""Pipeline de IA da "Triagem Inteligente de Intimações".

Três etapas encadeáveis (todas assíncronas; as chamadas bloqueantes — Claude
CLI, conversão docx->PDF via Word/COM — rodam em thread via
`asyncio.to_thread`, mesmo efeito de rodar "em background" sem travar o loop
de eventos do FastAPI durante uma requisição SSE longa):

  a) `analisar_processo`   — resume a intimação em linguagem simples, com
                             base SOMENTE nos atos do processo judicial
                             (e-proc): o(s) evento(s) referenciado(s) e
                             qualquer andamento posterior relevante.
  b) `confrontar_com_paj`  — classifica o caso em 1 dos 4 rótulos (nome
                             histórico da função — NÃO confronta mais com
                             nada do PAJ, ver restrição abaixo).
  c) `gerar_minuta`        — gera a minuta (petição OU despacho, conforme a
                             classificação) como RASCUNHO para revisão humana.

`rodar_pipeline` encadeia as três, persistindo o resultado parcial de cada
etapa via `services.intimacao_service.salvar_estado` (retomável: uma falha
numa etapa não apaga o que já foi salvo pelas anteriores).

⚠️ NUNCA ANALISAR O PAJ — pedido explícito do usuário (2026-08-29): "não
deveremos mais analisar o PAJ em nenhuma situação... toda a análise da
intimação deverá ficar restrita aos atos do processo... se houver
necessidade de analisar o PAJ isso será feito manualmente pelo defensor".
Isso significa: NENHUMA etapa deste pipeline pode sincronizar o PAJ
(`ingestao.sincronizador`), ler sua narrativa/movimentações/documentos
(`services.paj_service.ler_paj`), ou rodar a análise de pretensão
(`services.analise_pretensao_service`) — removido por completo (era o
gargalo de tempo de cada análise). O vínculo de PAJ (`paj_norm`) continua
exigido só como IDENTIFICADOR de onde salvar a minuta, nunca como fonte de
conteúdo pra análise.

⚠️ RESTRIÇÃO DE SEGURANÇA (separada da acima) — leia antes de mexer aqui:
este módulo NUNCA pode chamar nenhuma função de execução real no e-proc
(`services.acoes_eproc_service.executar_peticionamento` / `.confirmar_
peticionamento_real` / `.executar_gerenciamento_real`, nem `ingestao.
eproc_client.preparar_peticionamento` / `.confirmar_peticionamento` /
`.gerenciar_processo`) nem nenhuma função que movimente/tramite/conclua de
verdade no SISDPU (`services.acoes_sisdpu_service.executar_stream`, nem
`ingestao.sisdpu_movtram.movimentar_paj` / `.tramitar_paj` / `.concluir_
paj`). O trabalho desta feature termina em resumo, diagnóstico,
classificação e minuta pronta para revisão — a execução real continua
exclusiva dos consoles "Ações no e-proc"/"Ações no SISDPU" já existentes,
acionados manualmente pelo Defensor a partir da página do PAJ."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import config
from services import intimacao_service
from services.estilo_pecas_service import INSTRUCAO_ESTILO_PECA_DPU

LogCb = Callable[[str, str], None]


# --------------------------------------------------------------------------
# Helpers comuns (casamento de evento, chamada ao Claude CLI, formatação)
# --------------------------------------------------------------------------

def _so_data(s: str) -> str:
    """Só o 'dd/mm/aaaa' dentro da string — mesmo recorte usado em
    `services.peticao_ia_service._soData` / `services.acoes_eproc_service._so_data`
    pra casar prazo do Painel do Procurador com intimação da triagem MCP."""
    m = re.search(r"(\d{2}/\d{2}/\d{4})", s or "")
    return m.group(1) if m else ""


async def _triagem_com_fallback(processo: str) -> tuple[list[dict], dict, int]:
    """Tenta a triagem MCP na 1ª e depois na 2ª instância — mesmo padrão de
    `services.acoes_eproc_service.listar_prazos`. Devolve `(intimacoes,
    cabecalho, instancia)` — a instância importa pra depois baixar o
    documento certo (`_documentos_evento`), já que `baixar_documentos_evento`
    exige a instância correta (bug real corrigido: antes ficava hardcoded em
    1, então um processo só encontrado na 2ª instância nunca conseguia
    baixar o texto do ato referenciado). `cabecalho` alimenta
    `eproc_procurador_service.partes_do_procurador` (já vem de graça na
    mesma chamada, sem custo extra)."""
    from ingestao import eproc_mcp_client

    for instancia in (1, 2):
        try:
            res = await eproc_mcp_client.triagem_processo(processo, instancia=instancia)
        except Exception:
            res = None
        if res and not res.get("erro"):
            return res.get("intimacoes") or [], res.get("cabecalho") or {}, instancia
    return [], {}, 1


async def _candidatos_do_ato(processo: str, prazos_do_ato: list[dict]) -> tuple[list[dict], int]:
    """Todas as intimações AO VIVO (não só a "principal") que casam com ESTE
    ATO — base compartilhada de `_casar_evento_referenciado` (fecha o ato,
    devolve principal+coautores) e `listar_eventos_do_ato` (lista completa
    pro checklist "Selecione o(s) prazo(s)..." do modal de "Dar mera
    ciência" — pedido do usuário 2026-09-24, mesmo estilo do checklist já
    usado em "Ações no Eproc"). `prazos_do_ato` é o grupo já resolvido por
    `intimacao_service._agrupar_prazos_por_ato` (via `obter_caso(processo,
    ato)["prazos"]") — pode ter mais de 1 item só quando são "simultâneos"
    (mesmo ato, o Painel raspa 1 linha por parte representada), mas todos
    compartilham a MESMA assinatura dia+dias por definição do agrupamento,
    então usar só `prazos_do_ato[0]` já basta pra achar o casamento.

    Casamento por dia de envio + nº de dias + parte associada a MIM (ver
    `services.eproc_procurador_service` — cabeçalho de partes do processo
    cruzado com o identificador do procurador logado). Substituiu por
    completo (2026-09-23) a antiga heurística de "polo ativo", que assumia
    a DPU sempre no polo ativo e por isso escolhia a parte ERRADA sempre
    que a DPU representava o polo passivo (ver `eproc_procurador_service`
    pro caso real que expôs isso).

    Devolve `([], instancia)` se não houver casamento possível (cache
    desatualizado, identidade indisponível — cofre trancado —, ou nenhuma
    intimação bate)."""
    from services import eproc_procurador_service

    if not prazos_do_ato:
        return [], 1
    prazo = prazos_do_ato[0]
    intimacoes, cabecalho, instancia = await _triagem_com_fallback(processo)
    if not intimacoes:
        return [], instancia
    minhas_partes = eproc_procurador_service.partes_do_procurador(
        cabecalho, eproc_procurador_service.meu_identificador(processo))
    dia_p = _so_data(prazo.get("data_envio", ""))
    if not dia_p:
        return [], instancia
    try:
        dias_p = int(prazo.get("prazo_dias") or -1)
    except (TypeError, ValueError):
        dias_p = -1
    candidatos = []
    for it in intimacoes:
        if not it.get("ativa", True):
            continue
        if not eproc_procurador_service.parte_intimada_e_minha(it.get("parte_intimada", ""), minhas_partes):
            continue
        if _so_data(it.get("data_hora", "")) != dia_p:
            continue
        try:
            dias_it = int(it.get("prazo_dias") or -1)
        except (TypeError, ValueError):
            dias_it = -1
        if dias_p != -1 and dias_it != -1 and dias_p != dias_it:
            continue
        candidatos.append(it)
    # Ordena por nº de evento — só estética (o rótulo/descrição de "Dar mera
    # ciência" lê melhor "eventos 38, 39" do que na ordem crua em que a
    # triagem devolveu); não afeta qual é o "principal" nem quais fecham.
    def _num_evento(it: dict) -> int:
        try:
            return int(it.get("evento_intimacao") or 0)
        except (TypeError, ValueError):
            return 0
    candidatos.sort(key=_num_evento)
    return candidatos, instancia


async def _casar_evento_referenciado(processo: str, prazos_do_ato: list[dict]) -> dict | None:
    """Localiza o evento referenciado por ESTE ATO específico — devolve o
    candidato PRINCIPAL + eventuais coautores (litisconsórcio ativo — mais
    de uma parte associada a mim intimada no MESMO evento/dia/prazo, ex.
    casal, cocredores de uma mesma RPV, em qualquer polo). A triagem MCP
    traz UMA intimação POR PESSOA, cada uma com seu PRÓPRIO nº de evento, e
    cada uma precisa do SEU PRÓPRIO "Ciência, com renúncia ao prazo" pra
    fechar de verdade — fechar só a do 1º nome da lista deixa o prazo do 2º
    coautor aberto. Caso real que expôs isso (2026-08-29, PAJ
    2026/029-00675): RPV intimada a MANOEL e a LUCIANE no mesmo Ofício
    Requisitório/dia/prazo — sem isto, "Dar mera ciência" guardava só o
    evento de um dos dois e o outro prazo "nunca batia" no checkbox.

    None se não houver casamento possível (ver `_candidatos_do_ato`)."""
    candidatos, instancia = await _candidatos_do_ato(processo, prazos_do_ato)
    if not candidatos or not prazos_do_ato:
        return None
    principal = candidatos[0]
    outros_coautores = candidatos[1:]
    return {"prazo": prazos_do_ato[0], "intimacao": principal, "instancia": instancia,
            "outros_coautores": outros_coautores}


async def listar_eventos_do_ato(processo: str, prazos_do_ato: list[dict]) -> dict:
    """Todos os candidatos (ao vivo, já filtrados por identidade — ver
    `_candidatos_do_ato`) que casam com ESTE ato, cada um com um rótulo
    pronto pro checklist "Selecione o(s) prazo(s) a ser(em) fechado(s)" do
    modal de "Dar mera ciência" na página do PAJ (pedido do usuário
    2026-09-24) — MESMO formato oficial já usado em "Ações no Eproc"
    (`acoes_eproc_service.listar_prazos`/`static/js/acoes_eproc.js:
    prazoLabel`), ex.: "Expedida/certificada a intimação eletrônica -
    Sentença (10/09/2026 15:59:41) - Ref. Evento 35 - ELIO OMAR ZORZINI
    HERNANDEZ" (faltava o rótulo+data/hora aqui — bug real relatado
    2026-09-24: "faltam os dados da intimação, como ato referenciado, data,
    hora etc"). `descricao_ato` é o mesmo pro grupo inteiro (é o MESMO ato
    referenciado, ex. "Ato ordinatório praticado" ou "Juntado(a) - Ofício
    Requisitório Nr. ..."), usado pra montar a descrição da movimentação no
    SISDPU.

    Devolve `{"descricao_ato": str, "itens": [{"evento_intimacao", "label"}, ...]}`
    — `itens` vazio se não houver casamento possível."""
    candidatos, _ = await _candidatos_do_ato(processo, prazos_do_ato)
    itens = []
    descricao_ato = ""
    for it in candidatos:
        er = it.get("evento_referenciado")
        if not descricao_ato:
            descricao_ato = (er.get("descricao", "") if isinstance(er, dict) else "") or it.get("rotulo", "")
        nome = (it.get("parte_intimada") or "").split(" - ", 1)
        nome = nome[1] if len(nome) > 1 else nome[0]
        evento = it.get("evento_intimacao")
        label = (f'{it.get("rotulo", "")} ({it.get("data_hora", "")}) '
                 f'- Ref. Evento {evento} - {nome}').strip()
        itens.append({"evento_intimacao": evento, "label": label})
    return {"descricao_ato": descricao_ato, "itens": itens}


async def obter_evento_referenciado_leve(processo: str, prazos_do_ato: list[dict]) -> dict | None:
    """Localiza o(s) evento(s)/ato(s) referenciado(s) por ESTE ATO específico
    (`prazos_do_ato`, mesmo casamento de `_casar_evento_referenciado` usado
    por `analisar_processo`) — SEM NENHUMA chamada de IA, só MCP (leitura).
    `None` se não houver casamento possível (mesmo critério de
    `_casar_evento_referenciado`).

    Pedido explícito do usuário (2026-08-29): "esse ato referenciado deve
    estar disponível direto, independentemente de qualquer análise" — pra
    popular o badge "Ato referenciado" (com link pra abrir/baixar o
    documento) na listagem de Intimações ANTES de rodar "Analisar", do
    mesmo jeito que a página do PAJ já mostra em "Intimações pendentes"
    (`templates/partials/processo_judicial.html` — ver `eventoDe`/
    `docsDisponiveis`/`temDocPendente` em `static/js/processo_judicial.js`).

    Devolve `{"instancia": int, "evento_intimacao", "evento_intimacao_coautores",
    "rotulo", "evento_descricao", "atos_referenciados": [{"numero", "descricao",
    "ator_tipo", "documentos", "arquivos_baixados"}, ...]}`. Os 4 campos
    novos (2026-08-29) são os MESMOS que `analisar_processo` grava em
    `evento_referenciado` — expostos aqui pra "Dar mera ciência"
    (`mera_ciencia_service.dar_mera_ciencia`) saber qual evento fechar no
    e-proc SEM depender de o caso já ter sido analisado (pedido explícito do
    usuário: "quero que o botão esteja disponível sempre"). `documentos`
    é a lista crua do MCP (`{"sigla"/"nome", ...}`, cujo `url` NÃO é
    reaproveitável fora da sessão que o gerou — descoberto 2026-08-28, por
    isso não é usado aqui); `arquivos_baixados` é o que já está baixado
    localmente (mesmo formato de `ingestao.eproc_mcp_client.
    listar_arquivos_baixados`, anotado pela própria `triagem_processo` por
    evento) — o front decide, por evento, se mostra o badge de ABRIR (já
    baixado, via `/api/eproc-mcp/arquivo`) ou de BAIXAR (ainda não, via
    `/api/eproc-mcp/baixar-documentos`) — mesmos 2 endpoints genéricos já
    usados pela página do PAJ, sem nenhum endpoint novo."""
    casamento = await _casar_evento_referenciado(processo, prazos_do_ato)
    if not casamento:
        return None
    it = casamento["intimacao"]
    instancia = casamento.get("instancia", 1)

    er = it.get("evento_referenciado")
    eid = (er.get("numero") if isinstance(er, dict) else er) or it.get("evento_intimacao")
    eventos_ref_nums: list[str] = [str(eid)] if eid else []
    for extra in it.get("outros_eventos_referenciados") or []:
        extra = str(extra).strip()
        if extra and extra not in eventos_ref_nums:
            eventos_ref_nums.append(extra)

    # `evento_intimacao`/`evento_intimacao_coautores`/`rotulo`/`evento_descricao`
    # (2026-08-29): MESMOS campos que `analisar_processo` grava em `evento_
    # referenciado` — aqui disponíveis SEM IA, pra "Dar mera ciência"
    # (`static/js/intimacoes.js:darMeraCiencia`/`mera_ciencia_service.
    # dar_mera_ciencia`) funcionar mesmo pra um caso NUNCA analisado. Pedido
    # explícito do usuário: "quero que o botão esteja disponível sempre,
    # independentemente de ter sido feita a análise da intimação" — sem
    # esses campos aqui, o botão só tinha como saber qual evento fechar no
    # e-proc depois de rodar "Analisar" pelo menos uma vez.
    base = {
        "instancia": instancia,
        "evento_intimacao": it.get("evento_intimacao"),
        "evento_intimacao_coautores": [
            co.get("evento_intimacao") for co in (casamento.get("outros_coautores") or [])
            if co.get("evento_intimacao")
        ],
        "rotulo": it.get("rotulo", ""),
        "evento_descricao": er.get("descricao", "") if isinstance(er, dict) else "",
        # `parte_intimada` (2026-09-25): a parte JÁ CASADA POR IDENTIDADE (ver
        # `_candidatos_do_ato`/`eproc_procurador_service` — nunca mais "polo
        # ativo/autor" às cegas), formato cru do MCP ("PAPEL - NOME"). Exposta
        # aqui pra alimentar a coluna "Nome" da tabela de Intimações
        # (`static/js/intimacoes.js:nomeAssistido`) — achado real do usuário
        # (2026-09-25): a coluna mostrava OAB-PR/EMGEA (o outro polo) em vez
        # do assistido de verdade sempre que a DPU representava o polo
        # passivo, porque o parser antigo (`it.partes` cru) sempre assumia
        # polo ativo.
        "parte_intimada": it.get("parte_intimada", ""),
    }
    if not eventos_ref_nums:
        return {**base, "atos_referenciados": []}

    # A anotação de "já baixado" (`arquivos_baixados`) só vem nos eventos da
    # TIMELINE completa, não no sub-objeto `evento_referenciado` embutido na
    # intimação (mesmo motivo por trás de `eventoDe()`/`docsDisponiveis()`
    # no lado do PAJ) — por isso busca a timeline mesmo pro evento primário.
    timeline = await _timeline_completa(processo, instancia)
    eventos_por_numero = {str(ev.get("numero")): ev for ev in timeline}

    atos_referenciados = []
    for num in eventos_ref_nums:
        ev_dados = eventos_por_numero.get(num) or {}
        atos_referenciados.append({
            "numero": num,
            "descricao": ev_dados.get("descricao") or "",
            "ator_tipo": ev_dados.get("ator_tipo") or "",
            "documentos": ev_dados.get("documentos") or [],
            "arquivos_baixados": ev_dados.get("arquivos_baixados") or [],
        })
    return {**base, "atos_referenciados": atos_referenciados}


_TIMEOUT_EXTRACAO_PDF_SEG = 120  # teto total por documento (bem acima do custo típico de um
                                 # despacho/decisão curto; protege contra PDF gigante/corrompido)


async def _texto_pdf(p: Path, log_cb: LogCb | None) -> str:
    """Extrai o texto de UM PDF já baixado — mesmo motor usado pelos anexos
    comuns do PAJ (`ingestao.ocr.extrair_texto`: texto nativo se o PDF não for
    escaneado, com fallback automático pra OCR/Tesseract se for). Bug real
    corrigido: antes, todo PDF era simplesmente PULADO aqui ("PDF puro: sem
    extração de texto"), o que inutilizava o resumo sempre que o ato
    referenciado era um PDF — a imensa maioria dos despachos/decisões do
    e-proc. Roda em thread (pode envolver OCR, é bloqueante) com um teto de
    tempo total; se estourar ou falhar, devolve "" e o chamador segue com o
    que conseguiu dos outros documentos."""
    from ingestao import ocr

    if log_cb:
        log_cb("info", f"Lendo o conteúdo de {p.name} (texto nativo do PDF, com OCR se for "
                       "escaneado)…")
    try:
        texto = await asyncio.wait_for(
            asyncio.to_thread(ocr.extrair_texto, p, timeout_por_pagina_seg=config.TIMEOUT_OCR_POR_PAGINA_SEG),
            timeout=_TIMEOUT_EXTRACAO_PDF_SEG,
        )
    except asyncio.TimeoutError:
        if log_cb:
            log_cb("muted", f"Extração de {p.name} demorou demais (>{_TIMEOUT_EXTRACAO_PDF_SEG}s) — pulando.")
        return ""
    except Exception as e:  # noqa: BLE001
        if log_cb:
            log_cb("muted", f"Falha ao extrair texto de {p.name}: {e}")
        return ""
    if "[OCR indisponível" in texto or "[PyMuPDF não instalado" in texto or "[erro abrindo PDF" in texto:
        if log_cb:
            log_cb("muted", f"{p.name}: {texto.strip()[:200]}")
        return ""
    return texto


# Prefixos de sigla que costumam ser o ATO PRINCIPAL de um evento (mesmo
# catálogo de `_CATALOGO_SIGLAS_ATO_PRINCIPAL`, só que em forma de prefixo pra
# casamento heurístico — usado por `_identificar_principal` pra decidir QUAL
# documento ler primeiro, sem precisar extrair texto de nenhum ainda).
_PREFIXOS_SIGLA_PRINCIPAL = (
    "DESPADEC", "SENT", "PROACORDO", "PET", "CONTEST", "RECINO", "APELA",
    "ATOORD", "LAUDOPERIC", "LAUDO_SOC_ECON", "LAUDO", "RPV", "REQPAGAM",
)


def _identificar_principal(documentos_meta: list[dict]) -> int:
    """Índice do documento mais provável de ser o ATO PRINCIPAL do evento,
    por casamento de PREFIXO da sigla contra `_PREFIXOS_SIGLA_PRINCIPAL` —
    heurística barata (só olha o NOME, não abre nenhum arquivo) pra decidir
    qual documento extrair/ler PRIMEIRO. Pedido explícito do usuário: só
    avaliar os anexos se o principal sozinho não for suficiente pra entender
    o ato — ler todos os documentos de saída é desperdício de tempo/OCR
    quando o principal já resolve.

    Uma sigla genérica "ANEXOx" nunca é escolhida como principal. Se nenhuma
    sigla bater o catálogo, cai na 1ª que não seja "ANEXOx"; se todas forem
    genéricas, usa a primeira da lista (ordem do e-proc)."""
    for i, doc in enumerate(documentos_meta):
        sigla = (doc.get("sigla") or "").upper()
        if sigla.startswith("ANEXO"):
            continue
        if any(sigla.startswith(pref) for pref in _PREFIXOS_SIGLA_PRINCIPAL):
            return i
    for i, doc in enumerate(documentos_meta):
        if not (doc.get("sigla") or "").upper().startswith("ANEXO"):
            return i
    return 0


async def _listar_documentos_evento(processo: str, evento_numero: str, instancia: int = 1,
                                     chave: str | None = None) -> list[dict]:
    """Baixa (via MCP, idempotente, já na instância correta) TODOS os
    documentos do evento referenciado — o e-proc só permite baixar por
    evento inteiro, não documento a documento, então esse download nunca é
    evitável — mas devolve só os METADADOS (`[{"sigla", "arquivo"}, ...]`,
    na ordem do e-proc), SEM extrair texto de nenhum ainda. A extração (cara,
    pode envolver OCR) fica pra `_extrair_texto_documento`, chamada só para
    os documentos que realmente precisarem ser lidos (ver
    `_identificar_principal` e o fluxo em `analisar_processo`).

    `chave`: chave de acesso do processo (número+chave cadastrada no PAJ) —
    necessária pra baixar documento SIGILOSO (cálculo/RPV/precatório com
    valores). Usada pelo fluxo de conferência de RPV (ver
    `gerar_conferencia_rpv`/`eproc_mcp_client.buscar_chave_paj`); os demais
    chamadores seguem sem passar nada (None), comportamento inalterado."""
    if not evento_numero:
        return []
    from ingestao import eproc_mcp_client

    try:
        res = await eproc_mcp_client.baixar_documentos_evento(
            processo, instancia, [str(evento_numero)], chave=chave)
    except Exception:
        return []
    itens = (res.get("arquivos_por_evento") or {}).get(str(evento_numero)) or []
    documentos: list[dict] = []
    for item in itens:
        # `listar_arquivos_baixados` NÃO devolve um campo "sigla" — as chaves
        # reais são "nome_arquivo" (já sem o prefixo "evN_", ex.
        # "PROACORDO1.html"/"ANEXO2.pdf") e "arquivo" (nome completo do
        # arquivo local, ex. "ev23_PROACORDO1.html"). Bug real corrigido:
        # `item.get("sigla")`/`item.get("nome")` não existiam, então a sigla
        # caía sempre no fallback e virava o nome do ARQUIVO LOCAL inteiro
        # em vez da sigla do e-proc — exatamente a distinção de que o
        # catálogo de siglas/heurística de principal dependem.
        nome_arquivo = item.get("nome_arquivo") or item.get("arquivo") or ""
        sigla = Path(nome_arquivo).stem or nome_arquivo or "documento"
        nome = item.get("arquivo") or item.get("nome_arquivo") or ""
        documentos.append({"sigla": sigla, "arquivo": nome, "arquivo_txt": item.get("arquivo_txt")})
    return documentos


async def _extrair_texto_documento(processo: str, doc_meta: dict, log_cb: LogCb | None = None) -> str:
    """Extrai o texto de UM documento já baixado (identificado por
    `doc_meta`, saído de `_listar_documentos_evento`) — `.txt` limpo se
    houver (HTML), ou `_texto_pdf` (nativo + OCR fallback) se for PDF."""
    from ingestao import eproc_mcp_client

    pasta = eproc_mcp_client.EPROC_DOCS_DIR / re.sub(r"\D", "", processo or "")
    nome_txt = doc_meta.get("arquivo_txt")
    if nome_txt:
        p_txt = pasta / nome_txt
        if p_txt.exists():
            try:
                return p_txt.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    nome = doc_meta.get("arquivo") or ""
    if not nome:
        return ""
    p = pasta / nome
    if not p.exists():
        return ""
    if p.suffix.lower() == ".pdf":
        return await _texto_pdf(p, log_cb)
    try:
        return eproc_mcp_client.decode_html_documento(p.read_bytes())
    except Exception:
        return ""


def _claude_cmd():
    from services.chat_service import CLAUDE_CMD

    return CLAUDE_CMD


async def _chamar_claude_texto(prompt: str, timeout: int = 300) -> tuple[str, str | None]:
    """Chamada one-shot ao Claude CLI — "motor simples" (mesmo padrão de
    `services.minuta_ia_service`): prompt por STDIN, `cwd=MODELOS_DIR` (fora
    de qualquer monorepo de skills, pra não herdar CLAUDE.md alheio), sem
    pipeline pesado de anti-alucinação. Roda em thread (`asyncio.to_thread`)
    pra não bloquear o loop de eventos do FastAPI durante a chamada
    bloqueante (pode levar minutos). Retorna (texto, erro) — erro None em
    sucesso."""
    from services.peticao_ia_service import MODELOS_DIR

    cmd = [*_claude_cmd(), "-p", "--permission-mode", "bypassPermissions"]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, cwd=str(MODELOS_DIR), input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"
    saida = (proc.stdout or "").strip()
    if not saida:
        erro_txt = (proc.stderr or "").strip()
        return "", f"Resposta vazia da IA.{(' ' + erro_txt[-500:]) if erro_txt else ''}"
    return saida, None


def _extrair_bloco_html(saida: str) -> str:
    """Remove cercas de código markdown, se o modelo insistir em envolver a
    resposta nelas apesar da instrução de responder só com o HTML puro."""
    texto = (saida or "").strip()
    m = re.search(r"```(?:html)?\s*(.*?)```", texto, re.DOTALL)
    return m.group(1).strip() if m else texto


def _sufixo_arquivo_seguro(valor) -> str:
    """Sufixo curto e seguro para nome de arquivo Windows a partir de
    `evento_referenciado` (esperado: string/número do evento, ex. "23").
    Defesa em profundidade: mesmo já corrigido na origem (ver
    `analisar_processo`), qualquer valor que não seja um escalar simples
    (ex. um dict/objeto vazando de uma mudança futura no formato do MCP)
    cai no fallback por timestamp em vez de virar um nome de arquivo
    inválido (bug real corrigido: um dict inteiro no sufixo gerava
    caracteres como ':'/'{'/'\\', que o Windows rejeita com OSError)."""
    if isinstance(valor, (str, int)) and str(valor).strip():
        limpo = re.sub(r"[^A-Za-z0-9_-]", "", str(valor))
        if limpo:
            return limpo
    return datetime.now().strftime("%Y%m%d%H%M")


def _nome_minuta_livre(nome: str, *pastas: Path) -> str:
    """Acrescenta um sufixo numérico ('_v2', '_v3', ...) ao nome-base da
    minuta até achar um que não colida com nenhum `.docx`/`.pdf` já existente
    em nenhuma das `pastas` informadas.

    Existe porque REANALISAR a mesma intimação gera o mesmo nome de arquivo
    (derivado do nº do evento — determinístico, sem timestamp) e uma
    SUBSTITUIÇÃO direta do arquivo antigo é exatamente o padrão que
    engatilhou um bloqueio real do antivírus corporativo (FortiClient/EDR:
    "File Delete blocked for python.exe") ao tentar sobrescrever o `.docx`
    em "Peças IA" (2026-08-28). Gerar um nome novo em vez de sobrescrever
    evita esse padrão de "substituir arquivo existente" — e como bônus
    preserva a minuta anterior, que pode já ter sido revisada/usada pelo
    Defensor (mesmo espírito de nunca apagar/tocar peça já gerada)."""
    def livre(candidato: str) -> bool:
        return not any(
            (p / f"{candidato}.docx").exists() or (p / f"{candidato}.pdf").exists()
            for p in pastas
        )
    if livre(nome):
        return nome
    i = 2
    while not livre(f"{nome}_v{i}"):
        i += 1
    return f"{nome}_v{i}"


_PROJETO_DIR = Path(__file__).resolve().parents[1]
_DOCX_WORKER = _PROJETO_DIR / "services" / "_docx_worker.py"


async def _gerar_docx_pdf(html: str, docx_path: Path, pdf_path: Path) -> str | None:
    """Gera o `.docx`+`.pdf` da minuta através de `_docx_worker.py`, um
    processo Python de VIDA CURTA e SEM socket de rede — medida provisória
    (2026-08-28) enquanto o TI não libera uma exceção no FortiClient, que vem
    bloqueando o PROCESSO DO SERVIDOR (`app.py`, porta 8001 aberta) de
    gravar/converter `.docx` — mesmo um arquivo novo, nunca visto antes (ver
    `services._docx_worker` e conversa com o usuário, 2026-08-28). Um teste
    isolado confirmou que um processo assim, sem rede, grava/apaga
    livremente na mesma pasta.

    Devolve `None` em sucesso; uma mensagem de erro (str) em falha — nunca
    lança. Sem fallback pro método antigo (chamar as funções direto no
    processo do servidor): se o subprocesso falhar, é melhor devolver o erro
    claro do que arriscar reproduzir o bloqueio no processo principal."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(_DOCX_WORKER), str(docx_path), str(pdf_path),
        cwd=str(_PROJETO_DIR), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(html.encode("utf-8")), timeout=180)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return "timeout ao gerar o docx/pdf (worker não respondeu em 180s)"
    if proc.returncode != 0:
        return (stderr or b"").decode("utf-8", errors="replace").strip() or f"worker saiu com código {proc.returncode}"
    return None


def _colorir_colchetes_vermelho(docx_path: Path) -> None:
    """Pós-processamento do `.docx`: todo trecho entre colchetes `[ ]` (marca
    de conferência manual — ver `estilo_pecas_service.INSTRUCAO_ESTILO_PECA_DPU`,
    item d) sai em fonte VERMELHA, preservando negrito/itálico/sublinhado/
    tamanho do resto do run — regra INAFASTÁVEL de marcação para conferência.

    Necessário aqui porque a conversão HTML->docx desta feature é feita em
    PYTHON (`peticao_ia_service._html_para_docx`, via BeautifulSoup), não pelo
    próprio Claude rodando um script (caso em que ele mesmo aplicaria a cor,
    como instruído no item d) — sem este passo, os colchetes ficariam pretos
    como o resto do texto."""
    import docx
    from docx.shared import RGBColor

    doc = docx.Document(str(docx_path))
    padrao = re.compile(r"(\[[^\[\]]*\])")
    for paragrafo in doc.paragraphs:
        runs = list(paragrafo.runs)
        if not runs or "[" not in "".join(r.text for r in runs):
            continue
        pedacos: list[tuple[str, bool, object]] = []
        for r in runs:
            for parte in padrao.split(r.text):
                if parte:
                    pedacos.append((parte, bool(padrao.fullmatch(parte)), r))
        for r in runs:
            r._element.getparent().remove(r._element)
        for texto, vermelho, base in pedacos:
            novo = paragrafo.add_run(texto)
            novo.bold = base.bold
            novo.italic = base.italic
            novo.underline = base.underline
            with contextlib.suppress(Exception):
                if base.font.size:
                    novo.font.size = base.font.size
                if base.font.name:
                    novo.font.name = base.font.name
            if vermelho:
                novo.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
    doc.save(str(docx_path))


# --------------------------------------------------------------------------
# (a) Resumo da intimação
# --------------------------------------------------------------------------

# Catálogo de referência das siglas de ATO PRINCIPAL mais comuns no e-proc,
# fornecido pelo usuário (Defensor) organizado por origem do ato. NÃO é uma
# lista fechada — outros documentos podem ser o ato principal de uma
# intimação; serve só de apoio para a IA reconhecer os padrões mais
# frequentes, nunca para substituir a leitura do conteúdo real.
_CATALOGO_SIGLAS_ATO_PRINCIPAL = """\
Catálogo de referência de siglas de ATO PRINCIPAL no e-proc (apoio, NÃO uma lista fechada —
outras siglas/documentos também podem ser o ato principal; quando a sigla não constar aqui,
identifique o ato principal pelo CONTEÚDO e pela `descricao`/`ator_tipo` do evento, nunca só
pelo nome do arquivo):

- Origem em decisão judicial: DESPADEC1 (despacho/decisão), SENT1 (sentença).
- Origem em manifestação da parte: PET1 e PROACORDO1 (petição, inclusive proposta de
  acordo), CONTEST1 (contestação), RECINO1 (recurso inominado), APELA1 (apelação).
- Origem em ato da secretaria judicial ou demais atos processuais: ATOORD1 (ato
  ordinatório), LAUDOPERIC1 e LAUDO (laudo pericial), LAUDO_SOC_ECON1 (avaliação social).

Siglas genéricas e numeradas sem nome próprio (ex.: "ANEXO2", "ANEXO3", "ANEXO4") quase
sempre são documentos ACESSÓRIOS ao ato principal acima — comprovantes, formulários,
documentos de apoio — não o ato em si."""


_MARCADOR_PRECISA_ANEXOS = "PRECISA_ANEXOS"


def _contexto_tribunal(processo: str) -> list[str]:
    """Linhas de contexto para os prompts quando o processo é da Justiça Militar
    da União (segmento 7 do número, 17/10/2026): sem isso a IA partia do
    contexto previdenciário do TRF4 (INSS, JEF, RPV)."""
    from ingestao import eproc_jmu
    if not eproc_jmu.eh_processo_jmu(processo):
        return []
    return [
        "",
        "CONTEXTO DO TRIBUNAL: este processo tramita na JUSTIÇA MILITAR DA UNIÃO (e-proc da "
        "JMU — Auditorias das Circunscrições Judiciárias Militares, 2º grau no STM). É processo "
        "PENAL MILITAR (Código Penal Militar — Decreto-Lei 1.001/69; Código de Processo Penal "
        "Militar — Decreto-Lei 1.002/69), com o Ministério Público Militar como acusação e a "
        "Defensoria Pública da União, em regra, na DEFESA do acusado. Prazos, recursos e "
        "providências seguem o CPPM (e o CPP subsidiariamente) — NÃO trate como processo "
        "previdenciário/assistencial nem cite INSS, JEF ou RPV.",
    ]


def _montar_prompt_resumo(processo: str, evento_ref: dict | None, documentos: list[dict],
                           so_principal: bool = False, andamentos_posteriores: str = "") -> str:
    """Monta o prompt do resumo apresentando CADA documento SEPARADAMENTE
    (sigla + texto próprio), em vez de um blob único concatenado.

    `so_principal=True` (1ª tentativa, padrão): `documentos` tem só o(s)
    documento(s) que `_identificar_principal` escolheu como provável ATO
    PRINCIPAL — 1 por evento referenciado (ver abaixo). Pedido explícito do
    usuário: só avaliar os anexos se o(s) principal(is) sozinho(s) não
    bastar(em) pra entender o ato, evitando OCR/leitura desnecessária de
    documentos acessórios na maioria dos casos. Se mesmo assim não der pra
    entender, a IA é instruída a sinalizar isso com o marcador
    `_MARCADOR_PRECISA_ANEXOS`, e `analisar_processo` escalona
    automaticamente pra 2ª tentativa.

    `so_principal=False` (2ª tentativa, só quando escalonado): `documentos`
    tem TODOS os documentos do(s) evento(s) — aqui sim a IA precisa
    distinguir ATO PRINCIPAL × ANEXOS/acessórios entre vários, com o
    catálogo de referência como apoio (heurística de nome já não bastou
    sozinha).

    Cada item de `documentos` pode trazer `evento_numero` — a intimação pode
    se referir a MAIS DE UM evento do processo (e-proc: "Refer. aos Eventos:
    N e M"; bug real corrigido 2026-08-29: só o 1º evento era considerado,
    ignorando o 2º por inteiro — caso real: processo 5068227-80.2025.4.04.7000,
    intimação referia os eventos 70 E 73, só o 70 tinha sido lido). Quando há
    mais de 1 `evento_numero` distinto entre os documentos, eles saem
    agrupados por evento no prompt, nunca misturados.

    `andamentos_posteriores` (pedido explícito do usuário, mesmo caso real
    acima: a análise ignorava qualquer andamento do processo POSTERIOR ao
    evento referenciado — no caso, algo relevante aconteceu depois que
    mudava a resposta correta): texto pronto (`_montar_texto_andamentos_
    posteriores`) com a lista de eventos do processo que vieram DEPOIS do(s)
    evento(s) referenciado(s) acima — só rótulo/data, sem conteúdo de
    documento (isso seria caro/desnecessário na maioria dos casos) — pra que
    a IA perceba se algo mudou a situação desde então e avise no resumo."""
    partes = [
        "Você vai resumir uma intimação judicial recebida pela Defensoria "
        f"Pública da União no processo {processo}, em linguagem simples e "
        "objetiva, para um Defensor Público decidir rapidamente o que fazer.",
        *_contexto_tribunal(processo),
    ]
    if evento_ref:
        desc_evento = evento_ref.get("evento_descricao") or ""
        ator = evento_ref.get("evento_ator_tipo") or ""
        partes += [
            "",
            f"Rótulo oficial da intimação (e-proc): {evento_ref.get('rotulo', '')}",
            f"Data: {evento_ref.get('data_hora', '')} — Prazo: {evento_ref.get('prazo_dias', '')} dias"
            f" — Final: {evento_ref.get('final_prazo', '')}",
        ]
        if desc_evento:
            partes.append(f"Tipo/descrição do evento referenciado no e-proc: {desc_evento}"
                          + (f" (autor: {ator})" if ator else ""))

    eventos_distintos = list(dict.fromkeys(
        d.get("evento_numero") for d in documentos if d.get("evento_numero")))
    multi_evento = len(eventos_distintos) > 1
    if multi_evento:
        partes += [
            "",
            f"⚠️ Esta intimação se refere a MAIS DE UM evento/ato do processo "
            f"(eventos {', '.join(str(e) for e in eventos_distintos)}) — considere TODOS "
            "eles em conjunto no seu resumo, não só o primeiro. Quando forem complementares "
            "(ex.: um despacho em duas partes, ou um despacho + a decisão que ele executa), "
            "diga isso explicitamente.",
        ]

    if documentos and so_principal:
        partes += [
            "",
            ("Documento identificado (por heurística de sigla) como o provável ATO PRINCIPAL "
             + ("de cada evento" if multi_evento else "deste evento") +
             " — o(s) evento(s) pode(m) ter outros documentos anexos, que NÃO foram lidos "
             "ainda (só se ler o(s) principal(is) não bastar):"),
        ]
        for doc in documentos:
            texto_doc = (doc.get("texto") or "").strip()
            cabecalho = (f"Evento {doc['evento_numero']} — " if doc.get("evento_numero") else "")
            partes.append(f"\n--- {cabecalho}sigla \"{doc.get('sigla', '')}\" ({doc.get('arquivo', '')}) ---")
            partes.append(texto_doc[:12000] if texto_doc else "(texto não pôde ser extraído deste documento)")
        partes += [
            "",
            "Se esse(s) documento(s), sozinho(s), for(em) suficiente(s) pra você entender do "
            "que se trata e o que está sendo pedido/determinado, resuma normalmente "
            "(instruções no final). Mas se algum deles remeter explicitamente ao conteúdo de "
            "um anexo que você precisaria ler pra responder com segurança (ex.: \"conforme "
            "documento anexo\", texto incompleto/cortado, ou referência a dados que só "
            "estariam no anexo), NÃO adivinhe: responda SOMENTE com o marcador "
            f"\"{_MARCADOR_PRECISA_ANEXOS}\" seguido de 1 frase explicando por quê, e nada "
            "mais — o sistema vai buscar os anexos de TODOS os eventos referenciados e pedir "
            "de novo.",
        ]
    elif documentos:
        partes += [
            "",
            (f"O(s) evento(s) referenciado(s) tem(êm) {len(documentos)} documento(s) ao todo, "
             "cada um mostrado abaixo separadamente pela SIGLA/nome oficial do e-proc"
             + (", agrupado por evento" if multi_evento else "") + ":"),
        ]
        for i, doc in enumerate(documentos, start=1):
            sigla = doc.get("sigla") or doc.get("arquivo") or f"documento {i}"
            texto_doc = (doc.get("texto") or "").strip()
            cabecalho = (f"Evento {doc['evento_numero']} — " if doc.get("evento_numero") else "")
            partes.append(f"\n--- {cabecalho}Documento {i}: {sigla} ({doc.get('arquivo', '')}) ---")
            partes.append(texto_doc[:10000] if texto_doc else "(texto não pôde ser extraído deste documento)")
        partes += [
            "",
            "PRIMEIRO passo (obrigatório, faça isso antes de resumir): identifique, entre os "
            "documentos acima, qual é o ATO/DOCUMENTO PRINCIPAL de cada evento (o que "
            "efetivamente gera a intimação) e quais são meros ANEXOS/documentos acessórios "
            "(comprovantes, formulários, provas, declarações de apoio).",
            "",
            _CATALOGO_SIGLAS_ATO_PRINCIPAL,
            "",
            "Baseie o resumo NO CONTEÚDO DO(S) DOCUMENTO(S) PRINCIPAL(IS) — os acessórios "
            "servem só de contexto complementar, nunca de substituto. Sempre verifique de "
            "fato o(s) documento(s) a que a intimação se refere antes de responder.",
        ]
    else:
        partes += ["", "(Texto integral do ato não pôde ser obtido — baseie-se só no rótulo/dados "
                       "acima; se isso for insuficiente para um resumo confiável, diga isso "
                       "explicitamente em vez de inventar conteúdo.)"]

    if andamentos_posteriores:
        partes += [
            "",
            "ANDAMENTOS DO PROCESSO POSTERIORES AO(S) EVENTO(S) REFERENCIADO(S) ACIMA (só "
            "número/data/rótulo de cada evento seguinte, SEM o conteúdo dos documentos — "
            "verificação padrão pra não ignorar movimentação relevante que tenha acontecido "
            "DEPOIS do ato que gerou a intimação, ex.: uma decisão posterior que supera/"
            "retifica o que foi noticiado, ou uma petição que já resolveu o que se pedia). "
            "Se notar algo relevante aqui, mencione EXPLICITAMENTE no resumo (como um ponto "
            "a mais, depois do prazo) — inclusive se isso significar que a intimação já "
            "perdeu o objeto ou que a situação real é diferente da aparente:",
            andamentos_posteriores,
        ]

    partes += [
        "",
        "Responda em português, texto corrido (SEM markdown, SEM JSON), no máximo 12 linhas, "
        "cobrindo NESTA ORDEM:",
        "1) qual documento você identificou como o ATO PRINCIPAL de cada evento referenciado "
        "(cite a sigla) e do que ele trata (mesmo sem certeza absoluta, diga qual você "
        "escolheu e por quê em 1 frase);",
        "2) o que o juízo/a parte contrária está efetivamente pedindo/determinando, com base "
        "no conteúdo real do(s) documento(s) principal(is) — não invente além do que está "
        "escrito;",
        "3) o prazo (quantos dias e a data final, se souber);",
        "4) SE (e só se) você notou algo relevante nos andamentos posteriores acima, diga o "
        "quê e por que muda (ou não) a resposta cabível.",
    ]
    return "\n".join(partes)


def _montar_texto_andamentos_posteriores(timeline: list[dict], eventos_referenciados: list[str],
                                          limite: int = 40) -> str:
    """Lista compacta (número/data/rótulo/autor) dos eventos do processo
    POSTERIORES ao(s) evento(s) referenciado(s) pela intimação — pedido
    explícito do usuário (2026-08-29): verificar por padrão se algo mudou a
    situação depois do ato que gerou a intimação, não só o próprio ato.

    `timeline`: já em ordem cronológica ASCENDENTE (`_timeline_completa`).
    Sem conteúdo de documento (caro/desnecessário pra maioria dos casos) —
    só o suficiente pra IA perceber um andamento "estranho" e sinalizar.
    `limite`: teto de eventos mostrados (do mais RECENTE pra trás) pra não
    estourar o prompt em processos com timeline gigante — quando corta,
    avisa explicitamente (nunca corta em silêncio)."""
    try:
        maior_ref = max(int(n) for n in eventos_referenciados if str(n).strip().isdigit())
    except ValueError:
        return ""

    def _num(ev: dict) -> int:
        try:
            return int(ev.get("numero") or 0)
        except (TypeError, ValueError):
            return 0

    posteriores = [ev for ev in timeline if _num(ev) > maior_ref]
    if not posteriores:
        return "(nenhum — o(s) evento(s) referenciado(s) é(são) o(s) mais recente(s) do processo)"

    aviso = ""
    if len(posteriores) > limite:
        aviso = (f"\n(mostrando só os {limite} mais recentes de {len(posteriores)} eventos "
                 "posteriores — processo com bastante movimentação desde então)")
        posteriores = posteriores[-limite:]

    linhas = []
    for ev in posteriores:
        desc = (ev.get("descricao") or "").strip()
        if len(desc) > 160:
            desc = desc[:160] + "…"
        ator = ev.get("ator_tipo") or ""
        linhas.append(f"- Evento {ev.get('numero', '?')} ({ev.get('data_hora', '')}): {desc}"
                      + (f" [autor: {ator}]" if ator else ""))
    return "\n".join(linhas) + aviso


async def analisar_processo(processo: str, ato: str, prazos_do_ato: list[dict], log_cb: LogCb) -> dict:
    """Etapa (a): identifica o evento de intimação referenciado por ESTE ATO
    (`prazos_do_ato` — ver `intimacao_service._agrupar_prazos_por_ato`;
    triagem MCP, leitura) e pede à IA um resumo objetivo — do que trata, o
    que o juízo pede, prazo. Só leitura/preparo; não decide nada e não toca
    no PAJ. `ato` identifica o sub-caso pra `salvar_estado`/`chave_caso` —
    nunca mais "o processo inteiro", ver módulo `intimacao_service`."""
    chave = intimacao_service.chave_caso(processo, ato)
    log_cb("info", f"Consultando a triagem do processo {processo} no e-proc (MCP, leitura)…")
    intimacao_service.salvar_estado(chave, status_geral="analisando", erro=None)
    try:
        casamento = await _casar_evento_referenciado(processo, prazos_do_ato)
    except Exception as e:  # noqa: BLE001
        msg = f"Falha ao consultar a triagem do e-proc: {e}"
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    evento_ref = None
    documentos: list[dict] = []
    if casamento:
        prazo, it = casamento["prazo"], casamento["intimacao"]
        # `evento_referenciado`, no retorno do MCP (`triagem_processo`), é um
        # DICT resolvido ({numero, data_hora, descricao, usuario, ator_tipo,
        # documentos} — ou {numero, fora_da_janela, dica} se fora da janela de
        # eventos carregada), NUNCA um número solto. Bug real corrigido: usar
        # o dict inteiro como se fosse o número do evento quebrava tanto a
        # busca do texto do ato (`_documentos_evento` não achava o evento) quanto o
        # nome do arquivo da minuta (virava o repr do dict inteiro -> OSError
        # "Invalid argument" no Windows, por causa de ':'/'{'/'\\' no nome).
        er = it.get("evento_referenciado")
        # `er` (dict) traz também `descricao`/`ator_tipo` do evento REFERENCIADO
        # em si (ex.: "PETIÇÃO - Refer. ao Evento: 20", autor "procurador") —
        # diferente de `rotulo` (label da INTIMAÇÃO, ex.: "Expedida/certificada
        # a intimação eletrônica"). Sem isso a IA não sabia nem que TIPO de
        # evento estava lendo, só o rótulo genérico da intimação.
        eid = (er.get("numero") if isinstance(er, dict) else er) or it.get("evento_intimacao")
        evento_ref = {
            "evento_intimacao": it.get("evento_intimacao"),
            "evento_referenciado": eid,
            "evento_descricao": er.get("descricao", "") if isinstance(er, dict) else "",
            "evento_ator_tipo": er.get("ator_tipo", "") if isinstance(er, dict) else "",
            "rotulo": it.get("rotulo", ""),
            "data_hora": it.get("data_hora", ""),
            "parte_intimada": it.get("parte_intimada", ""),
            "prazo_dias": prazo.get("prazo_dias"),
            "final_prazo": prazo.get("final_prazo") or prazo.get("final_estimado") or "",
            # Litisconsórcio — outra(s) parte(s) ASSOCIADAS A MIM (ver
            # `eproc_procurador_service`) intimadas no MESMO evento/dia/prazo
            # que este (ver `_casar_evento_referenciado`), cada uma com seu
            # PRÓPRIO nº de evento de intimação que também precisa ser
            # fechado. Consumido só por "Dar mera ciência"
            # (`mera_ciencia_service.dar_mera_ciencia`) pra marcar TODOS os
            # checkboxes de prazo do processo, não só o do 1º nome — o
            # resumo/IA em si continua sobre o mesmo conteúdo (mesmo ato, só
            # a parte muda).
            "evento_intimacao_coautores": [
                co.get("evento_intimacao") for co in (casamento.get("outros_coautores") or [])
                if co.get("evento_intimacao")
            ],
        }
        log_cb("info", f"Intimação casada: {evento_ref['rotulo']} (evento {eid}"
                       + (f" — {evento_ref['evento_descricao']}" if evento_ref["evento_descricao"] else "")
                       + ").")
        if evento_ref["evento_intimacao_coautores"]:
            log_cb("muted", "Litisconsórcio ativo detectado — outro(s) coautor(es) intimado(s) "
                            "no mesmo evento/prazo (evento(s) de intimação "
                            + ", ".join(evento_ref["evento_intimacao_coautores"]) + "); se for dada "
                            "mera ciência neste caso, todos serão fechados juntos.")
        instancia = casamento.get("instancia", 1)
        evento_ref["instancia"] = instancia

        # Evento(s) referenciado(s) — o e-proc admite MAIS DE UM ("Refer. aos
        # Eventos: N e M"); o MCP só resolve automaticamente o 1º em
        # `evento_referenciado`, os demais vêm em `outros_eventos_referenciados`
        # (número solto, sem descrição/ator resolvidos). Bug real corrigido
        # (2026-08-29, caso 5068227-80.2025.4.04.7000): ler só o 1º evento
        # quando a intimação se referia a 2 fazia o resumo (e toda a análise
        # depois dele) ignorar por completo o conteúdo do 2º.
        eventos_ref_nums: list[str] = [str(eid)] if eid else []
        for extra in it.get("outros_eventos_referenciados") or []:
            extra = str(extra).strip()
            if extra and extra not in eventos_ref_nums:
                eventos_ref_nums.append(extra)
        evento_ref["eventos_referenciados"] = eventos_ref_nums

        # Verificação PADRÃO de andamentos posteriores (pedido explícito do
        # usuário, mesmo caso real acima: a análise nunca olhava se algo
        # relevante aconteceu no processo DEPOIS do ato que gerou a
        # intimação). Só timeline/metadados (sem baixar/ler documento
        # nenhum) — barato o bastante pra rodar sempre, não só nos fluxos
        # especializados que já faziam algo parecido.
        andamentos_posteriores = ""
        eventos_por_numero: dict[str, dict] = {}
        if eventos_ref_nums:
            log_cb("info", "Conferindo se houve andamento no processo depois do(s) evento(s) "
                           f"referenciado(s) ({', '.join(eventos_ref_nums)})…")
            timeline = await _timeline_completa(processo, instancia)
            if timeline:
                eventos_por_numero = {str(ev.get("numero")): ev for ev in timeline}
                for num in eventos_ref_nums[1:]:
                    ev_extra = eventos_por_numero.get(num)
                    if ev_extra:
                        log_cb("info", f"Evento adicional referenciado: {num} — "
                                       f"{(ev_extra.get('descricao') or '')[:120]}")
                andamentos_posteriores = _montar_texto_andamentos_posteriores(timeline, eventos_ref_nums)
                if andamentos_posteriores and not andamentos_posteriores.startswith("(nenhum"):
                    log_cb("muted", "Há andamento(s) posterior(es) ao(s) evento(s) referenciado(s) "
                                    "— incluído no resumo pra IA avaliar a relevância.")
            else:
                log_cb("muted", "Não consegui carregar a timeline completa pra checar andamentos "
                               "posteriores — seguindo só com o(s) evento(s) referenciado(s).")

        if eventos_ref_nums:
            if len(eventos_ref_nums) > 1:
                log_cb("info", f"Intimação refere-se a {len(eventos_ref_nums)} eventos: "
                               f"{', '.join(eventos_ref_nums)} — lendo o principal de cada um.")
            log_cb("info", f"Baixando os documentos do(s) evento(s) {', '.join(eventos_ref_nums)}…")
            documentos_meta_por_evento: dict[str, list[dict]] = {}
            documentos_principais: list[dict] = []
            for num in eventos_ref_nums:
                meta = await _listar_documentos_evento(processo, num, instancia=instancia)
                documentos_meta_por_evento[num] = meta
                if not meta:
                    log_cb("muted", f"Evento {num}: nenhum documento encontrado.")
                    continue
                log_cb("ok", f"{len(meta)} documento(s) no evento {num}: "
                            + ", ".join(d["sigla"] for d in meta) + ".")
                # Tentativa 1 — SÓ o documento principal de cada evento
                # (heurística de sigla, sem abrir os demais ainda). Pedido
                # explícito do usuário: só avaliar os anexos se o(s)
                # principal(is) sozinho(s) não bastar(em) — evita OCR/leitura
                # desnecessária na maioria dos casos.
                idx_principal = _identificar_principal(meta)
                doc_principal = dict(meta[idx_principal])
                doc_principal["evento_numero"] = num
                if num == eventos_ref_nums[0]:
                    evento_ref["sigla_principal"] = doc_principal["sigla"]

                log_cb("info", f"Lendo o provável ato principal do evento {num} ({doc_principal['sigla']})…")
                doc_principal["texto"] = await _extrair_texto_documento(processo, doc_principal, log_cb)
                documentos_principais.append(doc_principal)

            if documentos_principais:
                prompt = _montar_prompt_resumo(processo, evento_ref, documentos_principais,
                                                so_principal=True, andamentos_posteriores=andamentos_posteriores)
                log_cb("info", "Pedindo à IA um resumo objetivo com base só no(s) ato(s) principal(is)…")
                resumo, erro = await _chamar_claude_texto(prompt, timeout=180)
                if erro:
                    log_cb("erro", f"Falha ao gerar o resumo: {erro}")
                    intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
                    return {"ok": False, "erro": erro}

                if resumo.strip().upper().startswith(_MARCADOR_PRECISA_ANEXOS):
                    log_cb("muted", f"O(s) ato(s) principal(is) sozinho(s) não bastou(aram) "
                                    f"({resumo.strip()[:200]}) — lendo também os documentos "
                                    "acessórios de todos os eventos referenciados…")
                    documentos_todos: list[dict] = []
                    for num in eventos_ref_nums:
                        meta = documentos_meta_por_evento.get(num) or []
                        idx_principal = _identificar_principal(meta) if meta else -1
                        ja_lido = next((d for d in documentos_principais if d.get("evento_numero") == num), None)
                        for i, doc in enumerate(meta):
                            doc = dict(doc)
                            doc["evento_numero"] = num
                            if i == idx_principal and ja_lido:
                                doc["texto"] = ja_lido.get("texto", "")  # já lido, reaproveita
                            else:
                                doc["texto"] = await _extrair_texto_documento(processo, doc, log_cb)
                            documentos_todos.append(doc)
                    prompt = _montar_prompt_resumo(processo, evento_ref, documentos_todos,
                                                    so_principal=False, andamentos_posteriores=andamentos_posteriores)
                    log_cb("info", "Pedindo à IA o resumo de novo, agora com todos os documentos…")
                    resumo, erro = await _chamar_claude_texto(prompt, timeout=240)
                    if erro:
                        log_cb("erro", f"Falha ao gerar o resumo (com anexos): {erro}")
                        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
                        return {"ok": False, "erro": erro}
            else:
                prompt = _montar_prompt_resumo(processo, evento_ref, [],
                                                andamentos_posteriores=andamentos_posteriores)
                log_cb("info", "Pedindo à IA um resumo objetivo da intimação…")
                resumo, erro = await _chamar_claude_texto(prompt, timeout=180)
                if erro:
                    log_cb("erro", f"Falha ao gerar o resumo: {erro}")
                    intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
                    return {"ok": False, "erro": erro}
        else:
            prompt = _montar_prompt_resumo(processo, evento_ref, [])
            log_cb("info", "Pedindo à IA um resumo objetivo da intimação…")
            resumo, erro = await _chamar_claude_texto(prompt, timeout=180)
            if erro:
                log_cb("erro", f"Falha ao gerar o resumo: {erro}")
                intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
                return {"ok": False, "erro": erro}
    else:
        log_cb("muted", "Não foi possível casar um evento de intimação específico (cache "
                        "desatualizado ou sem intimação correspondente) — o resumo usará só os "
                        "dados do Painel do Procurador.")
        prompt = _montar_prompt_resumo(processo, evento_ref, [])
        log_cb("info", "Pedindo à IA um resumo objetivo da intimação…")
        resumo, erro = await _chamar_claude_texto(prompt, timeout=180)
        if erro:
            log_cb("erro", f"Falha ao gerar o resumo: {erro}")
            intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
            return {"ok": False, "erro": erro}

    log_cb("ok", "Resumo da intimação pronto.")
    intimacao_service.salvar_estado(chave, resumo_intimacao=resumo, evento_referenciado=evento_ref)
    return {"ok": True, "resumo": resumo, "evento_referenciado": evento_ref}


# --------------------------------------------------------------------------
# (b) Classificação — SOMENTE com base nos atos do processo judicial (etapa
# (a), já pronta). Pedido explícito do usuário (2026-08-29): "não deveremos
# mais analisar o PAJ em nenhuma situação... toda a análise da intimação
# deverá ficar restrita aos atos do processo... se houver necessidade de
# analisar o PAJ isso será feito manualmente pelo defensor" — removida por
# completo a etapa de escopo/sincronização/análise de pretensão do PAJ que
# existia aqui antes (era o gargalo de tempo de cada análise).
# --------------------------------------------------------------------------

def _montar_prompt_confronto(processo: str, resumo_intimacao: str) -> str:
    return "\n".join([
        "Você é um assistente jurídico da Defensoria Pública da União. Vai "
        "classificar uma intimação judicial em UM dos 4 rótulos abaixo, com "
        "base SOMENTE no que a própria intimação e o(s) ato(s) do processo "
        "judicial a que ela se refere dizem — SEM acesso ao PAJ (processo "
        "administrativo de assistência jurídica) do assistido; se a "
        "classificação depender de algo que só o PAJ tem (histórico de "
        "pedidos, documentos do assistido, etc.), diga isso na justificativa "
        "e classifique como REQUER_ANALISE_HUMANA.",
        "",
        f"PROCESSO JUDICIAL: {processo}",
        *_contexto_tribunal(processo),
        "",
        "RESUMO DA INTIMAÇÃO (já cobre o(s) ato(s) referenciado(s) e qualquer "
        "andamento posterior relevante do processo):",
        resumo_intimacao or "(resumo não disponível)",
        "",
        "PADRÃO DE REDAÇÃO DA CASA (considere se sua justificativa mencionar peça a produzir):",
        INSTRUCAO_ESTILO_PECA_DPU[:2000],
        "",
        "Classifique em EXATAMENTE um destes 4 rótulos:",
        "- CUMPRIMENTO_IMEDIATO_CIENCIA: nada a fazer além de tomar ciência (ex.: mera "
        "intimação de ato, decisão que não exige resposta da DPU).",
        "- CUMPRIMENTO_IMEDIATO_PETICIONAR: o próprio ato/intimação já traz tudo que é "
        "preciso pra uma resposta simples e objetiva, sem precisar de nada do PAJ "
        "(ex.: manifestação sobre laudo cujo teor já está nos autos do processo, "
        "cumprimento de determinação simples do juízo).",
        "- DILIGENCIA_NECESSARIA: falta alguma informação/documento/contato do "
        "assistido que não está nos autos do processo judicial — precisa de um "
        "despacho pedindo essa diligência antes de poder peticionar.",
        "- REQUER_ANALISE_HUMANA: o caso foge do padrão (complexo, ambíguo, ou "
        "depende de conhecer o PAJ pra decidir com segurança) — só resumo, sem "
        "sugerir peça.",
        "",
        "Responda APENAS com um bloco JSON delimitado exatamente assim, sem nenhum "
        "texto antes ou depois:",
        "```json",
        '{"classificacao": "<UM_DOS_4_ROTULOS>", "justificativa": "<2 a 4 frases>", '
        '"diligencias": ["<item 1>", "..."], "destinatario_diligencia": '
        '"<cartorio|assessoria|divcom|assistido|>"}',
        "```",
        "`diligencias` e `destinatario_diligencia` só fazem sentido quando a "
        "classificação for DILIGENCIA_NECESSARIA — caso contrário, devolva lista vazia "
        "e string vazia.",
    ])


def _parse_confronto(saida: str) -> dict | None:
    m = re.search(r"\{.*\}", saida or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    classif = str(data.get("classificacao") or "").strip().upper()
    if classif not in intimacao_service.CLASSIFICACOES:
        return None
    return {
        "classificacao": classif,
        "justificativa": str(data.get("justificativa") or "").strip(),
        "diligencias": [str(d).strip() for d in (data.get("diligencias") or []) if str(d).strip()],
        "destinatario_diligencia": str(data.get("destinatario_diligencia") or "").strip(),
        # Mantido por compatibilidade de formato com os fluxos especializados
        # (que sempre gravam este campo como False) — não existe mais
        # escalonamento pro PAJ, então nunca é lido/usado de fato.
        "precisa_documentos": False,
    }


async def confrontar_com_paj(processo: str, ato: str, log_cb: LogCb) -> dict:
    """Etapa (b): classifica a intimação em 1 dos 4 rótulos com base SOMENTE
    no resumo do processo judicial (etapa (a)) — NUNCA sincroniza, lê ou
    analisa o PAJ (pedido explícito do usuário, 2026-08-29). O nome da
    função ficou por compatibilidade com o resto do pipeline (`rodar_
    pipeline` a chama por este nome), mas não confronta mais com nada do
    PAJ — só classifica o que o processo judicial, sozinho, já diz.

    `ato` identifica o sub-caso (ver módulo `intimacao_service`) — NUNCA
    movimenta/tramita/conclui nada, só lê e classifica ESTE ato."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    if not caso or not caso.get("paj_norm"):
        msg = "PAJ não vinculado — vincule manualmente antes de classificar."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}
    resumo_intimacao = caso.get("resumo_intimacao") or ""

    intimacao_service.salvar_estado(chave, escopo_verificacao={
        "modo": "somente_processo",
        "motivo": "Classificação baseada apenas nos atos do processo judicial — não "
                  "sincroniza nem analisa o PAJ (se necessário, o Defensor faz isso "
                  "manualmente).",
        "escalonado": False, "motivo_escalonamento": None,
    })

    prompt = _montar_prompt_confronto(processo, resumo_intimacao)
    log_cb("info", "Pedindo à IA a classificação da intimação (só com base no processo judicial)…")
    saida, erro = await _chamar_claude_texto(prompt, timeout=180)
    if erro:
        log_cb("erro", f"Falha na classificação: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    confronto = _parse_confronto(saida)
    if not confronto:
        msg = "A IA não devolveu um JSON de classificação válido."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    log_cb("ok", f"Classificação: {confronto['classificacao']}")
    intimacao_service.salvar_estado(chave, confronto=confronto, status_geral="aguardando_revisao", erro=None)
    return {"ok": True, "confronto": confronto}


# --------------------------------------------------------------------------
# (c) Geração de minuta (petição OU despacho)
# --------------------------------------------------------------------------

def _montar_prompt_peticao(processo: str, caso: dict) -> str:
    confronto = caso.get("confronto") or {}
    return "\n".join([
        "Redija uma MANIFESTAÇÃO/PETIÇÃO simples e objetiva da Defensoria Pública da "
        f"União em resposta à intimação abaixo, no processo {processo}.",
        *_contexto_tribunal(processo),
        "",
        "RESUMO DA INTIMAÇÃO:",
        caso.get("resumo_intimacao") or "",
        "",
        "JUSTIFICATIVA DA CLASSIFICAÇÃO (por que cabe resposta simples e objetiva):",
        confronto.get("justificativa") or "",
        "",
        INSTRUCAO_ESTILO_PECA_DPU,
        "",
        "Responda APENAS com o corpo da peça em HTML simples (parágrafos `<p>`, sem "
        "`<html>`/`<head>`/`<body>`), SEM endereçamento nem qualificação (isso é "
        "adicionado depois, automaticamente, pelo sistema) — comece direto pelo "
        "relatório/manifestação e termine com o pedido. Marque com colchetes `[ ]` "
        "qualquer dado que precise de conferência do Defensor antes do protocolo.",
    ])


def _montar_prompt_despacho(processo: str, caso: dict) -> str:
    confronto = caso.get("confronto") or {}
    destinatario = confronto.get("destinatario_diligencia") or "assessoria"
    diligencias = confronto.get("diligencias") or []
    return "\n".join([
        "Redija um DESPACHO INTERNO (documento de uso interno do PAJ, NÃO é peça "
        f"judicial) sobre a intimação abaixo, no processo {processo}, pedindo ao "
        "destinatário indicado a diligência necessária antes de se poder cumprir a "
        "intimação.",
        "",
        f"DESTINATÁRIO: {destinatario} (cartório / assessoria / DIVCOM / assistido)",
        *_contexto_tribunal(processo),
        "",
        "RESUMO DA INTIMAÇÃO:",
        caso.get("resumo_intimacao") or "",
        "",
        "O QUE FALTA (diligência necessária):",
        ("\n".join(f"- {d}" for d in diligencias) if diligencias else "(ver justificativa abaixo)"),
        "",
        "JUSTIFICATIVA:",
        confronto.get("justificativa") or "",
        "",
        "Regras de redação: objetivo e direto — o que falta, por que é necessário para "
        "cumprir a intimação, e para quem se dirige. NÃO é peça judicial (sem "
        "endereçamento ao juízo, sem \"termos em que pede deferimento\"). Marque com "
        "colchetes `[ ]` qualquer dado que precise de conferência do Defensor.",
        "",
        "Responda APENAS com o corpo do despacho em HTML simples (parágrafos `<p>`, "
        "sem `<html>`/`<head>`/`<body>`).",
    ])


async def _gerar_peticao(processo: str, paj_norm: str, pasta_paj: Path, caso: dict, log_cb: LogCb) -> dict:
    chave = caso["chave_caso"]

    log_cb("info", "Gerando minuta de manifestação/petição…")
    saida, erro = await _chamar_claude_texto(_montar_prompt_peticao(processo, caso), timeout=400)
    if erro:
        log_cb("erro", f"Falha ao gerar a petição: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    peticao_html = _extrair_bloco_html(saida)
    evento_ref = caso.get("evento_referenciado") or {}
    sufixo = _sufixo_arquivo_seguro(evento_ref.get("evento_referenciado"))
    nome = f"manifestacao_intimacao_{sufixo}"
    pasta_destino = pasta_paj / "Peças IA"
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome = _nome_minuta_livre(nome, pasta_destino)
    docx_path = pasta_destino / f"{nome}.docx"
    pdf_path = pasta_destino / f"{nome}.pdf"

    erro_docx = await _gerar_docx_pdf(peticao_html, docx_path, pdf_path)
    if erro_docx:
        msg = f"Falha ao formatar/converter a minuta: {erro_docx}"
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    minuta = {"tipo": "peticao", "caminho": str(docx_path), "gerado_em": datetime.now().isoformat(timespec="seconds")}
    log_cb("ok", f"Minuta de petição salva em: {docx_path}")
    intimacao_service.salvar_estado(chave, minuta=minuta, status_geral="aguardando_revisao")
    return {"ok": True, "minuta": minuta}


async def _gerar_despacho(processo: str, paj_norm: str, pasta_paj: Path, caso: dict, log_cb: LogCb) -> dict:
    chave = caso["chave_caso"]

    log_cb("info", "Gerando minuta de despacho (diligência necessária)…")
    saida, erro = await _chamar_claude_texto(_montar_prompt_despacho(processo, caso), timeout=300)
    if erro:
        log_cb("erro", f"Falha ao gerar o despacho: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    despacho_html = _extrair_bloco_html(saida)
    evento_ref = caso.get("evento_referenciado") or {}
    sufixo = _sufixo_arquivo_seguro(evento_ref.get("evento_referenciado"))
    nome = f"despacho_intimacao_{sufixo}"
    # Salvo SÓ em "Peças IA/" (regra do usuário 2026-09-28: toda minuta do
    # painel vai para lá). Antes ia para a raiz do PAJ + uma cópia em "Peças
    # IA" — a raiz existia por causa da aba "Despachos" (PREFIXOS_DESPACHO),
    # removida em 2026-09-18.
    pasta_destino = pasta_paj / "Peças IA"
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome = _nome_minuta_livre(nome, pasta_destino)
    docx_path = pasta_destino / f"{nome}.docx"
    pdf_path = pasta_destino / f"{nome}.pdf"

    erro_docx = await _gerar_docx_pdf(despacho_html, docx_path, pdf_path)
    if erro_docx:
        msg = f"Falha ao formatar/converter o despacho: {erro_docx}"
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    minuta = {"tipo": "despacho", "caminho": str(docx_path), "gerado_em": datetime.now().isoformat(timespec="seconds")}
    log_cb("ok", f"Minuta de despacho salva em: {docx_path}")
    intimacao_service.salvar_estado(chave, minuta=minuta, status_geral="aguardando_revisao")
    return {"ok": True, "minuta": minuta}


async def gerar_minuta(processo: str, ato: str, log_cb: LogCb) -> dict:
    """Etapa (c): gera a minuta — petição/manifestação (CUMPRIMENTO_IMEDIATO_
    PETICIONAR) ou despacho interno (DILIGENCIA_NECESSARIA) — SEMPRE como
    rascunho `.docx`+`.pdf` para revisão humana. Não gera nada para os outros
    2 rótulos (CIÊNCIA/REQUER_ANALISE_HUMANA). NUNCA protocola/movimenta/
    tramita/conclui — só formata e salva um arquivo em disco. `ato`
    identifica o sub-caso (ver módulo `intimacao_service`)."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    if not caso or not caso.get("confronto"):
        msg = "Confronto ainda não realizado."
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}
    classif = (caso["confronto"] or {}).get("classificacao")
    if classif not in ("CUMPRIMENTO_IMEDIATO_PETICIONAR", "DILIGENCIA_NECESSARIA"):
        log_cb("muted", f"Classificação {classif} não gera minuta — pipeline encerra aqui.")
        intimacao_service.salvar_estado(chave, status_geral="aguardando_revisao")
        return {"ok": True, "minuta": None}

    paj_norm = caso.get("paj_norm")
    if not paj_norm:
        msg = "PAJ não vinculado — impossível salvar a minuta."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}
    pasta_paj = Path(config.PAJS_DIR) / paj_norm

    if classif == "CUMPRIMENTO_IMEDIATO_PETICIONAR":
        return await _gerar_peticao(processo, paj_norm, pasta_paj, caso, log_cb)
    return await _gerar_despacho(processo, paj_norm, pasta_paj, caso, log_cb)


# --------------------------------------------------------------------------
# Fluxo DEDICADO: intimação de LAUDO PERICIAL médico
# --------------------------------------------------------------------------
#
# Casos muito frequentes (pedido explícito do usuário) e com um padrão
# próprio, diferente do confronto genérico (b)/minuta genérica (c): quando o
# ATO PRINCIPAL identificado na etapa (a) é o laudo médico pericial
# (sigla "LAUDOPERIC*"), este fluxo substitui INTEIRAMENTE o confronto e a
# minuta genéricos (ver `rodar_pipeline`). Não sincroniza o PAJ nem roda
# análise de pretensão — trabalha só com documentos do PROCESSO JUDICIAL
# (e-proc): busca na timeline COMPLETA a petição inicial, eventuais petições
# intercorrentes com fundamentos de incapacidade e o(s) laudo(s) pericial(is)
# (original + complementar, se houver), e manda tudo pro Claude com o
# prompt EXATO que o usuário já usa no Project pessoal dele pra isso — o
# próprio prompt já classifica o caso (Tipo 1/2/3) e redige a peça certa
# (ciência simples, impugnação focada em mudar a DII, ou impugnação
# completa), então não há JSON de classificação separado aqui: a peça É a
# saída, sempre.

_PREFIXO_SIGLA_LAUDO_PERICIAL = "LAUDOPERIC"


def _eh_sigla_laudo_pericial(sigla: str) -> bool:
    return (sigla or "").strip().upper().startswith(_PREFIXO_SIGLA_LAUDO_PERICIAL)


# Prompt do Project pessoal do usuário para impugnação de laudo médico
# pericial — GUARDADO IPSIS LITERIS, por pedido explícito ("o ideal é que
# este prompt seja guardado ipsis literis... não gostaria que fosse
# resumido"). Qualquer ajuste futuro no TEXTO em si deve vir do usuário —
# quem mexe no código só deve alterar como ele é MONTADO/enviado (ver
# `_montar_prompt_impugnacao_laudo`), nunca o conteúdo abaixo.
PROMPT_IMPUGNACAO_LAUDO_PERICIAL_BI = '''#PERSONA
Você é um Defensor Público Federal que atua na área de Direito Previdenciário, especialista em processos envolvendo o INSS e os benefícios do Regime Geral de Previdência Social (Lei 8213/91; Lei 8212/91).

#CONTEXTO
Você está atuando num processo judicial de concessão de benefício por incapacidade, para o qual vige a fungibilidade entre os benefícios (auxílio-doença; aposentadoria por invalidez e auxílio-acidente).
Neste momento processual, foi realizada a perícia médica judicial no processo e é necessário analisar o laudo do perito médico da Justiça Federal para entender suas conclusões sobre a existência, ou não, de incapacidade do(a) autor(a) para o trabalho habitual.

#TAREFA
Analise o laudo médico pericial que será anexado para entender o tipo de petição jurídica que deverá ser gerada.

##REQUISITOS DE VALIDADE DO LAUDO JUDICIAL
Verifique se o laudo judicial atente aos requisitos abaixo:
a) Especialização do Perito: a perícia deve ser realizada preferencialmente por médico com a especialidade escolhida pelo autor e indicada na petição inicial. Embora não seja obrigatória a observância da especialidade, se houver elementos que indiquem prejuízo na avaliação do quadro, pode ser solicitado ao juiz uma nova perícia com observância da especialidade requerida.
b) Fundamentação adequada: perícias sem fundamentação adequada, que não analisam a documentação médica ou que ignoram aspectos essenciais da incapacidade podem ser declaradas nulas.
c) Data de Início da Incapacidade: quando existente, a perícia deve determinar a Data de Início da Incapacidade (DII) e, se o objeto do processo envolver análise de incapacidade pretérita, o perito deve informar se ela existiu justificadamente

##CLASSIFICAÇÃO DO CASO
-Tipo 1: Laudo favorável, que reconhece início da incapacidade (DII) em data igual ou anterior ao requerimento administrativo (DER), de forma compatível com aquilo que foi arguido na petição inicial.
Neste caso, gere uma petição simples de mera ciência às conclusões do laudo médico judicial, suscinta, sem necessidade de repetição de fundamentos.

-Tipo2: Laudo favorável, mas que indica uma incapacidade pré-existente. Neste caso, o laudo afirma que existe incapacidade, mas a DII indicada é anterior ao início das contribuições ou ao reingresso do segurado no RGPS. Estes casos podem ser identificados quando o motivo do indeferimento administrativo é "Data do Início da Doença - DID - anterior ao ingresso ou reingresso ao RGPS", ou qualquer outro que informe que a incapacidade é anterior ao ingresso ou reingresso no Regime Geral de Previdência Social (RGPS).
Se estivermos diante dessa situação, a análise das provas deve ser especificamente voltada a identificar elementos para modificar a data de início da incapacidade (DII) para uma data futura em que o autor/segurado tenha qualidade de segurado e carência. Ainda que já existisse uma doença, é necessário verificar a possibilidade de sustentar que a incapacidade ocorreu posteriormente ao início da doença (DID), em razão de algum agravamento do quadro clínico.
Se não houver informações suficientes na petição inicial sobre a qualidade de segurado, você poderá tentar identificar isso no CNIS do autor que sempre é anexado ao processo judicial, verificando se a DII ocorreu muito antes do ingresso ou do reingresso no RGPS.

-Tipo 3. Laudo totalmente desfavorável, que conclui não existir incapacidade laboral.
Neste caso, gere uma petição de impugnação sustentando que o autor se encontra incapacitado para o trabalho habitual desde o requerimento administrativo. Adote fundamentos da petição inicial. Aborde os pontos seguintes no texto, em parágrafos separados, seguindo a ordem indicada:
a) síntese da conclusão pericial, informando qual é o diagnóstico mencionado na perícia, indicando o nome das doenças (em negrito itálico) seguidas do CID (entre parenteses, sem formatação). Informar se foi reconhecida ou não a incapacidade atual ou pretérita para o trabalho do autor. Se houver período de incapacidade reconhecido, mencionar expressamente a data de início e de fim indicadas pelo perito (os períodos devem estar em negrito). Se for reconhecida a existência de alguma sequela consolidada decorrente de acidente, mencionar isso (se não for reconhecida sequela, não cite nada a respeito).
b) qual era a profissão do autor (em negrito sublinhado), com breve descrição das atividades inerentes a essa profissão, conforme tópico ##AnalisarProfissão.
c)  indicar elementos do caso concreto que indiquem maiores dificuldades de exercer a profissão em virtude de condições pessoais, sociais, econômicas. Sempre ressaltar as limitações que o perito afirmou existirem para o trabalho, principalmente quando elas prejudicam a realização das tarefas habituais e ou a empregabilidade do autor.
d) se houver diagnóstico de CID B24, considerar o entendimento da Súmula 78 da TNU que diz o seguinte: "Comprovado que o requerente de benefício é portador do vírus HIV, cabe ao julgador verificar as condições pessoais, sociais, econômicas e culturais, de forma a analisar a incapacidade em sentido amplo, em face da elevada estigmatização social da doença."  Nesses casos, portanto, fazer uma análise considerando a situação do tratamento médico do autor, se há efeitos colaterais que podem ser debilitantes, e se há indícios de algum estigma ou segregação profissional que limitem a inserção no mercado de trabalho.
e) quando não houver reconhecimento de incapacidade laboral pelo perito, mas houver indicação de uma sequela consolidada decorrente de acidente, sustentar que, de forma subsidiária ao pedido de benefício por incapacidade temporária (auxílio-doença) ou aposentadoria por invalidez (aposentadoria por incapacidade permanente), é cabível a concessão do benefício de auxílio-acidente, conforme artigo 86 da Lei 8213/1991.
f) avalie se a incapacidade é temporária ou permanente e se o segurado pode ser reabilitado para outra função. Caso haja possibilidade de reabilitação, pedir que o benefício seja mantido até que seja reabilitado;  Caso não haja possibilidade de reabilitação, requerer concessão da aposentadoria por incapacidade permanente.

##AnalisarProfissão
Neste tópico, é importante deixar clara a profissão do autor, as tarefas exercidas e as condições de desempenho desse trabalho habitual em face das limitações médicas comprovadas.
Identificar a última profissão declarada pelo autor.
Entender que tipo de tarefas, movimentos, esforços e limitações ele estava submetido no exercício dessa ultima profissão.
Fazer um cotejo entre as limitações médicas existentes e a capacidade de continuar exercendo a profissão, sem dores, dificuldades e violação à dignidade do autor.
Quando o histórico mencionar apenas profissões braçais, que exijam esforços físicos, e o autor já tiver idade avançada e diagnóstico de doenças que prejudicam essas atividades, reforçar essa condição que dificulta o reingresso no mercado de trabalho.

#FORMATO
Petição jurídica, com caracteristica concisa e objetiva, em texto corrido, seguindo a estrutura indicada no tópico #ESTRUTURA_DA_PETIÇÃO.
Ao citar doenças, sempre colocar em negrito e itálico, seguidas do CID entre parenteses sem nenhuma formataçao.
Se for utilzar marcadores, adote o padrão numérico (1., 2., 3., etc.) com subtópicos (1.1, 1.2, 2.1, 2.2.), e alíneas (a, b, c). Não utilize marcadores com bullets (•).

#ESTILO DE ESCRITA
Não utilizar pessoalização no texto. Evitar citar nomes de juízes, advogados, auxiliares do juízo como peritos e assistentes sociais, ou quaisquer atores do processo. Opte por citar sempre de maneira impessoal "o juízo decidiu que"; "a perícia concluiu..."; "o laudo pericial..."; " a avaliação social indicou que ..." etc.
Não precisa citar as datas em que os eventos ocorreram como perícia, petição, contestação. As datas são irrelevantes neste momento.
Ao criar parágrafos, depois de citar algum ato realizado no processo, prefira utilizar sujeito oculto nas sentenças seguintes para evitar repetições.  Dessa forma, a escrita deverá ficar mais profissional e fluida.

#FLUIDEZ E COESÃO TEXTUAL
Elaborar o texto de forma coesa e fluida, evitando estruturas rígidas ou mecânicas. Utilizar conectivos adequados para criar transições naturais entre os parágrafos. Sempre que possível, fazer referência cruzada com outros elementos probatórios dos autos (perícia médica, documentos médicos, etc.) para fortalecer a argumentação. Priorizar linguagem jurídica técnica, usando expressões como "restou confirmado", "restou demonstrado", "evidenciou-se", em lugar de construções mais simples. Evitar repetições desnecessárias e manter o texto enxuto, preservando apenas as informações essenciais para a fundamentação do pedido.

#EVITAR LINGUAGEM TÉCNICO-MÉDICA
Ao produzir minutas, os sintomas, limitações e histórico clínico devem ser narrados em linguagem natural, compreensível ao cidadão comum ou ao operador do Direito. Afinal, a petição é direcionado ao juiz, e não ao médico / perito.
Evitar uso repetitivo de CIDs, nomenclatura radiológica detalhada, nomes técnicos de exames e medicamentos, salvo quando estritamente necessário.
A terminologia técnico-médica só se justifica em situações como: (1) quando delimita a controvérsia (ex.: se determinado achado relevante existe ou não nos autos); ou (2) quando o detalhe técnico reforça diretamente um argumento jurídico (ex.: comprovar nexo causal, cronologia, ou objetividade de um achado específico).
Nas minutas deve prevalecer o uso de linguagem mais voltada ao cidadão comum com formação jurídica, a fluidez do relato e a força argumentativa.


#ESTRUTURA_DA_PETIÇÃO
**EXCELENTÍSSIMO(A) JUIZ(A) FEDERAL**

**NOME DO AUTOR**, qualificado(a) nos autos, representado(a) pela Defensoria Pública da União, comparece, perante Vossa Excelência, para se manifestar sobre a perícia médica realizada.

[Inserir o texto da manifestação que você deve criar]

Diante do exposto, requer seja reconhecida a incapacidade laboral do(a) autor(a) e **julgado procedente** o pedido de concessão/restabelecimento, nos termos requeridos na petição inicial.

Curitiba, [data por extenso]

Felipe Belache Kugler
Defensor Público Federal'''


# Prompt do Project pessoal do usuário para impugnação de laudo médico
# pericial em casos de BENEFÍCIO ASSISTENCIAL (BPC/LOAS) — GUARDADO IPSIS
# LITERIS, mesmo tratamento do prompt de BI acima (`PROMPT_IMPUGNACAO_
# LAUDO_PERICIAL_BI`). Qual dos dois é usado em cada caso é decidido pela
# etapa de classificação (`_classificar_tipo_beneficio`), que lê o PEDIDO da
# petição inicial — nunca os dois textos são enviados juntos/misturados.
PROMPT_IMPUGNACAO_LAUDO_PERICIAL_BPC = '''#PERSONA Você é um Defensor Público Federal que atua, especificamente, em processos envolvendo os Benefício Assistencial ao Idoso e ao Deficiente, regulado normativamente pela Constituição Federal (art. 203, V, CF88), pela Lei 8742/93 (artigos 20 a 21-B da SEÇÃO I - Do Benefício de Prestação Continuada), e pelo Decreto nº 6.214, de 2007.

#CONTEXTO Considerar que, no bojo de um processo judicial de concessão de benefício assistencial, é necessário analisar o laudo da perícia médica produzida em juízo. A partir da análise, será necessário enfatizar elementos que demonstrem o enquadramento do autor no critério de deficiência previsto pela legislação (art. 20,§2º, Lei 8742/93), que afirma o seguinte: "considera-se pessoa com deficiência aquela que tem impedimento de longo prazo de natureza física, mental, intelectual ou sensorial, o qual, em interação com uma ou mais barreiras, pode obstruir sua participação plena e efetiva na sociedade em igualdade de condições com as demais pessoas. " A legislação ainda exige que se trate de uma deficiência de longo prazo, caracterizada como aquela com duração de pelo menos 2 anos, nos termos do § 11º do mesmo artigo 20, que assim está redigido: "§ 10. Considera-se impedimento de longo prazo, para os fins do § 2o deste artigo, aquele que produza efeitos pelo prazo mínimo de 2 (dois) anos. " Portanto, para os fins de enquadramento no critério deficiência de longo prazo, faz-se necessário identificar as restrições médicas na perícia judicial , a fim de identificar no caso concreto as limitações existentes para reinserção do autor em novos trabalhos e para competir no mercado de trabalho em igualdade de condições com as demais pessoas saudáveis, bem como para verificar se é possível sustentar que tal quadro de deficiência tem duração de longo prazo, isto é, pelo menos 2 anos. Além disso, considerando que a avaliação da deficiência deve ser feita pelo critério biopsicossocial, é também necessário avaliar as demais barreiras pessoais, sociais, culturais e econômicas por meio de avaliação social, conforme prevê o art. 20,§6º, que afirma que "A concessão do benefício ficará sujeita à avaliação da deficiência e do grau de impedimento de que trata o § 2o, composta por avaliação médica e avaliação social".
Então, para caracterização da deficiência, devem ser avaliados, além das restrições médicas, também fatores outros que constituem barreiras para efetiva participação social e competição no mercado de trabalho. Busque a avaliação social judicial (LAUDO_SOC_ECON1 ), se houver, para aferição das informações. Se houver, em complemento, busque as informações das avaliações sociais administrativas do INSS (se houver, estará juntada no processo com o nome "Avaliação Social Detalhada") ou da DPU (se houver, estará juntada na inicial).

# ENTENDIMENTO JURISPRUDENCIAL APLICÁVEL
A Turma Nacional de Uniformização definiu no TEMA385/TNU parâmetros para a avaliação biopsicossocial nos processos que contém pretensão de concessão de benefício assistencial (bpc/LOAS):  O tema firmou os seguintes parâmetros: "1. Para fins de concessão do benefício de prestação continuada (BPC/LOAS), a caracterização da deficiência não exige a demonstração de incapacidade para o trabalho. O requisito legal é o impedimento de longo prazo de natureza física, mental, intelectual ou sensorial, entendido como alteração ou perda significativas na função ou estrutura do corpo, que, em interação com barreiras, obstrua a participação plena e efetiva do requerente na sociedade em igualdade de condições com as demais pessoas, devendo sua aferição ser realizada por meio de avaliação biopsicossocial. 2. Como medida de economia processual, o julgador poderá adotar os seguintes parâmetros: (i) a constatação judicial, com base em perícia médica, de inexistência de impedimento, de impedimento de grau leve ou de impedimento passível de resolução em menos de dois anos dispensa a realização de avaliação biopsicossocial, por afastar pressuposto necessário à caracterização da deficiência; (ii) a constatação de incapacidade total de longo prazo para qualquer atividade laborativa gera presunção relativa (iuris tantum) de deficiência, dispensando a avaliação social, ressalvada a possibilidade de o julgador afastá-la diante de elementos concretos que indiquem ausência de barreiras relevantes no caso específico; (iii) a constatação de impedimento de grau moderado ou grave sem incapacidade total de longo prazo exige a realização da avaliação biopsicossocial, com análise, feita por assistente social, dos fatores ambientais, das limitações de atividade e das restrições de participação social, a qual não se confunde com a análise de miserabilidade exigida para o segundo requisito do BPC."

#TAREFA

##VERIFICAR CONCLUSÃO DO LAUDO MÉDICO Analise e compreenda o laudo médico pericial que será anexado.
Tipo1 - BPC) Se a conclusão pericial for favorável, ou seja, se reconhecer que existe deficiência de longo prazo nos termos do art. 20,§2º, Lei 8742/93, gere uma minuta simples concordando com a conclusão pericial e requeira o prosseguimento do feito com acolhimento dos pedidos da inicial. Use o modelo de petição de mera ciência anexado ao projeto (Petição MODELO mera ciência do laudo médico.pdf). Após, encerre a tarefa, sem precisar avançar para os outros pontos.
Tipo2 - BPC) Se a conclusão pericial for desfavorável, negando a existência de deficiência de longo prazo nos termos do art. 20,§2º, Lei 8742/93, gere uma minuta de impugnação, com observância das orientações abaixo. Se existir avaliação social (LAUDO_SOC_ECON1 ), analise-a para identificar barreiras que impeçam o autor de competir por emprego em igualdade de condições com as demais pessoas no mercado de trabalho.
Caso tenha sido anexada a petição inicial do caso, analise-a para ter uma ideia das alegações adotadas pela Defensoria Pública em relação à deficiência de longo prazo. Se pertinente, repita, transcreva ou cite argumentos já lançados na petição inicial. Após analisar os documentos disponibilizados, gere um texto bem fundamentado sustentando que o autor se enquadra no critério legal de pessoa portadora de deficiência para fins de concessão do Benefício Assistencial, com enfoque na perspectiva biopsicossocial. Não crie subtópicos na peça, exceto se for expressamente solicitado ou necessário para melhor desenvolvimento da tese. Aborde os seguintes pontos no texto, em parágrafos separados, seguindo a ordem indicada: a) síntese da conclusão pericial, informando qual é o diagnóstico mencionado na perícia, indicando o nome das doenças (em negrito itálico) seguidas do CID (entre parenteses, sem formatação). Informar se foi reconhecida ou não a existência de deficiência de longo prazo. Se houver reconhecimento da deficiência, mencionar a data de início e a duração estimada pelo perito judicial. b) identificar qual era a profissão do autor e quais experiências profissionais ele já teve, a fim de identificar as potencialidades que possui para retornar ao mercado de trabalho. c) identificar as restrições que o autor possui em relação a condição econômica, condição social, escolaridade, idade avançada, doenças com potencial incapacitante, dependência de terceiros, acesso a serviços públicos entre outras barreiras que possam interferir em sua plena participação na sociedade em igualdade de condições com as demais pessoas d) elaborar fundamentação com todos os elementos disponíveis para afirmar que o autor apresenta deficiência de longo prazo e) Sempre ressaltar as limitações que o perito informou no laudo pericial, principalmente quando elas prejudicam a realização das tarefas habituais, a empregabilidade do autor, geram mais dependência ou gastos para o núcleo familiar. f) quando houver um elevado grau da deficiência , ressaltar essa informação na petição g) quando houver dependência do autor em relação a terceiros para o desempenho de atividades básicas da vida diária, ressaltar essa informação na petição h) quando a deficiência tiver potencial de elevados gastos e comprometimento do orçamento familiar com medicamentos, tratamentos, exames frequentes , itens de higiene , enfermagem, curativos , entre outros, sempre ressaltar isso na petição.

##CASOS ESPECÍFICOS
a) Portadores de HIV: se houver diagnóstico de CID B24, considerar o entendimento da Súmula 78 da TNU que diz o seguinte: "Comprovado que o requerente de benefício é portador do vírus HIV, cabe ao julgador verificar as condições pessoais, sociais, econômicas e culturais, de forma a analisar a incapacidade em sentido amplo, em face da elevada estigmatização social da doença."  Nesses casos, portanto, fazer uma análise considerando a situação do tratamento médico do autor, se há efeitos colaterais que podem ser debilitantes, e se há indícios de algum estigma ou segregação profissional que limitem a inserção no mercado de trabalho.

b) Portadores de TEA: se houver diagnóstico de transtorno do espectro autista (TEA), considerar o entendimento do Tema 376 da TNU que diz o seguinte: "Na análise do direito ao benefício de prestação continuada, a caracterização da deficiência da pessoa com transtorno do espectro autista exige avaliação biopsicossocial, sendo insuficientes o diagnóstico médico deste impedimento ou a perícia exclusivamente médica."  Nesses casos, portanto, fazer uma análise ampla das limitações médicas e das barreiras pessoais, sociais, econômicas , bem como do impacto que a doença causa para o autor no âmbito familiar (dependência, gastos, tratamentos multidisciplinares), no ambiente escolar, na socialização  e inclusão social.

c) Portadores de visão monocular: se houver diagnóstico de visão monocular, considerar o entendimento do Tema 378 da TNU que diz o seguinte: "Na análise do direito ao benefício de prestação continuada, a caracterização da deficiência da pessoa com visão monocular exige avaliação biopsicossocial, sendo insuficientes o diagnóstico do impedimento visual ou a perícia exclusivamente médica."  Nesses casos, portanto, fazer uma análise ampla das limitações médicas causadas pela visão monocular, das profissões já exercidas,  e das barreiras pessoais, sociais, econômicas , bem como do impacto que a doença causa para o autor na recolocação no mercado de trabalho e nas atividades diárias, bem como no ambiente familiar.


#FORMATO Petição jurídica, com caracteristica concisa e objetiva, em texto corrido, seguindo a estrutura indicada no tópico ##ESTRUTURA_DA_PETIÇÃO. Ao citar doenças, sempre colocar em negrito e itálico, seguidas do CID entre parenteses sem nenhuma formataçao. Se for utilzar marcadores, adote o padrão numérico (1., 2., 3., etc.) com subtópicos (1.1, 1.2, 2.1, 2.2.), e alíneas (a, b, c). Não utilize marcadores com bullets (•).

##ESTRUTURA_DA_PETICAO

EXCELENTÍSSIMO(A) JUIZ(A) FEDERAL

NOME DO AUTOR, qualificado(a) nos autos, representado(a) pela Defensoria Pública da União, comparece, perante Vossa Excelência, para se manifestar sobre a perícia médica realizada.

[Inserir o texto da manifestação que você deve criar]

Diante do exposto, requer seja reconhecido o enquadramento do(a) autor(a) como pessoa deficiente de longo prazo e julgado procedente o pedido para concessão/restabelecimento do benefício assistencial, nos termos requeridos na petição inicial.

Curitiba, [data por extenso]

Felipe Belache Kugler

Defensor Público Federal


#ESTILO DE ESCRITA Não utilizar pessoalização no texto. Evitar citar nomes de juízes, advogados, auxiliares do juízo como peritos e assistentes sociais, ou quaisquer atores do processo. Opte por citar sempre de maneira impessoal "o juízo decidiu que"; "a perícia concluiu..."; "o laudo pericial..."; " a avaliação social indicou que ..." etc. No caso de referência à testemunhas, aí é importante citar o nome delas, sempre usando o primeiro nome em caixa alta. Por exemplo: "A testemunha JOÃO referiu que..."; "A testemunha MARIA confirmou que...". Ao criar parágrafos, depois de citar algum ato realizado no processo, prefira utilizar sujeito oculto nas sentenças seguintes para evitar repetições. Dessa forma, a escrita deverá ficar mais profissional e fluida.

#FLUIDEZ E COESÃO TEXTUAL Elaborar o texto de forma coesa e fluida, evitando estruturas rígidas ou mecânicas. Utilizar conectivos adequados para criar transições naturais entre os parágrafos. Sempre que possível, fazer referência cruzada com outros elementos probatórios dos autos (perícia médica, documentos médicos, etc.) para fortalecer a argumentação. Priorizar linguagem jurídica técnica, usando expressões como "restou confirmado", "restou demonstrado", "evidenciou-se", em lugar de construções mais simples. Evitar repetições desnecessárias e manter o texto enxuto, preservando apenas as informações essenciais para a fundamentação do pedido.

#EVITAR LINGUAGEM TÉCNICO-MÉDICA Ao produzir minutas, os sintomas, limitações e histórico clínico devem ser narrados em linguagem natural, compreensível ao cidadão comum ou ao operador do Direito — não ao médico ou perito. Evitar uso repetitivo de CIDs, nomenclatura radiológica detalhada, nomes técnicos de exames e medicamentos, salvo quando estritamente necessário. A terminologia técnico-médica só se justifica em situações como: (1) quando delimita a controvérsia (ex.: se determinado achado relevante existe ou não nos autos); ou (2) quando o detalhe técnico reforça diretamente um argumento jurídico (ex.: comprovar nexo causal, cronologia, ou objetividade de um achado específico). Nas minutas deve prevalecer o uso de linguagem mais voltada ao cidadão comum com formação jurídica, a fluidez do relato e a força argumentativa.'''


async def _timeline_completa(processo: str, instancia: int) -> list[dict]:
    """Timeline INTEIRA do processo (não só a janela de `triagem_processo`
    padrão — precisa enxergar desde a petição inicial), em ordem
    CRONOLÓGICA ascendente (a tool devolve do mais recente ao mais antigo)."""
    eventos, _ = await _timeline_e_cabecalho(processo, instancia)
    return eventos


async def _timeline_e_cabecalho(processo: str, instancia: int) -> tuple[list[dict], dict]:
    """Mesma timeline de `_timeline_completa`, mas também devolve o
    `cabecalho` (classe/competência/partes) — usado pelo fluxo de RPV para
    decidir rito JEF x comum (ver `gerar_conferencia_rpv`). Função separada
    (em vez de mudar a assinatura de `_timeline_completa`, já usada pelo
    fluxo de laudo pericial) para não alterar contrato de quem já a chama."""
    from ingestao import eproc_mcp_client

    try:
        res = await eproc_mcp_client.triagem_processo(processo, instancia=instancia, todos_eventos=True)
    except Exception:
        return [], {}
    if not res or res.get("erro"):
        return [], {}
    eventos = res.get("eventos") or []

    def _num(ev: dict) -> int:
        try:
            return int(ev.get("numero") or 0)
        except (TypeError, ValueError):
            return 0
    return sorted(eventos, key=_num), (res.get("cabecalho") or {})


def _localizar_documentos_laudo_pericial(eventos: list[dict], evento_laudo_ref: str) -> dict:
    """Varre a timeline (ascendente, já em memória — nenhuma chamada de rede
    aqui) e localiza:

    - `peticao_inicial`: 1º evento com documento de sigla "INIC*" (a petição
      que abre o processo — praticamente sempre existe).
    - `peticoes_intercorrentes`: eventos "PETIÇÃO" do procurador (autor/DPU)
      ENTRE a inicial e o(s) laudo(s) — podem trazer atestados/exames novos
      que fundamentam a incapacidade (ex.: agravamento do quadro clínico).
    - `laudos`: TODOS os eventos cuja DESCRIÇÃO comece com "LAUDO PERICIAL"
      (critério é a descrição do EVENTO, nunca a sigla de um documento — um
      laudo do processo ADMINISTRATIVO pode vir anexado dentro da própria
      petição inicial, ex. sigla "LAUDOPERIC11" dentro do evento 1, e isso
      não pode ser confundido com o laudo JUDICIAL). Se houver mais de 1, o
      primeiro é o laudo original e os demais são complementares (pedido de
      esclarecimentos).
    - `avaliacoes_sociais`: eventos com documento de sigla "LAUDO_SOC_ECON*"
      — a avaliação social JUDICIAL (perícia biopsicossocial), pedida
      explicitamente pelo prompt de BPC (`PROMPT_IMPUGNACAO_LAUDO_PERICIAL_
      BPC`) — irrelevante pro fluxo de BI, mas incluída sempre que existir
      (não atrapalha, e a decisão de qual prompt usar é feita à parte, ver
      `_classificar_tipo_beneficio`)."""
    peticao_inicial = None
    for ev in eventos:
        docs = ev.get("documentos") or []
        if any((d.get("sigla") or "").upper().startswith("INIC") for d in docs):
            peticao_inicial = ev
            break

    laudos = [ev for ev in eventos if (ev.get("descricao") or "").strip().upper().startswith("LAUDO PERICIAL")]

    # Garante que o laudo já identificado pela intimação (ato principal —
    # ver `analisar_processo`/`_identificar_principal`) sempre entra na
    # lista, mesmo que a heurística de descrição não bata por algum motivo
    # (ex.: e-proc mudar o texto padrão do evento no futuro).
    if evento_laudo_ref and not any(str(ev.get("numero")) == str(evento_laudo_ref) for ev in laudos):
        alvo = next((ev for ev in eventos if str(ev.get("numero")) == str(evento_laudo_ref)), None)
        if alvo:
            laudos.append(alvo)
    laudos.sort(key=lambda ev: int(ev.get("numero") or 0))

    numeros_laudos = {ev.get("numero") for ev in laudos}
    avaliacoes_sociais = [
        ev for ev in eventos
        if ev.get("numero") not in numeros_laudos
        and any((d.get("sigla") or "").upper().startswith("LAUDO_SOC_ECON") for d in (ev.get("documentos") or []))
    ]

    numero_inicial = int(peticao_inicial.get("numero") or 0) if peticao_inicial else 0
    numero_primeiro_laudo = int(laudos[0].get("numero") or 0) if laudos else 10**9

    intercorrentes = [
        ev for ev in eventos
        if numero_inicial < int(ev.get("numero") or 0) < numero_primeiro_laudo
        and (ev.get("descricao") or "").strip().upper() == "PETIÇÃO"
        and (ev.get("ator_tipo") or "") == "procurador"
    ]

    return {
        "peticao_inicial": peticao_inicial, "peticoes_intercorrentes": intercorrentes,
        "laudos": laudos, "avaliacoes_sociais": avaliacoes_sociais,
    }


# Prefixos de sigla considerados "fundamento de incapacidade" dentro de uma
# petição intercorrente (além da própria petição) — atestados/exames/laudos
# novos que a acompanham. Documentos puramente administrativos/identitários
# (RG, comprovante de endereço, procuração, CNIS etc.) ficam de fora — o
# usuário pediu especificamente "fundamentos de incapacidade", não o dossiê
# inteiro anexado à petição.
_PREFIXOS_FUNDAMENTO_INCAPACIDADE = ("PET", "ATESTMED", "EXMMED", "LAUDOPERIC", "LAUDO")


async def _caminho_documento_bruto(processo: str, doc_meta: dict) -> Path | None:
    """Resolve o CAMINHO em disco de um documento já baixado (sem extrair
    texto) — devolve o `.txt` limpo se existir (documento HTML: já é texto
    puro sem ruído de marcação, ver `listar_arquivos_baixados` — não há
    ganho em mandar o Claude ler o `.html` bruto, só desperdiça leitura) ou
    o arquivo original (`arquivo`) caso contrário — tipicamente um PDF, que
    o Claude lê de verdade (visão nativa da própria ferramenta Read, não a
    extração/OCR deste painel) via `_caminhos_documentos_evento`."""
    from ingestao import eproc_mcp_client

    pasta = eproc_mcp_client.EPROC_DOCS_DIR / re.sub(r"\D", "", processo or "")
    nome_txt = doc_meta.get("arquivo_txt")
    if nome_txt:
        p_txt = pasta / nome_txt
        if p_txt.exists():
            return p_txt
    nome = doc_meta.get("arquivo") or ""
    if not nome:
        return None
    p = pasta / nome
    return p if p.exists() else None


async def _caminhos_documentos_evento(processo: str, instancia: int, evento: dict,
                                       prefixos_sigla: tuple[str, ...] | None,
                                       log_cb: LogCb, chave: str | None = None) -> list[tuple[str, Path]]:
    """Baixa os documentos de UM evento e devolve `[(sigla, caminho), ...]` —
    SEM extrair texto (nem OCR): pedido explícito do usuário, o Claude lê
    cada arquivo diretamente com sua própria ferramenta Read (visão nativa
    de PDF — tabelas, quesitos com caixa de marcação, formatação — não a
    extração/OCR que este painel fazia antes, que perde exatamente esse
    tipo de detalhe visual). Filtra por prefixo de sigla quando
    `prefixos_sigla` for dado (None = todos os documentos do evento — usado
    pros laudos, onde tudo que estiver anexado é relevante).

    `chave`: repassada para `_listar_documentos_evento` — necessária quando
    o evento tem documento SIGILOSO (RPV/precatório com valores; ver
    `gerar_conferencia_rpv`)."""
    numero = str(evento.get("numero") or "")
    if not numero:
        return []
    documentos_meta = await _listar_documentos_evento(processo, numero, instancia=instancia, chave=chave)
    if prefixos_sigla:
        documentos_meta = [d for d in documentos_meta
                           if (d.get("sigla") or "").upper().startswith(prefixos_sigla)]
    resultado: list[tuple[str, Path]] = []
    for doc in documentos_meta:
        caminho = await _caminho_documento_bruto(processo, doc)
        if caminho:
            log_cb("info", f"Documento localizado para leitura: {doc['sigla']} (evento {numero}) — "
                           f"{caminho.name}")
            resultado.append((doc["sigla"], caminho))
        else:
            log_cb("muted", f"{doc['sigla']} (evento {numero}): arquivo não encontrado em disco "
                            "após o download — não poderá ser lido.")
    return resultado


async def _classificar_tipo_beneficio(processo: str, instancia: int, peticao_inicial: dict | None,
                                       log_cb: LogCb) -> str:
    """Decide, ANTES de escolher qual dos dois prompts verbatim usar, se o
    caso é de BENEFÍCIO POR INCAPACIDADE ("BI" — auxílio-doença/aposentadoria
    por invalidez/auxílio-acidente, Lei 8.213/91) ou BENEFÍCIO ASSISTENCIAL
    ("BPC" — BPC/LOAS, Lei 8.742/93), lendo o PEDIDO da petição inicial —
    pedido explícito do usuário ("acho que a melhor forma é analisar o
    pedido da inicial"), em vez de uma classificação externa do processo
    (competência/assunto do e-proc), que pode estar genérica ou incorreta.

    Usa o texto já extraído (OCR/nativo — `_extrair_texto_documento`), NÃO a
    leitura nativa via Read: essa etapa é só uma classificação rápida e
    barata, não precisa de fidelidade visual (tabelas/checkboxes) — isso
    fica reservado pra análise principal do laudo em si.

    Devolve "BI", "BPC" ou "" (indefinido — quando a petição inicial não foi
    encontrada, não pôde ser lida, ou a resposta da IA não veio clara;
    `gerar_impugnacao_laudo_pericial` trata "" como REQUER_ANALISE_HUMANA em
    vez de arriscar o prompt errado)."""
    if not peticao_inicial:
        log_cb("muted", "Sem petição inicial localizada — não dá pra classificar BI x BPC pelo pedido.")
        return ""
    numero = str(peticao_inicial.get("numero") or "")
    documentos_meta = await _listar_documentos_evento(processo, numero, instancia=instancia)
    documentos_meta = [d for d in documentos_meta if (d.get("sigla") or "").upper().startswith("INIC")]
    if not documentos_meta:
        log_cb("muted", "Petição inicial sem documento INIC* legível — não dá pra classificar BI x BPC.")
        return ""

    log_cb("info", "Classificando o tipo de benefício pleiteado (BI x BPC) a partir do pedido da "
                   "petição inicial…")
    texto = await _extrair_texto_documento(processo, documentos_meta[0], log_cb)
    if not texto.strip():
        log_cb("muted", "Não consegui extrair texto da petição inicial — não dá pra classificar BI x BPC.")
        return ""

    prompt = "\n".join([
        "Leia o texto de uma petição inicial abaixo e identifique, com base no PEDIDO (não no "
        "diagnóstico médico eventualmente narrado nos fatos), qual tipo de benefício está sendo "
        "pleiteado:",
        "",
        "- \"BI\": benefício POR INCAPACIDADE — auxílio-doença, aposentadoria por invalidez ou "
        "auxílio-acidente (Lei 8.213/91, RGPS, contributivo).",
        "- \"BPC\": benefício ASSISTENCIAL — Benefício de Prestação Continuada / LOAS / amparo "
        "social à pessoa com deficiência ou idosa (Lei 8.742/93, não contributivo).",
        "",
        "TEXTO DA PETIÇÃO INICIAL (pode estar truncado):",
        texto[:15000],
        "",
        "Responda com EXATAMENTE uma palavra, sem mais nada: \"BI\" ou \"BPC\".",
    ])
    saida, erro = await _chamar_claude_texto(prompt, timeout=120)
    if erro:
        log_cb("muted", f"Falha ao classificar BI x BPC: {erro}")
        return ""
    resposta = (saida or "").strip().upper()
    if "BPC" in resposta:
        log_cb("ok", "Pedido classificado como Benefício Assistencial (BPC/LOAS).")
        return "BPC"
    if "BI" in resposta:
        log_cb("ok", "Pedido classificado como Benefício por Incapacidade.")
        return "BI"
    log_cb("muted", f"Resposta de classificação não veio clara ({resposta[:100]!r}) — tratando como indefinido.")
    return ""


def _montar_prompt_impugnacao_laudo(processo: str, secoes: list[tuple[str, list[tuple[str, Path]]]],
                                     prompt_base: str) -> str:
    """Prompt final = instrução do Project do usuário (VERBATIM — `prompt_base`
    é `PROMPT_IMPUGNACAO_LAUDO_PERICIAL_BI` ou `_BPC`, escolhido por
    `_classificar_tipo_beneficio` ANTES de chegar aqui) + os CAMINHOS dos
    documentos do caso (NÃO o texto já extraído) DEPOIS dela + instrução pra
    IA ler cada arquivo ela mesma (ferramenta Read) + uma instrução TÉCNICA
    de formato de resposta — nada disso altera o texto original.

    Pedido explícito do usuário (2026-08-27): em vez de extrair texto/OCR
    aqui e colar pronto no prompt (perde fidelidade — tabelas, quesitos com
    caixa de marcação, formatação do laudo), o Claude agora LÊ cada arquivo
    diretamente com sua própria ferramenta Read, que tem visão nativa de
    PDF (o mesmo mecanismo que o Project pessoal do usuário no claude.ai já
    usava). "Como geralmente são poucas peças, prefiro pecar pelo excesso,
    mas com uma qualidade maior" — por isso TODOS os documentos do caso
    passam por aqui, não só o laudo.

    Sem a instrução de formato no final, a IA devolvia tudo como texto
    corrido só com `**negrito**` estilo markdown (que `_html_para_docx` NÃO
    interpreta, vira asterisco literal no .docx) e misturava a análise/
    classificação prévia junto com a peça em si (bug real do primeiro teste
    ao vivo). Pedimos aqui as DUAS coisas separadas: a análise fica de fora
    de qualquer bloco de código (vira a `justificativa` do confronto), a
    peça em si vem SÓ dentro de um bloco ```html com tags reais."""
    partes = [
        prompt_base,
        "",
        "---",
        "",
        f"# DOCUMENTOS DO PROCESSO {processo} (para a análise pedida acima)",
        "",
        "Os documentos abaixo NÃO estão transcritos aqui — você deve abrir e LER cada um deles "
        "com a sua ferramenta Read antes de prosseguir com a análise. Não pule nenhum, mesmo que "
        "pareça longo. Se um arquivo for PDF com mais páginas do que a leitura padrão da "
        "ferramenta traz de uma vez, leia as páginas seguintes também (parâmetro `pages`) até "
        "cobrir o documento inteiro — isso é especialmente importante no(s) laudo(s) pericial(is), "
        "cuja íntegra (quesitos, tabelas, achados de exame) é o ponto central desta análise.",
    ]
    for titulo, docs in secoes:
        partes.append(f"\n## {titulo}")
        if docs:
            for sigla, caminho in docs:
                partes.append(f"- {sigla}: {caminho}")
        else:
            partes.append("(nenhum documento localizado nesta seção)")
    partes += [
        "",
        "---",
        "",
        "# FORMATO TÉCNICO DA RESPOSTA (instrução do sistema, não do usuário — siga à risca)",
        "Devolva a resposta em DUAS partes, nesta ordem exata:",
        "",
        "1) Um parágrafo curto (até 5 linhas), FORA de qualquer bloco de código, explicando qual "
        "Tipo (1, 2 ou 3, conforme ##CLASSIFICAÇÃO DO CASO acima) você identificou e por quê, e se "
        "os requisitos de validade do laudo (especialização, fundamentação, DII) foram atendidos. "
        "Isso é só para conferência interna do Defensor — nunca vai para os autos.",
        "",
        "2) A petição final, seguindo a #ESTRUTURA_DA_PETIÇÃO indicada acima (do cabeçalho "
        "\"EXCELENTÍSSIMO(A) JUIZ(A) FEDERAL\" até o pedido final), dentro de UM ÚNICO bloco de "
        "código ```html — formatada com TAGS HTML REAIS, nunca markdown: `<p>` para cada "
        "parágrafo, `<strong>` para negrito, `<em>` para itálico, `<u>` para sublinhado (combine "
        "`<strong><em>` quando o pedido acima for negrito+itálico juntos, como nos nomes de "
        "doenças). Não inclua NADA de análise/explicação dentro desse bloco — só o texto da "
        "petição em si, exatamente como sairia para protocolo. EXCEÇÃO: NÃO inclua o fecho final "
        "(\"Curitiba, [data]\", nome do Defensor e cargo) — o sistema adiciona isso "
        "automaticamente, de forma padronizada, depois; termine o bloco no parágrafo do pedido "
        "(\"Diante do exposto, requer...\").",
    ]
    return "\n".join(partes)


async def gerar_impugnacao_laudo_pericial(processo: str, ato: str, paj_norm: str, pasta_paj: Path,
                                           log_cb: LogCb) -> dict:
    """Fluxo especializado completo: localiza os documentos na timeline do
    e-proc, monta o prompt (instrução verbatim + documentos) e gera a peça —
    SEMPRE como rascunho `.docx`+`.pdf` em 'Peças IA/' para revisão do
    Defensor. NUNCA protocola nada — só formata e salva, igual ao resto da
    feature (ver restrição de segurança no topo do arquivo). `ato` identifica
    o sub-caso (ver módulo `intimacao_service`)."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    evento_ref = (caso or {}).get("evento_referenciado") or {}
    instancia = evento_ref.get("instancia", 1)
    evento_laudo_ref = evento_ref.get("evento_referenciado")

    log_cb("info", "Fluxo especializado de laudo pericial: lendo a timeline completa do processo…")
    eventos = await _timeline_completa(processo, instancia)
    if not eventos:
        msg = "Não foi possível ler a timeline completa do processo (MCP)."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    achados = _localizar_documentos_laudo_pericial(eventos, evento_laudo_ref)
    peticao_inicial = achados["peticao_inicial"]
    intercorrentes = achados["peticoes_intercorrentes"]
    laudos = achados["laudos"]
    avaliacoes_sociais = achados["avaliacoes_sociais"]

    if not laudos:
        msg = "Não localizei nenhum evento de 'LAUDO PERICIAL' na timeline do processo."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    # Decide qual dos dois prompts verbatim usar ANTES de gastar tempo lendo
    # todos os documentos com a ferramenta Read — pedido explícito do
    # usuário: classificar pelo PEDIDO da petição inicial (não pela
    # competência do processo no e-proc, que pode estar genérica/errada).
    # Indefinido -> REQUER_ANALISE_HUMANA, nunca um chute de qual prompt usar.
    tipo_beneficio = await _classificar_tipo_beneficio(processo, instancia, peticao_inicial, log_cb)
    if not tipo_beneficio:
        msg = ("Não foi possível classificar com segurança se o caso é de Benefício por "
               "Incapacidade ou Benefício Assistencial (BPC) a partir do pedido da petição "
               "inicial — requer análise humana antes de escolher a minuta certa.")
        log_cb("erro", msg)
        confronto = {
            "classificacao": "REQUER_ANALISE_HUMANA", "justificativa": msg,
            "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
        }
        intimacao_service.salvar_estado(chave, confronto=confronto, minuta=None,
                                        status_geral="aguardando_revisao", erro=None)
        return {"ok": True, "confronto": confronto, "minuta": None}
    prompt_base = PROMPT_IMPUGNACAO_LAUDO_PERICIAL_BPC if tipo_beneficio == "BPC" else PROMPT_IMPUGNACAO_LAUDO_PERICIAL_BI

    secoes: list[tuple[str, list[tuple[str, Path]]]] = []

    if peticao_inicial:
        log_cb("info", f"Petição inicial: evento {peticao_inicial['numero']}.")
        docs = await _caminhos_documentos_evento(processo, instancia, peticao_inicial, ("INIC",), log_cb)
        secoes.append(("PETIÇÃO INICIAL", docs))
    else:
        log_cb("muted", "Não localizei a petição inicial (sigla INIC*) na timeline — seguindo sem ela.")

    if intercorrentes:
        log_cb("info", f"{len(intercorrentes)} petição(ões) intercorrente(s) encontrada(s): "
                       + ", ".join(str(ev["numero"]) for ev in intercorrentes) + ".")
        for ev in intercorrentes:
            docs = await _caminhos_documentos_evento(processo, instancia, ev, _PREFIXOS_FUNDAMENTO_INCAPACIDADE, log_cb)
            secoes.append((f"PETIÇÃO INTERCORRENTE (evento {ev['numero']})", docs))
    else:
        log_cb("muted", "Nenhuma petição intercorrente com fundamentos de incapacidade encontrada.")

    log_cb("info", "Laudo(s) pericial(is): evento(s) " + ", ".join(str(ev["numero"]) for ev in laudos) + ".")
    docs_laudo = await _caminhos_documentos_evento(processo, instancia, laudos[0], None, log_cb)
    secoes.append((f"LAUDO MÉDICO PERICIAL (evento {laudos[0]['numero']})", docs_laudo))
    for ev in laudos[1:]:
        docs_compl = await _caminhos_documentos_evento(processo, instancia, ev, None, log_cb)
        secoes.append((f"LAUDO PERICIAL COMPLEMENTAR (evento {ev['numero']})", docs_compl))

    if avaliacoes_sociais:
        log_cb("info", "Avaliação(ões) social(is) judicial(is) encontrada(s): "
                       + ", ".join(str(ev["numero"]) for ev in avaliacoes_sociais) + ".")
        for ev in avaliacoes_sociais:
            docs_social = await _caminhos_documentos_evento(processo, instancia, ev, None, log_cb)
            secoes.append((f"AVALIAÇÃO SOCIAL JUDICIAL (evento {ev['numero']})", docs_social))

    prompt = _montar_prompt_impugnacao_laudo(processo, secoes, prompt_base)
    log_cb("info", f"Pedindo à IA a ler cada documento (ferramenta Read, visão nativa de PDF) e "
                   f"redigir seguindo o padrão do Project de {tipo_beneficio} do usuário — pode "
                   "levar vários minutos, de propósito: qualidade da leitura tem prioridade sobre "
                   "velocidade…")
    # Timeout bem mais folgado que o resto do painel (480s): agora é uma
    # chamada AGÊNTICA (o Claude abre e lê cada arquivo com sua própria
    # ferramenta Read, não texto já pronto no prompt) — pedido explícito do
    # usuário: "prefiro pecar pelo excesso, mas com uma qualidade maior".
    saida, erro = await _chamar_claude_texto(prompt, timeout=1200)
    if erro:
        log_cb("erro", f"Falha ao gerar a manifestação sobre o laudo: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    # Separa a peça (dentro do ```html pedido) da análise prévia (fora dele —
    # vira a `justificativa` do confronto, texto de verdade da IA sobre qual
    # Tipo/requisitos ela identificou, não mais uma frase estática genérica).
    m_fence = re.search(r"```(?:html)?\s*(.*?)```", saida or "", re.DOTALL)

    if not m_fence:
        # Bug real encontrado ao vivo (2026-08-27, PAJ 2026/029-00353): a IA
        # não devolve o bloco ```html quando identifica algo que IMPEDE
        # seguir o padrão — no caso real, um laudo de avaliação de
        # DEFICIÊNCIA (BPC/LOAS) sendo tratado como se fosse de incapacidade
        # laboral (a detecção por sigla "LAUDOPERIC*" não distingue os dois).
        # A IA corretamente recusou forçar o framework Tipo 1/2/3 (que não
        # se aplica) e devolveu uma pergunta pro Defensor em vez de uma
        # petição — sem essa checagem, esse texto de recusa virava .docx
        # (sem cabeçalho, com `**asterisco**` literal, não protocolável, e o
        # Defensor só descobriria abrindo o arquivo). Trata como
        # REQUER_ANALISE_HUMANA (mesmo rótulo do confronto genérico): a
        # explicação da IA fica visível na tela, SEM gerar nenhuma minuta.
        log_cb("muted", "A IA não devolveu o formato de petição esperado — normalmente sinal de "
                        "que ela identificou alguma incompatibilidade com o caso (ver justificativa "
                        "abaixo). Nenhuma minuta será gerada; requer análise humana.")
        confronto = {
            "classificacao": "REQUER_ANALISE_HUMANA",
            "justificativa": (saida or "").strip()[:4000] or
                "A IA não devolveu uma resposta utilizável (saída vazia) — revise manualmente.",
            "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
        }
        log_cb("ok", f"Classificação: {confronto['classificacao']} (sem minuta).")
        intimacao_service.salvar_estado(chave, confronto=confronto, minuta=None,
                                        status_geral="aguardando_revisao", erro=None)
        return {"ok": True, "confronto": confronto, "minuta": None}

    peticao_html = m_fence.group(1).strip()
    analise_previa = saida[:m_fence.start()].strip()

    sufixo = _sufixo_arquivo_seguro(laudos[0].get("numero"))
    nome = f"impugnacao_laudo_pericial_ev{sufixo}"
    pasta_destino = pasta_paj / "Peças IA"
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome = _nome_minuta_livre(nome, pasta_destino)
    docx_path = pasta_destino / f"{nome}.docx"
    pdf_path = pasta_destino / f"{nome}.pdf"

    erro_docx = await _gerar_docx_pdf(peticao_html, docx_path, pdf_path)
    if erro_docx:
        msg = f"Falha ao formatar/converter a manifestação: {erro_docx}"
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    minuta = {"tipo": "impugnacao_laudo_pericial", "subtipo": tipo_beneficio, "caminho": str(docx_path),
              "gerado_em": datetime.now().isoformat(timespec="seconds")}
    confronto = {
        "classificacao": "IMPUGNACAO_LAUDO_PERICIAL",
        "justificativa": analise_previa or (
            f"Processado pelo fluxo especializado de laudo pericial médico ({tipo_beneficio} — "
            "padrão do Project pessoal do usuário) — a própria peça já classifica o caso (Tipo "
            "1/2/3, ver prompt) com base na petição inicial, eventuais petições intercorrentes, "
            "o(s) laudo(s) e eventual avaliação social do processo. A IA não devolveu uma análise "
            "prévia textual desta vez."
        ),
        "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
    }
    log_cb("ok", f"Manifestação sobre o laudo pericial ({tipo_beneficio}) salva em: {docx_path}")
    intimacao_service.salvar_estado(
        chave, minuta=minuta, confronto=confronto, status_geral="aguardando_revisao", erro=None)
    return {"ok": True, "minuta": minuta, "confronto": confronto}


# --------------------------------------------------------------------------
# Fluxo dedicado: intimação de "Requisição de Pagamento Expedida para
# Conferência" (RPV/precatório) — pedido do usuário (2026-08-28), ensinado
# com 4 casos reais (3 de mera ciência + 1 retificação genuína + 1 "quase-
# erro" real do próprio usuário, corrigido na conversa — valiosíssimo pra
# calibrar a regra de honorários abaixo). Resumo das regras aprendidas:
#
# - Ato principal RPV1 (RPV) ou REQPAGAM1 (precatório) — evento de "Juntada
#   de Ofício Requisitório".
# - A BASE do valor requisitado pode ser (a) uma proposta de acordo
#   (PROACORDO*) aceita e homologada por sentença (comum em JEF), OU (b) um
#   cálculo de liquidação apresentado após sentença/acórdão de mérito — que
#   pode vir com sigla própria, mas também como uma PETIÇÃO genérica sem
#   sigla de cálculo. NUNCA presumir nome fixo — procurar pelo CONTEÚDO.
# - Honorários sucumbenciais da DPU: a fonte de verdade é SEMPRE o texto da
#   sentença/acórdão, NUNCA o cálculo do INSS (que pode estar errado — foi
#   exatamente o que gerou um pedido de retificação incorreto num caso real
#   usado para treinar este fluxo). JEF sem recurso = nunca há honorários
#   (art. 55, Lei 9.099/95). JEF com recurso = só se o recorrente perder.
#   Reafirmação de DER sem oposição do INSS = nunca há honorários (Tema
#   995/STJ), mesmo com valor no cálculo do INSS. Rito comum = só há
#   honorários com condenação EXPRESSA não revertida por decisão posterior.
# - Múltiplos "AUTOR" no cabeçalho: confirmar na petição inicial se é um
#   COCREDOR de verdade ou só o REPRESENTANTE LEGAL de um autor incapaz —
#   neste caso não há crédito próprio dele, e a RPV listar só o incapaz
#   está CORRETO.
# - "Devolução à Seção Judiciária" (honorários periciais) na RPV é normal.
# - Múltiplas intimações concomitantes sobre o MESMO evento de RPV (uma por
#   parte — autor(es)/INSS/MPF) não precisam de tratamento especial: o
#   índice desta feature é por PROCESSO, não por intimação — uma única
#   análise já vale para todas elas.
# --------------------------------------------------------------------------

_PREFIXOS_SIGLA_RPV = ("RPV", "REQPAGAM")


def _eh_sigla_rpv(sigla: str) -> bool:
    sigla = (sigla or "").strip().upper()
    return any(sigla.startswith(p) for p in _PREFIXOS_SIGLA_RPV)


_PREFIXOS_SIGLA_DECISAO = ("SENT", "ACOR", "RELVOTO")


def _localizar_documentos_rpv(eventos: list[dict], evento_rpv_ref: str) -> dict:
    """Varre a timeline (ascendente, já em memória) e localiza:

    - `evento_rpv`: o próprio evento de juntada da RPV/precatório.
    - `peticao_inicial`: 1º evento com documento "INIC*" — para o Claude
      conferir quem são os autores de verdade (cocredor x representante
      legal) e o pedido original.
    - `decisoes`: TODOS os eventos com documento SENT*/ACOR*/RELVOTO*
      ANTERIORES à RPV — pode ser mais de um (1ª instância + 2ª, se houve
      recurso); a ordem cronológica ajuda o Claude a saber qual prevalece.
    - `candidatos_base`: eventos "PETIÇÃO..." com documento anexado entre a
      ÚLTIMA decisão (ou a inicial, se nenhuma) e a RPV — candidatos a
      serem a proposta de acordo aceita ou o cálculo de liquidação.
      Deliberadamente inclusivo (pode trazer mais de um) — o Claude decide,
      lendo o conteúdo, qual é o relevante.
    - `apelacao`: True se algum documento de sigla "APELA*" foi encontrado
      em QUALQUER ponto da timeline — sinal para também checar a 2ª
      instância (ver `gerar_conferencia_rpv`)."""
    evento_rpv = next((ev for ev in eventos if str(ev.get("numero")) == str(evento_rpv_ref)), None)
    if not evento_rpv:
        return {"evento_rpv": None, "peticao_inicial": None, "decisoes": [],
                "candidatos_base": [], "apelacao": False}
    numero_rpv = int(evento_rpv.get("numero") or 0)

    peticao_inicial = None
    for ev in eventos:
        if any((d.get("sigla") or "").upper().startswith("INIC") for d in (ev.get("documentos") or [])):
            peticao_inicial = ev
            break

    anteriores = [ev for ev in eventos if int(ev.get("numero") or 0) < numero_rpv]
    decisoes = [
        ev for ev in anteriores
        if any((d.get("sigla") or "").upper().startswith(_PREFIXOS_SIGLA_DECISAO)
               for d in (ev.get("documentos") or []))
    ]

    # UNIÃO de dois padrões bem diferentes (bug real corrigido: a 1ª versão
    # só olhava petições DEPOIS da última decisão — certo para "cálculo
    # apresentado após o trânsito em julgado", mas errado para "proposta de
    # acordo", cuja proposta/aceite vêm ANTES da homologação, não depois):
    #   a) proposta de acordo (sigla PROACORDO*) e sua aceitação (descrição
    #      "ACEITA PROPOSTA DE ACORDO") — em QUALQUER ponto antes da RPV,
    #      tipicamente antes da homologação que a torna decisão;
    #   b) cálculo/petição de liquidação — qualquer "PETIÇÃO" com documento
    #      anexado DEPOIS da última decisão de mérito encontrada (pode não
    #      ter sigla própria de cálculo).
    numero_ultima_decisao = int(decisoes[-1].get("numero") or 0) if decisoes else 0
    candidatos_base = []
    for ev in anteriores:
        docs = ev.get("documentos") or []
        if not docs:
            continue
        siglas = [(d.get("sigla") or "").upper() for d in docs]
        descricao = (ev.get("descricao") or "").strip().upper()
        eh_proposta_ou_aceite = (
            any(s.startswith("PROACORDO") for s in siglas) or "ACEITA PROPOSTA DE ACORDO" in descricao
        )
        eh_calculo_pos_decisao = (
            int(ev.get("numero") or 0) > numero_ultima_decisao and "PETIÇÃO" in descricao
        )
        if eh_proposta_ou_aceite or eh_calculo_pos_decisao:
            candidatos_base.append(ev)
    candidatos_base.sort(key=lambda ev: int(ev.get("numero") or 0))

    apelacao = any(
        (d.get("sigla") or "").upper().startswith("APELA")
        for ev in eventos for d in (ev.get("documentos") or [])
    )

    return {
        "evento_rpv": evento_rpv, "peticao_inicial": peticao_inicial,
        "decisoes": decisoes, "candidatos_base": candidatos_base, "apelacao": apelacao,
    }


def _montar_prompt_rpv(processo: str, cabecalho: dict,
                        secoes: list[tuple[str, list[tuple[str, Path]]]],
                        andamentos_posteriores: str = "") -> str:
    """Monta o prompt do fluxo de conferência de RPV/precatório — instrução
    escrita para esta feature (ao contrário do fluxo de laudo pericial, o
    usuário não tinha um Project pronto pra isso: "a petição é bem simples
    e pode ser montada por você"). Mesma técnica dos demais fluxos
    especializados: os documentos são só CAMINHOS — o Claude lê cada um com
    sua própria ferramenta Read.

    Saída ESTRUTURADA em 5 blocos + classificação (reestruturado 2026-09-17,
    mesmo espírito da sentença homologatória — pedido explícito do usuário:
    o antigo "resumo da intimação" genérico, em texto corrido, trazia
    informação demais e mal organizada; ver `_parse_classificacao_rpv`).
    SUBSTITUI o resumo genérico da etapa (a) na tela pra casos de RPV/
    precatório (`templates/intimacoes.html` esconde "Resumo da intimação"
    quando `escopo_verificacao.modo === 'especializado_rpv'`)."""
    classe = cabecalho.get("classe") or ""
    competencia = cabecalho.get("competencia") or ""
    partes = cabecalho.get("partes") or []
    partes_txt = "\n".join(f"- {p.get('tipo', '')}: {p.get('nome', '')}" for p in partes) or "(não informado)"

    partes_bloco = [
        "#PERSONA",
        "Você é um Defensor Público Federal que atua em execução/cumprimento de sentença "
        "previdenciário e assistencial, conferindo Requisições de Pequeno Valor (RPV) e "
        "precatórios expedidos pelo juízo antes de dar ciência ao assistido.",
        "",
        "#CONTEXTO",
        f"Processo {processo} — classe: {classe!r}, competência: {competencia!r}.",
        "Partes do cabeçalho do processo:",
        partes_txt,
        "",
        "Uma RPV/precatório foi expedida e juntada aos autos. Sua tarefa é conferir se os "
        "valores/credores requisitados batem com o que foi efetivamente acordado ou decidido no "
        "processo, apresentando um relatório CURTO E OBJETIVO em tópicos — nada de texto corrido "
        "longo — e decidir entre três conclusões possíveis: CIENCIA (está tudo correto), "
        "RETIFICACAO_RPV (algo está errado ou faltando) ou REQUER_ANALISE_HUMANA (não deu pra "
        "concluir com segurança).",
        "",
        "#TAREFA",
        "1. EXPEDIÇÃO: leia a RPV/precatório (documento principal) e informe, em 1 linha curta: "
        "se é RPV ou Precatório, e se há BLOQUEIO de valores (ou está livre, sem bloqueio).",
        "2. BENEFICIÁRIOS: liste TODOS os credores e valores requisitados, um por linha, no "
        "formato \"Nome/Seção — papel (ex.: autor originário, devolução de honorários periciais) — "
        "R$ valor\". Inclua o beneficiário/autor original, honorários periciais \"Devolução à "
        "Seção Judiciária\" (normal, não é erro) e qualquer outra rubrica. Ao final, o VALOR TOTAL "
        "requisitado, se houver mais de um beneficiário. Antes de listar, leia a petição inicial "
        "(se disponível) para saber quem tem PRETENSÃO PRÓPRIA de verdade: quando o cabeçalho lista "
        "mais de um \"AUTOR\", confirme se o 2º nome é mesmo COCREDOR (pretensão própria) ou só o "
        "REPRESENTANTE LEGAL (genitor/curador) de um autor incapaz — neste caso NÃO há crédito "
        "próprio dele, e a RPV listar só o incapaz está CORRETO (não aponte falta de credor aqui).",
        "3. ANDAMENTOS POSTERIORES: veja a seção \"ANDAMENTOS POSTERIORES\" mais abaixo (se "
        "fornecida) e informe, em 1-2 frases curtas, se há algo relevante (decisão ou petição "
        "superveniente que mude a situação) ou se são só intimações replicadas da mesma RPV pra "
        "outras partes / rotina sem conteúdo novo.",
        "4. CÁLCULO BASE: identifique a BASE que define o valor devido — normalmente uma destas "
        "duas situações: (a) proposta de acordo aceita e homologada por sentença/decisão, ou (b) "
        "cálculo de liquidação apresentado após sentença/acórdão de mérito (pode vir como petição "
        "comum, com ou sem sigla própria de \"cálculo\"). Informe, em 1-2 linhas curtas: quem "
        "apresentou o cálculo/proposta (ex.: \"Cálculo de liquidação apresentado pela Coordenação "
        "de Cálculos da DPU\", ou \"Proposta de acordo do INSS\"), o valor, o Nº DO EVENTO, e se o "
        "INSS/a parte contrária concordou ou impugnou (com o Nº DO EVENTO dessa manifestação, se "
        "houver). Se nenhum documento localizado parecer ser a base correta, ou você não "
        "conseguir determinar com segurança qual é, diga isso explicitamente (e classifique como "
        "REQUER_ANALISE_HUMANA mais abaixo, em vez de arriscar um palpite).",
        "5. HONORÁRIOS SUCUMBENCIAIS DA DPU — siga esta regra à risca, é a parte mais delicada "
        "desta análise, mas a RESPOSTA deve ser CURTA (1 linha: \"Sem condenação (JEF sem "
        "recurso)\", \"Sem condenação (Tema 995/STJ — reafirmação de DER sem oposição do INSS)\", "
        "\"Com condenação (evento X)\", etc. — só o veredito e, se houver condenação, o evento "
        "que a fixou):",
        "   - A ÚNICA fonte de verdade sobre se honorários são devidos é o TEXTO da sentença/"
        "acórdão do processo (procure o trecho \"Honorários Advocatícios\"/\"Condeno... "
        "honorários\" em CADA decisão de mérito disponibilizada, inclusive de 2ª instância, se "
        "houver mais de uma — um acórdão pode manter, reverter ou alterar o que a sentença de 1º "
        "grau fixou; vale sempre a determinação MAIS RECENTE/definitiva).",
        "   - NUNCA conclua que honorários são devidos só porque o cálculo apresentado pelo INSS "
        "trouxe uma rubrica de honorários — esse cálculo pode estar simplesmente ERRADO "
        "(inclusive incluir uma rubrica que não deveria existir). Se o cálculo do INSS mostra "
        "honorários mas NENHUMA sentença/acórdão do processo os fixou expressamente a favor do "
        "autor/DPU, a RPV SEM honorários está CORRETA — não peça retificação.",
        "   - Se o processo é do rito de Juizado Especial Federal (JEF) e NÃO houve recurso "
        "(sentença transitou em julgado sem apelação), honorários NUNCA são devidos (art. 55, Lei "
        "9.099/95) — mesmo que alguma decisão mencione condenação, ela não se sustenta.",
        "   - Se é JEF e HOUVE recurso, honorários só são devidos se o recorrente tiver sido "
        "vencido no recurso (ex.: INSS recorreu e o recurso foi desprovido).",
        "   - Se a sentença/acórdão menciona \"reafirmação da DER\" e registra que o INSS NÃO se "
        "opôs a ela, honorários NÃO são devidos (Tema 995/STJ), mesmo que o cálculo do INSS traga "
        "um valor para essa rubrica.",
        "   - Se é rito comum (não-JEF), honorários seguem a regra normal — são devidos se alguma "
        "sentença/acórdão condenou expressamente a parte contrária (INSS) a pagá-los, e essa "
        "condenação não foi revertida por decisão posterior.",
        "6. CONCLUSÃO: compare os valores/credores/honorários da RPV com o que você concluiu ser "
        "correto (itens 2, 4 e 5), e decida, em texto CURTO (poucas linhas, sem repetir os dados "
        "já listados acima):",
        "   - CIENCIA: os valores/credores da RPV batem com a base correta. Não é necessário "
        "peticionar.",
        "   - RETIFICACAO_RPV: há divergência de valor, credor faltando (cocredor de verdade, não "
        "representante legal), ou honorários devidos e omitidos. Gere uma petição pedindo a "
        "retificação (explique aqui, objetivamente, qual é a divergência).",
        "   - REQUER_ANALISE_HUMANA: você não conseguiu determinar a base do valor com segurança, "
        "ou o caso tem alguma peculiaridade que foge do que está descrito acima.",
        "",
        "#ESTILO DA PETIÇÃO DE RETIFICAÇÃO (só se a conclusão for RETIFICACAO_RPV)",
        "Modelo REAL já usado pelo Defensor para este tipo de pedido — siga EXATAMENTE este nível "
        "de objetividade, sem desenvolvimento, sem síntese do processo, sem histórico:",
        "",
        "MM JUIZ(A) FEDERAL,",
        "",
        "A Defensoria Pública da União comparece para requerer a retificação da requisição de "
        "pagamento expedida no evento [número do evento da RPV], em razão de [motivo específico e "
        "direto identificado na sua análise — ex.: \"ausência de inclusão do crédito de honorários "
        "sucumbenciais fixados no evento X\", ou \"divergência de valor em relação ao cálculo "
        "homologado no evento Y\", ou \"ausência do crédito de [nome], parte autora com pretensão "
        "própria não incluída\"].",
        "",
        "Nestes termos, pede deferimento.",
        "",
        "(NÃO inclua \"Curitiba, [data]\", nome do Defensor e cargo — o sistema adiciona isso "
        "automaticamente depois.)",
        "",
        "---",
        "",
        f"# DOCUMENTOS DO PROCESSO {processo} (para a análise pedida acima)",
        "",
        "Os documentos abaixo NÃO estão transcritos aqui — abra e LEIA cada um com sua ferramenta "
        "Read antes de concluir. A RPV costuma ser um PDF estruturado curto; as decisões/cálculos "
        "podem ser mais longos — leia a íntegra, não só o início.",
    ]
    for titulo, docs in secoes:
        partes_bloco.append(f"\n## {titulo}")
        if docs:
            for sigla, caminho in docs:
                partes_bloco.append(f"- {sigla}: {caminho}")
        else:
            partes_bloco.append("(nenhum documento localizado nesta seção)")

    if andamentos_posteriores:
        partes_bloco += [
            "",
            "## ANDAMENTOS POSTERIORES À RPV/PRECATÓRIO (só número/data/rótulo, sem conteúdo de "
            "documento — pra você perceber se são só intimações replicadas da mesma RPV pra outras "
            "partes, ou algo realmente novo)",
            andamentos_posteriores,
        ]

    partes_bloco += [
        "",
        "---",
        "",
        "# FORMATO TÉCNICO DA RESPOSTA (instrução do sistema, não do usuário — siga à risca)",
        "Responda em DUAS partes, nesta ordem exata:",
        "",
        "1) Comece a resposta com uma linha EXATA, sem mais nada nela:",
        "CLASSIFICACAO: CIENCIA_RPV",
        "ou",
        "CLASSIFICACAO: RETIFICACAO_RPV",
        "ou",
        "CLASSIFICACAO: REQUER_ANALISE_HUMANA",
        "",
        "Depois dessa linha, EXATAMENTE estes 6 marcadores, cada um seguido do respectivo texto "
        "(marcador sozinho numa linha, sem mais nada nela — se não houver documento localizado "
        "pra alguma seção, diga isso explicitamente no texto dela em vez de omitir o marcador). "
        "TODOS os textos devem ser CURTOS e em TÓPICOS — nada de parágrafos longos em texto "
        "corrido, isso é pra revisão rápida do Defensor, não pra ir aos autos:",
        "### EXPEDICAO",
        "(RPV ou Precatório — com ou sem bloqueio — ver item 1)",
        "### BENEFICIARIOS",
        "(lista de credores/valores, 1 por linha — ver item 2)",
        "### ANDAMENTOS_POSTERIORES",
        "(curto — ver item 3)",
        "### CALCULO_BASE",
        "(quem elaborou, valor, evento, concordância/impugnação do INSS e evento — ver item 4)",
        "### HONORARIOS",
        "(veredito curto sobre honorários — ver item 5)",
        "### CONCLUSAO",
        "(a decisão do item 6, objetiva e curta — isso é só para conferência interna do Defensor, "
        "nunca vai para os autos)",
        "",
        "2) SOMENTE quando a classificação for RETIFICACAO_RPV, inclua também a petição, dentro "
        "de UM ÚNICO bloco de código ```html — com TAGS HTML REAIS (`<p>` por parágrafo, "
        "`<strong>` para negrito), seguindo o #ESTILO DA PETIÇÃO DE RETIFICAÇÃO acima. Nos outros "
        "dois casos (CIENCIA_RPV/REQUER_ANALISE_HUMANA), NÃO inclua bloco de código nenhum.",
    ]
    return "\n".join(partes_bloco)


_SECOES_RPV = ("EXPEDICAO", "BENEFICIARIOS", "ANDAMENTOS_POSTERIORES", "CALCULO_BASE",
               "HONORARIOS", "CONCLUSAO")


def _parse_classificacao_rpv(saida: str) -> dict:
    """Extrai `{classificacao, expedicao, beneficiarios, andamentos_posteriores,
    calculo_base, honorarios, justificativa, peticao_html}` da resposta
    estruturada da IA (ver formato pedido em `_montar_prompt_rpv`).
    Classificação ausente/não reconhecida -> REQUER_ANALISE_HUMANA (nunca
    arrisca tratar uma resposta mal-formada como CIENCIA_RPV silenciosa).

    Aceita "CIENCIA" solto como sinônimo de "CIENCIA_RPV" (defensivo — bug
    real corrigido 2026-08-28: o prompt pedia "CIENCIA_RPV", mas essa
    função só reconhecia "CIENCIA", então o rótulo salvo nunca batia com
    `intimacao_service.CLASSIFICACOES`/os mapas do front-end, e a tela não
    mostrava NADA mesmo com a análise correta e completa).

    Defensivo com o parsing dos 6 marcadores (reestruturado 2026-09-17, mesmo
    padrão da sentença homologatória): se a IA não seguir o formato à risca,
    NUNCA descarta o texto em silêncio — o que não casar com nenhum marcador
    cai inteiro em `justificativa`."""
    saida = saida or ""
    m_class = re.search(r"CLASSIFICACAO:\s*(CIENCIA_RPV|CIENCIA|RETIFICACAO_RPV|REQUER_ANALISE_HUMANA)",
                        saida, re.IGNORECASE)
    classificacao = m_class.group(1).upper() if m_class else "REQUER_ANALISE_HUMANA"
    if classificacao == "CIENCIA":
        classificacao = "CIENCIA_RPV"

    m_fence = re.search(r"```(?:html)?\s*(.*?)```", saida, re.DOTALL)
    peticao_html = m_fence.group(1).strip() if m_fence else None

    # Corta o bloco de código ANTES de procurar os marcadores "### NOME" —
    # o HTML da petição pode conter texto que colidiria com o parsing.
    resto = saida[m_class.end():] if m_class else saida
    if m_fence:
        corte = m_fence.start() - (m_class.end() if m_class else 0)
        resto = resto[:corte] if corte > 0 else resto

    marcadores = list(re.finditer(
        r"###\s*(" + "|".join(_SECOES_RPV) + r")\s*\n", resto, re.IGNORECASE))
    secoes: dict[str, str] = {}
    for i, mo in enumerate(marcadores):
        inicio = mo.end()
        fim = marcadores[i + 1].start() if i + 1 < len(marcadores) else len(resto)
        secoes[mo.group(1).upper()] = resto[inicio:fim].strip()

    sobra = resto[:marcadores[0].start()].strip() if marcadores else resto.strip()

    expedicao = secoes.get("EXPEDICAO", "")
    beneficiarios = secoes.get("BENEFICIARIOS", "")
    andamentos_posteriores = secoes.get("ANDAMENTOS_POSTERIORES", "")
    calculo_base = secoes.get("CALCULO_BASE", "")
    honorarios = secoes.get("HONORARIOS", "")
    justificativa = secoes.get("CONCLUSAO", "") or sobra
    if not justificativa:
        justificativa = "A IA não devolveu uma conclusão textual clara — revise manualmente."

    if classificacao != "RETIFICACAO_RPV":
        peticao_html = None
    elif not peticao_html:
        # Pediu retificação mas não devolveu a petição — trata como
        # REQUER_ANALISE_HUMANA em vez de gerar um docx vazio/quebrado.
        classificacao = "REQUER_ANALISE_HUMANA"
        justificativa += ("\n\n(A classificação original era RETIFICACAO_RPV, mas a IA não "
                          "devolveu o bloco da petição — revise manualmente.)")

    return {
        "classificacao": classificacao,
        "expedicao": expedicao[:500],
        "beneficiarios": beneficiarios[:2000],
        "andamentos_posteriores": andamentos_posteriores[:1000],
        "calculo_base": calculo_base[:1500],
        "honorarios": honorarios[:1000],
        "justificativa": justificativa[:4000],
        "peticao_html": peticao_html,
    }


async def gerar_conferencia_rpv(processo: str, ato: str, paj_norm: str, pasta_paj: Path, log_cb: LogCb) -> dict:
    """Fluxo especializado completo: localiza a RPV/precatório e os
    documentos-base (proposta de acordo ou cálculo, decisões de mérito) na
    timeline do e-proc, monta o prompt e pede à IA que confira e conclua —
    SEMPRE como rascunho `.docx`+`.pdf` em 'Peças IA/' quando houver
    retificação a pedir. NUNCA protocola nada — só formata e salva, igual
    ao resto da feature (ver restrição de segurança no topo do arquivo).
    `ato` identifica o sub-caso (ver módulo `intimacao_service`)."""
    from ingestao import eproc_mcp_client

    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    evento_ref = (caso or {}).get("evento_referenciado") or {}
    instancia = evento_ref.get("instancia", 1)
    evento_rpv_ref = evento_ref.get("evento_referenciado")

    chave_info = eproc_mcp_client.buscar_chave_paj(processo)
    chave = (chave_info or {}).get("chave")
    if chave:
        log_cb("info", "Chave de acesso sigiloso encontrada no PAJ — será usada para ler a "
                       "RPV/precatório, caso necessário.")

    log_cb("info", "Fluxo especializado de conferência de RPV/precatório: lendo a timeline "
                   "completa do processo…")
    eventos, cabecalho = await _timeline_e_cabecalho(processo, instancia)
    if not eventos:
        msg = "Não foi possível ler a timeline completa do processo (MCP)."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    achados = _localizar_documentos_rpv(eventos, evento_rpv_ref)
    evento_rpv = achados["evento_rpv"]
    if not evento_rpv:
        msg = f"Não localizei o evento {evento_rpv_ref} (RPV/precatório) na timeline do processo."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    # Decisões de mérito por instância — [(instancia, evento), ...]. Se
    # houve apelação, também consulta a 2ª instância: o acórdão pode ter
    # mantido, revertido ou alterado o que a sentença de 1º grau fixou
    # sobre honorários (aprendizado real com um caso de teste).
    decisoes: list[tuple[int, dict]] = [(instancia, ev) for ev in achados["decisoes"]]
    if achados["apelacao"]:
        log_cb("info", "Detectada apelação no histórico — consultando também a 2ª instância "
                       "(TRF4) para localizar o acórdão…")
        eventos_2g, _ = await _timeline_e_cabecalho(processo, 2)
        if eventos_2g:
            decisoes += [
                (2, ev) for ev in eventos_2g
                if any((d.get("sigla") or "").upper().startswith(_PREFIXOS_SIGLA_DECISAO)
                       for d in (ev.get("documentos") or []))
            ]
        else:
            log_cb("muted", "Não consegui acessar a 2ª instância — seguindo só com o que foi "
                            "decidido em 1º grau.")

    secoes: list[tuple[str, list[tuple[str, Path]]]] = []

    log_cb("info", f"RPV/precatório: evento {evento_rpv['numero']}.")
    docs_rpv = await _caminhos_documentos_evento(processo, instancia, evento_rpv, None, log_cb, chave=chave)
    secoes.append((f"RPV/PRECATÓRIO EXPEDIDO (evento {evento_rpv['numero']})", docs_rpv))

    if achados["peticao_inicial"]:
        ev_inic = achados["peticao_inicial"]
        log_cb("info", f"Petição inicial: evento {ev_inic['numero']}.")
        docs_inic = await _caminhos_documentos_evento(processo, instancia, ev_inic, ("INIC",), log_cb)
        secoes.append((f"PETIÇÃO INICIAL (evento {ev_inic['numero']})", docs_inic))
    else:
        log_cb("muted", "Não localizei a petição inicial (sigla INIC*) na timeline.")

    if decisoes:
        log_cb("info", f"{len(decisoes)} decisão(ões) de mérito/homologação encontrada(s): "
                       + ", ".join(f"{ev['numero']}/{inst}g" for inst, ev in decisoes) + ".")
        for inst, ev in decisoes:
            docs_dec = await _caminhos_documentos_evento(processo, inst, ev, None, log_cb, chave=chave)
            secoes.append((f"DECISÃO/HOMOLOGAÇÃO (evento {ev['numero']}, {inst}ª instância)", docs_dec))
    else:
        log_cb("muted", "Nenhuma sentença/acórdão localizada antes da RPV.")

    if achados["candidatos_base"]:
        log_cb("info", f"{len(achados['candidatos_base'])} petição(ões) candidata(s) a base do "
                       "valor (proposta de acordo/cálculo): "
                       + ", ".join(str(ev["numero"]) for ev in achados["candidatos_base"]) + ".")
        for ev in achados["candidatos_base"]:
            docs_base = await _caminhos_documentos_evento(processo, instancia, ev, None, log_cb, chave=chave)
            secoes.append((f"POSSÍVEL PROPOSTA DE ACORDO/CÁLCULO (evento {ev['numero']})", docs_base))
    else:
        log_cb("muted", "Nenhuma petição candidata a proposta de acordo/cálculo localizada entre "
                        "a última decisão e a RPV.")

    # Pedido explícito do usuário (2026-09-17): informar objetivamente se
    # houve andamentos DEPOIS da RPV/precatório, em vez de depender do resumo
    # genérico da etapa (a) — mesmo helper já usado na sentença homologatória.
    andamentos_posteriores = _montar_texto_andamentos_posteriores(eventos, [str(evento_rpv["numero"])])

    prompt = _montar_prompt_rpv(processo, cabecalho, secoes, andamentos_posteriores)
    log_cb("info", "Pedindo à IA a ler cada documento (ferramenta Read, visão nativa de PDF) e "
                   "conferir a RPV contra o que foi acordado/decidido — pode levar alguns "
                   "minutos…")
    saida, erro = await _chamar_claude_texto(prompt, timeout=900)
    if erro:
        log_cb("erro", f"Falha ao conferir a RPV/precatório: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    parsed = _parse_classificacao_rpv(saida)
    classificacao = parsed["classificacao"]
    campos_estruturados = {
        "expedicao": parsed["expedicao"],
        "beneficiarios": parsed["beneficiarios"],
        "andamentos_posteriores_rpv": parsed["andamentos_posteriores"],
        "calculo_base": parsed["calculo_base"],
        "honorarios_sucumbenciais": parsed["honorarios"],
    }

    if classificacao != "RETIFICACAO_RPV":
        log_cb("ok", f"Classificação: {classificacao} (sem minuta).")
        confronto = {
            "classificacao": classificacao, "justificativa": parsed["justificativa"],
            **campos_estruturados,
            "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
        }
        intimacao_service.salvar_estado(chave, confronto=confronto, minuta=None,
                                        status_geral="aguardando_revisao", erro=None)
        return {"ok": True, "confronto": confronto, "minuta": None}

    sufixo = _sufixo_arquivo_seguro(evento_rpv.get("numero"))
    nome = f"retificacao_rpv_ev{sufixo}"
    pasta_destino = pasta_paj / "Peças IA"
    pasta_destino.mkdir(parents=True, exist_ok=True)
    nome = _nome_minuta_livre(nome, pasta_destino)
    docx_path = pasta_destino / f"{nome}.docx"
    pdf_path = pasta_destino / f"{nome}.pdf"

    erro_docx = await _gerar_docx_pdf(parsed["peticao_html"], docx_path, pdf_path)
    if erro_docx:
        msg = f"Falha ao formatar/converter a petição de retificação: {erro_docx}"
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    minuta = {"tipo": "retificacao_rpv", "caminho": str(docx_path),
              "gerado_em": datetime.now().isoformat(timespec="seconds")}
    confronto = {
        "classificacao": "RETIFICACAO_RPV", "justificativa": parsed["justificativa"],
        **campos_estruturados,
        "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
    }
    log_cb("ok", f"Petição de retificação da RPV/precatório salva em: {docx_path}")
    intimacao_service.salvar_estado(
        chave, minuta=minuta, confronto=confronto, status_geral="aguardando_revisao", erro=None)
    return {"ok": True, "minuta": minuta, "confronto": confronto}


# --------------------------------------------------------------------------
# Fluxo dedicado: intimação de SENTENÇA HOMOLOGATÓRIA DE ACORDO — pedido do
# usuário (2026-08-28), ensinado com 3 casos reais (todos limpos, sem
# divergência — o padrão do CEJUSCON aparenta ser bem consistente).
# Reestruturado 2026-08-29 (saída em 5 seções — ver `_montar_prompt_
# sentenca_homologatoria`/`_parse_classificacao_sentenca_homologatoria`).
# Regras:
#
# - Gatilho: ato principal SENT* cujo evento tem descrição contendo
#   "HOMOLOG" (ex.: "Homologada a Transação - tipo B").
# - Pedido da petição inicial: transcrito IPSIS LITTERIS (cópia exata,
#   nunca resumo/paráfrase) — pedido explícito do usuário 2026-08-29: só o
#   texto do pedido em si, sem motivo do indeferimento administrativo nem
#   fundamentação da incapacidade/qualidade de segurado.
# - A sentença pode ser (a) PURAMENTE homologatória — só remete ao acordo,
#   sem repetir nada — ou (b) reproduzir, no próprio corpo, algum dado do
#   acordo (tipo de benefício, DIB/DII/DIP/DCB, valores etc.), às vezes só
#   PARCIALMENTE (ex.: reproduz a tabela de cumprimento mas remete à
#   proposta pra tudo o mais — visto nos 3 casos reais de treino).
# - Se for (a): sem problema — o INSS vai olhar o acordo original na hora
#   do cumprimento. Orientação: CIENCIA.
# - Se for (b): fazer o batimento campo a campo do que a sentença
#   REPRODUZIU contra a proposta de acordo original (o que ela só remeteu
#   por referência não precisa ser conferido — já está coberto pela
#   garantia de que o INSS vai olhar o original). Sem divergência ->
#   CIENCIA. Com divergência clara -> PETICIONAR (aponta a providência, ex.
#   embargos de declaração, mas NUNCA gera a peça). Batimento inconclusivo
#   (proposta não localizada, documento ilegível etc.) -> ANALISAR_
#   DETALHADAMENTE. NUNCA gera petição de verdade em nenhuma das 3 vias —
#   pedido explícito do usuário: só sinalizar/orientar, a redação (se
#   houver) fica por conta do Defensor.
# --------------------------------------------------------------------------

_PREFIXO_SIGLA_SENTENCA = "SENT"


def _eh_sentenca_homologatoria(sigla_principal: str, evento_descricao: str) -> bool:
    """Detecta o gatilho deste fluxo: ato principal de sigla SENT* cujo
    EVENTO (não o documento) tem descrição de homologação de transação/
    acordo — uma sentença de mérito comum (ex.: "Julgado improcedente")
    também usa sigla SENT*, então a sigla sozinha não basta."""
    sigla_principal = (sigla_principal or "").strip().upper()
    if not sigla_principal.startswith(_PREFIXO_SIGLA_SENTENCA):
        return False
    return "HOMOLOG" in (evento_descricao or "").upper()


def _localizar_documentos_sentenca_homologatoria(eventos: list[dict], evento_sent_ref: str) -> dict:
    """Varre a timeline (ascendente, já em memória) e localiza a própria
    sentença + candidatos a proposta de acordo original (mesma união de
    heurísticas validada no fluxo de RPV — `_localizar_documentos_rpv`:
    sigla PROACORDO* ou descrição "ACEITA PROPOSTA DE ACORDO", em qualquer
    ponto antes da sentença) + a petição inicial (1º evento com documento
    "INIC*" — mesma heurística do fluxo de RPV/laudo pericial — pedido
    explícito do usuário 2026-08-29: mostrar o PEDIDO original, pra saber o
    que foi pleiteado antes de conferir se o acordo/sentença atende)."""
    evento_sent = next((ev for ev in eventos if str(ev.get("numero")) == str(evento_sent_ref)), None)
    if not evento_sent:
        return {"evento_sentenca": None, "candidatos_base": [], "peticao_inicial": None}
    numero_sent = int(evento_sent.get("numero") or 0)
    anteriores = [ev for ev in eventos if int(ev.get("numero") or 0) < numero_sent]

    peticao_inicial = None
    for ev in eventos:
        if any((d.get("sigla") or "").upper().startswith("INIC") for d in (ev.get("documentos") or [])):
            peticao_inicial = ev
            break

    candidatos_base = []
    for ev in anteriores:
        docs = ev.get("documentos") or []
        if not docs:
            continue
        siglas = [(d.get("sigla") or "").upper() for d in docs]
        descricao = (ev.get("descricao") or "").strip().upper()
        if any(s.startswith("PROACORDO") for s in siglas) or "ACEITA PROPOSTA DE ACORDO" in descricao:
            candidatos_base.append(ev)
    candidatos_base.sort(key=lambda ev: int(ev.get("numero") or 0))

    return {"evento_sentenca": evento_sent, "candidatos_base": candidatos_base,
            "peticao_inicial": peticao_inicial}


def _montar_prompt_sentenca_homologatoria(processo: str, cabecalho: dict,
                                           secoes: list[tuple[str, list[tuple[str, Path]]]],
                                           andamentos_posteriores: str = "") -> str:
    """Monta o prompt do fluxo de conferência de sentença homologatória —
    instrução escrita para esta feature (o usuário não tinha Project pronto
    pra isso). Este fluxo NUNCA gera petição — só classifica, extrai os
    dados de cada peça separadamente e explica (pedido explícito: "sugerir
    atuação", não redigir nada ainda).

    Saída ESTRUTURADA em 5 blocos (pedido explícito do usuário, 2026-08-29,
    refinado 2026-09-17 depois de ver a saída real em produção — cada
    instrução do #TAREFA abaixo corresponde 1:1 a um marcador do #FORMATO
    TÉCNICO, ver `_parse_classificacao_sentenca_homologatoria`):
    pedido de mérito da inicial (só o essencial, sem pedidos acessórios) /
    dados da proposta (em tópicos fixos) / aceitação do acordo (conferência
    de procedimento) / sentença (enxuta, só divergência importa) /
    conclusão (veredito objetivo + andamentos posteriores, sem resumo
    genérico da intimação)."""
    classe = cabecalho.get("classe") or ""
    competencia = cabecalho.get("competencia") or ""

    partes_bloco = [
        "#PERSONA",
        "Você é um Defensor Público Federal que atua em processos previdenciários/assistenciais, "
        "conferindo sentenças homologatórias de acordo antes de dar ciência ao assistido.",
        "",
        "#CONTEXTO",
        f"Processo {processo} — classe: {classe!r}, competência: {competencia!r}.",
        "",
        "Uma sentença homologou um acordo celebrado entre a parte autora e o INSS. Sua tarefa é "
        "conferir se a sentença tem algum vício que possa comprometer o cumprimento correto do "
        "acordo pelo INSS, apresentando um relatório CURTO E OBJETIVO — nada de prosa longa, cada "
        "seção deve ser a mais enxuta possível mantendo a informação essencial.",
        "",
        "#TAREFA",
        "1. PEDIDO DE MÉRITO DA INICIAL: leia a PETIÇÃO INICIAL (se localizada) e transcreva "
        "IPSIS LITTERIS **apenas** o PEDIDO PRINCIPAL DE MÉRITO — o pedido condenatório que "
        "menciona a espécie do benefício, o NB (se houver) e as datas técnicas (DER/DIB/DII, "
        "conforme o caso) — e o PEDIDO SUBSIDIÁRIO de mérito, se houver (ex.: pedido alternativo "
        "caso o principal não seja acolhido). NÃO transcreva nem mencione NENHUM outro pedido — "
        "justiça gratuita, antecipação de tutela, citação, produção de provas, prioridade de "
        "tramitação, honorários, ou qualquer pedido processual/acessório ficam de fora, mesmo que "
        "constem na petição. Copie o texto do pedido de mérito exato, palavra por palavra, sem "
        "resumir nem parafrasear. Se não conseguir localizar um pedido de mérito bem delimitado, "
        "diga isso explicitamente em vez de inventar.",
        "2. DADOS DA PROPOSTA: leia a PROPOSTA DE ACORDO do INSS (se localizada) e informe, em "
        "tópicos curtos e objetivos, SOMENTE isto (nada além disso):",
        "   - Tipo de providência: concessão, implantação ou restabelecimento.",
        "   - Espécie do benefício.",
        "   - DIB, DII (se benefício por incapacidade), DIP, DCB (se benefício por prazo certo) — "
        "só os que se aplicarem ao caso.",
        "   - Atrasados: PERCENTUAL a calcular (se o valor ainda depender de cálculo posterior) OU "
        "VALOR EXATO já fixado na proposta (se for valor fechado) — diga qual dos dois é o caso.",
        "   - Se há fixação de honorários advocatícios (sim/não, e o percentual/valor se "
        "informado).",
        "   - Forma de pagamento: RPV, Precatório, ou via administrativa (Complemento Positivo - "
        "CP).",
        "3. ACEITAÇÃO DO ACORDO (mera conferência de procedimento): localize a PETIÇÃO da DPU que "
        "se manifesta sobre a proposta — geralmente com descrição \"PETIÇÃO - ACEITA PROPOSTA DE "
        "ACORDO\" ou \"PETIÇÃO - NÃO ACEITA PROPOSTA DE ACORDO\". Informe, objetivamente: (a) se a "
        "proposta foi ACEITA ou REJEITADA pela DPU, citando o número do evento; (b) se havia "
        "declaração assinada pelo(a) próprio(a) assistido(a) anexada a essa petição (sim/não). O "
        "objetivo AQUI é só perceber rapidamente se, por engano, o juízo tratou uma REJEIÇÃO como "
        "se fosse aceitação — não avalie o mérito da aceitação/rejeição em si, só relate o que "
        "consta nos autos.",
        "4. SENTENÇA (enxuta — sem repetir dados já listados no item 2): leia a SENTENÇA "
        "HOMOLOGATÓRIA e diga, em 1 a 2 frases, se ela REPRODUZ CORRETAMENTE os dados da proposta "
        "(sem repetir cada campo de novo) OU se existe alguma informação DIVERGENTE que mereça "
        "atenção — nesse caso, aponte exatamente qual é a divergência, campo a campo (data "
        "diferente, espécie de benefício diferente, valor diferente, benefício trocado, "
        "DIB/DII/DIP/DCB incompatível, etc.). Reprodução pode ser PARCIAL (ex.: repete a tabela de "
        "cumprimento mas remete à proposta só pro valor) — trate cada dado individualmente: o que "
        "foi REPRODUZIDO precisa bater; o que foi só REMETIDO por referência não precisa (o INSS "
        "vai olhar o original na hora do cumprimento). NÃO transcreva a sentença nem liste de novo "
        "os dados que já bateram — só o veredito curto e, se houver, a divergência.",
        "5. CONCLUSÃO: comece com uma destas duas frases EXATAS, sozinha na primeira linha do "
        "bloco:",
        "   - \"Há itens a serem analisados.\" — sempre que houver qualquer divergência (item 4) "
        "ou você não tenha conseguido concluir com segurança (ex.: proposta não localizada, "
        "documento ilegível/incompleto, situação ambígua demais).",
        "   - \"Não há divergências relevantes.\" — quando a sentença bateu com a proposta (ou foi "
        "puramente remissiva) e não há nada pendente de conferência.",
        "   Depois dessa frase, em texto curto: (a) mencione que se trata de uma intimação de "
        "sentença homologatória de acordo (1 frase, SEM resumo genérico do processo); (b) informe "
        "objetivamente se houve ANDAMENTOS POSTERIORES relevantes à sentença (ver seção "
        "\"ANDAMENTOS POSTERIORES\" mais abaixo, se fornecida) — se não houve nada relevante, diga "
        "isso em 1 frase; (c) se houver divergência/pendência, explique OBJETIVAMENTE qual é "
        "(campo a campo) e, brevemente, qual seria a providência cabível (ex.: embargos de "
        "declaração), SEM redigir a peça em si.",
        "",
        "IMPORTANTE: este fluxo NUNCA gera petição/minuta de verdade — sua resposta é só a "
        "classificação e os 5 blocos pedidos; a providência (se houver) fica por conta do "
        "Defensor. Todo o relatório deve ser CURTO — evite repetir informação entre seções.",
        "",
        "---",
        "",
        f"# DOCUMENTOS DO PROCESSO {processo} (para a análise pedida acima)",
        "",
        "Os documentos abaixo NÃO estão transcritos aqui — abra e LEIA cada um com sua ferramenta "
        "Read antes de concluir.",
    ]
    for titulo, docs in secoes:
        partes_bloco.append(f"\n## {titulo}")
        if docs:
            for sigla, caminho in docs:
                partes_bloco.append(f"- {sigla}: {caminho}")
        else:
            partes_bloco.append("(nenhum documento localizado nesta seção)")

    if andamentos_posteriores:
        partes_bloco += [
            "",
            "## ANDAMENTOS POSTERIORES À SENTENÇA (só número/data/rótulo, sem conteúdo de "
            "documento — verificação padrão pra não ignorar algo relevante que aconteceu DEPOIS "
            "da sentença, ex. uma decisão posterior que supera/retifica o acordo)",
            andamentos_posteriores,
        ]

    partes_bloco += [
        "",
        "---",
        "",
        "# FORMATO TÉCNICO DA RESPOSTA (instrução do sistema, não do usuário — siga à risca)",
        "Comece a resposta com uma linha EXATA, sem mais nada nela:",
        "CLASSIFICACAO: CIENCIA",
        "ou",
        "CLASSIFICACAO: ANALISAR_DETALHADAMENTE",
        "ou",
        "CLASSIFICACAO: PETICIONAR",
        "",
        "Depois dessa linha, EXATAMENTE estes 5 marcadores, cada um seguido do respectivo texto "
        "(marcador sozinho numa linha, sem mais nada nela — se não houver documento localizado "
        "pra alguma seção, diga isso explicitamente no texto dela em vez de omitir o marcador):",
        "### PEDIDO_INICIAL",
        "(cópia IPSIS LITTERIS do pedido de mérito principal e do subsidiário, se houver — ver "
        "item 1; NADA de pedidos acessórios)",
        "### PROPOSTA_ACORDO",
        "(só os tópicos objetivos do item 2, nada além disso)",
        "### ACEITACAO",
        "(aceita ou rejeitada, nº do evento, se há declaração do assistido — ver item 3)",
        "### SENTENCA",
        "(veredito curto: reproduziu certo, ou qual a divergência — ver item 4, sem repetir dados)",
        "### CONCLUSAO",
        "(a frase-veredito exata + o texto do item 5)",
        "",
        "NÃO inclua nenhum bloco de código, petição ou minuta nesta resposta — só os 5 blocos "
        "pedidos, cada um com texto corrido (sem markdown), o mais curto possível.",
    ]
    return "\n".join(partes_bloco)


_SECOES_SENTENCA_HOMOLOGATORIA = ("PEDIDO_INICIAL", "PROPOSTA_ACORDO", "ACEITACAO", "SENTENCA", "CONCLUSAO")


def _parse_classificacao_sentenca_homologatoria(saida: str) -> dict:
    """Extrai `{classificacao, pedido_inicial, proposta_acordo, dados_sentenca,
    justificativa}` da resposta estruturada da IA (ver formato pedido em
    `_montar_prompt_sentenca_homologatoria`). Sem petição nunca, ao contrário
    dos outros fluxos especializados — pedido explícito do usuário.
    Classificação ausente/não reconhecida -> ANALISAR_DETALHADAMENTE (nunca
    arrisca CIENCIA silenciosa).

    Defensivo com o parsing dos 4 marcadores: se a IA não seguir o formato à
    risca (marcador faltando/fora de ordem), NUNCA descarta o texto em
    silêncio — o que não casar com nenhum marcador cai inteiro em
    `justificativa` (o Defensor ainda vê tudo, só não separado em seções)."""
    saida = saida or ""
    m_class = re.search(r"CLASSIFICACAO:\s*(CIENCIA|ANALISAR_DETALHADAMENTE|PETICIONAR|"
                         r"REQUER_ANALISE_HUMANA)", saida, re.IGNORECASE)
    classificacao_raw = m_class.group(1).upper() if m_class else "ANALISAR_DETALHADAMENTE"
    # "REQUER_ANALISE_HUMANA" aceito como sinônimo defensivo de
    # ANALISAR_DETALHADAMENTE — nome antigo desta mesma ideia, formato usado
    # antes desta reestruturação (2026-08-29); nenhum dado de produção usa
    # mais isso daqui em diante, só proteção contra a IA "lembrar" o rótulo
    # velho.
    mapa_classificacao = {
        "CIENCIA": "CIENCIA_SENTENCA_HOMOLOGATORIA",
        "ANALISAR_DETALHADAMENTE": "SENTENCA_HOMOLOGATORIA_ANALISAR",
        "REQUER_ANALISE_HUMANA": "SENTENCA_HOMOLOGATORIA_ANALISAR",
        "PETICIONAR": "SENTENCA_HOMOLOGATORIA_PETICIONAR",
    }
    classificacao = mapa_classificacao[classificacao_raw]

    resto = saida[m_class.end():] if m_class else saida
    # Localiza cada marcador "### NOME" e recorta o texto até o próximo
    # marcador (ou o fim). `re.finditer` preserva a ordem de aparição na
    # resposta, não a ordem pedida no prompt — não importa, indexamos por
    # nome do marcador, não por posição.
    marcadores = list(re.finditer(
        r"###\s*(" + "|".join(_SECOES_SENTENCA_HOMOLOGATORIA) + r")\s*\n", resto, re.IGNORECASE))
    secoes: dict[str, str] = {}
    for i, mo in enumerate(marcadores):
        inicio = mo.end()
        fim = marcadores[i + 1].start() if i + 1 < len(marcadores) else len(resto)
        secoes[mo.group(1).upper()] = resto[inicio:fim].strip()

    sobra = resto[:marcadores[0].start()].strip() if marcadores else resto.strip()

    pedido_inicial = secoes.get("PEDIDO_INICIAL", "")
    proposta_acordo = secoes.get("PROPOSTA_ACORDO", "")
    aceitacao_acordo = secoes.get("ACEITACAO", "")
    dados_sentenca = secoes.get("SENTENCA", "")
    # "CONCLUSAO" é o nome atual do marcador (2026-09-17) — "ORIENTACAO" aceito
    # como sinônimo defensivo (nome usado pela versão anterior deste prompt;
    # protege contra a IA "lembrar" o marcador antigo de uma execução passada).
    justificativa = secoes.get("CONCLUSAO", "") or secoes.get("ORIENTACAO", "") or sobra
    if not justificativa:
        justificativa = "A IA não devolveu uma conclusão textual clara — revise manualmente."

    return {
        "classificacao": classificacao,
        # Limite maior que as demais seções — é transcrição IPSIS LITTERIS, não
        # resumo (pedidos com item de mérito + subsidiário podem passar de
        # 2000 caracteres; cortar apagaria parte do pedido de verdade).
        "pedido_inicial": pedido_inicial[:6000],
        "proposta_acordo": proposta_acordo[:2000],
        "aceitacao_acordo": aceitacao_acordo[:1000],
        "dados_sentenca": dados_sentenca[:2000],
        "justificativa": justificativa[:4000],
    }


async def gerar_conferencia_sentenca_homologatoria(processo: str, ato: str, paj_norm: str, pasta_paj: Path,
                                                     log_cb: LogCb) -> dict:
    """Fluxo especializado completo: localiza a petição inicial, a proposta
    de acordo original (se houver) e a sentença homologatória na timeline do
    e-proc, monta o prompt e pede à IA que resuma cada peça separadamente e
    conclua. NUNCA gera minuta — só classifica (CIENCIA, ANALISAR_
    DETALHADAMENTE ou PETICIONAR) e explica, para o Defensor decidir a
    providência (pedido explícito do usuário). `ato` identifica o sub-caso
    (ver módulo `intimacao_service`)."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    evento_ref = (caso or {}).get("evento_referenciado") or {}
    instancia = evento_ref.get("instancia", 1)
    evento_sent_ref = evento_ref.get("evento_referenciado")

    log_cb("info", "Fluxo especializado de sentença homologatória: lendo a timeline completa do "
                   "processo…")
    eventos, cabecalho = await _timeline_e_cabecalho(processo, instancia)
    if not eventos:
        msg = "Não foi possível ler a timeline completa do processo (MCP)."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    achados = _localizar_documentos_sentenca_homologatoria(eventos, evento_sent_ref)
    evento_sent = achados["evento_sentenca"]
    if not evento_sent:
        msg = f"Não localizei o evento {evento_sent_ref} (sentença homologatória) na timeline do processo."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    secoes: list[tuple[str, list[tuple[str, Path]]]] = []

    # Pedido explícito do usuário (2026-08-29): mostrar o pedido original —
    # mesma heurística "1º evento com documento INIC*" já usada nos fluxos
    # de RPV/laudo pericial.
    if achados["peticao_inicial"]:
        ev_inic = achados["peticao_inicial"]
        log_cb("info", f"Petição inicial: evento {ev_inic['numero']}.")
        docs_inic = await _caminhos_documentos_evento(processo, instancia, ev_inic, ("INIC",), log_cb)
        secoes.append((f"PETIÇÃO INICIAL (evento {ev_inic['numero']})", docs_inic))
    else:
        log_cb("muted", "Não localizei a petição inicial (sigla INIC*) na timeline.")

    log_cb("info", f"Sentença homologatória: evento {evento_sent['numero']}.")
    docs_sent = await _caminhos_documentos_evento(processo, instancia, evento_sent, None, log_cb)
    secoes.append((f"SENTENÇA HOMOLOGATÓRIA (evento {evento_sent['numero']})", docs_sent))

    if achados["candidatos_base"]:
        log_cb("info", f"{len(achados['candidatos_base'])} petição(ões) candidata(s) a proposta "
                       "de acordo original: "
                       + ", ".join(str(ev["numero"]) for ev in achados["candidatos_base"]) + ".")
        for ev in achados["candidatos_base"]:
            docs_base = await _caminhos_documentos_evento(processo, instancia, ev, None, log_cb)
            secoes.append((f"POSSÍVEL PROPOSTA DE ACORDO (evento {ev['numero']})", docs_base))
    else:
        log_cb("muted", "Nenhuma proposta de acordo localizada antes da sentença.")

    # Pedido explícito do usuário (2026-09-17): a conclusão deve dizer
    # objetivamente se houve andamentos relevantes DEPOIS da sentença, em vez
    # de depender do resumo genérico da etapa (a) — mesmo helper já usado lá
    # (`_montar_texto_andamentos_posteriores`), aqui ancorado só no evento da
    # própria sentença (não nos eventos que ELA referencia).
    andamentos_posteriores = _montar_texto_andamentos_posteriores(eventos, [str(evento_sent["numero"])])

    prompt = _montar_prompt_sentenca_homologatoria(processo, cabecalho, secoes, andamentos_posteriores)
    log_cb("info", "Pedindo à IA a ler a petição inicial, a proposta e a sentença, e resumir cada "
                   "uma separadamente…")
    saida, erro = await _chamar_claude_texto(prompt, timeout=600)
    if erro:
        log_cb("erro", f"Falha ao conferir a sentença homologatória: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    parsed = _parse_classificacao_sentenca_homologatoria(saida)
    classificacao = parsed["classificacao"]
    log_cb("ok", f"Classificação: {classificacao} (sem minuta — este fluxo não gera petição).")
    confronto = {
        "classificacao": classificacao, "justificativa": parsed["justificativa"],
        "pedido_inicial": parsed["pedido_inicial"],
        "dados_proposta_acordo": parsed["proposta_acordo"],
        "aceitacao_acordo": parsed["aceitacao_acordo"],
        "dados_sentenca": parsed["dados_sentenca"],
        "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
    }
    intimacao_service.salvar_estado(chave, confronto=confronto, minuta=None,
                                    status_geral="aguardando_revisao", erro=None)
    return {"ok": True, "confronto": confronto, "minuta": None}


# --------------------------------------------------------------------------
# Fluxo dedicado: intimação de SENTENÇA DE MÉRITO (procedente, parcialmente
# procedente, improcedente ou extinção) — pedido explícito do usuário
# (2026-09-24, caso real de treino: PAJ 2025/029-01021, processo
# 5060150-19.2024.4.04.7000, DPU como curadora especial do corréu SAMUEL
# HENRIQUE REZENDE SIMOES sem oposição ao pedido do autor — sentença de
# procedência favorável à mãe/autora, logo SEM interesse recursal do lado
# representado pela DPU). MUITO mais complexo que o batimento da sentença
# homologatória de acordo — exige uma cadeia de 5 julgamentos, cada um
# dependente do anterior:
#
# 1. REPRESENTAÇÃO: quem a DPU representa (autor ou réu), a partir da
#    intimação e das partes associadas — base de tudo que vem depois (a
#    análise de interesse recursal só faz sentido do ponto de vista de QUEM
#    a DPU defende, nunca do processo como um todo).
# 2. NATUREZA DA ATUAÇÃO: representação ORDINÁRIA (pedido de assistência +
#    outorga de poderes, tipicamente evento 1) OU CURADORIA ESPECIAL
#    (nomeação pela Justiça Federal — citação por edital/hora certa, réu
#    revel, incapaz sem representante). Se curadoria especial: hipótese
#    legal + CONFLITO DE INTERESSES com o autor (sim = defesa plena; não =
#    atuação PRÓ-FORMA, sem interesse recursal real mesmo com resultado
#    formalmente desfavorável ao curatelado).
# 3. TESE SUSTENTADA: síntese objetiva do que foi defendido (pedidos, teses,
#    provas), lendo TODAS as manifestações processuais relevantes — não só
#    o ato principal, ao contrário dos outros fluxos especializados.
# 4. RESULTADO DA SENTENÇA: procedente/parcial/improcedente/extinção, e se é
#    FAVORÁVEL ou DESFAVORÁVEL aos interesses de quem a DPU representa
#    (nunca ao processo em geral).
# 5. ORIENTAÇÃO: CIENCIA (sem interesse recursal) / EMBARGOS (vício sanável
#    — indicar os pontos omissos) / RECURSO (inominado ou apelação conforme
#    o rito — indicar pontos de sucumbência e teses recursais, objetivo e
#    conciso, SEM aprofundamento) / ANALISAR_DETALHADAMENTE (não deu pra
#    concluir com segurança algum dos passos acima). NUNCA gera a peça
#    recursal/embargos de verdade — mesma restrição de segurança dos demais
#    fluxos especializados: só orienta, o Defensor decide e redige.
# --------------------------------------------------------------------------

_PREFIXOS_SIGLA_MANIFESTACAO = ("PET", "CONTEST", "IMPUGN", "ALEGFIN", "RECINO", "APELA", "CONTRARRAZ")


def _eh_sentenca_merito(sigla_principal: str, evento_descricao: str) -> bool:
    """Gatilho deste fluxo — COMPLEMENTO de `_eh_sentenca_homologatoria`:
    mesma sigla SENT* do ato principal, mas SEM "HOMOLOG" na descrição do
    evento (ex.: "Julgado procedente o pedido", "Julgado improcedente",
    "Extinto o processo sem resolução de mérito")."""
    sigla_principal = (sigla_principal or "").strip().upper()
    if not sigla_principal.startswith(_PREFIXO_SIGLA_SENTENCA):
        return False
    return "HOMOLOG" not in (evento_descricao or "").upper()


def _localizar_documentos_sentenca_merito(eventos: list[dict], evento_sent_ref: str,
                                           limite_manifestacoes: int = 12) -> dict:
    """Varre a timeline (ascendente, já em memória) e localiza:

    - `evento_sentenca`: a própria sentença de mérito.
    - `peticao_inicial`: 1º evento com documento "INIC*" (mesma heurística
      dos demais fluxos especializados).
    - `evento_curadoria`: 1º evento, em QUALQUER ponto antes da sentença,
      cuja descrição contenha "CURADOR" (ex.: "Nomeação de curador
      especial") — sinal de que a DPU pode atuar como curadora especial,
      não em representação ordinária. None se não encontrado (não decide
      nada sozinho — a IA ainda confere pelo conteúdo).
    - `manifestacoes`: eventos ENTRE a petição inicial e a sentença com
      documento de sigla de manifestação (`_PREFIXOS_SIGLA_MANIFESTACAO`)
      OU `ator_tipo == "procurador"` — TODAS as partes, não só a
      representada pela DPU (a IA precisa ler o que a parte contrária
      também alegou/produziu). Deliberadamente mais abrangente que os
      outros fluxos (que buscam 1-2 documentos específicos): aqui o pedido
      explícito do usuário é "ler todas as manifestações processuais
      anteriores para entender a tese sustentada". Cortado em
      `limite_manifestacoes` (as MAIS RECENTES, mais próximas da sentença)
      se passar disso — processos com litígio longo poderiam gerar um
      prompt gigante; `manifestacoes_cortadas=True` avisa quando isso
      acontece (nunca corta em silêncio)."""
    evento_sent = next((ev for ev in eventos if str(ev.get("numero")) == str(evento_sent_ref)), None)
    if not evento_sent:
        return {"evento_sentenca": None, "peticao_inicial": None, "evento_curadoria": None,
                "manifestacoes": [], "manifestacoes_cortadas": False}
    numero_sent = int(evento_sent.get("numero") or 0)
    anteriores = [ev for ev in eventos if int(ev.get("numero") or 0) < numero_sent]

    peticao_inicial = None
    for ev in eventos:
        if any((d.get("sigla") or "").upper().startswith("INIC") for d in (ev.get("documentos") or [])):
            peticao_inicial = ev
            break

    evento_curadoria = None
    for ev in anteriores:
        if "CURADOR" in (ev.get("descricao") or "").upper():
            evento_curadoria = ev
            break

    numero_inicial = int(peticao_inicial.get("numero") or 0) if peticao_inicial else 0
    manifestacoes = []
    for ev in anteriores:
        numero_ev = int(ev.get("numero") or 0)
        if numero_ev <= numero_inicial:
            continue
        docs = ev.get("documentos") or []
        if not docs:
            continue
        siglas = [(d.get("sigla") or "").upper() for d in docs]
        eh_manifestacao = (
            any(s.startswith(pref) for s in siglas for pref in _PREFIXOS_SIGLA_MANIFESTACAO)
            or ev.get("ator_tipo") == "procurador"
        )
        if eh_manifestacao:
            manifestacoes.append(ev)
    manifestacoes.sort(key=lambda ev: int(ev.get("numero") or 0))

    manifestacoes_cortadas = False
    if len(manifestacoes) > limite_manifestacoes:
        manifestacoes = manifestacoes[-limite_manifestacoes:]
        manifestacoes_cortadas = True

    return {
        "evento_sentenca": evento_sent, "peticao_inicial": peticao_inicial,
        "evento_curadoria": evento_curadoria, "manifestacoes": manifestacoes,
        "manifestacoes_cortadas": manifestacoes_cortadas,
    }


def _montar_texto_linha_do_tempo(eventos: list[dict], numero_inicio: int, numero_fim: int,
                                  limite: int = 60) -> str:
    """Lista compacta (número/data/rótulo/autor) dos eventos ENTRE
    `numero_inicio` e `numero_fim` (inclusive) — mesmo formato/espírito de
    `_montar_texto_andamentos_posteriores`, mas pro INTERVALO da petição
    inicial até a sentença, não só "depois de". Dá à IA uma visão geral do
    andamento processual mesmo pros eventos cujo documento não foi baixado
    individualmente (só as `manifestacoes` relevantes são, ver
    `_localizar_documentos_sentenca_merito`) — ajuda a perceber a natureza
    da atuação da DPU (ex.: decisão que nomeia curador especial, decisão
    saneadora que define os pontos controvertidos) sem precisar baixar
    TUDO."""
    intervalo = [ev for ev in eventos
                 if numero_inicio <= int(ev.get("numero") or 0) <= numero_fim]
    if not intervalo:
        return ""
    aviso = ""
    if len(intervalo) > limite:
        aviso = f"\n(mostrando só os {limite} mais recentes de {len(intervalo)} eventos do intervalo)"
        intervalo = intervalo[-limite:]
    linhas = []
    for ev in intervalo:
        desc = (ev.get("descricao") or "").strip()
        if len(desc) > 160:
            desc = desc[:160] + "…"
        ator = ev.get("ator_tipo") or ""
        linhas.append(f"- Evento {ev.get('numero', '?')} ({ev.get('data_hora', '')}): {desc}"
                      + (f" [autor: {ator}]" if ator else ""))
    return "\n".join(linhas) + aviso


def _montar_prompt_sentenca_merito(processo: str, cabecalho: dict,
                                    secoes: list[tuple[str, list[tuple[str, Path]]]],
                                    linha_do_tempo: str = "", minhas_partes: set[str] | None = None) -> str:
    """Monta o prompt do fluxo de conferência de sentença de MÉRITO —
    instrução escrita pra esta feature a partir de um caso real ensinado
    pelo usuário (2026-09-24, PAJ 2025/029-01021). MUITO mais complexo que a
    sentença homologatória de acordo (que só faz um batimento de dados): uma
    cadeia de 5 julgamentos dependentes entre si — quem a DPU representa,
    como ela atua (ordinária ou curadoria especial, com ou sem conflito de
    interesses), a tese sustentada, o resultado e se ele é favorável, e só
    então a orientação final. NUNCA gera a peça de embargos/recurso —
    mesma restrição de segurança dos demais fluxos especializados.

    `minhas_partes` (opcional): nomes das partes associadas ao procurador
    logado no e-proc, já resolvidos por `services.eproc_procurador_service.
    partes_do_procurador` (cadastro oficial de partes/representantes do
    processo — MESMO mecanismo que já substituiu, 2026-09-23, a antiga
    heurística de "polo ativo" no casamento de intimação). Passar isso como
    FATO conhecido evita que a IA precise adivinhar quem a DPU representa
    só lendo documentos — a parte mais arriscada de errar desta análise
    inteira. Vazio quando a identidade não pôde ser resolvida (cofre
    trancado, sem credencial "eproc" salva, ou o cadastro do processo não
    bateu) — nesse caso a IA cai no modo antigo (determinar só pela leitura
    dos documentos), com um aviso explícito de que o dado automático não
    está disponível."""
    classe = cabecalho.get("classe") or ""
    competencia = cabecalho.get("competencia") or ""
    partes = cabecalho.get("partes") or []
    partes_txt = "\n".join(f"- {p.get('tipo', '')}: {p.get('nome', '')}" for p in partes) or "(não informado)"

    if minhas_partes:
        minhas_partes_txt = (
            "DADO CONFIÁVEL (cadastro oficial de partes/representantes do processo, já resolvido "
            "automaticamente — use como base, mas confirme e cite evidência dos documentos se "
            "possível): a DPU representa, NESTE processo: " + ", ".join(sorted(minhas_partes)) + "."
        )
    else:
        minhas_partes_txt = (
            "Não foi possível resolver automaticamente quem a DPU representa neste processo "
            "(identidade do procurador logado indisponível, ou o cadastro de partes não bateu) — "
            "determine isso você mesmo, com cautela redobrada, lendo a intimação recebida e a "
            "petição inicial."
        )

    partes_bloco = [
        "#PERSONA",
        "Você é um Defensor Público Federal que atua em processos previdenciários/assistenciais, "
        "avaliando sentenças de MÉRITO (procedência, procedência parcial, improcedência ou "
        "extinção) para decidir a providência cabível antes de dar ciência ao assistido.",
        "",
        "#CONTEXTO",
        f"Processo {processo} — classe: {classe!r}, competência: {competencia!r}.",
        "Partes do cabeçalho do processo:",
        partes_txt,
        "",
        minhas_partes_txt,
        "",
        "Uma sentença de MÉRITO foi proferida e juntada aos autos. Diferente de uma sentença "
        "homologatória de acordo (que só exige um batimento de dados contra uma proposta), esta "
        "análise exige entender quem a DPU representa, COMO ela atua no processo, e SE há "
        "interesse recursal contra o que foi decidido. Siga TODOS os passos abaixo, na ordem, sem "
        "pular etapas — cada um depende da conclusão do anterior.",
        "",
        "#TAREFA",
        "1. REPRESENTAÇÃO: identifique COM PRECISÃO quem a DPU representa neste processo — "
        "autor(a) ou réu/executado — e o NOME da parte. Use o DADO CONFIÁVEL acima quando "
        "disponível (é o cadastro oficial de partes/representantes do processo); na ausência dele, "
        "determine a partir do cabeçalho do processo, da intimação recebida e da petição inicial. "
        "Isso é a base de TUDO que vem depois: a análise de interesse recursal só faz sentido do "
        "ponto de vista de QUEM a DPU defende, nunca do processo como um todo ou do resultado geral "
        "da ação.",
        "2. NATUREZA DA ATUAÇÃO: verifique se a atuação da DPU se dá pelas vias ORDINÁRIAS de "
        "representação processual (o assistido procurou a DPU — pedido de assistência judiciária + "
        "outorga de poderes, tipicamente perto do evento 1) OU se a DPU atua como CURADORA "
        "ESPECIAL (nomeação pela Justiça Federal — hipóteses típicas: citação por edital/hora "
        "certa, réu revel, parte incapaz sem representante). Veja a seção \"DESIGNAÇÃO DE "
        "CURADORIA ESPECIAL\" abaixo, se localizada, e cite o Nº DO EVENTO que evidencia a "
        "natureza da atuação.",
        "   - Se for CURADORIA ESPECIAL: identifique a hipótese legal e avalie se há CONFLITO DE "
        "INTERESSES entre o representado e a parte autora/exequente.",
        "     - Havendo conflito de interesses genuíno (ex.: o réu curatelado tem defesa de mérito "
        "real a opor ao pedido — nega o débito, contesta o direito etc.): a atuação da DPU é "
        "defesa PLENA, com todos os interesses processuais normais de qualquer parte.",
        "     - NÃO havendo conflito de interesses real (ex.: o interesse do réu curatelado na "
        "verdade COINCIDE com o do autor — caso típico: curadoria de um dependente menor/incapaz "
        "numa ação em que a procedência não prejudica em nada seus interesses): a atuação é "
        "apenas PRÓ-FORMA — existe defesa técnica processual, mas NÃO há interesse recursal REAL a "
        "proteger, mesmo que o resultado seja formalmente \"contrário\" ao curatelado.",
        "3. TESE SUSTENTADA: leia TODAS as manifestações processuais relevantes já apresentadas no "
        "processo (contestação, réplica, impugnações, alegações finais — ver seção "
        "\"MANIFESTAÇÕES PROCESSUAIS\" abaixo) e sintetize, objetivamente: qual foi a tese de "
        "defesa/pretensão sustentada pela parte que a DPU representa, quais foram os pedidos "
        "formulados, e quais provas foram produzidas/consideradas relevantes (perícia, prova "
        "documental, testemunhal). Isso é indispensável para avaliar, no próximo passo, se a "
        "sentença acolheu ou rejeitou corretamente essa tese.",
        "4. RESULTADO DA SENTENÇA: leia a SENTENÇA e diga, objetivamente: ela julgou PROCEDENTE, "
        "PARCIALMENTE PROCEDENTE, IMPROCEDENTE, ou EXTINGUIU o processo (com ou sem resolução de "
        "mérito — diga o motivo)? Em seguida, avalie se o resultado é FAVORÁVEL ou DESFAVORÁVEL "
        "aos interesses da parte que a DPU representa (nunca ao processo em geral ou à parte "
        "contrária). Lembre-se do item 2: se a DPU atua como curadora especial SEM conflito de "
        "interesses real, um resultado formalmente \"contrário\" ao réu curatelado pode não ser "
        "substancialmente desfavorável — avalie a partir do interesse DE VERDADE, não só do polo "
        "processual.",
        "5. ORIENTAÇÃO FINAL: com base em TUDO acima, decida entre estas quatro:",
        "   - CIENCIA: não há interesse recursal — a sentença foi favorável aos interesses reais da "
        "parte representada, OU é curadoria especial pró-forma sem conflito de interesses (mesmo "
        "com resultado formalmente desfavorável), OU qualquer outra razão objetiva pela qual não "
        "há motivo para recorrer.",
        "   - EMBARGOS: a sentença tem algum vício sanável por embargos de declaração (omissão, "
        "contradição, obscuridade, erro material) que precisa ser corrigido antes de decidir sobre "
        "outros recursos. Indique OBJETIVAMENTE cada ponto omisso/contraditório/obscuro.",
        "   - RECURSO: há interesse recursal de MÉRITO contra a sentença (recurso inominado, se "
        "Juizado Especial Federal; apelação, se rito comum). Indique, de forma OBJETIVA E CONCISA "
        "(sem aprofundamento doutrinário, sem desenvolver a tese por extenso): (a) os pontos de "
        "SUCUMBÊNCIA — o que exatamente foi decidido contra os interesses do representado; (b) as "
        "TESES RECURSAIS cabíveis — teses já conhecidas a repetir, ou teses novas a levantar, "
        "sempre ligadas ao motivo ESPECÍFICO da improcedência/procedência parcial identificado na "
        "sentença (ex.: requisito legal tido por não cumprido, prova considerada insuficiente, "
        "interpretação questionável do juízo).",
        "   - ANALISAR_DETALHADAMENTE: você não conseguiu concluir com segurança quem a DPU "
        "representa, a natureza da atuação, ou o resultado da sentença — explique exatamente o que "
        "falta pro Defensor resolver manualmente.",
        "",
        "Dois exemplos REAIS do NÍVEL DE OBJETIVIDADE esperado quando a orientação for RECURSO "
        "(ilustração de ESTILO — não são deste processo, não os repita aqui):",
        "a) \"BPC improcedente pelo não cumprimento do requisito econômico. Há viabilidade recursal "
        "sustentando necessidade de exclusão do Bolsa Família da renda familiar; dedução de "
        "despesas comprovadas e não consideradas pelo juízo; e flexibilização do critério de renda "
        "para 1/2 salário mínimo (art. 20, §11-A, Lei 8.742/93).\"",
        "b) \"BPC improcedente por não enquadramento em deficiência de longo prazo. Há viabilidade "
        "recursal pois os documentos comprovam [X, Y, Z] limitações que restringem a participação "
        "social e a competição em igualdade de condições, e não houve avaliação social para "
        "análise biopsicossocial (nulidade por cerceamento de defesa).\"",
        "",
        "IMPORTANTE: este fluxo NUNCA gera a petição/minuta de embargos ou recurso de verdade — "
        "sua resposta é só a classificação e os 5 blocos pedidos; a redação (se houver) fica por "
        "conta do Defensor.",
        "",
        "---",
        "",
        f"# DOCUMENTOS DO PROCESSO {processo} (para a análise pedida acima)",
        "",
        "Os documentos abaixo NÃO estão transcritos aqui — abra e LEIA cada um com sua ferramenta "
        "Read antes de concluir.",
    ]
    for titulo, docs in secoes:
        partes_bloco.append(f"\n## {titulo}")
        if docs:
            for sigla, caminho in docs:
                partes_bloco.append(f"- {sigla}: {caminho}")
        else:
            partes_bloco.append("(nenhum documento localizado nesta seção)")

    if linha_do_tempo:
        partes_bloco += [
            "",
            "## LINHA DO TEMPO DO PROCESSO (só número/data/rótulo/autor, sem conteúdo de documento "
            "— visão geral do andamento processual, útil pra perceber a natureza da atuação da DPU "
            "mesmo em eventos cujo documento não foi baixado individualmente)",
            linha_do_tempo,
        ]

    partes_bloco += [
        "",
        "---",
        "",
        "# FORMATO TÉCNICO DA RESPOSTA (instrução do sistema, não do usuário — siga à risca)",
        "Comece a resposta com uma linha EXATA, sem mais nada nela:",
        "CLASSIFICACAO: CIENCIA",
        "ou",
        "CLASSIFICACAO: EMBARGOS",
        "ou",
        "CLASSIFICACAO: RECURSO",
        "ou",
        "CLASSIFICACAO: ANALISAR_DETALHADAMENTE",
        "",
        "Depois dessa linha, EXATAMENTE estes 5 marcadores, cada um seguido do respectivo texto "
        "(marcador sozinho numa linha, sem mais nada nela — se não conseguir concluir alguma "
        "seção, diga isso explicitamente no texto dela em vez de omitir o marcador). Cada seção "
        "deve ser OBJETIVA E CONCISA, sem aprofundamento doutrinário nem repetição entre seções:",
        "### REPRESENTACAO",
        "(quem a DPU representa — autor ou réu, nome da parte, nº do evento-base — ver item 1)",
        "### NATUREZA_ATUACAO",
        "(ordinária ou curadoria especial; se curadoria, hipótese legal + conflito de interesses "
        "sim/não, com justificativa — ver item 2)",
        "### TESE_SUSTENTADA",
        "(síntese objetiva do que foi defendido: pedidos, teses, provas — ver item 3)",
        "### RESULTADO_SENTENCA",
        "(procedente/parcial/improcedente/extinção + favorável ou desfavorável ao representado — "
        "ver item 4)",
        "### ORIENTACAO",
        "(a decisão do item 5, com a fundamentação objetiva correspondente — pontos omissos se "
        "EMBARGOS, sucumbência+teses se RECURSO)",
        "",
        "NÃO inclua nenhum bloco de código, petição, embargos ou minuta nesta resposta — só os 5 "
        "blocos pedidos, cada um com texto corrido (sem markdown).",
    ]
    return "\n".join(partes_bloco)


_SECOES_SENTENCA_MERITO = ("REPRESENTACAO", "NATUREZA_ATUACAO", "TESE_SUSTENTADA",
                           "RESULTADO_SENTENCA", "ORIENTACAO")


def _parse_classificacao_sentenca_merito(saida: str) -> dict:
    """Extrai `{classificacao, representacao, natureza_atuacao, tese_sustentada,
    resultado_sentenca, justificativa}` da resposta estruturada da IA (ver
    formato pedido em `_montar_prompt_sentenca_merito`). Classificação
    ausente/não reconhecida -> ANALISAR_DETALHADAMENTE (nunca arrisca CIENCIA
    silenciosa — o risco de perder um prazo recursal por engano é grave
    demais pra esse fallback ser qualquer outra coisa).

    Defensivo com o parsing dos 5 marcadores (mesmo padrão da sentença
    homologatória/RPV): se a IA não seguir o formato à risca, NUNCA descarta
    o texto em silêncio — o que não casar com nenhum marcador cai inteiro em
    `justificativa`."""
    saida = saida or ""
    m_class = re.search(r"CLASSIFICACAO:\s*(CIENCIA|EMBARGOS|RECURSO|ANALISAR_DETALHADAMENTE)",
                        saida, re.IGNORECASE)
    classificacao_raw = m_class.group(1).upper() if m_class else "ANALISAR_DETALHADAMENTE"
    mapa_classificacao = {
        "CIENCIA": "SENTENCA_MERITO_CIENCIA",
        "EMBARGOS": "SENTENCA_MERITO_EMBARGOS",
        "RECURSO": "SENTENCA_MERITO_RECURSO",
        "ANALISAR_DETALHADAMENTE": "SENTENCA_MERITO_ANALISAR",
    }
    classificacao = mapa_classificacao[classificacao_raw]

    resto = saida[m_class.end():] if m_class else saida
    marcadores = list(re.finditer(
        r"###\s*(" + "|".join(_SECOES_SENTENCA_MERITO) + r")\s*\n", resto, re.IGNORECASE))
    secoes: dict[str, str] = {}
    for i, mo in enumerate(marcadores):
        inicio = mo.end()
        fim = marcadores[i + 1].start() if i + 1 < len(marcadores) else len(resto)
        secoes[mo.group(1).upper()] = resto[inicio:fim].strip()

    sobra = resto[:marcadores[0].start()].strip() if marcadores else resto.strip()

    representacao = secoes.get("REPRESENTACAO", "")
    natureza_atuacao = secoes.get("NATUREZA_ATUACAO", "")
    tese_sustentada = secoes.get("TESE_SUSTENTADA", "")
    resultado_sentenca = secoes.get("RESULTADO_SENTENCA", "")
    justificativa = secoes.get("ORIENTACAO", "") or sobra
    if not justificativa:
        justificativa = "A IA não devolveu uma orientação textual clara — revise manualmente."

    return {
        "classificacao": classificacao,
        "representacao": representacao[:1000],
        "natureza_atuacao": natureza_atuacao[:2000],
        "tese_sustentada": tese_sustentada[:3000],
        "resultado_sentenca": resultado_sentenca[:1500],
        "justificativa": justificativa[:4000],
    }


async def gerar_conferencia_sentenca_merito(processo: str, ato: str, paj_norm: str, pasta_paj: Path,
                                             log_cb: LogCb) -> dict:
    """Fluxo especializado completo: localiza a petição inicial, a
    designação de curadoria especial (se houver), as manifestações
    processuais relevantes e a sentença de mérito na timeline do e-proc,
    monta o prompt e pede à IA que percorra a cadeia de 5 julgamentos (quem
    a DPU representa -> natureza da atuação -> tese sustentada -> resultado
    -> orientação) e conclua. NUNCA gera minuta — só classifica (CIENCIA,
    EMBARGOS, RECURSO ou ANALISAR_DETALHADAMENTE) e explica, para o Defensor
    decidir a providência e redigir, se for o caso (pedido explícito do
    usuário). `ato` identifica o sub-caso (ver módulo `intimacao_service`)."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso = intimacao_service.obter_caso(processo, ato)
    evento_ref = (caso or {}).get("evento_referenciado") or {}
    instancia = evento_ref.get("instancia", 1)
    evento_sent_ref = evento_ref.get("evento_referenciado")

    log_cb("info", "Fluxo especializado de sentença de mérito: lendo a timeline completa do "
                   "processo…")
    eventos, cabecalho = await _timeline_e_cabecalho(processo, instancia)
    if not eventos:
        msg = "Não foi possível ler a timeline completa do processo (MCP)."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    achados = _localizar_documentos_sentenca_merito(eventos, evento_sent_ref)
    evento_sent = achados["evento_sentenca"]
    if not evento_sent:
        msg = f"Não localizei o evento {evento_sent_ref} (sentença) na timeline do processo."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    secoes: list[tuple[str, list[tuple[str, Path]]]] = []

    if achados["peticao_inicial"]:
        ev_inic = achados["peticao_inicial"]
        log_cb("info", f"Petição inicial: evento {ev_inic['numero']}.")
        docs_inic = await _caminhos_documentos_evento(processo, instancia, ev_inic, ("INIC",), log_cb)
        secoes.append((f"PETIÇÃO INICIAL (evento {ev_inic['numero']})", docs_inic))
    else:
        log_cb("muted", "Não localizei a petição inicial (sigla INIC*) na timeline.")

    if achados["evento_curadoria"]:
        ev_cur = achados["evento_curadoria"]
        log_cb("info", f"Possível designação de curadoria especial: evento {ev_cur['numero']}.")
        docs_cur = await _caminhos_documentos_evento(processo, instancia, ev_cur, None, log_cb)
        secoes.append((f"DESIGNAÇÃO DE CURADORIA ESPECIAL (evento {ev_cur['numero']})", docs_cur))

    if achados["manifestacoes"]:
        aviso_corte = " (só as mais recentes — processo com muitas manifestações)" \
            if achados["manifestacoes_cortadas"] else ""
        log_cb("info", f"{len(achados['manifestacoes'])} manifestação(ões) processual(is) "
                       f"relevante(s){aviso_corte}: "
                       + ", ".join(str(ev["numero"]) for ev in achados["manifestacoes"]) + ".")
        for ev in achados["manifestacoes"]:
            docs_manif = await _caminhos_documentos_evento(processo, instancia, ev, None, log_cb)
            secoes.append((f"MANIFESTAÇÃO PROCESSUAL (evento {ev['numero']})", docs_manif))
    else:
        log_cb("muted", "Nenhuma manifestação processual localizada entre a petição inicial e a "
                        "sentença.")

    log_cb("info", f"Sentença: evento {evento_sent['numero']}.")
    docs_sent = await _caminhos_documentos_evento(processo, instancia, evento_sent, None, log_cb)
    secoes.append((f"SENTENÇA (evento {evento_sent['numero']})", docs_sent))

    numero_inicial = int((achados["peticao_inicial"] or {}).get("numero") or 1)
    numero_sent = int(evento_sent.get("numero") or 0)
    linha_do_tempo = _montar_texto_linha_do_tempo(eventos, numero_inicial, numero_sent)

    # Quem a DPU representa — mesmo cadastro oficial de partes/representantes
    # já usado pra casar a intimação certa (`eproc_procurador_service`,
    # substituiu a antiga heurística de "polo ativo" em 2026-09-23). Passar
    # isso como fato conhecido evita que a IA precise adivinhar o passo mais
    # arriscado de errar desta análise inteira.
    from services import eproc_procurador_service

    identificador = eproc_procurador_service.meu_identificador(processo)
    minhas_partes = eproc_procurador_service.partes_do_procurador(cabecalho, identificador)
    if minhas_partes:
        log_cb("info", f"DPU representa (cadastro oficial de partes): {', '.join(sorted(minhas_partes))}.")
    else:
        log_cb("muted", "Não foi possível resolver automaticamente quem a DPU representa "
                        "(identidade do procurador indisponível ou cadastro não bateu) — a IA vai "
                        "determinar isso pela leitura dos documentos.")

    prompt = _montar_prompt_sentenca_merito(processo, cabecalho, secoes, linha_do_tempo, minhas_partes)
    log_cb("info", "Pedindo à IA a percorrer a análise completa (representação, natureza da "
                   "atuação, tese sustentada, resultado e orientação) — pode levar alguns "
                   "minutos…")
    saida, erro = await _chamar_claude_texto(prompt, timeout=900)
    if erro:
        log_cb("erro", f"Falha ao conferir a sentença de mérito: {erro}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=erro)
        return {"ok": False, "erro": erro}

    parsed = _parse_classificacao_sentenca_merito(saida)
    classificacao = parsed["classificacao"]
    log_cb("ok", f"Classificação: {classificacao} (sem minuta — este fluxo não gera petição).")
    confronto = {
        "classificacao": classificacao, "justificativa": parsed["justificativa"],
        "representacao": parsed["representacao"],
        "natureza_atuacao": parsed["natureza_atuacao"],
        "tese_sustentada": parsed["tese_sustentada"],
        "resultado_sentenca": parsed["resultado_sentenca"],
        "diligencias": [], "destinatario_diligencia": "", "precisa_documentos": False,
    }
    intimacao_service.salvar_estado(chave, confronto=confronto, minuta=None,
                                    status_geral="aguardando_revisao", erro=None)
    return {"ok": True, "confronto": confronto, "minuta": None}


# --------------------------------------------------------------------------
# Encadeamento completo
# --------------------------------------------------------------------------

async def rodar_pipeline(processo: str, ato: str, log_cb: LogCb) -> dict:
    """Encadeia (a) resumo -> confere vínculo de PAJ -> [fluxo especializado
    de laudo pericial/RPV/sentença homologatória, OU (b) confronto -> (c)
    minuta genérica]. Cada etapa já persiste seu próprio resultado parcial
    (ver `services.intimacao_service.salvar_estado`), então uma falha no
    meio do caminho não perde o que já foi apurado — o usuário pode rodar de
    novo depois de corrigir o problema (ex.: vincular o PAJ).

    `ato` identifica o SUB-CASO (ver módulo `intimacao_service` — mudança
    2026-09-XX, pedido explícito do usuário: "o foco é a intimação, não o
    processo"). Um processo com 2 atos autônomos (ex.: Sentença + RPV) roda
    2 pipelines INDEPENDENTES, um por `ato`, cada um com seu próprio resumo/
    classificação/minuta/status — nenhum interfere no outro. `ato` já vem
    resolvido pelo chamador (`routes/intimacoes.py`, a partir de um item de
    `intimacao_service.listar_casos()`), nunca escolhido aqui.

    ⚠️ Nenhuma etapa daqui SINCRONIZA, LÊ ou ANALISA o PAJ (pedido explícito
    do usuário, 2026-08-29: "não deveremos mais analisar o PAJ em nenhuma
    situação... toda a análise da intimação deverá ficar restrita aos atos
    do processo"). O vínculo de PAJ (`paj_norm`) continua exigido só como
    IDENTIFICADOR — pra saber em qual pasta salvar a minuta gerada —, nunca
    pra ler o conteúdo/narrativa/movimentações do PAJ em si. Se a
    classificação depender de algo que só o PAJ tem, a IA é instruída a
    responder REQUER_ANALISE_HUMANA e deixar essa análise para o Defensor
    fazer manualmente."""
    chave = intimacao_service.chave_caso(processo, ato)
    caso0 = intimacao_service.obter_caso(processo, ato)
    if not caso0:
        msg = "Ato não encontrado (pode já ter saído da caixa de Prazos e-proc)."
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}
    prazos_do_ato = caso0.get("prazos") or []

    r1 = await analisar_processo(processo, ato, prazos_do_ato, log_cb)
    if not r1.get("ok"):
        return r1

    caso = intimacao_service.obter_caso(processo, ato)
    if not caso or not caso.get("paj_norm"):
        msg = "PAJ ainda não vinculado a este processo — vincule manualmente e rode novamente."
        log_cb("erro", msg)
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    # Caso muito frequente (pedido explícito do usuário): quando o ATO
    # PRINCIPAL da intimação é um laudo médico pericial, o fluxo dedicado
    # (padrão do Project pessoal do usuário) substitui inteiramente o
    # confronto/minuta genéricos — não sincroniza o PAJ, só os documentos do
    # processo judicial (ver `gerar_impugnacao_laudo_pericial`).
    sigla_principal = (caso.get("evento_referenciado") or {}).get("sigla_principal") or ""
    # JMU (processo penal militar, 17/10/2026): os fluxos especializados abaixo
    # (laudo médico pericial, RPV, sentença homologatória de acordo com o INSS,
    # sentença de mérito previdenciária) são do TRF4 — na JMU vai direto ao
    # caminho geral (confronto → minuta), com o contexto penal militar nos prompts.
    from ingestao import eproc_jmu
    if eproc_jmu.eh_processo_jmu(processo):
        log_cb("info", "Processo da Justiça Militar da União — análise pelo caminho geral "
                       "(sem os fluxos especializados previdenciários).")
        sigla_principal = ""
    if _eh_sigla_laudo_pericial(sigla_principal):
        log_cb("novo", f"Ato principal é um laudo pericial ({sigla_principal}) — usando o fluxo "
                       "especializado de impugnação de laudo, não o confronto genérico.")
        intimacao_service.salvar_estado(chave, escopo_verificacao={
            "modo": "especializado_laudo_pericial",
            "motivo": "Fluxo dedicado a laudo pericial médico — usa só os documentos do processo "
                      "judicial (petição inicial, intercorrentes, laudo(s)); não precisa "
                      "sincronizar o PAJ nem rodar o confronto genérico.",
            "escalonado": False, "motivo_escalonamento": None,
        })
        r_laudo = await gerar_impugnacao_laudo_pericial(
            processo, ato, caso["paj_norm"], Path(config.PAJS_DIR) / caso["paj_norm"], log_cb)
        if not r_laudo.get("ok"):
            return r_laudo
        log_cb("ok", "Triagem concluída (fluxo especializado de laudo pericial) — pronta para "
                    "revisão do Defensor.")
        return {"ok": True}

    # Mesma lógica para RPV/precatório expedido para conferência — compara
    # os valores requisitados com o que foi acordado/decidido no processo
    # judicial (ver `gerar_conferencia_rpv`); também não sincroniza o PAJ.
    if _eh_sigla_rpv(sigla_principal):
        log_cb("novo", f"Ato principal é uma RPV/precatório ({sigla_principal}) — usando o fluxo "
                       "especializado de conferência de RPV, não o confronto genérico.")
        intimacao_service.salvar_estado(chave, escopo_verificacao={
            "modo": "especializado_rpv",
            "motivo": "Fluxo dedicado a conferência de RPV/precatório — compara os valores "
                      "requisitados com o que foi acordado/decidido no processo judicial; não "
                      "precisa sincronizar o PAJ nem rodar o confronto genérico.",
            "escalonado": False, "motivo_escalonamento": None,
        })
        r_rpv = await gerar_conferencia_rpv(
            processo, ato, caso["paj_norm"], Path(config.PAJS_DIR) / caso["paj_norm"], log_cb)
        if not r_rpv.get("ok"):
            return r_rpv
        log_cb("ok", "Triagem concluída (fluxo especializado de conferência de RPV) — pronta "
                    "para revisão do Defensor.")
        return {"ok": True}

    # Mesma lógica para sentença homologatória de acordo — confere se ela é
    # puramente remissiva ou se reproduz (e bate) os termos do acordo; NUNCA
    # gera minuta, só classifica e explica (ver `gerar_conferencia_
    # sentenca_homologatoria`). Também não sincroniza o PAJ.
    evento_descricao = (caso.get("evento_referenciado") or {}).get("evento_descricao") or ""
    if eproc_jmu.eh_processo_jmu(processo):
        evento_descricao = ""
    if _eh_sentenca_homologatoria(sigla_principal, evento_descricao):
        log_cb("novo", f"Ato principal é uma sentença homologatória ({sigla_principal}) — usando "
                       "o fluxo especializado de conferência, não o confronto genérico.")
        intimacao_service.salvar_estado(chave, escopo_verificacao={
            "modo": "especializado_sentenca_homologatoria",
            "motivo": "Fluxo dedicado a conferência de sentença homologatória de acordo — "
                      "confere se ela reproduz (e bate com) os termos da proposta original; não "
                      "precisa sincronizar o PAJ nem rodar o confronto genérico. Nunca gera "
                      "minuta — só classifica e explica.",
            "escalonado": False, "motivo_escalonamento": None,
        })
        r_sent = await gerar_conferencia_sentenca_homologatoria(
            processo, ato, caso["paj_norm"], Path(config.PAJS_DIR) / caso["paj_norm"], log_cb)
        if not r_sent.get("ok"):
            return r_sent
        log_cb("ok", "Triagem concluída (fluxo especializado de sentença homologatória) — pronta "
                    "para revisão do Defensor.")
        return {"ok": True}

    # Mesma sigla SENT*, mas SEM "HOMOLOG" na descrição do evento — sentença
    # de MÉRITO (procedente/parcial/improcedente/extinção), não de acordo.
    # Análise bem mais complexa (ver comentário no topo da seção): quem a
    # DPU representa, natureza da atuação (ordinária ou curadoria especial,
    # com ou sem conflito de interesses), tese sustentada, resultado e só
    # então a orientação (ciência/embargos/recurso). NUNCA gera minuta.
    if _eh_sentenca_merito(sigla_principal, evento_descricao):
        log_cb("novo", f"Ato principal é uma sentença de mérito ({sigla_principal}) — usando o "
                       "fluxo especializado de análise, não o confronto genérico.")
        intimacao_service.salvar_estado(chave, escopo_verificacao={
            "modo": "especializado_sentenca_merito",
            "motivo": "Fluxo dedicado a sentença de mérito — analisa quem a DPU representa, a "
                      "natureza da atuação (representação ordinária ou curadoria especial) e o "
                      "interesse recursal contra o resultado; não precisa sincronizar o PAJ nem "
                      "rodar o confronto genérico. Nunca gera minuta — só classifica e explica.",
            "escalonado": False, "motivo_escalonamento": None,
        })
        r_merito = await gerar_conferencia_sentenca_merito(
            processo, ato, caso["paj_norm"], Path(config.PAJS_DIR) / caso["paj_norm"], log_cb)
        if not r_merito.get("ok"):
            return r_merito
        log_cb("ok", "Triagem concluída (fluxo especializado de sentença de mérito) — pronta para "
                    "revisão do Defensor.")
        return {"ok": True}

    r2 = await confrontar_com_paj(processo, ato, log_cb)
    if not r2.get("ok"):
        return r2

    classif = (r2.get("confronto") or {}).get("classificacao")
    if classif in ("CUMPRIMENTO_IMEDIATO_PETICIONAR", "DILIGENCIA_NECESSARIA"):
        r3 = await gerar_minuta(processo, ato, log_cb)
        if not r3.get("ok"):
            return r3
    else:
        intimacao_service.salvar_estado(chave, status_geral="aguardando_revisao")

    log_cb("ok", "Triagem concluída — pronta para revisão do Defensor.")
    return {"ok": True}
