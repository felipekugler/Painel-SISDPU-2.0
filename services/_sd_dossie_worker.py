"""Worker de vida curta que monta um Dossiê do Seguro-Desemprego Web FORA do
processo do servidor — mesma medida provisória (FortiClient) de
`services._docx_worker`/`services._arquivo_worker` (ver docstring lá).

Uso: `python services/_sd_dossie_worker.py <montador.py> <n_pacotes> <destino> [args do montador...]`,
com os N PACOTES JSON dos coletores na entrada padrão (stdin, UTF-8),
separados por um byte NUL (JSON nunca contém NUL cru — controle é escapado).

Grava cada pacote num temporário, roda o montador da skill como
`python <montador> <pacote1> [<pacote2>] <destino> ...` (`sd_dossie.py`/
`sd_req_dossie.py` recebem 1 pacote; `sd_dossie_unificado.py` do SD-Dossies
recebe 2: vínculos e requerimentos, nessa ordem) e APAGA os pacotes ao final
(contêm dados pessoais), com sucesso ou não.

Saída: repassa o stdout do montador (JSON com "arquivo") + código 0; em falha,
`ERRO: <mensagem>` em stderr + código 1."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if len(sys.argv) < 4:
        print("ERRO: uso: _sd_dossie_worker.py <montador.py> <n_pacotes> <destino> [args...]",
              file=sys.stderr)
        return 1
    montador, n, destino, extras = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4:]
    conteudos = sys.stdin.buffer.read().split(b"\x00")
    if len(conteudos) != n:
        print(f"ERRO: esperados {n} pacote(s), recebidos {len(conteudos)}", file=sys.stderr)
        return 1

    pacotes: list[str] = []
    try:
        for c in conteudos:
            fd, caminho = tempfile.mkstemp(prefix="sd_pacote_", suffix=".json")
            pacotes.append(caminho)
            with os.fdopen(fd, "wb") as fh:
                fh.write(c)
        r = subprocess.run([sys.executable, montador, *pacotes, destino, *extras],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()[-1500:]
            print(msg if msg.startswith("ERRO") else f"ERRO: {msg}", file=sys.stderr)
            return 1
        sys.stdout.write(r.stdout)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        for caminho in pacotes:
            try:
                os.remove(caminho)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
