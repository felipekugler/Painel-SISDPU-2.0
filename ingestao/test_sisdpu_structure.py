"""Testes para garantir que a estrutura de captura de movimentações está correta.

Este arquivo testa que:
1. Movimentações têm os campos corretos (seq, data, movimentacao, fases, descricao, usuario)
2. Fases começam com "°" ou contêm descrições de fase (Gabinete, MOVIMENTAÇÃO MANUAL, etc)
3. Movimentação contém o tipo de movimento
4. Seq é numérico e ordem decrescente (mais recente primeiro)

Se algum teste falhar durante sincronização, isso indica que a ordem das
colunas no SISDPU mudou ou o código foi alterado incorretamente.
"""

from pathlib import Path
import json
import re


def test_movimentacao_structure(metadata_path: Path) -> list[str]:
    """Valida estrutura de movimentações em um metadata.json.

    Returns: lista de problemas encontrados (vazio = OK)
    """
    problemas = []

    if not metadata_path.exists():
        return [f"Arquivo nao encontrado: {metadata_path}"]

    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
    except json.JSONDecodeError as e:
        return [f"JSON invalido: {e}"]

    sisdpu_raw = metadata.get("sisdpu_raw", {}) or {}
    movimentacoes = sisdpu_raw.get("movimentacoes", []) or []

    if not movimentacoes:
        return []  # OK, pode nao ter movimentacoes

    # Verifica estrutura geral
    campos_obrigatorios = {"seq", "data", "movimentacao", "fases", "descricao"}

    for i, mov in enumerate(movimentacoes):
        # Verifica campos obrigatorios
        para_verificar = campos_obrigatorios - set(mov.keys())
        if para_verificar:
            problemas.append(f"Mov #{i}: campos faltando {para_verificar}")
            continue

        seq = mov.get("seq")
        fases = mov.get("fases", "").strip()
        movimentacao = mov.get("movimentacao", "").strip()
        descricao = mov.get("descricao", "").strip()

        # Seq deve ser numerico
        if not (isinstance(seq, (int, str)) and str(seq).isdigit()):
            problemas.append(f"Mov #{i}: seq nao e numerico (valor: {seq})")

        # CRITICO: fases deve comear com "°" (simbolo de grau)
        if fases and not fases.startswith("°"):
            problemas.append(
                f"Mov #{i}: fases NAO comea com grau "
                f"(ERRO CRITICO - pode indicar inversao de colunas). "
                f"Valor: {fases[:50]}"
            )

        # CRITICO: movimentacao NAO deve comecar com "°"
        if movimentacao.startswith("°"):
            problemas.append(
                f"Mov #{i}: movimentacao comea com grau "
                f"(ERRO CRITICO - colunas invertidas!). "
                f"Valor: {movimentacao[:50]}"
            )

        # Descricao deve ter conteudo
        if not descricao:
            problemas.append(f"Mov #{i}: descricao vazia")

    # Verifica ordem: seq deve estar em ordem DECRESCENTE
    if movimentacoes:
        seqs = [int(m.get("seq", 0) or 0) for m in movimentacoes]
        if seqs != sorted(seqs, reverse=True):
            problemas.append(
                f"Movimentacoes NAO estao em ordem decrescente por seq. "
                f"Ordem atual: {seqs}"
            )

    return problemas


def test_all_pajs():
    """Testa todos os PAJs sincronizados."""
    pajs_dir = Path(r"C:\Claude\DPU\PREV4-Painel\PAJs")

    if not pajs_dir.exists():
        print(f"[ERRO] Diretorio nao encontrado: {pajs_dir}")
        return

    total = 0
    com_problemas = 0
    problemas_criticos = 0

    for paj_folder in sorted(pajs_dir.glob("PAJ-*")):
        if not paj_folder.is_dir():
            continue

        total += 1
        metadata_file = paj_folder / "metadata.json"

        problemas = test_movimentacao_structure(metadata_file)

        if problemas:
            com_problemas += 1
            print(f"\n[AVISO] {paj_folder.name}:")
            for p in problemas:
                if "ERRO CRITICO" in p:
                    problemas_criticos += 1
                    print(f"   [CRITICO] {p}")
                else:
                    print(f"   [AVISO] {p}")
        else:
            print(f"[OK] {paj_folder.name}")

    print(f"\n{'='*70}")
    print(f"Total de PAJs testados: {total}")
    print(f"Com problemas: {com_problemas}")
    print(f"Erros criticos (inversao de colunas): {problemas_criticos}")

    if problemas_criticos > 0:
        print(f"\n[FALHA] Deteccao de inversao de colunas!")
        print("Isso significa que sisdpu_client.py esta capturando as colunas erradas.")
        exit(1)
    elif com_problemas > 0:
        print(f"\n[AVISO] Alguns problemas encontrados, mas nenhum critico.")
        exit(1)
    else:
        print(f"\n[OK] Todos os PAJs estao com estrutura correta!")
        exit(0)


def validar_paj_apos_sync(paj_norm: str) -> tuple[bool, list[str]]:
    """Valida um PAJ apos sincronizacao. Retorna (ok, problemas).

    Use isto APÓS salvar metadata.json durante sincronizacao.
    Retorna True se OK, False se houver erros criticos.
    """
    pajs_dir = Path(r"C:\Claude\DPU\PREV4-Painel\PAJs")
    paj_folder = pajs_dir / paj_norm

    if not paj_folder.exists():
        return False, [f"Pasta do PAJ nao encontrada: {paj_folder}"]

    metadata_file = paj_folder / "metadata.json"
    problemas = test_movimentacao_structure(metadata_file)

    tem_critico = any("ERRO CRITICO" in p for p in problemas)
    return not tem_critico, problemas


if __name__ == "__main__":
    test_all_pajs()
