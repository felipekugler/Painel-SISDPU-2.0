"""Leitor autônomo das intimações/prazos do Painel do Procurador (e-proc).

Percorre TODAS as categorias da tabela "Citações/Intimações" (JFPR) — prazo em
aberto, pendentes, urgentes, art. 334, decursos etc. Para cada categoria com
quantidade > 0, CLICA no link da própria página (nunca monta URL — os hashes do
eproc são assinados/uso único), espera a listagem ESTABILIZAR e extrai as linhas.

Fluxo por categoria:
  1. clica o link da categoria no painel (o eproc abre a listagem em janela
     nomeada `painel_remotoPR`);
  2. espera a página estabilizar (linhas com nº CNJ + ícones de lembrete
     carregados — senão o PAJ, que vem do lembrete, sai vazio);
  3. lê a `infraTable` e extrai os campos combinados com o usuário.

Regras de negócio (usuário 2026-07-24):
  - Multi-parte: cada parte gera um evento de intimação separado (1 linha/parte).
    Agrupamos por PROCESSO num único caso; `n_prazos` = nº de linhas.
  - PAJ: vem do texto do LEMBRETE. Se vier sem a unidade (AAAA/NNNNN), completa
    com 029 (Curitiba) → AAAA/029-NNNNN.
  - Texto completo do lembrete preservado.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import datetime as dt
from pathlib import Path

UNIDADE_PADRAO = "029"  # Curitiba — assumida quando o PAJ vem sem a unidade

# --- Estimador de início/final do prazo (quando ainda não abertos) ----------
# Padrão identificado analisando os casos REAIS de "prazo em aberto" (2026-08-02,
# 11 casos, 100% de acerto no início):
#   1) "Ciência tácita" (Lei 11.419/2006, art. 5º §3º): D+10 dias CORRIDOS a
#      partir do envio/expedição da intimação; se cair em dia não útil, desloca
#      pro próximo dia útil.
#   2) O PRAZO começa a contar no próximo dia útil seguinte a essa ciência.
# Para o FINAL, aplicamos a regra padrão do CPC (art. 219/224): conta os dias
# úteis declarados a partir do dia seguinte ao início (exclui início, inclui
# final), pulando fins de semana e feriados nacionais. Essa regra bateu exato
# nos casos de 10/15/30 dias observados; nos de 5 dias o final real caiu 1 dia
# útil ANTES do previsto (possível contagem inclusiva nesse tipo de prazo curto
# — amostra pequena, causa não confirmada). Por isso o resultado é rotulado
# como ESTIMATIVA (badge cinza na UI), não como data oficial.


def _pascoa(ano: int) -> dt.date:
    """Domingo de Páscoa (algoritmo de Gauss/Meeus)."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    mes = (h + L - 7 * m + 114) // 31
    dia = ((h + L - 7 * m + 114) % 31) + 1
    return dt.date(ano, mes, dia)


_FERIADOS_CACHE: dict[int, frozenset] = {}


def _feriados_nacionais(ano: int) -> frozenset:
    if ano in _FERIADOS_CACHE:
        return _FERIADOS_CACHE[ano]
    fixos = {
        dt.date(ano, 1, 1), dt.date(ano, 4, 21), dt.date(ano, 5, 1),
        dt.date(ano, 9, 7), dt.date(ano, 10, 12), dt.date(ano, 11, 2),
        dt.date(ano, 11, 15), dt.date(ano, 11, 20), dt.date(ano, 12, 25),
    }
    pascoa = _pascoa(ano)
    moveis = {
        pascoa - dt.timedelta(days=48),  # Segunda de Carnaval
        pascoa - dt.timedelta(days=47),  # Terça de Carnaval
        pascoa - dt.timedelta(days=2),   # Sexta-feira Santa
        pascoa + dt.timedelta(days=60),  # Corpus Christi
    }
    resultado = frozenset(fixos | moveis)
    _FERIADOS_CACHE[ano] = resultado
    return resultado


def _eh_dia_util(d: dt.date) -> bool:
    if d.weekday() >= 5:  # 5=sáb, 6=dom
        return False
    return d not in _feriados_nacionais(d.year)


def _proximo_dia_util(d: dt.date) -> dt.date:
    while not _eh_dia_util(d):
        d += dt.timedelta(days=1)
    return d


def _parse_data_br(s: str) -> dt.date | None:
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def estimar_inicio_prazo(data_envio: dt.date) -> dt.date:
    """D+10 dias corridos (ciência tácita) + próximo dia útil seguinte."""
    ciencia = _proximo_dia_util(data_envio + dt.timedelta(days=10))
    return _proximo_dia_util(ciencia + dt.timedelta(days=1))


def estimar_final_prazo(inicio: dt.date, dias: int) -> dt.date:
    """Conta `dias` dias úteis a partir do dia seguinte ao início (exclui o
    início, inclui o final), pulando fins de semana e feriados nacionais."""
    cur = inicio
    restantes = dias
    while restantes > 0:
        cur += dt.timedelta(days=1)
        if _eh_dia_util(cur):
            restantes -= 1
    return cur

_STORE = Path(__file__).resolve().parents[1].parent / "dados" / "_eproc_intimacoes.json"

# Categorias da tabela "Citações/Intimações" (na ordem em que o eproc as exibe).
# `acao` = valor do parâmetro acao= no link; `urgente`: True/False filtra o param
# urgente=true (None = não se aplica); `exato`: acao NÃO pode ter sufixo "_...".
CATEGORIAS = [
    {"chave": "prazo_aberto", "label": "Processos com prazo em aberto",
     "acao": "citacao_intimacao_prazo_aberto_listar", "urgente": False, "exato": False},
    {"chave": "prazo_aberto_urgente", "label": "Processos com prazo em aberto — Urgentes",
     "acao": "citacao_intimacao_prazo_aberto_listar", "urgente": True, "exato": False},
    {"chave": "pendente_urgente", "label": "Processos pendentes de citação/intimação — Urgentes",
     "acao": "citacao_intimacao_pendente_listar_urgente", "urgente": None, "exato": False},
    {"chave": "pendente", "label": "Processos pendentes de citação/intimação",
     "acao": "citacao_intimacao_pendente_listar", "urgente": None, "exato": True},
    {"chave": "pendente_acordo", "label": "Processos pendentes de intimação de homologação de acordo",
     "acao": "citacao_intimacao_pendente_listar_acordo", "urgente": None, "exato": False},
    {"chave": "art334", "label": "Processos pendentes de citação — art. 334 CPC",
     "acao": "citacao_art334_pendente_listar", "urgente": None, "exato": False},
    {"chave": "decurso", "label": "Decursos de prazo nos últimos 30 dias",
     "acao": "citacao_intimacao_decurso_prazo_listar", "urgente": None, "exato": False},
]


def _casa(url_ou_href: str, cat: dict) -> bool:
    """True se a URL/href pertence à categoria `cat` (link JFPR direto, não TRF4)."""
    u = (url_ou_href or "").lower()
    if "controlador_links" in u or "secao=" in u:
        return False
    acao = cat["acao"].lower()
    if f"acao={acao}" not in u:
        return False
    if cat.get("exato") and f"acao={acao}_" in u:
        return False
    if cat.get("urgente") is True and "urgente=true" not in u:
        return False
    if cat.get("urgente") is False and "urgente=true" in u:
        return False
    return True


# ---- JavaScript injetado ----------------------------------------------------

# Lê a tabela-resumo "Citações/Intimações" (JFPR): rótulo + quantidade + href.
_JS_RESUMO = r"""()=>{
  const norm=s=>(s||'').replace(/\s+/g,' ').trim();
  const tabs=[...document.querySelectorAll('table.infraTable')];
  let alvo=null;
  for(const t of tabs){
    const head=t.querySelector('tr');
    const ht=head?norm(head.innerText).toLowerCase():'';
    if(!(ht.includes('tipo') && ht.includes('quantidade'))) continue;
    const link=t.querySelector('a[href*="citacao_intimacao"], a[href*="citacao_art334"]');
    if(link && !(link.getAttribute('href')||'').includes('controlador_links')){ alvo=t; break; }
  }
  if(!alvo) return [];
  const out=[];
  for(const tr of alvo.querySelectorAll('tr')){
    const tds=[...tr.querySelectorAll('td')];
    if(tds.length<2) continue;
    const label=norm(tds[0].innerText);
    if(!label || /^tipo$/i.test(label)) continue;
    const a=tr.querySelector('a[href]');
    out.push({label, quantidade:norm(tds[tds.length-1].innerText), href:a?a.getAttribute('href'):''});
  }
  return out;
}"""

# Clica o link JFPR da categoria (cfg = {acao, urgente, exato}).
_JS_CLICAR_CATEGORIA = r"""(cfg)=>{
  const as=[...document.querySelectorAll('a[href]')];
  const a=as.find(x=>{
    const h=(x.getAttribute('href')||'');
    if(h.includes('controlador_links')||h.includes('secao=')) return false;
    if(!h.includes('acao='+cfg.acao)) return false;
    if(cfg.exato && h.includes('acao='+cfg.acao+'_')) return false;
    if(cfg.urgente===true && !h.includes('urgente=true')) return false;
    if(cfg.urgente===false && h.includes('urgente=true')) return false;
    return true;
  });
  if(a){a.click(); return a.getAttribute('href');} return null;
}"""

# Métricas de estabilização: linhas totais, linhas com CNJ e ícones de lembrete.
_JS_METRICS = r"""()=>({
  rows: document.querySelectorAll('table.infraTable tr').length,
  cnjs: [...document.querySelectorAll('table.infraTable tr')]
          .filter(tr=>/\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/.test(tr.innerText)).length,
  icons: document.querySelectorAll('a[onmouseover*="infraTooltip"]').length
})"""

# Extrai as linhas de dados da maior infraTable. Uma entrada por LINHA (= por parte).
_JS_EXTRAIR = r"""()=>{
  const norm=s=>(s||'').replace(/\s+/g,' ').trim();
  const reCNJ=/\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/;
  const tabs=[...document.querySelectorAll('table.infraTable')];
  let best=null,bn=0; tabs.forEach(t=>{const n=t.querySelectorAll('tr').length; if(n>bn){bn=n;best=t;}});
  if(!best) return [];
  const out=[];
  for(const tr of best.querySelectorAll('tr')){
    const tds=[...tr.querySelectorAll('td')];
    if(tds.length<8) continue;
    const cel1=norm(tds[1].innerText);
    const mcnj=cel1.match(reCNJ); if(!mcnj) continue;
    // As listagens de "Urgentes" têm uma coluna extra "Urgente" (Sim/Não) logo
    // após "Processo", deslocando as demais colunas em +1 (9 tds em vez de 8).
    // Detectado pela CONTAGEM de células (não pelo nome da categoria — mais robusto).
    const off = tds.length >= 9 ? 1 : 0;
    const urgente = off ? norm(tds[2].innerText) : '';
    const juizo=(cel1.match(/Ju.zo:\s*([A-Z0-9]+)/i)||[])[1]||'';
    let lembrete='', lembrete_itens=[];
    const lem=tds[1].querySelector('a[onmouseover*="infraTooltip"]');
    if(lem){
      const oc=lem.getAttribute('onmouseover')||'';
      const m=oc.match(/infraTooltipMostrar\('([\s\S]*?)','Lembretes'/) || oc.match(/infraTooltipMostrar\('([\s\S]*)'/);
      let html=m?m[1]:'';
      const tmp=document.createElement('div'); tmp.innerHTML=html;
      // itens estruturados: cada <tr> do tooltip = [seq, texto] (mais recente no topo)
      tmp.querySelectorAll('tr').forEach(tr=>{
        const cs=[...tr.querySelectorAll('td')];
        if(cs.length>=2){ lembrete_itens.push({seq:norm(cs[0].innerText), texto:norm(cs[1].innerText)}); }
      });
      lembrete=lembrete_itens.map(it=>it.texto).join(' · ');
    }
    let partes=cel1.replace(reCNJ,'').replace(/Ju.zo:\s*[A-Z0-9]+/i,'')
                   .replace(/Cadastrar Lembrete/ig,'').trim();
    // link do processo no eproc (abre a Consulta Processual em aba nova)
    const aProc=tds[1].querySelector('a[href*="processo_selecionar"]')
                || [...tds[1].querySelectorAll('a[href]')].find(a=>reCNJ.test(a.innerText));
    let processo_url=''; try{ if(aProc) processo_url=new URL(aProc.getAttribute('href'), location.href).href; }catch(e){}
    const ev=norm(tds[4+off].innerText);
    // cor de fundo da linha no eproc (semântica: fase processual + tipo de demanda)
    let cor=getComputedStyle(tr).backgroundColor||'';
    if(cor==='rgba(0, 0, 0, 0)'||cor==='transparent'){ const td2=tds[1]; if(td2) cor=getComputedStyle(td2).backgroundColor||''; }
    out.push({
      processo:mcnj[0], juizo, partes, lembrete, lembrete_itens, cor, processo_url, urgente,
      classe:norm(tds[2+off].innerText), assunto:norm(tds[3+off].innerText),
      evento:ev, prazo_dias:((ev.match(/(\d+)\s*dias?/i)||[])[1]||''),
      data_envio:norm(tds[5+off].innerText),
      inicio_prazo:norm(tds[6+off].innerText), final_prazo:norm(tds[7+off].innerText)
    });
  }
  return out;
}"""

_RE_PAJ_COM_UNIDADE = re.compile(r"(\d{4})/(\d{3})-(\d{5})")
_RE_PAJ_SEM_UNIDADE = re.compile(r"(\d{4})/(\d{4,5})(?!\d)")


def _extrair_paj(lembrete: str) -> str:
    if not lembrete:
        return ""
    m = _RE_PAJ_COM_UNIDADE.search(lembrete)
    if m:
        return f"{m.group(1)}/{m.group(2)}-{m.group(3)}"
    m = _RE_PAJ_SEM_UNIDADE.search(lembrete)
    if m:
        return f"{m.group(1)}/{UNIDADE_PADRAO}-{m.group(2).zfill(5)}"
    return ""


def _limpar_prazo(v: str) -> str:
    v = (v or "").strip()
    if not v or "abrir prazo" in v.lower():
        return ""
    m = re.search(r"\d{2}/\d{2}/\d{4}", v)
    return m.group(0) if m else v


def agrupar(raws: list[dict]) -> list[dict]:
    """Agrupa linhas (uma por parte) em CASOS por processo."""
    casos: dict[str, dict] = {}
    for r in raws:
        proc = r["processo"]
        c = casos.get(proc)
        if c is None:
            c = {
                "processo": proc, "juizo": r.get("juizo", ""),
                "classe": r.get("classe", ""), "assunto": r.get("assunto", ""),
                "partes": r.get("partes", ""), "lembrete": r.get("lembrete", ""),
                "lembrete_itens": r.get("lembrete_itens", []),
                "paj": _extrair_paj(r.get("lembrete", "")),
                "processo_url": r.get("processo_url", ""),  # link p/ abrir o processo no eproc
                "cor": r.get("cor", ""),  # cor da linha no eproc (fase/tipo de demanda)
                "urgente": r.get("urgente", ""),  # coluna extra ("Sim") nas listagens de urgentes
                "prazos": [],
            }
            casos[proc] = c
        inicio_real = _limpar_prazo(r.get("inicio_prazo", ""))
        item = {
            "parte": r.get("partes", ""),
            "evento": r.get("evento", ""),
            "prazo_dias": r.get("prazo_dias", ""),
            "data_envio": r.get("data_envio", ""),
            "inicio_prazo": inicio_real,
            "final_prazo": _limpar_prazo(r.get("final_prazo", "")),
            "inicio_estimado": "",
            "final_estimado": "",
        }
        # Prazo ainda não aberto ("Abrir Prazo"): estima início/final (ver nota no
        # topo do arquivo). Só estima quando o real está ausente — nunca sobrepõe
        # uma data oficial já aberta no eproc.
        if not inicio_real:
            envio_d = _parse_data_br(item["data_envio"])
            if envio_d:
                inicio_est = estimar_inicio_prazo(envio_d)
                item["inicio_estimado"] = inicio_est.strftime("%d/%m/%Y")
                if item["prazo_dias"].isdigit():
                    final_est = estimar_final_prazo(inicio_est, int(item["prazo_dias"]))
                    item["final_estimado"] = final_est.strftime("%d/%m/%Y")
        c["prazos"].append(item)
    out = []
    for c in casos.values():
        c["n_prazos"] = len(c["prazos"])
        dias = [int(p["prazo_dias"]) for p in c["prazos"] if p["prazo_dias"].isdigit()]
        c["prazo_dias_min"] = min(dias) if dias else None
        out.append(c)
    def _chave(c):
        d = c["prazos"][0]["data_envio"] if c["prazos"] else ""
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", d)
        return (m.group(3) + m.group(2) + m.group(1)) if m else ""
    out.sort(key=_chave, reverse=True)
    return out


# ---- armazenamento ----------------------------------------------------------

# Campos LOCAIS (não vêm do eproc) que devem sobreviver às sincronizações.
_CAMPOS_PRESERVADOS = ("primeira_vez", "tratado", "obs")


def salvar(resultado: dict) -> dict:
    """Mescla o resultado novo com o já sincronizado (merge diferencial), preservando
    metadados locais, e grava. Identidade de um caso = PROCESSO (global à caixa) —
    assim, um processo que só muda de categoria (ex.: pendente → prazo aberto)
    continua "mantido", e só conta como "saiu" quem deixa a caixa por completo.

    Retorna o payload gravado (inclui `diff` e `saidas`)."""
    anterior = carregar()
    idx_ant: dict[str, dict] = {}
    cat_ant: dict[str, str] = {}
    for chave, casos in (anterior.get("secoes") or {}).items():
        for c in casos:
            idx_ant[c.get("processo")] = c
            cat_ant[c.get("processo")] = chave

    agora = dt.datetime.now().isoformat(timespec="seconds")
    novas = mantidas = 0
    procs_atuais: set[str] = set()
    secoes = resultado.get("secoes", {}) or {}

    for chave, casos in secoes.items():
        for c in casos:
            proc = c.get("processo")
            procs_atuais.add(proc)
            ant = idx_ant.get(proc)
            if ant:
                c["primeira_vez"] = ant.get("primeira_vez", agora)
                for campo in _CAMPOS_PRESERVADOS:
                    if campo != "primeira_vez" and campo in ant:
                        c[campo] = ant[campo]
                # marca "novo" quando muda de categoria (nova situação processual)
                c["novo"] = False
                c["mudou_categoria"] = cat_ant.get(proc) not in (None, chave)
                mantidas += 1
            else:
                c["primeira_vez"] = agora
                c["novo"] = True
                c["mudou_categoria"] = False
                novas += 1

    # saíram da caixa: processo estava antes e não está em NENHUMA seção agora
    saidas = []
    for proc, ant in idx_ant.items():
        if proc not in procs_atuais:
            saidas.append({
                "chave": cat_ant.get(proc, ""), "processo": proc,
                "paj": ant.get("paj", ""), "partes": ant.get("partes", ""),
                "primeira_vez": ant.get("primeira_vez", ""), "saiu_em": agora,
            })

    payload = {
        "sincronizado_em": agora,
        "resumo": resultado.get("resumo", []),
        "secoes": secoes,
        "diff": {"novas": novas, "mantidas": mantidas, "removidas": len(saidas)},
        "saidas": saidas,
    }
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def carregar() -> dict:
    if not _STORE.exists():
        return {"sincronizado_em": "", "resumo": [], "secoes": {}, "diff": {}, "saidas": []}
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"sincronizado_em": "", "resumo": [], "secoes": {}, "diff": {}, "saidas": []}


# ---- leitura ----------------------------------------------------------------

async def _esperar_listagem(ctx, painel, cat: dict):
    """Espera a listagem da categoria abrir e estabilizar (CNJs presentes + ícones
    de lembrete carregados, ou CNJs estáveis quando não há lembretes)."""
    ultimo, estavel, melhor = -1, 0, None
    for _ in range(40):  # ~20s
        await painel.wait_for_timeout(500)
        cand = None
        for p in ctx.pages:
            with contextlib.suppress(Exception):
                if _casa(p.url or "", cat):
                    cand = p
        if cand is None:
            continue
        melhor = cand
        met = {}
        with contextlib.suppress(Exception):
            met = await cand.evaluate(_JS_METRICS)
        cnjs, icons = met.get("cnjs", 0), met.get("icons", 0)
        estavel = estavel + 1 if (cnjs > 0 and cnjs == ultimo) else 0
        ultimo = cnjs
        if cnjs > 0 and icons > 0 and estavel >= 1:
            return cand
        if cnjs > 0 and estavel >= 6:  # ~3s estável e sem ícones → categoria sem lembretes
            return cand
    return melhor


async def _ler_categoria(ctx, painel, cat: dict) -> list[dict]:
    clicou = None
    with contextlib.suppress(Exception):
        clicou = await painel.evaluate(_JS_CLICAR_CATEGORIA,
                                       {"acao": cat["acao"], "urgente": cat["urgente"], "exato": cat["exato"]})
    if not clicou:
        return []
    lista = await _esperar_listagem(ctx, painel, cat)
    if lista is None:
        return []
    raws = []
    for _ in range(4):
        with contextlib.suppress(Exception):
            raws = await lista.evaluate(_JS_EXTRAIR)
        if raws:
            break
        await lista.wait_for_timeout(1000)
    return agrupar(raws or [])


def _int(s: str) -> int:
    try:
        return int(re.sub(r"\D", "", s or "") or 0)
    except Exception:  # noqa: BLE001
        return 0


async def ler_todos() -> dict:
    """Lê TODAS as categorias de Citações/Intimações (JFPR) com quantidade > 0.

    Retorna {ok, resumo:[{chave,label,quantidade}], secoes:{chave:[casos]}}."""
    from ingestao import eproc_client as ec

    browser, page = await ec._connect()
    ctx = page.context
    try:
        painel = None
        for p in ctx.pages:
            with contextlib.suppress(Exception):
                if "painel_advogado_listar" in (p.url or ""):
                    painel = p
                    break
        if painel is None:
            painel = page
            with contextlib.suppress(Exception):
                await painel.goto(ec.EPROC_HOME + "controlador.php?acao=painel_advogado_listar",
                                  wait_until="domcontentloaded", timeout=60000)
                await painel.wait_for_timeout(2000)

        if not ec._status_dict(painel.url)["logado"]:
            return {"ok": False, "erro": "Sessão do e-proc inativa. Use 'Abrir e-proc / Login' antes de sincronizar."}

        painel.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        # 1) tabela-resumo (quantidades por categoria)
        resumo_raw = []
        with contextlib.suppress(Exception):
            resumo_raw = await painel.evaluate(_JS_RESUMO)

        resumo = []
        for cat in CATEGORIAS:
            qtd = 0
            for r in resumo_raw:
                if _casa(r.get("href", ""), cat):
                    qtd = _int(r.get("quantidade", "0"))
                    break
            resumo.append({"chave": cat["chave"], "label": cat["label"], "quantidade": qtd})

        # 2) lê cada categoria com quantidade > 0
        secoes: dict[str, list] = {}
        for cat in CATEGORIAS:
            q = next((x["quantidade"] for x in resumo if x["chave"] == cat["chave"]), 0)
            if q <= 0:
                continue
            casos = await _ler_categoria(ctx, painel, cat)
            if casos:
                secoes[cat["chave"]] = casos
            # volta ao painel para o próximo clique ficar disponível
            with contextlib.suppress(Exception):
                await painel.bring_to_front()

        return {"ok": True, "resumo": resumo, "secoes": secoes}
    finally:
        with contextlib.suppress(Exception):
            await browser.close()
