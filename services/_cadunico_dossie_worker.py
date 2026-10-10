"""Worker de vida curta que monta o PDF do Dossiê CadÚnico FORA do processo do
servidor — mesma medida provisória (FortiClient) de `services._sd_dossie_worker`.

Entrada (stdin): 1ª linha = JSON
    {"destino": "<caminho do .pdf>", "capa": {...}, "partes": [{"titulo": ..., "tamanho": N}, ...]}
seguida dos bytes dos PDFs das partes, concatenados na mesma ordem.

Gera: capa (título + conteúdo), página de dados (PAJ, família, integrantes) + os PDFs oficiais do CadÚnico na
ordem recebida, com marcadores (bookmarks) por parte.

Saída: `{"arquivo": "<caminho>"}` no stdout + código 0; em falha,
`ERRO: <mensagem>` em stderr + código 1."""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path

import fitz  # PyMuPDF

_CSS = """
* { font-family: sans-serif; }
h1 { font-size: 17px; margin: 0 0 2px 0; }
.sub { font-size: 10px; color: #555; margin-bottom: 12px; }
h2 { font-size: 12px; margin: 14px 0 4px 0; border-bottom: 1px solid #999; }
table { border-collapse: collapse; width: 100%; font-size: 9.5px; }
td, th { border: 1px solid #bbb; padding: 3px 4px; text-align: left; vertical-align: top; }
th { background: #e8efe8; }
.k { width: 170px; background: #f4f4f4; font-weight: bold; }
.exc { color: #a00; font-weight: bold; }
.nota { font-size: 9px; color: #555; margin-top: 12px; }
"""


TITULO_DADOS = "Dados do PAJ, da família e dos integrantes"


def _capa_html(capa: dict, sumario: list[tuple[str, int]]) -> str:
    """Página 1: título, emissão e a tabela "Conteúdo" (layout pedido pelo
    usuário em 2026-10-09)."""
    h = [f"<h1>{escape(capa.get('titulo') or 'Dossiê do Cadastro Único (CadÚnico)')}</h1>",
         f"<div class='sub'>Documentos emitidos pelo sistema do Cadastro Único "
         f"(CAIXA — cadastrounico.caixa.gov.br) em {escape(capa.get('emitido_em', ''))}</div>",
         "<h2>Conteúdo</h2><table><tr><th>Documento</th><th>Página</th></tr>"]
    for titulo, pag in sumario:
        h.append(f"<tr><td>{escape(titulo)}</td><td>{pag}</td></tr>")
    h.append("</table>")
    return "".join(h)


def _dados_html(capa: dict) -> str:
    """Página 2: PAJ, família, integrantes (com data/motivo de exclusão) e avisos."""
    def linha(k: str, v: str) -> str:
        return (f"<tr><td class='k' width='170'>{escape(k)}</td>"
                f"<td>{escape(v or '—')}</td></tr>")

    fam = capa.get("familia") or {}
    h = ["<h2>PAJ</h2><table>",
         linha("PAJ", capa.get("paj", "")),
         linha("Assistido(a)", capa.get("nome", "")),
         linha("CPF consultado", capa.get("cpf", "")),
         "</table>",
         "<h2>Família</h2><table>",
         linha("Código familiar", fam.get("codigo_familiar", "")),
         linha("Estado cadastral da família", fam.get("estado_cadastral", "")),
         linha("Data de inclusão", fam.get("data_inclusao", "")),
         linha("Última atualização", fam.get("data_atualizacao", "")),
         linha("Renda per capita", fam.get("renda_per_capita", "")),
         "</table>"]
    integ = fam.get("integrantes") or []
    if integ:
        h.append("<h2>Integrantes (inclusive excluídos)</h2><table>"
                 "<tr><th>Nome</th><th>NIS</th><th>Parentesco</th><th>Situação</th>"
                 "<th>Data da exclusão</th><th>Motivo da exclusão</th></tr>")
        for it in integ:
            c = (it.get("celulas") or []) + [""] * 6
            situ = c[4] or ""
            cls = " class='exc'" if it.get("excluido") else ""
            h.append(f"<tr><td>{escape(c[0])}</td><td>{escape(c[1])}</td>"
                     f"<td>{escape(c[3])}</td><td{cls}>{escape(situ)}</td>"
                     f"<td{cls}>{escape(it.get('data_exclusao') or '')}</td>"
                     f"<td{cls}>{escape(it.get('motivo_exclusao') or '')}</td></tr>")
        h.append("</table>")
    for aviso in capa.get("avisos") or []:
        h.append(f"<div class='nota'>⚠ {escape(aviso)}</div>")
    return "".join(h)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    try:
        dados = sys.stdin.buffer.read()
        nl = dados.index(b"\n")
        cab = json.loads(dados[:nl].decode("utf-8"))
        corpo = dados[nl + 1:]
        partes = cab["partes"]
        if sum(int(p["tamanho"]) for p in partes) != len(corpo):
            raise ValueError("tamanho dos PDFs não confere com o cabeçalho")

        docs: list[tuple[str, fitz.Document]] = []
        pos = 0
        for p in partes:
            n = int(p["tamanho"])
            docs.append((p["titulo"], fitz.open(stream=corpo[pos:pos + n], filetype="pdf")))
            pos += n

        # página 1 = capa/conteúdo, página 2 = dados; as partes começam na 3
        sumario, pag = [(TITULO_DADOS, 2)], 3
        for titulo, d in docs:
            sumario.append((titulo, pag))
            pag += d.page_count

        dados_capa = cab.get("capa") or {}
        saida = fitz.open()
        a4 = fitz.Rect(40, 40, 555, 802)
        saida.new_page(width=595, height=842).insert_htmlbox(
            a4, _capa_html(dados_capa, sumario), css=_CSS)
        saida.new_page(width=595, height=842).insert_htmlbox(
            a4, _dados_html(dados_capa), css=_CSS)
        toc = [[1, "Capa e conteúdo", 1], [1, TITULO_DADOS, 2]]
        for (titulo, d), (_t, inicio) in zip(docs, sumario[1:]):
            saida.insert_pdf(d)
            toc.append([1, titulo, inicio])
        saida.set_toc(toc)

        destino = Path(cab["destino"])
        destino.parent.mkdir(parents=True, exist_ok=True)
        saida.save(str(destino), garbage=3, deflate=True)
        print(json.dumps({"arquivo": str(destino), "paginas": saida.page_count}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"ERRO: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
