"""Seletor de arquivos que abre JÁ NA PASTA DO PAJ ("+ Adicionar Externo" de
Ações no Eproc e de Ações no SISDPU — 06/11/2026, pedido do usuário).

O `<input type="file">` do navegador não deixa a página escolher a pasta
inicial (ele sempre volta para a última usada). Como o painel roda no próprio
computador do Defensor, o servidor abre a janela NATIVA do Windows ("Abrir")
com a pasta do PAJ como inicial e devolve os arquivos escolhidos. O front
baixa cada um por um token (só os caminhos escolhidos nesta janela ficam
acessíveis — nada de caminho arbitrário na URL) e segue o fluxo normal de
anexo externo, como se tivesse vindo do `<input>`.

A janela roda num subprocesso (tkinter fora da thread principal do servidor é
instável); se ele falhar por qualquer motivo, o front volta ao seletor do
navegador."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

from config import PAJS_DIR

_SCRIPT = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
root.update()
arqs = filedialog.askopenfilenames(parent=root, initialdir=sys.argv[1], title=sys.argv[2])
root.destroy()
sys.stdout.write(json.dumps(list(arqs)))
"""

_tokens: dict[str, Path] = {}
_lock = asyncio.Lock()
_MAX_TOKENS = 200


def _pasta_inicial(paj_norm: str) -> Path | None:
    if not re.fullmatch(r"PAJ-\d{4}-\d{3}-\d{5}", paj_norm or ""):
        return None
    pasta = PAJS_DIR / paj_norm
    return pasta if pasta.is_dir() else None


def _rodar_dialogo(pasta: Path, titulo: str) -> list[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run([sys.executable, "-c", _SCRIPT, str(pasta), titulo],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=1800, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "falha ao abrir a janela").strip()[-300:])
    return json.loads(r.stdout or "[]")


async def escolher(paj_norm: str, titulo: str = "") -> dict:
    """Abre a janela "Abrir" na pasta do PAJ. Retorna
    {ok, arquivos: [{token, nome, tamanho}]} — lista vazia se o Defensor
    cancelar — ou {ok: False, motivo, erro} para o front usar o seletor do
    navegador."""
    pasta = _pasta_inicial(paj_norm)
    if not pasta:
        return {"ok": False, "motivo": "sem_pasta", "erro": "Pasta do PAJ não encontrada."}
    if _lock.locked():
        return {"ok": False, "motivo": "ocupado", "erro": "Já há uma janela de seleção aberta."}
    async with _lock:
        try:
            caminhos = await asyncio.to_thread(_rodar_dialogo, pasta, titulo or "Escolher arquivo(s)")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "motivo": "falha", "erro": f"{e}"[:300]}
    if len(_tokens) > _MAX_TOKENS:
        _tokens.clear()
    arquivos = []
    for c in caminhos:
        p = Path(c)
        if not p.is_file():
            continue
        token = secrets.token_urlsafe(16)
        _tokens[token] = p
        arquivos.append({"token": token, "nome": p.name, "tamanho": p.stat().st_size})
    return {"ok": True, "arquivos": arquivos}


def caminho(token: str) -> Path | None:
    p = _tokens.get(token or "")
    return p if p and p.is_file() else None
