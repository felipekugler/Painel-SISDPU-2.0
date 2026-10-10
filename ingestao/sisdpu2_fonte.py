"""Fonte de dados "MCP do SIS-DPU 2.0" para a sincronização — mesmo CONTRATO da raspagem.

O sincronizador (ingestao/sincronizador.py) fala com o SISDPU por
`sisdpu_client.X(...)` e `sisdpu_movtram.X(...)`. Este módulo oferece as
MESMAS funções, com o MESMO formato de retorno, lendo do MCP em vez da tela —
assim o sincronizador, o metadata.json e todas as telas continuam iguais.

A troca é feita por `ProxyFonte` (fim do arquivo): o sincronizador recebe um
proxy no lugar dos dois módulos; a cada chamada, se a fonte configurada for
"mcp" E a função existir aqui, usa esta; senão cai no módulo legado. Por isso
o que NÃO está aqui continua no legado — em especial o DOWNLOAD dos PDFs
(baixar_todos_arquivos / baixar_documento_via_commandlink), porque o MCP só
devolve texto. Sync Levíssima e Leve pelo MCP não abrem navegador nenhum; a
Completa abre o legado só para baixar os arquivos.

Fatos que sustentam o mapeamento (conferidos com dados reais em 10/10/2026):
  - idProcesso do MCP = `id=` da URL legada; idTramite = `data-rk` da caixa;
    id de arquivo = id de /sisdpu/rest/download/pdf/{id}/{nome}.
  - O "Seq." do legado é a posição cronológica da movimentação. Aqui: ordem por
    (dataHora, id) crescente — movimentações com o mesmo carimbo de hora
    desempatam pelo id (PAJ 2024/029-01203: ids 122577125/122577126 de
    21/09/2026 14:38:52 = Seq 78/79 no legado).
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any, Optional

from config import PAJS_DIR
from ingestao import sisdpu2_mcp_client as sis2

URL_DETALHE_LEGADO = ("https://sisdpu.dpu.def.br/sisdpu/pages/atendimento/"
                      "detalhamentoProcesso.xhtml?faces-redirect=true&tp=D&id={id}")

# Estado equivalente ao "navegador parado no detalhe do PAJ": extrair_documentos_por_seq
# do legado assume a página aberta pelo movimentacoes_paj anterior; aqui guardamos
# o que aquela leitura trouxe.
_ultimo: dict = {}


# ---------------------------------------------------------------------------
# Utilidades de formato (deixar igual ao que a raspagem produz)
# ---------------------------------------------------------------------------

def _dt(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)[:26])
    except ValueError:
        return None


def _br(iso: Optional[str], com_hora: bool = True) -> str:
    d = _dt(iso)
    if not d:
        return ""
    return d.strftime("%d/%m/%Y %H:%M:%S" if com_hora else "%d/%m/%Y")


def _cpf(v: Optional[str]) -> str:
    s = re.sub(r"\D", "", v or "")
    return f"{s[:3]}.{s[3:6]}.{s[6:9]}-{s[9:]}" if len(s) == 11 else (v or "")


def _html_para_texto(h: Optional[str]) -> str:
    if not h:
        return ""
    t = re.sub(r"(?i)<br\s*/?>|</p>", "\n", h)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _fases(f: Optional[str]) -> str:
    """Legado mostra cada fase como "° Nome;" — a tabela do painel tira o "°"."""
    f = (f or "").strip()
    return f"° {f};" if f else ""


def _paj_partes(paj: str) -> tuple[str, str, str]:
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    return (m.group(3), m.group(1), m.group(2)) if m else ("", "", "")


def _paj_fmt(numero: str) -> str:
    """'202402901203' (formato do MCP na caixa) -> '2024/029-01203'."""
    s = re.sub(r"\D", "", numero or "")
    return f"{s[:4]}/{s[4:7]}-{s[7:]}" if len(s) >= 8 else (numero or "")


def _oficio(nome: str) -> str:
    return (nome or "").replace("E.", "").strip()[:120]


def _metadata_atual(paj: str) -> dict:
    num, ano, uni = _paj_partes(paj)
    p = PAJS_DIR / f"PAJ-{ano}-{uni}-{num}" / "metadata.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Caixa de entrada
# ---------------------------------------------------------------------------

def _caixa_completa() -> list[dict]:
    """Todos os trâmites pendentes, paginando. Levanta erro se o MCP não
    responder com a estrutura esperada — NUNCA devolve [] por falha (o
    sincronizador trataria caixa vazia como "tudo concluído")."""
    itens: list[dict] = []
    pagina = 0
    while True:
        r = sis2.chamar("caixa_entrada", {"limite": 100, "pagina": pagina,
                                          "ordenarPor": "DATA_ENVIO", "direcao": "DESC"})
        if not isinstance(r, dict) or "itens" not in r or "total" not in r:
            raise RuntimeError(f"Caixa do SIS-DPU 2.0 veio em formato inesperado: {str(r)[:200]}")
        itens.extend(r["itens"] or [])
        if pagina + 1 >= int(r.get("totalPaginas") or 1):
            break
        pagina += 1
    total = int(r.get("total") or 0)
    if len(itens) < total:
        raise RuntimeError(f"Caixa do SIS-DPU 2.0 incompleta: {len(itens)} de {total} itens.")
    return itens


async def caixa_de_entrada() -> dict:
    itens = _caixa_completa()
    # O sincronizador passa cada string a parse_item_caixa — aqui elas são JSON.
    return {"url": "mcp-sisdpu2:caixa_entrada",
            "itens_tabela": [json.dumps(i, ensure_ascii=False) for i in itens],
            "texto_pagina": ""}


def parse_item_caixa(item_raw: str) -> dict | None:
    if not item_raw.startswith("{"):  # linha da raspagem (troca de fonte no meio do caminho)
        from ingestao import sisdpu_client
        return sisdpu_client.parse_item_caixa(item_raw)
    it = json.loads(item_raw)
    paj = _paj_fmt(it.get("processo", ""))
    if not re.match(r"\d{4}/\d{3}-\d+", paj):
        return None
    etiquetas = [str(m.get("descricao") if isinstance(m, dict) else m) for m in (it.get("marcadores") or [])]
    return {
        "paj": paj,
        "oficio": _oficio(it.get("oficio", "")),
        "assistido": (it.get("assistido") or "")[:120],
        "data": _br(it.get("dataEnvio")),
        "desc": html.unescape(it.get("descricao") or ""),
        "etiqueta": " ".join(e for e in etiquetas if e)[:80],
    }


async def listar_caixa_estruturada() -> dict:
    try:
        itens = _caixa_completa()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), "itens": []}
    out = []
    for it in itens:
        desc = html.unescape(it.get("descricao") or "")
        paj = _paj_fmt(it.get("processo", ""))
        out.append({
            "id_tramite": str(it.get("idTramite") or ""),
            "id": str(it.get("idProcesso") or ""),
            "paj": paj,
            "assistido": it.get("assistido") or "",
            "data_envio": _br(it.get("dataEnvio")),
            "remetente": it.get("remetente") or "",
            "prazo": _br(it.get("prazo"), com_hora=False) if it.get("prazo") else "",
            "descricao": desc,
            "linha_texto": " | ".join(x for x in (paj, it.get("assistido") or "", _br(it.get("dataEnvio")),
                                                  it.get("remetente") or "", desc) if x),
            "url_detalhe": URL_DETALHE_LEGADO.format(id=it.get("idProcesso")),
        })
    return {"ok": True, "itens": out}


# ---------------------------------------------------------------------------
# Detalhe do PAJ
# ---------------------------------------------------------------------------

def _movimentacoes(id_processo: int, capa: dict) -> list[dict]:
    """Todas as movimentações, já no formato da raspagem e com o Seq do legado."""
    hist = (capa.get("historico") or capa.get("movimentacoes")
            or capa.get("historicoMovimentacoes") or [])
    total = int(capa.get("totalDeMovimentacoes") or 0)
    if not hist or (total and len(hist) < total):
        # sem histórico completo na capa: lista paginada (sem o indicador de anexo)
        r = sis2.chamar("paj_movimentacoes", {"idProcesso": id_processo, "limite": 500})
        hist = r.get("itens", []) if isinstance(r, dict) else []
    ordenadas = sorted(hist, key=lambda m: (m.get("dataHora") or "", int(m.get("id") or 0)))
    movs = []
    for seq, m in enumerate(ordenadas, start=1):
        movs.append({
            "seq": str(seq),
            "id_movimentacao": m.get("id"),
            "anexo": "",
            "tem_anexo_icone": bool(m.get("possuiArquivo") or m.get("quantidadeArquivos")),
            "data": _br(m.get("dataHora")),
            "movimentacao": m.get("tipo") or "",
            "fases": _fases(m.get("fases")),
            "descricao": m.get("descricao") or "",
            "links_decisao": [],
            "usuario": m.get("usuario") or "",
            "descricao_retorno": _html_para_texto(m.get("textoRetorno")),
        })
    movs.reverse()  # o legado lista da mais recente para a mais antiga
    return movs


def _contatos(capa: dict, paj: str) -> dict:
    """Telefones/e-mails/endereços do assistido principal (ficha do SIS 2)."""
    principal = next((a for a in capa.get("assistidos") or [] if a.get("principal")), None)
    if not principal:
        return {}
    try:
        ficha = sis2.chamar("assistido_ficha", {"idPessoa": int(principal["idPessoa"])})
    except Exception:  # noqa: BLE001
        return {}  # sem as chaves: o sincronizador preserva o que já tinha
    if not isinstance(ficha, dict):
        return {}
    tels = [{"numero": c.get("valor", ""), "obs": c.get("observacao") or ""}
            for c in ficha.get("contatos") or [] if c.get("idTipo") == 1]
    emails = [c.get("valor", "") for c in ficha.get("contatos") or [] if c.get("idTipo") == 2]
    # A ficha traz só o id do município: aproveita município/UF já conhecidos
    # do mesmo endereço (sync anterior pela tela) em vez de apagá-los.
    antigos = _metadata_atual(paj).get("enderecos") or []
    ends = []
    for e in ficha.get("enderecos") or []:
        mesmo = next((a for a in antigos if a.get("logradouro") == e.get("logradouro")
                      and str(a.get("numero")) == str(e.get("numero"))), {})
        ends.append({"tipo": mesmo.get("tipo", ""), "logradouro": e.get("logradouro") or "",
                     "numero": e.get("numero") or "", "complemento": e.get("complemento") or "",
                     "bairro": e.get("bairro") or "", "municipio": mesmo.get("municipio", ""),
                     "uf": mesmo.get("uf", ""), "cep": e.get("cep") or ""})
    return {"telefones": tels, "emails": emails, "enderecos": ends}


def _detalhe(id_processo: int, paj_esperado: str = "") -> dict:
    capa = sis2.chamar("paj_capa", {"idProcesso": id_processo, "comHistorico": True})
    if not isinstance(capa, dict) or not capa.get("numeroProcessoFormatado"):
        return {"erro": f"SIS-DPU 2.0 não devolveu a capa do PAJ {paj_esperado or id_processo}."}
    paj = capa["numeroProcessoFormatado"]
    num, ano, uni = _paj_partes(paj)

    assistidos = capa.get("assistidos") or []
    principal = next((a for a in assistidos if a.get("principal")), assistidos[0] if assistidos else {})

    pjs = []
    try:
        for p in sis2.chamar("paj_processos_judiciais", {"idProcesso": id_processo}) or []:
            pjs.append({"processo_judicial": p.get("numeroProcessoJudicial") or "",
                        "juizo": p.get("juizo"), "data_inclusao": _br(p.get("dtCadastro"), com_hora=False) or None,
                        "observacoes_processo": p.get("observacao"),
                        "validacao": {"sucesso": True, "motivo": "Vínculo declarado no SIS-DPU 2.0"}})
    except Exception:  # noqa: BLE001
        pjs = None  # sem a chave: o sincronizador preserva os anteriores

    reps = None
    try:
        reps = [{"idPessoa": str(r.get("idPessoa") or ""), "nome": r.get("nome") or "",
                 "cpf": _cpf(r.get("cpf") or r.get("cpfCnpj"))}
                for r in sis2.chamar("paj_representantes", {"idProcesso": id_processo}) or []]
    except Exception:  # noqa: BLE001
        pass

    auds = []
    try:
        for a in sis2.chamar("audiencias_do_paj", {"idProcesso": id_processo}) or []:
            quando = a.get("dataHora") or a.get("data")
            auds.append({"data_hora": _br(quando)[:16] if quando else "",
                         "local": a.get("local") or "", "observacao": a.get("observacao") or ""})
    except Exception:  # noqa: BLE001
        pass

    det: dict[str, Any] = {
        "assistido": capa.get("nomeAssistidos") or principal.get("nome", ""),
        "cpf_assistido": _cpf(principal.get("cpfCnpj")),
        "contato_app_sisdpu": bool(capa.get("recebePeloApp")),
        "assistidos_pf": [{"idPessoa": str(a.get("idPessoa")), "nome": a.get("nome", ""),
                           "cpf": _cpf(a.get("cpfCnpj")), "principal": bool(a.get("principal"))}
                          for a in assistidos],
        "paj": paj,
        "status_paj": "Arquivado" if capa.get("arquivado") else (
            "Arquivado provisoriamente" if capa.get("arquivadoProvisorio") else "Ativo"),
        "oficio": capa.get("nomeOficio") or "",
        "pretensao": capa.get("pretensaoPrincipal") or "",
        "data_abertura": (_dt(capa.get("dataAbertura")).strftime("%d/%m/%Y às %H:%M")
                          if _dt(capa.get("dataAbertura")) else ""),
        "decurso": (f"{_br(capa.get('dataDecursoPrazo'), com_hora=False)} ({capa.get('situacaoDecursoPrazo')})"
                    if capa.get("dataDecursoPrazo") else ""),
        "foro_detalhado": "",
        "movimentacoes": _movimentacoes(id_processo, capa),
        "audiencias": auds,
        "narrativa": capa.get("narrativa") or "",
        "paj_numero": num, "paj_ano": ano, "paj_unidade": uni,
        "url": URL_DETALHE_LEGADO.format(id=id_processo),
        "urls_decisoes": [],
        "fonte": "mcp-sisdpu2",
    }
    if pjs is not None:
        det["processos_judiciais"] = pjs
        ult = pjs[-1] if pjs else {}
        det["processo_judicial"] = ult.get("processo_judicial")
        det["juizo"] = ult.get("juizo")
        det["data_inclusao"] = ult.get("data_inclusao")
        det["observacoes_processo"] = ult.get("observacoes_processo")
        det["validacao_processo"] = ult.get("validacao")
    if reps is not None:
        det["representantes_legais"] = reps
    det.update(_contatos(capa, paj))
    _ultimo.clear()
    _ultimo.update({"paj": paj, "id_processo": id_processo, "det": det})
    return det


def _id_processo(paj: str) -> Optional[int]:
    """Resolve o idProcesso: metadata.json (url legada) ou processo_resolver."""
    url = ((_metadata_atual(paj).get("sisdpu_raw") or {}).get("url") or "")
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return int(m.group(1))
    r = sis2.chamar("processo_resolver", {"numero": paj})
    return int(r["idProcesso"]) if isinstance(r, dict) and r.get("idProcesso") else None


async def movimentacoes_paj(numero: str, ano: str, unidade: str, esperas_curtas: bool = False,
                            **_kw) -> dict:
    paj = f"{ano}/{unidade}-{numero}"
    try:
        idp = _id_processo(paj)
        if not idp:
            return {"erro": f"PAJ {paj} não encontrado no SIS-DPU 2.0"}
        return _detalhe(idp, paj)
    except Exception as e:  # noqa: BLE001
        # NUNCA levanta: exceção aqui faria o sincronizador deixar o PAJ fora de
        # `pajs_na_caixa` e a reconciliação o daria por concluído (apagando anexos).
        # Com {"erro"} ele segue como "na caixa, leitura falhou".
        return {"erro": f"Falha ao ler o PAJ {paj} no SIS-DPU 2.0: {e}"}


async def buscar_paj_global(numero: str, ano: str, unidade: str, forcar_sessao: bool = True,
                            esperas_curtas: bool = False, **_kw) -> dict:
    return await movimentacoes_paj(numero, ano, unidade, esperas_curtas=esperas_curtas)


async def abrir_detalhamento_paj(numero: str, ano: str, unidade: str, esperas_curtas: bool = False,
                                 **_kw) -> dict:
    return await movimentacoes_paj(numero, ano, unidade, esperas_curtas=esperas_curtas)


async def extrair_documentos_por_seq(seqs_conhecidos=None, deve_cancelar=None, log=None, **_kw) -> dict:
    """Anexos de cada movimentação do ÚLTIMO PAJ lido, no formato de 5 chaves que
    o metadata.json guarda. Só consulta as movimentações com indicador de anexo
    e ainda não conhecidas (mesma economia da raspagem)."""
    det = _ultimo.get("det")
    if not det:
        return {"erro": "nenhum PAJ aberto", "movimentacoes_com_documentos": [],
                "total_documentos": 0, "falhou": True}
    conhecidos = {str(s) for s in (seqs_conhecidos or [])}
    com_docs, sem_docs, pulados, total = [], [], [], 0
    for mov in det["movimentacoes"]:
        seq = str(mov["seq"])
        if seq in conhecidos:
            pulados.append(seq)
            continue
        if not mov.get("tem_anexo_icone"):
            sem_docs.append(seq)
            continue
        if deve_cancelar and deve_cancelar():
            break
        try:
            arqs = sis2.chamar("movimentacao_arquivos", {"idMovimentacao": int(mov["id_movimentacao"])}) or []
        except Exception as e:  # noqa: BLE001
            return {"erro": str(e), "movimentacoes_com_documentos": com_docs,
                    "total_documentos": total, "falhou": True}
        docs = [{"tipoArquivo": a.get("tipoArquivo") or "",
                 "descricao": a.get("descricao") or "",
                 "nomeArquivo": a.get("nomeArquivo") or "",
                 "urlDownload": sis2.url_download_legado(a["id"], a.get("nomeArquivo") or "arquivo"),
                 "commandLinkId": None} for a in arqs if a.get("id")]
        if docs:
            total += len(docs)
            com_docs.append({"seq": seq, "dataHora": mov["data"], "movimentacao": mov["movimentacao"],
                             "documentos": docs})
        else:
            sem_docs.append(seq)
        if log:
            log(f"  [mcp] seq {seq}: {len(docs)} anexo(s)")
    return {"paj": det["paj"], "assistido": det.get("assistido", ""),
            "movimentacoes_com_documentos": com_docs, "total_documentos": total, "indice": 0,
            "seqsPuladas": pulados, "seqsVerificadosSemDocumento": sem_docs, "tabela_markdown": ""}


async def reconectar() -> dict:
    """Sem navegador: só confere se a sessão do MCP continua válida."""
    if not sis2.conectado():
        return {"ok": False, "status": "credenciais", "detalhe": "Conecte o painel ao SIS-DPU 2.0."}
    return {"ok": True, "status": "ok", "detalhe": "MCP do SIS-DPU 2.0"}


async def _ensure_logged_in() -> None:
    if not sis2.conectado():
        raise sis2.Sisdpu2LoginNecessario("Conecte o painel ao SIS-DPU 2.0 (Configurações).")


# ---------------------------------------------------------------------------
# Troca de fonte
# ---------------------------------------------------------------------------

def fonte_mcp_ativa() -> bool:
    from services import config_painel_service
    return config_painel_service.get_fonte_dados() == "mcp"


class ProxyFonte:
    """Fica no lugar de `sisdpu_client` / `sisdpu_movtram` dentro do sincronizador.
    Com a fonte "mcp", usa a função deste módulo quando ela existe; o resto
    (downloads, utilitários puros) segue no módulo legado."""

    def __init__(self, legado) -> None:
        self._legado = legado

    def __getattr__(self, nome: str):
        if fonte_mcp_ativa():
            proprio = globals().get(nome)
            if callable(proprio) and nome in _SUBSTITUIDAS:
                return proprio
        return getattr(self._legado, nome)


_SUBSTITUIDAS = {
    "caixa_de_entrada", "parse_item_caixa", "listar_caixa_estruturada",
    "movimentacoes_paj", "buscar_paj_global", "abrir_detalhamento_paj",
    "extrair_documentos_por_seq", "reconectar", "_ensure_logged_in",
}
