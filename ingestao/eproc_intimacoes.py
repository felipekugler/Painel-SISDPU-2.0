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

    # Uma sincronização REAL sempre vence sobre a baixa local otimista (ver
    # `services.prazo_baixa_service`) — descarta a baixa de todo processo
    # tocado por este sync (continuou na caixa ou saiu dela): se o prazo
    # realmente fechou, nem aparece mais no scrape; se por engano ainda
    # estiver aberto, volta a aparecer — nunca fica escondido pra sempre.
    from services import prazo_baixa_service

    for proc in set(idx_ant.keys()) | procs_atuais:
        prazo_baixa_service.limpar_processo(proc)

    return payload


def carregar() -> dict:
    if not _STORE.exists():
        return {"sincronizado_em": "", "resumo": [], "secoes": {}, "diff": {}, "saidas": []}
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"sincronizado_em": "", "resumo": [], "secoes": {}, "diff": {}, "saidas": []}


def carregar_visivel() -> dict:
    """Como `carregar()`, mas com a baixa LOCAL de prazos já cumpridos dentro
    do painel aplicada (ver `services.prazo_baixa_service`) — usar em toda
    tela/cálculo que reflete "o que ainda está pendente" (tabela de Prazos,
    Agenda, badge de intimação pendente, checkbox de "Ações no Eproc",
    página Intimações, bloqueio de Gerenciamento de Processo por prazo
    residual).

    NUNCA usar dentro de `salvar()` (precisa do estado CRU pra decidir o que
    preservar/o que saiu da caixa) nem em `purgar_casos_resolvidos()` (decide
    o que apagar do disco — só uma sincronização REAL, nunca uma baixa
    otimista local, pode confirmar que um ato saiu de vez da caixa)."""
    from services import prazo_baixa_service

    dados = carregar()
    secoes = prazo_baixa_service.aplicar_filtro(dados.get("secoes") or {})
    return {**dados, "secoes": secoes}


def buscar_lembretes(numero: str) -> dict:
    """Lembretes NATIVOS do e-proc (ícone/tooltip da listagem de intimações) de
    um processo, lidos do cache já sincronizado por "Sincronizar intimações"
    (aba Prazos) — NÃO é uma leitura ao vivo por NPU avulso: essa tela de
    lembretes só é vista via navegador na listagem de citações/intimações, o
    MNI/scraper do processo (usado pela página "Consultar processo") não a
    expõe. Por isso só acha algo se o processo já passou por aquela sync.

    Retorna {"encontrado": bool, "lembrete_itens": [{seq,texto}, ...],
    "sincronizado_em": "..."}."""
    dados = carregar()
    alvo = re.sub(r"\D", "", numero or "")
    if alvo:
        for casos in (dados.get("secoes") or {}).values():
            for c in casos:
                if re.sub(r"\D", "", c.get("processo") or "") == alvo:
                    return {
                        "encontrado": True,
                        "lembrete_itens": c.get("lembrete_itens") or [],
                        "sincronizado_em": dados.get("sincronizado_em") or "",
                    }
    return {"encontrado": False, "lembrete_itens": [], "sincronizado_em": dados.get("sincronizado_em") or ""}


def paj_do_processo(numero: str) -> dict:
    """PAJ associado a este processo, lido do cache já sincronizado pela aba
    Prazos (mesmo cache/critério de `buscar_lembretes` — extraído do texto do
    LEMBRETE, ver `_extrair_paj`). Usado pelo card "PAJ associado" da página
    "Consultar processo". Só acha algo se o processo já tiver passado por
    aquela sincronização E tiver lembrete com o PAJ no texto.

    Retorna {"encontrado": bool, "paj": "AAAA/UUU-NNNNN" | ""}."""
    dados = carregar()
    alvo = re.sub(r"\D", "", numero or "")
    if alvo:
        for casos in (dados.get("secoes") or {}).values():
            for c in casos:
                if re.sub(r"\D", "", c.get("processo") or "") == alvo:
                    paj = c.get("paj") or ""
                    if paj:
                        return {"encontrado": True, "paj": paj}
    return {"encontrado": False, "paj": ""}


def caso_e_prazos_mesclados(numero: str) -> dict:
    """Mescla os PRAZOS de um processo entre TODAS as categorias do Painel do
    Procurador em que ele aparece — dedupe por (evento, data_envio,
    prazo_dias). Necessário porque uma mesma intimação pode estar listada em
    mais de uma categoria SIMULTANEAMENTE (ex.: "pendente" e "pendente_urgente"
    ao mesmo tempo — "urgente" é uma VISÃO FILTRADA de "pendente", não uma
    categoria mutuamente exclusiva no e-proc). Sem esta mescla, pegar só a
    PRIMEIRA categoria encontrada (comportamento antigo) tanto podia DUPLICAR
    (se o chamador somasse todas as categorias, como `acoes_eproc_service.
    listar_prazos` fazia) quanto OMITIR prazos reais (se dependesse de qual
    categoria o dict de secoes lista primeiro) — bug real confirmado 2026-08-13
    no PAJ 2026/029-01734 (3 prazos mostrados pra fechamento quando só 2
    existiam de fato, a Requisição de Pagamento duplicada entre "pendente" e
    "pendente_urgente").

    Retorna {"encontrado": bool, "caso": {processo,juizo,classe,assunto,
    partes,urgente,...} | None, "prazos": [{parte,evento,prazo_dias,
    data_envio,inicio_prazo,final_prazo,...}, ...]}. `caso` traz os campos de
    NÍVEL DE PROCESSO (mesmo valor em qualquer categoria em que apareça,
    exceto `urgente` — que vira True se QUALQUER categoria/caso encontrado
    indicar urgência).

    Usa a leitura VISÍVEL (`carregar_visivel`) — prazo já baixado localmente
    (ver `services.prazo_baixa_service`) não entra aqui, o que também é o
    que faz o bloqueio de Gerenciamento de Processo por prazo residual
    (`acoes_eproc_service._prazo_residual_no_meu_painel`, que usa esta
    função via `prazos_processo`) liberar sozinho assim que o(s) prazo(s)
    forem fechados dentro do painel, sem esperar nova sincronização."""
    dados = carregar_visivel()
    alvo = re.sub(r"\D", "", numero or "")
    if not alvo:
        return {"encontrado": False, "caso": None, "prazos": []}
    caso_repr: dict | None = None
    urgente = False
    # Conta ocorrências da chave (evento+data_envio+prazo_dias) EM CADA
    # categoria separadamente, e fica com o MAIOR count entre elas — nunca
    # um dedup fixo pra 1 (isso escondia intimações SIMULTÂNEAS reais com
    # texto idêntico dentro da MESMA categoria — o Painel do Procurador só
    # mostra o 1º autor do processo, então 2+ intimações genuinamente
    # distintas podem ser byte-a-byte idênticas, ver nota em `prazos.html:
    # _expandirCaso`) nem a SOMA entre categorias (dobraria uma intimação
    # real só por ela estar mirrorada em 2 categorias, ex. "pendente" +
    # "pendente_urgente" — o próprio motivo desta mescla existir, ver acima).
    # Bug real confirmado 2026-09-23 (PAJ 2023/029-03488, processo
    # 5012327-49.2024.4.04.7000): 3 intimações reais, 2 com chave idêntica
    # na MESMA categoria "prazo_aberto" — o dedup antigo reduzia pra 2.
    melhor_amostra: dict[tuple, dict] = {}
    melhor_contagem: dict[tuple, int] = {}
    for categoria, casos in (dados.get("secoes") or {}).items():
        for c in casos:
            if re.sub(r"\D", "", c.get("processo") or "") != alvo:
                continue
            if caso_repr is None:
                caso_repr = c
            if "urgente" in categoria or c.get("urgente"):
                urgente = True
            contagem_aqui: dict[tuple, int] = {}
            for p in c.get("prazos") or []:
                chave = (p.get("evento", ""), p.get("data_envio", ""), str(p.get("prazo_dias") or ""))
                contagem_aqui[chave] = contagem_aqui.get(chave, 0) + 1
                melhor_amostra.setdefault(chave, p)
            for chave, n in contagem_aqui.items():
                if n > melhor_contagem.get(chave, 0):
                    melhor_contagem[chave] = n
    if caso_repr is None:
        return {"encontrado": False, "caso": None, "prazos": []}
    prazos: list[dict] = []
    for chave, n in melhor_contagem.items():
        prazos.extend([melhor_amostra[chave]] * n)
    return {"encontrado": True, "caso": {**caso_repr, "urgente": urgente}, "prazos": prazos}


def prazos_processo(numero: str) -> dict:
    """Prazos do MEU Painel do Procurador (cache já sincronizado) para este
    NPU — usado pra confirmar quais intimações da triagem MCP (que lê o
    processo INTEIRO, de TODAS as partes) são efetivamente da DPU.

    Não usa casamento por NOME de propósito: o e-proc roteia o Painel do
    Procurador por PROCURADOR REGISTRADO, não por nome — isso já resolve
    corretamente representante legal e litisconsórcio ativo (todas as partes
    que a DPU representa aparecem aqui, sem heurística nenhuma), e evita
    omissão por divergência de nome cadastrado entre SISDPU e e-proc.

    Retorna {"encontrado": bool, "prazos": [{parte,evento,prazo_dias,
    data_envio,inicio_prazo,final_prazo,...}, ...], "sincronizado_em": "..."}
    — `prazos` já vem MESCLADO/deduplicado entre categorias (ver
    `caso_e_prazos_mesclados`). `encontrado=False` (processo não sincronizado
    ainda, ou sem prazo atual no Painel) — o chamador deve tratar como "sem
    informação" e NÃO filtrar (fail-open: melhor mostrar demais do que omitir
    uma intimação real)."""
    dados = carregar()
    m = caso_e_prazos_mesclados(numero)
    return {
        "encontrado": m["encontrado"],
        "prazos": m["prazos"],
        "sincronizado_em": dados.get("sincronizado_em") or "",
    }


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
    from ingestao import eproc_client as ec

    # Garante que `painel` ainda esteja na tela-resumo antes de clicar. O link
    # da categoria tem `target="painel_remotoPR"` (deveria abrir a listagem
    # numa aba SEPARADA) — mas o clique aqui é disparado via `page.evaluate()`
    # (script), não um clique real de mouse, e sem a "ativação de usuário" de
    # um clique real o Chromium às vezes RECUSA abrir aba nova e navega a
    # própria aba de origem pra URL da listagem. Resultado: a partir da 2ª
    # categoria do loop, `painel` já não é mais o resumo (é a listagem da
    # categoria anterior) — o link da categoria seguinte não existe ali,
    # `clicou` sai vazio e a categoria inteira fica de fora do resultado.
    # Bug real confirmado 2026-08-17 (diagnóstico ao vivo: só "prazo_aberto",
    # a 1ª categoria do loop, aparecia sincronizada — todas as demais, mesmo
    # com quantidade > 0 no resumo, ficavam ausentes de `secoes`).
    with contextlib.suppress(Exception):
        if "painel_advogado_listar" not in (painel.url or ""):
            # pelo link ASSINADO (acao= sem o hash pode encerrar a sessão)
            await ec._ir_para_inicio(painel, 1000)

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

    Retorna {ok, resumo:[{chave,label,quantidade}], secoes:{chave:[casos]}} —
    NUNCA lança: qualquer exceção inesperada (ex.: a aba foi fechada por outra
    operação concorrente no meio da leitura — `playwright.TargetClosedError`,
    bug real relatado 2026-09-18) vira `{"ok": False, "erro": "..."}` com a
    mensagem real, em vez de subir crua e virar um 500 sem corpo (o front-end
    então mostrava "erro desconhecido", sem nada acionável)."""
    try:
        return await _ler_todos_corpo()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]}


async def _ler_todos_corpo() -> dict:
    from ingestao import eproc_client as ec

    browser, page = await ec._connect()
    ctx = page.context
    try:
        # Sessão ativa? Confere em TODAS as abas já abertas (mesma lógica de
        # `eproc_client.peek()` — "logado se ALGUMA aba está na área
        # autenticada"), ANTES de decidir se precisa navegar alguma aba pra
        # verificar. Sem isso, a checagem dependia SÓ de conseguir navegar a
        # aba escolhida como `painel` até `painel_advogado_listar` — e depois
        # que qualquer sincronização inteira roda, `painel` fica na ÚLTIMA
        # categoria lida (listagem), não mais no resumo; a próxima chamada
        # então cai no fallback `painel = page` (a 1ª aba do contexto,
        # qualquer uma que seja) e tenta navegá-la de novo. Se essa navegação
        # específica falhasse ou demorasse (aba fechada, travada por outro
        # motivo alheio à sessão), a sessão era erroneamente reportada como
        # INATIVA mesmo com outra aba aberta e autenticada — disparando a
        # janela de login/cofre sem necessidade. Bug real relatado 2026-08-17.
        sessao_ativa = False
        for p in ctx.pages:
            with contextlib.suppress(Exception):
                if ec._status_dict(p.url or "")["logado"]:
                    sessao_ativa = True
                    break

        painel = None
        for p in ctx.pages:
            with contextlib.suppress(Exception):
                if "painel_advogado_listar" in (p.url or ""):
                    painel = p
                    break
        if painel is None:
            painel = page
            with contextlib.suppress(Exception):
                # pelo link ASSINADO (acao= sem o hash pode encerrar a sessão)
                await ec._ir_para_inicio(painel, 2000)
            if not sessao_ativa:
                sessao_ativa = ec._status_dict(painel.url)["logado"]

        if not sessao_ativa:
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
        #
        # Defensivo (bug real 2026-08-27): o resumo pode reportar quantidade >
        # 0 numa categoria e a extração de detalhe (`_ler_categoria`) ainda
        # assim vir vazia — falha de clique/navegação/timing, não a categoria
        # tendo esvaziado de verdade. Sem tratamento, isso "some" a categoria
        # inteira do `secoes` desta rodada, e `salvar()` (merge por processo)
        # interpreta CADA processo que estava nela como tendo "saído da
        # caixa" — apagando prazos/urgências reais da tela por causa de uma
        # falha de sincronização, não de um esvaziamento legítimo. Mesmo
        # princípio já aplicado em `ingestao/sincronizador.py` (sync nunca
        # apaga dado por falha parcial de extração): quando isso acontece,
        # preserva os casos da sincronização ANTERIOR só para essa categoria
        # específica, em vez de tratá-la como vazia.
        anterior = carregar()
        secoes_anteriores = anterior.get("secoes") or {}
        secoes: dict[str, list] = {}
        categorias_com_falha: list[str] = []
        for cat in CATEGORIAS:
            q = next((x["quantidade"] for x in resumo if x["chave"] == cat["chave"]), 0)
            if q <= 0:
                continue
            casos = await _ler_categoria(ctx, painel, cat)
            if casos:
                secoes[cat["chave"]] = casos
            else:
                anteriores_desta_cat = secoes_anteriores.get(cat["chave"]) or []
                if anteriores_desta_cat:
                    secoes[cat["chave"]] = anteriores_desta_cat
                    categorias_com_falha.append(cat["chave"])
            # volta ao painel para o próximo clique ficar disponível
            with contextlib.suppress(Exception):
                await painel.bring_to_front()

        resultado = {"ok": True, "resumo": resumo, "secoes": secoes}
        if categorias_com_falha:
            resultado["aviso"] = (
                "Falha ao carregar o detalhe de: " + ", ".join(categorias_com_falha) +
                " (o resumo indicava casos, mas a extração veio vazia) — mantidos os dados "
                "da sincronização anterior para essas categorias. Tente sincronizar de novo "
                "em instantes."
            )
        # Fecha as abas de listagem abertas durante o loop de categorias (o
        # link tem target="painel_remotoPR" — às vezes o Chromium abre de
        # fato uma aba nova em vez de navegar `painel` mesmo, ver comentário
        # em `_ler_categoria`) — mantém só `painel` na janela do usuário.
        with contextlib.suppress(Exception):
            await ec._fechar_abas_extras(ctx, painel)
        return resultado
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


# --- Sincronização completa: sessão + login automático + leitura + persistência,
# serializada (single-flight) ------------------------------------------------
#
# `ler_todos()` (e o login automático de `eproc_client.abrir()`, chamado quando a
# sessão está inativa) operam sobre a MESMA aba única do Chromium controlado por
# CDP. Sem nenhuma exclusão mútua, um clique duplicado em "Sincronizar
# intimações" (ou uma chamada concorrente qualquer — reload da página no meio do
# caminho, por exemplo) disparava uma 2ª tentativa em paralelo, disputando a
# mesma aba: uma preenchia/enviava o formulário de login enquanto a outra
# navegava a aba de novo pro meio do caminho (`page.goto()`), reiniciando o
# fluxo OAuth do zero (o e-proc gera um `state`/`nonce` novo a cada navegação).
# O sintoma visível era a janela ficando presa tentando logar indefinidamente,
# mesmo com credenciais corretas no cofre — nunca chegava a concluir. Bug real
# relatado 2026-09-16 (4 cliques seguidos em "Sincronizar intimações").
#
# A correção: `sincronizar()` é "single-flight" — uma chamada concorrente feita
# enquanto outra já está em andamento NUNCA inicia uma nova tentativa; ela só
# espera a que já está rodando terminar e reaproveita o MESMO resultado.
_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None


async def _sincronizar_impl() -> dict:
    """Corpo real de uma sincronização completa — só chamado de dentro do
    single-flight de `sincronizar()`, nunca diretamente. Tenta ler direto; se
    a sessão estiver inativa, loga sozinho (quando o cofre estiver
    destrancado e tiver credenciais salvas) e tenta ler de novo.

    Roda inteiro sob `eproc_client._browser_lock` — a MESMA trava que
    `eproc_client.abrir()` usa (por isso chama `ec._abrir_impl` diretamente,
    o núcleo sem lock de `abrir()`, evitando deadlock de adquirir a trava não-
    reentrante duas vezes). Sem isso, uma chamada a `abrir()` feita por outro
    caminho (ex.: botão "Abrir e-proc / Login", ou a reconexão automática no
    início do painel) podia rodar EM PARALELO com esta sincronização e fechar,
    debaixo dela, a aba que a leitura de categorias estava usando — bug real
    relatado 2026-09-18."""
    from ingestao import eproc_client as ec
    from services import cofre_service

    # Fonte "mcp" (EPROC_INTIMACOES_FONTE no .env — Painel SISDPU 2.0): lê pelo
    # MCP do e-proc, SEM navegador (ingestao/eproc_intimacoes_mcp.py). Mesmo
    # formato; só se falhar cai na leitura pelo navegador abaixo, como reserva.
    aviso_mcp = ""
    if _fonte_mcp():
        from ingestao import eproc_intimacoes_mcp
        res = await eproc_intimacoes_mcp.ler_todos()
        if res.get("ok"):
            salvar(res)
            return {"ok": True, **carregar_visivel(), **({"aviso": res["aviso"]} if res.get("aviso") else {})}
        aviso_mcp = f"Leitura pelo MCP do e-proc falhou ({res.get('erro')}); tentando pelo navegador."

    async with ec._browser_lock:
        res = await ler_todos()
        if not res.get("ok"):
            if not cofre_service.destrancado():
                res["cofre_trancado"] = True
                return res
            creds = cofre_service.get_credenciais("eproc")
            if creds:
                usuario, senha = creds
                await ec._abrir_impl(ec.EPROC_INTERNO, usuario, senha)
                res = await ler_todos()
    if res.get("ok"):
        salvar(res)
        # `carregar()` devolve o payload persistido (sincronizado_em/resumo/
        # secoes/diff/saidas) SEM a chave "ok" — sem repô-la aqui, o chamador
        # (`routes.prazos.api_prazos_sincronizar`, que decide o status HTTP
        # por `res.get("ok")`, e `prazos.html`, que trata `resp.ok === false`
        # como falha) via um sucesso real como se fosse falha ("Falha ao
        # sincronizar.", mesmo com o dado já salvo corretamente em disco —
        # só sumia depois de um F5, que recarrega por `/api/prazos/
        # intimacoes` em vez de reusar esta resposta). Bug real relatado
        # 2026-09-22. Usa `carregar_visivel()` (não a crua) por consistência
        # com o que `/api/prazos/intimacoes` devolveria logo em seguida (ex.:
        # um F5) — ainda que, recém-sincronizado, não sobre baixa local pra
        # nenhum processo tocado (ver `salvar()`, que já a limpa).
        return {"ok": True, **carregar_visivel(), **({"aviso": aviso_mcp} if aviso_mcp else {})}
    if aviso_mcp:
        res["erro"] = aviso_mcp + " " + (res.get("erro") or "")
    return res


def _fonte_mcp() -> bool:
    import os
    return (os.getenv("EPROC_INTIMACOES_FONTE") or "").strip().lower() == "mcp"


async def sincronizar() -> dict:
    """Sincroniza tudo — verifica a sessão, loga sozinho se preciso (cofre
    destrancado + credenciais salvas), lê todas as categorias de Citações/
    Intimações e persiste. Serializada (single-flight, ver comentário acima):
    seguro chamar mais de uma vez em paralelo — todas as chamadas concorrentes
    esperam a mesma execução e recebem o mesmo resultado, em vez de disputar a
    aba do e-proc entre si.

    Retorna o payload salvo (`eproc_intimacoes.carregar()`) em caso de
    sucesso, ou `{"ok": False, "erro": ...}` (com `cofre_trancado: true`
    quando for o caso) — mesmo formato que `ler_todos()` já devolvia."""
    global _sync_task
    async with _sync_lock:
        if _sync_task is None or _sync_task.done():
            _sync_task = asyncio.create_task(_sincronizar_impl())
        task = _sync_task
    return await task
