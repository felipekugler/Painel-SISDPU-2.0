"""Item "Baixar Extrato de Processo CRPS" (menu Consultas da página do PAJ).

O Defensor informa o nº do processo de recurso (protocolo e-Sisrec); o painel
gera o "Relatório de Andamento" do CRPS pela mesma automação do botão "Baixar
Extrato do Processo" da página Consultar e-Sisrec (`esisrec_client.
baixar_extrato`, headless, login GERID com a credencial do SAT Central) e salva
o PDF na subpasta `E-sisrec/` do PAJ.

Depois, opcionalmente, junta ao SISDPU — mesma movimentação da juntada do
Dossiê SD / SAT Central (fase "Juntada de documento", anexo como "Documentos
do Assistido", sem tramitar nem concluir), com a descrição "Extrato do
Andamento do Processo no CRPS". A juntada roda como task no loop PRINCIPAL do
painel (onde vive a sessão do SISDPU); o front faz polling de `get_status`."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services import historico

SUBPASTA = "E-sisrec"
MOV_DESCRICAO = "Extrato do Andamento do Processo no CRPS"

# {paj_norm: {"arquivo": ..., "numero": ..., "juntada": None|"running"|"ok"|"erro", ...}}
_estado: dict[str, dict] = {}


def get_status(paj_norm: str) -> dict:
    sess = _estado.get(paj_norm)
    return dict(sess) if sess else {"arquivo": "", "juntada": None}


def caminho_arquivo(paj_norm: str, nome: str) -> Path | None:
    base = Path(nome or "").name  # anti path-traversal
    p = PAJS_DIR / paj_norm / SUBPASTA / base
    return p if base.lower().endswith(".pdf") and p.is_file() else None


async def obter(paj_norm: str, numero: str) -> dict:
    """Gera o extrato no e-Sisrec e salva em `E-sisrec/` do PAJ."""
    from ingestao import esisrec_client
    from routes.esisrec import _credenciais_sat

    digitos = re.sub(r"\D", "", numero or "")
    if not digitos:
        return {"ok": False, "erro": "Informe o número do processo."}
    pasta_paj = PAJS_DIR / paj_norm
    if not pasta_paj.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    usuario, senha = _credenciais_sat()
    try:
        corpo, _nome, _ctype = await esisrec_client.baixar_extrato(digitos, usuario, senha)
    except esisrec_client.EsisrecErro as e:
        return {"ok": False, "erro": str(e)}
    if corpo[:5] != b"%PDF-":
        return {"ok": False, "erro": "O e-Sisrec não devolveu um PDF."}
    pasta = pasta_paj / SUBPASTA
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"Extrato Andamento CRPS {digitos} {datetime.now():%Y-%m-%d}.pdf"
    (pasta / nome).write_bytes(corpo)
    _estado[paj_norm] = {"arquivo": nome, "numero": digitos, "juntada": None}
    with contextlib.suppress(Exception):
        historico.registrar(paj_norm, "extrato_crps", status="done",
                            resumo=f"Extrato do processo {digitos} salvo em {SUBPASTA}/{nome}")
    return {"ok": True, "arquivo": nome}


async def _juntar(paj_norm: str, sess: dict) -> None:
    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service
    from services.dossie_sd_service import _id_processo_local, _ler_meta

    try:
        meta = _ler_meta(paj_norm)
        paj_label = (meta.get("paj") or "").strip()
        p = caminho_arquivo(paj_norm, sess.get("arquivo", ""))
        if not p:
            raise RuntimeError(f"PDF do extrato não encontrado em {SUBPASTA}/.")
        anexos = [{"nome": p.name, "caminho": str(p), "tipo": "pdf"}]
        id_proc, _eventos, erro = await acoes_sisdpu_service._resolver_id_processo(
            _id_processo_local(meta), paj_label)
        if not id_proc:
            raise RuntimeError(erro or "id do PAJ no SISDPU não encontrado.")
        pajs = [{"id": id_proc, "numero": paj_label}]
        mv, exc, _eventos = await acoes_sisdpu_service.com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, MOV_DESCRICAO, sat_service.FASE_MOV, anexos,
                                           sat_service.TIPO_ARQUIVO, False))
        if exc is not None:
            raise RuntimeError(f"{type(exc).__name__}: {exc}")
        if not (mv or {}).get("ok"):
            raise RuntimeError((mv or {}).get("detalhe") or "falha na movimentação")
        sess["juntada"] = "ok"
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "extrato_crps_juntada", status="ok",
                                resumo=f"{p.name} juntado ao SISDPU")
        with contextlib.suppress(Exception):
            from services import historico_painel

            historico_painel.registrar({
                "paj": paj_label, "paj_norm": paj_norm,
                "url_detalhe": (meta.get("sisdpu_raw") or {}).get("url", ""),
                "assistido": meta.get("assistido_caixa") or "",
                "origem": "e-sisrec",
                "atividade": "Juntada do Extrato do Andamento do Processo no CRPS",
                "movimentacao": sat_service.FASE_MOV, "tramitado_para": "",
                "concluido": False,
                "detalhe": p.name,
            })
    except Exception as e:  # noqa: BLE001
        sess["juntada"] = "erro"
        sess["erro_juntada"] = f"{e}"[:400]
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "extrato_crps_juntada", status="error",
                                resumo=sess["erro_juntada"])


def juntar(paj_norm: str) -> dict:
    """Dispara a juntada como task no loop CORRENTE — chamar de uma rota async."""
    sess = _estado.get(paj_norm)
    if not sess or not sess.get("arquivo"):
        return {"ok": False, "erro": "Nenhum extrato obtido para juntar neste PAJ."}
    if sess.get("juntada") in ("running", "ok"):
        return {"ok": False, "erro": "A juntada deste extrato já foi feita ou está em andamento."}
    sess["juntada"] = "running"
    sess.pop("erro_juntada", None)
    asyncio.get_running_loop().create_task(_juntar(paj_norm, sess))
    return {"ok": True}
