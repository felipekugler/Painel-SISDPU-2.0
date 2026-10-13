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

import asyncio
import contextvars
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

# Quanto ler do PAJ (ver `_detalhe`). Quem define é o chamador (services/
# ao_vivo_service.py); os botões de sincronização usam o padrão "completo".
MODO: contextvars.ContextVar[str] = contextvars.ContextVar("sisdpu2_modo", default="completo")
# Ao abrir um PAJ, os trâmites vêm da última leitura da caixa (mesmo antiga):
# reler a caixa inteira a cada PAJ aberto custaria ~1 registro por PAJ da caixa.
CAIXA_EM_CACHE_SERVE: contextvars.ContextVar[bool] = contextvars.ContextVar("sisdpu2_cache_caixa", default=False)


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

_CACHE_CAIXA_SEG = 180
_cache_caixa: dict = {"ts": 0.0, "itens": None}


def assinatura_caixa() -> tuple[int, str]:
    """Conferência BARATA da caixa (1 registro): total de trâmites + o mais
    recente. Mudou um dos dois → vale reler a lista inteira. Um trâmite que sai
    muda o total; um que entra vira o mais recente."""
    r = sis2.chamar("caixa_entrada", {"limite": 1, "pagina": 0, "ordenarPor": "DATA_ENVIO", "direcao": "DESC"})
    if not isinstance(r, dict) or "total" not in r:
        raise RuntimeError(f"Caixa do SIS-DPU 2.0 veio em formato inesperado: {str(r)[:200]}")
    topo = (r.get("itens") or [{}])[0]
    return int(r["total"]), str(topo.get("idTramite") or "")


def invalidar_cache_caixa() -> None:
    _cache_caixa.update(ts=0.0, itens=None)


def _caixa_completa() -> list[dict]:
    """Todos os trâmites pendentes, paginando. Levanta erro se o MCP não
    responder com a estrutura esperada — NUNCA devolve [] por falha (o
    sincronizador trataria caixa vazia como "tudo concluído").

    Cache curto: a mesma rodada lê a caixa até 3 vezes (lista, lista
    estruturada, trâmites do PAJ) — cada leitura custa ~1 registro por PAJ do
    teto diário do servidor."""
    import time as _t
    if _cache_caixa["itens"] is not None and (
            CAIXA_EM_CACHE_SERVE.get() or _t.monotonic() - _cache_caixa["ts"] < _CACHE_CAIXA_SEG):
        return list(_cache_caixa["itens"])
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
    _cache_caixa.update(ts=_t.monotonic(), itens=list(itens))
    return itens


async def caixa_de_entrada() -> dict:
    itens = await asyncio.to_thread(_caixa_completa)
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
        itens = await asyncio.to_thread(_caixa_completa)
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

def _mov_raw(m: dict, seq: int) -> dict:
    """Uma movimentação do MCP no formato da raspagem."""
    return {
        "seq": str(seq),
        "id_movimentacao": m.get("id"),
        "anexo": "",
        "tem_anexo_icone": bool(m.get("possuiArquivo") or m.get("quantidadeArquivos")),
        "data": _br(m.get("dataHora")),
        "movimentacao": m.get("tipo") or "",
        "fases": _fases(m.get("fases")),
        # a tela do legado mostra "Não Possui" quando a movimentação não tem texto
        "descricao": m.get("descricao") or "Não Possui",
        "links_decisao": [],
        "usuario": m.get("usuario") or "",
        "descricao_retorno": _html_para_texto(m.get("textoRetorno")),
    }


def _ordem(m: dict) -> tuple:
    return (m.get("dataHora") or "", int(m.get("id") or 0))


def _movimentacoes(id_processo: int, capa: dict) -> list[dict]:
    """Histórico INTEIRO, no formato da raspagem e com o Seq do legado
    (mais recente primeiro). Caro: cada movimentação conta no teto diário."""
    hist = capa.get("movimentacoes") or []  # paj_capa comHistorico=true (com possuiArquivo)
    total = int(capa.get("totalDeMovimentacoes") or 0)
    if not hist or (total and len(hist) < total):
        r = sis2.chamar("paj_movimentacoes", {"idProcesso": id_processo, "limite": 500})
        hist = r.get("itens", []) if isinstance(r, dict) else []
    movs = [_mov_raw(m, seq) for seq, m in enumerate(sorted(hist, key=_ordem), start=1)]
    movs.reverse()  # o legado lista da mais recente para a mais antiga
    return movs


def _locais_como_raw(paj: str) -> list[dict]:
    """Movimentações já guardadas no metadata.json, de volta ao formato da
    raspagem (mais recente primeiro) — para o sincronizador reescrever o
    sisdpu.txt sem perder nada quando a leitura não traz o histórico."""
    movs = ((_metadata_atual(paj).get("detalhes_sisdpu") or {}).get("movimentacoes") or [])
    out = []
    for m in sorted(movs, key=lambda x: int(x.get("seq") or 0), reverse=True):
        r = {"seq": str(m.get("seq")), "anexo": "", "tem_anexo_icone": bool(m.get("tem_anexo_icone")),
             "data": m.get("data_original") or "", "movimentacao": m.get("movimentacao") or "",
             "fases": m.get("fases") or "", "descricao": m.get("descricao") or "",
             "links_decisao": m.get("links_decisao") or [], "usuario": m.get("usuario") or "",
             "descricao_retorno": m.get("descricao_retorno") or ""}
        if m.get("id_movimentacao"):
            r["id_movimentacao"] = m["id_movimentacao"]
        out.append(r)
    return out


def _incremental(id_processo: int, capa: dict, locais: list[dict]) -> Optional[list[dict]]:
    """Só as movimentações NOVAS desde a última leitura, somadas às locais.
    None = não deu para encaixar com segurança (aí o chamador lê tudo).

    Confere o encaixe pela data/hora da movimentação local mais recente: ela
    tem de ser exatamente a que vem logo antes das novas no SIS 2."""
    total = capa.get("totalDeMovimentacoes")
    if total is None or not locais:
        return None
    total, n_local = int(total), len(locais)
    if total < n_local:
        return None
    novos = total - n_local
    ultimas = {m.get("id"): m for m in capa.get("ultimasMovimentacoes") or []}
    if novos == 0:
        topo = max(capa.get("ultimasMovimentacoes") or [{}], key=_ordem)
        return locais if (not topo or _br(topo.get("dataHora")) == locais[0]["data"][:19]) else None
    r = sis2.chamar("paj_movimentacoes", {"idProcesso": id_processo, "limite": min(novos + 1, 500), "pagina": 1})
    itens = sorted((r or {}).get("itens") or [], key=_ordem) if isinstance(r, dict) else []
    if len(itens) != novos + 1 or _br(itens[0].get("dataHora")) != locais[0]["data"][:19]:
        return None
    novas = []
    for i, m in enumerate(itens[1:], start=1):
        raw = _mov_raw(ultimas.get(m.get("id"), m), n_local + i)
        if m.get("id") not in ultimas:
            raw["tem_anexo_icone"] = True  # sem indicador nesta lista: confere os anexos dela
        novas.append(raw)
    novas.reverse()
    return novas + locais


_ficha_lida_em: dict = {}  # idPessoa -> "AAAA-MM-DD" da última leitura da ficha


def _contatos(capa: dict, paj: str) -> dict:
    """Telefones/e-mails/endereços do assistido principal (ficha do SIS 2)."""
    principal = next((a for a in capa.get("assistidos") or [] if a.get("principal")), None)
    if not principal:
        return {}
    # A ficha custa ~7 registros e quase nunca muda: no máximo 1 leitura por
    # pessoa por dia. Sem as chaves, o sincronizador preserva os contatos atuais.
    hoje = datetime.now().strftime("%Y-%m-%d")
    if _ficha_lida_em.get(principal.get("idPessoa")) == hoje and _metadata_atual(paj).get("telefones") is not None:
        return {}
    _ficha_lida_em[principal.get("idPessoa")] = hoje
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


def _extras(id_processo: int, capa: dict, paj: str) -> dict:
    """Processos judiciais, representantes, audiências e contatos (~4-6 registros).
    Chave ausente = o sincronizador preserva o valor anterior."""
    out: dict[str, Any] = {}
    try:
        pjs = [{"processo_judicial": p.get("numeroProcessoJudicial") or "",
                "juizo": p.get("juizo"), "data_inclusao": _br(p.get("dtCadastro"), com_hora=False) or None,
                "observacoes_processo": p.get("observacao"),
                "validacao": {"sucesso": True, "motivo": "Vínculo declarado no SIS-DPU 2.0"}}
               for p in sis2.chamar("paj_processos_judiciais", {"idProcesso": id_processo}) or []]
        out["processos_judiciais"] = pjs
        ult = pjs[-1] if pjs else {}
        out.update(processo_judicial=ult.get("processo_judicial"), juizo=ult.get("juizo"),
                   data_inclusao=ult.get("data_inclusao"), observacoes_processo=ult.get("observacoes_processo"),
                   validacao_processo=ult.get("validacao"))
    except Exception:  # noqa: BLE001
        pass
    try:
        out["representantes_legais"] = [
            {"idPessoa": str(r.get("idPessoa") or ""), "nome": r.get("nome") or "",
             "cpf": _cpf(r.get("cpf") or r.get("cpfCnpj"))}
            for r in sis2.chamar("paj_representantes", {"idProcesso": id_processo}) or []]
    except Exception:  # noqa: BLE001
        pass
    try:
        auds = []
        for a in sis2.chamar("audiencias_do_paj", {"idProcesso": id_processo}) or []:
            quando = a.get("dataHora") or a.get("data")
            auds.append({"data_hora": _br(quando)[:16] if quando else "",
                         "local": a.get("local") or "", "observacao": a.get("observacao") or ""})
        out["audiencias"] = auds
    except Exception:  # noqa: BLE001
        pass
    out.update(_contatos(capa, paj))
    return out


def _detalhe(id_processo: int, paj_esperado: str = "") -> dict:
    """Detalhe do PAJ conforme o MODO da leitura (ver `MODO`):
      - "caixa": só a capa SEM histórico (pretensão, assistido etc.); as
        movimentações são as já guardadas. ~1-4 registros.
      - "paj": capa + só as movimentações novas (incremental) + extras; cai
        para o histórico inteiro na 1ª leitura do PAJ ou se o encaixe falhar.
      - "completo": histórico inteiro sempre (botões de sincronização)."""
    modo = MODO.get()
    locais = _locais_como_raw(paj_esperado) if paj_esperado and modo != "completo" else []
    com_historico = modo == "completo" or (modo == "paj" and not locais)
    capa = sis2.chamar("paj_capa", {"idProcesso": id_processo, "comHistorico": com_historico})
    if not isinstance(capa, dict) or not capa.get("numeroProcessoFormatado"):
        return {"erro": f"SIS-DPU 2.0 não devolveu a capa do PAJ {paj_esperado or id_processo}."}
    paj = capa["numeroProcessoFormatado"]
    num, ano, uni = _paj_partes(paj)

    if com_historico:
        movs, leitura = _movimentacoes(id_processo, capa), "completa"
    elif modo == "caixa":
        movs, leitura = locais, "cabecalho"
    else:
        movs, leitura = _incremental(id_processo, capa, locais), "incremental"
        if movs is None:  # encaixe incerto: relê tudo
            capa = sis2.chamar("paj_capa", {"idProcesso": id_processo, "comHistorico": True})
            movs, leitura = _movimentacoes(id_processo, capa), "completa"

    assistidos = capa.get("assistidos") or []
    principal = next((a for a in assistidos if a.get("principal")), assistidos[0] if assistidos else {})
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
        "movimentacoes": movs,
        "narrativa": capa.get("narrativa") or "",
        "paj_numero": num, "paj_ano": ano, "paj_unidade": uni,
        "url": URL_DETALHE_LEGADO.format(id=id_processo),
        "urls_decisoes": [],
        "fonte": "mcp-sisdpu2",
        "leitura_mcp": leitura,
    }
    if modo != "caixa":
        det.update(_extras(id_processo, capa, paj))
    _ultimo.clear()
    _ultimo.update({"paj": paj, "id_processo": id_processo, "det": det})
    return det


def _id_processo(paj: str) -> Optional[int]:
    """Resolve o idProcesso: caixa em cache, metadata.json (url legada) ou processo_resolver."""
    for it in _cache_caixa["itens"] or []:
        if _paj_fmt(it.get("processo", "")) == paj and it.get("idProcesso"):
            return int(it["idProcesso"])
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
        idp = await asyncio.to_thread(_id_processo, paj)
        if not idp:
            return {"erro": f"PAJ {paj} não encontrado no SIS-DPU 2.0"}
        return await asyncio.to_thread(_detalhe, idp, paj)
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
            arqs = await asyncio.to_thread(
                sis2.chamar, "movimentacao_arquivos", {"idMovimentacao": int(mov["id_movimentacao"])}) or []
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


_cache_tramites: dict = {}  # (id_processo, limite) -> (monotonic, resultado)
_CACHE_TRAMITES_SEG = 120


def invalidar_tramites(id_processo) -> None:
    """Chamada depois de toda gravação do painel no PAJ: a próxima leitura do
    painel "Caixas onde o PAJ está aberto" já traz o que acabou de ser gravado."""
    for chave in [c for c in _cache_tramites if c[0] == int(id_processo)]:
        _cache_tramites.pop(chave, None)


def _tramitacoes(id_processo: int, limite: int) -> dict:
    itens = (sis2.chamar("tramite_historico", {"idProcesso": id_processo, "limite": limite, "pagina": 1})
             or {}).get("itens") or []
    todas, abertas = [], []
    for t in itens:
        local = t.get("grupo") or t.get("destinatario") or ""
        linha = {"local": local, "grupo": t.get("grupo") or "", "destinatario": t.get("destinatario") or "",
                 "remetente": t.get("remetente") or "", "data": _br(t.get("dataTramite")),
                 "descricao": html.unescape(t.get("descricao") or ""),
                 "prazo": _br(t.get("prazo"), com_hora=False) if t.get("prazo") else "",
                 "conclusao": _br(t.get("dataConclusao")) if t.get("concluido") else ""}
        todas.append(linha)
        if not t.get("concluido") and local:
            abertas.append(linha)
    return {"ok": True, "abertas": abertas, "tramitacoes": todas, "erro": "", "fonte": "mcp-sisdpu2"}


async def caixas_e_historico_paj(id_processo: str, limite: int = 10) -> dict:
    """Mesmo retorno de sisdpu_movtram.caixas_e_historico_paj (painel "Caixas
    onde o PAJ está aberto"), pelo MCP. ~`limite` registros; cache curto
    (_CACHE_TRAMITES_SEG) porque a página do PAJ consulta isto a cada abertura —
    e apagado a cada gravação do painel no PAJ (invalidar_tramites)."""
    import time as _t
    if not id_processo:
        return {"ok": False, "abertas": [], "tramitacoes": [], "erro": "id_processo ausente."}
    chave = (int(id_processo), limite)
    em_cache = _cache_tramites.get(chave)
    if em_cache and _t.monotonic() - em_cache[0] < _CACHE_TRAMITES_SEG:
        return em_cache[1]
    try:
        res = await asyncio.to_thread(_tramitacoes, int(id_processo), limite)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "abertas": [], "tramitacoes": [], "erro": f"SIS-DPU 2.0: {e}"}
    _cache_tramites[chave] = (_t.monotonic(), res)
    return res


# ---------------------------------------------------------------------------
# Download dos arquivos originais (documento_baixar)
# ---------------------------------------------------------------------------

# Progresso por pasta de destino, para a página do PAJ mostrar "baixando 3/10".
PROGRESSO_DOWNLOAD: dict[str, dict] = {}
# Pastas cujo download foi interrompido pelo Defensor (toast "Interromper"): o
# laço de baixar_todos_arquivos para antes do próximo arquivo.
INTERROMPER_DOWNLOAD: set[str] = set()

_GRAVADOR = (
    "import os,sys\n"
    "d=sys.argv[1];t=d+'.tmp'\n"
    "open(t,'wb').write(sys.stdin.buffer.read());os.replace(t,d)\n"
)


def _gravar_binario(destino, dados: bytes) -> None:
    """Grava atômico (tmp + replace). Se o antivírus barrar o processo do
    servidor (PermissionError — ver sincronizador._gravar_via_subprocesso),
    grava por um processo Python de vida curta, sem rede."""
    import os
    import subprocess
    import sys
    tmp = str(destino) + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(dados)
        os.replace(tmp, str(destino))
        return
    except PermissionError:
        pass
    r = subprocess.run([sys.executable, "-c", _GRAVADOR, str(destino)], input=dados,
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise OSError(f"gravação bloqueada: {r.stderr.decode('utf-8', 'replace')[:200]}")


def baixar_arquivo_mcp(id_arquivo: int) -> tuple[bytes, dict]:
    """Baixa o arquivo inteiro (todas as partes) e confere o sha256 do servidor."""
    import base64
    import hashlib
    partes, info, n = [], {}, 1
    while True:
        r = sis2.chamar("documento_baixar", {"idArquivo": int(id_arquivo), "parte": n})
        if not isinstance(r, dict) or "conteudoBase64" not in r:
            raise RuntimeError(f"resposta inesperada do documento_baixar: {str(r)[:200]}")
        info = r
        partes.append(base64.b64decode(r["conteudoBase64"]))
        if n >= int(r.get("totalPartes") or 1):
            break
        n += 1
    dados = b"".join(partes)
    esperado = (info.get("sha256DoArquivo") or "").lower()
    if esperado and hashlib.sha256(dados).hexdigest() != esperado:
        raise RuntimeError("arquivo corrompido na montagem (sha256 não confere)")
    return dados, info


async def baixar_todos_arquivos(pasta_destino, lista_arquivos: list[dict], log=None,
                                seq_por_file_id: dict | None = None, **_kw) -> dict:
    """Mesmo contrato do sisdpu_client.baixar_todos_arquivos (itens com seq,
    data, nomeArq, href) e o MESMO nome em disco: "seq075 - 43202969 - nome.pdf".
    Pula o que já existe. Sem navegador nem SISDPU legado."""
    from pathlib import Path
    pasta = Path(pasta_destino)
    pasta.mkdir(parents=True, exist_ok=True)
    prog = PROGRESSO_DOWNLOAD.setdefault(str(pasta), {})
    prog.update(total=len(lista_arquivos), feitos=0, baixados=0, erros=0, rodando=True)
    baixados = pulados = 0
    erros: list[str] = []
    try:
        INTERROMPER_DOWNLOAD.discard(str(pasta))
        for item in lista_arquivos:
            if str(pasta) in INTERROMPER_DOWNLOAD:
                INTERROMPER_DOWNLOAD.discard(str(pasta))
                prog["interrompido"] = True
                if log:
                    log("  [mcp] download interrompido pelo Defensor")
                break
            href = item.get("href") or ""
            nome = item.get("nomeArq") or "arquivo"
            m = re.search(r"/(\d+)/[^/]*$", href)
            if not m:
                erros.append(f"{nome}: sem id de arquivo (anexo antigo só por clique) — não baixado")
                prog["feitos"] += 1
                continue
            file_id = m.group(1)
            seq = (seq_por_file_id or {}).get(file_id, item.get("seq"))
            try:
                seq_fmt = f"{int(seq):03d}"
            except (TypeError, ValueError):
                seq_fmt = str(seq)
            destino = pasta / f"seq{seq_fmt} - {file_id} - {nome}"
            if destino.exists() and destino.stat().st_size > 0:
                pulados += 1
                prog["feitos"] += 1
                continue
            try:
                dados, _info = await asyncio.to_thread(baixar_arquivo_mcp, int(file_id))
                await asyncio.to_thread(_gravar_binario, destino, dados)
                baixados += 1
                prog["baixados"] += 1
                if log:
                    log(f"  [mcp] baixado {destino.name} ({len(dados)} bytes)")
            except Exception as e:  # noqa: BLE001
                erros.append(f"{nome}: {e}")
                prog["erros"] += 1
                if log:
                    log(f"  [mcp] ERRO {nome}: {e}")
            prog["feitos"] += 1
    finally:
        prog["rodando"] = False
    return {"baixados": baixados, "pulados": pulados, "erros": erros, "total": len(lista_arquivos)}


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
    "baixar_todos_arquivos",
}
