"""Item "Baixar íntegra do processo" (menu Consultas › E-sisrec da página do PAJ).

O Defensor informa o nº do processo de recurso (protocolo e-Sisrec); o painel
monta UM PDF com a íntegra e salva na subpasta `E-sisrec/` do PAJ (mesma do
extrato, ver `extrato_crps_service`). Formato definido com o Defensor
(04/11/2026, processo 44233.692705/2018-19):

  1. o extrato do processo ("Relatório de Andamento" do e-Sisrec) na frente,
     no lugar de uma capa;
  2. para cada evento, em ordem cronológica (nº do evento crescente), uma
     página separadora (nº, data, descrição, lista de documentos) seguida dos
     documentos do evento;
  3. sem tarja no canto das páginas, sem página de fechamento e sem tabela de
     histórico no fim.

Depois, opcionalmente, junta ao SISDPU pela rotina única dos dossiês
(`juntada_dossie_service`, 05/11/2026), com a descrição "Íntegra do Processo
de Recurso no CRPS (e-Sisrec)".

Leitura e downloads pela mesma automação headless da página Consultar
e-Sisrec (`ingestao.esisrec_client`, login GERID com a credencial do SAT
Central). As páginas entram em A4 retrato com a mesma normalização do "Baixar
PAJ Completo" (`download_completo_service`). Roda como task (pode levar alguns
minutos em processos com muitos documentos); o front acompanha por polling de
`get_status`."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

from config import PAJS_DIR
from services import historico, juntada_dossie_service
from services.extrato_crps_service import SUBPASTA

MOV_DESCRICAO = "Íntegra do Processo de Recurso no CRPS (e-Sisrec)"

# {paj_norm: {"estado": "running"|"ok"|"erro", "etapa", "numero", "arquivo",
#             "arquivos", "erro", "juntada", "erro_juntada"}}
_estado: dict[str, dict] = {}

_IMAGENS = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff"}


def get_status(paj_norm: str) -> dict:
    sess = _estado.get(paj_norm)
    return dict(sess) if sess else {"estado": None, "arquivo": ""}


def caminho_arquivo(paj_norm: str, nome: str) -> Path | None:
    base = Path(nome or "").name  # anti path-traversal
    p = PAJS_DIR / paj_norm / SUBPASTA / base
    return p if base.lower().endswith(".pdf") and p.is_file() else None


def iniciar(paj_norm: str, numero: str) -> dict:
    """Dispara a montagem da íntegra como task no loop CORRENTE (rota async)."""
    digitos = re.sub(r"\D", "", numero or "")
    if not digitos:
        return {"ok": False, "erro": "Informe o número do processo."}
    if not (PAJS_DIR / paj_norm).exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if (_estado.get(paj_norm) or {}).get("estado") == "running":
        return {"ok": False, "erro": "Já há uma íntegra sendo montada para este PAJ."}
    sess = {"estado": "running", "etapa": "Consultando o processo no e-Sisrec…", "numero": digitos, "arquivo": ""}
    _estado[paj_norm] = sess
    asyncio.get_running_loop().create_task(_montar(paj_norm, digitos, sess))
    return {"ok": True}


async def _montar(paj_norm: str, numero: str, sess: dict) -> None:
    from ingestao import esisrec_client
    from routes.esisrec import _credenciais_sat

    try:
        usuario, senha = _credenciais_sat()
        dados = await esisrec_client.consultar(numero, usuario, senha)
        if not dados.get("ok"):
            raise RuntimeError(dados.get("erro") or "Falha na consulta ao e-Sisrec.")
        eventos = sorted(dados.get("eventos") or [], key=lambda e: int(e.get("n") or 0))

        sess["etapa"] = "Baixando o extrato do processo…"
        extrato, _n, _c = await esisrec_client.baixar_extrato(numero, usuario, senha)

        total = sum(len(e.get("documentos") or []) for e in eventos)
        baixados: dict[tuple[str, int], tuple[bytes, str]] = {}
        k = 0
        for e in eventos:
            for idx, _doc in enumerate(e.get("documentos") or []):
                k += 1
                sess["etapa"] = f"Baixando documento {k} de {total}…"
                try:
                    corpo, nome, _ct = await esisrec_client.baixar_documento(
                        numero, e["n"], idx, e.get("pagina") or "1", usuario, senha)
                    baixados[(str(e["n"]), idx)] = (corpo, nome)
                except esisrec_client.EsisrecErro:
                    pass  # vira "(não baixado)" na separadora

        sess["etapa"] = "Montando o PDF…"
        pdf = await asyncio.to_thread(_gerar_pdf, extrato, eventos, baixados)
        pasta = PAJS_DIR / paj_norm / SUBPASTA
        pasta.mkdir(parents=True, exist_ok=True)
        protocolo = (dados.get("numero") or numero).replace("/", "-")
        nome = f"Íntegra e-Sisrec {protocolo} {datetime.now():%Y-%m-%d}.pdf"
        (pasta / nome).write_bytes(pdf)
        faltando = total - len(baixados)
        sess.update(estado="ok", arquivo=nome, arquivos=[nome], juntada=None,
                    etapa=f"{total} documento(s), {len(eventos)} evento(s)"
                          + (f" — {faltando} documento(s) não baixado(s)" if faltando else ""))
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "esisrec_integra", status="done",
                                resumo=f"Íntegra do processo {numero} salva em {SUBPASTA}/{nome}")
    except Exception as e:  # noqa: BLE001
        sess.update(estado="erro", erro=f"{e}"[:400])
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "esisrec_integra", status="error", resumo=sess["erro"])


def juntar(paj_norm: str) -> dict:
    """Dispara a juntada — chamar de uma rota async (loop principal)."""
    sess = _estado.get(paj_norm)
    if not sess or sess.get("estado") != "ok":
        return {"ok": False, "erro": "Nenhuma íntegra montada para juntar neste PAJ."}
    return juntada_dossie_service.disparar(
        paj_norm, sess, pasta=PAJS_DIR / paj_norm / SUBPASTA, descricao=MOV_DESCRICAO,
        origem="e-sisrec", atividade="Juntada da Íntegra do Processo de Recurso no CRPS",
        chave_historico="esisrec_integra_juntada")


def dispensar_juntada(paj_norm: str) -> dict:
    return juntada_dossie_service.dispensar(_estado.get(paj_norm))


def _gerar_pdf(extrato: bytes, eventos: list[dict], baixados: dict) -> bytes:
    from services import download_completo_service as dc
    from services.pdf_compress_service import comprimir_pdf_bytes

    a4, m = dc.A4, dc.MARGEM
    doc = fitz.open()

    def anexar_pdf(corpo: bytes) -> None:
        with fitz.open(stream=corpo, filetype="pdf") as src:
            for i in range(src.page_count):
                dc._normalizar_pagina_pdf(doc, src, i)

    def anexar_imagem(corpo: bytes) -> None:
        page = dc._nova_pagina(doc)
        page.insert_image(fitz.Rect(m, 30, a4.width - m, a4.height - m), stream=corpo, keep_proportion=True)

    def pagina_aviso(texto: str) -> None:
        page = dc._nova_pagina(doc)
        dc._texto(page, m, m, a4.width - m, 60, texto, fontsize=10, color=(0.35, 0.35, 0.35))

    if extrato[:5] == b"%PDF-":
        anexar_pdf(extrato)

    for e in eventos:
        docs = e.get("documentos") or []
        page = dc._nova_pagina(doc)
        page.draw_rect(page.rect, color=None,
                       fill=dc.COR_PETICIONAMENTO_CLARA if docs else dc.COR_PADRAO_CLARA)
        y = m
        dc._texto(page, m, y, a4.width - m, 36, f"EVENTO {e['n']}", fontsize=18, fontname=dc.FONTE_NEGRITO)
        y += 46
        campos = [("Data/Hora", e.get("data"))] + ([("Usuário", e["usuario"])] if e.get("usuario") else [])
        for rot, val in campos:
            dc._texto(page, m, y, m + 90, 20, f"{rot}:", fontsize=10, fontname=dc.FONTE_NEGRITO)
            dc._texto(page, m + 95, y, a4.width - m, 20, val or "-", fontsize=10)
            y += 24
        desc = " ".join((e.get("descricao") or "-").split())
        alt = dc._altura_texto_livre(desc, a4.width - m - (m + 95), 10) * 1.4 + 8
        dc._texto(page, m, y, m + 90, alt, "Descrição:", fontsize=10, fontname=dc.FONTE_NEGRITO)
        dc._texto(page, m + 95, y, a4.width - m, alt, desc, fontsize=10)
        y += alt + 14
        dc._texto(page, m, y, a4.width - m, 20, "Documentos deste evento:", fontsize=10, fontname=dc.FONTE_NEGRITO)
        y += 24
        if not docs:
            dc._texto(page, m + 10, y, a4.width - m, 18, "Nenhum documento neste evento.",
                      fontsize=9, color=(0.35, 0.35, 0.35))
        for idx, d in enumerate(docs):
            b = baixados.get((str(e["n"]), idx))
            comp = f"({len(b[0]) / 1024:.1f} KB)" if b else "(não baixado)"
            dc._texto(page, m + 10, y, a4.width - m, 18, f"- {d.get('nome') or 'Documento'}  {comp}", fontsize=9)
            y += 18

        for idx, d in enumerate(docs):
            b = baixados.get((str(e["n"]), idx))
            if not b:
                continue
            corpo, nome = b
            ext = Path(nome).suffix.lower().lstrip(".")
            try:
                if corpo[:5] == b"%PDF-":
                    anexar_pdf(corpo)
                elif ext in _IMAGENS:
                    anexar_imagem(corpo)
                else:
                    pagina_aviso(f"Documento \"{d.get('nome')}\" ({nome}) em formato que não entra no PDF — "
                                 "abra-o pela página Consultar e-Sisrec.")
            except Exception as ex:  # noqa: BLE001
                pagina_aviso(f"Não foi possível incluir \"{d.get('nome')}\" ({nome}): {ex}")

    pdf = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    pdf, _ = comprimir_pdf_bytes(pdf)
    return pdf
