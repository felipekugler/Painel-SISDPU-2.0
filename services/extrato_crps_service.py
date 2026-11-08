"""Item "Baixar Extrato de Processo CRPS" (menu Consultas da página do PAJ).

O Defensor informa o nº do processo de recurso (protocolo e-Sisrec); o painel
gera o "Relatório de Andamento" do CRPS pela mesma automação do botão "Baixar
Extrato do Processo" da página Consultar e-Sisrec (`esisrec_client.
baixar_extrato`, headless, login GERID com a credencial do SAT Central) e salva
o PDF na subpasta `E-sisrec/` do PAJ.

Depois, opcionalmente, junta ao SISDPU pela rotina única dos dossiês
(`juntada_dossie_service`), com a descrição "Extrato do Andamento do Processo
no CRPS"; o front faz polling de `get_status`."""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services import historico, juntada_dossie_service

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
    _estado[paj_norm] = {"arquivo": nome, "arquivos": [nome], "numero": digitos, "juntada": None}
    with contextlib.suppress(Exception):
        historico.registrar(paj_norm, "extrato_crps", status="done",
                            resumo=f"Extrato do processo {digitos} salvo em {SUBPASTA}/{nome}")
    return {"ok": True, "arquivo": nome}


def juntar(paj_norm: str) -> dict:
    """Dispara a juntada — chamar de uma rota async (loop principal)."""
    return juntada_dossie_service.disparar(
        paj_norm, _estado.get(paj_norm), pasta=PAJS_DIR / paj_norm / SUBPASTA, descricao=MOV_DESCRICAO,
        origem="e-sisrec", atividade="Juntada do Extrato do Andamento do Processo no CRPS",
        chave_historico="extrato_crps_juntada")


def dispensar_juntada(paj_norm: str) -> dict:
    return juntada_dossie_service.dispensar(_estado.get(paj_norm))
