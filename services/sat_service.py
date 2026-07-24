"""Serviço da feature "Arquivos SAT".

Orquestra, para um PAJ que tem na caixa de entrada o trâmite "Ao(À) Defensor(a)
para juntada dos documentos do SAT Central. Após, tramitar à DIVPREV.":

    (download dos PDFs do SAT — feito FORA, pela skill no Chrome) →
    movimentação (fase "Juntada de documento", anexos = PDFs como
    "Documentos do Assistido") → tramitação (01 - PREV_DIVPREV) →
    conclusão do trâmite originário.

O download NÃO é feito aqui (exige 2FA/CAPTCHA no Chrome do usuário). Este serviço
assume que os PDFs já estão em `PAJS_DIR/<paj_norm>/Arquivos SAT/` e executa as
etapas headless via `ingestao.sisdpu_movtram`, transmitindo o progresso (SSE) e
gravando o resultado em `historico_sat`.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import config
from ingestao import sisdpu_movtram as movtram
from services import historico_sat

# --- parâmetros de negócio (aprovados pelo usuário) ---
DESCRICAO_ALVO = "para juntada dos documentos do sat central"  # trecho p/ filtro (normalizado)
FASE_MOV = "Juntada de documento"
TIPO_ARQUIVO = "Documentos do Assistido"
MOV_DESCRICAO = "Juntada dos documentos obtidos junto ao SAT Central (INSS/Dataprev)"
MOV_DESCRICAO_INSTITUIDOR = ("Juntada dos documentos do INSTITUIDOR obtidos junto ao SAT "
                             "Central (INSS/Dataprev)")
MOV_DESCRICAO_COMPLEMENTAR = ("Juntada complementar de documentos obtidos junto ao SAT "
                              "Central (INSS/Dataprev)")
TRAM_DESCRICAO = "Documentos do SAT Central juntados ao PAJ"
GRUPO_PADRAO = "01 - PREV_DIVPREV"
SUBPASTA_SAT = "Arquivos SAT"
SUBPASTA_INSTITUIDOR = "Instituidor"  # dentro de "Arquivos SAT" (download/anexação à parte)
SUBPASTA_COMPLEMENTAR = "Complementar"  # download complementar (anexado por movimentação)
# Originais dos PDFs compilados por tipo (espelha sat_client.SUBPASTA_ORIGINAIS_COMPILADOS).
# Ficam fora da anexação; apagados só APÓS a conclusão da tramitação (ver processar_paj_stream).
SUBPASTA_ORIGINAIS_COMPILADOS = "Compilados (originais)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _num_to_norm(paj: str) -> str:
    """'2026/029-01651' -> 'PAJ-2026-029-01651'."""
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (paj or "")


def _norm_to_num(paj_norm: str) -> str:
    """'PAJ-2026-029-01651' -> '2026/029-01651'."""
    m = re.match(r"PAJ-(\d{4})-(\d{3})-(\d+)", paj_norm or "")
    return f"{m.group(1)}/{m.group(2)}-{m.group(3)}" if m else (paj_norm or "")


def _pasta_paj(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _ler_metadata(paj_norm: str) -> dict:
    p = _pasta_paj(paj_norm) / "metadata.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cpf_do_paj(paj_norm: str) -> str:
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    return (raw.get("cpf_assistido") or "").strip()


def _html_para_texto(h: str) -> str:
    """Converte o HTML da narrativa do SISDPU em texto limpo, preservando parágrafos."""
    if not h:
        return ""
    h = re.sub(r"(?i)<\s*br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</\s*(p|div|tr|li)\s*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)          # remove as tags restantes
    h = _html.unescape(h)
    h = h.replace("\xa0", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def narrativa_paj(paj_norm: str) -> dict:
    """Texto da narrativa do PAJ (a partir do HTML salvo em sisdpu_raw.narrativa)."""
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    texto = _html_para_texto(raw.get("narrativa") or "")
    return {"paj_norm": paj_norm, "texto": texto}


def pasta_arquivos_sat(paj_norm: str) -> Path:
    """Pasta destino dos PDFs do SAT para um PAJ."""
    return _pasta_paj(paj_norm) / SUBPASTA_SAT


def listar_arquivos_sat(paj_norm: str) -> list[dict]:
    """PDFs já baixados na subpasta 'Arquivos SAT' (ignora o _LOG e não-PDF)."""
    pasta = pasta_arquivos_sat(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def pasta_instituidor(paj_norm: str) -> Path:
    """Subpasta dos documentos do INSTITUIDOR (download e anexação SEPARADOS)."""
    return pasta_arquivos_sat(paj_norm) / SUBPASTA_INSTITUIDOR


def listar_arquivos_instituidor(paj_norm: str) -> list[dict]:
    """PDFs baixados do instituidor (subpasta 'Instituidor')."""
    pasta = pasta_instituidor(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


# marcador (normalizado) → key do tipo, para reconstruir o inventário de logs antigos
_MARCADOR_KEY = [
    ("cnis - dados cadastrais", "cnis_dados_cadastrais"),
    ("cnis - atividades", "cnis_atividades"),
    ("cnis - elos", "cnis_elos"),
    ("cnis - microfichas", "cnis_microfichas"),
    ("cnis - remuneracoes", "cnis_remuneracoes"),
    ("cnis - vinculos", "cnis_vinculos"),
    ("declaracao de beneficios", "declaracao_beneficios"),
    ("requerimentos do sibe", "requerimentos_sibe"),
    ("revisao de beneficio", "revisao_art29"),
    ("hisconsignados", "consignados"),
    ("hiscre", "hiscre"),
    ("laudo social", "laudo_social"),
    ("laudo medico", "laudo_medico"),
    ("procadm", "pat"),
    ("tarefa", "pat"),
    ("ccon", "carta_concessao"),
    ("crer", "crer"),
]


def _key_de_texto(txt: str) -> str:
    t = _norm(txt)
    for marc, key in _MARCADOR_KEY:
        if marc in t:
            return key
    return ""


def _sub_de_texto(txt: str, key: str) -> str:
    if key == "pat":
        m = re.search(r"prot\s+(\d{5,})", txt, re.I) or re.search(r"tarefa\s+(\d{5,})", txt, re.I)
        return m.group(1) if m else ""
    m = re.search(r"\d{3}\.\d{3}\.\d{3}-\d(?!\d)", txt)
    return m.group(0) if m else ""


def _status_de_pulado(txt: str) -> str:
    t = _norm(txt)
    if "relacionado" in t:
        return "nao_relacionado"
    if "nao consultado" in t:
        return "nao_consultado"
    if "antirob" in t or "sem resposta" in t:
        return "antirobo"
    if "indisponiv" in t or "inacessiv" in t:
        return "indisponivel"
    if "falha" in t:
        return "falha"
    if "sem dados" in t or "sem linhas" in t or "sem tarefas" in t or "nao retornou" in t:
        return "sem_dados"
    return "sem_dados"


def _inventario_de_log(d: dict) -> list[dict]:
    """Fallback: reconstrói o inventário a partir de salvos[]/pulados[] de logs antigos."""
    inv: list[dict] = []
    for nome in d.get("salvos", []) or []:
        key = _key_de_texto(nome)
        if key:
            inv.append({"key": key, "sub": _sub_de_texto(nome, key), "nome": nome, "status": "baixado"})
    for p in d.get("pulados", []) or []:
        key = _key_de_texto(p)
        if not key:
            continue
        inv.append({"key": key, "sub": _sub_de_texto(p, key), "nome": "", "status": _status_de_pulado(p)})
    return inv


_STATUS_FALHA = {"falha", "antirobo", "indisponivel"}


def _download_tem_falha(paj_norm: str) -> bool:
    """True se o último log de download registrou FALHA real (status de erro no inventário
    ou evento de nível 'erro'). Skips intencionais (ignorado/pulado/sem_dados/etc.) NÃO
    contam como falha."""
    p = pasta_arquivos_sat(paj_norm) / "_log_download.json"
    if not os.path.isfile(str(p)):
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if any(it.get("status") in _STATUS_FALHA for it in (d.get("inventario") or [])):
        return True
    return any(ev.get("level") == "erro" for ev in (d.get("eventos") or []))


def _pat_servico_map(d: dict) -> dict:
    """Mapa {protocolo: serviço} do PAT, reconstruído dos nomes/textos do log — para exibir
    o tipo de serviço no "Baixar mais" mesmo em logs sem o campo `rotulo`.
    Nomes de arquivo: 'PROCADM <Serviço> - ... PROT <prot>'; pulados: 'PAT <Serviço>
    (protocolo <prot>)'."""
    mapa: dict[str, str] = {}
    for nome in d.get("salvos", []) or []:
        m = re.search(r"PROCADM\s+(.+?)\s+-\s+.*?PROT\s+(\d{5,})", nome, re.I)
        if m:
            mapa.setdefault(m.group(2), m.group(1).strip())
    for p in d.get("pulados", []) or []:
        m = re.search(r"PAT\s+(.+?)\s+\(protocolo\s+(\d{5,})\)", p, re.I)
        if m:
            mapa.setdefault(m.group(2), m.group(1).strip())
    return mapa


def inventario_paj(paj_norm: str) -> dict:
    """Inventário dos documentos revelados no SAT (1º download) + marcas ja_baixado /
    ja_anexado. Usa `inventario` do log (novos) ou reconstrói de salvos/pulados (antigos)."""
    log_p = pasta_arquivos_sat(paj_norm) / "_log_download.json"
    cpf, inv, pat_serv = "", [], {}
    if os.path.isfile(str(log_p)):
        try:
            d = json.loads(log_p.read_text(encoding="utf-8"))
            cpf = d.get("cpf", "") or ""
            inv = d.get("inventario") or _inventario_de_log(d)
            pat_serv = _pat_servico_map(d)  # {protocolo: serviço} p/ rótulo do PAT
        except Exception:  # noqa: BLE001
            inv = []
    if not cpf:
        cpf = _cpf_do_paj(paj_norm)
    # já anexados: nomes em QUALQUER registro do histórico deste PAJ
    anexados: set[str] = set()
    for r in historico_sat.listar():
        if r.get("paj_norm") == paj_norm:
            for a in (r.get("arquivos") or []):
                anexados.add(a)
    # já baixados: arquivos presentes nas pastas (principal + instituidor + complementar)
    baixados: set[str] = set()
    for lst in (listar_arquivos_sat(paj_norm), listar_arquivos_instituidor(paj_norm),
                listar_arquivos_complementar(paj_norm)):
        for a in lst:
            baixados.add(a["nome"])
    # dedup por (key, sub) — a paginação do PAT pode revelar o mesmo protocolo 2x; itens
    # duplicados quebravam o x-for do "Baixar mais" (chave repetida). Preferência de status:
    # baixado > qualquer outro (mantém o registro mais informativo).
    _PRIO = {"baixado": 3}
    dedup: dict[tuple, dict] = {}
    for it in inv:
        chave = (it.get("key", ""), it.get("sub", ""))
        atual = dedup.get(chave)
        if atual is None:
            dedup[chave] = dict(it)
            continue
        # preserva o rótulo/serviço se o vencedor atual não tiver
        if not atual.get("rotulo") and it.get("rotulo"):
            atual["rotulo"] = it["rotulo"]
        # troca o vencedor SÓ quando o novo tem status de prioridade MAIOR (baixado manda)
        if _PRIO.get(it.get("status", ""), 0) > _PRIO.get(atual.get("status", ""), 0):
            novo = dict(it)
            if not novo.get("rotulo") and atual.get("rotulo"):
                novo["rotulo"] = atual["rotulo"]
            dedup[chave] = novo
    itens = []
    for it in dedup.values():
        nome = it.get("nome", "")
        key, sub = it.get("key", ""), it.get("sub", "")
        servico = it.get("rotulo", "") or (pat_serv.get(sub, "") if key == "pat" else "")
        itens.append({
            "key": key, "sub": sub, "nome": nome, "servico": servico,
            "status": it.get("status", ""),
            "ja_baixado": bool(it.get("status") == "baixado" and (not nome or nome in baixados)),
            "ja_anexado": bool(nome and nome in anexados),
        })
    return {"paj_norm": paj_norm, "cpf": cpf, "itens": itens}


def pasta_complementar(paj_norm: str) -> Path:
    """Subpasta do download COMPLEMENTAR (baixado depois, anexado por movimentação)."""
    return pasta_arquivos_sat(paj_norm) / SUBPASTA_COMPLEMENTAR


def listar_arquivos_complementar(paj_norm: str) -> list[dict]:
    """PDFs na subpasta 'Complementar'."""
    pasta = pasta_complementar(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def pasta_excluidos(paj_norm: str) -> Path:
    """Subpasta-lixeira dos documentos removidos da lista (soft delete)."""
    return pasta_arquivos_sat(paj_norm) / "Excluídos"


def listar_todos_arquivos_sat(paj_norm: str) -> list[dict]:
    """Ativos (pasta principal, serão anexados) + excluídos (subpasta Excluídos),
    cada um com flag `excluido`. Usado pela UI (accordion)."""
    out = listar_arquivos_sat(paj_norm)  # só a pasta principal (não desce em subpastas)
    for a in out:
        a["excluido"] = False
    exc = pasta_excluidos(paj_norm)
    if exc.exists():
        for f in sorted(exc.glob("*.pdf")):
            try:
                tam = f.stat().st_size
            except Exception:  # noqa: BLE001
                tam = 0
            out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf",
                        "tamanho": tam, "excluido": True})
    return out


def _mover_pdf(origem: Path, destino_dir: Path) -> None:
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / origem.name
    if destino.exists():
        destino.unlink()
    shutil.move(str(origem), str(destino))


def excluir_arquivo_sat(paj_norm: str, nome: str) -> dict:
    """SOFT DELETE: move o documento da pasta principal para 'Excluídos' (não some do
    disco; sai da lista de anexação e pode ser recuperado). Anti path-traversal."""
    pasta = pasta_arquivos_sat(paj_norm).resolve()
    alvo = (pasta / Path(nome or "").name).resolve()
    if pasta not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado.", "n_arquivos": len(listar_arquivos_sat(paj_norm))}
    try:
        _mover_pdf(alvo, pasta_excluidos(paj_norm))
        return {"ok": True, "detalhe": f"Removido da lista: {alvo.name}",
                "n_arquivos": len(listar_arquivos_sat(paj_norm))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), "n_arquivos": len(listar_arquivos_sat(paj_norm))}


def recuperar_arquivo_sat(paj_norm: str, nome: str) -> dict:
    """Traz o documento de volta de 'Excluídos' para a pasta principal (será anexado)."""
    exc = pasta_excluidos(paj_norm).resolve()
    alvo = (exc / Path(nome or "").name).resolve()
    if exc not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado em Excluídos.",
                "n_arquivos": len(listar_arquivos_sat(paj_norm))}
    try:
        _mover_pdf(alvo, pasta_arquivos_sat(paj_norm))
        return {"ok": True, "detalhe": f"Recuperado: {alvo.name}",
                "n_arquivos": len(listar_arquivos_sat(paj_norm))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), "n_arquivos": len(listar_arquivos_sat(paj_norm))}


# ---------------------------------------------------------------------------
# listagem de pendências (para a tabela)
# ---------------------------------------------------------------------------
async def listar_pendentes() -> dict:
    """Lê a caixa de entrada do usuário e devolve os PAJs cujo trâmite tem a descrição
    de juntada dos documentos do SAT Central. Cruza CPF/nome do metadata e conta os
    PDFs já baixados. Retorna {ok, itens, detalhe}."""
    caixa = await movtram.listar_caixa_estruturada()
    if not caixa.get("ok"):
        return {"ok": False, "itens": [], "detalhe": caixa.get("detalhe", "Falha ao ler a caixa.")}
    alvo = DESCRICAO_ALVO
    itens = []
    for it in caixa.get("itens", []):
        texto = _norm(it.get("descricao", "") + " | " + it.get("linha_texto", ""))
        if alvo not in texto:
            continue
        paj = it.get("paj", "")
        paj_norm = _num_to_norm(paj)
        meta = _ler_metadata(paj_norm)
        arquivos = listar_arquivos_sat(paj_norm)
        ja = historico_sat.por_id_tramite(it.get("id_tramite", ""))
        item = {
            "paj": paj,
            "paj_norm": paj_norm,
            "id_processo": it.get("id", ""),
            "id_tramite": it.get("id_tramite", ""),
            "assistido": it.get("assistido", "") or meta.get("assistido_caixa", ""),
            "cpf": _cpf_do_paj(paj_norm),
            "pretensao": meta.get("pretensaopura") or meta.get("pretensao", ""),
            "descricao": it.get("descricao", ""),
            "url_detalhe": it.get("url_detalhe", ""),
            "n_arquivos": len(arquivos),
            "n_instituidor": len(listar_arquivos_instituidor(paj_norm)),
            "tem_pasta": _pasta_paj(paj_norm).exists(),
            "sincronizado": bool(meta),
            "tem_log": (pasta_arquivos_sat(paj_norm) / "_log_download.json").exists(),
            "tem_falha": _download_tem_falha(paj_norm),
            "ja_processado": bool(ja and ja.get("status") == "concluido"),
        }
        # FALLBACK: PAJ ainda NÃO sincronizado (sem metadata) → sem CPF/pretensão local.
        # Busca direto no detalhamento do PAJ, na mesma sessão headless.
        if not item["cpf"] and item["url_detalhe"]:
            det = await movtram.detalhes_paj(item["url_detalhe"])
            if det.get("cpf"):
                item["cpf"] = det["cpf"].strip()
            if not item["pretensao"] and det.get("pretensao"):
                try:
                    from ingestao import parser as _parser
                    item["pretensao"] = (_parser.extrair_pretensao_pura(det["pretensao"])
                                         or det["pretensao"]).strip()
                except Exception:  # noqa: BLE001
                    item["pretensao"] = det["pretensao"].strip()
            if not item["assistido"] and det.get("assistido"):
                item["assistido"] = det["assistido"].strip()
            if det.get("narrativa"):
                item["narrativa"] = _html_para_texto(det["narrativa"])
        itens.append(item)
    return {"ok": True, "itens": itens, "detalhe": f"{len(itens)} pendência(s)."}


# ---------------------------------------------------------------------------
# processamento (SSE) — movimentar → tramitar → concluir
# ---------------------------------------------------------------------------
def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _passo_ev(etapa: str, ok: bool | None, detalhe: str) -> dict:
    icone = "✓" if ok else ("✗" if ok is False else "•")
    level = "ok" if ok else ("erro" if ok is False else "muted")
    return {"level": level, "msg": f"     {icone} {etapa}" + (f": {detalhe}" if detalhe else "")}


async def processar_paj_stream(
    paj_norm: str,
    grupo: str | None = None,
    prazo_dias: int = 0,
    dry_run: bool = False,
) -> AsyncIterator[dict]:
    """Gera eventos SSE do processamento de UM PAJ. Assume os PDFs já baixados em
    'Arquivos SAT'. `prazo_dias` > 0 define prazo de conclusão da tramitação (hoje +
    N dias); 0 = sem prazo. Termina com {'done': True, 'resultado': {...}}."""
    grupo = grupo or GRUPO_PADRAO
    prazo_data = None
    if prazo_dias and int(prazo_dias) > 0:
        prazo_data = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")
    log: list[dict] = []

    def passo(etapa: str, ok: bool | None, detalhe: str) -> dict:
        log.append({"ts": datetime.now().isoformat(timespec="seconds"),
                    "etapa": etapa, "ok": (None if ok is None else bool(ok)),
                    "detalhe": str(detalhe or "")[:600]})
        return _passo_ev(etapa, ok, detalhe)

    num = _norm_to_num(paj_norm)
    prefixo = "[dry-run] " if dry_run else ""
    yield _ev("novo", f"{prefixo}Processando {num} (grupo destino: {grupo})")

    # 1) localizar o trâmite na caixa (ids frescos)
    yield _ev("info", "Localizando o trâmite na caixa de entrada...")
    caixa = await movtram.listar_caixa_estruturada()
    if not caixa.get("ok"):
        yield passo("Leitura da caixa", False, caixa.get("detalhe", ""))
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": caixa.get("detalhe", "")}}
        return
    item = None
    for it in caixa.get("itens", []):
        if _num_to_norm(it.get("paj", "")) == paj_norm and \
           DESCRICAO_ALVO in _norm(it.get("descricao", "") + " | " + it.get("linha_texto", "")):
            item = it
            break
    if not item:
        d = f"Trâmite de juntada do SAT não encontrado na caixa para {num}."
        yield passo("Localização do trâmite", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    id_proc = item.get("id", "")
    id_tramite = item.get("id_tramite", "")
    yield passo("Localização do trâmite", True, f"id_processo={id_proc} · id_tramite={id_tramite}")

    # 2) anexos (PDFs do SAT já baixados)
    anexos = listar_arquivos_sat(paj_norm)
    if not anexos:
        d = (f"Nenhum PDF em '{SUBPASTA_SAT}' para {num}. Baixe os documentos do SAT antes "
             f"de processar (pasta: {pasta_arquivos_sat(paj_norm)}).")
        yield passo("Documentos do SAT", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    yield passo("Documentos do SAT", True, f"{len(anexos)} arquivo(s) para juntar")

    pajs = [{"id": id_proc, "numero": num}]
    resultado: dict[str, Any] = {"paj": num, "paj_norm": paj_norm, "grupo": grupo}

    # 3) MOVIMENTAR
    yield _ev("info", "Movimentando (juntada de documento)...")
    mv = await movtram.movimentar_paj(pajs, MOV_DESCRICAO, FASE_MOV, anexos, TIPO_ARQUIVO, dry_run)
    yield passo("Movimentação do PAJ (Juntada de documento)", mv.get("ok"), mv.get("detalhe", ""))
    resultado["mov_status"] = mv.get("status")
    resultado["mov_seq"] = mv.get("mov_seq")
    resultado["mov_detalhe"] = mv.get("detalhe", "")
    if not mv.get("ok"):
        _gravar_hist(paj_norm, item, anexos, grupo, resultado, "falha", "Falha na movimentação.", log)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
        return

    # 4) TRAMITAR
    prazo_txt = f"prazo até {prazo_data}" if prazo_data else "sem prazo"
    yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})...")
    tr = await movtram.tramitar_paj(pajs, grupo=grupo, descricao=TRAM_DESCRICAO,
                                    prazo_data=prazo_data, dry_run=dry_run)
    yield passo(f"Tramitação para {grupo} ({prazo_txt})", tr.get("ok"), tr.get("detalhe", ""))
    resultado["tram_status"] = tr.get("status")
    resultado["tram_caixa"] = grupo
    resultado["tram_detalhe"] = tr.get("detalhe", "")
    if not tr.get("ok"):
        _gravar_hist(paj_norm, item, anexos, grupo, resultado, "parcial", "Movimentou, mas falhou a tramitação.", log)
        yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
        return

    # 5) CONCLUIR o trâmite originário
    yield _ev("info", "Concluindo o trâmite originário...")
    cc = await movtram.concluir_paj([id_tramite], dry_run)
    yield passo("Conclusão do trâmite originário", cc.get("ok"), cc.get("detalhe", ""))
    resultado["concluido_status"] = cc.get("status")
    resultado["concluido_detalhe"] = cc.get("detalhe", "")

    status = "dry_run" if dry_run else ("concluido" if cc.get("ok") else "parcial")
    detalhe = ("[dry-run] Simulação concluída — nada foi efetivado." if dry_run
               else ("Fluxo concluído: movimentado, tramitado e trâmite concluído."
                     if cc.get("ok") else "Movimentou e tramitou, mas a conclusão falhou."))
    # Compilação: apagar os PDFs originais SÓ APÓS a anexação/conclusão (o compilado já foi
    # anexado na movimentação; os originais ficaram numa subpasta não-anexada até aqui).
    if cc.get("ok") and not dry_run:
        orig = pasta_arquivos_sat(paj_norm) / SUBPASTA_ORIGINAIS_COMPILADOS
        if orig.is_dir():
            n = len(list(orig.glob("*.pdf")))
            try:
                shutil.rmtree(orig, ignore_errors=True)
                yield passo("Limpeza dos originais compilados", True,
                            f"{n} PDF(s) originais removidos após a anexação do(s) compilado(s).")
            except Exception as e:  # noqa: BLE001
                yield passo("Limpeza dos originais compilados", None, str(e))
    _gravar_hist(paj_norm, item, anexos, grupo, resultado, status, detalhe, log)
    yield {"done": True, "resultado": {"ok": bool(dry_run or cc.get("ok")), "status": status, "detalhe": detalhe}}


async def _mov_extra_stream(
    paj_norm: str, id_processo: str, assistido: str, anexos: list[dict],
    descricao: str, escopo: str, titulo: str,
) -> AsyncIterator[dict]:
    """Anexa (SÓ movimentação/juntada) um conjunto de documentos ao PAJ, acessando-o pelo
    id/número (independe da caixa). NÃO tramita nem conclui. Grava registro SEPARADO no
    histórico com `escopo` (instituidor|complementar). Retorna também 'salvos' no done."""
    num = _norm_to_num(paj_norm)
    log: list[dict] = []

    def passo(etapa: str, ok: bool | None, detalhe: str) -> dict:
        log.append({"ts": datetime.now().isoformat(timespec="seconds"),
                    "etapa": etapa, "ok": (None if ok is None else bool(ok)),
                    "detalhe": str(detalhe or "")[:600]})
        return _passo_ev(etapa, ok, detalhe)

    yield _ev("novo", f"{titulo} — {num} (movimentação/juntada)")
    if not anexos:
        d = "Nenhum documento para juntar."
        yield passo("Documentos", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    if not id_processo:
        d = "id do PAJ ausente — não foi possível abrir a movimentação."
        yield passo("Localização do PAJ", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    yield passo("Documentos", True, f"{len(anexos)} arquivo(s) para juntar")

    pajs = [{"id": id_processo, "numero": num}]
    yield _ev("info", "Movimentando (juntada de documento)...")
    mv = await movtram.movimentar_paj(pajs, descricao, FASE_MOV, anexos, TIPO_ARQUIVO)
    yield passo("Movimentação (Juntada de documento)", mv.get("ok"), mv.get("detalhe", ""))
    ok = bool(mv.get("ok"))
    status = "concluido" if ok else "falha"
    try:
        historico_sat.registrar({
            "paj": num, "paj_norm": paj_norm, "id_processo": id_processo, "id_tramite": "",
            "assistido": assistido or _ler_metadata(paj_norm).get("assistido_caixa", ""),
            "cpf": "", "escopo": escopo,
            "n_arquivos": len(anexos), "arquivos": [a["nome"] for a in anexos],
            "mov_status": mv.get("status"), "mov_seq": mv.get("mov_seq"),
            "mov_detalhe": mv.get("detalhe", ""),
            "tram_status": "ja_foi", "tram_caixa": "", "tram_detalhe": "",
            "concluido_status": "ja_foi", "concluido_detalhe": "",
            "status": status,
            "detalhe": (f"{titulo}: juntada (movimentação) concluída." if ok
                        else f"{titulo}: falha na movimentação."),
            "log": log,
        })
    except Exception:  # noqa: BLE001
        pass
    yield {"done": True, "resultado": {"ok": ok, "status": status,
           "detalhe": mv.get("detalhe", ""), "escopo": escopo}}


async def processar_instituidor_stream(
    paj_norm: str, id_processo: str, assistido: str = "",
) -> AsyncIterator[dict]:
    """Anexa (movimentação-only) os documentos do INSTITUIDOR (subpasta 'Instituidor')."""
    async for ev in _mov_extra_stream(
            paj_norm, id_processo, assistido, listar_arquivos_instituidor(paj_norm),
            MOV_DESCRICAO_INSTITUIDOR, "instituidor", "Documentos do INSTITUIDOR"):
        yield ev


async def processar_complementar_stream(
    paj_norm: str, id_processo: str, assistido: str = "",
) -> AsyncIterator[dict]:
    """Anexa (movimentação-only) os documentos do DOWNLOAD COMPLEMENTAR (subpasta
    'Complementar'); ao final, move os PDFs para a pasta principal (preserva o acervo)."""
    anexos = listar_arquivos_complementar(paj_norm)
    resultado_ok = False
    async for ev in _mov_extra_stream(
            paj_norm, id_processo, assistido, anexos,
            MOV_DESCRICAO_COMPLEMENTAR, "complementar", "Download complementar"):
        if ev.get("done"):
            resultado_ok = bool(ev.get("resultado", {}).get("ok"))
        yield ev
    # move os PDFs complementares para a pasta principal (só se a movimentação deu certo)
    if resultado_ok:
        destino = pasta_arquivos_sat(paj_norm)
        for a in anexos:
            try:
                shutil.move(a["caminho"], str(destino / Path(a["caminho"]).name))
            except Exception:  # noqa: BLE001
                pass


def mesclar_complementar_pendente(paj_norm: str) -> dict:
    """Download complementar em PAJ AINDA NÃO ANEXADO: move os PDFs da subpasta
    'Complementar' para a pasta principal (juntam-se ao acervo pendente, para entrarem na
    futura movimentação) e MESCLA o log/inventário no _log_download.json principal —
    upsert do inventário por (key, sub), salvos/pulados somados e eventos anexados com um
    marcador (log complementar visível no mesmo visualizador). SEM movimentação. Retorna
    {ok, movidos, n_arquivos}."""
    comp = pasta_complementar(paj_norm)
    principal = pasta_arquivos_sat(paj_norm)
    principal.mkdir(parents=True, exist_ok=True)
    movidos: list[str] = []
    if comp.is_dir():
        for f in sorted(comp.glob("*.pdf")):
            try:
                shutil.move(str(f), str(principal / f.name))
                movidos.append(f.name)
            except Exception:  # noqa: BLE001
                pass

    def _ler(p):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if os.path.isfile(str(p)) else {}
        except Exception:  # noqa: BLE001
            return {}

    main = _ler(principal / "_log_download.json")
    cdata = _ler(comp / "_log_download.json")
    if cdata:
        salvos = list(main.get("salvos", []) or [])
        for s in cdata.get("salvos", []) or []:
            if s not in salvos:
                salvos.append(s)
        main["salvos"] = salvos
        inv = list(main.get("inventario", []) or [])
        idx = {(it.get("key"), it.get("sub")): i for i, it in enumerate(inv)}
        for it in cdata.get("inventario", []) or []:
            chave = (it.get("key"), it.get("sub"))
            if chave in idx:
                inv[idx[chave]] = it   # comp é mais novo → prevalece
            else:
                inv.append(it); idx[chave] = len(inv) - 1
        main["inventario"] = inv
        main["pulados"] = list(main.get("pulados", []) or []) + list(cdata.get("pulados", []) or [])
        ev = list(main.get("eventos", []) or [])
        ev.append({"level": "novo",
                   "msg": f"── Download complementar (pendência) — "
                          f"{datetime.now():%d-%m-%Y %H:%M} ──"})
        ev.extend(cdata.get("eventos", []) or [])
        main["eventos"] = ev
        try:
            (principal / "_log_download.json").write_text(
                json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    with __import__("contextlib").suppress(Exception):
        shutil.rmtree(comp, ignore_errors=True)
    return {"ok": True, "movidos": movidos, "n_arquivos": len(listar_arquivos_sat(paj_norm))}


def _gravar_hist(paj_norm, item, anexos, grupo, resultado, status, detalhe, log) -> None:
    if resultado.get("_dry"):  # nunca grava dry-run
        return
    if status == "dry_run":
        return
    historico_sat.registrar({
        "paj": _norm_to_num(paj_norm),
        "paj_norm": paj_norm,
        "id_processo": item.get("id", ""),
        "id_tramite": item.get("id_tramite", ""),
        "assistido": item.get("assistido", ""),
        "cpf": _cpf_do_paj(paj_norm),
        "n_arquivos": len(anexos),
        "arquivos": [a["nome"] for a in anexos],
        "mov_status": resultado.get("mov_status"),
        "mov_seq": resultado.get("mov_seq"),
        "mov_detalhe": resultado.get("mov_detalhe", ""),
        "tram_status": resultado.get("tram_status"),
        "tram_caixa": grupo,
        "tram_detalhe": resultado.get("tram_detalhe", ""),
        "concluido_status": resultado.get("concluido_status"),
        "concluido_detalhe": resultado.get("concluido_detalhe", ""),
        "status": status,
        "detalhe": detalhe,
        "log": log,
    })
