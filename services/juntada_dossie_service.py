"""Juntada ao SISDPU dos dossiês gerados pelo menu Consultas da página do PAJ
— rotina ÚNICA (05/11/2026, pedido do usuário: "uniformize essas rotinas de
download de dossiês e de juntada ao PAJ").

Usada por: Dossiê Seguro-Desemprego Web (`dossie_sd_service`), Dossiê
CadÚnico (`dossie_cadunico_service`), Extrato de processo CRPS
(`extrato_crps_service`) e Íntegra do processo e-Sisrec
(`esisrec_integra_service`). Cada um guarda o estado numa `sess` (dict) com
`arquivos` (nomes dos PDFs na subpasta do PAJ) e, opcionalmente, `log`; aqui
ficam os campos `juntada` (None | "running" | "ok" | "erro" | "dispensada"),
`erro_juntada` e `mov_seq`.

Movimentação: fase "Juntada de documento", anexos como "Documentos do
Assistido", sem tramitar nem concluir (mesma da juntada do SAT Central). Roda
como task no loop PRINCIPAL do painel (onde vive a sessão do SISDPU) — por
isso `disparar` deve ser chamado de uma rota async. O front acompanha por
polling do status do próprio serviço do dossiê."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services import historico


def ler_meta(paj_norm: str) -> dict:
    with contextlib.suppress(Exception):
        return json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    return {}


def id_processo_local(meta: dict) -> str:
    """id interno do SISDPU pela URL de detalhamento gravada pelo sync (mesma
    extração de `paj_service`); vazio se o PAJ nunca foi sincronizado."""
    m = re.search(r"[?&]id=(\d+)", (meta.get("sisdpu_raw") or {}).get("url", "") or "")
    return m.group(1) if m else ""


def dispensar(sess: dict | None) -> dict:
    """"Não juntar": só evita repetir a pergunta (inclusive após F5)."""
    if sess and sess.get("juntada") is None:
        sess["juntada"] = "dispensada"
    return {"ok": True}


def disparar(paj_norm: str, sess: dict | None, *, pasta: Path, descricao: str, origem: str,
             atividade: str, chave_historico: str) -> dict:
    """Valida e dispara a juntada como task no loop CORRENTE (rota async)."""
    if not sess or not sess.get("arquivos"):
        return {"ok": False, "erro": "Nenhum dossiê gerado para juntar neste PAJ."}
    if sess.get("juntada") in ("running", "ok"):
        return {"ok": False, "erro": "A juntada deste dossiê já foi feita ou está em andamento."}
    sess["juntada"] = "running"
    sess.pop("erro_juntada", None)
    asyncio.get_running_loop().create_task(_juntar(
        paj_norm, sess, pasta=pasta, descricao=descricao, origem=origem,
        atividade=atividade, chave_historico=chave_historico))
    return {"ok": True}


async def _juntar(paj_norm: str, sess: dict, *, pasta: Path, descricao: str, origem: str,
                  atividade: str, chave_historico: str) -> None:
    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service

    def log(level: str, msg: str) -> None:
        if isinstance(sess.get("log"), list):
            sess["log"].append({"level": level, "msg": msg,
                                "ts": datetime.now().isoformat(timespec="seconds")})

    try:
        meta = ler_meta(paj_norm)
        paj_label = (meta.get("paj") or "").strip()
        anexos = [{"nome": n, "caminho": str(pasta / n), "tipo": "pdf"}
                  for n in sess.get("arquivos") or [] if (pasta / n).is_file()]
        if not anexos:
            raise RuntimeError(f"PDF do dossiê não encontrado em {pasta.name}/.")
        id_proc, eventos, erro = await acoes_sisdpu_service._resolver_id_processo(
            id_processo_local(meta), paj_label)
        for ev in eventos:
            log(ev.get("level") or "info", ev.get("msg") or "")
        if not id_proc:
            raise RuntimeError(erro or "id do PAJ no SISDPU não encontrado.")
        log("info", "Movimentando no SISDPU (juntada de documento)…")
        pajs = [{"id": id_proc, "numero": paj_label}]
        mv, exc, eventos = await acoes_sisdpu_service.com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, descricao, sat_service.FASE_MOV, anexos,
                                           sat_service.TIPO_ARQUIVO, False))
        for ev in eventos:
            log(ev.get("level") or "info", ev.get("msg") or "")
        if exc is not None:
            raise RuntimeError(f"{type(exc).__name__}: {exc}")
        if not (mv or {}).get("ok"):
            raise RuntimeError((mv or {}).get("detalhe") or "falha na movimentação")
        sess["juntada"] = "ok"
        sess["mov_seq"] = mv.get("mov_seq")
        log("ok", "Juntado ao SISDPU" + (f" (movimentação {mv['mov_seq']})." if mv.get("mov_seq") else "."))
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, chave_historico, status="ok",
                                resumo=f"{len(anexos)} arquivo(s) juntado(s) ao SISDPU")
        with contextlib.suppress(Exception):
            from services import historico_painel

            historico_painel.registrar({
                "paj": paj_label, "paj_norm": paj_norm,
                "url_detalhe": (meta.get("sisdpu_raw") or {}).get("url", ""),
                "assistido": meta.get("assistido_caixa") or "",
                "origem": origem,
                "atividade": atividade,
                "movimentacao": sat_service.FASE_MOV, "tramitado_para": "",
                "concluido": False,
                "detalhe": ", ".join(a["nome"] for a in anexos) if len(anexos) == 1
                           else f"{len(anexos)} arquivo(s) juntado(s).",
            })
    except Exception as e:  # noqa: BLE001
        sess["juntada"] = "erro"
        sess["erro_juntada"] = f"{e}"[:400]
        log("erro", f"Juntada ao SISDPU: {sess['erro_juntada']}")
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, chave_historico, status="error", resumo=sess["erro_juntada"])
