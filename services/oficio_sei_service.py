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
    if rascunho and st.get("status") in (None, "idle"):
        st["status"] = "done"
    st["rascunho"] = rascunho
    st["criados"] = _ler_json(pasta / ARQ_CRIADOS, [])
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
        "assunto": str(r.get("assunto") or "").strip(),
        "referencia": str(r.get("referencia") or "").strip(),
        "corpo_html": str(r.get("corpo_html") or "").strip(),
        "observacoes": str(r.get("observacoes") or "").strip(),
        "orientacao": str(r.get("orientacao") or "").strip(),
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }


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
Corpo (depois de "Prezado(a) Senhor(a)," — que o painel já coloca):
  A DEFENSORIA PÚBLICA DA UNIÃO, com fulcro no art. 44, inciso X, da Lei Complementar nº 80/94, e atuando nos interesses do assistido FULANO DE TAL, CPF 000.000.000-00, solicita envio dos seguintes documentos:
  a) cópia do processo administrativo do NB 000.000.000-0
  Contando com sua presteza, solicito atendimento no prazo de 15 dias, podendo encaminhar sua resposta com documento digitalizado para o e-mail dpu.prev.pr@dpu.gov.br.
  Ao responder, por gentileza, fazer referência ao Processo de Assistência Jurídica PAJ 2026/029-00000.
(o painel encerra com "Atenciosamente," — não repita)

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
5. Não inclua cabeçalho, número do ofício, data, "Prezado(a) Senhor(a)", "Atenciosamente" nem assinatura
   — o SEI e o painel colocam.

## Formato da resposta
Responda APENAS com um objeto JSON válido (sem texto antes ou depois, sem cercas de código):
{{
  "destinatario_linhas": ["NOME DO ÓRGÃO/UNIDADE EM MAIÚSCULAS", "endereço", "CEP - cidade/UF", "e-mail: ..."],
  "assunto": "Solicitação de documentos",
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
    partes = [_p("Prezado(a) Senhor(a),", _CLS_CORPO)]
    partes += [_p(t, _CLS_CORPO) for t in paragrafos if str(t).strip()]
    partes.append(_p("Atenciosamente,", _CLS_CORPO))
    return "\n".join(partes)


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
            "referencia": f"Em resposta a este Ofício, por gentileza, indicar expressamente o PAJ {ctx['paj']}",
            "corpo_html": _corpo_html([str(x) for x in (j.get("corpo_paragrafos") or [])]),
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


def _assunto_html(assunto: str, referencia: str) -> str:
    # No modelo da COFE CTB, Assunto e Referência dividem UMA seção só
    # (idSecaoModelo 1049) — por isso vão juntos no campo `assunto`.
    partes = [f'<p class="{_CLS_ESQ}">Assunto: <b>{html.escape(assunto.strip())}</b></p>']
    if referencia.strip():
        partes.append(f'<p class="{_CLS_ESQ}">Referência: {html.escape(referencia.strip())}</p>')
    return "\n".join(partes)


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

    try:
        sei.chamar("auth_select_unit", {"unidade_id": UNIDADE_COFE_CTB})
        resp = sei.chamar("criar_minuta", {
            "numero_processo": PROCESSO_OFICIOS,
            "tipo_documento_id": TIPO_OFICIO,
            "titulo": f"PAJ {ctx['paj']}",
            "destinatario": _destinatario_html(r["destinatario"]),
            "assunto": _assunto_html(r["assunto"], r["referencia"]),
            "html": _normalizar_corpo(r["corpo_html"]),
        }, timeout=120)
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
    numero = str(resp.get("numero") or resp.get("numero_provisorio") or "")
    conferido = False
    with contextlib.suppress(Exception):
        sec = sei.chamar("listar_secoes", {"id_documento": doc_id, "incluir_conteudo": True,
                                           "max_chars_conteudo": 6000})
        tudo = html.unescape(" ".join(s.get("conteudo", "") for s in sec.get("secoes", [])))
        m = re.search(r"n\S*\s*(\d{6,})/\d{4}", tudo)
        if m:
            numero = m.group(1)
        conferido = PROCESSO_OFICIOS in tudo and ctx["paj"] in tudo
        if r["assunto"] not in tudo:
            avisos.append("O assunto não apareceu no documento gravado — confira no SEI.")
    if not conferido:
        avisos.append("Não consegui confirmar pela releitura que o ofício ficou no processo certo — confira no SEI.")

    registro = {
        "documento_id": doc_id,
        "numero_sei": numero,
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
