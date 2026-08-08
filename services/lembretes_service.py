"""Lembretes por PAJ — anotações coloridas nos moldes da ferramenta de
"Lembretes" do e-proc (Cadastro de Lembretes Externos): descrição livre +
cor, num mini-painel sempre visível na página do PAJ, e um ícone com
tooltip no dashboard (igual ao já usado em Prazos, vide prazos.html).

Diferenças propositais em relação ao e-proc — aqui o Painel é uma ferramenta
pessoal/local (não multiusuário):
  - sem campo "Destino" (não há para quem compartilhar);
  - sem "Período de Validade" (fica para uma iteração futura, se fizer falta);
  - a remoção é definitiva (o e-proc só "desativa"/soft-delete).

Persistência: cada PAJ guarda os seus lembretes em `LEMBRETES.json`, na
própria pasta — mesmo padrão do NOTAS.md (paj_service.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR

ARQUIVO = "LEMBRETES.json"

# Mesma paleta do modal "Cadastro de Lembretes Externos" do e-proc — cores
# extraídas diretamente do DOM do formulário (radios "Cor do Lembrete").
CORES: dict[str, str] = {
    "amarelo": "#efef8f",
    "azul": "#87adcd",
    "laranja": "#f5b574",
    "verde": "#a7eda7",
    "vermelho": "#db8080",
}
COR_PADRAO = "amarelo"


def _validar_paj_norm(paj_norm: str) -> Path | None:
    """Valida paj_norm e retorna a pasta (ou None se inválido).

    Mesma lógica de `services.paj_service._validar_paj_norm`, duplicada aqui
    de propósito: este módulo não importa paj_service (e vice-versa, ele
    importa este) para não criar dependência circular.
    """
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return None
    if not paj_norm.startswith("PAJ-"):
        return None
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not pasta.is_dir():
        return None
    return pasta


def _ler(pasta: Path) -> list[dict]:
    arquivo = pasta / ARQUIVO
    if not arquivo.exists():
        return []
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        return dados if isinstance(dados, list) else []
    except Exception:
        return []


def _salvar(pasta: Path, lembretes: list[dict]) -> None:
    (pasta / ARQUIVO).write_text(
        json.dumps(lembretes, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ordenar(lembretes: list[dict]) -> list[dict]:
    """Mais recente (criado OU editado) primeiro — mesma ordem do
    mini-painel do e-proc ("esquerda = mais recente")."""
    return sorted(lembretes, key=lambda l: l.get("atualizado_em", ""), reverse=True)


def ler_lembretes_pasta(pasta: Path) -> list[dict]:
    """Variante que recebe a pasta já resolvida — usada por paj_service, que
    já percorre PAJS_DIR e tem o Path em mãos (evita revalidar paj_norm por
    PAJ, dentro do loop de listar_pajs())."""
    return _ordenar(_ler(pasta))


def listar_lembretes(paj_norm: str) -> list[dict]:
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return []
    return ler_lembretes_pasta(pasta)


def criar_lembrete(paj_norm: str, descricao: str, cor: str) -> dict | None:
    pasta = _validar_paj_norm(paj_norm)
    descricao = (descricao or "").strip()
    if pasta is None or not descricao:
        return None
    if cor not in CORES:
        cor = COR_PADRAO

    agora = datetime.now().isoformat(timespec="seconds")
    lembrete = {
        "id": uuid.uuid4().hex,
        "descricao": descricao,
        "cor": cor,
        "criado_em": agora,
        "atualizado_em": agora,
    }
    lembretes = _ler(pasta)
    lembretes.append(lembrete)
    _salvar(pasta, lembretes)
    return lembrete


def atualizar_lembrete(paj_norm: str, lembrete_id: str, descricao: str, cor: str) -> dict | None:
    pasta = _validar_paj_norm(paj_norm)
    descricao = (descricao or "").strip()
    if pasta is None or not descricao:
        return None
    if cor not in CORES:
        cor = COR_PADRAO

    lembretes = _ler(pasta)
    for lembrete in lembretes:
        if lembrete.get("id") == lembrete_id:
            lembrete["descricao"] = descricao
            lembrete["cor"] = cor
            lembrete["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            _salvar(pasta, lembretes)
            return lembrete
    return None


def remover_lembrete(paj_norm: str, lembrete_id: str) -> bool:
    pasta = _validar_paj_norm(paj_norm)
    if pasta is None:
        return False

    lembretes = _ler(pasta)
    restantes = [l for l in lembretes if l.get("id") != lembrete_id]
    if len(restantes) == len(lembretes):
        return False
    _salvar(pasta, restantes)
    return True
