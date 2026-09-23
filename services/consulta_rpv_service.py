"""Botão "Consulta RPV/Precatório" (seção "Ações com IA" da página do PAJ) —
verifica se cada RPV/Precatório do(s) processo(s) judicial(is) vinculado(s) ao
PAJ já foi PAGO, consultando o site público do banco depositário (CEF hoje; BB
ainda sem suporte, sem caso real para validar o formulário — ver
`ingestao.rpv_precatorio_client.consultar_bb`), e registra automaticamente a
movimentação correspondente no SISDPU (uma por RPV/Precatório encontrado).

**ONE-CLICK, sem nenhum dado digitado pelo Defensor** (pedido explícito do
usuário, 2026-09-22): tudo que a rotina precisa já está no processo e no PAJ —

  1. Acha o(s) processo(s) judicial(is) vinculado(s) ao PAJ (`metadata.json`).
  2. Para cada processo, varre a timeline do e-proc (via `eproc_mcp_client`,
     sem navegador) procurando eventos de "Requisição de pagamento de pequeno
     valor paga" cujo documento anexado seja um DEMTRANSF — NUNCA um REQPAGAM
     (esse é só o envio da requisição ao Tribunal, antes do pagamento; ver
     docstring de `_e_demtransf`).
  3. Baixa e lê cada DEMTRANSF, extraindo Banco e "Conta Depósito" do rodapé
     (Agência/Operação da CEF são constantes fixas — ver
     `rpv_precatorio_client._CEF_AGENCIA_FIXA` — não variam por credor).
  4. Consulta o site do banco (CEF hoje). Sem pagamento → movimentação
     "Aguardando tramitação judicial/administrativa". Com pagamento → baixa o
     comprovante e movimentação "Juntada de documento", com o comprovante
     anexado.
  5. Executa TODAS as movimentações encontradas numa ÚNICA fila do SISDPU
     (`acoes_sisdpu_service.executar_fila_stream`) — uma por credor/DEMTRANSF.

O comprovante baixado do banco NÃO é uma peça do PAJ (não vai para "Peças
IA") — é só um anexo de passagem para a movimentação no SISDPU. Fica staged em
`_anexos_externos_tmp/` (pasta temporária dentro da pasta do PAJ, mesmo
mecanismo de upload externo de `acoes_sisdpu_service.salvar_anexo_externo`) e é
apagado automaticamente pelo próprio SISDPU service quando a movimentação é
confirmada com sucesso (`acoes_sisdpu_service._executar_um_item_stream`) —
pedido explícito do usuário: "não deve ser salvo em Peças IA... apagado depois
que houver a juntada com sucesso".

Estado persistido em `consulta_rpv.json` (1 por PAJ) evita reconsultar o banco
para um DEMTRANSF já confirmado como pago em execução anterior (pagamento é
definitivo — não muda); os demais status (sem dados, bloqueado por anti-robô,
indisponível, erro) SÃO reconsultados a cada clique, porque podem ter mudado
ou podem ter sido só uma falha transitória."""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from ingestao import eproc_mcp_client, rpv_precatorio_client
from services import historico

_STATE_FILE = "consulta_rpv.json"

# {paj_norm: {"status": "running"|"done"|"error", "log": [...], "resultados": {...}}}
_execucoes: dict[str, dict] = {}
_lock = threading.Lock()


def _norm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _arquivo_estado(paj_norm: str) -> Path:
    return PAJS_DIR / paj_norm / _STATE_FILE


def _ler_estado(paj_norm: str) -> dict:
    f = _arquivo_estado(paj_norm)
    if not f.exists():
        return {"resultados": {}}
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc.setdefault("resultados", {})
        return doc
    except Exception:
        return {"resultados": {}}


def _salvar_estado(paj_norm: str, doc: dict) -> None:
    with contextlib.suppress(Exception):
        (PAJS_DIR / paj_norm).mkdir(parents=True, exist_ok=True)
        _arquivo_estado(paj_norm).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def get_status(paj_norm: str) -> dict:
    with _lock:
        exec_mem = _execucoes.get(paj_norm)
        if exec_mem:
            return dict(exec_mem)
    disco = _ler_estado(paj_norm)
    return {"status": disco.get("status", "idle"), "log": disco.get("log", []),
            "resultados": disco.get("resultados", {})}


# --------------------------------------------------------------------------
# Localização dos processos vinculados e dos eventos de RPV paga (DEMTRANSF)
# --------------------------------------------------------------------------

def _ler_metadata(paj_norm: str) -> dict | None:
    p = PAJS_DIR / paj_norm / "metadata.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _processos_do_paj(metadata: dict) -> list[dict]:
    """Mesma extração de `services.retornos_service._extrair_processos`
    (duplicada aqui — cada módulo "Ações no X"/"Consulta X" deste painel é
    autocontido, mesma convenção já usada em `acoes_sisdpu_service.py` e
    `mera_ciencia_service.py`): TODOS os processos judiciais vinculados ao
    PAJ, priorizando a lista completa `processos_judiciais`, com fallback pro
    campo singular antigo."""
    procs = metadata.get("processos_judiciais")
    if not procs and metadata.get("processo_judicial"):
        procs = [{"processo_judicial": metadata["processo_judicial"]}]
    out = []
    for p in (procs or []):
        numero = (p.get("processo_judicial") or "").strip()
        if numero:
            out.append(numero)
    return out


def _id_processo_sisdpu(metadata: dict) -> str:
    """Mesma extração de `mera_ciencia_service._id_processo_sisdpu` — o `id`
    interno do PAJ no SISDPU vem embutido na URL de detalhamento gravada pelo
    último sync (`sisdpu_raw.url`). Vazio se o PAJ nunca foi sincronizado;
    nesse caso `acoes_sisdpu_service.executar_fila_stream` ainda consegue
    resolver pelo NÚMERO do PAJ (ver `_resolver_id_processo`)."""
    url = ((metadata.get("sisdpu_raw") or {}).get("url")) or ""
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else ""


def _e_demtransf(ev: dict) -> list[dict]:
    """Documentos deste evento cuja sigla/nome contenha "DEMTRANSF" — o
    demonstrativo de transferência que só existe DEPOIS que a RPV/precatório
    foi efetivamente PAGA (evento "Requisição de pagamento de pequeno valor
    paga"). NUNCA confundir com "REQPAGAM" (envio da requisição ao Tribunal,
    ANTES do pagamento) — filtra explicitamente fora, mesmo que por algum
    motivo apareça junto no mesmo evento (não deveria, mas o usuário pediu
    atenção explícita a essa distinção)."""
    docs = ev.get("documentos") or []
    achados = []
    for d in docs:
        rotulo = _norm(str(d.get("sigla") or d.get("nome") or ""))
        if "reqpagam" in rotulo:
            continue
        if "demtransf" in rotulo:
            achados.append(d)
    return achados


async def _triagem_com_fallback_instancia(numero: str, chave: str | None) -> tuple[dict, int]:
    """Tenta instância 1 (JEF/vara federal — caso comum de RPV) e cai pra 2 se
    a 1ª não achar o processo. Retorna (resultado_triagem, instancia_usada)."""
    for inst in (1, 2):
        res = await eproc_mcp_client.triagem_processo(numero, instancia=inst, todos_eventos=True, chave=chave)
        if not res.get("erro"):
            return res, inst
    return res, 2  # devolve o erro da 2ª tentativa (mais informativo que desistir na 1ª)


# --------------------------------------------------------------------------
# Extração de Banco / Conta Depósito / credor do texto do DEMTRANSF
# --------------------------------------------------------------------------

_RE_CONTA_DEPOSITO = re.compile(r"conta\s+dep[óo]sito\s*:?\s*(\d{5,})", re.IGNORECASE)
_RE_BANCO = re.compile(r"banco\s*:?\s*([a-zà-ú0-9 .\-]{2,40})", re.IGNORECASE)
_RE_CREDOR = re.compile(
    r"(?:benefici[áa]ri[oa]|favorecid[oa]|requerente|autor)\s*:?\s*([^\n\r]{3,120})",
    re.IGNORECASE,
)


def _extrair_texto_pdf(caminho: Path) -> str:
    """Texto de TODAS as páginas (não só a 1ª — o dado do banco fica "ao final
    do arquivo, antes das notas explicativas", conforme o usuário descreveu)."""
    texto = ""
    with contextlib.suppress(Exception):
        import fitz  # PyMuPDF

        with fitz.open(caminho) as doc:
            texto = "\n".join(pg.get_text() for pg in doc)
    if texto.strip():
        return texto
    # fallback: companion .txt (documento HTML convertido pelo eproc_mcp_client)
    txt = caminho.with_suffix(".txt")
    if txt.exists():
        with contextlib.suppress(Exception):
            return txt.read_text(encoding="utf-8", errors="replace")
    return ""


def _parsear_demtransf(texto: str) -> dict:
    """{"banco": "CEF"|"BB"|"", "conta_deposito": "161710049", "credor": "..."}
    — heurística sobre o texto extraído; AINDA NÃO VALIDADA contra um DEMTRANSF
    real (ver ressalva no topo do módulo `rpv_precatorio_client`). `credor`
    fica vazio se nenhum rótulo conhecido for encontrado — o chamador degrada
    graciosamente nesse caso (usa o número do processo na descrição da
    movimentação em vez do nome)."""
    m_banco = _RE_BANCO.search(texto)
    banco_raw = _norm(m_banco.group(1)) if m_banco else ""
    if "cef" in banco_raw or "caixa" in banco_raw:
        banco = "CEF"
    elif "bb" in banco_raw or "brasil" in banco_raw:
        banco = "BB"
    else:
        banco = ""

    m_conta = _RE_CONTA_DEPOSITO.search(texto)
    conta_deposito = m_conta.group(1) if m_conta else ""

    m_credor = _RE_CREDOR.search(texto)
    credor = re.sub(r"\s{2,}.*$", "", (m_credor.group(1).strip() if m_credor else ""))

    return {"banco": banco, "conta_deposito": conta_deposito, "credor": credor}


# --------------------------------------------------------------------------
# Orquestração principal
# --------------------------------------------------------------------------

def _chave_item(numero_processo: str, evento_numero: str, doc: dict) -> str:
    doc_id = str(doc.get("idDocumento") or doc.get("id") or doc.get("nome") or doc.get("sigla") or "")
    return f"{re.sub(r'[^0-9]', '', numero_processo)}:{evento_numero}:{doc_id}"


async def _coletar_itens_pendentes(paj_norm: str, metadata: dict, log) -> list[dict]:
    """1 item por DEMTRANSF encontrado em qualquer processo vinculado ao PAJ
    (independente de já ter sido consultado antes — a decisão de RECONSULTAR
    o banco ou não é tomada depois, item a item, em `_processar_item`)."""
    itens: list[dict] = []
    processos = _processos_do_paj(metadata)
    if not processos:
        log("muted", "Nenhum processo judicial vinculado a este PAJ.")
        return itens

    from services import paj_service

    for numero in processos:
        chave_info = paj_service.buscar_chave_por_processo(numero)
        chave = (chave_info or {}).get("chave")
        log("info", f"{numero}: consultando a timeline do e-proc (triagem)…")
        res, inst = await _triagem_com_fallback_instancia(numero, chave)
        if res.get("erro"):
            log("erro", f"{numero}: {res.get('erro')}")
            continue
        eventos = res.get("eventos") or []
        encontrados = 0
        for ev in eventos:
            docs_demtransf = _e_demtransf(ev)
            if not docs_demtransf:
                continue
            evento_numero = str(ev.get("numero") or "")
            for doc in docs_demtransf:
                itens.append({
                    "processo": numero, "instancia": inst, "chave": chave,
                    "evento": evento_numero, "doc": doc,
                    "chave_item": _chave_item(numero, evento_numero, doc),
                })
                encontrados += 1
        log("info" if encontrados else "muted",
            f"{numero}: {encontrados} RPV/Precatório(s) paga(s) com DEMTRANSF encontrada(s)."
            if encontrados else f"{numero}: nenhuma RPV/Precatório paga (DEMTRANSF) encontrada.")
    return itens


async def _processar_item(item: dict, estado_anterior: dict, log) -> tuple[dict, bytes | None]:
    """Baixa/lê o DEMTRANSF e consulta o banco, SALVO se `estado_anterior` já
    marcar este item como definitivamente pago (não reconsulta o banco à
    toa — ver docstring do módulo). Retorna sempre (resultado, pdf_bytes) —
    `pdf_bytes` só vem preenchido quando `status == "pago"` NESTA execução
    (um pagamento já confirmado antes não tem o PDF re-baixado)."""
    numero, evento_numero = item["processo"], item["evento"]
    ja = estado_anterior.get(item["chave_item"]) or {}
    resultado = dict(ja)
    resultado["processo"] = numero
    resultado["evento"] = evento_numero

    if ja.get("status") == "pago":
        log("muted", f"{numero} (evento {evento_numero}): já confirmado como pago antes — não reconsulta o banco.")
        return resultado, None

    log("info", f"{numero} (evento {evento_numero}): baixando o DEMTRANSF do e-proc…")
    try:
        res_dl = await eproc_mcp_client.baixar_documentos_evento(
            numero, item["instancia"], [evento_numero], chave=item["chave"])
    except Exception as e:  # noqa: BLE001
        resultado.update(status="erro", mensagem=f"Falha ao baixar o DEMTRANSF: {type(e).__name__}: {e}"[:300])
        log("erro", f"{numero} (evento {evento_numero}): {resultado['mensagem']}")
        return resultado, None

    arquivos = (res_dl.get("arquivos_por_evento") or {}).get(evento_numero) or []
    alvo = next((a for a in arquivos if "demtransf" in _norm(a.get("nome_arquivo", ""))), None)
    if not alvo:
        resultado.update(status="erro", mensagem="DEMTRANSF não encontrado entre os documentos baixados do evento.")
        log("erro", f"{numero} (evento {evento_numero}): {resultado['mensagem']}")
        return resultado, None

    caminho = eproc_mcp_client.EPROC_DOCS_DIR / re.sub(r"\D", "", numero) / alvo["arquivo"]
    texto = _extrair_texto_pdf(caminho)
    dados = _parsear_demtransf(texto)
    resultado["credor"] = dados["credor"]
    resultado["banco"] = dados["banco"]

    if not dados["conta_deposito"] or len(dados["conta_deposito"]) < 2:
        resultado.update(status="erro", mensagem="Não consegui ler \"Conta Depósito\" no DEMTRANSF — parsing "
                                                  "precisa de ajuste (ver ingestao/consulta_rpv_service.py).")
        log("erro", f"{numero} (evento {evento_numero}): {resultado['mensagem']}")
        return resultado, None

    conta, dv = dados["conta_deposito"][:-1], dados["conta_deposito"][-1]
    credor_label = dados["credor"] or f"credor do processo {numero}"

    if dados["banco"] == "CEF":
        log("info", f"{numero}: consultando a CEF (conta {conta}-{dv}) — {credor_label}…")
        res_banco = await rpv_precatorio_client.consultar_cef(conta, dv)
    elif dados["banco"] == "BB":
        res_banco = await rpv_precatorio_client.consultar_bb(conta, dv)
    else:
        resultado.update(status="erro", mensagem=f"Banco não identificado no DEMTRANSF (texto lido: "
                                                  f"{dados['banco'] or '—'!r}).")
        log("erro", f"{numero} (evento {evento_numero}): {resultado['mensagem']}")
        return resultado, None

    status_banco = res_banco.get("status")
    resultado["status"] = status_banco
    resultado["mensagem"] = res_banco.get("mensagem", "")
    if status_banco == "pago":
        log("ok", f"{numero}: pagamento CONFIRMADO — {credor_label}.")
    elif status_banco == "sem_dados":
        log("info", f"{numero}: ainda SEM pagamento — {credor_label}.")
    else:
        log("erro" if status_banco == "erro" else "muted",
            f"{numero}: {res_banco.get('mensagem') or status_banco} — {credor_label}.")
    return resultado, res_banco.get("pdf_bytes")


def _nome_arquivo_seguro(s: str) -> str:
    s = re.sub(r"[^\w\-. ]", "_", s, flags=re.UNICODE).strip() or "comprovante"
    return s[:120]


async def _executar(paj_norm: str, sess: dict) -> None:
    log_lock = threading.Lock()

    def log(level: str, msg: str) -> None:
        with log_lock:
            sess["log"].append({"level": level, "msg": msg, "ts": datetime.now().isoformat(timespec="seconds")})

    metadata = _ler_metadata(paj_norm)
    if not metadata:
        sess["status"] = "error"
        log("erro", "metadata.json do PAJ não encontrado.")
        return

    estado_anterior = _ler_estado(paj_norm).get("resultados", {})
    itens = await _coletar_itens_pendentes(paj_norm, metadata, log)
    if not itens:
        sess["status"] = "done"
        log("muted", "Nenhuma RPV/Precatório paga (DEMTRANSF) encontrada em nenhum processo vinculado.")
        _salvar_estado(paj_norm, {"status": "done", "log": sess["log"], "resultados": estado_anterior,
                                   "atualizado_em": datetime.now().isoformat(timespec="seconds")})
        return

    from services import acoes_sisdpu_service

    novos_resultados = dict(estado_anterior)
    fila_itens: list[dict] = []
    fila_chaves: list[str] = []

    for item in itens:
        resultado, pdf_bytes = await _processar_item(item, estado_anterior, log)
        resultado["consultado_em"] = datetime.now().isoformat(timespec="seconds")
        resultado.setdefault("registrado_sisdpu", False)
        novos_resultados[item["chave_item"]] = resultado

        if resultado.get("status") == "pago" and not resultado.get("registrado_sisdpu"):
            credor_label = resultado.get("credor") or f"credor do processo {resultado['processo']}"
            nome_arquivo = _nome_arquivo_seguro(
                f"Comprovante RPV - {resultado.get('banco', '')} - {credor_label}.pdf")
            anexos = []
            if pdf_bytes:
                res_anexo = acoes_sisdpu_service.salvar_anexo_externo(paj_norm, nome_arquivo, pdf_bytes)
                if res_anexo.get("ok"):
                    anexos = [{"origem": "externo", "ref": res_anexo["ref"],
                               "descricao": f"Comprovante de pagamento RPV/Precatório — {credor_label}",
                               "tipo_arquivo": ""}]
                else:
                    log("erro", f"Falha ao preparar o comprovante como anexo: {res_anexo.get('detalhe')}")
            fila_itens.append({
                "fase": "Juntada de documento",
                "descricao_mov": f"PAGAMENTO EFETUADO: Comprovante de pagamento da RPV/Precatório "
                                  f"referente a {credor_label}",
                "anexos": anexos,
            })
            fila_chaves.append(item["chave_item"])
        elif resultado.get("status") == "sem_dados" and not resultado.get("registrado_sisdpu"):
            credor_label = resultado.get("credor") or f"credor do processo {resultado['processo']}"
            fila_itens.append({
                "fase": "Aguardando tramitação judicial/administrativa",
                "descricao_mov": f"AGUARDANDO PAGAMENTO: Em consulta, ainda não há Comprovante de "
                                  f"pagamento da RPV/Precatório referente a {credor_label}",
                "anexos": [],
            })
            fila_chaves.append(item["chave_item"])
        elif resultado.get("status") == "pago" and resultado.get("registrado_sisdpu"):
            pass  # já concluído numa execução anterior — nada a fazer
        else:
            log("muted", f"{resultado['processo']} (evento {resultado['evento']}): não entrou na fila do SISDPU "
                          f"agora ({resultado.get('status')}) — resolva manualmente se persistir.")

    if not fila_itens:
        sess["status"] = "done"
        log("muted", "Nenhuma movimentação nova para registrar no SISDPU (tudo já resolvido, ou nenhum item "
                      "ficou pronto — ver mensagens acima).")
        _salvar_estado(paj_norm, {"status": "done", "log": sess["log"], "resultados": novos_resultados,
                                   "atualizado_em": datetime.now().isoformat(timespec="seconds")})
        return

    id_processo = _id_processo_sisdpu(metadata)
    paj_numero = metadata.get("paj", "")
    log("info", f"Registrando {len(fila_itens)} movimentação(ões) no SISDPU (mesma sessão)…")
    itens_concluidos: list[int] = []
    async for ev in acoes_sisdpu_service.executar_fila_stream(id_processo, paj=paj_numero, paj_norm=paj_norm,
                                                              itens=fila_itens):
        if ev.get("item_done"):
            continue
        if ev.get("done"):
            itens_concluidos = (ev.get("resultado") or {}).get("itens_concluidos") or []
        else:
            log(ev.get("level") or "info", ev.get("msg") or str(ev))

    for idx in itens_concluidos:
        novos_resultados[fila_chaves[idx]]["registrado_sisdpu"] = True

    ok_total = len(itens_concluidos) == len(fila_itens)
    sess["status"] = "done" if ok_total else "error"
    log("ok" if ok_total else "erro",
        f"Fila concluída: {len(itens_concluidos)}/{len(fila_itens)} movimentação(ões) registrada(s) no SISDPU.")

    _salvar_estado(paj_norm, {"status": sess["status"], "log": sess["log"], "resultados": novos_resultados,
                              "atualizado_em": datetime.now().isoformat(timespec="seconds")})
    with contextlib.suppress(Exception):
        historico.registrar(paj_norm, "consulta_rpv", status=sess["status"],
                            resumo=f"{len(itens_concluidos)}/{len(fila_itens)} movimentação(ões) de "
                                   f"RPV/Precatório registrada(s).")
    if ok_total:
        with contextlib.suppress(Exception):
            from services import lembretes_service

            lembretes_service.criar_lembrete(paj_norm, "Consulta de RPV/Precatório concluída", "vermelho")


def iniciar(paj_norm: str) -> dict:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    with _lock:
        existente = _execucoes.get(paj_norm)
        if existente and existente.get("status") == "running":
            return {"ok": False, "erro": "Já há uma consulta de RPV/Precatório em andamento para este PAJ."}
        sess = {"status": "running", "log": []}
        _execucoes[paj_norm] = sess

    def _run() -> None:
        import asyncio

        try:
            asyncio.run(_executar(paj_norm, sess))
        except Exception as e:  # noqa: BLE001
            sess["status"] = "error"
            sess["log"].append({"level": "erro", "msg": f"{type(e).__name__}: {e}"[:300],
                                "ts": datetime.now().isoformat(timespec="seconds")})
            _salvar_estado(paj_norm, {"status": "error", "log": sess["log"],
                                      "resultados": _ler_estado(paj_norm).get("resultados", {})})
        finally:
            time.sleep(1)  # respiro pro front terminar de ler o último poll antes de sumir da memória
            with _lock:
                _execucoes.pop(paj_norm, None)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
