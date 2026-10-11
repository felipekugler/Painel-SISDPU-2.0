"""Leitor das intimações do e-proc SEM NAVEGADOR, pelo MCP do e-proc (HTTP).

Mesmo resultado de `eproc_intimacoes.ler_todos()` — {ok, resumo, secoes} com os
casos montados por `eproc_intimacoes.agrupar()` — para a tela de Prazos e todo o
resto do painel seguirem iguais. Escolhido pela variável EPROC_INTIMACOES_FONTE
(.env): "mcp" usa este leitor; o padrão segue o navegador (Painel 1.0).

De onde vem cada dado (conferido com dados reais em 11/10/2026, só leitura —
nada aqui marca ciência nem abre prazo):
  - LISTAS: painel do procurador → AJAX `retornar_citacao_intimacao_estado`
    (contadores + links assinados por categoria), via o MCP eproc-trf4 importado
    como biblioteca (login SSO + TOTP automático dele, sem cofre).
  - LINHAS: lidas do HTML da lista como a tela mostra (inclusive a coluna extra
    "Urgente" das listagens de urgentes — por isso não se usa o
    `parsers.parse_intimacoes` do MCP, que lê colunas por posição fixa).
  - LEMBRETES (de onde sai o PAJ): não vêm no HTML; a página os busca depois em
    `controlador_ajax.php?acao_ajax=listar_lembretes_externos` com POST
    `arrProcesso[]=<id>,<seq>` para cada processo da lista (string `arrProcessos`
    do próprio JS da página) → array JSON, um HTML por linha, na ordem das linhas.
  - COR DA LINHA: o e-proc pinta pelo CSS dinâmico `css/cor-capa.php`
    (`tr[data-classe='X'][data-competencia='Y'] {background-color:…}`), a partir
    dos atributos data-classe/data-competencia de cada linha.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from ingestao import eproc_intimacoes as ei
from ingestao.eproc_mcp_client import MCP_DIR

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

ESTADO = "PR"  # 1ª instância = JFPR (o mesmo painel que a leitura pelo navegador lê)

# categoria do painel (chave do eproc_intimacoes.CATEGORIAS) -> situação do MCP
SITUACAO = {
    "prazo_aberto": "aberto",
    "prazo_aberto_urgente": "aberto_urgente",
    "pendente_urgente": "pendente_urgente",
    "pendente": "pendente",
    "pendente_acordo": "pendente_acordo",
    "art334": "art334",
    "decurso": "decurso",
}

_RE_CNJ = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
_RE_TOOLTIP = re.compile(r"infraTooltipMostrar\(\s*'([\s\S]*?)'\s*,\s*'Lembretes'")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------------
# Cores (css/cor-capa.php)
# ---------------------------------------------------------------------------

def _tabela_cores(css: str) -> dict:
    """{(classe, competencia|None): 'rgb(r, g, b)'} — regras posteriores vencem,
    como no CSS; a mais específica (com competência) é preferida na consulta."""
    cores: dict = {}
    for seletores, corpo in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        m = re.search(r"background-color\s*:\s*#([0-9a-fA-F]{6})", corpo)
        if not m:
            continue
        h = m.group(1)
        rgb = f"rgb({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)})"
        for cls, comp in re.findall(
                r"tr\.infraTr(?:Clara|Escura)\[data-classe='([^']*)'\](?:\[data-competencia='([^']*)'\])?",
                seletores):
            cores[(cls, comp or None)] = rgb
    return cores


def _cor(cores: dict, classe: str | None, competencia: str | None) -> str:
    if classe is None:
        return ""
    return cores.get((classe, competencia)) or cores.get((classe, None)) or cores.get(("", None)) or ""


# ---------------------------------------------------------------------------
# Lembretes (listar_lembretes_externos)
# ---------------------------------------------------------------------------

def _texto(no) -> str:
    # textContent (sem quebra nos <br>), igual ao que a leitura pelo navegador
    # grava — mantém os lembretes já salvos idênticos entre as duas leituras.
    return _norm(no.get_text(""))


def _parse_lembrete(trecho: str) -> tuple[str, list[dict]]:
    from bs4 import BeautifulSoup
    m = _RE_TOOLTIP.search(trecho or "")
    if not m:
        return "", []
    tabela = _html.unescape(m.group(1).replace("\\'", "'"))
    itens = []
    for tr in BeautifulSoup(tabela, "html.parser").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            itens.append({"seq": _texto(tds[0]), "texto": _texto(tds[1])})
    return " · ".join(i["texto"] for i in itens), itens


async def _lembretes(cli, html: str) -> list[tuple[str, list[dict]]]:
    m_url = re.search(r"url:\s*'(controlador_ajax\.php\?acao_ajax=listar_lembretes_externos[^']+)'", html)
    m_arr = re.search(r"arrProcessos\s*=\s*'([^']*)'", html)
    if not m_url or not m_arr or not m_arr.group(1):
        return []
    corpo = [("arrProcesso[]", x) for x in m_arr.group(1).split(";") if x]
    r = await cli.post(_html.unescape(m_url.group(1)), data=corpo)
    try:
        lista = json.loads(r.text)
    except ValueError:
        raise RuntimeError(f"lembretes: resposta inesperada do e-proc: {r.text[:200]}")
    return [_parse_lembrete(x) for x in lista]


# ---------------------------------------------------------------------------
# Linhas de uma lista
# ---------------------------------------------------------------------------

def _linhas(html: str, cores: dict, base: str, lembretes: list) -> list[dict]:
    """Mesmo dict por linha que o _JS_EXTRAIR da leitura pelo navegador."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    i_lem = 0
    for tr in soup.select("tr.infraTrClara, tr.infraTrEscura"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue
        cel1 = _norm(tds[1].get_text(" "))
        mcnj = _RE_CNJ.search(cel1)
        if not mcnj:
            continue
        off = 1 if len(tds) >= 9 else 0          # listas de urgentes: coluna "Urgente" a mais
        urgente = _norm(tds[2].get_text(" ")) if off else ""
        juizo = (re.search(r"Ju.zo:\s*([A-Z0-9]+)", cel1, re.I) or [None, ""])[1]
        partes = _RE_CNJ.sub("", cel1)
        partes = re.sub(r"Ju.zo:\s*[A-Z0-9]+", "", partes, count=1, flags=re.I)
        partes = _norm(re.sub(r"Cadastrar Lembrete", "", partes, flags=re.I))
        a_proc = tds[1].find("a", href=re.compile(r"processo_selecionar"))
        processo_url = urljoin(base, a_proc["href"]) if a_proc else ""
        # lembrete: na ordem dos divProcessoLembrete (um por linha da lista)
        lembrete, itens = ("", [])
        if tds[1].find(id=re.compile(r"^divProcessoLembrete")) is not None:
            if i_lem < len(lembretes):
                lembrete, itens = lembretes[i_lem]
            i_lem += 1
        ev = _norm(tds[4 + off].get_text(" "))
        out.append({
            "processo": mcnj.group(0), "juizo": juizo, "partes": partes,
            "lembrete": lembrete, "lembrete_itens": itens,
            "cor": _cor(cores, tr.get("data-classe"), tr.get("data-competencia")),
            "processo_url": processo_url, "urgente": urgente,
            "classe": _norm(tds[2 + off].get_text(" ")), "assunto": _norm(tds[3 + off].get_text(" ")),
            "evento": ev, "prazo_dias": (re.search(r"(\d+)\s*dias?", ev, re.I) or [None, ""])[1],
            "data_envio": _norm(tds[5 + off].get_text(" ")),
            "inicio_prazo": _norm(tds[6 + off].get_text(" ")),
            "final_prazo": _norm(tds[7 + off].get_text(" ")),
        })
    return out


# ---------------------------------------------------------------------------
# Leitura completa
# ---------------------------------------------------------------------------

async def _ler_todos_corpo() -> dict:
    import client as mcp_client  # type: ignore  # MCP eproc-trf4
    import parsers  # type: ignore
    import server  # type: ignore

    cli = await server._get_client(1)
    base = mcp_client.base_url(1)
    estados = dict(await server._intimacao_estados(cli))
    if ESTADO not in estados:
        return {"ok": False, "erro": f"O painel do e-proc não trouxe a seção {ESTADO} (estados: {list(estados)})."}
    data = json.loads((await cli.get(estados[ESTADO])).text)
    contadores = parsers.resumo_intimacoes(data)

    resumo = [{"chave": c["chave"], "label": c["label"],
               "quantidade": int(contadores.get(SITUACAO[c["chave"]], 0) or 0)} for c in ei.CATEGORIAS]

    cores = _tabela_cores((await cli.get("css/cor-capa.php")).text)

    # Mesma defesa da leitura pelo navegador: categoria com contador > 0 e
    # detalhe vazio NÃO vira "todos saíram da caixa" — mantém a sincronização anterior.
    secoes_anteriores = (ei.carregar().get("secoes") or {})
    secoes: dict[str, list] = {}
    falhas: list[str] = []
    for cat in ei.CATEGORIAS:
        q = next(x["quantidade"] for x in resumo if x["chave"] == cat["chave"])
        if q <= 0:
            continue
        link = parsers.link_intimacao(data, SITUACAO[cat["chave"]])
        casos = []
        if link:
            html = (await cli.get(link)).text
            casos = ei.agrupar(_linhas(html, cores, base, await _lembretes(cli, html)))
        if casos:
            secoes[cat["chave"]] = casos
        elif secoes_anteriores.get(cat["chave"]):
            secoes[cat["chave"]] = secoes_anteriores[cat["chave"]]
            falhas.append(cat["chave"])

    res = {"ok": True, "resumo": resumo, "secoes": secoes, "fonte": "mcp-eproc"}
    if falhas:
        res["aviso"] = ("Falha ao carregar o detalhe de: " + ", ".join(falhas) +
                        " (o resumo indicava casos, mas a extração veio vazia) — mantidos os dados "
                        "da sincronização anterior para essas categorias.")
    return res


async def ler_todos() -> dict:
    """Mesmo contrato de eproc_intimacoes.ler_todos(): nunca lança."""
    try:
        return await _ler_todos_corpo()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"MCP do e-proc: {type(e).__name__}: {e}"[:300]}
