"""Historico estruturado de eventos por PAJ — append-only em JSONL.

Cada PAJ tem um `historico.jsonl` na sua pasta. Cada linha e' um dict com no
minimo `ts` (ISO 8601) e `evento`. Demais campos sao livres por evento.

Eventos atualmente registrados:
  - elaborar    : sessao Claude concluida (skill, status, summary_first_line)
  - correcao    : usuario pediu correcao/refazer (texto_correcao truncado)
  - gerar_docx  : DOCX/PDF gerado (formato, arquivo_origem, arquivo_saida)

A escrita e' best-effort: tudo embrulhado em try/suppress, append em modo
texto, sem flock/lock — o painel e' single-user (uvicorn workers=1) e os
eventos sao raros o suficiente pra colisao ser improvavel. Se algum dia
virar concorrente, troca-se por filelock.

Leitura: `ler(paj_norm, limit=100)` retorna lista de eventos (mais recentes
primeiro). Util pra UI exibir um timeline do PAJ.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from config import PAJS_DIR


_NOME_ARQUIVO = "historico.jsonl"
_MAX_TEXTO_LIVRE = 1000  # limita campos longos pra arquivo nao virar log gigante

# Mesmo regex/lógica de `_formatar_pretensao` em app.py (filtro Jinja) —
# duplicado aqui porque aquele é um closure privado só registrado como
# filtro, sem módulo próprio pra importar; usado pra computar "assunto" nos
# registros de análise (ver listar_analises() abaixo), já que a formatação
# do lado do template não se aplica a dados servidos via JSON/API.
_RE_PRETENSAO_PARENS = re.compile(r"\(([^()]{4,})\)\s*$")


def _formatar_pretensao(valor: str | None) -> str:
    if not valor:
        return ""
    m = _RE_PRETENSAO_PARENS.search(str(valor))
    return m.group(1).strip() if m else str(valor)


# Store da página "Análises e Iniciais" (subitem da Caixa de Entrada) —
# marca PAJs cujo caso já foi revisado/tratado pelo Defensor, pra sumirem da
# lista de trabalho. MESMO PADRÃO de `services/prazo_baixa_service.py`
# ("baixa local"): um cache local, nunca a verdade definitiva — se um NOVO
# evento `analise_pretensao` (análise refeita, nova petição gerada) acontecer
# DEPOIS da marca de conclusão, o caso volta a aparecer (ver
# listar_analises_iniciais_pendentes()). Fora de PAJS_DIR/OFICIO_GERAL de
# propósito (mesmo local de _prazos_baixados.json) — é estado do PAINEL, não
# do caso em si.
_CONCLUIDOS_STORE = Path(__file__).resolve().parents[1].parent / "dados" / "_analises_iniciais_concluidas.json"


def _carregar_concluidos() -> dict:
    if not _CONCLUIDOS_STORE.exists():
        return {}
    try:
        return json.loads(_CONCLUIDOS_STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _salvar_concluidos(dados: dict) -> None:
    _CONCLUIDOS_STORE.parent.mkdir(parents=True, exist_ok=True)
    _CONCLUIDOS_STORE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def concluir_analise_inicial(
    paj_norm: str, paj: str = "", assistido: str = "", atividade: str = ""
) -> bool:
    """Marca este PAJ como tratado na página "Análises e Iniciais" — some da
    lista até que um evento `analise_pretensao` mais recente que esta marca
    apareça nesse PAJ (mesma filosofia de `prazo_baixa_service`: nunca
    escondido pra sempre por engano).

    Também registra a conclusão no Histórico já existente — tabela
    "Conclusões da Caixa de Entrada" (`services.historico_painel`, mesma
    usada por Retornos/SAT) — pedido do usuário, 2026-09-30: "para fins de
    histórico". `paj`/`assistido`/`atividade` vêm do próprio registro que o
    frontend já tinha em mãos (evita reler metadata.json/historico.jsonl só
    pra repetir o que a página "Análises e Iniciais" já mostrava); `origem`
    "analise-ia" identifica esses registros na tabela (mesmo padrão do
    badge `r.origem` já exibido lá pra "retorno"/"sat")."""
    pasta = _pasta_paj(paj_norm)
    if pasta is None:
        return False
    dados = _carregar_concluidos()
    dados[paj_norm] = _dt.datetime.now().isoformat(timespec="seconds")
    _salvar_concluidos(dados)

    with contextlib.suppress(Exception):
        from services import historico_painel

        historico_painel.registrar({
            "paj": paj or paj_norm,
            "paj_norm": paj_norm,
            "assistido": assistido,
            "origem": "analise-ia",
            "atividade": atividade or "Análise/Petição Inicial (IA)",
            "concluido": True,
        })
    return True


def _pasta_paj(paj_norm: str) -> Path | None:
    """Valida e retorna a pasta do PAJ (ou None se invalido)."""
    if not paj_norm or "/" in paj_norm or "\\" in paj_norm or ".." in paj_norm:
        return None
    if not paj_norm.startswith("PAJ-"):
        return None
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists() or not pasta.is_dir():
        return None
    return pasta


def _truncar(valor: Any) -> Any:
    """Trunca strings longas pra evitar inflar o JSONL com PROMPT_MAX inteiro."""
    if isinstance(valor, str) and len(valor) > _MAX_TEXTO_LIVRE:
        return valor[:_MAX_TEXTO_LIVRE].rstrip() + "..."
    return valor


def registrar(paj_norm: str, evento: str, **dados: Any) -> bool:
    """Append nao-bloqueante de um evento no historico.jsonl do PAJ.

    Retorna True se gravou, False se PAJ invalido ou falha de IO. Falhas sao
    silenciosas — historico nunca deve interromper o fluxo principal.
    """
    pasta = _pasta_paj(paj_norm)
    if pasta is None:
        return False
    if not evento:
        return False

    registro: dict[str, Any] = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "evento": str(evento),
    }
    for k, v in dados.items():
        if v is None or v == "":
            continue
        registro[str(k)] = _truncar(v)

    arquivo = pasta / _NOME_ARQUIVO
    try:
        with arquivo.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def ler(paj_norm: str, limit: int | None = None) -> list[dict]:
    """Le o historico em ordem cronologica reversa (mais recente primeiro).

    `limit=None` retorna tudo. Linhas malformadas sao puladas silenciosamente.
    """
    pasta = _pasta_paj(paj_norm)
    if pasta is None:
        return []
    arquivo = pasta / _NOME_ARQUIVO
    if not arquivo.exists():
        return []

    eventos: list[dict] = []
    with contextlib.suppress(Exception):
        for linha in arquivo.read_text(encoding="utf-8", errors="replace").splitlines():
            linha = linha.strip()
            if not linha:
                continue
            try:
                eventos.append(json.loads(linha))
            except json.JSONDecodeError:
                continue

    eventos.reverse()
    if limit is not None and limit > 0:
        eventos = eventos[:limit]
    return eventos


def listar_analises(eventos: set[str] | None = None) -> list[dict]:
    """Agrega, entre TODAS as pastas de PAJ, os eventos de análise/elaboração
    por IA — alimenta `listar_analises_iniciais_pendentes()` (página "Análises
    e Iniciais", subitem da Caixa de Entrada; a antiga aba "Análises (IA)" do
    Histórico foi removida em 2026-09-30, pedido do usuário — mesma função,
    lugar melhor).

    Diferente de `ler()` (por PAJ), esta função varre `PAJS_DIR` inteiro —
    mesmo padrão de `services/paj_service.listar_pajs()` (itera todas as
    pastas) e de `services/historico_painel.listar()` (resolve o assistido
    pelo metadata.json, cobrindo registros antigos que não guardaram o nome).

    Sem armazenamento próprio: lê direto o `historico.jsonl` de cada PAJ (via
    `ler()`) — nenhum dado é duplicado. `eventos` filtra por tipo (default:
    só `analise_pretensao`, a origem de "Análise Prévia"/"Gerada Petição
    Inicial" — ver `services/analise_pretensao_service._persistir`); passe um
    conjunto maior pra incluir outros eventos (`elaborar`, `calcular_cnis`,
    `calcular_qs`, `consulta_rpv`, `organizar_ajuizamento`, `peticao_ia`).

    Compatibilidade com entradas ANTIGAS (gravadas antes do campo `atividade`
    existir — painel rodando código anterior, sem restart ainda): quando um
    evento `analise_pretensao` não tem `atividade` persistida, aplica um
    FALLBACK só pro evento MAIS RECENTE de cada PAJ (`ler()` já devolve mais
    recente primeiro): se a petição inicial existe agora nesse PAJ
    (`organizar_ajuizamento_service.tem_peticao_inicial`), rotula como
    "Gerada Petição Inicial (retroativo)" — sinalizando que é inferido pela
    existência atual do arquivo, não capturado no momento exato. Eventos mais
    antigos do mesmo PAJ sem `atividade` ficam "Análise Prévia" (não dá pra
    saber com certeza qual rodada específica gerou o arquivo, então não
    arriscamos marcar mais de uma)."""
    eventos = eventos or {"analise_pretensao"}
    if not PAJS_DIR.exists():
        return []

    from services.organizar_ajuizamento_service import tem_peticao_inicial

    out: list[dict] = []
    for pasta in PAJS_DIR.iterdir():
        if not pasta.is_dir() or not pasta.name.startswith("PAJ-"):
            continue
        if not (pasta / _NOME_ARQUIVO).exists():
            continue
        paj_norm = pasta.name
        meta: dict = {}
        with contextlib.suppress(Exception):
            meta = json.loads((pasta / "metadata.json").read_text(encoding="utf-8"))
        paj_display = meta.get("paj") or paj_norm
        assistido = str(meta.get("assistido_caixa") or meta.get("assistido") or "").strip()
        pretensao_raw = meta.get("pretensaopura") or meta.get("classificacao") or ""
        assunto = _formatar_pretensao(pretensao_raw)
        ja_aplicou_fallback = False
        for i, ev in enumerate(ler(paj_norm)):
            if ev.get("evento") not in eventos:
                continue
            atividade = ev.get("atividade")
            if not atividade and ev.get("evento") == "analise_pretensao":
                if not ja_aplicou_fallback:
                    ja_aplicou_fallback = True
                    peticao_existe = False
                    with contextlib.suppress(Exception):
                        peticao_existe = tem_peticao_inicial(paj_norm)
                    atividade = "Gerada Petição Inicial (retroativo)" if peticao_existe else "Análise Prévia"
                else:
                    atividade = "Análise Prévia"
            out.append({
                # Sintético (não persistido) — só pra `:key` único no x-for do
                # frontend (ver memória "x-for duplicate key collapses items";
                # `ts` sozinho colide se dois eventos do mesmo PAJ caírem no
                # mesmo segundo).
                "id": f"{paj_norm}:{i}",
                "ts": ev.get("ts", ""),
                "paj_norm": paj_norm,
                "paj": paj_display,
                "assistido": assistido,
                "assunto": assunto,
                "evento": ev.get("evento", ""),
                "atividade": atividade or ev.get("evento", ""),
                "skill": ev.get("skill", ""),
                "status": ev.get("status", ""),
                "resumo": ev.get("resumo", ""),
            })
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def listar_em_processamento() -> list[dict]:
    """Casos com uma sessão de análise/petição rodando AGORA (em memória) —
    tabela "Em processamento" da página "Análises e Iniciais", primeira das
    três (pedido do usuário, 2026-09-30): pura visibilidade, sem nenhuma ação
    disponível. O caso some daqui sozinho assim que a sessão termina (a
    fonte, `analise_pretensao_service.get_status_todos()`, só lista sessões
    'running'), e na sincronização seguinte do frontend já aparece em
    "Petições Iniciais" ou "Análises" (`listar_analises_iniciais_pendentes()`
    — o evento só é persistido no historico.jsonl quando a sessão conclui).
    Nada é persistido aqui; import local de analise_pretensao_service pra
    evitar import circular (esse módulo já importa `historico`)."""
    from services import analise_pretensao_service

    status_todos = analise_pretensao_service.get_status_todos()
    if not status_todos:
        return []
    out: list[dict] = []
    for paj_norm, info in status_todos.items():
        meta: dict = {}
        with contextlib.suppress(Exception):
            meta = json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
        paj_display = meta.get("paj") or paj_norm
        assistido = str(meta.get("assistido_caixa") or meta.get("assistido") or "").strip()
        pretensao_raw = meta.get("pretensaopura") or meta.get("classificacao") or ""
        tipo = info.get("tipo", "")
        tipo_cfg = analise_pretensao_service.CONFIG_TIPOS.get(tipo) or {}
        out.append({
            "id": paj_norm,
            "paj_norm": paj_norm,
            "paj": paj_display,
            "assistido": assistido,
            "assunto": _formatar_pretensao(pretensao_raw),
            "tipo": tipo,
            "tipo_label": tipo_cfg.get("label", tipo),
            "last_action": info.get("last_action", ""),
            "iniciado_em": info.get("iniciado_em", ""),
        })
    out.sort(key=lambda r: r.get("iniciado_em", ""), reverse=True)
    return out


def listar_analises_iniciais_pendentes() -> list[dict]:
    """Lista de TRABALHO da página "Análises e Iniciais" (subitem da Caixa de
    Entrada, pedido do usuário 2026-09-30) — UM REGISTRO POR CASO (PAJ), não
    um por evento como `listar_analises()`: pega só o evento MAIS RECENTE de
    cada PAJ (`listar_analises()` já devolve ordenado por `ts` desc, então o
    primeiro visto por PAJ já é o mais recente) e omite PAJs marcados como
    concluídos aqui (`concluir_analise_inicial`) — A MENOS que tenham rodado
    uma análise NOVA depois da marca de conclusão, caso em que voltam a
    aparecer (mesma lógica de "baixa local" de prazo_baixa_service: a marca é
    um cache otimista, nunca a verdade definitiva).

    Cada registro ganha `doc_status` — coluna "Documentos" (pedido do usuário
    2026-09-30): "sem_peticao" (ainda não há petição inicial gerada — botão
    "Organizar Documentos para Ajuizamento" fica desabilitado também em
    paj_detail.html nesse caso), ou o status de
    `organizar_ajuizamento_service.get_status()` (idle/running/done/error) —
    MESMA fonte usada pelo botão da página do PAJ, nada duplicado."""
    from services import organizar_ajuizamento_service

    todos = listar_analises()
    concluidos = _carregar_concluidos()
    vistos: set[str] = set()
    out: list[dict] = []
    for r in todos:
        paj_norm = r["paj_norm"]
        if paj_norm in vistos:
            continue
        vistos.add(paj_norm)
        concluido_em = concluidos.get(paj_norm)
        if concluido_em and concluido_em >= r.get("ts", ""):
            continue
        tem_peticao = False
        with contextlib.suppress(Exception):
            tem_peticao = organizar_ajuizamento_service.tem_peticao_inicial(paj_norm)
        if tem_peticao:
            r["doc_status"] = organizar_ajuizamento_service.get_status(paj_norm).get("status", "idle")
            # Petição gerada FORA de uma sessão de análise (ex.: sessão do Claude
            # Code direto na pasta) não grava evento — o último continua "Análise
            # Prévia" e o caso ficava em "Análises" embora a etapa do PAJ já diga
            # "Inicial minutada" (que olha só o arquivo). Mesma fonte da etapa:
            # a existência da petição decide a tabela.
            if "Petição Inicial" not in (r.get("atividade") or ""):
                r["atividade"] = "Gerada Petição Inicial (retroativo)"
        else:
            r["doc_status"] = "sem_peticao"
        out.append(r)
    return out


def primeira_linha_util(texto: str) -> str:
    """Extrai a primeira linha nao-vazia de um texto. Util pra resumir
    `summary` de elaboracao em uma frase no historico."""
    if not texto:
        return ""
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha:
            return linha[:200]
    return ""
