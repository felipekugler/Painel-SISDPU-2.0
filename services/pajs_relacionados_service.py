"""Seção "PAJs relacionados" da página do PAJ (pedido do Defensor, 16/10/2026).

Duas relações, lidas do SIS-DPU 2.0 (MCP) só quando a seção é aberta:

  - OUTROS PAJs DO ASSISTIDO: pela busca por CPF (`pessoa_pesquisar`). A
    ferramenta própria (`pajs_da_pessoa`) devolve lista vazia mesmo quando o
    assistido tem outros PAJs (testado em 16/10/2026 — PAJ 2026/029-02670,
    que no SIS 2 web mostra 3 relacionados); a busca por CPF traz os mesmos da
    tela do SIS 2, com ofício, pretensão, processo judicial e arquivamento.
  - APENSOS (`paj_apensos`), nos dois sentidos: "apensos" = PAJs apensados A
    ESTE; o histórico mostra também o PAJ ao qual ESTE foi apensado (pelo PAJ
    apensado, a lista de apensos vem vazia — o vínculo só aparece no histórico).
"""

from __future__ import annotations

import asyncio
import json
import re

from config import PAJS_DIR
from ingestao import sisdpu2_mcp_client as sis2


def _paj_norm_de(numero: str) -> str:
    """'2026/029_02670' ou '2026/029-02670' → 'PAJ-2026-029-02670'."""
    m = re.match(r"(\d{4})/(\d{3})[-_](\d+)", numero or "")
    return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _numero(numero: str) -> str:
    return (numero or "").replace("_", "-")


def _dados_locais(paj_norm: str) -> tuple[int | None, list[dict]]:
    """id do PAJ no SISDPU e assistidos (nome, CPF, idPessoa) do metadata."""
    try:
        m = json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, []
    raw = m.get("sisdpu_raw") or {}
    achado = re.search(r"[?&]id=(\d+)", raw.get("url") or "")
    idp = int(achado.group(1)) if achado else None
    assistidos = [{"nome": a.get("nome", ""), "cpf": re.sub(r"\D", "", a.get("cpf") or "")}
                  for a in (raw.get("assistidos_pf") or []) if re.sub(r"\D", "", a.get("cpf") or "")]
    if not assistidos and raw.get("cpf_assistido"):
        assistidos = [{"nome": raw.get("assistido", ""), "cpf": re.sub(r"\D", "", raw["cpf_assistido"])}]
    return idp, assistidos


# Data de instauração e etapa de cada PAJ relacionado (22/10/2026, pedido do
# Defensor). PAJ com pasta no painel: metadata + etapa_paj_service (com as
# evidências automáticas). Sem pasta: capa (data) e as últimas movimentações
# do SIS 2, pela MESMA regra de etapa (movimentação "Etapa: …", arquivamento,
# "Petição inicial") — sem as evidências que só existem na pasta local.
_MOVS_ETAPA = 60  # cada movimentação lida conta 1 registro no teto diário


def _data_br(valor: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", valor or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", valor or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def _etapa_resumo(e: dict) -> dict:
    return {"chave": e.get("chave"), "rotulo": e.get("rotulo"), "cor_fundo": e.get("cor_fundo"),
            "cor_texto": e.get("cor_texto"), "icone": e.get("icone"), "arquivado": bool(e.get("arquivado"))}


def _detalhes(paj_norm: str, id_processo) -> dict:
    """{data_abertura (ISO), etapa} de um PAJ relacionado. Falha → vazio."""
    from services import etapa_paj_service

    pasta = PAJS_DIR / paj_norm if paj_norm else None
    if pasta and (pasta / "metadata.json").exists():
        try:
            meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
            data = _data_br(meta.get("data_abertura") or (meta.get("sisdpu_raw") or {}).get("data_abertura") or "")
            return {"data_abertura": data, "etapa": _etapa_resumo(etapa_paj_service.calcular(paj_norm, meta)),
                    "local": True}
        except Exception:  # noqa: BLE001
            pass
    if not id_processo:
        return {"data_abertura": "", "etapa": None}
    out = {"data_abertura": "", "etapa": None, "local": False}
    try:
        capa = sis2.chamar("paj_capa", {"idProcesso": int(id_processo), "comHistorico": False}) or {}
        out["data_abertura"] = _data_br(str(capa.get("dataAbertura") or ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        r = sis2.chamar("paj_movimentacoes", {"idProcesso": int(id_processo), "limite": _MOVS_ETAPA}) or {}
        itens = r.get("itens") or []
        movs = []
        for i, mv in enumerate(itens):
            dh = str(mv.get("dataHora") or "")
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})", dh)
            if not m:
                continue
            movs.append({"seq": len(itens) - i, "fases": mv.get("fases") or "",
                         "descricao": mv.get("descricao") or "",
                         "data_original": f"{m.group(3)}/{m.group(2)}/{m.group(1)} {m.group(4)}"})
        meta = {"detalhes_sisdpu": {"movimentacoes": movs}}
        out["etapa"] = _etapa_resumo(etapa_paj_service.calcular(paj_norm or "PAJ-0000-000-00000", meta))
    except Exception:  # noqa: BLE001
        pass
    return out


def _ler(paj_norm: str) -> dict:
    idp, assistidos = _dados_locais(paj_norm)
    if not idp:
        return {"ok": False, "erro": "PAJ sem identificação no SISDPU — sincronize o PAJ."}
    if not assistidos:  # sem CPF no metadata: lê da capa
        capa = sis2.chamar("paj_capa", {"idProcesso": idp, "comHistorico": False}) or {}
        assistidos = [{"nome": a.get("nome", ""), "cpf": re.sub(r"\D", "", a.get("cpfCnpj") or "")}
                      for a in capa.get("assistidos") or [] if a.get("cpfCnpj")]

    outros: dict[int, dict] = {}
    for a in assistidos:
        r = sis2.chamar("pessoa_pesquisar", {"cpfOuCnpj": a["cpf"], "limite": 50}) or {}
        for it in r.get("itens") or []:
            pid = it.get("idProcesso")
            if not pid or pid == idp or pid in outros:
                continue
            outros[pid] = {
                "numero": _numero(it.get("numeroProcessoFormatado")),
                "paj_norm": _paj_norm_de(it.get("numeroProcessoFormatado")),
                "assistido": it.get("nomeAssistido") or a["nome"],
                "oficio": it.get("dsOficio") or "",
                "unidade": it.get("noUnidade") or "",
                "pretensao": it.get("pretensao") or "",
                "processo_judicial": it.get("processoJudicial") or "",
                "arquivado": bool(it.get("arquivado")),
                "id_processo": pid,
            }

    ap = sis2.chamar("paj_apensos", {"idProcesso": idp, "comHistorico": True}) or {}
    ids_apensos = {x.get("idProcesso") for x in ap.get("apensos") or []}
    historico = sorted(ap.get("historico") or [], key=lambda h: h.get("data") or "")
    # Último ato do histórico para cada PAJ: "Apensado" a um PAJ que NÃO está na
    # lista de apensos = ESTE PAJ foi apensado a ele (visto pelo lado do apensado).
    ultimo: dict = {}
    for h in historico:
        ultimo[h.get("idProcesso")] = h
    apensados = [{"numero": _numero(x.get("numeroProcessoFormatado")), "id_processo": x.get("idProcesso"),
                  "paj_norm": _paj_norm_de(x.get("numeroProcessoFormatado")),
                  "motivo": x.get("motivo") or (ultimo.get(x.get("idProcesso")) or {}).get("motivo") or "",
                  # data e responsável do apensamento vêm do histórico
                  "data": ((ultimo.get(x.get("idProcesso")) or {}).get("data") or "")[:10],
                  "responsavel": (ultimo.get(x.get("idProcesso")) or {}).get("nomeResponsavel") or ""}
                 for x in ap.get("apensos") or []]
    apensado_a = [{"numero": _numero(h.get("numeroProcessoFormatado")), "id_processo": pid,
                   "paj_norm": _paj_norm_de(h.get("numeroProcessoFormatado")),
                   "motivo": h.get("motivo") or "", "data": (h.get("data") or "")[:10],
                   "responsavel": h.get("nomeResponsavel") or ""}
                  for pid, h in ultimo.items()
                  if pid not in ids_apensos and "apensad" in (h.get("nomeAcao") or "").lower()
                  and "desapens" not in (h.get("nomeAcao") or "").lower()]
    hist = [{"acao": h.get("nomeAcao") or "", "numero": _numero(h.get("numeroProcessoFormatado")),
             "data": (h.get("data") or "")[:10], "motivo": h.get("motivo") or "",
             "responsavel": h.get("nomeResponsavel") or ""} for h in reversed(historico)]

    vistos: dict = {}
    for item in [*outros.values(), *apensados, *apensado_a]:
        chave = item.get("paj_norm") or item.get("id_processo")
        if chave not in vistos:
            vistos[chave] = _detalhes(item.get("paj_norm"), item.get("id_processo"))
        item.update(vistos[chave])

    # ativos primeiro; dentro de cada grupo, do mais recente para o mais antigo
    ordem = sorted(outros.values(), key=lambda o: o["numero"], reverse=True)
    ordem.sort(key=lambda o: o["arquivado"])
    return {"ok": True, "outros": ordem, "apensados": apensados, "apensado_a": apensado_a,
            "historico_apensos": hist}


async def pajs_relacionados(paj_norm: str) -> dict:
    if not sis2.conectado():
        return {"ok": False, "erro": "Painel não conectado ao SIS-DPU 2.0."}
    try:
        return await asyncio.to_thread(_ler, paj_norm)
    except sis2.Sisdpu2TetoDiario:
        return {"ok": False, "erro": "Limite diário de leitura do SIS-DPU 2.0 atingido — volta amanhã."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"SIS-DPU 2.0: {e}"[:300]}
