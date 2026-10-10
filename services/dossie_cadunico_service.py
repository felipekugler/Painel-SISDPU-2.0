"""Item "Dossiê CadÚnico" (menu Consultas da página do PAJ) — gera, por
automação direta (sem IA), um dossiê com os PDFs OFICIAIS do Cadastro Único
de cada família em que o CPF do assistido está cadastrado, e salva na
subpasta `CadÚnico/` da pasta do PAJ. Pedido do usuário em 2026-10-09
("semelhante ao dossiê de vínculos e seguro-desemprego").

Decisões do usuário (2026-10-09):
  - UM PDF POR FAMÍLIA (um CPF pode estar em mais de uma).
  - Conteúdo, nesta ordem: Folha Resumo → Formulário Principal (que já embute
    os suplementares preenchidos) → suplementar preenchido que NÃO esteja no
    Principal (nunca os em branco) → Histórico da Família (de 01/01/1950 até
    hoje, todos os campos) → Motivo de exclusão de cada integrante excluído.
    Avulsos 1-3 ficam de fora (repetem partes do Principal).
  - Login é MANUAL (usuário, senha, código por e-mail e 2FA): o painel abre a
    janela e espera; o navegador fica aberto entre um dossiê e outro.

Fluxo: ver `ingestao.cadunico_client`. A montagem (capa + PDFs + marcadores)
roda em subprocesso sem rede (`services/_cadunico_dossie_worker.py`, FortiClient).
Em sucesso o front pergunta se junta ao SISDPU — mesma movimentação do Dossiê
SD (fase "Juntada de documento", "Documentos do Assistido", sem tramitar nem
concluir), descrição "Dossiê do CadÚnico", em task no loop PRINCIPAL.

Job em thread própria (sobrevive a sair da página); um por vez (a sessão do
CadÚnico é uma só e os servlets dependem da família aberta nela)."""

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
from datetime import date, datetime
from pathlib import Path

from config import PAJS_DIR
from services import historico
from services.dossie_sd_service import _dados_assistido, _id_processo_local, _ler_meta

SUBPASTA = "CadÚnico"
# tipos do submenu (pedido do usuário, 2026-10-09): completo | só Folha Resumo
# | só Histórico da Família. Todos levam capa + página de dados (com os
# motivos de exclusão lidos da tela — os PDFs dos motivos só no completo).
TIPOS = {
    "completo": {"rotulo": "Dossiê CadÚnico Completo", "arquivo": "Dossiê CadÚnico",
                 "titulo": "Dossiê do Cadastro Único (CadÚnico)",
                 "mov": "Dossiê do CadÚnico"},
    "folha": {"rotulo": "Folha Resumo do CadÚnico", "arquivo": "CadÚnico Folha Resumo",
              "titulo": "Cadastro Único (CadÚnico) — Folha Resumo",
              "mov": "Folha Resumo do CadÚnico"},
    "historico": {"rotulo": "Histórico da Família do CadÚnico",
                  "arquivo": "CadÚnico Histórico da Família",
                  "titulo": "Cadastro Único (CadÚnico) — Histórico da Família",
                  "mov": "Histórico da Família do CadÚnico"},
}
DATA_INICIO_HISTORICO = date(1950, 1, 1)  # pedido do usuário, 2026-10-09

_WORKER = Path(__file__).resolve().parent / "_cadunico_dossie_worker.py"

_execucoes: dict[str, dict] = {}
_ultimos: dict[str, dict] = {}
_lock = threading.Lock()


def get_status(paj_norm: str) -> dict:
    with _lock:
        sess = _execucoes.get(paj_norm) or _ultimos.get(paj_norm)
        if sess:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in sess.items()}
    return {"status": "idle", "log": [], "arquivos": []}


def caminho_arquivo(paj_norm: str, nome: str) -> Path | None:
    base = Path(nome or "").name  # anti path-traversal
    p = PAJS_DIR / paj_norm / SUBPASTA / base
    return p if base.lower().endswith(".pdf") and p.is_file() else None


def _formatar_cpf(cpf: str) -> str:
    d = re.sub(r"\D", "", cpf or "")
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else ""


def _nome_arquivo(codigo_familiar: str, nome: str, prefixo: str = "Dossiê CadÚnico") -> str:
    limpo = re.sub(r'[<>:"/\\|?*]+', " ", nome or "").strip()
    return re.sub(r"\s+", " ", f"{prefixo} - Família {codigo_familiar} - {limpo} - "
                               f"{datetime.now():%Y-%m-%d}.pdf")


# --------------------------------------------------------------------------
# Suplementares: só os preenchidos (item 1.09 do Principal) e ainda não
# embutidos no próprio Principal
# --------------------------------------------------------------------------

def _texto_paginas(pdf: bytes) -> list[set[str]]:
    import fitz

    with fitz.open(stream=pdf, filetype="pdf") as d:
        return [set(p.get_text().split()) for p in d]


def _suplementares_preenchidos(principal: bytes) -> list[int]:
    """Números dos Formulários Suplementares listados no item 1.09
    ("Formulário(s) preenchido(s)") do Principal, ex.:
    "0 - Principal . 3 - Formulário Suplementar 1 ." → [1]."""
    import fitz

    with fitz.open(stream=principal, filetype="pdf") as d:
        texto = "\n".join(p.get_text() for p in d)
    linhas = [ln for ln in texto.splitlines() if re.search(r"\d\s*-\s*Principal\s*\.", ln)]
    nums: set[int] = set()
    for ln in linhas:
        nums.update(int(n) for n in re.findall(r"Formul[áa]rio Suplementar\s*(\d)", ln))
    return sorted(nums)


def _ja_no_principal(principal_paginas: list[set[str]], sup: bytes) -> bool:
    """True se a 1ª página do suplementar já está, praticamente idêntica,
    dentro do Principal (Jaccard ≥ 0.9 — medido 0.99 no caso real)."""
    sp = _texto_paginas(sup)
    if not sp:
        return True
    alvo = sp[0]
    for pg in principal_paginas:
        uni = len(pg | alvo)
        if uni and len(pg & alvo) / uni >= 0.9:
            return True
    return False


# --------------------------------------------------------------------------
# Montagem (subprocesso)
# --------------------------------------------------------------------------

def _montar(destino: Path, capa: dict, partes: list[tuple[str, bytes]]) -> dict:
    cab = {"destino": str(destino), "capa": capa,
           "partes": [{"titulo": t, "tamanho": len(b)} for t, b in partes]}
    entrada = json.dumps(cab, ensure_ascii=False).encode("utf-8") + b"\n" + b"".join(b for _, b in partes)
    r = subprocess.run([sys.executable, str(_WORKER)], input=entrada,
                       capture_output=True, timeout=600,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = r.stdout.decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError((err or out or "falha na montagem do PDF")[-500:])
    linha = next((ln for ln in reversed(out.splitlines()) if ln.strip().startswith("{")), "")
    return json.loads(linha) if linha else {}


# --------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------

async def _coletar_familia(page, cpf: str, fam_busca: dict, log,
                           tipo: str = "completo") -> tuple[dict, list[tuple[str, bytes]], list[str]]:
    from ingestao import cadunico_client as cad

    completo = tipo == "completo"
    dados = await cad.abrir_familia(page, cpf, fam_busca["codigo"], fam_busca["dv"])
    cod = dados.get("codigo_familiar") or f"{fam_busca['codigo']}-{fam_busca['dv']}"
    partes: list[tuple[str, bytes]] = []
    avisos: list[str] = []

    if tipo in ("completo", "folha"):
        folha = await cad.baixar_pdf(page, cad.SERVLET_FOLHA_RESUMO)
        if folha:
            partes.append(("Folha Resumo", folha))
            log("info", f"Família {cod}: Folha Resumo baixada.")
        elif completo:
            avisos.append("A Folha Resumo não foi emitida pelo sistema.")
        else:
            raise RuntimeError(f"família {cod}: a Folha Resumo não foi emitida pelo sistema")

    if completo:
        principal = await cad.baixar_pdf(page, cad.SERVLET_PRINCIPAL)
        if not principal:
            raise RuntimeError(f"família {cod}: o Formulário Principal não foi emitido pelo sistema")
        partes.append(("Formulário Principal (inclui os suplementares preenchidos)", principal))
        log("info", f"Família {cod}: Formulário Principal baixado.")

        pags_principal = await asyncio.to_thread(_texto_paginas, principal)
        for n in await asyncio.to_thread(_suplementares_preenchidos, principal):
            sup = await cad.baixar_pdf(page, cad.SERVLET_SUPLEMENTAR.format(n=n))
            if not sup:
                avisos.append(f"O Formulário Suplementar {n} consta como preenchido, mas o sistema não o emitiu.")
            elif not await asyncio.to_thread(_ja_no_principal, pags_principal, sup):
                partes.append((f"Formulário Suplementar {n}", sup))

    # motivos de exclusão ANTES do histórico (o histórico sai da Visualizar
    # Família). Em todos os tipos: data/motivo vão para a tabela de
    # integrantes; o PDF de cada motivo só entra no completo.
    exclusoes = await cad.baixar_motivos_exclusao(page, com_pdf=completo)
    _anotar_exclusoes(dados.get("integrantes") or [], exclusoes)
    n_excl = sum(1 for i in dados.get("integrantes") or [] if i.get("excluido"))
    n_pdf = sum(1 for e in exclusoes if e.get("pdf"))
    if completo and n_excl and n_pdf < n_excl:
        avisos.append(f"{n_excl} integrante(s) excluído(s), mas só {n_pdf} motivo(s) de "
                      "exclusão foram emitidos pelo sistema.")

    if tipo in ("completo", "historico"):
        hist = await cad.baixar_historico_familia(page, DATA_INICIO_HISTORICO)
        if hist:
            partes.append(("Histórico da Família (desde 01/01/1950, todos os campos)", hist))
            log("info", f"Família {cod}: Histórico da Família baixado.")
        elif completo:
            avisos.append("Histórico da Família: nenhuma movimentação retornada pelo sistema.")
        else:
            raise RuntimeError(f"família {cod}: o Histórico da Família não retornou movimentações")

    for e in exclusoes:
        if e.get("pdf"):
            partes.append((f"Motivo de exclusão — {e.get('nome') or 'integrante excluído'}", e["pdf"]))
    if n_pdf:
        log("info", f"Família {cod}: {n_pdf} motivo(s) de exclusão baixado(s).")
    return dados, partes, avisos


def _anotar_exclusoes(integrantes: list[dict], exclusoes: list[dict]) -> None:
    """Põe `data_exclusao`/`motivo_exclusao` no integrante correspondente
    (pelo NIS; sem NIS, pelo nome) — vão para a tabela da capa."""
    def so_dig(s: str) -> str:
        return re.sub(r"\D", "", s or "")

    def nome_de(it: dict) -> str:  # célula 0 = "1 - NOME"
        c = (it.get("celulas") or [""])[0]
        return re.sub(r"^\s*\d+\s*-\s*", "", c).strip().upper()

    for e in exclusoes:
        alvo = None
        if so_dig(e.get("nis")):
            alvo = next((it for it in integrantes
                         if so_dig((it.get("celulas") or ["", ""])[1]) == so_dig(e["nis"])), None)
        if alvo is None and e.get("nome"):
            alvo = next((it for it in integrantes if nome_de(it) == e["nome"].strip().upper()), None)
        if alvo is not None:
            alvo["data_exclusao"] = e.get("data") or ""
            alvo["motivo_exclusao"] = e.get("motivo") or ""


async def _executar(paj_norm: str, sess: dict) -> None:
    from ingestao import cadunico_client as cad

    def log(level: str, msg: str) -> None:
        sess["log"].append({"level": level, "msg": msg,
                            "ts": datetime.now().isoformat(timespec="seconds")})

    cpf, nome, paj_label = _dados_assistido(paj_norm)
    cpf_fmt = _formatar_cpf(cpf)
    if not cpf_fmt:
        raise RuntimeError("CPF do assistido não encontrado no PAJ.")
    destino_dir = PAJS_DIR / paj_norm / SUBPASTA

    coletas: list[tuple[dict, list[tuple[str, bytes]], list[str]]] = []
    pw = browser = None
    try:
        pw, browser, page = await cad.conectar()
        log("info", "Abrindo o Cadastro Único…")
        if not await cad.garantir_login(page, log):
            raise RuntimeError("Login no CadÚnico não concluído (tempo esgotado).")
        sess["aguardando_login"] = False
        log("ok", "Sessão ativa no CadÚnico.")

        for tentativa in (1, 2):
            try:
                familias = await cad.buscar_familias(page, cpf_fmt)
                break
            except cad.SessaoExpirada:
                if tentativa == 2 or not await cad.garantir_login(page, log):
                    raise RuntimeError("Sessão do CadÚnico expirou e o novo login não foi concluído.")
        if not familias:
            raise RuntimeError(f"Nenhuma família encontrada no CadÚnico para o CPF {cpf_fmt}.")
        log("info", f"{len(familias)} família(s) encontrada(s) para o CPF {cpf_fmt}.")

        for fam in familias:
            for tentativa in (1, 2):
                try:
                    coletas.append(await _coletar_familia(page, cpf_fmt, fam, log, sess["tipo"]))
                    break
                except cad.SessaoExpirada:
                    if tentativa == 2 or not await cad.garantir_login(page, log):
                        raise RuntimeError("Sessão do CadÚnico expirou e o novo login não foi concluído.")
                    log("aviso", "Sessão expirou — relogado, repetindo a família…")
    finally:
        # NÃO fecha o Chromium: o login é manual; a sessão fica para o próximo dossiê
        await cad.desconectar(pw, browser)

    emitido = datetime.now().strftime("%d/%m/%Y %H:%M")
    cfg = TIPOS[sess["tipo"]]
    for dados, partes, avisos in coletas:
        cod = dados.get("codigo_familiar") or "?"
        log("info", f"Família {cod}: montando o PDF…")
        capa = {"titulo": cfg["titulo"], "paj": paj_label, "nome": nome, "cpf": cpf_fmt,
                "emitido_em": emitido, "familia": dados, "avisos": avisos}
        destino = destino_dir / _nome_arquivo(cod, nome, cfg["arquivo"])
        res = await asyncio.to_thread(_montar, destino, capa, partes)
        arq = Path(res.get("arquivo") or "").name
        if not arq:
            raise RuntimeError("o montador não informou o arquivo gerado")
        sess["arquivos"].append(arq)
        log("ok", f"Família {cod}: dossiê salvo em {SUBPASTA}/{arq}")
        for a in avisos:
            log("aviso", f"Família {cod}: {a}")


def iniciar(paj_norm: str, tipo: str = "completo") -> dict:
    if tipo not in TIPOS:
        return {"ok": False, "erro": "Tipo de dossiê inválido."}
    if not (PAJS_DIR / paj_norm).exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    with _lock:
        if any(s.get("status") == "running" for s in _execucoes.values()):
            return {"ok": False, "erro": "Já há um Dossiê CadÚnico sendo gerado "
                                         "(a sessão do CadÚnico só atende uma coleta por vez)."}
        sess = {"status": "running", "tipo": tipo, "rotulo": TIPOS[tipo]["rotulo"],
                "mov_descricao": TIPOS[tipo]["mov"], "log": [], "arquivos": [],
                "juntada": None, "aguardando_login": True}
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
        sess["aguardando_login"] = False
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "dossie_cadunico", status=sess["status"],
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


async def _juntar(paj_norm: str, sess: dict) -> None:
    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service

    def log(level: str, msg: str) -> None:
        sess["log"].append({"level": level, "msg": msg,
                            "ts": datetime.now().isoformat(timespec="seconds")})

    try:
        meta = _ler_meta(paj_norm)
        paj_label = (meta.get("paj") or "").strip()
        pasta = PAJS_DIR / paj_norm / SUBPASTA
        anexos = [{"nome": n, "caminho": str(pasta / n), "tipo": "pdf"}
                  for n in sess.get("arquivos") or [] if (pasta / n).is_file()]
        if not anexos:
            raise RuntimeError(f"PDF do dossiê não encontrado em {SUBPASTA}/.")
        id_proc, eventos, erro = await acoes_sisdpu_service._resolver_id_processo(
            _id_processo_local(meta), paj_label)
        for ev in eventos:
            log(ev.get("level") or "info", ev.get("msg") or "")
        if not id_proc:
            raise RuntimeError(erro or "id do PAJ no SISDPU não encontrado.")
        log("info", "Movimentando no SISDPU (juntada de documento)…")
        pajs = [{"id": id_proc, "numero": paj_label}]
        mv, exc, eventos = await acoes_sisdpu_service.com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, sess["mov_descricao"], sat_service.FASE_MOV, anexos,
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
            historico.registrar(paj_norm, "dossie_cadunico_juntada", status="ok",
                                resumo=f"{len(anexos)} arquivo(s) juntado(s) ao SISDPU")
        with contextlib.suppress(Exception):
            from services import historico_painel

            historico_painel.registrar({
                "paj": paj_label, "paj_norm": paj_norm,
                "url_detalhe": (meta.get("sisdpu_raw") or {}).get("url", ""),
                "assistido": meta.get("assistido_caixa") or "",
                "origem": "cadunico",
                "atividade": f"Juntada: {sess['mov_descricao']}",
                "movimentacao": sat_service.FASE_MOV, "tramitado_para": "",
                "concluido": False,
                "detalhe": f"{len(anexos)} arquivo(s) juntado(s).",
            })
    except Exception as e:  # noqa: BLE001
        sess["juntada"] = "erro"
        sess["erro_juntada"] = f"{e}"[:400]
        log("erro", f"Juntada ao SISDPU: {sess['erro_juntada']}")
        with contextlib.suppress(Exception):
            historico.registrar(paj_norm, "dossie_cadunico_juntada", status="error",
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
