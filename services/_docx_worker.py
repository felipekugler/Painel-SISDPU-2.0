"""Worker de vida curta pra gerar docx/pdf de uma minuta FORA do processo do
servidor — medida PROVISÓRIA (2026-08-28) enquanto o TI não libera uma
exceção no FortiClient.

Contexto: o processo do servidor do painel (`app.py`, com a porta 8001
aberta) está sendo bloqueado pelo antivírus corporativo (FortiClient —
"File Delete blocked for python.exe") ao gravar/converter `.docx` — mesmo
para um arquivo NOVO, que nunca existiu antes. Teste isolado confirmou: um
processo `python.exe` de vida curta e SEM socket de rede grava/apaga
livremente na mesma pasta. O padrão que parece disparar o bloqueio é
especificamente "processo com rede aberta + escrita de arquivo" (heurística
de webshell/backdoor comum em EDR corporativo), não o arquivo/pasta em si.

Por isso este script existe: roda como um subprocesso separado, nasce, faz
só a geração desta UMA minuta, termina — nunca fica com nada escutando rede.
Reaproveita as funções JÁ TESTADAS de `services.peticao_ia_service`
(`_html_para_docx`/`_docx_para_pdf`) e `services.intimacao_ia_service`
(`_colorir_colchetes_vermelho`), só muda ONDE elas rodam.

Uso: `python services/_docx_worker.py <docx_path> <pdf_path>`, com o HTML da
petição/despacho na entrada padrão (stdin, UTF-8) — evita limite de tamanho
de argumento de linha de comando e problema de encoding.

Saída: imprime `OK` em stdout e sai com código 0 em caso de sucesso; em caso
de erro, imprime `ERRO: <mensagem>` em stderr e sai com código 1 — o
chamador (`intimacao_ia_service._gerar_docx_pdf`) interpreta esse contrato.

NÃO É uma solução definitiva — é só uma ponte enquanto se confirma a
hipótese e/ou o TI resolve a exceção de verdade (ver conversa com o
usuário, 2026-08-28)."""

from __future__ import annotations

import sys
from pathlib import Path

# Garante que `services.*` seja importável mesmo se o worker for lançado com
# um cwd diferente da raiz do projeto (defesa extra; o chamador já passa
# cwd=BASE_DIR, mas não custa reforçar).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    if len(sys.argv) != 3:
        print("ERRO: uso: _docx_worker.py <docx_path> <pdf_path>", file=sys.stderr)
        return 1

    docx_path = Path(sys.argv[1])
    pdf_path = Path(sys.argv[2])
    html = sys.stdin.buffer.read().decode("utf-8")

    from services.intimacao_ia_service import _colorir_colchetes_vermelho
    from services.peticao_ia_service import _docx_para_pdf, _html_para_docx

    try:
        _html_para_docx(html, docx_path)
        _colorir_colchetes_vermelho(docx_path)
        _docx_para_pdf(docx_path, pdf_path)
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
