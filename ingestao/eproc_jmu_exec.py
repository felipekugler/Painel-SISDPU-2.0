"""Executa UMA função de `ingestao.eproc_mcp_client` com o MCP da JMU — num
PROCESSO À PARTE (chamado por `ingestao.eproc_jmu.executar_mcp`).

O painel carrega o MCP do TRF4 como biblioteca (módulos `server`, `client`,
`parsers`, `mni`, `triagem`, `auth`); a cópia da JMU tem os MESMOS nomes. Aqui
os módulos da JMU são importados ANTES do painel — `eproc_mcp_client` (que
importa esses módulos pelo nome) passa a falar com o e-proc da JMU sem mudar
uma linha. `PAINEL_MCP_JMU_FILHO=1` impede que a função desvie de novo para cá.

Entrada: nome da função em argv[1]; argumentos (JSON) no stdin.
Saída: UMA linha JSON no stdout (o log do MCP vai para o stderr).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["PAINEL_MCP_JMU_FILHO"] = "1"

JMU_DIR = Path("C:/Claude/MCP/eproc-jmu")
PAINEL_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(JMU_DIR))
import client  # noqa: E402,F401  (MCP da JMU — fixa os nomes em sys.modules)
import server  # noqa: E402,F401

sys.path.insert(1, str(PAINEL_DIR))
from ingestao import eproc_mcp_client  # noqa: E402

PERMITIDAS = {"consultar_processo", "cabecalho_completo", "lembretes_do_processo",
              "triagem_processo", "baixar_documentos_evento", "resolver_acesso_integra", "status"}


async def main() -> object:
    funcao = sys.argv[1] if len(sys.argv) > 1 else ""
    if funcao not in PERMITIDAS:
        return {"erro": f"Função não permitida na JMU: {funcao!r}"}
    kwargs = json.loads(sys.stdin.read() or "{}")
    return await getattr(eproc_mcp_client, funcao)(**kwargs)


if __name__ == "__main__":
    try:
        res = asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        res = {"erro": f"e-proc da JMU: {type(e).__name__}: {e}"[:400]}
    # UTF-8 explícito: no Windows o stdout em pipe sai na codificação do sistema
    # (cp1252) e o painel lia "Ação" como "A��o" (17/10/2026).
    sys.stdout.buffer.write((json.dumps(res, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    sys.stdout.flush()
