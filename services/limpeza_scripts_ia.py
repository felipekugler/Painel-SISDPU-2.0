"""Remove da pasta do PAJ os scripts auxiliares que a IA escreve para si mesma
durante uma sessão (ex.: `_gerar_docx.py` para chamar o engine de formatação,
`organizar_PAJ-….py` para montar a pasta Ajuizamento). Não são conteúdo do PAJ
— o Defensor encontrava esses .py soltos na pasta (16/10/2026).

As instruções já pedem que a IA os crie na pasta temporária do sistema; esta
limpeza, chamada ao fim de cada sessão, garante o resultado mesmo se ela não
obedecer. Só olha a raiz do PAJ e as pastas que as sessões de IA escrevem —
nunca "Arquivos" (anexos baixados do SISDPU).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from config import PAJS_DIR

_EXTENSOES = {".py", ".pyc", ".ps1", ".bat", ".cmd", ".sh"}
_PASTAS = ("", "Peças IA", "Ajuizamento")


def limpar_scripts_auxiliares(paj_norm: str) -> list[str]:
    """Apaga os scripts auxiliares e devolve os nomes removidos."""
    pasta = PAJS_DIR / paj_norm
    removidos: list[str] = []
    for sub in _PASTAS:
        alvo = pasta / sub if sub else pasta
        if not alvo.is_dir():
            continue
        for f in alvo.iterdir():
            if f.is_file() and f.suffix.lower() in _EXTENSOES:
                with contextlib.suppress(OSError):
                    f.unlink()
                    removidos.append(str(Path(sub) / f.name) if sub else f.name)
        cache = alvo / "__pycache__"
        if cache.is_dir():
            with contextlib.suppress(OSError):
                for f in cache.iterdir():
                    f.unlink()
                cache.rmdir()
    return removidos
