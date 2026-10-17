"""Tribunais cujas intimações entram na aba Prazos — o PADRÃO para incluir outros.

Cada tribunal tem (17/10/2026, pedido do Defensor):
  - `rotulo`: nome no filtro da tabela de Prazos ("Todos · TRF4 · JMU · …");
  - `selos`: o selo que aparece ao lado do número do processo, por instância
    (ex.: TRF4 → "JFPR" na 1ª instância, "TRF4" na 2ª);
  - `cor`: cor de fundo do selo (texto branco).

Todo caso de intimação levado à tela recebe `tribunal`, `instancia`, `selo` e
`selo_cor` por `marcar()`. Para um tribunal novo (ex.: SEEU): uma entrada aqui
+ a leitura das intimações dele marcando os casos com `marcar(caso, "SEEU", n)`
e somadas na tela como as da JMU (ver `ingestao.eproc_jmu.mesclar`).
"""

from __future__ import annotations

TRIBUNAIS: dict[str, dict] = {
    "TRF4": {"rotulo": "TRF4", "selos": {1: "JFPR", 2: "TRF4"}, "cor": "#1d4e89"},
    "JMU": {"rotulo": "JMU", "selos": {1: "JMU", 2: "STM"}, "cor": "#556b2f"},
    # "SEEU": {"rotulo": "SEEU", "selos": {1: "SEEU"}, "cor": "#6b3a8a"},  # futuro
}


def selo(tribunal: str, instancia: int = 1) -> tuple[str, str]:
    t = TRIBUNAIS.get(tribunal) or {}
    selos = t.get("selos") or {}
    return selos.get(instancia) or next(iter(selos.values()), tribunal), t.get("cor", "#6b7280")


def marcar(caso: dict, tribunal: str, instancia: int = 1) -> dict:
    """Marca o caso com tribunal/instância/selo — não sobrescreve marca existente."""
    caso.setdefault("tribunal", tribunal)
    caso.setdefault("instancia", instancia)
    rot, cor = selo(caso["tribunal"], caso["instancia"])
    caso.setdefault("selo", rot)
    caso.setdefault("selo_cor", cor)
    return caso


def para_tela(presentes: set[str]) -> list[dict]:
    """[{chave, rotulo}] dos tribunais com casos — botões do filtro."""
    return [{"chave": k, "rotulo": v["rotulo"]} for k, v in TRIBUNAIS.items() if k in presentes]
