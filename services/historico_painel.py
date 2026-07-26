"""Histórico UNIFICADO de PAJs concluídos DENTRO do painel.

Um JSONL append-only (`OFICIO_GERAL/_historico_painel.jsonl`) que registra TODA
conclusão de trâmite da caixa de entrada efetivada pelo painel — venha da aba
Retornos (despacho + conclusão) ou da aba Arquivos SAT (juntada + conclusão) ou
de features futuras. Alimenta o submenu "Histórico de PAJs Concluídos" (visão
panorâmica simples: data/hora, PAJ, atividade, movimentação, destino).

Campos do registro:
  id, ts, paj, paj_norm, url_detalhe, origem ('retorno'|'sat'|...),
  atividade (texto legível), movimentacao (fase), tramitado_para (grupo/caixa),
  concluido (bool), detalhe

O nome do assistido NÃO é persistido no registro — é resolvido na leitura a partir
do metadata.json do PAJ (assim cobre também registros antigos).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import config

_ARQ = Path(config.OFICIO_GERAL) / "_historico_painel.jsonl"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _assistido_do_paj(paj_norm: str, cache: dict) -> str:
    """Nome do assistido lido do metadata.json do PAJ (com cache por chamada)."""
    if not paj_norm:
        return ""
    if paj_norm in cache:
        return cache[paj_norm]
    nome = ""
    try:
        p = Path(config.PAJS_DIR) / paj_norm / "metadata.json"
        if p.exists():
            meta = json.loads(p.read_text(encoding="utf-8"))
            nome = str(meta.get("assistido_caixa") or meta.get("assistido") or "").strip()
    except Exception:  # noqa: BLE001
        nome = ""
    cache[paj_norm] = nome
    return nome


def registrar(record: dict) -> dict:
    """Append de um registro (gera id/ts se faltarem)."""
    record.setdefault("id", uuid.uuid4().hex[:12])
    record.setdefault("ts", _now_iso())
    _ARQ.parent.mkdir(parents=True, exist_ok=True)
    with _ARQ.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def listar() -> list[dict]:
    """Todos os registros, mais recentes primeiro."""
    if not _ARQ.exists():
        return []
    out: list[dict] = []
    for linha in _ARQ.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            out.append(json.loads(linha))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    # Resolve o nome do assistido pelo metadata.json (cobre registros antigos).
    cache: dict = {}
    for r in out:
        if not r.get("assistido"):
            r["assistido"] = _assistido_do_paj(r.get("paj_norm") or "", cache)
    return out
