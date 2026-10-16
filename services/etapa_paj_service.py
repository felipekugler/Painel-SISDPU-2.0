"""Etapa do PAJ — da abertura ao ajuizamento (pedido do Defensor, 15/10/2026).

Dez etapas, em parte MANUAIS (escolhidas pelo Defensor e registradas no
SISDPU como movimentação "Etapa: …" na fase FASE_ETAPA — rastreável no próprio
PAJ) e em parte AUTOMÁTICAS (deduzidas pelo painel a partir do que as rotinas
deixam na pasta do PAJ e nas movimentações; não gravam nada no SISDPU).

Regra de cálculo:
  - PAJ arquivado (movimentação "Arquivado. …" mais recente que um
    desarquivamento) encerra o fluxo: a barra de etapa some (a de arquivado
    já aparece).
  - Base = a movimentação "Etapa: …" mais recente; sem nenhuma, "Aguardando
    análise".
  - Evidências automáticas MAIS RECENTES que a base e MAIS ADIANTE no trilho
    fazem a etapa avançar (nunca recuar — uma minuta antiga não passa por cima
    de uma decisão nova).
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR

FASE_ETAPA = "Aguardando tramitação judicial/Administrativa"
PREFIXO = "Etapa:"
MARCADOR_MINUTA = "etapa_minuta_solicitada.json"

# chave, rótulo (exato, como o Defensor especificou), ordem no trilho, manual?,
# cor de fundo, cor do texto, ícone (Tabler) ou "spinner"
ETAPAS = [
    ("aguardando_analise", "Aguardando análise", 0, False, "#D3D1C7", "#2C2C2A", "inbox"),
    # Automática e passageira: análise por IA rodando num PAJ que ainda estava
    # "Aguardando análise" (16/10/2026). Mesma cor, com spinner. Terminada a
    # análise, volta a "Aguardando análise" até o Defensor marcar a etapa.
    ("em_analise", "Em análise", 0, False, "#D3D1C7", "#2C2C2A", "spinner"),
    ("inviavel", "Analisado – Inviável", 1, True, "#F7C1C1", "#791F1F", "ban"),
    ("diligencia", "Analisado – Em diligência", 1, True, "#FDF0B8", "#633806", "hourglass"),
    ("viavel", "Analisado – Viável", 2, True, "#C0DD97", "#173404", "circle-check"),
    ("elaborando_minuta", "Elaborando minuta de inicial", 3, False, "#B5D4F4", "#042C53", "spinner"),
    ("inicial_minutada", "Inicial minutada", 4, False, "#B5D4F4", "#042C53", "file-text"),
    ("organizando_pasta", "Organizando pasta para ajuizamento", 5, False, "#CECBF6", "#26215C", "spinner"),
    ("pasta_pronta", "Pasta de ajuizamento pronta", 6, False, "#7F77DD", "#FFFFFF", "folder-check"),
    ("ajuizado", "Ajuizado", 7, True, "#378ADD", "#FFFFFF", "gavel"),
    ("cumprimento_sentenca", "Cumprimento de sentença", 8, True, "rgb(238, 173, 45)", "#412402", "coins"),
]
_POR_CHAVE = {e[0]: e for e in ETAPAS}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[‒-―\-–—]+", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_POR_ROTULO = {_norm(e[1]): e[0] for e in ETAPAS}


def info(chave: str) -> dict:
    e = _POR_CHAVE.get(chave) or _POR_CHAVE["aguardando_analise"]
    return {"chave": e[0], "rotulo": e[1], "ordem": e[2], "manual": e[3],
            "cor_fundo": e[4], "cor_texto": e[5], "icone": e[6]}


def opcoes_manuais() -> list[dict]:
    return [{"chave": e[0], "rotulo": e[1]} for e in ETAPAS if e[3]]


def opcoes_todas() -> list[dict]:
    # "Em análise" é só um estado passageiro (análise rodando) — não se escolhe
    return [{"chave": e[0], "rotulo": e[1]} for e in ETAPAS if e[0] != "em_analise"]


def _analise_em_andamento(paj_norm: str) -> dict | None:
    """Sessão de análise por IA rodando agora neste PAJ (a mesma fonte da
    página "Análises e iniciais"); None se não houver."""
    try:
        from services.analise_pretensao_service import get_status_todos
        return get_status_todos().get(paj_norm)
    except Exception:  # noqa: BLE001
        return None


def descricao_movimentacao(chave: str) -> str:
    return f"{PREFIXO} {info(chave)['rotulo']}"


# --- leitura das evidências --------------------------------------------------

def _dt_mov(mv: dict) -> datetime | None:
    for campo, fmt in (("data_original", "%d/%m/%Y %H:%M:%S"), ("data_original", "%d/%m/%Y %H:%M"),
                       ("data", "%Y-%m-%d"), ("data", "%d/%m/%Y")):
        v = (mv.get(campo) or "").strip()
        if v:
            try:
                return datetime.strptime(v[:len(datetime.now().strftime(fmt))], fmt)
            except ValueError:
                continue
    return None


def _texto(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _fases(mv: dict) -> list[str]:
    bruto = mv.get("fases") or mv.get("movimentacao") or ""
    return [re.sub(r"^[^\wÀ-ú]+", "", f).strip() for f in bruto.split(";") if f.strip()]


def _dt_iso(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _arquivo_minuta(pasta: Path) -> Path | None:
    """A petição inicial mais recente em "Peças IA" (ou na raiz, compatibilidade)."""
    achados = []
    for p in (pasta / "Peças IA", pasta):
        if p.is_dir():
            for f in p.iterdir():
                nome = unicodedata.normalize("NFD", f.stem).encode("ascii", "ignore").decode().lower()
                if f.is_file() and f.suffix.lower() == ".docx" and "peticao" in nome:
                    achados.append(f)
    return max(achados, key=lambda f: f.stat().st_mtime) if achados else None


def calcular(paj_norm: str, metadata: dict | None = None) -> dict:
    """Etapa atual do PAJ: info() + {desde, mov_seq, detalhe, arquivado}."""
    pasta = PAJS_DIR / paj_norm
    if metadata is None:
        try:
            metadata = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            metadata = {}
    movs = (metadata.get("detalhes_sisdpu") or {}).get("movimentacoes") or []
    com_data = [(mv, _dt_mov(mv)) for mv in movs]
    com_data = [(mv, d) for mv, d in com_data if d is not None]
    com_data.sort(key=lambda x: (x[1], int(str(x[0].get("seq") or 0) or 0)), reverse=True)

    # 1) arquivado encerra o fluxo
    for mv, d in com_data:
        fs = [_norm(f) for f in _fases(mv)]
        if any(f.startswith("paj desarquivado") for f in fs):
            break
        if any(f.startswith("arquivado") for f in fs):
            return {**info("aguardando_analise"), "arquivado": True, "desde": d.isoformat(timespec="seconds"),
                    "mov_seq": mv.get("seq"), "detalhe": ""}

    # 2) base: a movimentação "Etapa: …" mais recente
    base_chave, base_dt, base_seq = "aguardando_analise", None, None
    for mv, d in com_data:
        t = _texto(mv.get("descricao") or "")
        if t.lower().startswith(PREFIXO.lower()):
            chave = _POR_ROTULO.get(_norm(t[len(PREFIXO):]))
            if chave:
                base_chave, base_dt, base_seq = chave, d, mv.get("seq")
                break
    if base_dt is None and com_data:
        base_dt = com_data[-1][1]  # aguardando análise desde a abertura

    # 3) evidências automáticas
    evid: list[tuple[str, datetime, str]] = []
    m = _dt_iso((_ler_json(pasta / MARCADOR_MINUTA) or {}).get("em"))
    if m:
        evid.append(("elaborando_minuta", m, ""))
    f = _arquivo_minuta(pasta)
    if f:
        evid.append(("inicial_minutada", datetime.fromtimestamp(f.stat().st_mtime), f.name))
    try:
        from services import organizar_ajuizamento_service as org
        st = org.get_status(paj_norm)
        disco = org.ler_disco(paj_norm) or {}
        quando = _dt_iso(disco.get("atualizado_em")) or datetime.now()
        if st.get("status") == "running":
            evid.append(("organizando_pasta", datetime.now(), ""))
        elif st.get("status") == "done" and org.pasta_ajuizamento_existe(paj_norm):
            evid.append(("pasta_pronta", quando, ""))
    except Exception:  # noqa: BLE001
        pass
    for a in _ler_json(pasta / "peticao_inicial_ajuizada.json") or []:
        d = _dt_iso((a or {}).get("ajuizado_em"))
        if d:
            evid.append(("ajuizado", d, (a or {}).get("numero") or ""))
    for mv, d in com_data:
        if any(_norm(x).startswith("peticao inicial") for x in _fases(mv)):
            num = re.search(r"\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}", _texto(mv.get("descricao") or ""))
            evid.append(("ajuizado", d, num.group(0) if num else ""))
            break

    # 4) avança só para a frente e só com evidência mais recente que a base
    atual = (base_chave, base_dt, base_seq, "")
    ordem_base = info(base_chave)["ordem"]
    melhor = None
    for chave, d, det in evid:
        if (base_dt is None or d >= base_dt) and info(chave)["ordem"] > ordem_base:
            if melhor is None or info(chave)["ordem"] > info(melhor[0])["ordem"]:
                melhor = (chave, d, None, det)
    if melhor:
        atual = melhor
    chave, d, seq, det = atual
    # Só "Aguardando análise" vira "Em análise": numa etapa à frente, uma nova
    # análise/ajuste pela IA não faz o PAJ parecer que voltou atrás.
    if chave == "aguardando_analise":
        rodando = _analise_em_andamento(paj_norm)
        if rodando:
            return {**info("em_analise"), "arquivado": False, "desde": rodando.get("iniciado_em") or "",
                    "mov_seq": None, "detalhe": ""}
    return {**info(chave), "arquivado": False, "desde": d.isoformat(timespec="seconds") if d else "",
            "mov_seq": seq, "detalhe": det}


def _ler_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def marcar_minuta_solicitada(paj_norm: str) -> dict:
    pasta = PAJS_DIR / paj_norm
    if not pasta.is_dir():
        return {"ok": False, "erro": "PAJ não encontrado."}
    (pasta / MARCADOR_MINUTA).write_text(
        json.dumps({"em": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    return {"ok": True}


async def registrar(paj_norm: str, chave: str) -> dict:
    """Grava a movimentação "Etapa: …" no SIS-DPU 2.0 (ficha no modal de
    confirmação) e relê o PAJ. Etapas automáticas só são aceitas aqui pelo
    desarquivamento (que passa pelo módulo de ações, não por esta função)."""
    if chave not in _POR_CHAVE or not info(chave)["manual"]:
        return {"ok": False, "erro": "Etapa inválida para escolha manual."}
    from ingestao import sisdpu2_movtram
    if not sisdpu2_movtram.ativo():
        return {"ok": False, "erro": "Fonte SIS-DPU 2.0 (MCP) não está ativa/conectada."}
    meta = _ler_json(PAJS_DIR / paj_norm / "metadata.json") or {}
    paj = meta.get("paj") or re.sub(r"^PAJ-(\d{4})-(\d{3})-(\d+)$", r"\1/\2-\3", paj_norm)
    r = await sisdpu2_movtram.movimentar_paj([{"numero": paj}], descricao_movimentacao(chave), FASE_ETAPA,
                                             tipo_ficha="etapa")
    if not r.get("ok"):
        return {"ok": False, "erro": r.get("detalhe") or "Falha ao registrar a etapa."}
    try:
        from services import ao_vivo_service
        await ao_vivo_service.atualizar_paj(paj_norm, forcar=True)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "etapa": calcular(paj_norm)}
