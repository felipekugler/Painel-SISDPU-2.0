"""Runner do cálculo do valor da causa — executado como SUBPROCESS pelo
`valor_causa_service`, sob o Python do sistema (que tem python-docx + pywin32).

Isola o Word/COM e a dependência de docx do processo do painel (cujo venv não
tem essas libs). Faz duas coisas: gera o PDF da tabela de prestações (via
`calc_valor_causa.gerar_pdf`) e injeta o total no texto da petição (placeholder
no `.txt` e no `.docx`).

Uso:
    python valor_causa_runner.py <job.json>

job.json:
    {
      "calc_dir":  "C:\\Claude\\DPU\\Modelos\\Peticoes",
      "dados":     { ...entrada do calc_valor_causa... },
      "pdf_path":  "...\\CALC VALOR DA CAUSA.pdf",
      "placeholder": "{{VALOR_CAUSA}}",
      "txt_files":  ["...\\peticao_inicial.txt"],
      "docx_files": ["...\\peticao_inicial.docx"]
    }

Saída (stdout, ÚLTIMA linha, prefixada por RESULT_JSON:):
    RESULT_JSON:{"ok": true, "total_nominal": ..., "total_atualizado": ...,
                 "valor_causa": "R$ ...", "injetado_em": [...]}
"""

import contextlib
import json
import sys
from pathlib import Path


def _injetar_txt(caminho: Path, placeholder: str, valor: str) -> bool:
    with contextlib.suppress(Exception):
        c = caminho.read_text(encoding="utf-8", errors="replace")
        if placeholder in c:
            caminho.write_text(c.replace(placeholder, valor), encoding="utf-8")
            return True
    return False


def _injetar_docx(caminho: Path, placeholder: str, valor: str) -> bool:
    """Caminho rápido: placeholder num único run (preserva a formatação).
    Fallback: placeholder quebrado entre runs -> concatena no primeiro run."""
    try:
        from docx import Document
    except Exception:
        return False
    with contextlib.suppress(Exception):
        doc = Document(str(caminho))
        n = 0

        def _proc(par) -> None:
            nonlocal n
            for r in par.runs:
                if placeholder in r.text:
                    r.text = r.text.replace(placeholder, valor)
                    n += 1
                    return
            if placeholder in par.text and par.runs:
                novo = par.text.replace(placeholder, valor)
                for r in par.runs:
                    r.text = ""
                par.runs[0].text = novo
                n += 1

        for p in doc.paragraphs:
            _proc(p)
        for t in doc.tables:
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        _proc(p)
        if n:
            doc.save(str(caminho))
            return True
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print('RESULT_JSON:{"ok": false, "erro": "job.json não informado"}')
        return 1
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    # sys.path para importar calc_valor_causa + peticao_base (pasta-mãe)
    calc_dir = job["calc_dir"]
    sys.path.insert(0, calc_dir)
    sys.path.insert(0, str(Path(calc_dir).parent))

    try:
        import calc_valor_causa as calc
    except Exception as e:
        print('RESULT_JSON:' + json.dumps({"ok": False, "erro": f"import calc: {e}"}))
        return 1

    dados = job["dados"]
    pdf_path = job["pdf_path"]

    # calc.gerar_pdf imprime no stdout — redireciona pro stderr pra não poluir o
    # JSON de saída.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            total_nom, total_atual = calc.gerar_pdf(dados, pdf_path)
    except Exception as e:
        print('RESULT_JSON:' + json.dumps({"ok": False, "erro": f"gerar_pdf: {e}"}))
        return 1

    valor_final = total_atual or total_nom
    valor_str = calc._moeda(valor_final)

    placeholder = job.get("placeholder", "{{VALOR_CAUSA}}")
    injetados = []
    for t in job.get("txt_files", []):
        if _injetar_txt(Path(t), placeholder, valor_str):
            injetados.append(Path(t).name)
    for d in job.get("docx_files", []):
        if _injetar_docx(Path(d), placeholder, valor_str):
            injetados.append(Path(d).name)

    print("RESULT_JSON:" + json.dumps({
        "ok": True,
        "total_nominal": total_nom,
        "total_atualizado": total_atual,
        "valor_causa": valor_str,
        "injetado_em": injetados,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
