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
    return out
