"""Serviço da feature "Retornos".

Gestão dos ATENDIMENTOS DE RETORNO na caixa de entrada do SISDPU. Espelha o
mecanismo da aba "Arquivos SAT" (identificação por descrição da tramitação) e a
UX do PAINEL COMUNICAÇÕES (tabela + linha retrátil com Resumo IA e Sugestão IA).

Fluxo da sincronização (SSE):
    listar_caixa_estruturada() → filtra os itens cuja descrição contém
    "atendimento de retorno" → para cada PAJ, detalha via
    sisdpu_client.movimentacoes_paj (últimas movimentações + descrição integral
    do retorno) → dispara a IA leve (services.retornos_ia) → grava no store.

Persistência: um único JSON (`OFICIO_GERAL/_retornos_store.json`) mapeando
id_tramite → registro. Assim, ao reabrir a aba, o Resumo/Sugestão da IA e a
edição manual da sugestão são preservados sem reanalisar.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import config
from ingestao import sisdpu_client
from ingestao import sisdpu_movtram as movtram
from services import historico_painel, retornos_ia

GRUPO_PADRAO = "01 - PREV_DIVPREV"


def _log_noop(_msg: str) -> None:
    """Log silencioso para a persistência unificada (o progresso da Retornos usa o SSE)."""

# --- parâmetros de negócio (aprovados pelo usuário) ---
DESCRICAO_ALVO = "atendimento de retorno"  # trecho p/ filtro (normalizado)

_STORE = Path(config.OFICIO_GERAL) / "_retornos_store.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _num_to_norm(paj: str) -> str:
    """'2026/029-01651' -> 'PAJ-2026-029-01651'."""
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (paj or "")


def _paj_partes(paj: str) -> tuple[str, str, str]:
    """'2026/029-01651' -> (numero, ano, unidade) = ('01651', '2026', '029')."""
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    if not m:
        return ("", "", "")
    return (m.group(3), m.group(1), m.group(2))


def _pasta_paj(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _limpar_oficio(s: str) -> str:
    """Normaliza o ofício para o mesmo formato da tabela de PAJs (metadata.oficio_caixa):
    'E. 04º OFÍCIO PREVIDENCIÁRIO - (D) FELIPE... Tramitar para titular' -> '04º OFÍCIO
    PREVIDENCIÁRIO'. Corta no primeiro ' - ' e remove o prefixo 'E.'. Idempotente."""
    s = (s or "").strip()
    if not s:
        return ""
    s = re.split(r"\s+-\s+", s, 1)[0].strip()   # remove '- (D) NOME ... Tramitar para titular'
    s = re.sub(r"^E\.\s*", "", s).strip()        # remove prefixo 'E. '
    return s


def _extrair_processos(meta: dict) -> list[dict]:
    """TODOS os processos judiciais vinculados ao PAJ, fiéis ao cadastrado no SISDPU
    (o PAJ pode ter mais de um). Prioriza `metadata.processos_judiciais` (lista
    completa, gravada direto do scrape — sem reparse de texto); cai no campo singular
    antigo (`processo_judicial` + `sisdpu_raw.juizo`) para metadata.json gravados antes
    desta extensão. Cada item: {numero, juizo, data_inclusao}."""
    procs = meta.get("processos_judiciais")
    if not procs and meta.get("processo_judicial"):
        raw = meta.get("sisdpu_raw") or {}
        procs = [{
            "processo_judicial": meta["processo_judicial"],
            "juizo": raw.get("juizo") or "",
            "data_inclusao": raw.get("data_inclusao") or "",
        }]
    out = []
    for p in (procs or []):
        numero = (p.get("processo_judicial") or "").strip()
        if not numero:
            continue
        out.append({
            "numero": numero,
            "juizo": (p.get("juizo") or "").strip(),
            "data_inclusao": (p.get("data_inclusao") or "").strip(),
        })
    return out


def _normalizar_num_processo(s: str) -> str:
    """Só os dígitos do número CNJ — o SISDPU grava sem pontuação (ex.:
    '50041632720264047000') e o e-proc com pontuação (ex.: '5004163-27.2026.4.04.7000');
    normalizar para dígitos permite cruzar os dois."""
    return re.sub(r"\D", "", s or "")


def _mapa_intimacoes_eproc() -> dict[str, dict]:
    """Índice {processo (só dígitos) -> caso} de TODAS as intimações/prazos JÁ
    sincronizados na aba "Prazos e-proc" — leitura LOCAL do JSON persistido por
    `eproc_intimacoes.salvar()` (não navega no e-proc; instantâneo). Vazio se essa
    sincronização nunca rodou — nesse caso nenhum processo mostra o badge."""
    from ingestao import eproc_intimacoes
    dados = eproc_intimacoes.carregar()
    idx: dict[str, dict] = {}
    for categoria, casos in (dados.get("secoes") or {}).items():
        for caso in casos:
            num = _normalizar_num_processo(caso.get("processo", ""))
            if num:
                c = dict(caso)
                c["categoria"] = categoria
                idx[num] = c
    return idx


def _enriquecer_com_intimacoes(processos: list[dict], idx_eproc: dict[str, dict]) -> list[dict]:
    """Anexa `intimacao` a cada processo com base no índice de Prazos e-proc (ver
    `_mapa_intimacoes_eproc`). `intimacao` fica None quando o processo não aparece
    nas intimações/prazos sincronizados (não é um erro — pode não ter pendência,
    ou a sincronização de Prazos e-proc ainda não rodou)."""
    for p in processos:
        caso = idx_eproc.get(_normalizar_num_processo(p.get("numero", "")))
        if not caso:
            p["intimacao"] = None
            continue
        prazos = caso.get("prazos") or []
        data_prazo, estimado = "", False
        for pr in prazos:
            if pr.get("final_prazo"):
                data_prazo, estimado = pr["final_prazo"], False
                break
        if not data_prazo:
            for pr in prazos:
                if pr.get("final_estimado"):
                    data_prazo, estimado = pr["final_estimado"], True
                    break
        p["intimacao"] = {
            "categoria": caso.get("categoria", ""),
            "urgente": bool(caso.get("urgente")),
            "n_prazos": caso.get("n_prazos", len(prazos)),
            "prazo_dias_min": caso.get("prazo_dias_min"),
            "data_prazo": data_prazo,
            "estimado": estimado,
            "processo_url": caso.get("processo_url", ""),
        }
    return processos


def _ler_metadata(paj_norm: str) -> dict:
    p = _pasta_paj(paj_norm) / "metadata.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _html_para_texto(h: str) -> str:
    if not h:
        return ""
    h = re.sub(r"(?i)<\s*br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</\s*(p|div|tr|li)\s*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = _html.unescape(h)
    h = h.replace("\xa0", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _narrativa_do_paj(paj_norm: str) -> str:
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    return _html_para_texto(raw.get("narrativa") or "")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# store (JSON: id_tramite -> registro)
# ---------------------------------------------------------------------------
def _ler_store() -> dict:
    if not _STORE.exists():
        return {}
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _gravar_store(store: dict) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def listar() -> dict:
    """Registros da Retornos. O NÚCLEO do PAJ (assistido, ofício, pretensão,
    descrição do retorno, nº de movs) vem do `metadata.json` — FONTE ÚNICA. O store
    guarda só os campos exclusivos (IA, ponteiros) + um cache leve usado como fallback
    para registros antigos ainda sem metadata. Os que estão na caixa vêm primeiro,
    mais recentes no topo."""
    store = _ler_store()
    idx_eproc = _mapa_intimacoes_eproc()
    itens = []
    for rec in store.values():
        item = dict(rec)
        meta = _ler_metadata(rec.get("paj_norm", ""))
        if meta:  # fonte de verdade
            item["assistido"] = meta.get("assistido_caixa") or item.get("assistido", "")
            item["oficio"] = meta.get("oficio_caixa") or item.get("oficio", "")
            item["pretensao"] = (meta.get("pretensaopura") or meta.get("pretensao")
                                 or item.get("pretensao", ""))
            processos = _extrair_processos(meta) or item.get("processos_judiciais", [])
            item["processos_judiciais"] = _enriquecer_com_intimacoes(processos, idx_eproc)
            movs = (meta.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", []) or []
            if movs:
                item["n_movimentacoes"] = len(movs)
                r = _extrair_retorno_das_movs(movs)
                if r.get("descricao_retorno"):
                    item["descricao_retorno"] = r["descricao_retorno"]
                    item["data_retorno"] = r.get("data") or item.get("data_retorno", "")
        # compat: registro antigo guardava o array `movimentacoes`; deriva a contagem.
        if "n_movimentacoes" not in item:
            item["n_movimentacoes"] = len(item.get("movimentacoes", []) or [])
        item.pop("movimentacoes", None)  # nunca trafega o array pela listagem
        item["oficio"] = _limpar_oficio(item.get("oficio", ""))
        itens.append(item)
    # em_caixa=True antes; dentro de cada grupo, mais recentes primeiro
    itens.sort(key=lambda r: r.get("atualizado_em", ""), reverse=True)
    itens.sort(key=lambda r: 0 if r.get("em_caixa", True) else 1)
    return {"ok": True, "itens": itens}


def salvar_sugestao(id_tramite: str, texto: str) -> dict:
    """Persiste a edição manual da sugestão (prevalece sobre a sugestão da IA)."""
    store = _ler_store()
    rec = store.get(id_tramite)
    if not rec:
        return {"ok": False, "detalhe": "Retorno não encontrado (sincronize novamente)."}
    rec["sugestao_editada"] = (texto or "").strip()
    rec["atualizado_em"] = _now_iso()
    store[id_tramite] = rec
    _gravar_store(store)
    return {"ok": True}


async def reanalisar(id_tramite: str, prompt_extra: str = "") -> dict:
    """Reanalisa UM retorno com a IA, opcionalmente com instruções adicionais
    (`prompt_extra`). Vazio = segue a análise padrão. Atualiza resumo/sugestão no
    store (descarta a edição manual da sugestão) e devolve os novos campos."""
    if not retornos_ia.disponivel():
        return {"ok": False, "detalhe": "IA indisponível (claude não encontrado ou desligado)."}
    store = _ler_store()
    rec = store.get(id_tramite)
    if not rec:
        return {"ok": False, "detalhe": "Retorno não encontrado (sincronize novamente)."}

    paj_norm = rec.get("paj_norm", "")
    narrativa = _narrativa_do_paj(paj_norm)
    # movs da FONTE ÚNICA (metadata); fallback ao array antigo do store, se houver.
    meta = _ler_metadata(paj_norm)
    movs = ((meta.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", [])
            or rec.get("movimentacoes", []))
    analise = await asyncio.to_thread(
        retornos_ia.analisar_retorno,
        narrativa, movs, rec.get("descricao_retorno", ""),
        rec.get("assistido", ""), rec.get("pretensao", ""), prompt_extra or "",
    )
    if not analise:
        return {"ok": False, "detalhe": "A IA não retornou análise. Tente novamente."}

    rec["resumo_ia"] = analise.get("resumo", "")
    rec["precisa_acao"] = analise.get("precisa_acao")
    rec["sugestao_ia"] = analise.get("sugestao", "")
    rec["sugestao_editada"] = ""  # a reanálise substitui a sugestão vigente
    rec["ia_status"] = "ok"
    rec["atualizado_em"] = _now_iso()
    store[id_tramite] = rec
    _gravar_store(store)
    return {"ok": True, "resumo_ia": rec["resumo_ia"],
            "precisa_acao": rec["precisa_acao"], "sugestao_ia": rec["sugestao_ia"]}


# ---------------------------------------------------------------------------
# detalhamento de um PAJ de retorno
# ---------------------------------------------------------------------------
def _extrair_retorno_das_movs(movs: list[dict]) -> dict:
    """Localiza a movimentação de 'Atendimento de retorno' (a que caiu na caixa) e
    devolve {descricao_retorno, data, remetente}. Usa a mais recente encontrada."""
    for m in movs:
        fases = _norm(m.get("fases", ""))
        if "atendimento de retorno" in fases:
            return {
                "descricao_retorno": (m.get("descricao_retorno") or m.get("descricao") or "").strip(),
                "data": (m.get("data") or "").strip(),
                "remetente": (m.get("usuario") or "").strip(),
            }
    return {"descricao_retorno": "", "data": "", "remetente": ""}


async def _detalhar(paj: str) -> dict:
    """Abre o PAJ e extrai movimentações + descrição integral do retorno + cabeçalho
    + narrativa (tudo na MESMA navegação). Usa `esperas_curtas` (caminho rápido, sem
    baixar anexos). Retorna {} em falha."""
    numero, ano, unidade = _paj_partes(paj)
    if not numero:
        return {}
    try:
        det = await sisdpu_client.movimentacoes_paj(numero, ano, unidade, esperas_curtas=True)
    except Exception:  # noqa: BLE001
        det = {}
    if not isinstance(det, dict) or det.get("erro"):
        return {}
    return det


# ---------------------------------------------------------------------------
# sincronização (SSE) — lê a caixa, filtra, detalha, analisa com IA
# ---------------------------------------------------------------------------
def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


async def sincronizar_stream() -> AsyncIterator[dict]:
    """Gera eventos de progresso (dicts) enquanto sincroniza os retornos.
    Eventos de log: {level, msg}. Evento final: {done: True, resultado: {...}}."""
    yield _ev("info", "Lendo a caixa de entrada do SISDPU…")
    caixa, exc, eventos = await _com_auto_reconexao(movtram.listar_caixa_estruturada)
    for ev in eventos:
        yield ev
    if exc is not None:
        yield _ev("erro", f"Falha ao ler a caixa ({type(exc).__name__}): {str(exc)[:200]}.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": str(exc)[:300]}}
        return
    if not caixa.get("ok"):
        yield _ev("erro", caixa.get("detalhe", "Falha ao ler a caixa."))
        yield {"done": True, "resultado": {"ok": False, "detalhe": caixa.get("detalhe", "")}}
        return

    alvo = DESCRICAO_ALVO
    pendentes = []
    for it in caixa.get("itens", []):
        texto = _norm(it.get("descricao", "") + " | " + it.get("linha_texto", ""))
        if alvo in texto:
            pendentes.append(it)

    yield _ev("info", f"{len(pendentes)} atendimento(s) de retorno na caixa.")

    store = _ler_store()
    idx_eproc = _mapa_intimacoes_eproc()
    ids_caixa: set[str] = set()
    ia_ok = retornos_ia.disponivel()
    if not ia_ok:
        yield _ev("muted", "IA indisponível (claude não encontrado ou desligado) — retornos listados sem análise.")

    # Pipeline: o detalhamento (Playwright) é SERIAL — o navegador é único —, mas cada
    # análise da IA (subprocess independente) é disparada assim que o PAJ é detalhado e
    # roda em PARALELO (até RETORNOS_CONCORRENCIA de cada vez), sobreposta ao
    # detalhamento dos PAJs seguintes. Reduz o tempo de "soma das IAs" para
    # "total/concorrência", sobreposto ao Playwright.
    sem = asyncio.Semaphore(retornos_ia.RETORNOS_CONCORRENCIA)

    async def _analisar_task(id_tramite: str, paj: str, narrativa: str, movs: list[dict],
                             descricao_retorno: str, assistido: str, pretensao: str):
        async with sem:
            analise = await asyncio.to_thread(
                retornos_ia.analisar_retorno,
                narrativa, movs, descricao_retorno, assistido, pretensao,
            )
        return (id_tramite, paj, analise)

    tarefas: list = []

    n = 0
    for it in pendentes:
        paj = it.get("paj", "")
        paj_norm = _num_to_norm(paj)
        id_tramite = it.get("id_tramite", "")
        ids_caixa.add(id_tramite)
        n += 1
        yield _ev("novo", f"[{n}/{len(pendentes)}] {paj} — sincronizando (dados unificados)…")

        # Sincroniza o PAJ pelo PIPELINE UNIFICADO (mesma persistência do dashboard):
        # grava metadata.json com movs NORMALIZADAS + narrativa + categoria_caixa=
        # "retorno". Assim a Retornos NÃO mantém dados paralelos do PAJ — só os
        # campos exclusivos (IA, id_tramite, caixas abertas) + cache leve.
        # Caminho RÁPIDO (a análise de retorno não precisa de documentos por seq):
        #   `baixar_anexos=False`  → não baixa os binários da aba Arquivos;
        #   `extrair_documentos=False` → pula a abertura de 1 popup por movimentação
        #        com anexo (o gargalo da sync de retorno); docs já conhecidos ficam;
        #   `esperas_curtas=True`  → esperas fixas curtas no detalhamento.
        # Um sync completo do dashboard depois reextrai os documentos que faltarem.
        from ingestao import sincronizador
        adapter = {
            "paj": paj,
            "assistido": it.get("assistido", ""),
            "oficio": "",
            "etiqueta": "",
            "data": it.get("data_envio", ""),
            # descrição concatenada p/ o classificador central marcar categoria="retorno"
            "desc": (it.get("descricao", "") + " | " + it.get("linha_texto", "")).strip(),
        }
        _, exc, eventos = await _com_auto_reconexao(lambda: sincronizador._processar_paj(
            adapter, _log_noop, baixar_anexos=False,
            extrair_documentos=False, esperas_curtas=True))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("muted", f"    {paj} — falha ao gravar dados unificados: {exc}")

        # Deriva o núcleo do metadata recém-gravado (fonte única). Fallback: linha da caixa.
        meta = _ler_metadata(paj_norm)
        movs = (meta.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", []) or []
        ret = _extrair_retorno_das_movs(movs) if movs else {}
        assistido = (meta.get("assistido_caixa") or it.get("assistido", "")).strip()
        oficio = _limpar_oficio(meta.get("oficio_caixa", "")).strip()
        pretensao = (meta.get("pretensaopura") or meta.get("pretensao", "")).strip()
        processos_judiciais = _enriquecer_com_intimacoes(_extrair_processos(meta), idx_eproc)
        narrativa = _narrativa_do_paj(paj_norm)
        data_retorno = ret.get("data") or it.get("data_envio", "")
        remetente = ret.get("remetente") or it.get("remetente", "")
        descricao_retorno = ret.get("descricao_retorno") or it.get("descricao", "")

        # onde o PAJ está aberto agora (tramitações sem conclusão) — cache p/ o badge.
        try:
            abertas = await movtram.caixas_abertas_paj(it.get("id", ""))
        except Exception:  # noqa: BLE001
            abertas = []

        rec_antigo = store.get(id_tramite, {})
        rec: dict[str, Any] = {
            # ponteiros + exclusivos da Retornos
            "id_tramite": id_tramite,
            "id_processo": it.get("id", ""),
            "paj": paj,
            "paj_norm": paj_norm,
            "url_detalhe": it.get("url_detalhe", ""),
            "caixas_abertas": abertas,
            "em_caixa": True,
            "atualizado_em": _now_iso(),
            # cache LEVE p/ a listagem (fonte de verdade = metadata; ver listar()).
            # NUNCA guardamos o array de movimentações aqui (vem do metadata).
            "assistido": assistido,
            "oficio": oficio,
            "pretensao": pretensao,
            "processos_judiciais": processos_judiciais,
            "data_retorno": data_retorno,
            "remetente": remetente,
            "descricao_retorno": descricao_retorno,
            "n_movimentacoes": len(movs),
            # IA (exclusivo) — preservado entre sincronizações
            "sugestao_editada": rec_antigo.get("sugestao_editada", ""),
            "resumo_ia": rec_antigo.get("resumo_ia", ""),
            "precisa_acao": rec_antigo.get("precisa_acao"),
            "sugestao_ia": rec_antigo.get("sugestao_ia", ""),
            "ia_status": rec_antigo.get("ia_status", "pendente"),
        }
        store[id_tramite] = rec
        _gravar_store(store)  # grava incremental (recuperável se interromper)

        # Dispara a análise em paralelo (só se disponível e ainda sem resumo salvo).
        if ia_ok and not rec["resumo_ia"]:
            tarefas.append(asyncio.create_task(
                _analisar_task(id_tramite, paj, narrativa, movs,
                               descricao_retorno, assistido, pretensao)))
        elif not ia_ok:
            rec["ia_status"] = "indisponivel"
            store[id_tramite] = rec
            _gravar_store(store)

    # Aguarda as análises (que já vinham rodando em paralelo) e grava conforme concluem.
    if tarefas:
        yield _ev("info", f"Analisando {len(tarefas)} retorno(s) com a IA "
                          f"(até {retornos_ia.RETORNOS_CONCORRENCIA} em paralelo)…")
        for fut in asyncio.as_completed(tarefas):
            id_tramite, paj, analise = await fut
            rec = store.get(id_tramite) or {}
            if analise:
                rec["resumo_ia"] = analise.get("resumo", "")
                rec["precisa_acao"] = analise.get("precisa_acao")
                rec["sugestao_ia"] = analise.get("sugestao", "")
                rec["ia_status"] = "ok"
                yield _ev("ok", f"    {paj} — analisado.")
            else:
                rec["ia_status"] = "erro"
                yield _ev("muted", f"    {paj} — IA não retornou análise.")
            store[id_tramite] = rec
            _gravar_store(store)

    # Retornos é espelho da caixa: quem não veio nesta leitura SAIU da caixa (foi
    # despachado/concluído por fora, ou a tramitação de retorno não existe mais) —
    # remove do store em vez de só marcar, para não poluir a lista.
    idts_conhecidos = list(store.keys())
    saidos = 0
    for idt in idts_conhecidos:
        if idt not in ids_caixa:
            store.pop(idt, None)
            saidos += 1
    if saidos:
        _gravar_store(store)

    yield _ev("ok", f"Sincronização concluída: {len(pendentes)} na caixa"
                    + (f", {saidos} removido(s) por terem saído da caixa." if saidos else "."))
    yield {"done": True, "resultado": {"ok": True, "n_caixa": len(pendentes), "n_saidos": saidos}}


async def ressincronizar_paj_stream(id_tramite: str) -> AsyncIterator[dict]:
    """Ressincroniza UM retorno já conhecido, pelo MESMO caminho leve da sincronização
    em lote (`sincronizador._processar_paj` com `baixar_anexos=False,
    extrair_documentos=False, esperas_curtas=True`) — NUNCA a sincronização pesada da
    aba PAJs. Não dispara a IA (isso é o botão "Reanálise pela IA"); só refresca o
    núcleo do metadata (ofício, assistido, pretensão, movimentações) + caixas abertas."""
    store = _ler_store()
    rec = store.get(id_tramite)
    if not rec:
        yield _ev("erro", "Retorno não encontrado (sincronize novamente).")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Retorno não encontrado."}}
        return

    paj = rec.get("paj", "")
    yield _ev("novo", f"Ressincronizando {paj}…")

    yield _ev("info", "Localizando o trâmite na caixa de entrada…")
    caixa, exc, eventos = await _com_auto_reconexao(movtram.listar_caixa_estruturada)
    for ev in eventos:
        yield ev
    if exc is not None:
        yield _ev("erro", f"Falha ao ler a caixa ({type(exc).__name__}): {str(exc)[:200]}.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": str(exc)[:300]}}
        return
    if not caixa.get("ok"):
        yield _ev("erro", caixa.get("detalhe", "Falha ao ler a caixa."))
        yield {"done": True, "resultado": {"ok": False, "detalhe": caixa.get("detalhe", "")}}
        return
    it = next((x for x in caixa.get("itens", []) if x.get("id_tramite") == id_tramite), None)
    if not it:
        # Espelho da caixa: se não está mais lá, remove do store (mesma regra do sync em lote).
        store.pop(id_tramite, None)
        _gravar_store(store)
        yield _ev("erro", f"{paj} não está mais na caixa de entrada — removido da lista.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "PAJ saiu da caixa."}}
        return

    paj_norm = _num_to_norm(it.get("paj", paj))
    from ingestao import sincronizador
    adapter = {
        "paj": it.get("paj", paj),
        "assistido": it.get("assistido", ""),
        "oficio": "",
        "etiqueta": "",
        "data": it.get("data_envio", ""),
        "desc": (it.get("descricao", "") + " | " + it.get("linha_texto", "")).strip(),
    }
    yield _ev("info", "Regravando dados unificados (modo rápido, sem anexos)…")
    _, exc, eventos = await _com_auto_reconexao(lambda: sincronizador._processar_paj(
        adapter, _log_noop, baixar_anexos=False,
        extrair_documentos=False, esperas_curtas=True))
    for ev in eventos:
        yield ev
    if exc is not None:
        yield _ev("erro", f"Falha ao ressincronizar {paj}: {type(exc).__name__}: {exc}")
        yield {"done": True, "resultado": {"ok": False, "detalhe": str(exc)[:300]}}
        return

    meta = _ler_metadata(paj_norm)
    movs = (meta.get("detalhes_sisdpu", {}) or {}).get("movimentacoes", []) or []
    ret = _extrair_retorno_das_movs(movs) if movs else {}
    assistido = (meta.get("assistido_caixa") or it.get("assistido", "")).strip()
    oficio = _limpar_oficio(meta.get("oficio_caixa", "")).strip()
    pretensao = (meta.get("pretensaopura") or meta.get("pretensao", "")).strip()
    processos_judiciais = _enriquecer_com_intimacoes(_extrair_processos(meta), _mapa_intimacoes_eproc())
    data_retorno = ret.get("data") or it.get("data_envio", "")
    remetente = ret.get("remetente") or it.get("remetente", "")
    descricao_retorno = ret.get("descricao_retorno") or it.get("descricao", "")

    yield _ev("info", "Verificando caixas onde o PAJ está aberto…")
    try:
        abertas = await movtram.caixas_abertas_paj(it.get("id", ""))
    except Exception:  # noqa: BLE001
        abertas = rec.get("caixas_abertas", [])

    rec.update({
        "id_processo": it.get("id", rec.get("id_processo", "")),
        "paj": it.get("paj", paj),
        "paj_norm": paj_norm,
        "url_detalhe": it.get("url_detalhe", rec.get("url_detalhe", "")),
        "caixas_abertas": abertas,
        "atualizado_em": _now_iso(),
        "assistido": assistido,
        "oficio": oficio,
        "processos_judiciais": processos_judiciais,
        "pretensao": pretensao,
        "data_retorno": data_retorno,
        "remetente": remetente,
        "descricao_retorno": descricao_retorno,
        "n_movimentacoes": len(movs),
        # IA preservada — este botão não reanalisa (ver "Reanálise pela IA").
    })
    store[id_tramite] = rec
    _gravar_store(store)

    yield _ev("ok", f"{paj} — ressincronizado com sucesso.")
    yield {"done": True, "resultado": {"ok": True}}


# ---------------------------------------------------------------------------
# Ações (coluna Ações): fases, grupos e execução mov→tram→concluir
# ---------------------------------------------------------------------------
def _um_id_processo() -> str:
    """Um id_processo qualquer do store (para abrir as telas de fase/grupo)."""
    for rec in _ler_store().values():
        if rec.get("id_processo"):
            return str(rec["id_processo"])
    return ""


async def algum_id_processo() -> str:
    """id_processo do store; se vazio, lê a caixa de entrada para achar um PAJ.
    Usado pela importação de fases/caixas (Configurações), que independe de sync."""
    idp = _um_id_processo()
    if idp:
        return idp
    try:
        caixa = await movtram.listar_caixa_estruturada()
        for it in caixa.get("itens", []):
            if it.get("id"):
                return str(it["id"])
    except Exception:  # noqa: BLE001
        pass
    return ""


async def listar_fases() -> dict:
    """Fases de movimentação disponíveis no SISDPU (para o dropdown Movimentar)."""
    idp = _um_id_processo()
    fases = await movtram.listar_fases_movimentacao(idp) if idp else []
    return {"ok": bool(fases), "fases": fases,
            "detalhe": "" if fases else "Não foi possível ler as fases (há retornos sincronizados?)."}


async def listar_grupos() -> dict:
    """Caixas de grupo da unidade para o dropdown de Tramitar. As caixas favoritas
    (dropdown) e frequentes (badges) vêm de /api/config/sisdpu, não daqui."""
    idp = _um_id_processo()
    grupos = await movtram.listar_grupos_unidade(idp) if idp else []
    return {"ok": True, "grupos": grupos, "padrao": GRUPO_PADRAO}


async def caixas_abertas(id_tramite: str) -> dict:
    """Onde o PAJ do retorno está aberto agora (tramitações sem conclusão)."""
    rec = _ler_store().get(id_tramite)
    idp = (rec or {}).get("id_processo", "")
    if not idp:
        return {"ok": False, "abertas": [], "detalhe": "id_processo ausente (sincronize)."}
    abertas = await movtram.caixas_abertas_paj(idp)
    return {"ok": True, "abertas": abertas}


def _iso_to_br(s: str) -> str:
    """'2026-08-15' -> '15/08/2026'. Aceita já-BR ou vazio (devolve intacto)."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def _parece_problema_sessao(resultado, excecao: Exception | None) -> bool:
    """True quando a falha indica SESSÃO indisponível (conflito, deslogado, cofre
    bloqueado) — casos em que vale reconectar e tentar de novo, em vez de desistir.
    Qualquer exceção aqui (timeout de navegação, RuntimeError de credenciais) é
    candidata: o pior caso é reconectar à toa antes de reportar o erro original.
    `resultado` pode não ser um dict (ex.: `_processar_paj` retorna tupla) — nesse
    caso só a exceção é considerada, já que não há campo "ok"/"detalhe" pra checar."""
    if excecao is not None:
        return True
    if isinstance(resultado, dict) and not resultado.get("ok"):
        det = _norm(str(resultado.get("detalhe") or ""))
        return any(t in det for t in ("conflito de sessao", "sessao", "login", "cofre bloqueado"))
    return False


async def _com_auto_reconexao(chamada):
    """Executa `chamada()` (coroutine factory); se falhar por sessão indisponível,
    RECONECTA automaticamente (login fresco no SISDPU — via `sisdpu_client.reconectar()`,
    que usa as credenciais do cofre se já destrancado) e tenta a MESMA ação mais uma vez.
    Retorna (resultado, excecao, eventos_sse) — eventos_sse já prontos para `yield`."""
    eventos: list[dict] = []
    try:
        resultado = await chamada()
        excecao = None
    except Exception as e:  # noqa: BLE001
        resultado, excecao = None, e

    if _parece_problema_sessao(resultado, excecao):
        eventos.append(_ev("muted", "Sessão do SISDPU indisponível — reconectando automaticamente…"))
        try:
            rec_conn = await sisdpu_client.reconectar()
        except Exception as e:  # noqa: BLE001
            rec_conn = {"ok": False, "detalhe": str(e)[:300]}
        eventos.append(_ev("ok" if rec_conn.get("ok") else "erro",
                           f"Reconexão automática: {rec_conn.get('detalhe', '')}"))
        if rec_conn.get("ok"):
            try:
                resultado = await chamada()
                excecao = None
            except Exception as e:  # noqa: BLE001
                resultado, excecao = None, e
    return resultado, excecao, eventos


async def executar_stream(id_tramite: str, fase: str, descricao: str,
                          grupo: str | None = None, prazo_dias: int = 0,
                          prazo_data: str = "", concluir: bool = True,
                          dry_run: bool = False) -> AsyncIterator[dict]:
    """Executa o despacho de UM retorno: movimenta (fase + despacho como descrição),
    tramita (se `grupo`), conclui (se `concluir`). Ao concluir com sucesso, registra no
    histórico do painel e REMOVE o retorno da lista. Emite eventos SSE."""
    store = _ler_store()
    rec = store.get(id_tramite)
    if not rec:
        yield _ev("erro", "Retorno não encontrado (sincronize novamente).")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Retorno não encontrado."}}
        return

    paj = rec.get("paj", "")
    paj_norm = rec.get("paj_norm", "")
    id_proc = rec.get("id_processo", "")
    fase = (fase or "").strip()
    # despacho: usa o texto passado; se vazio, cai no salvo (edição manual ou IA).
    descricao = (descricao or "").strip() or (
        rec.get("sugestao_editada") or rec.get("sugestao_ia") or "").strip()
    if not id_proc:
        yield _ev("erro", "id_processo ausente — sincronize novamente.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "id_processo ausente."}}
        return
    if not fase and not grupo and not concluir:
        yield _ev("erro", "Selecione ao menos uma ação: movimentar, tramitar ou concluir.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Nenhuma ação selecionada."}}
        return
    if fase and not descricao:
        yield _ev("erro", "O despacho (descrição da movimentação) está vazio.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Despacho vazio."}}
        return

    # prazo: data explícita (calendário) tem precedência sobre dias
    prazo_data_final = None
    if grupo:
        if prazo_data:
            prazo_data_final = _iso_to_br(prazo_data)
        elif prazo_dias and int(prazo_dias) > 0:
            prazo_data_final = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")

    pajs = [{"id": id_proc, "numero": paj}]
    pref = "[dry-run] " if dry_run else ""
    yield _ev("novo", f"{pref}Executando {paj}…")

    # 1) MOVIMENTAR (opcional — carrega o despacho como descrição da movimentação)
    mv: dict = {}
    if fase:
        yield _ev("info", f"Movimentando (fase: {fase})…")
        mv, exc, eventos = await _com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, descricao, fase, [], "Documentos do Assistido", dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na movimentação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Verifique a conexão do SISDPU (selo/Reconectar) e tente de novo.")
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if mv.get("ok") else "erro", f"Movimentação: {mv.get('detalhe', '')}")
        if not mv.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
            return

    # 2) TRAMITAR (opcional)
    tram_caixa = ""
    if grupo:
        prazo_txt = f"prazo até {prazo_data_final}" if prazo_data_final else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})…")
        tr, exc, eventos = await _com_auto_reconexao(
            lambda: movtram.tramitar_paj(pajs, grupo=grupo, descricao=descricao,
                                         prazo_data=prazo_data_final, dry_run=dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na tramitação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "A movimentação já foi registrada; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if tr.get("ok") else "erro", f"Tramitação: {tr.get('detalhe', '')}")
        if not tr.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
            return
        tram_caixa = grupo

    # 3) CONCLUIR (opcional) — é o que retira o retorno da caixa de entrada
    concluido = False
    ja_ausente = False
    if concluir:
        yield _ev("info", "Concluindo o trâmite de retorno…")
        cc, exc, eventos = await _com_auto_reconexao(lambda: movtram.concluir_paj([id_tramite], dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na conclusão ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Mov./tramitação já podem ter sido registradas; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        if cc.get("status") == "ja_ausente":
            # O trâmite já não estava na caixa/grupo ANTES desta execução — provável
            # conclusão manual anterior (ou id desatualizado). NADA foi concluído agora;
            # eventual movimentação/tramitação acima, se houve, já foi efetivada de
            # verdade e permanece registrada abaixo.
            yield _ev("muted", f"Conclusão: {cc.get('detalhe', '')}")
            ja_ausente = True
        else:
            yield _ev("ok" if cc.get("ok") else "erro", f"Conclusão: {cc.get('detalhe', '')}")
            if not cc.get("ok"):
                yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": cc.get("detalhe", "")}}
                return
            concluido = not dry_run

    # registra no histórico do painel SÓ se algo foi de fato efetivado nesta execução
    # (movimentação, tramitação ou conclusão real — não um "ja_ausente" isolado, que é
    # um no-op). Remove da lista de Retornos quando concluído OU quando confirmamos que
    # o trâmite já não está mais na caixa (espelho da caixa).
    if not dry_run:
        algo_efetivado = bool(concluido or tram_caixa or fase)
        if algo_efetivado:
            if concluido:
                atividade = "Conclusão de atendimento de retorno"
            elif tram_caixa:
                atividade = "Tramitação de atendimento de retorno"
            else:
                atividade = "Movimentação de atendimento de retorno"
            historico_painel.registrar({
                "paj": paj, "paj_norm": paj_norm, "url_detalhe": rec.get("url_detalhe", ""),
                "assistido": rec.get("assistido", ""),
                "origem": "retorno", "atividade": atividade,
                "movimentacao": fase or "—", "tramitado_para": tram_caixa,
                "concluido": concluido, "detalhe": descricao[:300],
            })
        if concluido or ja_ausente:
            store = _ler_store()
            store.pop(id_tramite, None)
            _gravar_store(store)

    if ja_ausente:
        yield _ev("muted", f"{paj} — o trâmite de retorno já não estava na caixa antes desta "
                           "execução (provável conclusão manual anterior). Removido da lista.")
        yield {"done": True, "resultado": {"ok": False, "status": "ja_ausente", "concluido": False,
                                           "tramitado_para": tram_caixa,
                                           "detalhe": "Trâmite já estava ausente antes desta execução — "
                                                     "nada foi concluído agora."}}
        return

    yield _ev("ok", f"{paj} — executado com sucesso.")
    yield {"done": True, "resultado": {"ok": True, "concluido": concluido,
                                       "tramitado_para": tram_caixa, "mov_seq": mv.get("mov_seq")}}
