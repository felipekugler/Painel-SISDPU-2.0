"""Botão "Dossiê Seguro-Desemprego Web" (menu Consultas da página do PAJ) —
gera, por automação direta (sem IA), os dossiês das skills
/SD-DossieVinculos, /SD-DossieRequerimentos ou ambos (/SD-Dossies) e salva o(s)
PDF(s) na subpasta `Arquivos SD Web/` da pasta do PAJ (pedido do usuário,
2026-10-02 — separada da `Arquivos/` padrão, como a `Arquivos SAT/`).

ONE-CLICK: CPF e nome vêm do `metadata.json`. Fluxo (ver `ingestao.sd_client`):
  1. Abre o Chromium próprio do SD e loga no GERID do MTE com as credenciais do
     SAT (cofre) + 2FA pela seed do SAT.
  2. Roda o(s) coletor(es) JS da skill na aba logada (Vínculos, depois
     Requerimentos — a sessão JSF é uma só, nunca em paralelo). Sessão
     expirada no meio → reloga e repete só a etapa que falhou (1 vez).
  3. Monta o PDF com o montador Python da skill, num subprocesso sem rede
     (`services/_sd_dossie_worker.py`), que também apaga os pacotes JSON.
     "Completo" = UM ÚNICO PDF unificado, com linha do tempo (montador
     `SD-Dossies/sd_dossie_unificado.py`, que recebe os 2 pacotes) — a skill
     SD-Dossies passou a gerar assim em 2026-10-02.
  4. FECHA o navegador ao final, sempre (pedido do usuário, 2026-10-02).
  5. Em sucesso, o front PERGUNTA se junta os PDFs ao SISDPU. Sim → `juntar()`:
     movimentação igual à da juntada do SAT Central (`sat_service`: fase
     "Juntada de documento", anexos como "Documentos do Assistido", sem
     tramitar nem concluir), só com a descrição "Dossiê do Seguro Desemprego
     Web". Roda como task no loop PRINCIPAL do painel (onde vive a sessão do
     SISDPU) — nunca na thread/loop próprio do dossiê.

Job em thread própria, desacoplado da conexão HTTP (sair da página não
cancela); o front só faz polling de `get_status`. Um job por PAJ."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services import historico

# etapas = coletas, na ordem (a sessão JSF é uma só); a montagem vem depois,
# com todos os pacotes coletados.
TIPOS = {
    "completo": {"rotulo": "Dossiê Completo", "etapas": ["vinculos", "requerimentos"]},
    "vinculos": {"rotulo": "Dossiê de Vínculos", "etapas": ["vinculos"]},
    "requerimentos": {"rotulo": "Dossiê de Seguro-Desemprego", "etapas": ["requerimentos"]},
}
_ROTULO_ETAPA = {"vinculos": "Dossiê de Vínculos", "requerimentos": "Dossiê de Requerimentos"}

_WORKER = Path(__file__).resolve().parent / "_sd_dossie_worker.py"
SUBPASTA_SD = "Arquivos SD Web"
MOV_DESCRICAO = "Dossiê do Seguro Desemprego Web"

# {paj_norm: {"status": "running"|"done"|"error", "tipo": ..., "log": [...], "arquivos": [...]}}
_execucoes: dict[str, dict] = {}
_ultimos: dict[str, dict] = {}  # resultado final (em memória) p/ o front ler após o término
_lock = threading.Lock()


def get_status(paj_norm: str) -> dict:
    with _lock:
        sess = _execucoes.get(paj_norm) or _ultimos.get(paj_norm)
        if sess:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in sess.items()}
    return {"status": "idle", "log": [], "arquivos": []}


def _dados_assistido(paj_norm: str) -> tuple[str, str, str]:
    """(cpf, nome, paj_label) do metadata.json."""
    p = PAJS_DIR / paj_norm / "metadata.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "", "", ""
    raw = meta.get("sisdpu_raw") or {}
    cpf = (raw.get("cpf_assistido") or "").strip()
    if not cpf:
        pf = raw.get("assistidos_pf") or []
        if pf and isinstance(pf[0], dict):
            cpf = (pf[0].get("cpf") or "").strip()
    nome = (meta.get("assistido_caixa") or raw.get("assistido") or "").strip()
    return cpf, nome, (meta.get("paj") or "").strip()


def _credenciais_sat() -> tuple[str, str]:
    with contextlib.suppress(Exception):
        from services import cofre_service

        creds = cofre_service.get_credenciais("sat")
        if creds:
            return creds
    return "", ""


def _montar(tipo: str, pacotes: list[str], destino: Path, paj: str, nome: str, cpf_fmt: str) -> dict:
    from ingestao import sd_client

    if tipo == "vinculos":
        montador = sd_client.MONTADOR_VINCULOS
        extras = ["--paj", paj, "--nome", nome]
    elif tipo == "requerimentos":
        montador = sd_client.MONTADOR_REQUERIMENTOS
        extras = ["--paj", paj, "--nome", nome, "--cpf", cpf_fmt]
    else:  # completo: pacotes = [vínculos, requerimentos]
        montador = sd_client.MONTADOR_UNIFICADO
        extras = ["--paj", paj, "--nome", nome, "--cpf", cpf_fmt]
    r = subprocess.run([sys.executable, str(_WORKER), str(montador), str(len(pacotes)),
                        str(destino), *extras],
                       input=b"\x00".join(p.encode("utf-8") for p in pacotes),
                       capture_output=True, timeout=1200,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError((err or out or "falha na montagem do PDF")[-500:])
    linha = next((ln for ln in reversed(out.splitlines()) if ln.strip().startswith("{")), "")
    return json.loads(linha) if linha else {}


async def _executar(paj_norm: str, sess: dict) -> None:
    from ingestao import sd_client

    def log(level: str, msg: str) -> None:
        sess["log"].append({"level": level, "msg": msg,
                            "ts": datetime.now().isoformat(timespec="seconds")})

    cpf, nome, paj_label = _dados_assistido(paj_norm)
    cpf_fmt = sd_client.formatar_cpf(cpf)
    if not cpf_fmt:
        raise RuntimeError("CPF do assistido não encontrado no PAJ.")
    destino = PAJS_DIR / paj_norm / SUBPASTA_SD
    usuario, senha = _credenciais_sat()
    etapas = TIPOS[sess["tipo"]]["etapas"]

    pw = browser = None
    try:
        pw, browser, page = await sd_client.conectar()
        log("info", "Abrindo o Seguro-Desemprego Web…")
        if not await sd_client.garantir_login(page, usuario, senha, log):
            raise RuntimeError("Login no Seguro-Desemprego Web não concluído (tempo esgotado).")
        log("ok", "Sessão ativa no Seguro-Desemprego Web.")

        pacotes: list[str] = []
        for etapa in etapas:
            rotulo = _ROTULO_ETAPA[etapa]
            coletar = (sd_client.coletar_vinculos if etapa == "vinculos"
                       else sd_client.coletar_requerimentos)
            log("info", f"{rotulo}: coletando dados…")
            try:
                pacote = await coletar(page, cpf_fmt)
            except sd_client.SessaoExpirada:
                log("aviso", f"{rotulo}: sessão expirou — relogando e repetindo…")
                if not await sd_client.garantir_login(page, usuario, senha, log):
                    raise RuntimeError("Sessão expirada e novo login não concluído.")
                pacote = await coletar(page, cpf_fmt)
            pacotes.append(pacote)
            log("ok", f"{rotulo}: dados coletados.")
    finally:
        await sd_client.desconectar(pw, browser)
        with contextlib.suppress(Exception):
            sd_client.fechar_navegador()

    # montagem só depois de fechar o navegador — não precisa mais do site
    log("info", "Montando o PDF…")
    res = await asyncio.to_thread(_montar, sess["tipo"], pacotes, destino,
                                  paj_label, nome, cpf_fmt)
    del pacotes
    arq = Path(res.get("arquivo") or "").name
    if not arq:
        raise RuntimeError("o montador não informou o arquivo gerado")
    sess["arquivos"].append(arq)
    log("ok", f"{sess['rotulo']} salvo em {SUBPASTA_SD}/{arq}")


def iniciar(paj_norm: str, tipo: str) -> dict:
    if tipo not in TIPOS:
        return {"ok": False, "erro": "Tipo de dossiê inválido."}
    if not (PAJS_DIR / paj_norm).exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    with _lock:
        if any(s.get("status") == "running" for s in _execucoes.values()):
            return {"ok": False, "erro": "Já há um Dossiê do Seguro-Desemprego Web sendo gerado "
                                         "(o site só permite uma coleta por vez)."}
        # juntada: None = ainda não perguntado | "dispensada" | "running" | "ok" | "erro"
        sess = {"status": "running", "tipo": tipo, "rotulo": TIPOS[tipo]["rotulo"],
                "log": [], "arquivos": [], "juntada": None}
        _execucoes[paj_norm] = sess
        _ultimos.pop(paj_norm, None)

    def _run() -> None:
        try:
            asyncio.run(_executar(paj_norm, sess))
            sess["status"] = "done"
        except Exception as e:  # noqa: BLE001
            sess["status"] = "error"
            sess["erro"] = f"{e}"[:400]
            sess["log"].append({"level": "erro", "msg": sess["erro"],
                                "ts": datetime.now().isoformat(timespec="seconds")})
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "dossie_sd", status=sess["status"],
                                resumo=(f"{sess['rotulo']} gerado: " + ", ".join(sess["arquivos"])
                                        if sess["status"] == "done"
                                        else f"{sess['rotulo']}: {sess.get('erro', '')}"))
        time.sleep(1)
        with _lock:
            _ultimos[paj_norm] = sess
            _execucoes.pop(paj_norm, None)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------------------
# Juntada ao SISDPU (opcional, perguntada ao Defensor após o sucesso)
# --------------------------------------------------------------------------

def _sessao_final(paj_norm: str) -> dict | None:
    with _lock:
        return _ultimos.get(paj_norm)


def dispensar_juntada(paj_norm: str) -> dict:
    sess = _sessao_final(paj_norm)
    if sess and sess.get("juntada") is None:
        sess["juntada"] = "dispensada"
    return {"ok": True}


def _ler_meta(paj_norm: str) -> dict:
    with contextlib.suppress(Exception):
        return json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    return {}


def _id_processo_local(meta: dict) -> str:
    """id interno do SISDPU pela URL de detalhamento gravada pelo sync (mesma
    extração de `paj_service`); vazio se o PAJ nunca foi sincronizado."""
    m = re.search(r"[?&]id=(\d+)", (meta.get("sisdpu_raw") or {}).get("url", "") or "")
    return m.group(1) if m else ""


async def _juntar(paj_norm: str, sess: dict) -> None:
    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service

    def log(level: str, msg: str) -> None:
        sess["log"].append({"level": level, "msg": msg,
                            "ts": datetime.now().isoformat(timespec="seconds")})

    try:
        meta = _ler_meta(paj_norm)
        paj_label = (meta.get("paj") or "").strip()
        pasta = PAJS_DIR / paj_norm / SUBPASTA_SD
        anexos = [{"nome": n, "caminho": str(pasta / n), "tipo": "pdf"}
                  for n in sess.get("arquivos") or [] if (pasta / n).is_file()]
        if not anexos:
            raise RuntimeError(f"PDF do dossiê não encontrado em {SUBPASTA_SD}/.")
        id_proc, eventos, erro = await acoes_sisdpu_service._resolver_id_processo(
            _id_processo_local(meta), paj_label)
        for ev in eventos:
            log(ev.get("level") or "info", ev.get("msg") or "")
        if not id_proc:
            raise RuntimeError(erro or "id do PAJ no SISDPU não encontrado.")
        log("info", "Movimentando no SISDPU (juntada de documento)…")
        pajs = [{"id": id_proc, "numero": paj_label}]
        mv, exc, eventos = await acoes_sisdpu_service.com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, MOV_DESCRICAO, sat_service.FASE_MOV, anexos,
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
            historico.registrar(paj_norm, "dossie_sd_juntada", status="ok",
                                resumo=f"{len(anexos)} arquivo(s) juntado(s) ao SISDPU")
        with contextlib.suppress(Exception):
            from services import historico_painel

            historico_painel.registrar({
                "paj": paj_label, "paj_norm": paj_norm,
                "url_detalhe": (meta.get("sisdpu_raw") or {}).get("url", ""),
                "assistido": meta.get("assistido_caixa") or "",
                "origem": "sd web",
                "atividade": "Juntada do Dossiê do Seguro Desemprego Web",
                "movimentacao": sat_service.FASE_MOV, "tramitado_para": "",
                "concluido": False,
                "detalhe": f"{len(anexos)} arquivo(s) juntado(s).",
            })
    except Exception as e:  # noqa: BLE001
        sess["juntada"] = "erro"
        sess["erro_juntada"] = f"{e}"[:400]
        log("erro", f"Juntada ao SISDPU: {sess['erro_juntada']}")
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "dossie_sd_juntada", status="error",
                                resumo=sess["erro_juntada"])


def juntar(paj_norm: str) -> dict:
    """Dispara a juntada como task no loop CORRENTE — chamar de uma rota async
    (loop principal do uvicorn, o mesmo da sessão do SISDPU)."""
    sess = _sessao_final(paj_norm)
    if not sess or sess.get("status") != "done" or not sess.get("arquivos"):
        return {"ok": False, "erro": "Nenhum dossiê recém-gerado para juntar neste PAJ."}
    if sess.get("juntada") in ("running", "ok"):
        return {"ok": False, "erro": "A juntada deste dossiê já foi feita ou está em andamento."}
    sess["juntada"] = "running"
    sess.pop("erro_juntada", None)
    asyncio.get_running_loop().create_task(_juntar(paj_norm, sess))
    return {"ok": True}
