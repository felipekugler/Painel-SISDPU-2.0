"""Baixa LOCAL de prazos cumpridos dentro do próprio Painel — evita que a
tabela de "Prazos e-proc" (e tudo que dela deriva: Agenda, badge de
"intimação pendente", checkbox de "Ações no Eproc", página Intimações,
bloqueio de Gerenciamento de Processo por prazo residual) continue mostrando
um prazo que o usuário já resolveu por dentro do painel (peticionou, deu
mera ciência) até a PRÓXIMA "Sincronizar intimações" rodar — pedido do
usuário 2026-09-19: exigir sincronizações repetidas só pra "limpar" a tabela
é ruim.

Suprime pela CHAVE DO PRAZO (evento + data_envio + prazo_dias — mesmo
critério de identidade já usado em `eproc_intimacoes.caso_e_prazos_
mesclados`), NUNCA por posição/índice (instável entre sincronizações) e
NUNCA pelo campo "parte" (o Painel do Procurador só mostra o 1º autor do
processo — duas intimações reais e distintas podem ter texto byte-a-byte
idêntico, ver nota em `prazos.html`). Guarda uma CONTAGEM por chave (não um
booleano): se o processo tiver 2 intimações reais idênticas na chave
(litisconsórcio ativo) e só uma delas for baixada aqui, a outra continua
visível — nunca esconde mais do que o que foi de fato confirmado como
resolvido.

É um cache OTIMISTA e TEMPORÁRIO, nunca a verdade — a próxima sincronização
real (`eproc_intimacoes.salvar`) sempre vence: `limpar_processo()` é chamada
de lá pra cada processo resincronizado, descartando a baixa local em favor
do que o e-proc mostrar de fato (se o prazo realmente fechou, nem aparece
mais no scrape; se por algum motivo ainda estiver aberto, volta a aparecer
— nunca fica escondido pra sempre por engano). Por isso a leitura "crua"
(`eproc_intimacoes.carregar()`), usada por `salvar()` e por
`purgar_casos_resolvidos()` (ações destrutivas/de mesclagem que precisam do
estado real), NUNCA passa por este filtro — só a leitura "visível"
(`eproc_intimacoes.carregar_visivel()`), usada por telas e cálculos que
mostram "o que ainda está pendente"."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_STORE = Path(__file__).resolve().parents[1].parent / "dados" / "_prazos_baixados.json"


def _normalizar_num_processo(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _chave_prazo(p: dict) -> str:
    return "|".join([
        (p.get("evento") or "").strip(),
        (p.get("data_envio") or "").strip(),
        str(p.get("prazo_dias") or "").strip(),
    ])


def _carregar() -> dict:
    if not _STORE.exists():
        return {}
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _salvar(dados: dict) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def marcar_baixa(processo: str, prazo: dict, origem: str, detalhe: str = "") -> dict:
    """Registra que UMA ocorrência da chave (evento+data_envio+prazo_dias) deste
    processo foi resolvida dentro do painel (`origem`: "eproc-peticionamento",
    "mera-ciencia", etc. — só pra auditoria/depuração). Incrementa a
    contagem — nunca substitui, várias chamadas pra chaves iguais se somam."""
    chave_proc = _normalizar_num_processo(processo)
    chave_p = _chave_prazo(prazo or {})
    if not chave_proc or not chave_p.strip("|"):
        return {"ok": False, "erro": "processo ou prazo sem dados suficientes p/ identidade."}
    dados = _carregar()
    proc_map = dados.setdefault(chave_proc, {})
    entry = proc_map.setdefault(chave_p, {"qtd": 0, "eventos": []})
    entry["qtd"] += 1
    entry["eventos"].append({
        "em": datetime.now().isoformat(timespec="seconds"),
        "origem": origem, "detalhe": detalhe,
    })
    _salvar(dados)
    return {"ok": True, "qtd": entry["qtd"]}


def limpar_processo(processo: str) -> None:
    """Descarta toda baixa local deste processo — chamada a cada sincronização
    REAL (`eproc_intimacoes.salvar`) que toque o processo, que passa a valer
    como verdade a partir dali."""
    chave_proc = _normalizar_num_processo(processo)
    if not chave_proc:
        return
    dados = _carregar()
    if chave_proc in dados:
        del dados[chave_proc]
        _salvar(dados)


def aplicar_filtro(secoes: dict) -> dict:
    """Devolve uma CÓPIA de `secoes` (formato de `eproc_intimacoes.carregar()
    ["secoes"]`) com as ocorrências baixadas removidas de `prazos` em cada
    caso, e casos que ficarem sem NENHUM prazo restante removidos da seção
    (nunca deixa uma linha "fantasma" sem prazo nenhum).

    A contagem de baixa é reaplicada do zero para CADA lista `prazos`
    encontrada (mesmo processo pode aparecer em mais de uma categoria/seção,
    ex. "pendente" e "pendente_urgente" simultaneamente — ver nota em
    `caso_e_prazos_mesclados`) — nunca consome de um saldo compartilhado
    entre categorias, senão suprimir numa esconderia o "resto" que deveria
    continuar visível na outra."""
    dados = _carregar()
    if not dados:
        return secoes
    out: dict = {}
    for chave_secao, casos in (secoes or {}).items():
        novos_casos = []
        for c in casos or []:
            chave_proc = _normalizar_num_processo(c.get("processo") or "")
            baixas = dados.get(chave_proc) or {}
            if not baixas:
                novos_casos.append(c)
                continue
            restante = {k: v.get("qtd", 0) for k, v in baixas.items()}
            prazos_visiveis = []
            for p in c.get("prazos") or []:
                chave_p = _chave_prazo(p)
                if restante.get(chave_p, 0) > 0:
                    restante[chave_p] -= 1
                    continue
                prazos_visiveis.append(p)
            if prazos_visiveis:
                novo = dict(c)
                novo["prazos"] = prazos_visiveis
                novos_casos.append(novo)
        out[chave_secao] = novos_casos
    return out
