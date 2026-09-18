"""Serviço da página "Agenda" — calendário e tabela unificando, num só lugar:

  - **Audiências** já capturadas do SISDPU por PAJ (`metadata.json["sisdpu_raw"]
    ["audiencias"]`, gravadas por `ingestao/sisdpu_client.py` a cada sync do PAJ)
  - **Prazos judiciais finais** já sincronizados do e-proc (cache mantido pela
    página "Prazos" em `dados/_eproc_intimacoes.json`, via `ingestao/eproc_intimacoes.py`)
  - **Compromissos manuais** e **períodos de afastamento** cadastrados pelo
    próprio defensor nesta tela

Este serviço NÃO sincroniza nada por conta própria — audiências e prazos são
lidos do que outras telas já mantêm atualizado; só compromissos/afastamentos
manuais são persistidos aqui.

Persistência dos itens manuais: `<PAJS_DIR>/.agenda.json`, no mesmo espírito
de `.watchlist.json` — um dict com `atualizada_em` e duas coleções
(`compromissos`, `afastamentos`), cada uma indexada por id (uuid4 hex).

Todo evento devolvido por `listar_eventos()` sai num formato único, pronto
para o FullCalendar do front-end:
    {id, tipo: "audiencia"|"prazo"|"manual"|"afastamento",
     data_inicio, data_fim (ambas "YYYY-MM-DD"), hora_inicio ("HH:MM" ou None),
     titulo, descricao, local, paj_norm, processo, status, origem,
     urgente?, tipo_afastamento?}
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from datetime import datetime

from config import PAJS_DIR

AGENDA_FILE = PAJS_DIR / ".agenda.json"

TIPOS_AFASTAMENTO: dict[str, str] = {
    "ferias": "Férias",
    "recesso": "Recesso forense",
    "suspensao_distribuicao": "Suspensão de distribuição",
    "licenca": "Licença",
    "substituicao": "Substituição",
    "feriado": "Feriado",
    "plantao": "Plantão",
    "outro": "Outro",
}


def listar_tipos_afastamento() -> list[dict]:
    """{value, label} de cada tipo, sempre em ordem alfabética pelo label — o
    front-end busca daqui (em vez de fixar `<option>`s em HTML) pra um tipo
    novo já nascer na posição certa sem precisar reordenar nada na tela."""
    return [
        {"value": chave, "label": label}
        for chave, label in sorted(TIPOS_AFASTAMENTO.items(), key=lambda item: item[1])
    ]

# Paleta de cores pra "Compromisso"/"Cadastrar Evento" — 8 pares (matiz igual,
# suave e forte). Fonte única de verdade: o front-end (templates/agenda.html)
# usa a MESMA lista (mesma ordem) pra desenhar os círculos do seletor e pra
# decidir texto claro/escuro em cima da cor. Validação aqui evita gravar um
# hex arbitrário vindo de fora do seletor (mesmo padrão de
# services/lembretes_service.py: cor fora da paleta -> cai pro padrão).
CORES_EVENTO: list[str] = [
    # suaves
    "#FDE68A", "#FED7AA", "#FECACA", "#FBCFE8", "#E9D5FF", "#BFDBFE", "#99F6E4", "#BBF7D0",
    # fortes (mesma ordem de matiz das suaves acima)
    "#F59E0B", "#EA580C", "#DC2626", "#DB2777", "#9333EA", "#2563EB", "#0D9488", "#16A34A",
]
_CORES_FORTES = set(CORES_EVENTO[8:])


def _validar_cor(cor: str | None) -> str:
    cor = (cor or "").strip().upper()
    return cor if cor in CORES_EVENTO else ""

_RE_DATA_BR = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_RE_HORA = re.compile(r"(\d{1,2}):(\d{2})")


def _data_br_para_iso(texto: str) -> str:
    """Acha a primeira data DD/MM/AAAA num texto livre e devolve em ISO
    (YYYY-MM-DD). Usado tanto para o texto livre de audiências do SISDPU
    quanto para as datas BR do cache de prazos do e-proc. '' se não achar."""
    m = _RE_DATA_BR.search(texto or "")
    if not m:
        return ""
    dia, mes, ano = m.groups()
    return f"{ano}-{mes}-{dia}"


def _hora_de_texto_livre(texto: str) -> str | None:
    m = _RE_HORA.search(texto or "")
    if not m:
        return None
    return f"{m.group(1).zfill(2)}:{m.group(2)}"


_RE_PAJ_NORM = re.compile(r"^PAJ-(\d{4})-(\d{3})-(\d{5})$")


def _paj_norm_para_exibicao(paj_norm: str) -> str:
    """'PAJ-2026-029-00136' -> '2026/029-00136' (formato de exibição), pro
    título do evento ficar no mesmo padrão usado para prazos judiciais."""
    m = _RE_PAJ_NORM.match(paj_norm or "")
    return f"{m.group(1)}/{m.group(2)}-{m.group(3)}" if m else paj_norm


# --- fonte 1: audiências já capturadas do SISDPU (por PAJ) ------------------


def _listar_audiencias() -> list[dict]:
    """Varre PAJS_DIR e agrega as audiências que a sincronização do PAJ já
    grava em metadata.json["sisdpu_raw"]["audiencias"] (ver
    ingestao/sisdpu_client.py, extrairAudiencias). Nível do PAJ — não amarrado
    a um processo individual, mesmo quando há mais de um vinculado."""
    eventos: list[dict] = []
    if not PAJS_DIR.exists():
        return eventos
    for pasta in PAJS_DIR.iterdir():
        if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
            continue
        meta_path = pasta / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        audiencias = ((metadata.get("sisdpu_raw") or {}).get("audiencias")) or []
        if not audiencias:
            continue
        paj_exibicao = _paj_norm_para_exibicao(pasta.name)
        for i, a in enumerate(audiencias):
            data_iso = _data_br_para_iso(a.get("data_hora", ""))
            if not data_iso:
                continue
            eventos.append({
                "id": f"audiencia-{pasta.name}-{i}",
                "tipo": "audiencia",
                "data_inicio": data_iso,
                "data_fim": data_iso,
                "hora_inicio": _hora_de_texto_livre(a.get("data_hora", "")),
                "titulo": f"Audiência - PAJ {paj_exibicao}",
                "descricao": a.get("observacao", ""),
                "local": a.get("local", ""),
                "paj_norm": pasta.name,
                "processo": "",
                "status": "",
                "origem": "sisdpu",
            })
    return eventos


# --- fonte 2: prazos judiciais finais já sincronizados do e-proc ------------


def _listar_prazos() -> list[dict]:
    """Projeta o cache que a página "Prazos" mantém (dados/_eproc_intimacoes.json)
    em eventos de agenda — um por prazo com data final (real ou estimada).
    Não dispara sincronização nenhuma; só lê o que já está em disco.

    Agrupa por (PAJ, dia) antes de devolver: a tabela de "/prazos" trata cada
    intimação separadamente (é o correto lá — cada uma pode ter origem/prazo
    diferente), mas na Agenda várias intimações do MESMO PAJ vencendo no MESMO
    dia devem aparecer como um único compromisso — ver `_agrupar_prazos_por_dia`."""
    from ingestao import eproc_intimacoes

    brutos: list[dict] = []
    dados = eproc_intimacoes.carregar()
    contador = 0  # desempate de chave — um mesmo processo pode aparecer em
    # mais de uma categoria do e-proc ao mesmo tempo (ex.: "prazo em aberto" e
    # "decurso de prazo"); sem isso, prazos sem PAJ/processo resolvido
    # colidiriam na chave de agrupamento.
    for casos in (dados.get("secoes") or {}).values():
        for caso in casos:
            processo = caso.get("processo", "")
            paj = caso.get("paj", "")
            urgente = str(caso.get("urgente", "")).strip().lower() in ("sim", "true", "1")
            for p in caso.get("prazos") or []:
                final_real = p.get("final_prazo") or ""
                final_estimado = p.get("final_estimado") or ""
                data_iso = _data_br_para_iso(final_real or final_estimado)
                if not data_iso:
                    continue
                contador += 1
                brutos.append({
                    "data_inicio": data_iso,
                    "paj": paj,
                    "processo": processo,
                    "urgente": urgente,
                    "estimado": not final_real,
                    "evento": p.get("evento") or "Prazo final",
                    "seq": contador,
                })
    return _agrupar_prazos_por_dia(brutos)


def _agrupar_prazos_por_dia(brutos: list[dict]) -> list[dict]:
    grupos: dict[tuple[str, str], list[dict]] = {}
    for b in brutos:
        chave_paj = b["paj"] or b["processo"] or f"sem-chave-{b['seq']}"
        grupos.setdefault((chave_paj, b["data_inicio"]), []).append(b)

    eventos: list[dict] = []
    for (chave_paj, data_iso), itens in grupos.items():
        paj = itens[0]["paj"]
        urgente = any(i["urgente"] for i in itens)
        estimado = all(i["estimado"] for i in itens)
        # Só mostra o processo se TODOS os prazos do grupo forem do mesmo
        # processo — um PAJ pode ter mais de um processo judicial vinculado,
        # e escolher um dos processos "no chute" seria enganoso.
        processos_unicos = {i["processo"] for i in itens if i["processo"]}
        processo = next(iter(processos_unicos)) if len(processos_unicos) == 1 else ""

        # Título curto pra exibir no calendário/tabela — o texto crú do e-proc
        # (`evento`, ex.: "Expedida/certificada a intimação eletrônica...") é
        # jurídico-processual, não serve de rótulo visual.
        if paj:
            titulo = f"Prazo judicial - PAJ {paj}"
        elif processo:
            titulo = f"Prazo judicial - {processo}"
        else:
            titulo = "Prazo judicial"
        # Estimado (prazo ainda não aberto oficialmente no e-proc — só uma
        # projeção pela regra de ciência tácita) marca com "*" no título, sem
        # parênteses (ver legenda da página). Quantidade de prazos agrupados
        # NÃO entra no título — não interessa pra quem só quer ver o
        # calendário; fica só na descrição (tooltip), ver abaixo.
        if estimado:
            titulo += " *"

        if len(itens) > 1:
            descricao = (
                f"{len(itens)} prazos com vencimento nesta data neste PAJ "
                "— veja o detalhamento de cada um na página \"Prazos\"."
            )
        else:
            descricao = itens[0]["evento"]

        eventos.append({
            "id": f"prazo-{chave_paj}-{data_iso}",
            "tipo": "prazo",
            "data_inicio": data_iso,
            "data_fim": data_iso,
            "hora_inicio": None,
            "titulo": titulo,
            "descricao": descricao,
            "local": "",
            "paj_norm": paj,
            "processo": processo,
            "status": "estimado" if estimado else "aberto",
            "urgente": urgente,
            "origem": "eproc",
            "n_prazos": len(itens),
        })
    return eventos


# --- persistência dos itens manuais (compromissos e afastamentos) ----------


def _carregar() -> dict:
    if not AGENDA_FILE.exists():
        return {"compromissos": {}, "afastamentos": {}, "atualizada_em": None}
    try:
        dados = json.loads(AGENDA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"compromissos": {}, "afastamentos": {}, "atualizada_em": None}
    dados.setdefault("compromissos", {})
    dados.setdefault("afastamentos", {})
    return dados


def _salvar(dados: dict) -> None:
    dados["atualizada_em"] = datetime.now().isoformat(timespec="seconds")
    PAJS_DIR.mkdir(parents=True, exist_ok=True)
    AGENDA_FILE.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --- CRUD: compromissos manuais ---------------------------------------------


def criar_compromisso(
    titulo: str,
    descricao: str = "",
    data_inicio: str = "",
    hora_inicio: str | None = None,
    paj_norm: str = "",
    processo: str = "",
    status: str = "agendado",
    cor: str = "",
) -> dict:
    dados = _carregar()
    novo_id = uuid.uuid4().hex
    agora = datetime.now().isoformat(timespec="seconds")
    item = {
        "titulo": (titulo or "").strip(),
        "descricao": (descricao or "").strip(),
        "data_inicio": (data_inicio or "").strip(),
        "hora_inicio": (hora_inicio or "").strip() or None,
        "paj_norm": (paj_norm or "").strip(),
        "processo": (processo or "").strip(),
        "status": (status or "agendado").strip(),
        "cor": _validar_cor(cor),
        "criado_em": agora,
        "atualizado_em": agora,
    }
    dados["compromissos"][novo_id] = item
    _salvar(dados)
    return {"id": novo_id, **item}


_CAMPOS_COMPROMISSO = ("titulo", "descricao", "data_inicio", "hora_inicio", "paj_norm", "processo", "status", "cor")


def atualizar_compromisso(compromisso_id: str, **campos) -> dict | None:
    dados = _carregar()
    item = dados["compromissos"].get(compromisso_id)
    if item is None:
        return None
    for chave in _CAMPOS_COMPROMISSO:
        valor = campos.get(chave)
        if valor is None:
            continue
        if chave == "cor":
            valor = _validar_cor(valor)
        item[chave] = valor.strip() if isinstance(valor, str) else valor
    item["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    _salvar(dados)
    return {"id": compromisso_id, **item}


def remover_compromisso(compromisso_id: str) -> bool:
    dados = _carregar()
    if compromisso_id not in dados["compromissos"]:
        return False
    del dados["compromissos"][compromisso_id]
    _salvar(dados)
    return True


# --- CRUD: períodos de afastamento ------------------------------------------


def criar_afastamento(
    tipo_afastamento: str,
    data_inicio: str,
    data_fim: str,
    titulo: str = "",
    descricao: str = "",
    cor: str = "",
) -> dict:
    dados = _carregar()
    novo_id = uuid.uuid4().hex
    agora = datetime.now().isoformat(timespec="seconds")
    tipo_afastamento = tipo_afastamento if tipo_afastamento in TIPOS_AFASTAMENTO else "outro"
    item = {
        "tipo_afastamento": tipo_afastamento,
        "titulo": (titulo or "").strip(),
        "descricao": (descricao or "").strip(),
        "data_inicio": (data_inicio or "").strip(),
        "data_fim": (data_fim or "").strip(),
        "cor": _validar_cor(cor),
        "criado_em": agora,
        "atualizado_em": agora,
    }
    dados["afastamentos"][novo_id] = item
    _salvar(dados)
    return {"id": novo_id, **item}


_CAMPOS_AFASTAMENTO = ("tipo_afastamento", "titulo", "descricao", "data_inicio", "data_fim", "cor")


def atualizar_afastamento(afastamento_id: str, **campos) -> dict | None:
    dados = _carregar()
    item = dados["afastamentos"].get(afastamento_id)
    if item is None:
        return None
    for chave in _CAMPOS_AFASTAMENTO:
        valor = campos.get(chave)
        if valor is None:
            continue
        if chave == "tipo_afastamento" and valor not in TIPOS_AFASTAMENTO:
            continue
        if chave == "cor":
            valor = _validar_cor(valor)
        item[chave] = valor.strip() if isinstance(valor, str) else valor
    item["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    _salvar(dados)
    return {"id": afastamento_id, **item}


def remover_afastamento(afastamento_id: str) -> bool:
    dados = _carregar()
    if afastamento_id not in dados["afastamentos"]:
        return False
    del dados["afastamentos"][afastamento_id]
    _salvar(dados)
    return True


# --- agregação final ---------------------------------------------------------


def listar_eventos(inicio: str | None = None, fim: str | None = None) -> list[dict]:
    """Une as 4 fontes num formato só. `inicio`/`fim` (ISO "YYYY-MM-DD",
    opcionais) filtram por sobreposição de intervalo — comparação de string
    funciona porque o formato ISO já ordena lexicograficamente."""
    eventos: list[dict] = []
    eventos.extend(_listar_audiencias())
    eventos.extend(_listar_prazos())

    dados = _carregar()
    for cid, item in dados.get("compromissos", {}).items():
        # Nome exibido = "Tipo - Título" (aqui o "tipo" é sempre "Compromisso"
        # — não tem sub-tipo como o afastamento). Título é obrigatório no
        # cadastro (ver routes/agenda.py CompromissoIn), então sempre existe.
        titulo_usuario = item.get("titulo", "")
        titulo = f"Compromisso - {titulo_usuario}" if titulo_usuario else "Compromisso"
        eventos.append({
            "id": cid,
            "tipo": "manual",
            "data_inicio": item.get("data_inicio", ""),
            "data_fim": item.get("data_inicio", ""),
            "hora_inicio": item.get("hora_inicio"),
            "titulo": titulo,
            "descricao": item.get("descricao", ""),
            "local": "",
            "paj_norm": item.get("paj_norm", ""),
            "processo": item.get("processo", ""),
            "status": item.get("status", ""),
            "cor": item.get("cor", ""),
            "origem": "manual",
        })
    for aid, item in dados.get("afastamentos", {}).items():
        # Nome exibido = "Tipo - Título" (ex.: "Feriado - Independência do
        # Brasil"); título é opcional aqui, então sem ele fica só o tipo.
        tipo_label = TIPOS_AFASTAMENTO.get(item.get("tipo_afastamento", ""), "Afastamento")
        titulo_usuario = item.get("titulo", "")
        titulo = f"{tipo_label} - {titulo_usuario}" if titulo_usuario else tipo_label
        eventos.append({
            "id": aid,
            "tipo": "afastamento",
            "data_inicio": item.get("data_inicio", ""),
            "data_fim": item.get("data_fim") or item.get("data_inicio", ""),
            "hora_inicio": None,
            "titulo": titulo,
            "descricao": item.get("descricao", ""),
            "local": "",
            "paj_norm": "",
            "processo": "",
            "status": "",
            "cor": item.get("cor", ""),
            "tipo_afastamento": item.get("tipo_afastamento", ""),
            "origem": "manual",
        })

    if inicio:
        eventos = [e for e in eventos if not e.get("data_fim") or e["data_fim"] >= inicio]
    if fim:
        eventos = [e for e in eventos if not e.get("data_inicio") or e["data_inicio"] <= fim]

    with contextlib.suppress(Exception):
        eventos.sort(key=lambda e: (e.get("data_inicio") or "", e.get("hora_inicio") or ""))
    return eventos
