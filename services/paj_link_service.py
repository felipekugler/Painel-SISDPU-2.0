"""Resolve a URL de detalhe de um PAJ no SISDPU (para abrir como link).

Não temos o `idProcesso` interno do SISDPU a partir do número do PAJ — só o número
(AAAA/UUU-NNNNN). A URL de detalhe (`detalhamentoProcesso.xhtml?...idProcesso=X`) é
STATELESS (só depende do cookie de sessão), mas exige esse id. Então resolvemos o id
pela BUSCA global do SISDPU (o "método da caixa de pesquisa", já implementado em
`sisdpu_client.buscar_paj_global`), UMA vez por PAJ, e guardamos em cache.

A URL resolvida é devolvida ao navegador do usuário (nova aba), onde ele já está
logado no SISDPU — por ser stateless, abre direto no detalhamento do PAJ.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime
from pathlib import Path

_CACHE_FILE = Path(__file__).resolve().parents[1].parent / "dados" / "_paj_urls.json"
_RE_PAJ = re.compile(r"(\d{4})/(\d{3})-(\d{5})")

# Serializa as resoluções: `sisdpu_client` usa uma página global; chamadas
# concorrentes se atrapalhariam.
_lock = asyncio.Lock()


def _ler_cache() -> dict:
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _gravar_cache(cache: dict) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse(paj: str) -> tuple[str, str, str] | None:
    """'2026/029-00136' → (ano, unidade, numero). None se não casar o formato."""
    m = _RE_PAJ.search(paj or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


async def resolver(paj: str, forcar: bool = False) -> dict:
    """Retorna {ok, url} do detalhe do PAJ no SISDPU (via cache ou busca).

    `forcar=True` ignora o cache e re-resolve. Em falha: {ok:False, erro}."""
    partes = _parse(paj)
    if not partes:
        return {"ok": False, "erro": f"Número de PAJ inválido: {paj!r} (esperado AAAA/UUU-NNNNN)."}
    ano, unidade, numero = partes
    chave = f"{ano}/{unidade}-{numero}"

    cache = _ler_cache()
    if not forcar and chave in cache and cache[chave].get("url"):
        return {"ok": True, "url": cache[chave]["url"], "cache": True}

    async with _lock:
        # re-checa dentro do lock (outra requisição pode ter resolvido enquanto esperávamos)
        cache = _ler_cache()
        if not forcar and chave in cache and cache[chave].get("url"):
            return {"ok": True, "url": cache[chave]["url"], "cache": True}

        from ingestao import sisdpu2_fonte, sisdpu2_mcp_client
        if sisdpu2_fonte.fonte_mcp_ativa() and sisdpu2_mcp_client.conectado():
            # Fonte SIS-DPU 2.0: o id vem do MCP (mesmo id do legado), sem navegador.
            try:
                import asyncio
                rr = await asyncio.to_thread(sisdpu2_mcp_client.chamar, "processo_resolver", {"numero": chave})
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "erro": f"Falha ao buscar o PAJ no SIS-DPU 2.0: {e}"}
            if not isinstance(rr, dict) or not rr.get("idProcesso"):
                return {"ok": False, "erro": "PAJ não localizado no SIS-DPU 2.0."}
            url = sisdpu2_fonte.URL_DETALHE_LEGADO.format(id=rr["idProcesso"])
            cache[chave] = {"url": url, "resolvido_em": datetime.now().isoformat(timespec="seconds")}
            with contextlib.suppress(Exception):
                _gravar_cache(cache)
            return {"ok": True, "url": url, "cache": False}

        from ingestao import sisdpu_client
        try:
            r = await sisdpu_client.buscar_paj_global(
                numero=numero, ano=ano, unidade=unidade,
                forcar_sessao=False, esperas_curtas=True, somente_url=True,
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "erro": f"Falha ao buscar o PAJ no SISDPU: {type(e).__name__}: {e}"}

        url = (r or {}).get("url", "")
        if not url:
            return {"ok": False, "erro": (r or {}).get("erro") or "PAJ não localizado no SISDPU."}

        cache[chave] = {"url": url, "resolvido_em": datetime.now().isoformat(timespec="seconds")}
        with contextlib.suppress(Exception):
            _gravar_cache(cache)
        return {"ok": True, "url": url, "cache": False}
