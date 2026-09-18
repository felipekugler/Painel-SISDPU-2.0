"""Resolve o vínculo entre um PAJ e o(s) processo(s) judicial(is) — usado pela
página "Movimentar Eproc + SISDPU" (peticionar/movimentar em conjunto, ver
`routes/movimentar_conjunto.py`) quando o minimenu só fornece UM dos dois
identificadores (PAJ ou número do processo) e o outro precisa ser descoberto.

Cruza 3 fontes, que podem DIVERGIR entre si — por isso as funções aqui nunca
"escolhem" um candidato sozinhas quando há mais de um: devolvem a lista toda e
quem chama decide (a página mostra um dropdown com aviso quando há ambiguidade,
pedido do usuário 2026-09-11):
- o cadastro do PAJ sincronizado (`metadata.json` -> `processos_judiciais`,
  gravado direto do scrape do SISDPU — ver `retornos_service._extrair_processos`,
  a fonte mais confiável das três);
- o cache de "Prazos e-proc" (`ingestao/eproc_intimacoes.py` — PAJ extraído do
  texto livre do LEMBRETE do e-proc, pode estar desatualizado ou ambíguo);
- o registro de "Intimações" (`services/intimacao_service.py` — o mesmo cache
  acima, mas com um vínculo MANUAL por cima quando o usuário já corrigiu um
  vínculo que o lembrete trouxe errado — `definir_vinculo_manual`)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import config


def _normalizar(s: str | None) -> str:
    """Só os dígitos do número CNJ — mesma normalização duplicada em vários
    módulos do projeto (retornos_service/acoes_eproc_service/intimacao_service),
    reimplementada aqui pelo mesmo motivo: evitar um import cruzado só por uma
    linha de regex."""
    return re.sub(r"\D", "", s or "")


def _ler_json(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# Índice reverso processo(dígitos) -> [{"paj_norm","paj_label","juizo"}, ...],
# construído varrendo TODOS os metadata.json — mesmo princípio de cache curto
# por TTL usado em `paj_service.listar_pajs()` (a varredura completa, ~257
# PAJs hoje, é rápida; o TTL só evita repeti-la a cada clique/render).
_INDEX_TTL_SEG = 10.0
_index_cache: dict = {"ts": 0.0, "por_processo": {}}


def _indice_processo_para_pajs() -> dict[str, list[dict]]:
    agora = time.monotonic()
    if _index_cache["por_processo"] and agora - _index_cache["ts"] < _INDEX_TTL_SEG:
        return _index_cache["por_processo"]

    from services.retornos_service import _extrair_processos

    indice: dict[str, list[dict]] = {}
    if config.PAJS_DIR.exists():
        for pasta in config.PAJS_DIR.iterdir():
            if not pasta.is_dir():
                continue
            metadata = _ler_json(pasta / "metadata.json")
            if not metadata:
                continue
            paj_norm = pasta.name
            paj_label = metadata.get("paj") or ""
            for p in _extrair_processos(metadata):
                chave = _normalizar(p["numero"])
                if not chave:
                    continue
                candidatos = indice.setdefault(chave, [])
                if not any(c["paj_norm"] == paj_norm for c in candidatos):
                    candidatos.append({"paj_norm": paj_norm, "paj_label": paj_label, "juizo": p.get("juizo") or ""})

    _index_cache["ts"] = agora
    _index_cache["por_processo"] = indice
    return indice


def resolver_pajs_de_processo(numero_processo: str) -> list[dict]:
    """PAJ(s) candidato(s) pra este número de processo — pode devolver mais de
    1 (fontes divergentes, ou o mesmo processo genuinamente vinculado a mais
    de um PAJ — ex. litisconsórcio com PAJs distintos por assistido) ou
    nenhum. Cada item: {"paj_norm", "paj_label", "juizo"}."""
    chave = _normalizar(numero_processo)
    if not chave:
        return []
    from services import intimacao_service

    candidatos: dict[str, dict] = {}

    # 1) cadastro do PAJ sincronizado (reverso) — fonte mais confiável.
    for c in _indice_processo_para_pajs().get(chave, []):
        candidatos[c["paj_norm"]] = c

    # 2) cache de "Prazos e-proc" — TODAS as ocorrências do processo (não só a
    # 1ª categoria em que aparece — categorias diferentes podem trazer PAJ
    # diferente pro MESMO processo, o que é em si um sinal de ambiguidade).
    from ingestao import eproc_intimacoes

    dados = eproc_intimacoes.carregar()
    for casos in (dados.get("secoes") or {}).values():
        for c in casos:
            if _normalizar(c.get("processo") or "") != chave:
                continue
            paj_norm = intimacao_service.paj_para_norm(c.get("paj"))
            if paj_norm and paj_norm not in candidatos:
                candidatos[paj_norm] = {"paj_norm": paj_norm, "paj_label": c.get("paj") or "", "juizo": ""}

    # 3) "Intimações" — inclui vínculo MANUAL (definir_vinculo_manual), que
    # tem prioridade sobre o texto do lembrete quando o usuário já corrigiu.
    caso = intimacao_service.obter_caso(numero_processo)
    if caso and caso.get("paj_norm"):
        candidatos.setdefault(caso["paj_norm"], {
            "paj_norm": caso["paj_norm"], "paj_label": caso.get("paj") or "", "juizo": "",
        })

    return list(candidatos.values())


def resolver_processos_de_paj(paj_norm: str) -> list[dict]:
    """Processo(s) judicial(is) candidato(s) deste PAJ — normalmente vem
    completo do próprio cadastro (`processos_judiciais`, já pode ter mais de 1
    de verdade); soma (nunca substitui) com o que "Prazos e-proc"/"Intimações"
    tiverem detectado pra este PAJ mas que porventura ainda não conste do
    cadastro (sync desatualizada). Cada item: {"numero", "juizo"}."""
    if not paj_norm:
        return []
    from services import intimacao_service
    from services.retornos_service import _extrair_processos

    metadata = _ler_json(config.PAJS_DIR / paj_norm / "metadata.json")
    candidatos: dict[str, dict] = {}
    for p in _extrair_processos(metadata):
        chave = _normalizar(p["numero"])
        if chave:
            candidatos[chave] = {"numero": p["numero"], "juizo": p.get("juizo") or ""}

    from ingestao import eproc_intimacoes

    dados = eproc_intimacoes.carregar()
    for casos in (dados.get("secoes") or {}).values():
        for c in casos:
            if intimacao_service.paj_para_norm(c.get("paj")) != paj_norm:
                continue
            chave = _normalizar(c.get("processo") or "")
            if chave:
                candidatos.setdefault(chave, {"numero": c.get("processo") or "", "juizo": ""})

    return list(candidatos.values())
