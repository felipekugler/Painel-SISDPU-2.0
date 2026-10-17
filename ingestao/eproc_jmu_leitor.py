"""Leitura das intimações do e-proc da JMU — roda num PROCESSO À PARTE.

Chamado por `ingestao.eproc_jmu.ler_intimacoes()`. O MCP da JMU
(`C:\\Claude\\MCP\\eproc-jmu`) é uma cópia do eproc-trf4 com os mesmos nomes de
módulo (`server`, `client`, `parsers`…); os dois não convivem no mesmo processo
Python (o primeiro importado venceria para sempre). Aqui os módulos da JMU são
importados ANTES de qualquer coisa do painel, e a leitura reaproveita o mesmo
`eproc_intimacoes_mcp._ler_todos_corpo` da aba Prazos (cores pelo
`css/cor-capa.php` da JMU, lembretes, agrupamento por processo).

Uso: python eproc_jmu_leitor.py <arquivo-json-das-secoes-anteriores>
Saída: UMA linha JSON no stdout (o log do MCP vai para o stderr).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

JMU_DIR = Path("C:/Claude/MCP/eproc-jmu")
PAINEL_DIR = Path(__file__).resolve().parents[1]

# 1º os módulos do MCP da JMU (ficam em sys.modules com estes nomes)…
sys.path.insert(0, str(JMU_DIR))
import client as mcp_client  # noqa: E402
import parsers  # noqa: E402
import server  # noqa: E402

# …depois o painel (que, ao ser importado, põe o eproc-trf4 no sys.path — sem
# efeito: `server`/`client`/`parsers` já estão carregados, os da JMU).
sys.path.insert(1, str(PAINEL_DIR))
from ingestao import eproc_intimacoes_mcp  # noqa: E402


async def main() -> dict:
    anteriores = {}
    if len(sys.argv) > 1:
        try:
            anteriores = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            anteriores = {}
    cli = await server._get_client(1)
    estados = [e for e, _ in await server._intimacao_estados(cli)]
    # A JMU lista uma seção por UF, todas com o mesmo conteúdo — a "JMU" basta.
    estado = "JMU" if "JMU" in estados else (estados[0] if estados else "JMU")
    return await eproc_intimacoes_mcp._ler_todos_corpo(
        server=server, mcp_client=mcp_client, parsers=parsers,
        estado=estado, secoes_anteriores=anteriores)


if __name__ == "__main__":
    try:
        res = asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "erro": f"{type(e).__name__}: {e}"[:400]}
    # UTF-8 explícito: no Windows o stdout em pipe sai na codificação do sistema
    # (cp1252) e o painel lia "Ação" como "A��o" (17/10/2026).
    sys.stdout.buffer.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()
