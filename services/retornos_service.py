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
    """Registros conhecidos, os que estão na caixa primeiro, mais recentes no topo."""
    store = _ler_store()
    itens = list(store.values())
    itens.sort(key=lambda r: (not r.get("em_caixa", True), r.get("atualizado_em", "")), reverse=False)
    # em_caixa=True antes; dentro de cada grupo, mais recentes primeiro
    itens.sort(key=lambda r: r.get("atualizado_em", ""), reverse=True)
    itens.sort(key=lambda r: 0 if r.get("em_caixa", True) else 1)
    # normaliza o ofício na leitura (cobre registros antigos com ofício sujo) — idempotente
    for r in itens:
        r["oficio"] = _limpar_oficio(r.get("oficio", ""))
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

    narrativa = _narrativa_do_paj(rec.get("paj_norm", ""))
    analise = await asyncio.to_thread(
        retornos_ia.analisar_retorno,
        narrativa, rec.get("movimentacoes", []), rec.get("descricao_retorno", ""),
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
    caixa = await movtram.listar_caixa_estruturada()
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
        yield _ev("novo", f"[{n}/{len(pendentes)}] {paj} — detalhando…")

        det = await _detalhar(paj)
        movs = (det.get("movimentacoes") or [])[:30]
        ret = _extrair_retorno_das_movs(det.get("movimentacoes") or [])

        # cabeçalho do retorno: prioriza a movimentação; cai na linha da caixa
        data_retorno = ret.get("data") or it.get("data_envio", "")
        remetente = ret.get("remetente") or it.get("remetente", "")
        descricao_retorno = ret.get("descricao_retorno") or it.get("descricao", "")

        meta = _ler_metadata(paj_norm)
        assistido = (det.get("assistido") or it.get("assistido", "")
                     or meta.get("assistido_caixa", "")).strip()
        # ofício: prioriza o metadata (limpo, igual à tabela de PAJs); limpa o fallback
        oficio = (meta.get("oficio_caixa") or _limpar_oficio(det.get("oficio", ""))).strip()
        pretensao = (det.get("pretensao") or meta.get("pretensaopura")
                     or meta.get("pretensao", "")).strip()
        # narrativa vem da MESMA navegação (movimentacoes_paj); fallback: metadata local
        narrativa = _html_para_texto(det.get("narrativa") or "") or _narrativa_do_paj(paj_norm)

        # onde o PAJ está aberto agora (tramitações sem conclusão) — extraído JÁ na
        # sincronização e cacheado, para o badge aparecer instantâneo ao expandir.
        try:
            abertas = await movtram.caixas_abertas_paj(it.get("id", ""))
        except Exception:  # noqa: BLE001
            abertas = []

        rec_antigo = store.get(id_tramite, {})
        rec: dict[str, Any] = {
            "id_tramite": id_tramite,
            "id_processo": it.get("id", ""),
            "paj": paj,
            "paj_norm": paj_norm,
            "assistido": assistido,
            "oficio": oficio,
            "pretensao": pretensao,
            "data_retorno": data_retorno,
            "remetente": remetente,
            "descricao_retorno": descricao_retorno,
            "url_detalhe": it.get("url_detalhe", ""),
            "movimentacoes": movs,
            "caixas_abertas": abertas,
            "em_caixa": True,
            "atualizado_em": _now_iso(),
            # preserva edição manual da sugestão entre sincronizações
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

    # marca como fora da caixa os registros conhecidos que não vieram desta leitura
    saidos = 0
    for idt, rec in store.items():
        if rec.get("em_caixa") and idt not in ids_caixa:
            rec["em_caixa"] = False
            rec["atualizado_em"] = _now_iso()
            saidos += 1
    if saidos:
        _gravar_store(store)

    yield _ev("ok", f"Sincronização concluída: {len(pendentes)} na caixa"
                    + (f", {saidos} saíram." if saidos else "."))
    yield {"done": True, "resultado": {"ok": True, "n_caixa": len(pendentes), "n_saidos": saidos}}


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
    if not fase:
        yield _ev("erro", "Escolha a fase de movimentação.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Fase não informada."}}
        return
    if not descricao:
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

    # 1) MOVIMENTAR (carrega o despacho como descrição da movimentação)
    yield _ev("info", f"Movimentando (fase: {fase})…")
    mv = await movtram.movimentar_paj(pajs, descricao, fase, [], "Documentos do Assistido", dry_run)
    yield _ev("ok" if mv.get("ok") else "erro", f"Movimentação: {mv.get('detalhe', '')}")
    if not mv.get("ok"):
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
        return

    # 2) TRAMITAR (opcional)
    tram_caixa = ""
    if grupo:
        prazo_txt = f"prazo até {prazo_data_final}" if prazo_data_final else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})…")
        tr = await movtram.tramitar_paj(pajs, grupo=grupo, descricao=descricao,
                                        prazo_data=prazo_data_final, dry_run=dry_run)
        yield _ev("ok" if tr.get("ok") else "erro", f"Tramitação: {tr.get('detalhe', '')}")
        if not tr.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
            return
        tram_caixa = grupo

    # 3) CONCLUIR (opcional) — é o que retira o retorno da caixa de entrada
    concluido = False
    if concluir:
        yield _ev("info", "Concluindo o trâmite de retorno…")
        cc = await movtram.concluir_paj([id_tramite], dry_run)
        yield _ev("ok" if cc.get("ok") else "erro", f"Conclusão: {cc.get('detalhe', '')}")
        if not cc.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": cc.get("detalhe", "")}}
            return
        concluido = not dry_run

    # sucesso — registra no histórico do painel e remove da lista se concluído
    if not dry_run:
        atividade = ("Conclusão de atendimento de retorno" if concluido
                     else "Movimentação de atendimento de retorno")
        historico_painel.registrar({
            "paj": paj, "paj_norm": paj_norm, "url_detalhe": rec.get("url_detalhe", ""),
            "origem": "retorno", "atividade": atividade,
            "movimentacao": fase, "tramitado_para": tram_caixa,
            "concluido": concluido, "detalhe": descricao[:300],
        })
        if concluido:
            store = _ler_store()
            store.pop(id_tramite, None)
            _gravar_store(store)

    yield _ev("ok", f"{paj} — executado com sucesso.")
    yield {"done": True, "resultado": {"ok": True, "concluido": concluido,
                                       "tramitado_para": tram_caixa, "mov_seq": mv.get("mov_seq")}}
