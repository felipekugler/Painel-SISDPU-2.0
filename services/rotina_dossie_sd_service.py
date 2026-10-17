"""Rotina automática: Juntada de Dossiê Seguro-Desemprego Web
(Configurações › Rotinas automáticas — pedido do Defensor, 16/10/2026).

PAJ NOVO na caixa com trâmite "Abertura de PAJ" e pretensão HABILITADA (lista
em Configurações; pretensão fora da lista não passa pela rotina) → gera o Dossiê Completo do Seguro-Desemprego Web
(services/dossie_sd_service, o mesmo do botão) e junta ao PAJ ("Juntada de
documento", "Dossiê do Seguro Desemprego Web").

Pré-aprovação da juntada (autorizada pelo Defensor): só quando a ficha bater
EXATAMENTE com o modelo (PAJ, fase, descrição e o PDF gerado agora) — ver
aprovacao_mcp_service.pre_aprovar. Antes de juntar, confere que o CPF do PDF é
o do assistido do PAJ. Qualquer divergência → não junta (erro no histórico) ou,
na ficha, cai no modal normal.

"Novo" = trâmite "Abertura de PAJ" enviado depois da ativação da rotina; os que
já estavam na caixa quando ela foi ligada ficam registrados como anteriores.
Um PAJ por vez (o site do SD aceita uma coleta só). Histórico em
dados/_rotina_dossie_sd_historico.jsonl.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR

log = logging.getLogger("rotina_dossie_sd")

_DADOS = Path(__file__).resolve().parents[1].parent / "dados"
_ARQ_ESTADO = _DADOS / "_rotina_dossie_sd.json"
_ARQ_HIST = _DADOS / "_rotina_dossie_sd_historico.jsonl"
INTERVALO_SEG = 300
TEMPO_MAX_DOSSIE = 900
TEMPO_MAX_JUNTADA = 900

PADRAO = {"ativa": False, "pre_aprovar": True, "incluir_pretensoes": [], "ativada_em": None,
          "intervalo_min": INTERVALO_SEG // 60, "apenas_dias_uteis": True}
_execucao: dict = {"rodando": False, "paj": "", "etapa": "", "ultimo_ciclo": None, "aguardando": ""}
_gatilho = asyncio.Event()
_forcar = {"proximo": False}  # "Executar agora": roda mesmo em fim de semana


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _ler_json(p: Path, padrao):
    with contextlib.suppress(Exception):
        return json.loads(p.read_text(encoding="utf-8"))
    return padrao


def _gravar_json(p: Path, dados) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- configuração

def get_config() -> dict:
    from services import config_painel_service
    raw = config_painel_service._ler_raw().get("rotina_dossie_sd") or {}
    return {**PADRAO, **raw}


def set_config(novo: dict) -> dict:
    from services import config_painel_service
    atual = get_config()
    cfg = dict(atual)
    if "ativa" in novo:
        cfg["ativa"] = bool(novo["ativa"])
    if "pre_aprovar" in novo:
        cfg["pre_aprovar"] = bool(novo["pre_aprovar"])
    if "apenas_dias_uteis" in novo:
        # só segunda a sexta (sem monitorar a caixa nos fins de semana)
        cfg["apenas_dias_uteis"] = bool(novo["apenas_dias_uteis"])
    if "intervalo_min" in novo:
        # periodicidade da conferência do gatilho (Configurações; 1 min a 24 h)
        try:
            cfg["intervalo_min"] = max(1, min(int(novo["intervalo_min"]), 1440))
        except (TypeError, ValueError):
            pass
    if "incluir_pretensoes" in novo:
        termos = novo["incluir_pretensoes"] or []
        if isinstance(termos, str):
            termos = termos.split(",")
        vistos_ = set()
        cfg["incluir_pretensoes"] = [t.strip() for t in termos
                                     if str(t).strip() and not (_norm(t) in vistos_ or vistos_.add(_norm(t)))]
    cfg.pop("excluir_pretensoes", None)  # versão anterior (lista de exclusão)
    if cfg["ativa"] and not atual["ativa"]:
        # ligou agora: o que já está na caixa fica como "anterior à ativação"
        cfg["ativada_em"] = datetime.now().isoformat(timespec="seconds")
        estado = _ler_json(_ARQ_ESTADO, {"vistos": {}})
        for pn, _d, _p in _candidatos_brutos():
            estado["vistos"].setdefault(pn, {"em": cfg["ativada_em"], "resultado": "anterior"})
        _gravar_json(_ARQ_ESTADO, estado)
    raw = config_painel_service._ler_raw()
    raw["rotina_dossie_sd"] = cfg
    config_painel_service._salvar_raw(raw)
    if cfg["ativa"] or "intervalo_min" in novo:
        _gatilho.set()  # aplica já (novo intervalo conta a partir de agora)
    return cfg


# ---------------------------------------------------------------- histórico

def _registrar(paj_norm: str, resultado: str, detalhe: str, **extra) -> None:
    meta = _ler_json(PAJS_DIR / paj_norm / "metadata.json", {})
    linha = {"ts": datetime.now().isoformat(timespec="seconds"), "paj_norm": paj_norm,
             "paj": meta.get("paj") or paj_norm, "assistido": meta.get("assistido_caixa") or "",
             "pretensao": meta.get("pretensaopura") or "", "resultado": resultado,
             "detalhe": detalhe, **extra}
    _ARQ_HIST.parent.mkdir(parents=True, exist_ok=True)
    with _ARQ_HIST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def historico(limite: int = 200) -> list[dict]:
    if not _ARQ_HIST.exists():
        return []
    linhas = _ARQ_HIST.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in reversed(linhas[-limite:]):
        with contextlib.suppress(Exception):
            out.append(json.loads(ln))
    return out


# ---------------------------------------------------------------- candidatos

def _data_br(s: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        with contextlib.suppress(Exception):
            return datetime.strptime((s or "").strip()[:len(fmt) + 2], fmt)
    return None


def _candidatos_brutos() -> list[tuple[str, datetime | None, str]]:
    """PAJs na caixa com trâmite "Abertura de PAJ": (paj_norm, data do envio, pretensão)."""
    out = []
    for p in PAJS_DIR.iterdir():
        if not p.is_dir():
            continue
        m = _ler_json(p / "metadata.json", None)
        if not m or not m.get("em_caixa_atual"):
            continue
        trs = [t for t in m.get("tramitacoes_caixa") or [] if _norm(t.get("descricao")) == "abertura de paj"]
        if trs or _norm(m.get("desc_mov_caixa")) == "abertura de paj":
            data = _data_br(trs[0].get("data_envio")) if trs else None
            out.append((p.name, data, m.get("pretensaopura") or m.get("pretensao") or ""))
    return out


def _habilitada(pretensao: str, termos: list[str]) -> str:
    """O item da lista que habilita esta pretensão ('' = não habilitada). Casa
    pelo nome (sem maiúsculas/acentos); um item pode ser parte do nome."""
    alvo = _norm(pretensao)
    return next((t for t in termos if _norm(t) and _norm(t) in alvo), "") if alvo else ""


def pretensoes_conhecidas() -> list[str]:
    """Pretensões que já aparecem nos PAJs do painel — sugestões para a lista."""
    vistas = {}
    for p in PAJS_DIR.iterdir():
        m = _ler_json(p / "metadata.json", None) if p.is_dir() else None
        pr = ((m or {}).get("pretensaopura") or "").strip()
        if pr:
            vistas.setdefault(_norm(pr), pr)
    return sorted(vistas.values(), key=_norm)


def candidatos() -> list[dict]:
    cfg = get_config()
    vistos = _ler_json(_ARQ_ESTADO, {"vistos": {}})["vistos"]
    ativada = datetime.fromisoformat(cfg["ativada_em"]) if cfg.get("ativada_em") else None
    out = []
    for pn, data, pret in sorted(_candidatos_brutos(), key=lambda x: x[1] or datetime.min):
        if pn in vistos:
            continue
        if ativada and data and data < ativada:
            continue
        out.append({"paj_norm": pn, "pretensao": pret, "habilitada_por": _habilitada(pret, cfg["incluir_pretensoes"])})
    return out


def _marcar(paj_norm: str, resultado: str) -> None:
    estado = _ler_json(_ARQ_ESTADO, {"vistos": {}})
    estado["vistos"][paj_norm] = {"em": datetime.now().isoformat(timespec="seconds"), "resultado": resultado}
    _gravar_json(_ARQ_ESTADO, estado)


def reprocessar(paj_norm: str) -> dict:
    """Tira o PAJ da lista de vistos — volta à fila no próximo ciclo."""
    estado = _ler_json(_ARQ_ESTADO, {"vistos": {}})
    estado["vistos"].pop(paj_norm, None)
    _gravar_json(_ARQ_ESTADO, estado)
    _gatilho.set()
    return {"ok": True}


# ---------------------------------------------------------------- execução

def _cpf_confere(pdf: Path, cpf: str) -> bool:
    digitos = re.sub(r"\D", "", cpf or "")
    if len(digitos) != 11:
        return False
    try:
        import fitz
        with fitz.open(str(pdf)) as d:
            texto = "".join(d[i].get_text() for i in range(min(3, d.page_count)))
    except Exception:  # noqa: BLE001
        return False
    return digitos in re.sub(r"\D", "", texto)


async def _processar(paj_norm: str) -> None:
    from services import aprovacao_mcp_service as aprov
    from services import dossie_sd_service as sd
    from services.sat_service import FASE_MOV

    cfg = get_config()
    meta = _ler_json(PAJS_DIR / paj_norm / "metadata.json", {})
    paj_label = (meta.get("paj") or "").strip()

    # 1) dossiê
    _execucao["etapa"] = "Gerando o Dossiê do Seguro-Desemprego Web"
    r = sd.iniciar(paj_norm, "completo")
    if not r.get("ok"):
        raise RuntimeError(r.get("erro") or "não foi possível iniciar o dossiê")
    for _ in range(TEMPO_MAX_DOSSIE // 3):
        await asyncio.sleep(3)
        st = sd.get_status(paj_norm)
        if st.get("status") != "running":
            break
    st = sd.get_status(paj_norm)
    if st.get("status") != "done" or not st.get("arquivos"):
        raise RuntimeError("dossiê: " + (st.get("erro") or "não concluído (tempo esgotado)"))
    arquivos = st["arquivos"]

    # 2) trava: CPF do PDF = CPF do assistido do PAJ
    cpf, _nome, _lbl = sd._dados_assistido(paj_norm)
    pdf = PAJS_DIR / paj_norm / sd.SUBPASTA_SD / arquivos[0]
    if not _cpf_confere(pdf, cpf):
        raise RuntimeError("o CPF do dossiê gerado não confere com o do assistido do PAJ — NÃO juntado")

    # 3) juntada (pré-aprovada só com o modelo exato)
    _execucao["etapa"] = "Juntando ao PAJ"
    if cfg["pre_aprovar"]:
        aprov.pre_aprovar(paj_label, FASE_MOV, sd.MOV_DESCRICAO, list(arquivos),
                          origem="rotina_dossie_sd", validade_seg=TEMPO_MAX_JUNTADA)
    r = sd.juntar(paj_norm)
    if not r.get("ok"):
        raise RuntimeError("juntada: " + (r.get("erro") or "não iniciada"))
    sess = None
    for _ in range(TEMPO_MAX_JUNTADA // 3):
        await asyncio.sleep(3)
        sess = sd._sessao_final(paj_norm) or {}
        if sess.get("juntada") in ("ok", "erro"):
            break
    sess = sess or {}
    if sess.get("juntada") != "ok":
        raise RuntimeError("juntada: " + (sess.get("erro_juntada") or "não concluída (tempo esgotado)"))
    _registrar(paj_norm, "sucesso", f"Dossiê gerado e juntado ({', '.join(arquivos)})",
               mov_seq=sess.get("mov_seq"), pre_aprovada=bool(cfg["pre_aprovar"]))
    _marcar(paj_norm, "sucesso")
    with contextlib.suppress(Exception):
        from services import lembretes_service
        lembretes_service.criar_lembrete(paj_norm, "Dossiê SD Web gerado e juntado (rotina automática)", "verde")


async def ciclo() -> dict:
    """Uma passada: processa os candidatos, um de cada vez."""
    from services import cofre_service

    cfg = get_config()
    if not cfg["ativa"] or _execucao["rodando"]:
        return {"ok": False, "motivo": "inativa" if not cfg["ativa"] else "em_execucao"}
    _execucao.update(rodando=True, ultimo_ciclo=datetime.now().isoformat(timespec="seconds"), aguardando="")
    feitos = 0
    try:
        for c in candidatos():
            pn = c["paj_norm"]
            if not c["habilitada_por"]:
                _registrar(pn, "ignorado", "Pretensão não habilitada para a rotina"
                           + (f' ("{c["pretensao"]}")' if c["pretensao"] else " (PAJ sem pretensão cadastrada)"))
                _marcar(pn, "ignorado")
                continue
            if cofre_service.configurado() and not cofre_service.destrancado():
                _execucao["aguardando"] = "Cofre trancado — desbloqueie para a rotina usar o login do SAT."
                break
            _execucao.update(paj=pn, etapa="Iniciando")
            try:
                await _processar(pn)
                feitos += 1
            except Exception as e:  # noqa: BLE001
                _registrar(pn, "erro", str(e)[:400])
                _marcar(pn, "erro")
            finally:
                _execucao.update(paj="", etapa="")
    finally:
        _execucao["rodando"] = False
    return {"ok": True, "processados": feitos}


def estado() -> dict:
    cfg = get_config()
    return {**cfg, **{k: v for k, v in _execucao.items()}, "fila": len(candidatos()) if cfg["ativa"] else 0,
            "pausada_fim_de_semana": bool(cfg.get("apenas_dias_uteis") and _fim_de_semana())}


def disparar(forcar: bool = False) -> None:
    if forcar:
        _forcar["proximo"] = True
    _gatilho.set()


def _fim_de_semana() -> bool:
    return datetime.now().weekday() >= 5  # 5 = sábado, 6 = domingo


async def laco() -> None:
    """Laço de fundo (startup do app): um ciclo a cada `intervalo_min` (Configurações;
    padrão INTERVALO_SEG), ou na hora
    em que for disparado (ativação, "Executar agora", reprocessar)."""
    await asyncio.sleep(30)
    while True:
        with contextlib.suppress(Exception):
            cfg = get_config()
            forcar, _forcar["proximo"] = _forcar["proximo"], False
            if cfg["ativa"] and (forcar or not (cfg.get("apenas_dias_uteis") and _fim_de_semana())):
                await ciclo()
        _gatilho.clear()
        try:
            espera = max(1, int(get_config().get("intervalo_min") or 5)) * 60
        except (TypeError, ValueError):
            espera = INTERVALO_SEG
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_gatilho.wait(), timeout=espera)
