"""Classificação CENTRAL dos PAJs da caixa de entrada por CATEGORIA de submenu.

Fonte única da regra que decide a qual "submenu especializado" um PAJ da caixa
pertence, a partir da descrição da tramitação que o colocou na caixa:

    ""        → PAJ geral (aparece no dashboard "PAJs")
    "retorno" → Atendimento de retorno (aparece na aba Retornos)
    "sat"     → Juntada dos documentos do SAT Central (aparece na aba Arquivos SAT)

Decisão do usuário (2026-07-24): um PAJ que pertence a um submenu especializado
NÃO aparece no dashboard — só no submenu. O dashboard filtra por categoria == "".

O valor é gravado em `metadata.json["categoria_caixa"]` pela sincronização
(ingestao/sincronizador), e lido pelo dashboard (services/paj_service.listar_pajs).

Fail-safe: quando a descrição não casa nenhuma regra, retorna "" (geral) — ou seja,
o comportamento padrão é mostrar no dashboard (nada some por engano).
"""

from __future__ import annotations

import re
import unicodedata

# Rótulos das categorias (chave → nome exibível).
CATEGORIAS = {
    "": "Geral",
    "retorno": "Atendimento de retorno",
    "sat": "Juntada SAT Central",
}

# Trechos-alvo (normalizados) que definem cada categoria. Mesmos usados pelos
# filtros das features (retornos_service.DESCRICAO_ALVO / sat_service.DESCRICAO_ALVO).
_ALVOS = [
    ("retorno", "atendimento de retorno"),
    ("sat", "para juntada dos documentos do sat central"),
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def classificar(*textos: str) -> str:
    """Retorna a categoria ('', 'retorno' ou 'sat') a partir de um ou mais textos
    da caixa (descrição da tramitação, linha inteira, etiqueta...). O primeiro alvo
    que casar vence. Sem casamento → "" (geral)."""
    alvo = _norm(" | ".join(t for t in textos if t))
    if not alvo:
        return ""
    for categoria, trecho in _ALVOS:
        if trecho in alvo:
            return categoria
    return ""


def eh_de_submenu(categoria: str) -> bool:
    """True se a categoria pertence a um submenu especializado (não vai ao dashboard)."""
    return bool(categoria) and categoria in CATEGORIAS and categoria != ""
