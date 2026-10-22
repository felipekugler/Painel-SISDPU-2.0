"""Minutar Ofício no SEI (menu "Ações com IA" › "Minutar Ofício").

Fluxo em 3 etapas, com o Defensor no controle de cada uma:
  1. `iniciar_redacao` — a IA redige o ofício a partir da caixa "Orientação
     diretiva para análise da IA" + dados do PAJ (assistidos/CPF,
     representante legal, pretensão, narrativa, análises já feitas). Devolve
     só TEXTO estruturado (destinatário / assunto / referência / corpo) pra
     revisão na tela — nada vai pro SEI aqui.
  2. `criar_no_sei` — grava o texto REVISADO como minuta (não assinada) no
     processo de controle de ofícios do setor previdenciário
     (08166.000183/2017-39), na unidade COFE CTB, via `ingestao.sei_mcp_client`
     (chamada direta, sem IA no meio). Confere a gravação com uma leitura
     independente.
  3. "Abrir para assinar" (front) — abre o SEI já no documento
     (`sei_mcp_client.link_documento`); o Defensor assina com a própria senha.
     A assinatura é ato pessoal — o painel NÃO assina nem preenche senha, e o
     MCP-SEI nem tem essa função (bloqueada por desenho da STI).
O envio ao destinatário (E-mail pelo SEI) continua com a assessoria.

Padrão de redação: o dos ofícios reais da COFE CTB nesse processo (ex.: Ofício
9331099/2026 — lido no piloto de 28/09/2026): art. 44, X, da LC 80/94; prazo
de 15 dias; resposta para dpu.prev.pr@dpu.gov.br; referência expressa ao PAJ.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from ingestao import sei_mcp_client as sei
from services.chat_service import CLAUDE_CMD

PROCESSO_OFICIOS = "08166.000183/2017-39"
ID_PROCEDIMENTO_FALLBACK = "10000001908833"  # consultado no piloto (28/09/2026)
UNIDADE_COFE_CTB = 110002671
TIPO_OFICIO = 649
EMAIL_RESPOSTA = "dpu.prev.pr@dpu.gov.br"

VOCATIVO_PADRAO = "Prezado(a) Senhor(a),"
FECHAMENTO_PADRAO = "Atenciosamente,"

ARQ_RASCUNHO = "oficio_sei_rascunho.json"
ARQ_CRIADOS = "oficios_sei.json"

_CLS_ESQ = "Texto_Alinhado_Esquerda_Espaçamento_Simples"
_CLS_CORPO = "Texto_Justificado_Recuo_Primeira_Linha"

# paj_norm -> {status: idle|running|done|error, erro}
_status: dict[str, dict] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistência por PAJ
# ---------------------------------------------------------------------------

def _pasta(paj_norm: str) -> Path:
    p = PAJS_DIR / paj_norm
    if not p.exists():
        raise FileNotFoundError(f"PAJ não encontrado: {paj_norm}")
    return p


def _ler_json(path: Path, padrao):
    with contextlib.suppress(Exception):
        return json.loads(path.read_text(encoding="utf-8"))
    return padrao


def _gravar_json(path: Path, dados) -> None:
    path.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def status(paj_norm: str) -> dict:
    st = dict(_status.get(paj_norm, {"status": "idle"}))
    pasta = PAJS_DIR / paj_norm
    rascunho = _ler_json(pasta / ARQ_RASCUNHO, None)
    if rascunho:
        rascunho = _separar_vocativo_fechamento(rascunho)
    if rascunho and st.get("status") in (None, "idle"):
        st["status"] = "done"
    st["rascunho"] = rascunho
    st["criados"] = _ler_json(pasta / ARQ_CRIADOS, [])
    # Juntada marcada "running" sem tarefa viva = o painel reiniciou no meio.
    if not _tarefas_juntada and any(o.get("juntada") == "running" for o in st["criados"]):
        for o in st["criados"]:
            if o.get("juntada") == "running":
                o["juntada"] = "erro"
                o["erro_juntada"] = "Interrompida (o painel reiniciou) — confira as movimentações do PAJ antes de repetir."
        _gravar_json(pasta / ARQ_CRIADOS, st["criados"])
    # Juntada "preparada" (console Ações no SISDPU pré-preenchido): concluída
    # quando o PDF do ofício aparece como documento de alguma movimentação do PAJ.
    preparadas = [o for o in st["criados"] if o.get("juntada") == "preparada" and o.get("juntada_arquivo")]
    if preparadas:
        meta = _ler_json(pasta / "metadata.json", {})
        movs = (meta.get("detalhes_sisdpu") or {}).get("movimentacoes") or []
        mudou = False
        for o in preparadas:
            alvo = Path(o["juntada_arquivo"]).stem.lower()
            for mv in movs:
                if any(Path(str(d.get("nomeArquivo") or "")).stem.lower() == alvo
                       for d in (mv.get("documentos") or []) if isinstance(d, dict)):
                    o.update(juntada="ok", juntada_mov_seq=mv.get("seq"), juntada_em=mv.get("data_original") or "")
                    mudou = True
                    break
        if mudou:
            _gravar_json(pasta / ARQ_CRIADOS, st["criados"])
    st["sei_conectado"] = sei.conectado()
    return st


def salvar_rascunho(paj_norm: str, rascunho: dict) -> dict:
    _gravar_json(_pasta(paj_norm) / ARQ_RASCUNHO, _limpar_rascunho(rascunho))
    return {"ok": True}


def descartar_rascunho(paj_norm: str) -> dict:
    with contextlib.suppress(FileNotFoundError):
        (_pasta(paj_norm) / ARQ_RASCUNHO).unlink()
    with _lock:
        _status.pop(paj_norm, None)
    return {"ok": True}


def _limpar_rascunho(r: dict) -> dict:
    return {
        "destinatario": str(r.get("destinatario") or "").strip(),
        "assunto": _sem_rotulo(str(r.get("assunto") or ""), "Assunto"),
        "referencia": _sem_rotulo(str(r.get("referencia") or ""), "Referência"),
        "vocativo": str(r.get("vocativo") or "").strip(),
        "corpo_html": _sem_cores(str(r.get("corpo_html") or "")).strip(),
        "fechamento": str(r.get("fechamento") or "").strip(),
        "observacoes": str(r.get("observacoes") or "").strip(),
        "orientacao": str(r.get("orientacao") or "").strip(),
        # Restrito (LGPD — "Informação pessoal"): aplicado no SEI logo após gravar.
        "restrito": bool(r.get("restrito")),
        "motivo_restrito": str(r.get("motivo_restrito") or "").strip(),
        # Edição de minuta já existente no SEI ("Abrir ofício nº"): {documento_id,
        # numero_sei, processo, versao}. Sem isso, "Criar no SEI" cria um novo.
        "edicao": r.get("edicao") if isinstance(r.get("edicao"), dict) and r["edicao"].get("documento_id") else None,
        # Ofício de reiteração: nº/ano do ofício reiterado (só informativo).
        "reiteracao_de": str(r.get("reiteracao_de") or "").strip(),
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }


def _sem_rotulo(texto: str, rotulo: str) -> str:
    """O modelo do SEI já escreve "Assunto:"/"Referência:" — o campo leva só o
    conteúdo (com o rótulo junto, o ofício saía com o rótulo duplicado)."""
    t = texto.strip()
    return re.sub(rf"^\s*{rotulo[:-1]}\w?\s*:\s*", "", t, flags=re.I).strip() if rotulo else t


_RE_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)


def _separar_vocativo_fechamento(r: dict) -> dict:
    """Rascunho antigo: "Prezado(a) Senhor(a)…," era o 1º parágrafo do corpo e
    "Atenciosamente," o último — passam para os campos próprios (seções
    Vocativo e Fechamento do modelo de Ofício)."""
    if "vocativo" in r and "fechamento" in r:
        return r
    r = dict(r)
    corpo = r.get("corpo_html") or ""
    paras = list(_RE_P.finditer(corpo))
    vocativo = fechamento = ""
    if paras and re.match(r"\s*(prezad|senhor|excelent|ilustr)", _texto_html(paras[0].group(1)), re.I):
        vocativo = _texto_html(paras[0].group(1))
        corpo = corpo[:paras[0].start()] + corpo[paras[0].end():]
        paras = list(_RE_P.finditer(corpo))
    if paras and re.match(r"\s*(atenciosamente|respeitosamente|cordialmente)", _texto_html(paras[-1].group(1)), re.I):
        fechamento = _texto_html(paras[-1].group(1))
        corpo = corpo[:paras[-1].start()] + corpo[paras[-1].end():]
    r["corpo_html"] = corpo.strip()
    r.setdefault("vocativo", vocativo or VOCATIVO_PADRAO)
    r.setdefault("fechamento", fechamento or FECHAMENTO_PADRAO)
    return r


# ---------------------------------------------------------------------------
# 1. Redação pela IA
# ---------------------------------------------------------------------------

def iniciar_redacao(paj_norm: str, orientacao: str, base: dict | None = None) -> dict:
    """`base` (opcional): {"documento_id", "modo": "reiterar"|"base"|"revisar"} —
    ofício já existente no SEI que a IA usa como ponto de partida:
      - reiterar: novo ofício de REITERAÇÃO ao mesmo destinatário (orientação opcional);
      - base: novo ofício usando o existente como modelo;
      - revisar: o rascunho em edição ("Abrir ofício nº") é reescrito conforme a
        orientação, mantendo o modo edição (salva no MESMO documento)."""
    orientacao = (orientacao or "").strip()
    modo = (base or {}).get("modo") or ""
    if not orientacao and modo != "reiterar":
        return {"ok": False, "erro": "Escreva na caixa \"Orientação diretiva\" o que o ofício deve pedir e a "
                                      "quem (ex.: \"oficiar o SINE Curitiba pedindo cadastro da instituidora "
                                      "após 04/2023\")."}
    if base and modo not in ("reiterar", "base", "revisar"):
        return {"ok": False, "erro": f"Modo desconhecido: {modo!r}."}
    _pasta(paj_norm)
    atual = _status.get(paj_norm)
    if atual and atual.get("status") == "running":
        return {"ok": True, "status": "running"}
    with _lock:
        _status[paj_norm] = {"status": "running", "erro": "", "etapa": "Atualizando o PAJ…",
                             "iniciado_em": datetime.now().isoformat(timespec="seconds")}
    # Chamado de rota async: a atualização do PAJ (SIS-DPU 2.0) roda no loop do
    # painel (usa o lock da sincronização); a IA roda numa thread.
    t = asyncio.get_running_loop().create_task(_redigir_tarefa(paj_norm, orientacao, base))
    _tarefas_redacao.add(t)
    t.add_done_callback(_tarefas_redacao.discard)
    return {"ok": True, "status": "running"}


_tarefas_redacao: set = set()


def _falhar(paj_norm: str, erro: str) -> None:
    with _lock:
        _status[paj_norm] = {"status": "error", "erro": erro}


def _etapa(paj_norm: str, texto: str) -> None:
    with _lock:
        st = _status.get(paj_norm)
        if st and st.get("status") == "running":
            st["etapa"] = texto[:160]


_RE_MOV_CITADA = re.compile(r"mov(?:imenta[çc][ãa]o|\.)?\s*(?:n[º°o.]*\s*)?(\d{1,4})", re.I)


def _movs_citadas(orientacao: str) -> set[int]:
    return {int(n) for n in _RE_MOV_CITADA.findall(orientacao or "")}


async def _atualizar_paj(paj_norm: str, orientacao: str) -> list[str]:
    """Relê o PAJ no SIS-DPU 2.0 e baixa os anexos que faltam das últimas
    movimentações + das CITADAS na orientação. Falha aqui não impede a redação
    (a IA trabalha com o que já estiver em disco) — só vira aviso."""
    avisos: list[str] = []
    try:
        from services import ao_vivo_service as ao_vivo
    except Exception:  # noqa: BLE001
        return avisos
    if not ao_vivo.ativo():
        return ["O painel não está conectado ao SIS-DPU 2.0 — usei os dados do PAJ já sincronizados."]
    r = await ao_vivo.atualizar_paj(paj_norm, forcar=True)
    if not r.get("ok") and r.get("motivo") not in ("recente",):
        avisos.append(f"Não consegui reler o PAJ no SIS-DPU 2.0 ({r.get('motivo')}) — usei a última sincronização.")
    _etapa(paj_norm, "Baixando anexos das movimentações…")
    meta = _ler_json(PAJS_DIR / paj_norm / "metadata.json", {})
    movs = (meta.get("detalhes_sisdpu") or {}).get("movimentacoes") or []
    citadas = _movs_citadas(orientacao)
    alvo = {str(m.get("seq")): m for m in ao_vivo._movs_para_download(paj_norm)}
    for m in movs:
        with contextlib.suppress(ValueError):
            if int(str(m.get("seq") or 0)) in citadas:
                alvo[str(m.get("seq"))] = m
    if not alvo:
        return avisos

    async def _rodar():
        tok = ao_vivo.fonte.MODO.set("paj")
        try:
            return await ao_vivo.sincronizador._baixar_anexos_sisdpu(
                PAJS_DIR / paj_norm / "Arquivos", lambda _l: None, limite=0,
                movs=list(alvo.values()), rodar_ocr=False)
        finally:
            ao_vivo.fonte.MODO.reset(tok)

    for _ in range(24):  # espera a vez se houver outra sincronização (até ~2 min)
        res = await ao_vivo._com_lock(_rodar)
        if res is not None:
            break
        await asyncio.sleep(5)
    else:
        avisos.append("Havia uma sincronização em andamento — anexos recentes podem não ter sido baixados.")
    return avisos


async def _redigir_tarefa(paj_norm: str, orientacao: str, base: dict | None = None) -> None:
    avisos: list[str] = []
    try:
        avisos = await _atualizar_paj(paj_norm, orientacao)
    except Exception as e:  # noqa: BLE001
        avisos = [f"Atualização do PAJ falhou ({type(e).__name__}) — usei os dados já sincronizados."]
    await asyncio.to_thread(_redigir_worker, paj_norm, orientacao, avisos, base)


def _texto_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</td>|</tr>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def _contexto_paj(paj_norm: str) -> dict:
    pasta = _pasta(paj_norm)
    meta = _ler_json(pasta / "metadata.json", {})
    raw = meta.get("sisdpu_raw") or {}
    assistidos = [{"nome": a.get("nome", ""), "cpf": a.get("cpf", ""), "principal": bool(a.get("principal"))}
                  for a in (raw.get("assistidos_pf") or [])]
    if not assistidos and raw.get("assistido"):
        assistidos = [{"nome": raw.get("assistido"), "cpf": raw.get("cpf_assistido", ""), "principal": True}]
    representantes = [{"nome": r.get("nome", ""), "cpf": r.get("cpf", "")}
                      for r in (raw.get("representantes_legais") or [])]
    analises = []
    for nome in ("Parecer de Viabilidade Juridica.md", "analise_ia.md"):
        f = pasta / nome
        if f.exists():
            with contextlib.suppress(Exception):
                analises.append(f"### {nome}\n" + f.read_text(encoding="utf-8")[:8000])
    f = pasta / "analise_pretensao.json"
    if f.exists():
        with contextlib.suppress(Exception):
            analises.append("### analise_pretensao.json\n" + f.read_text(encoding="utf-8")[:6000])
    return {
        "paj": meta.get("paj") or raw.get("paj") or paj_norm,
        "pretensao": meta.get("pretensaopura") or meta.get("pretensao") or "",
        "assistidos": assistidos,
        "representantes": representantes,
        "processos_judiciais": [p.get("processo_judicial") for p in (meta.get("processos_judiciais") or [])
                                if p.get("processo_judicial")],
        "narrativa": _texto_html(raw.get("narrativa", ""))[:6000],
        "analises": "\n\n".join(analises),
    }


_MODELO_REFERENCIA = """\
Destinatário (uma informação por linha):
  AGÊNCIA DA PREVIDÊNCIA SOCIAL CÂNDIDO LOPES
  Rua Cândido Lopes, 270 – Centro
  CEP: 80.020-060 - Curitiba/PR
  e-mail: gexctb@inss.gov.br
Assunto: Solicitação de documentos
Vocativo: Prezado(a) Senhor(a),
Corpo:
  A DEFENSORIA PÚBLICA DA UNIÃO, com fulcro no art. 44, inciso X, da Lei Complementar nº 80/94, e atuando nos interesses do assistido FULANO DE TAL, CPF 000.000.000-00, solicita envio dos seguintes documentos:
  a) cópia do processo administrativo do NB 000.000.000-0
  Contando com sua presteza, solicito atendimento no prazo de 15 dias, podendo encaminhar sua resposta com documento digitalizado para o e-mail dpu.prev.pr@dpu.gov.br.
  Ao responder, por gentileza, fazer referência ao Processo de Assistência Jurídica PAJ 2026/029-00000.
Fechamento: Atenciosamente,

Outro exemplo real (SINE, pensão por morte, dois menores): o 1º parágrafo apresenta os
assistidos com CPF e a pretensão, o 2º explica em uma frase por que a informação é
necessária (ex.: demonstrar a qualidade de segurada da instituidora, falecida em
10/07/2024), o 3º faz o pedido com fundamento no art. 44, X, da LC 80/94."""


def _inventario_paj(pasta: Path, citadas: set[int]) -> str:
    """Índice do que existe na pasta do PAJ, para a IA saber o que abrir:
    movimentações (seq, data, descrição, arquivos em disco) e demais arquivos.
    As citadas na orientação vêm com a descrição inteira e marcadas."""
    meta = _ler_json(pasta / "metadata.json", {})
    movs = (meta.get("detalhes_sisdpu") or {}).get("movimentacoes") or []
    arquivos = sorted(p.name for p in (pasta / "Arquivos").glob("*")) if (pasta / "Arquivos").exists() else []
    por_seq: dict[str, list[str]] = {}
    for nome in arquivos:
        m = re.match(r"seq(\d+) - ", nome)
        if m:
            por_seq.setdefault(str(int(m.group(1))), []).append(nome)

    def _seq(mv):
        with contextlib.suppress(ValueError):
            return int(str(mv.get("seq") or 0))
        return 0

    linhas = []
    for mv in sorted(movs, key=_seq, reverse=True):
        seq = str(_seq(mv))
        citada = _seq(mv) in citadas
        desc = _texto_html(mv.get("descricao") or "") or (mv.get("movimentacao") or "")
        if not citada:
            desc = desc[:220] + ("…" if len(desc) > 220 else "")
        arqs = por_seq.get(seq, [])
        docs_remotos = [d.get("nomeArquivo", "") for d in (mv.get("documentos") or []) if isinstance(d, dict)]
        faltando = [n for n in docs_remotos if n and not any(n in a for a in arqs)]
        linhas.append(
            f"- {'★ CITADA — ' if citada else ''}mov {seq} ({mv.get('data') or ''}, {mv.get('usuario') or ''}): {desc}"
            + (f"\n    arquivos em disco: {'; '.join('Arquivos/' + a for a in arqs)}" if arqs else "")
            + (f"\n    anexos NÃO baixados: {'; '.join(faltando)}" if faltando else ""))
    outros = sorted(p.name for p in pasta.iterdir()
                    if p.is_file() and p.suffix.lower() in (".md", ".txt", ".pdf", ".docx", ".json")
                    and p.name not in ("metadata.json", ARQ_RASCUNHO, ARQ_CRIADOS))
    subpastas = sorted(p.name for p in pasta.iterdir() if p.is_dir() and p.name != "Arquivos")
    return ("### Movimentações (mais recentes primeiro)\n" + ("\n".join(linhas) or "(nenhuma)")
            + "\n\n### Outros arquivos na pasta do PAJ\n" + (", ".join(outros) or "(nenhum)")
            + ("\n\n### Subpastas\n" + ", ".join(subpastas) if subpastas else ""))


def _bloco_base(base_doc: dict | None, modo: str) -> str:
    """Trecho do prompt com o ofício existente que serve de ponto de partida."""
    if not base_doc:
        return ""
    texto = (f"Destinatário:\n{base_doc.get('destinatario', '')}\n"
             f"Assunto: {base_doc.get('assunto', '')}\n"
             + (f"Referência: {base_doc['referencia']}\n" if base_doc.get("referencia") else "")
             + f"Vocativo: {base_doc.get('vocativo', '')}\n"
             f"Corpo:\n{_texto_html(base_doc.get('corpo_html', ''))}\n"
             f"Fechamento: {base_doc.get('fechamento', '')}")
    rotulo = f"Ofício nº {base_doc.get('numero') or '?'}" + (f", de {base_doc['data']}" if base_doc.get("data") else "")
    if modo == "reiterar":
        return f"""
## OFÍCIO A REITERAR — {rotulo} (processo SEI {base_doc.get('processo', '')})
{texto}

## TAREFA ESPECÍFICA: ofício de REITERAÇÃO
- Mesmo destinatário e mesmo vocativo do ofício original (o painel copia o destinatário — não altere).
- Assunto: "Reiteração do {rotulo.split(',')[0]} - <assunto original>".
- Corpo: informe que o {rotulo} não foi respondido até o momento (salvo se a ordem do Defensor disser
  outra coisa), reitere o pedido de forma clara (resuma o que foi pedido, sem copiar o texto inteiro),
  renove o prazo (15 dias, salvo ordem diversa), resposta para {EMAIL_RESPOSTA} e referência ao PAJ.
- Não precisa reler o PAJ inteiro: só o necessário para conferir dados; o original é a fonte principal.
"""
    if modo == "revisar":
        return f"""
## OFÍCIO EM EDIÇÃO — {rotulo} (minuta não assinada, processo SEI {base_doc.get('processo', '')})
{texto}

## TAREFA ESPECÍFICA: REVISAR este ofício conforme a ordem do Defensor
- Faça só as mudanças pedidas; mantenha o resto do texto como está (inclusive destinatário,
  salvo se a ordem mandar mudar). Devolva o ofício COMPLETO já revisado no formato abaixo.
"""
    return f"""
## OFÍCIO DE REFERÊNCIA (use como modelo de estrutura, tom e pedido) — {rotulo}
{texto}
"""


def _montar_prompt(ctx: dict, orientacao: str, pasta: Path, inventario: str, dir_eproc: Path,
                   base_doc: dict | None = None, modo: str = "") -> str:
    assistidos = "\n".join(
        f"  - {a['nome']} — CPF {a['cpf'] or '(não informado)'}{' (principal)' if a['principal'] else ''}"
        for a in ctx["assistidos"]) or "  (não informado)"
    reps = "\n".join(f"  - {r['nome']} — CPF {r['cpf'] or '(não informado)'}" for r in ctx["representantes"])
    if not orientacao and modo == "reiterar":
        orientacao = "Reiterar o ofício abaixo, que não foi respondido."
    return f"""Você é assessor de Defensor Público Federal da Defensoria Pública da União (DPU), setor
previdenciário de Curitiba/PR (Coordenação dos Ofícios Especializados — COFE CTB). Sua tarefa é
INVESTIGAR o caso e redigir UM ofício administrativo a ser minutado no SEI.

## ORDEM DO DEFENSOR (decisão já tomada — siga à risca; define destinatário e pedido)
{orientacao}
{_bloco_base(base_doc, modo)}
## Como trabalhar (você tem ferramentas — USE-AS antes de redigir)
- A pasta do PAJ é o diretório atual: {pasta}
  Leia com a ferramenta Read os documentos de que precisar (PDFs inclusive — o Read lê PDF; se houver
  um .txt de OCR com o mesmo nome, ele é mais rápido). Use Glob/Grep para achar arquivos e trechos.
- Movimentações marcadas "★ CITADA" no índice abaixo foram apontadas pelo Defensor: leia a descrição
  e TODOS os arquivos delas antes de redigir.
- Processo judicial: use as ferramentas MCP do e-proc (eproc-trf4 para TRF4/JFPR — número com ".4.04.";
  eproc-jmu para Justiça Militar — número com ".7."). Comece por triagem_processo/listar_eventos e só
  baixe peças com baixar_documentos se precisar, passando diretorio_destino="{dir_eproc}".
- Endereço/e-mail oficial de órgão público: pode confirmar na web (WebSearch/WebFetch), SEM colocar
  nome, CPF ou qualquer dado pessoal da assistida na busca.
- Você NÃO grava nada em lugar nenhum: só lê. O resultado é o JSON final.
- Seja objetivo: leia o que for relevante para o ofício, não o PAJ inteiro.

## Índice do PAJ
{inventario}

## Dados do PAJ
- PAJ: {ctx['paj']}
- Pretensão: {ctx['pretensao']}
- Assistido(s):
{assistidos}
{('- Representante(s) legal(is):' + chr(10) + reps) if reps else ''}
- Processo(s) judicial(is) vinculado(s): {', '.join(ctx['processos_judiciais']) or '(nenhum)'}

## Narrativa do PAJ (SISDPU)
{ctx['narrativa'] or '(vazia)'}

## Análises já feitas no painel (use para achar NB, DER, datas, nomes — não invente)
{ctx['analises'] or '(nenhuma)'}

## Modelo da unidade (ofícios reais da COFE CTB — siga estrutura e tom)
{_MODELO_REFERENCIA}

## Regras
1. Linguagem formal, concisa, sem jargão desnecessário. Use "assistido(a)" e "Defensoria Pública da União".
2. NUNCA invente dado: endereço, e-mail, NB, datas ou nomes que não estejam na ordem do Defensor, nos
   dados acima ou que você não conheça com segurança. Se faltar, escreva o marcador entre colchetes
   (ex.: "[ENDEREÇO A CONFIRMAR]", "[E-MAIL A CONFIRMAR]") e registre em "observacoes".
   Endereço/e-mail de órgão público: só use se tiver certeza; na dúvida, marcador.
3. Prazo padrão de 15 dias e resposta para {EMAIL_RESPOSTA}, salvo ordem diversa do Defensor.
4. Menores/incapazes: mencione o representante legal se houver.
5. Não inclua no corpo cabeçalho, número do ofício, data, vocativo ("Prezado(a) Senhor(a)…"),
   fechamento ("Atenciosamente") nem assinatura — cada um tem seção própria no SEI. O vocativo vai
   no campo "vocativo", ajustado ao destinatário (ex.: "Prezado(a) Senhor(a) Diretor(a),").
6. "assunto" é só o texto do assunto — sem a palavra "Assunto:" (o modelo do SEI já escreve).
7. Nível de acesso: sugira "restrito_sugerido": true quando o ofício trouxer dado pessoal sensível —
   saúde/deficiência, suspeita de crime ou violência, criança/adolescente em risco, dados bancários ou
   patrimoniais, endereço/telefone/e-mail pessoal do assistido — e explique em "motivo_restrito".
   Só nome, CPF, NB e a pretensão previdenciária (padrão dos ofícios da unidade) NÃO bastam.

## Formato da resposta
Responda APENAS com um objeto JSON válido (sem texto antes ou depois, sem cercas de código):
{{
  "destinatario_linhas": ["NOME DO ÓRGÃO/UNIDADE EM MAIÚSCULAS", "endereço", "CEP - cidade/UF", "e-mail: ..."],
  "assunto": "Solicitação de documentos",
  "vocativo": "Prezado(a) Senhor(a),",
  "corpo_paragrafos": ["parágrafo 1", "a) item", "parágrafo de prazo", "parágrafo de referência ao PAJ"],
  "observacoes": "o que o Defensor deve conferir antes de criar no SEI (ou string vazia)",
  "restrito_sugerido": false,
  "motivo_restrito": "",
  "fontes": ["mov 136 — Mensagens_Dona_Shirley_Organizadas.pdf", "e-proc 5000000-00.2026.4.04.7000 — evento 12", "..."]
}}
Em "fontes", liste o que você efetivamente leu e usou.
"""


def _extrair_json(saida: str) -> dict:
    saida = (saida or "").strip()
    m = re.search(r"\{.*\}", saida, flags=re.S)
    if not m:
        raise ValueError("a IA não devolveu JSON")
    return json.loads(m.group(0))


def _p(texto: str, cls: str) -> str:
    return f'<p class="{cls}">{html.escape(texto.strip())}</p>'


def _corpo_html(paragrafos: list[str]) -> str:
    # Vocativo e fechamento NÃO entram aqui — têm seção própria no modelo do
    # SEI (campos `vocativo`/`fechamento` do criar_minuta). No corpo, a
    # saudação aparecia colada ao texto principal (relato do Defensor, 16/10/2026).
    return "\n".join(_p(t, _CLS_CORPO) for t in paragrafos if str(t).strip())


# Servidores MCP SÓ DE LEITURA entregues à IA na redação (mesma configuração do
# `claude mcp list` do Defensor). O conector do SEI fica de fora de propósito
# (--strict-mcp-config): redigir não grava nada; a gravação é `criar_no_sei`,
# chamada direta e confirmada pelo Defensor.
_MCP_LEITURA = ("eproc-trf4", "eproc-jmu")
_TIMEOUT_REDACAO_SEG = 900


def _mcp_config_leitura() -> Path:
    cfg = _ler_json(Path.home() / ".claude.json", {}).get("mcpServers") or {}
    servidores = {n: cfg[n] for n in _MCP_LEITURA if n in cfg}
    arq = Path(tempfile.gettempdir()) / "painel_oficio_mcp.json"
    arq.write_text(json.dumps({"mcpServers": servidores}, ensure_ascii=False), encoding="utf-8")
    return arq


def _descrever_ferramenta(nome: str, entrada: dict) -> str:
    if nome == "Read":
        return "Lendo " + Path(str(entrada.get("file_path") or "")).name
    if nome in ("Glob", "Grep"):
        return "Procurando nos arquivos do PAJ…"
    if nome in ("WebSearch", "WebFetch"):
        return "Conferindo endereço/contato oficial na web…"
    if nome.startswith("mcp__eproc"):
        return "Consultando o processo no e-proc (" + nome.split("__")[-1].replace("_", " ") + ")…"
    return f"Usando {nome}…"


def _redigir_worker(paj_norm: str, orientacao: str, avisos_atualizacao: list[str] | None = None,
                    base: dict | None = None) -> None:
    try:
        from ingestao.eproc_mcp_client import EPROC_DOCS_DIR

        pasta = _pasta(paj_norm)
        ctx = _contexto_paj(paj_norm)
        dir_eproc = EPROC_DOCS_DIR
        dir_eproc.mkdir(parents=True, exist_ok=True)
        modo = (base or {}).get("modo") or ""
        base_doc = None
        edicao_atual = None
        if modo == "revisar":
            # Revisão do rascunho em edição: o texto ATUAL da tela (gravado) é a base.
            atual = _ler_json(pasta / ARQ_RASCUNHO, None) or {}
            edicao_atual = atual.get("edicao")
            if not edicao_atual:
                _falhar(paj_norm, "Não há ofício em edição para revisar — use \"Abrir ofício nº\" primeiro.")
                return
            base_doc = {**atual, "numero": edicao_atual.get("numero_sei"), "processo": edicao_atual.get("processo")}
        elif modo:
            _etapa(paj_norm, "Lendo o ofício de base no SEI…")
            base_doc = ler_oficio_para_rascunho(str(base.get("documento_id")))
        inventario = _inventario_paj(pasta, _movs_citadas(orientacao))
        prompt = _montar_prompt(ctx, orientacao, pasta, inventario, dir_eproc, base_doc, modo)
        _etapa(paj_norm, "IA analisando o PAJ…")
        # Agente SÓ DE LEITURA: lê a pasta do PAJ (cwd) e as peças do e-proc
        # baixadas; consulta o e-proc por MCP e a web. Bash/Write/Edit
        # bloqueados; nenhum outro MCP carregado (nem o do SEI).
        cmd = [*CLAUDE_CMD, "-p", "--output-format", "stream-json", "--verbose",
               "--strict-mcp-config", "--mcp-config", str(_mcp_config_leitura()),
               "--permission-mode", "bypassPermissions",
               "--add-dir", str(dir_eproc),
               "--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit", "Task", "Agent"]
        proc = subprocess.Popen(cmd, cwd=str(pasta), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        estourou = threading.Event()

        def _matar():
            estourou.set()
            proc.kill()

        relogio = threading.Timer(_TIMEOUT_REDACAO_SEG, _matar)
        relogio.start()
        resultado, bruto = "", []
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for linha in proc.stdout:
                bruto.append(linha)
                with contextlib.suppress(ValueError):
                    ev = json.loads(linha)
                    if ev.get("type") == "assistant":
                        for c in (ev.get("message") or {}).get("content") or []:
                            if c.get("type") == "tool_use":
                                _etapa(paj_norm, _descrever_ferramenta(c.get("name", ""), c.get("input") or {}))
                    elif ev.get("type") == "result":
                        resultado = ev.get("result") or ""
            proc.wait()
        finally:
            relogio.cancel()
        if estourou.is_set():
            raise subprocess.TimeoutExpired(cmd, _TIMEOUT_REDACAO_SEG)
        try:
            j = _extrair_json(resultado)
        except ValueError as e:
            saida = (resultado or "".join(bruto[-8:])).strip()
            if "Failed to authenticate" in saida or "Not logged in" in saida or "/login" in saida:
                _falhar(paj_norm, "O Claude Code deste computador está desconectado da sua conta (login "
                                  "expirado) — não é problema do SEI. Rode \"claude auth login\" num terminal, "
                                  "conclua o login no navegador e tente de novo.")
                return
            _falhar(paj_norm, f"Resposta da IA ilegível ({e}). Saída: {saida[-1200:] or '(vazia)'}")
            return
        obs = str(j.get("observacoes") or "").strip()
        extras = list(avisos_atualizacao or [])
        fontes = [str(f) for f in (j.get("fontes") or []) if str(f).strip()]
        if fontes:
            extras.append("Fontes consultadas pela IA: " + "; ".join(fontes))
        j["observacoes"] = "\n".join([x for x in [obs, *extras] if x])
        destinatario = "\n".join(str(x) for x in (j.get("destinatario_linhas") or []) if str(x).strip())
        vocativo = str(j.get("vocativo") or "").strip() or VOCATIVO_PADRAO
        if modo == "reiterar" and base_doc:
            # Reiteração vai ao MESMO destinatário — copiado do original, não da IA.
            destinatario = base_doc.get("destinatario") or destinatario
            vocativo = base_doc.get("vocativo") or vocativo
        restrito = bool(j.get("restrito_sugerido"))
        if base_doc and base_doc.get("restrito"):
            restrito = True  # o original já era restrito: a reiteração/revisão segue igual
        rascunho = _limpar_rascunho({
            "destinatario": destinatario,
            "assunto": j.get("assunto") or "Solicitação de documentos",
            # Vazia por padrão: o corpo do modelo já termina pedindo que a resposta
            # cite o PAJ — a referência padrão repetia a mesma frase.
            "referencia": (base_doc or {}).get("referencia", "") if modo == "revisar" else "",
            "vocativo": vocativo,
            "corpo_html": _corpo_html([str(x) for x in (j.get("corpo_paragrafos") or [])]),
            "fechamento": (base_doc or {}).get("fechamento") if modo == "revisar" else FECHAMENTO_PADRAO,
            "observacoes": j.get("observacoes") or "",
            "orientacao": orientacao,
            "restrito": restrito,
            "motivo_restrito": j.get("motivo_restrito") or "",
            "edicao": edicao_atual,
            "reiteracao_de": (base_doc or {}).get("numero", "") if modo == "reiterar" else "",
        })
        _gravar_json(_pasta(paj_norm) / ARQ_RASCUNHO, rascunho)
        with _lock:
            _status[paj_norm] = {"status": "done", "erro": "",
                                 "concluido_em": datetime.now().isoformat(timespec="seconds")}
    except subprocess.TimeoutExpired:
        _falhar(paj_norm, "A IA excedeu o tempo limite (15 min) redigindo o ofício.")
    except sei.SeiLoginNecessario:
        _falhar(paj_norm, "Conecte o painel ao SEI (botão \"Conectar ao SEI\") para ler o ofício de base.")
    except Exception as e:  # noqa: BLE001
        _falhar(paj_norm, f"{type(e).__name__}: {e}"[:500])


# ---------------------------------------------------------------------------
# 2. Gravação no SEI
# ---------------------------------------------------------------------------

def _destinatario_html(texto: str) -> str:
    linhas = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    return "\n".join(_p(ln, _CLS_ESQ) for ln in linhas)


_RE_COR_CSS = re.compile(r"(?:^|;)\s*(?:color|background(?:-color)?)\s*:[^;]*", re.I)


def _sem_cores(corpo: str) -> str:
    """Tira TODA cor de texto/fundo do HTML — o texto fica com a cor automática
    do SEI. O editor da tela roda no tema escuro do painel: texto colado ou
    copiado de dentro do painel chegava com `color: rgb(255,255,255)` (branco)
    e saía ILEGÍVEL no ofício (Ofício 9433745/2026, 21/10/2026)."""
    def _estilo(m: re.Match) -> str:
        resto = _RE_COR_CSS.sub("", m.group(2)).strip(" ;")
        return f' style="{resto}"' if resto else ""

    s = re.sub(r"""\sstyle\s*=\s*(["'])(.*?)\1""", lambda m: _estilo(m), corpo or "", flags=re.I | re.S)
    s = re.sub(r"\s(?:color|bgcolor)\s*=\s*([\"']).*?\1", "", s, flags=re.I)  # atributos legados
    s = re.sub(r"</?font\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"\sclass\s*=\s*([\"'])ql-(?:color|bg)-[^\"']*\1", "", s, flags=re.I)  # classes de cor do Quill
    # <span> que ficou sem atributo nenhum não serve pra nada — desembrulha
    for _ in range(3):
        s = re.sub(r"<span>(.*?)</span>", r"\1", s, flags=re.I | re.S)
    return s


def _normalizar_corpo(corpo: str) -> str:
    """O editor da tela (Quill) descarta classes que não conhece — reaplica o
    estilo de parágrafo do modelo de Ofício da COFE CTB em todo <p> — e tira
    qualquer cor (ver `_sem_cores`)."""
    return re.sub(r"<p(\s[^>]*)?>", f'<p class="{_CLS_CORPO}">', _sem_cores(corpo))


def _numero_oficio(texto: str) -> str:
    """Nº do ofício ("Ofício - nº 9416409/2026 - COFE CTB") — NÃO o nº do
    processo de controle (08166.000183/2017-39), que o regex antigo pegava."""
    m = re.search(r"Of[ií]cio\s*-?\s*n\S{0,2}\s*(\d{5,})\s*/\s*(\d{4})", texto or "", re.I)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


_id_procedimento_cache: dict[str, str] = {}


def _id_procedimento(processo: str = PROCESSO_OFICIOS) -> str:
    if processo not in _id_procedimento_cache:
        idp = ID_PROCEDIMENTO_FALLBACK if processo == PROCESSO_OFICIOS else ""
        with contextlib.suppress(Exception):
            p = sei.chamar("consultar_processo", {"numero_processo": processo, "nivel_acesso_efetivo": False})
            idp = str(p.get("id") or idp)
        if idp:
            _id_procedimento_cache[processo] = idp
        return idp
    return _id_procedimento_cache[processo]


# ---------------------------------------------------------------------------
# Restrito (LGPD) — hipótese "Informação pessoal" (art. 31, § 1º, I, Lei
# 12.527/2011), id 9 no SEI da DPU (listar_hipoteses_legais, 21/10/2026).
# ---------------------------------------------------------------------------

NIVEL_RESTRITO = 1
HIPOTESE_INFORMACAO_PESSOAL = "9"


def _nivel_acesso_atual(documento_id: str) -> str:
    d = sei.chamar("ler_documento", {"documento_id": str(documento_id), "formato": "texto"}, timeout=120)
    return str((d or {}).get("nivelAcesso") or (d or {}).get("nivel_acesso") or "") if isinstance(d, dict) else ""


def _aplicar_restrito(documento_id: str, descricao: str = "") -> str:
    """Torna o documento restrito com a hipótese "Informação pessoal" e confere
    pela releitura. Devolve "" se deu certo ou a mensagem de aviso."""
    args = {"id_documento": str(documento_id), "nivel_acesso": NIVEL_RESTRITO,
            "id_hipotese_legal": HIPOTESE_INFORMACAO_PESSOAL}
    if descricao:
        args["descricao"] = descricao  # a API do SEI pode exigir o reenvio da descrição
    try:
        sei.chamar("alterar_documento_interno", args, timeout=120)
    except sei.SeiErro as e:
        return f"Não consegui tornar o ofício restrito: {e}"[:400]
    with contextlib.suppress(Exception):
        nivel = _nivel_acesso_atual(documento_id).lower()
        if nivel and not nivel.startswith("restri"):
            return f"Pedi o nível restrito, mas o SEI mostra \"{nivel}\" — confira no SEI."
    return ""


def restringir(paj_norm: str, documento_id: str) -> dict:
    """Botão "Restringir" de um ofício já criado (assinado ou não)."""
    criados = _ler_json(_pasta(paj_norm) / ARQ_CRIADOS, [])
    registro = next((o for o in criados if str(o.get("documento_id")) == str(documento_id)), None)
    if not registro:
        return {"ok": False, "erro": "Ofício não pertence a este PAJ."}
    try:
        aviso = _aplicar_restrito(documento_id, registro.get("descricao_sei") or "")
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "Conecte o painel ao SEI."}
    if aviso:
        return {"ok": False, "erro": aviso}
    _salvar_registro(paj_norm, documento_id, nivel_acesso="restrito")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Localizar um ofício pelo número SEI (qualquer processo) — "Abrir ofício nº"
# ---------------------------------------------------------------------------
# A busca do próprio MCP (`buscar_documento`) não acha documento em processo
# grande, e a busca na caixa só olha os 15 primeiros processos (36 s). Por isso:
# 1) registro local (ofícios criados por qualquer PAJ do painel);
# 2) índice local de buscas anteriores;
# 3) busca binária nas PÁGINAS dos processos de ofícios conhecidos (o nº SEI é
#    crescente na ordem dos documentos; `start` do listar_documentos = página);
# 4) busca no teor (Solr) pelo número, que aparece no texto do próprio ofício.

PROCESSOS_OFICIOS_CONHECIDOS = ["08166.000007/2026-98", PROCESSO_OFICIOS]
_ARQ_INDICE = Path(__file__).resolve().parents[1].parent / "dados" / "_oficios_sei_indice.json"
_PAGINA = 100


def _indice() -> dict:
    return _ler_json(_ARQ_INDICE, {"documentos": {}, "processos": []})


def _gravar_indice(ind: dict) -> None:
    with contextlib.suppress(Exception):
        _ARQ_INDICE.parent.mkdir(parents=True, exist_ok=True)
        _gravar_json(_ARQ_INDICE, ind)


def _pagina(processo: str, p: int) -> list[dict]:
    r = sei.chamar("listar_documentos", {"numero_processo": processo, "start": p, "limit": _PAGINA}, timeout=120)
    return (r or {}).get("documentos") or [] if isinstance(r, dict) else []


def _num(d: dict) -> int:
    with contextlib.suppress(ValueError, TypeError):
        return int(str(d.get("numero") or "0"))
    return 0


def _procurar_no_processo(processo: str, numero: int, ind: dict, dica_pagina: int | None = None) -> tuple[dict, int] | None:
    """Busca binária nas páginas do processo. `ind` guarda a última página
    conhecida de cada processo e a página de cada documento já achado — a
    próxima busca custa 1-2 consultas em vez de ~10 (cada uma ~1-2 s)."""
    cache: dict[int, list[dict]] = {}

    def pg(p: int) -> list[dict]:
        if p not in cache:
            cache[p] = _pagina(processo, p) if p >= 0 else []
        return cache[p]

    def _na_pagina(p: int) -> dict | None:
        return next((d for d in pg(p) if _num(d) == numero), None)

    if dica_pagina is not None:
        for p in (dica_pagina, dica_pagina + 1, dica_pagina - 1):
            d = _na_pagina(p)
            if d:
                return d, p
    info = ind.setdefault("processos_info", {}).setdefault(processo, {})
    conhecida = info.get("ultima_pagina")
    if isinstance(conhecida, int) and pg(conhecida) and not pg(conhecida + 1):
        lo = conhecida
    else:
        if not pg(0):
            return None
        base = conhecida if isinstance(conhecida, int) and pg(conhecida) else 0
        passo = 1
        while pg(base + passo) and passo < 4096:  # acha uma página vazia (fim da lista)
            passo *= 2
        lo, ultima = base, base + passo
        while lo + 1 < ultima:  # última página não vazia
            m = (lo + ultima) // 2
            if pg(m):
                lo = m
            else:
                ultima = m
    info["ultima_pagina"] = lo
    if numero > _num(pg(lo)[-1]):
        return None
    a, b = 0, lo
    while a < b:  # primeira página cujo último nº >= procurado
        m = (a + b) // 2
        if _num(pg(m)[-1]) < numero:
            a = m + 1
        else:
            b = m
    for p in (a, a - 1, a + 1):  # a vizinha cobre documento fora de ordem
        if 0 <= p <= lo:
            d = _na_pagina(p)
            if d:
                return d, p
    return None


def localizar_oficio(numero: str, processo: str = "") -> dict:
    """{ok, documento_id, numero, processo, tipo, assinado, cancelado, unidade, nivel_acesso}."""
    alvo = re.sub(r"\D", "", (numero or "").split("/")[0])
    if not alvo:
        return {"ok": False, "erro": "Informe o número SEI do ofício (ex.: 9433745)."}
    processo = (processo or "").strip()
    ind = _indice()
    ind.setdefault("documentos", {})
    ind.setdefault("processos", [])
    candidatos: list[str] = []
    dica = ind["documentos"].get(alvo) or {}
    if processo:
        candidatos.append(processo)
    else:
        if dica.get("processo"):
            candidatos.append(dica["processo"])
        for pasta in PAJS_DIR.iterdir() if PAJS_DIR.exists() else []:
            for o in _ler_json(pasta / ARQ_CRIADOS, []) if (pasta / ARQ_CRIADOS).exists() else []:
                if str(o.get("numero_sei") or "").split("/")[0] == alvo and o.get("processo"):
                    candidatos.append(o["processo"])
        candidatos += [*ind["processos"], *PROCESSOS_OFICIOS_CONHECIDOS]
    vistos: list[str] = []
    for proc in candidatos:
        if proc in vistos:
            continue
        vistos.append(proc)
        achado = _procurar_no_processo(proc, int(alvo), ind,
                                       dica.get("pagina") if dica.get("processo") == proc else None)
        if achado:
            return _achado(achado[0], proc, ind, achado[1])
    if not processo:  # último recurso: busca no teor (o nº aparece no texto do ofício)
        with contextlib.suppress(Exception):
            r = sei.chamar("pesquisar_processos", {"texto": alvo, "limit": 5}, timeout=120)
            for p in ((r or {}).get("processos") or (r or {}).get("resultados") or [])[:3]:
                proc = p.get("numero") or p.get("protocoloFormatado") or ""
                if proc and proc not in vistos:
                    vistos.append(proc)
                    achado = _procurar_no_processo(proc, int(alvo), ind)
                    if achado:
                        return _achado(achado[0], proc, ind, achado[1])
    _gravar_indice(ind)  # guarda ao menos as últimas páginas descobertas
    return {"ok": False, "erro": f"Não encontrei o documento SEI nº {alvo} nos processos de ofícios conhecidos "
                                  f"({', '.join(vistos[:4])}). Informe o processo SEI em que ele está."}


def _achado(d: dict, processo: str, ind: dict, pagina: int | None = None) -> dict:
    ind["documentos"][str(d.get("numero"))] = {"documento_id": str(d.get("id")), "processo": processo,
                                               "pagina": pagina}
    if processo not in ind["processos"] and processo not in PROCESSOS_OFICIOS_CONHECIDOS:
        ind["processos"].insert(0, processo)
        ind["processos"] = ind["processos"][:10]
    _gravar_indice(ind)
    return {"ok": True, "documento_id": str(d.get("id")), "numero": str(d.get("numero")), "processo": processo,
            "tipo": d.get("tipo") or "", "assinado": bool(d.get("assinado")), "cancelado": bool(d.get("cancelado")),
            "unidade": d.get("siglaUnidade") or "", "nivel_acesso": d.get("nivelAcesso") or ""}


def ler_oficio_para_rascunho(documento_id: str) -> dict:
    """Seções do Ofício no SEI → campos do rascunho (destinatário, assunto,
    referência, vocativo, corpo, fechamento) + nº, data e nível de acesso."""
    sec = sei.chamar("listar_secoes", {"id_documento": str(documento_id), "incluir_conteudo": True,
                                       "max_chars_conteudo": 50000}, timeout=120)
    por = {str(s.get("idSecaoModelo")): html.unescape(html.unescape(s.get("conteudo", "")))
           for s in sec.get("secoes", []) if str(s.get("idSecaoModelo")) not in ("304", "305")}
    principal = next((html.unescape(html.unescape(s.get("conteudo", ""))) for s in sec.get("secoes", [])
                      if s.get("PrincipalSecaoDocumento") == "S"), por.get("186", ""))
    linhas_dest = [ln.strip() for ln in _texto_html(por.get("184", "")).splitlines() if ln.strip()]
    assunto = referencia = ""
    for ln in _texto_html(por.get("1049", "")).splitlines():
        if re.match(r"\s*assunto\s*:", ln, re.I):
            assunto = _sem_rotulo(ln, "Assunto")
        elif re.match(r"\s*refer[êe]ncia\s*:", ln, re.I):
            referencia = _sem_rotulo(ln, "Referência")
    vocativo = _texto_html(por.get("185", "")).strip()
    # corpo: tira parágrafos vazios (&nbsp;) das pontas e as cores
    corpo = _sem_cores(principal)
    paras = [m.group(0) for m in _RE_P.finditer(corpo)]
    while paras and not _texto_html(paras[0]).strip():
        paras.pop(0)
    while paras and not _texto_html(paras[-1]).strip():
        paras.pop()
    r = {"corpo_html": "\n".join(paras) or corpo}
    if not vocativo:
        # vocativo digitado no início do corpo (ofícios feitos à mão no SEI)
        primeiros = list(_RE_P.finditer(r["corpo_html"]))
        if primeiros and re.match(r"\s*(prezad|senhor|excelent|ilustr)", _texto_html(primeiros[0].group(1)), re.I):
            vocativo = _texto_html(primeiros[0].group(1))
            r["corpo_html"] = r["corpo_html"][primeiros[0].end():].strip()
    r["vocativo"] = vocativo
    r = _separar_fechamento(r)
    data = ""
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", _texto_html(por.get("183", "")), re.I)
    if m and m.group(2).lower() in _MESES:
        data = f"{int(m.group(1)):02d}/{_MESES[m.group(2).lower()]:02d}/{m.group(3)}"
    nivel = ""
    with contextlib.suppress(Exception):
        nivel = _nivel_acesso_atual(documento_id)
    return {"destinatario": "\n".join(linhas_dest), "assunto": assunto, "referencia": referencia,
            "vocativo": r.get("vocativo") or VOCATIVO_PADRAO, "corpo_html": r.get("corpo_html", ""),
            "fechamento": r.get("fechamento") or FECHAMENTO_PADRAO,
            "numero": _numero_oficio(_texto_html(por.get("182", ""))) or "", "data": data,
            "versao": str(sec.get("ultima_versao") or ""),
            "restrito": nivel.lower().startswith("restri"), "nivel_acesso": nivel,
            "aviso_leitura": r.get("aviso_leitura", "")}


_RE_FECHO = re.compile(r"\s*(atenciosamente|respeitosamente|cordialmente)\b", re.I)


def _separar_fechamento(r: dict) -> dict:
    """Tira do fim do corpo o fecho ("Atenciosamente,") e, se houver, a
    assinatura DIGITADA depois dele (nome/cargo) — o modelo do SEI já põe o
    fechamento e a assinatura eletrônica. Procura nos últimos 4 parágrafos."""
    r = dict(r)
    corpo = r.get("corpo_html") or ""
    paras = [p for p in _RE_P.finditer(corpo) if _texto_html(p.group(1)).strip()]
    for p in reversed(paras[-4:]):
        if _RE_FECHO.match(_texto_html(p.group(1))):
            depois = _texto_html(corpo[p.end():]).strip()
            r["fechamento"] = _RE_FECHO.match(_texto_html(p.group(1))).group(1).capitalize() + ","
            r["corpo_html"] = corpo[:p.start()].strip()
            if depois:
                r["aviso_leitura"] = (f"Tirei do fim do corpo o fecho e a assinatura digitada (\"{depois[:80]}\") — "
                                      "o SEI já coloca o fechamento e a assinatura eletrônica de quem assinar.")
            break
    return r


def abrir_oficio(paj_norm: str, numero: str, processo: str = "") -> dict:
    """"Abrir ofício nº": minuta NÃO assinada da COFE CTB → vira o rascunho em
    edição (salvar grava no MESMO documento). Assinado → devolve o resumo, para
    a tela oferecer "Reiterar" / "Usar como base"."""
    _pasta(paj_norm)
    try:
        loc = localizar_oficio(numero, processo)
        if not loc.get("ok"):
            return loc
        if not str(loc["tipo"]).lower().startswith("of"):
            return {"ok": False, "erro": f"O documento SEI nº {loc['numero']} é \"{loc['tipo']}\", não um Ofício."}
        if loc["cancelado"]:
            return {"ok": False, "erro": f"O Ofício nº {loc['numero']} está cancelado no SEI."}
        doc = ler_oficio_para_rascunho(loc["documento_id"])
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "Conecte o painel ao SEI para abrir o ofício."}
    except sei.SeiErro as e:
        return {"ok": False, "erro": str(e)}
    resumo = {"documento_id": loc["documento_id"], "numero_sei": doc["numero"] or loc["numero"],
              "processo": loc["processo"], "destinatario": (doc["destinatario"].splitlines() or [""])[0],
              "assunto": doc["assunto"], "data": doc["data"], "unidade": loc["unidade"]}
    if loc["assinado"]:
        return {"ok": True, "assinado": True, "oficio": resumo}
    if loc["unidade"] and loc["unidade"] != "COFE CTB":
        return {"ok": False, "erro": f"O Ofício nº {loc['numero']} foi gerado pela unidade {loc['unidade']} — "
                                      "só a unidade geradora pode editá-lo."}
    rascunho = _limpar_rascunho({
        **{k: doc[k] for k in ("destinatario", "assunto", "referencia", "vocativo", "corpo_html", "fechamento")},
        "restrito": doc["restrito"],
        "edicao": {"documento_id": loc["documento_id"], "numero_sei": resumo["numero_sei"],
                   "processo": loc["processo"], "versao": doc["versao"]},
        "observacoes": doc.get("aviso_leitura", ""),
    })
    _gravar_json(_pasta(paj_norm) / ARQ_RASCUNHO, rascunho)
    with _lock:
        _status[paj_norm] = {"status": "done", "erro": ""}
    return {"ok": True, "assinado": False, "oficio": resumo, "rascunho": rascunho}


def _salvar_edicao(paj_norm: str, r: dict) -> dict:
    """Grava o rascunho em edição no MESMO documento (alterar_minuta)."""
    ed = r["edicao"]
    doc_id = str(ed["documento_id"])
    try:
        # Conflito: alguém (ex.: a assessoria) mexeu na minuta depois que ela foi aberta aqui?
        atual = sei.chamar("listar_secoes", {"id_documento": doc_id}, timeout=120)
        if ed.get("versao") and str(atual.get("ultima_versao") or "") not in ("", str(ed["versao"])):
            return {"ok": False, "erro": f"A minuta foi alterada no SEI por outra pessoa depois que você a abriu "
                                          f"(versão {ed['versao']} → {atual.get('ultima_versao')}). Abra o ofício "
                                          "de novo para pegar a versão atual."}
        args = {"documento_id": doc_id, "destinatario": _destinatario_html(r["destinatario"]),
                "assunto": r["assunto"], "vocativo": r["vocativo"] or VOCATIVO_PADRAO,
                "html": _normalizar_corpo(r["corpo_html"]), "fechamento": r["fechamento"] or FECHAMENTO_PADRAO}
        if r["referencia"]:
            args["referencia"] = r["referencia"]
        sei.chamar("auth_select_unit", {"unidade_id": UNIDADE_COFE_CTB})
        resp = sei.chamar("alterar_minuta", args, timeout=120)
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "O painel não está conectado ao SEI — clique em \"Conectar ao SEI\"."}
    except sei.SeiErro as e:
        return {"ok": False, "erro": str(e)}
    avisos = [w.get("mensagem", "") for w in ((resp or {}).get("warnings") or [])
              if isinstance(w, dict) and w.get("severidade") in ("warn", "error")] if isinstance(resp, dict) else []
    nova_versao = str((resp or {}).get("versao_nova") or "") if isinstance(resp, dict) else ""
    with contextlib.suppress(Exception):
        relido = ler_oficio_para_rascunho(doc_id)
        if r["assunto"] not in relido["assunto"]:
            avisos.append("O assunto não apareceu como esperado no documento gravado — confira no SEI.")
        if _texto_html(relido["corpo_html"]).split() != _texto_html(_normalizar_corpo(r["corpo_html"])).split():
            avisos.append("O corpo relido do SEI difere do texto enviado — confira no SEI.")
    if r["restrito"]:
        aviso = _aplicar_restrito(doc_id, f"PAJ {_contexto_paj(paj_norm)['paj']}")
        if aviso:
            avisos.append(aviso)
    registro = {
        "documento_id": doc_id, "numero_sei": ed.get("numero_sei") or "", "processo": ed.get("processo") or "",
        "destinatario": r["destinatario"].splitlines()[0] if r["destinatario"] else "", "assunto": r["assunto"],
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "link": sei.link_documento(_id_procedimento(ed.get("processo") or PROCESSO_OFICIOS), doc_id),
        "conferido": not avisos, "avisos": [a for a in avisos if a], "origem": "editado",
        "versao": nova_versao, "nivel_acesso": "restrito" if r["restrito"] and not any("restrito" in a for a in avisos) else "",
    }
    pasta = _pasta(paj_norm)
    criados = [o for o in _ler_json(pasta / ARQ_CRIADOS, []) if str(o.get("documento_id")) != doc_id]
    antigo = next((o for o in _ler_json(pasta / ARQ_CRIADOS, []) if str(o.get("documento_id")) == doc_id), {})
    criados.append({**antigo, **registro, "criado_em": antigo.get("criado_em") or registro["criado_em"],
                    "editado_em": registro["criado_em"]})
    _gravar_json(pasta / ARQ_CRIADOS, criados)
    with contextlib.suppress(FileNotFoundError):
        (pasta / ARQ_RASCUNHO).unlink()
    with _lock:
        _status.pop(paj_norm, None)
    return {"ok": True, "oficio": criados[-1], "editado": True}


def criar_no_sei(paj_norm: str, rascunho: dict) -> dict:
    r = _limpar_rascunho(rascunho)
    ctx = _contexto_paj(paj_norm)
    faltando = [c for c, v in (("destinatário", r["destinatario"]), ("assunto", r["assunto"]),
                               ("corpo", _texto_html(r["corpo_html"]))) if not v]
    if faltando:
        return {"ok": False, "erro": "Preencha antes de criar: " + ", ".join(faltando) + "."}
    pendentes = re.findall(r"\[[A-ZÁÉÍÓÚÂÊÔÃÕÇ /-]*CONFIRMAR[^\]]*\]",
                           r["destinatario"] + " " + r["assunto"] + " " + _texto_html(r["corpo_html"]))
    if pendentes:
        return {"ok": False, "erro": "Ainda há marcadores a completar: " + ", ".join(sorted(set(pendentes)))}
    _gravar_json(_pasta(paj_norm) / ARQ_RASCUNHO, r)
    if r["edicao"]:
        return _salvar_edicao(paj_norm, r)

    # Campos ESTRUTURADOS do criar_minuta (manual do MCP-SEI): cada um vai para a
    # sua seção do modelo de Ofício — assunto só com o texto (o modelo já põe o
    # rótulo), referência no campo próprio, vocativo e fechamento fora do corpo.
    args = {
        "numero_processo": PROCESSO_OFICIOS,
        "tipo_documento_id": TIPO_OFICIO,
        "titulo": f"PAJ {ctx['paj']}",
        "destinatario": _destinatario_html(r["destinatario"]),
        "assunto": r["assunto"],
        "vocativo": r["vocativo"] or VOCATIVO_PADRAO,
        "html": _normalizar_corpo(r["corpo_html"]),
        "fechamento": r["fechamento"] or FECHAMENTO_PADRAO,
    }
    if r["referencia"]:
        args["referencia"] = r["referencia"]
    try:
        sei.chamar("auth_select_unit", {"unidade_id": UNIDADE_COFE_CTB})
        resp = sei.chamar("criar_minuta", args, timeout=120)
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "O painel não está conectado ao SEI — clique em \"Conectar ao SEI\"."}
    except sei.SeiErro as e:
        return {"ok": False, "erro": str(e)}

    if not isinstance(resp, dict):
        return {"ok": False, "erro": f"Resposta inesperada do SEI: {str(resp)[:400]}"}
    doc_id = str(resp.get("documento_id") or resp.get("id_documento") or resp.get("idDocumento") or "")
    if not doc_id:
        return {"ok": False, "erro": "O SEI não devolveu o id do documento — confira no processo antes de "
                                      f"tentar de novo. Resposta: {json.dumps(resp, ensure_ascii=False)[:400]}"}

    # Leitura independente (regra do próprio MCP-SEI: nunca confiar só no
    # payload de sucesso de uma gravação).
    avisos = [w.get("mensagem", "") for w in (resp.get("warnings") or [])
              if isinstance(w, dict) and w.get("severidade") in ("warn", "error")]
    numero = ""
    conferido = False
    with contextlib.suppress(Exception):
        sec = sei.chamar("listar_secoes", {"id_documento": doc_id, "incluir_conteudo": True,
                                           "max_chars_conteudo": 6000})
        secoes = sec.get("secoes", [])
        # o listar_secoes devolve o HTML da seção escapado DUAS vezes (&amp;lt;…)
        tudo = html.unescape(html.unescape(" ".join(s.get("conteudo", "") for s in secoes)))
        numero = _numero_oficio(tudo)
        conferido = PROCESSO_OFICIOS in tudo and ctx["paj"] in _texto_html(tudo)
        texto = _texto_html(tudo)
        if r["assunto"] not in texto:
            avisos.append("O assunto não apareceu no documento gravado — confira no SEI.")
        if len(re.findall(r"Assunto\s*:", texto, re.I)) > 1:
            avisos.append("O rótulo \"Assunto:\" saiu repetido — confira no SEI.")
        principal = next((s for s in secoes if s.get("PrincipalSecaoDocumento") == "S"), None)
        voc = (r["vocativo"] or VOCATIVO_PADRAO).strip()
        if principal and voc[:12].lower() in _texto_html(html.unescape(principal.get("conteudo", ""))).lower():
            avisos.append("O vocativo saiu dentro do texto principal — confira no SEI.")
    if not conferido:
        avisos.append("Não consegui confirmar pela releitura que o ofício ficou no processo certo — confira no SEI.")
    nivel = "publico"
    if r["restrito"]:
        aviso = _aplicar_restrito(doc_id, args["titulo"])
        if aviso:
            avisos.append(aviso)
        else:
            nivel = "restrito"

    registro = {
        "documento_id": doc_id,
        "numero_sei": numero or str(resp.get("numero") or resp.get("numero_provisorio") or ""),
        "processo": PROCESSO_OFICIOS,
        "destinatario": r["destinatario"].splitlines()[0] if r["destinatario"] else "",
        "assunto": r["assunto"],
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "link": sei.link_documento(_id_procedimento(), doc_id),
        "conferido": conferido,
        "avisos": [a for a in avisos if a],
        "nivel_acesso": nivel,
        "descricao_sei": args["titulo"],
        "reiteracao_de": r["reiteracao_de"],
    }
    pasta = _pasta(paj_norm)
    criados = _ler_json(pasta / ARQ_CRIADOS, [])
    criados.append(registro)
    _gravar_json(pasta / ARQ_CRIADOS, criados)
    with contextlib.suppress(FileNotFoundError):
        (pasta / ARQ_RASCUNHO).unlink()
    with _lock:
        _status.pop(paj_norm, None)
    return {"ok": True, "oficio": registro}


# ---------------------------------------------------------------------------
# 3. Depois de criado: assinatura e leitura
# ---------------------------------------------------------------------------

# Ofício excluído/cancelado no SEI (22/10/2026, pedido do Defensor): o painel
# guardava o ofício como "Assinado" com os botões ativos e o PDF pronto para
# juntada, mesmo depois de excluído no SEI. Conferido pela LISTA de documentos
# do processo de ofícios (documento excluído some dela; o "acesso não
# autorizado" da leitura também aparece por falta de permissão, então não
# serve de prova). Só nos ofícios dos últimos 90 dias.
_DIAS_CONFERIR_EXISTENCIA = 90


def _conferir_existencia(o: dict, ind: dict) -> bool:
    """Atualiza `o` com a situação do documento na lista do processo: excluído
    (some da lista), cancelado, ou assinatura removida. True se mudou algo.
    Falha de consulta nunca marca exclusão."""
    if o.get("excluido_no_sei") or o.get("cancelado_no_sei"):
        return False
    m = re.fullmatch(r"(\d{5,})/\d{4}", str(o.get("numero_sei") or ""))
    if not m:
        return False
    with contextlib.suppress(Exception):
        criado = datetime.fromisoformat(o.get("criado_em") or "")
        if (datetime.now() - criado).days > _DIAS_CONFERIR_EXISTENCIA:
            return False
    numero = int(m.group(1))
    processo = o.get("processo") or PROCESSO_OFICIOS
    try:
        achado = _procurar_no_processo(processo, numero, ind)
    except Exception:  # noqa: BLE001 — sem resposta do SEI: não conclui nada
        return False
    agora = datetime.now().isoformat(timespec="seconds")
    if achado is None:
        # Ausente só vale como "excluído" se a lista já passou desse número
        # (senão pode ser só documento novo ainda fora da última página lida).
        ultima = (ind.get("processos_info", {}).get(processo) or {}).get("ultima_pagina")
        try:
            pagina = _pagina(processo, ultima) if isinstance(ultima, int) else []
        except Exception:  # noqa: BLE001
            return False
        if not pagina or _num(pagina[-1]) < numero:
            return False
        o["excluido_no_sei"] = True
        o["excluido_visto_em"] = agora
        return True
    d = achado[0]
    if str(d.get("id") or "") and str(d.get("id")) != str(o.get("documento_id")):
        return False  # mesmo número, outro documento: não mexe
    if d.get("cancelado"):
        o["cancelado_no_sei"] = True
        o["excluido_visto_em"] = agora
        return True
    if o.get("assinado") and d.get("assinado") is False:
        o["assinado"] = False
        o.pop("assinantes", None)
        o["assinatura_removida_em"] = agora
        return True
    return False


def _retirar_juntada_preparada(paj_norm: str, o: dict) -> bool:
    """Tira do rascunho do "Ações no SISDPU" o PDF do ofício excluído/cancelado
    (posto lá por "Anexar ao PAJ"). Se o rascunho ficar sem anexos e a
    descrição for a do próprio ofício, o rascunho inteiro sai."""
    from services import acoes_sisdpu_service

    if o.get("juntada") != "preparada" or not o.get("juntada_arquivo"):
        return False
    ref = f"{SUBPASTA_OFICIOS}/{o['juntada_arquivo']}"
    atual = acoes_sisdpu_service.obter_rascunho(paj_norm) or {}
    anexos = atual.get("anexos") or []
    restantes = [a for a in anexos if a.get("ref") != ref]
    if len(restantes) != len(anexos):
        numero = str(o.get("numero_sei") or "")
        so_do_oficio = not restantes and (not numero or numero in (atual.get("descricao_mov_html") or ""))
        if so_do_oficio:
            acoes_sisdpu_service.excluir_rascunho(paj_norm)
        else:
            novo = {k: v for k, v in atual.items() if k not in ("paj_norm", "salvo_em")}
            novo["anexos"] = restantes
            acoes_sisdpu_service.salvar_rascunho(paj_norm, novo)
    o["juntada"] = ""
    o["juntada_retirada_em"] = datetime.now().isoformat(timespec="seconds")
    return True


def atualizar_situacao(paj_norm: str) -> dict:
    """Confere no SEI os ofícios deste PAJ ainda não vistos como assinados:
    assinaturas (listar_assinaturas) e o nº definitivo do ofício. Assinado, o
    item deixa de mostrar "conferir"/"Abrir para assinar" (os avisos eram da
    minuta — quem assina já revisou). Só leitura no SEI."""
    pasta = _pasta(paj_norm)
    criados = _ler_json(pasta / ARQ_CRIADOS, [])
    mudou = False
    ind = _indice()
    for o in criados:
        doc = o.get("documento_id")
        # Excluído/cancelado no SEI ou assinatura removida (22/10/2026).
        try:
            if doc and _conferir_existencia(o, ind):
                mudou = True
        except sei.SeiLoginNecessario:
            return {"ok": False, "login": True, "criados": criados}
        if o.get("excluido_no_sei") or o.get("cancelado_no_sei"):
            with contextlib.suppress(Exception):
                if _retirar_juntada_preparada(paj_norm, o):
                    mudou = True
            continue
        if doc and not o.get("nivel_acesso"):
            # ofícios anteriores a 21/10/2026 não guardavam o nível — lê uma vez
            with contextlib.suppress(Exception):
                n = _nivel_acesso_atual(doc).lower()
                if n:
                    o["nivel_acesso"] = "restrito" if n.startswith("restri") else n
                    mudou = True
        if not doc or o.get("assinado"):
            continue
        try:
            a = sei.chamar("listar_assinaturas", {"documento_id": doc})
        except sei.SeiLoginNecessario:
            return {"ok": False, "login": True, "criados": criados}
        except sei.SeiErro:
            continue
        assinaturas = (a or {}).get("assinaturas") or []
        if assinaturas:
            o["assinado"] = True
            o["assinantes"] = [x.get("nome", "") for x in assinaturas if x.get("nome")]
            o["assinatura_vista_em"] = datetime.now().isoformat(timespec="seconds")
            mudou = True
        if not re.fullmatch(r"\d{5,}/\d{4}", o.get("numero_sei") or ""):
            with contextlib.suppress(Exception):
                sec = sei.chamar("listar_secoes", {"id_documento": doc, "incluir_conteudo": True,
                                                   "max_chars_conteudo": 600})
                n = _numero_oficio(_texto_html(html.unescape(html.unescape(" ".join(
                    x.get("conteudo", "") for x in sec.get("secoes", []) if str(x.get("idSecaoModelo")) != "304")))))
                if n:
                    o["numero_sei"] = n
                    mudou = True
    if mudou:
        _gravar_json(pasta / ARQ_CRIADOS, criados)
    return {"ok": True, "criados": criados}


# Tira só o que EXECUTA (scripts, objetos, handlers on*) — estilos e imagens
# (timbre, códigos de barras, QR da assinatura, todos embutidos em data:) ficam,
# para o ofício sair igual ao SEI. O modal ainda o mostra num iframe sandbox.
_SANITIZAR = re.compile(r"<(script|iframe|object|embed)\b.*?</\1>|<(script|iframe|object|embed)\b[^>]*>|"
                        r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I | re.S)


def ler_oficio(paj_norm: str, documento_id: str) -> dict:
    """Conteúdo do ofício (assinado ou não) para leitura no painel — só de
    documento que este PAJ criou (oficios_sei.json)."""
    criados = _ler_json(_pasta(paj_norm) / ARQ_CRIADOS, [])
    if not any(str(o.get("documento_id")) == str(documento_id) for o in criados):
        return {"ok": False, "erro": "Ofício não pertence a este PAJ."}
    try:
        # "html" = documento completo, como o SEI exibe (o "stripped" remove imagens e estilos)
        d = sei.chamar("ler_documento", {"documento_id": str(documento_id), "formato": "html"}, timeout=120)
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "Conecte o painel ao SEI para ler o ofício."}
    except sei.SeiErro as e:
        return {"ok": False, "erro": str(e)}
    d = d if isinstance(d, dict) else {}
    corpo = d.get("conteudo_html") or d.get("html") or d.get("conteudo") or ""
    if not corpo and d.get("texto"):
        corpo = "".join(f"<p>{html.escape(ln)}</p>" for ln in str(d["texto"]).splitlines() if ln.strip())
    assinaturas = d.get("assinaturas") or []
    return {"ok": True, "html": _SANITIZAR.sub("", str(corpo)),
            "assinaturas": [{"nome": a.get("nome", ""), "cargo": a.get("cargo", ""),
                             "data": a.get("data") or a.get("data_hora") or a.get("timestamp") or ""}
                            for a in assinaturas if isinstance(a, dict)]}


# ---------------------------------------------------------------------------
# 4. "Anexar ao PAJ": ofício assinado → movimentação "Juntada de documento"
#    no SISDPU, com o PDF do ofício (pedido do Defensor, 16/10/2026)
# ---------------------------------------------------------------------------

SUBPASTA_OFICIOS = "Ofícios SEI"
# Tipo de arquivo do PDF do ofício na tabela de anexos da movimentação (catálogo
# do SISDPU — mesmo valor do seletor em static/js/acoes_sisdpu.js).
TIPO_ARQUIVO_OFICIO = "Ofício"
_MESES = {m: i for i, m in enumerate(["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
                                       "agosto", "setembro", "outubro", "novembro", "dezembro"], start=1)}
_RE_ENDERECO = re.compile(r"^(rua|r\.|av\.?|avenida|travessa|rodovia|pra[çc]a|alameda|estrada|cep\b|e-?mail|"
                          r"telefone|fone|endere[çc]o|caixa postal)|\d{2}\.?\d{3}-\d{3}", re.I)
_tarefas_juntada: set = set()


def _dados_do_oficio(documento_id: str, registro: dict) -> dict:
    """Número, data, destinatário e assunto lidos do PRÓPRIO documento no SEI
    (seções do modelo de Ofício) — o registro local é só reserva."""
    sec = sei.chamar("listar_secoes", {"id_documento": documento_id, "incluir_conteudo": True,
                                       "max_chars_conteudo": 4000})
    por_modelo = {str(s.get("idSecaoModelo")): _texto_html(html.unescape(html.unescape(s.get("conteudo", ""))))
                  for s in sec.get("secoes", []) if str(s.get("idSecaoModelo")) != "304"}
    titulo = re.sub(r"\s+", " ", por_modelo.get("182", "")).strip()
    if not re.search(r"\d{5,}/\d{4}", titulo):
        titulo = f"Ofício - nº {registro.get('numero_sei') or ''} - COFE CTB"
    data = ""
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", por_modelo.get("183", ""), re.I)
    if m and m.group(2).lower() in _MESES:
        data = f"{int(m.group(1)):02d}/{_MESES[m.group(2).lower()]:02d}/{m.group(3)}"
    elif registro.get("criado_em"):
        data = datetime.fromisoformat(registro["criado_em"]).strftime("%d/%m/%Y")
    linhas = [ln.strip() for ln in por_modelo.get("184", "").splitlines() if ln.strip()]
    nome = []
    for ln in linhas:  # destinatário = linhas antes do endereço/CEP/e-mail
        if _RE_ENDERECO.search(ln):
            break
        nome.append(ln)
    destinatario = " - ".join(nome) or registro.get("destinatario", "")
    assunto = re.sub(r"^\s*Assunto\s*:\s*", "", por_modelo.get("1049", "").splitlines()[0]
                     if por_modelo.get("1049") else "", flags=re.I).strip() or registro.get("assunto", "")
    return {"titulo": titulo, "data": data, "destinatario": destinatario, "assunto": assunto}


def _descricao_juntada(d: dict) -> str:
    linhas = [d["titulo"], f"Data: {d['data']}", f"Destinatário: {d['destinatario']}",
              f"Assunto: {d['assunto']}"]
    return "".join(f"<p>{html.escape(ln)}</p>" for ln in linhas)


def _descricao_anexo(d: dict) -> str:
    """Descrição do PDF anexado: "Ofício - nº 9416409/2026 - DIRETORIA
    EXECUTIVA - COMPLEXO DE SAÚDE PEQUENO COTOLENGO" (sem a unidade e sem o
    "À/Ao" do destinatário — pedido do Defensor, 16/10/2026)."""
    numero = re.search(r"\d{5,}/\d{4}", d["titulo"])
    destino = re.sub(r"^(à|ao|aos|às|a|o)\s+", "", d["destinatario"].strip(), flags=re.I)
    partes = [f"Ofício - nº {numero.group(0)}" if numero else d["titulo"], destino]
    return " - ".join(p for p in partes if p)


def _salvar_registro(paj_norm: str, documento_id: str, **campos) -> None:
    pasta = _pasta(paj_norm)
    criados = _ler_json(pasta / ARQ_CRIADOS, [])
    for o in criados:
        if str(o.get("documento_id")) == str(documento_id):
            o.update(campos)
    _gravar_json(pasta / ARQ_CRIADOS, criados)


async def _juntar_oficio(paj_norm: str, registro: dict) -> None:
    import asyncio
    import base64

    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service
    from services.dossie_sd_service import _id_processo_local, _ler_meta

    doc = str(registro["documento_id"])
    try:
        dados = await asyncio.to_thread(_dados_do_oficio, doc, registro)
        pdf = await asyncio.to_thread(sei.chamar, "baixar_documento_pdf", {"documento_id": doc}, 180)
        b64 = (pdf or {}).get("conteudo_base64") if isinstance(pdf, dict) else None
        if not b64:
            raise RuntimeError("o SEI não devolveu o PDF do ofício "
                               f"({(pdf or {}).get('modo') if isinstance(pdf, dict) else str(pdf)[:120]}).")
        conteudo = base64.b64decode(b64)
        if conteudo[:5] != b"%PDF-":
            raise RuntimeError("o arquivo devolvido pelo SEI não é um PDF.")
        numero = re.search(r"(\d{5,})/(\d{4})", dados["titulo"])
        nome_pdf = (f"Oficio {numero.group(1)}-{numero.group(2)} - COFE CTB.pdf" if numero
                    else f"Oficio SEI {doc}.pdf")
        pasta = _pasta(paj_norm) / SUBPASTA_OFICIOS
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / nome_pdf).write_bytes(conteudo)

        meta = _ler_meta(paj_norm)
        paj_label = (meta.get("paj") or "").strip()
        id_proc, _ev, erro = await acoes_sisdpu_service._resolver_id_processo(_id_processo_local(meta), paj_label)
        if not id_proc:
            raise RuntimeError(erro or "id do PAJ no SISDPU não encontrado.")
        anexos = [{"nome": nome_pdf, "caminho": str(pasta / nome_pdf), "tipo": "pdf",
                   "descricao": _descricao_anexo(dados)}]
        mv, exc, _ev = await acoes_sisdpu_service.com_auto_reconexao(
            lambda: movtram.movimentar_paj([{"id": id_proc, "numero": paj_label}],
                                           _descricao_juntada(dados), sat_service.FASE_MOV,
                                           anexos, sat_service.TIPO_ARQUIVO, False, descricao_html=True))
        if exc is not None:
            raise RuntimeError(f"{type(exc).__name__}: {exc}")
        if not (mv or {}).get("ok"):
            detalhe = (mv or {}).get("detalhe") or "falha na movimentação"
            if "não foi confirmada" in detalhe:
                # O Defensor recusou a ficha: só cancela — nada foi gravado, o
                # botão volta ao normal, sem mensagem de erro (16/10/2026).
                _salvar_registro(paj_norm, doc, juntada="", erro_juntada="")
                return
            raise RuntimeError(detalhe)
        _salvar_registro(paj_norm, doc, juntada="ok", juntada_mov_seq=mv.get("mov_seq"),
                         juntada_em=datetime.now().isoformat(timespec="seconds"), juntada_arquivo=nome_pdf,
                         erro_juntada="")
        with contextlib.suppress(Exception):
            from services import ao_vivo_service
            await ao_vivo_service.atualizar_paj(paj_norm, forcar=True)
    except Exception as e:  # noqa: BLE001
        _salvar_registro(paj_norm, doc, juntada="erro", erro_juntada=f"{e}"[:400])


def preparar_juntada(paj_norm: str, documento_id: str) -> dict:
    """"Anexar ao PAJ" (versão 21/10/2026): em vez de movimentar sozinho só com
    o ofício, baixa o PDF do ofício assinado do SEI para "Ofícios SEI/" do PAJ e
    deixa o console "Ações no SISDPU" PRÉ-PREENCHIDO (rascunho em disco — mesmo
    mecanismo do "Peticionar" do e-proc): fase "Juntada de documento", descrição
    (nº, data, destinatário, assunto) e o PDF do ofício como 1º anexo. O
    Defensor acrescenta os demais anexos (do PAJ, Peças IA ou externos) e
    executa ali, com a ficha de confirmação de sempre. Se já houver rascunho
    no console, o ofício é ACRESCENTADO a ele (nada do que estava é perdido).
    Síncrono (chamar via threadpool)."""
    import base64

    from services import acoes_sisdpu_service, sat_service

    pasta_paj = _pasta(paj_norm)
    criados = _ler_json(pasta_paj / ARQ_CRIADOS, [])
    registro = next((o for o in criados if str(o.get("documento_id")) == str(documento_id)), None)
    if not registro:
        return {"ok": False, "erro": "Ofício não pertence a este PAJ."}
    if not registro.get("assinado"):
        return {"ok": False, "erro": "Só o ofício assinado pode ser anexado ao PAJ."}
    doc = str(registro["documento_id"])
    try:
        dados = _dados_do_oficio(doc, registro)
        pdf = sei.chamar("baixar_documento_pdf", {"documento_id": doc}, 180)
    except sei.SeiLoginNecessario:
        return {"ok": False, "login": True, "erro": "Conecte o painel ao SEI para baixar o ofício."}
    except sei.SeiErro as e:
        return {"ok": False, "erro": str(e)}
    b64 = (pdf or {}).get("conteudo_base64") if isinstance(pdf, dict) else None
    if not b64:
        return {"ok": False, "erro": "O SEI não devolveu o PDF do ofício."}
    conteudo = base64.b64decode(b64)
    if conteudo[:5] != b"%PDF-":
        return {"ok": False, "erro": "O arquivo devolvido pelo SEI não é um PDF."}
    numero = re.search(r"(\d{5,})/(\d{4})", dados["titulo"])
    nome_pdf = (f"Oficio {numero.group(1)}-{numero.group(2)} - COFE CTB.pdf" if numero
                else f"Oficio SEI {doc}.pdf")
    pasta = pasta_paj / SUBPASTA_OFICIOS
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / nome_pdf).write_bytes(conteudo)

    ref = f"{SUBPASTA_OFICIOS}/{nome_pdf}"
    anexo = {"origem": "paj", "ref": ref, "nome": nome_pdf, "tamanho": len(conteudo), "tipo": "pdf",
             "descricao": _descricao_anexo(dados), "tipoArquivo": TIPO_ARQUIVO_OFICIO}
    atual = acoes_sisdpu_service.obter_rascunho(paj_norm) or {}
    tinha_rascunho = bool(atual.get("fase") or atual.get("anexos") or atual.get("descricao_mov_html"))
    anexos = [a for a in (atual.get("anexos") or []) if a.get("ref") != ref]
    novo = dict(atual)
    novo.pop("paj_norm", None)
    novo.pop("salvo_em", None)
    novo["paj_label"] = atual.get("paj_label") or (_ler_json(pasta_paj / "metadata.json", {}).get("paj") or "")
    novo["fase"] = atual.get("fase") or sat_service.FASE_MOV
    if not (atual.get("descricao_mov_html") or "").strip():
        novo["descricao_mov_html"] = _descricao_juntada(dados)
    novo["anexos"] = [anexo, *anexos]
    r = acoes_sisdpu_service.salvar_rascunho(paj_norm, novo)
    if not r.get("ok"):
        return {"ok": False, "erro": r.get("erro") or "Falha ao preparar o console Ações no SISDPU."}
    _salvar_registro(paj_norm, doc, juntada="preparada", juntada_arquivo=nome_pdf,
                     juntada_preparada_em=datetime.now().isoformat(timespec="seconds"), erro_juntada="")
    return {"ok": True, "arquivo": nome_pdf, "tinha_rascunho": tinha_rascunho}


def anexar_ao_paj(paj_norm: str, documento_id: str) -> dict:
    """Dispara a juntada (task no loop corrente — chamar de rota async). A
    movimentação passa pela ficha de confirmação do SIS-DPU 2.0, como as demais."""
    import asyncio

    criados = _ler_json(_pasta(paj_norm) / ARQ_CRIADOS, [])
    registro = next((o for o in criados if str(o.get("documento_id")) == str(documento_id)), None)
    if not registro:
        return {"ok": False, "erro": "Ofício não pertence a este PAJ."}
    if not registro.get("assinado"):
        return {"ok": False, "erro": "Só o ofício assinado pode ser anexado ao PAJ."}
    if registro.get("juntada") in ("running", "ok"):
        return {"ok": False, "erro": "Este ofício já foi anexado ao PAJ ou está sendo anexado."}
    _salvar_registro(paj_norm, documento_id, juntada="running", erro_juntada="")
    t = asyncio.get_running_loop().create_task(_juntar_oficio(paj_norm, registro))
    _tarefas_juntada.add(t)
    t.add_done_callback(_tarefas_juntada.discard)
    return {"ok": True}
