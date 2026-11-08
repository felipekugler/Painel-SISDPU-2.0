"""Limite de 9 MB por anexo do SISDPU (06/11/2026, pedido do usuário).

Todo anexo de movimentação passa por `ajustar_anexos` antes de ir ao SISDPU
(ponto único: `ingestao.sisdpu_movtram.movimentar_paj`, que atende tanto o
SIS-DPU 2.0 pelo MCP quanto a automação antiga). PDF acima do limite:

  1. é COMPRIMIDO (imagens a ~150 DPI, JPEG 60 — `pdf_compress_service`, mesmo
     algoritmo da skill /PDF-Comprimir_Dividir), mantendo o nome;
  2. se ainda passar, é DIVIDIDO por páginas inteiras em partes de tamanho
     parecido ("<nome> (parte 1 de 2).pdf" …), cada uma dentro do limite.

Antes disso o próprio servidor do SIS-DPU 2.0 cortava o arquivo, em partes
bem desiguais (ex.: 2,9 + 7,4 + 0,2 MB). O original nunca é alterado: o
resultado vai para uma pasta temporária do sistema, reaproveitada enquanto o
arquivo não mudar (o resumo da movimentação conjunta e a gravação real veem as
mesmas partes). Formatos que não são PDF passam como estão."""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
import time
from pathlib import Path

LIMITE_MB = 9
LIMITE_BYTES = LIMITE_MB * 1024 * 1024
# Folga para o que o servidor acrescenta/arredonda.
ALVO_BYTES = int(LIMITE_BYTES * 0.97)

_PASTA = Path(tempfile.gettempdir()) / "painel_sisdpu_anexos"
_VALIDADE_S = 2 * 24 * 3600


class AnexoGrandeDemais(Exception):
    """Não foi possível deixar o anexo dentro do limite (ex.: uma única página
    maior que 9 MB)."""


def _limpar_antigos() -> None:
    if not _PASTA.exists():
        return
    agora = time.time()
    for d in _PASTA.iterdir():
        try:
            if agora - d.stat().st_mtime > _VALIDADE_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _paginas_para_bytes(src, paginas: list[int]) -> bytes:
    import fitz

    with fitz.open() as d:
        for i in paginas:
            d.insert_pdf(src, from_page=i, to_page=i)
        return d.tobytes(garbage=4, deflate=True)


def _pesos_paginas(src) -> list[int]:
    """Peso PRÓPRIO de cada página: o conteúdo dela + as imagens que ela usa
    (cada imagem contada só na 1ª página em que aparece). Fontes e outros
    recursos compartilhados ficam de fora — medir cada página como PDF avulso
    os repetiria em todas e desequilibraria a divisão."""
    vistos: set[int] = set()
    pesos = []
    for pg in src:
        w = 1024
        with_contents = 0
        for x in pg.get_contents():
            with_contents += len(src.xref_stream_raw(x) or b"")
        w += with_contents
        for img in pg.get_images(full=True):
            x = img[0]
            if x in vistos:
                continue
            vistos.add(x)
            w += len(src.xref_stream_raw(x) or b"")
        pesos.append(w)
    return pesos


def _dividir(pdf: bytes, alvo: int) -> list[bytes]:
    """Divide em N partes de tamanho parecido (páginas inteiras, em ordem),
    aumentando N até todas caberem no alvo."""
    import fitz

    with fitz.open(stream=pdf, filetype="pdf") as src:
        total_pag = src.page_count
        pesos = _pesos_paginas(src)
        for i in range(total_pag):  # só as páginas pesadas precisam da conferência real
            if pesos[i] > alvo / 2 and len(_paginas_para_bytes(src, [i])) > alvo:
                raise AnexoGrandeDemais("uma das páginas sozinha passa de 9 MB")
        soma = sum(pesos)
        n = max(2, math.ceil(len(pdf) / alvo))
        while n <= total_pag:
            grupos: list[list[int]] = [[] for _ in range(n)]
            acumulado = 0.0
            for i, w in enumerate(pesos):
                k = min(n - 1, int((acumulado + w / 2) / (soma / n)))
                grupos[k].append(i)
                acumulado += w
            grupos = [g for g in grupos if g]
            partes = [_paginas_para_bytes(src, g) for g in grupos]
            if all(len(p) <= alvo for p in partes):
                return partes
            n += 1
    raise AnexoGrandeDemais("não foi possível dividir em partes de até 9 MB")


def _ajustar_um(a: dict) -> list[dict]:
    from services.pdf_compress_service import comprimir_pdf_bytes

    caminho = Path(a.get("caminho") or "")
    if not caminho.is_file() or caminho.suffix.lower() != ".pdf" or caminho.stat().st_size <= ALVO_BYTES:
        return [a]
    st = caminho.stat()
    chave = hashlib.sha1(f"{caminho.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    pasta = _PASTA / chave
    nome_base = Path(a.get("nome") or caminho.name).stem
    desc = (a.get("descricao") or "").strip()

    if not (pasta / "pronto").exists():
        shutil.rmtree(pasta, ignore_errors=True)
        pasta.mkdir(parents=True, exist_ok=True)
        pdf, _ = comprimir_pdf_bytes(caminho.read_bytes())
        if len(pdf) <= ALVO_BYTES:
            (pasta / f"{nome_base}.pdf").write_bytes(pdf)
        else:
            partes = _dividir(pdf, ALVO_BYTES)
            for i, p in enumerate(partes, start=1):
                (pasta / f"{nome_base} (parte {i} de {len(partes)}).pdf").write_bytes(p)
        (pasta / "pronto").write_text("ok", encoding="utf-8")

    saidas = sorted((p for p in pasta.glob("*.pdf")),
                    key=lambda p: int(p.stem.rsplit("(parte ", 1)[-1].split(" ")[0]) if "(parte " in p.stem else 0)
    novos = []
    for p in saidas:
        sufixo = p.stem[len(nome_base):].strip()  # "(parte 1 de 2)" ou ""
        novos.append({**a, "nome": p.name, "caminho": str(p), "tipo": "pdf",
                      "descricao": f"{desc} {sufixo}".strip() if desc else p.stem})
    return novos


def ajustar_anexos(anexos: list[dict] | None) -> list[dict]:
    """Devolve a lista de anexos com cada PDF dentro do limite do SISDPU
    (comprimido e, se preciso, dividido). Levanta `AnexoGrandeDemais`."""
    if not anexos:
        return list(anexos or [])
    _limpar_antigos()
    saida: list[dict] = []
    for a in anexos:
        try:
            saida += _ajustar_um(a)
        except AnexoGrandeDemais as e:
            nome = (a or {}).get("nome") or Path((a or {}).get("caminho") or "").name
            raise AnexoGrandeDemais(f'O anexo "{nome}" não coube no limite de {LIMITE_MB} MB do SISDPU: {e}.') from e
    return saida
