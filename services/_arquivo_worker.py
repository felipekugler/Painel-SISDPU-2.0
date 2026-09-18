"""Worker de vida curta pra gravar um arquivo de texto (JSON) de forma
ATÔMICA FORA do processo do servidor — mesma medida provisória e mesmo
motivo de `services._docx_worker` (ver docstring lá; não repetido aqui).

Uso: `python services/_arquivo_worker.py <destino>`, com o CONTEÚDO na
entrada padrão (stdin, UTF-8). Grava via arquivo temporário + `os.replace`
(atômico) — igual ao padrão já usado em `services.intimacao_service.
_salvar_raw`/`services.config_painel_service._salvar_raw`, só que agora
rodando num processo sem socket de rede.

Saída: `OK` em stdout + código 0 em sucesso; `ERRO: <mensagem>` em stderr +
código 1 em falha."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("ERRO: uso: _arquivo_worker.py <destino>", file=sys.stderr)
        return 1

    destino = Path(sys.argv[1])
    conteudo = sys.stdin.buffer.read().decode("utf-8")

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(destino) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(conteudo)
        os.replace(tmp, str(destino))
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
