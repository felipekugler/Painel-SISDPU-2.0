"""Auto-atualização dos parâmetros externos do cálculo do valor da causa:

  • Índices de correção monetária (INPC, IPCA-E) — API do IBGE (SIDRA)
  • SELIC acumulada no mês (substitui INPC/IPCA-E a partir de 12/2021, EC
    113/2021) — API do Banco Central (SGS série 4390)
  • Salário mínimo — API do Banco Central (SGS série 1619)

Grava caches JSON ao lado de indices_cm.py / salario_minimo.py, que esses
módulos mesclam no import. Assim o cálculo usa sempre os valores mais frescos,
sem manutenção manual mensal/anual.

Séries pinadas (equivalência validada mês a mês contra as tabelas locais):
  INPC   -> SIDRA agregado 1736, variável 44   (variação mensal %)
  IPCA-E -> IPCA-15 -> SIDRA agregado 3065, variável 355  (variação mensal %)
  SELIC  -> BCB SGS série 4390  (taxa acumulada no mês, %)
  Salário mínimo -> BCB SGS série 1619  (valor mensal, R$)

Offline-first: falha de rede não quebra nada — o cálculo cai no cache/tabela e o
painel avisa quando um mês ainda não tem índice.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import sys
import urllib.request
from pathlib import Path

from config import VALOR_CAUSA_DIR

# ── Correção monetária (IBGE) ────────────────────────────────────────────────
_CACHE_CM = Path(VALOR_CAUSA_DIR) / "indices_cm_extra.json"
_TTL_CM_HORAS = 12
_SERIES_CM = {"INPC": (1736, 44), "IPCA_E": (3065, 355)}
# O IBGE aceita User-Agent custom; o BCB é sensível (bloqueia UA de app, aceita
# UA de navegador). Por isso cada fonte usa o seu.
_UA_IBGE = {"User-Agent": "PainelSISDPU/valor-causa"}
_UA_BCB = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}

# ── Salário mínimo (Banco Central) ───────────────────────────────────────────
_CACHE_SM = Path(VALOR_CAUSA_DIR) / "salario_minimo_extra.json"
_TTL_SM_HORAS = 24 * 7  # muda ~1x/ano; checar 1x/semana basta
_SGS_SALARIO_MINIMO = 1619

# ── SELIC (Banco Central) — mesmo cache/TTL da correção monetária, pois é o
# índice que a substitui a partir de 12/2021 (ver indices_cm.SELIC_INICIO) ────
_SGS_SELIC = 4390


# ── Helpers de cache ─────────────────────────────────────────────────────────

def _ler(caminho: Path) -> dict:
    with contextlib.suppress(Exception):
        return json.loads(caminho.read_text(encoding="utf-8"))
    return {}


def _fresco(cache: dict, ttl_horas: int) -> bool:
    ts = cache.get("atualizado_em")
    if not ts:
        return False
    with contextlib.suppress(Exception):
        return (_dt.datetime.now() - _dt.datetime.fromisoformat(ts)).total_seconds() < ttl_horas * 3600
    return False


# ── Correção monetária ───────────────────────────────────────────────────────

def _fetch_ibge(tab: int, var: int, n: int = 12) -> dict[str, float]:
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/agregados/{tab}"
        f"/periodos/-{n}/variaveis/{var}?localidades=N1[all]"
    )
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA_IBGE), timeout=30) as r:
        d = json.load(r)
    serie = d[0]["resultados"][0]["series"][0]["serie"]
    out: dict[str, float] = {}
    for periodo, valor in serie.items():
        if len(periodo) != 6:  # "202606"
            continue
        try:
            out[f"{periodo[:4]}-{periodo[4:]}"] = float(valor)
        except (TypeError, ValueError):
            continue  # "...", "-"
    return out


def _fetch_bcb_ultimos(codigo: int, n: int) -> dict[str, float]:
    """Série mensal do BCB (SGS), últimos N pontos. 3 tentativas (o BCB tem WAF
    e às vezes falha/some de forma transitória — mesma defesa já usada em
    `_fetch_bcb_sm`). O endpoint por intervalo de datas (`dataInicial`/
    `dataFinal`) desse mesmo SGS se mostrou instável (bloqueado pelo WAF em
    testes) mesmo com parâmetros válidos — por isso `ultimos/N` é preferido
    aqui, igual ao salário mínimo; N grande (>~20) também já deu 400 Bad
    Request nessa série, então o chamador deve pedir só o necessário."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{n}?formato=json"
    d = None
    ultimo_erro = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA_BCB), timeout=30) as r:
                d = json.load(r)
            if isinstance(d, list) and d:
                break
            ultimo_erro = ValueError("resposta inesperada")
            d = None
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            d = None
    if d is None:
        raise ultimo_erro or ValueError("BCB sem dados")
    out: dict[str, float] = {}
    for p in d:
        data = p.get("data", "")  # "DD/MM/AAAA"
        if len(data) != 10:
            continue
        try:
            out[f"{data[6:]}-{data[3:5]}"] = float(p["valor"])
        except (TypeError, ValueError):
            continue
    return out


def _fetch_selic(n: int = 14) -> dict[str, float]:
    """SELIC acumulada no mês — só os últimos `n` meses (a base histórica desde
    12/2021 já está na tabela estática de indices_cm.SELIC; aqui só se busca o
    que pode ter mudado/saído desde a última atualização, igual ao INPC/IPCA-E)."""
    return _fetch_bcb_ultimos(_SGS_SELIC, n)


def atualizar_cm(force: bool = False) -> dict:
    cache = _ler(_CACHE_CM)
    if not force and _fresco(cache, _TTL_CM_HORAS):
        return {"ok": True, "skip": "cache fresco", "ultimo": _ultimo_cm(cache)}
    novo = {
        "INPC": dict(cache.get("INPC") or {}),
        "IPCA_E": dict(cache.get("IPCA_E") or {}),
        "SELIC": dict(cache.get("SELIC") or {}),
    }
    erros: list[str] = []
    for chave, (tab, var) in _SERIES_CM.items():
        try:
            novo[chave].update(_fetch_ibge(tab, var))
        except Exception as e:  # noqa: BLE001
            erros.append(f"{chave}: {type(e).__name__}")
    try:
        novo["SELIC"].update(_fetch_selic())
    except Exception as e:  # noqa: BLE001
        erros.append(f"SELIC: {type(e).__name__}")
    if erros and not (novo["INPC"] or novo["IPCA_E"] or novo["SELIC"]):
        return {"ok": False, "erro": "; ".join(erros), "ultimo": _ultimo_cm(cache)}
    novo["atualizado_em"] = _dt.datetime.now().isoformat(timespec="minutes")
    with contextlib.suppress(Exception):
        _CACHE_CM.write_text(json.dumps(novo, ensure_ascii=False, indent=2), encoding="utf-8")
    _remesclar("indices_cm", "_carregar_indices_extra")
    return {"ok": True, "atualizado": True, "ultimo": _ultimo_cm(novo), "erros": erros}


def _ultimo_cm(cache: dict) -> dict:
    out = {}
    for chave in ("INPC", "IPCA_E", "SELIC"):
        meses = sorted((cache.get(chave) or {}).keys())
        out[chave] = meses[-1] if meses else None
    return out


# ── Salário mínimo ───────────────────────────────────────────────────────────

_IPEA_SM_COD = "MTE12_SALMIN12"  # IPEADATA: salário mínimo nominal mensal (R$)


def _reduzir_vigencias(pontos: list[tuple[int, int, float]], anos_recentes: int = 4) -> dict[str, float]:
    """De uma série mensal (ano, mês, valor), extrai só os pontos de MUDANÇA
    (vigências), limitando aos últimos `anos_recentes` anos (o histórico antigo
    já está na tabela-base). Retorna {'AAAA-MM': valor}."""
    corte = _dt.date.today().year - anos_recentes
    vig: dict[str, float] = {}
    ant = None
    for ano, mes, val in sorted(pontos):
        if val != ant:
            if ano >= corte:
                vig[f"{ano}-{mes:02d}"] = val
            ant = val
    return vig


def _fetch_ipeadata_sm() -> dict[str, float]:
    """Salário mínimo via IPEADATA (OData JSON). Fonte primária — estável."""
    import urllib.parse

    orderby = urllib.parse.quote("VALDATA desc")
    url = (
        f"http://www.ipeadata.gov.br/api/odata4/ValoresSerie(SERCODIGO='{_IPEA_SM_COD}')"
        f"?$orderby={orderby}"
    )
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA_BCB), timeout=40) as r:
        d = json.load(r)
    pontos: list[tuple[int, int, float]] = []
    for v in d.get("value", []):
        data = v.get("VALDATA") or ""   # ISO "AAAA-MM-DD..."
        val = v.get("VALVALOR")
        if len(data) < 7 or val is None:
            continue
        try:
            pontos.append((int(data[:4]), int(data[5:7]), round(float(val), 2)))
        except (TypeError, ValueError):
            continue
    return _reduzir_vigencias(pontos)


def _fetch_bcb_sm(n: int = 36) -> dict[str, float]:
    """Salário mínimo via BCB SGS 1619 — fallback (o BCB tem WAF e às vezes falha)."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{_SGS_SALARIO_MINIMO}/dados/ultimos/{n}?formato=json"
    d = None
    ultimo_erro = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA_BCB), timeout=30) as r:
                d = json.load(r)
            if isinstance(d, list) and d:
                break
            ultimo_erro = ValueError("resposta inesperada")
            d = None
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            d = None
    if d is None:
        raise ultimo_erro or ValueError("BCB sem dados")
    pontos: list[tuple[int, int, float]] = []
    for p in d:
        data = p.get("data", "")  # "DD/MM/AAAA"
        if len(data) != 10:
            continue
        try:
            pontos.append((int(data[6:]), int(data[3:5]), round(float(p["valor"]), 2)))
        except (TypeError, ValueError):
            continue
    return _reduzir_vigencias(pontos)


# Fallbacks HTML (última linha de defesa, se IPEADATA e BCB falharem). São
# tabelas web; o parser extrai (vigência DD/MM/AAAA -> R$ valor) e, na falta de
# datas, cai para (ano -> R$ valor). A proteção contra dados imperfeitos está no
# salario_minimo.py: o cache NUNCA sobrescreve o histórico validado da tabela-base.
# debit.com.br fica de fora: renderiza a tabela via JavaScript, então o HTML
# estático não traz os valores (só um navegador headless pegaria — exagero para
# um fallback de última linha). contabeis (com data de vigência) e ieprev cobrem.
_HTML_SM_SOURCES = [
    ("contabeis", "https://www.contabeis.com.br/tabelas/salario-minimo/"),
    ("ieprev", "https://www.ieprev.com.br/ferramentas/tabela-dos-salarios-minimos"),
]
_MONEY_BR = r"R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})"


def _valor_br(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def _fetch_html_sm(url: str) -> dict[str, float]:
    """Extrai vigências do salário mínimo de uma tabela HTML. Prioriza pares
    'DD/MM/AAAA … R$ valor' (com data de vigência); se não achar o bastante, cai
    para 'ano … R$ valor'. Sanidade: só valores no intervalo de um salário mínimo."""
    import re

    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA_BCB), timeout=40) as r:
        html = r.read().decode("utf-8", "replace")
    txt = re.sub(r"\s+", " ", html)
    pontos: list[tuple[int, int, float]] = []
    for m in re.finditer(r"(\d{2})/(\d{2})/(\d{4}).{0,80}?" + _MONEY_BR, txt):
        _, mm, yy, val = m.groups()
        pontos.append((int(yy), int(mm), _valor_br(val)))
    if len({p[0] for p in pontos}) < 3:  # tabela sem datas — usa ano
        for m in re.finditer(r"\b(20\d{2})\b.{0,80}?" + _MONEY_BR, txt):
            pontos.append((int(m.group(1)), 1, _valor_br(m.group(2))))
    pontos = [(a, mth, v) for a, mth, v in pontos if 500 <= v <= 6000]
    return _reduzir_vigencias(pontos)


def atualizar_sm(force: bool = False) -> dict:
    cache = _ler(_CACHE_SM)
    if not force and _fresco(cache, _TTL_SM_HORAS):
        return {"ok": True, "skip": "cache fresco", "ultimo": _ultimo_sm(cache)}
    vig = dict(cache.get("SALARIO_MINIMO") or {})
    fonte = None
    erros: list[str] = []
    # Ordem de preferência: APIs JSON (IPEADATA, BCB) e, por último, tabelas HTML.
    fontes = [("IPEADATA", _fetch_ipeadata_sm), ("BCB", _fetch_bcb_sm)]
    fontes += [(nome, (lambda u=u: _fetch_html_sm(u))) for nome, u in _HTML_SM_SOURCES]
    for nome, fn in fontes:
        try:
            novos = fn()
            if novos:
                vig.update(novos)
                fonte = nome
                break
        except Exception as e:  # noqa: BLE001
            erros.append(f"{nome}: {type(e).__name__}")
    if not vig:
        return {"ok": False, "erro": "; ".join(erros) or "sem fontes", "ultimo": None}
    novo = {
        "SALARIO_MINIMO": vig,
        "fonte": fonte,
        "atualizado_em": _dt.datetime.now().isoformat(timespec="minutes"),
    }
    with contextlib.suppress(Exception):
        _CACHE_SM.write_text(json.dumps(novo, ensure_ascii=False, indent=2), encoding="utf-8")
    _remesclar("salario_minimo", "_carregar_extra")
    return {"ok": True, "atualizado": True, "fonte": fonte, "ultimo": _ultimo_sm(novo), "erros": erros}


def _ultimo_sm(cache: dict) -> str | None:
    meses = sorted((cache.get("SALARIO_MINIMO") or {}).keys())
    return meses[-1] if meses else None


# ── Comum ────────────────────────────────────────────────────────────────────

def _remesclar(modulo: str, funcao: str) -> None:
    """Reflete o cache recém-gravado no módulo já carregado neste processo."""
    with contextlib.suppress(Exception):
        if str(VALOR_CAUSA_DIR) not in sys.path:
            sys.path.insert(0, str(VALOR_CAUSA_DIR))
        mod = __import__(modulo)
        getattr(mod, funcao)()


def atualizar(force: bool = False) -> dict:
    """Atualiza CM (IBGE) e salário mínimo (BCB). Chamada no início de cada
    cálculo (TTL-gated) e pelo endpoint de refresh manual. Best-effort."""
    return {"cm": atualizar_cm(force=force), "salario_minimo": atualizar_sm(force=force)}
