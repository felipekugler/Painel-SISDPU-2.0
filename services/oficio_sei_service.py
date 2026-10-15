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
        "corpo_html": str(r.get("corpo_html") or "").strip(),
        "fechamento": str(r.get("fechamento") or "").strip(),
        "observacoes": str(r.get("observacoes") or "").strip(),
        "orientacao": str(r.get("orientacao") or "").strip(),
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

def iniciar_redacao(paj_norm: str, orientacao: str) -> dict:
    orientacao = (orientacao or "").strip()
    if not orientacao:
        return {"ok": False, "erro": "Escreva na caixa \"Orientação diretiva\" o que o ofício deve pedir e a "
                                      "quem (ex.: \"oficiar o SINE Curitiba pedindo cadastro da instituidora "
                                      "após 04/2023\")."}
    _pasta(paj_norm)
    atual = _status.get(paj_norm)
    if atual and atual.get("status") == "running":
        return {"ok": True, "status": "running"}
    with _lock:
        _status[paj_norm] = {"status": "running", "erro": "",
                             "iniciado_em": datetime.now().isoformat(timespec="seconds")}
    threading.Thread(target=_redigir_worker, args=(paj_norm, orientacao), daemon=True).start()
    return {"ok": True, "status": "running"}


def _falhar(paj_norm: str, erro: str) -> None:
    with _lock:
        _status[paj_norm] = {"status": "error", "erro": erro}


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


def _montar_prompt(ctx: dict, orientacao: str) -> str:
    assistidos = "\n".join(
        f"  - {a['nome']} — CPF {a['cpf'] or '(não informado)'}{' (principal)' if a['principal'] else ''}"
        for a in ctx["assistidos"]) or "  (não informado)"
    reps = "\n".join(f"  - {r['nome']} — CPF {r['cpf'] or '(não informado)'}" for r in ctx["representantes"])
    return f"""Você é assessor de Defensor Público Federal da Defensoria Pública da União (DPU), setor
previdenciário de Curitiba/PR (Coordenação dos Ofícios Especializados — COFE CTB). Redija UM
ofício administrativo a ser minutado no SEI.

## ORDEM DO DEFENSOR (decisão já tomada — siga à risca; define destinatário e pedido)
{orientacao}

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

## Formato da resposta
Responda APENAS com um objeto JSON válido (sem texto antes ou depois, sem cercas de código):
{{
  "destinatario_linhas": ["NOME DO ÓRGÃO/UNIDADE EM MAIÚSCULAS", "endereço", "CEP - cidade/UF", "e-mail: ..."],
  "assunto": "Solicitação de documentos",
  "vocativo": "Prezado(a) Senhor(a),",
  "corpo_paragrafos": ["parágrafo 1", "a) item", "parágrafo de prazo", "parágrafo de referência ao PAJ"],
  "observacoes": "o que o Defensor deve conferir antes de criar no SEI (ou string vazia)"
}}
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


def _redigir_worker(paj_norm: str, orientacao: str) -> None:
    try:
        ctx = _contexto_paj(paj_norm)
        prompt = _montar_prompt(ctx, orientacao)
        # --strict-mcp-config: NENHUM servidor MCP nesta chamada (o conector
        # do SEI está no escopo do usuário — redigir não pode gravar nada).
        # Modo de permissão padrão (sem bypass): no -p, ferramenta que pediria
        # permissão é negada. cwd neutro: sem CLAUDE.md de outro projeto.
        cmd = [*CLAUDE_CMD, "-p", "--strict-mcp-config", "--output-format", "text"]
        proc = subprocess.run(
            cmd, cwd=tempfile.gettempdir(), input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        try:
            j = _extrair_json(proc.stdout)
        except ValueError as e:
            saida = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            _falhar(paj_norm, f"Resposta da IA ilegível ({e}). Saída: {saida[-1200:] or '(vazia)'}")
            return
        rascunho = _limpar_rascunho({
            "destinatario": "\n".join(str(x) for x in (j.get("destinatario_linhas") or []) if str(x).strip()),
            "assunto": j.get("assunto") or "Solicitação de documentos",
            # Vazia por padrão: o corpo do modelo já termina pedindo que a resposta
            # cite o PAJ — a referência padrão repetia a mesma frase.
            "referencia": "",
            "vocativo": str(j.get("vocativo") or "").strip() or VOCATIVO_PADRAO,
            "corpo_html": _corpo_html([str(x) for x in (j.get("corpo_paragrafos") or [])]),
            "fechamento": FECHAMENTO_PADRAO,
            "observacoes": j.get("observacoes") or "",
            "orientacao": orientacao,
        })
        _gravar_json(_pasta(paj_norm) / ARQ_RASCUNHO, rascunho)
        with _lock:
            _status[paj_norm] = {"status": "done", "erro": "",
                                 "concluido_em": datetime.now().isoformat(timespec="seconds")}
    except subprocess.TimeoutExpired:
        _falhar(paj_norm, "A IA excedeu o tempo limite (5 min) redigindo o ofício.")
    except Exception as e:  # noqa: BLE001
        _falhar(paj_norm, f"{type(e).__name__}: {e}"[:500])


# ---------------------------------------------------------------------------
# 2. Gravação no SEI
# ---------------------------------------------------------------------------

def _destinatario_html(texto: str) -> str:
    linhas = [ln.strip() for ln in (texto or "").splitlines() if ln.strip()]
    return "\n".join(_p(ln, _CLS_ESQ) for ln in linhas)


def _normalizar_corpo(corpo: str) -> str:
    """O editor da tela (Quill) descarta classes que não conhece — reaplica o
    estilo de parágrafo do modelo de Ofício da COFE CTB em todo <p>."""
    return re.sub(r"<p(\s[^>]*)?>", f'<p class="{_CLS_CORPO}">', corpo or "")


def _numero_oficio(texto: str) -> str:
    """Nº do ofício ("Ofício - nº 9416409/2026 - COFE CTB") — NÃO o nº do
    processo de controle (08166.000183/2017-39), que o regex antigo pegava."""
    m = re.search(r"Of[ií]cio\s*-?\s*n\S{0,2}\s*(\d{5,})\s*/\s*(\d{4})", texto or "", re.I)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


_id_procedimento_cache: dict[str, str] = {}


def _id_procedimento() -> str:
    if PROCESSO_OFICIOS not in _id_procedimento_cache:
        idp = ID_PROCEDIMENTO_FALLBACK
        with contextlib.suppress(Exception):
            p = sei.chamar("consultar_processo", {"numero_processo": PROCESSO_OFICIOS,
                                                  "nivel_acesso_efetivo": False})
            idp = str(p.get("id") or idp)
        _id_procedimento_cache[PROCESSO_OFICIOS] = idp
    return _id_procedimento_cache[PROCESSO_OFICIOS]


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

def atualizar_situacao(paj_norm: str) -> dict:
    """Confere no SEI os ofícios deste PAJ ainda não vistos como assinados:
    assinaturas (listar_assinaturas) e o nº definitivo do ofício. Assinado, o
    item deixa de mostrar "conferir"/"Abrir para assinar" (os avisos eram da
    minuta — quem assina já revisou). Só leitura no SEI."""
    pasta = _pasta(paj_norm)
    criados = _ler_json(pasta / ARQ_CRIADOS, [])
    mudou = False
    for o in criados:
        doc = o.get("documento_id")
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
