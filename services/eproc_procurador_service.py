"""Identidade do procurador logado ("quem sou eu" pro resto do painel) e
resolução de "a quais partes deste processo o procurador está associado" —
substitui por completo (2026-09-23) a antiga heurística de "polo ativo"
(`_PAPEL_POLO_ATIVO`, existia em 3 lugares: `acoes_eproc_service.
listar_prazos`, `intimacao_ia_service._casar_evento_referenciado` e
`static/js/processo_judicial.js::intimacoesAtivas`), que assumia a DPU
sempre no polo ativo (o segurado, contra o INSS) e por isso escolhia a
parte ERRADA sempre que a DPU representava o polo passivo. Caso real que
expôs isso: PAJ 2021/029-02882, processo 5011174-83.2021.4.04.7000
(Curadoria — DPU representa LAURA KNOLL GARCIA DE OLIVEIRA como RÉ numa
disputa de dependentes de pensão por morte; a heurística escolhia a
intimação da AUTORA — já fechada — e escondia a da assistida real, ainda
aberta). Pedido explícito do usuário: não há como fixar um padrão de polo,
já que o defensor pode estar em qualquer polo, com uma ou mais partes
associadas — a única fonte confiável é o PRÓPRIO cadastro de partes do
processo.

`cabecalho.partes[*].representantes[*].identificador` (devolvido de graça
por `eproc_mcp_client.triagem_processo`/`cabecalho_completo`, sem custo
extra de consulta) já diz exatamente a quais partes cada procurador está
associado, sem adivinhação nenhuma."""

from __future__ import annotations

import contextlib
import re


def meu_identificador(numero_processo: str = "") -> str:
    """Identificador do procurador logado no e-proc — reaproveita a própria
    credencial de LOGIN já cadastrada no cofre unificado (mesmo valor que
    aparece em Configurações como "cadastrado · <usuario>"), confirmado
    2026-09-23 que é o MESMO texto usado pelo e-proc como `identificador`
    de representante (ex.: "DPU551") — nenhum cadastro novo é necessário.

    1ª opção (pedido do Defensor, 11/10/2026): o usuário do LOGIN do MCP do
    e-proc, guardado no Credential Manager do Windows — é o MESMO
    identificador (ex.: "DPU551") e não depende do cofre, que no Painel
    SISDPU 2.0 fica trancado (não pede a senha-mestra ao abrir). O cofre é
    só reserva, para quem não tiver o MCP do e-proc configurado.

    Vazio quando nenhuma das duas fontes tem o usuário — nesse caso NÃO há
    como saber a quais partes o procurador está associado. Os chamadores
    devem tratar isso como identidade indisponível e NUNCA arriscar
    mostrar/agir sobre uma parte adivinhada.

    Processo da Justiça Militar da União (segmento 7 do número CNJ): o
    identificador é o login do MCP da JMU (ex.: "DPUKUGLER"), não o do TRF4
    (17/10/2026) — passe `numero_processo` sempre que souber de qual processo
    se trata. Sem o login da JMU configurado, devolve vazio (nunca o do TRF4)."""
    from services import cofre_service

    if numero_processo:
        from ingestao import eproc_jmu
        if eproc_jmu.eh_processo_jmu(numero_processo):
            return eproc_jmu.meu_identificador()

    return (_usuario_mcp_eproc()
            or (cofre_service.get_usuario("eproc") or "").strip())


def _usuario_mcp_eproc() -> str:
    """Usuário do login do MCP eproc-trf4 (chave definida no config.json dele)."""
    try:
        import json

        import keyring

        from ingestao.eproc_mcp_client import MCP_DIR
        chave = "mcp-eproc"
        with contextlib.suppress(Exception):
            chave = json.loads((MCP_DIR / "config.json").read_text(encoding="utf-8")).get("credential_key") or chave
        return (keyring.get_password(chave, "usuario") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _normalizar(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def _sem_acento(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def partes_do_procurador(cabecalho: dict, identificador: str) -> set[str]:
    """Nomes (normalizados) das partes do processo cujo(s) representante(s)
    (`cabecalho.partes[*].representantes[*]`) têm este `identificador` —
    ou seja, as partes que ESTE procurador especificamente representa
    NESTE processo. Vazio se `identificador` vier vazio (identidade
    indisponível) ou nenhuma parte bater (cabeçalho incompleto/processo
    sem este procurador cadastrado)."""
    alvo = _normalizar(identificador)
    if not alvo:
        return set()
    nomes: set[str] = set()
    for parte in (cabecalho or {}).get("partes") or []:
        for rep in parte.get("representantes") or []:
            if _normalizar(rep.get("identificador")) == alvo:
                nomes.add(_normalizar(parte.get("nome")))
                break
    return nomes


def parte_intimada_e_minha(parte_intimada: str, minhas_partes: set[str]) -> bool:
    """True se o NOME em `parte_intimada` (formato "PAPEL - Nome", ex.
    "REQUERIDO - LAURA KNOLL GARCIA DE OLIVEIRA") é uma das partes em
    `minhas_partes` (de `partes_do_procurador`) — casa pelo NOME, nunca
    pelo rótulo de papel: "REU" no cabeçalho de partes aparece como
    "REQUERIDO"/"EXECUTADO"/"IMPETRADO"/... na intimação, dependendo da
    classe processual — nome é o único campo estável entre as duas
    representações.

    Exceção literal, independente de `minhas_partes`: quando o NOME intimado
    é a própria "Defensoria Pública da União" (ex. papel "INTERESSADO" —
    curadoria especial/terceiro interessado, onde `cabecalho.partes[*]
    .representantes` não lista ninguém pro réu, então `partes_do_procurador`
    nunca teria como achar isso por identificador). Bug real 2026-09-25 (PAJ
    2018/029-01953, processo 5048558-22.2017.4.04.7000): réu sem
    representante nenhum no cabeçalho, intimação foi endereçada direto à
    "INTERESSADO - DEFENSORIA PÚBLICA DA UNIÃO" — `minhas_partes` ficava
    vazio e o prazo nunca casava, sumindo do resumo de execução E da
    seleção real de fechamento no e-proc (não só da tela). Não é uma
    heurística de polo (a mesma que este módulo eliminou, ver docstring do
    arquivo) — é un match textual direto e sem ambiguidade: só existe uma
    Defensoria Pública da União num processo, e ela SEMPRE sou eu quando
    estou logado com uma credencial DPU."""
    _papel, nome = _papel_e_nome(parte_intimada)
    nome_norm = _sem_acento(_normalizar(nome))
    if "DEFENSORIA PUBLICA DA UNIAO" in nome_norm:
        return True
    if not minhas_partes:
        return False
    return _normalizar(nome) in minhas_partes


def _papel_e_nome(parte_intimada: str) -> tuple[str, str]:
    """"INTERESSADO - Fulano" -> ("INTERESSADO", "Fulano")."""
    partes = (parte_intimada or "").split(" - ", 1)
    return (partes[0], partes[1]) if len(partes) > 1 else ("", partes[0])


# --- Intimações vinculadas ao procurador FORA do cadastro de partes ----------
# O cabeçalho de partes devolvido pelo MCP do e-proc só traz AUTOR/RÉU — quem
# está no processo como INTERESSADO (representante legal cadastrado à parte,
# conforme a vara, ou a própria DPU em nome próprio) não aparece ali, e por
# isso nunca casava com `partes_do_procurador`. Regra do Defensor (27/10/2026):
# vale TODA intimação vinculada ao procurador, como parte ou como interessado.
# Caso real: processo 5056781-46.2026.4.04.7000 (PAJ 2026/029-02412) — prazo
# de "INTERESSADO - AIDIO FERNANDES" (representante legal) no MEU Painel do
# Procurador, mas sem casar com a triagem: aparecia sem parte/ato na página do
# PAJ e não era fechado ao peticionar pelo Ações no Eproc.
#
# O universo continua sendo o MEU Painel do Procurador (só traz o que é meu);
# a parte fora do cabeçalho só serve pra CASAR um prazo desse Painel com a
# intimação ao vivo, com prioridade menor que a parte confirmada no cabeçalho.
# Parte que ESTÁ no cabeçalho com outro procurador (ex. INSS) nunca entra, nem
# papéis que jamais são a DPU (perito, Ministério Público...).
_PAPEIS_ALHEIOS = ("PERITO", "MPF", "MINISTERIO PUBLICO", "ASSISTENTE TECNICO",
                   "TESTEMUNHA", "LEILOEIRO", "CONTADOR", "OFICIAL DE JUSTICA")


def vinculo_intimacao(parte_intimada: str, cabecalho: dict, identificador: str) -> int | None:
    """Quão certo é que esta intimação é do procurador `identificador`:
    0 = parte dele no cabeçalho (ou a própria DPU pelo nome); 1 = parte fora
    do cabeçalho (interessado/representante legal), só aceitável casada com
    um prazo do MEU Painel do Procurador; None = não é dele. Sem
    `identificador` (identidade indisponível) só vale o nome da DPU."""
    minhas = partes_do_procurador(cabecalho, identificador)
    if parte_intimada_e_minha(parte_intimada, minhas):
        return 0
    if not _normalizar(identificador):
        return None
    papel, nome = _papel_e_nome(parte_intimada)
    no_cabecalho = {_normalizar(p.get("nome")) for p in (cabecalho or {}).get("partes") or []}
    if _normalizar(nome) in no_cabecalho:
        return None
    texto = _sem_acento(_normalizar(papel + " " + nome))
    if any(a in texto for a in _PAPEIS_ALHEIOS):
        return None
    return 1


def _dia(s: str) -> str:
    m = re.search(r"\d{2}/\d{2}/\d{4}", s or "")
    return m.group(0) if m else ""


def _dia_hora(s: str) -> str:
    m = re.search(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}(:\d{2})?", s or "")
    return m.group(0) if m else ""


def _dias(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def candidatos_do_prazo(prazo: dict, intimacoes: list[dict], cabecalho: dict, identificador: str,
                        *, so_ativas: bool = True, ignorar: set[int] | None = None) -> list[int]:
    """Índices (em `intimacoes`, da triagem ao vivo) que podem ser ESTE prazo
    do MEU Painel do Procurador — mesmo dia de envio, mesmo nº de dias e
    vinculada a mim (ver `vinculo_intimacao`) —, do mais provável ao menos:
    1º a expedição no MESMO segundo do `data_envio` do Painel (distingue
    autor e representante intimados no mesmo ato), depois a parte confirmada
    no cabeçalho antes da de fora dele, depois a ainda ativa."""
    dia_p = _dia(prazo.get("data_envio", ""))
    if not dia_p:
        return []
    hora_p = _dia_hora(prazo.get("data_envio", ""))
    dias_p = _dias(prazo.get("prazo_dias"))
    ranqueados = []
    for i, it in enumerate(intimacoes):
        if ignorar and i in ignorar:
            continue
        if so_ativas and not it.get("ativa", True):
            continue
        if _dia(it.get("data_hora", "")) != dia_p:
            continue
        dias_it = _dias(it.get("prazo_dias"))
        if dias_p != -1 and dias_it != -1 and dias_p != dias_it:
            continue
        vinculo = vinculo_intimacao(it.get("parte_intimada", ""), cabecalho, identificador)
        if vinculo is None:
            continue
        mesmo_segundo = bool(hora_p) and _dia_hora(it.get("data_hora", "")) == hora_p
        ranqueados.append(((0 if mesmo_segundo else 1, vinculo, 0 if it.get("ativa", True) else 1), i))
    ranqueados.sort()
    return [i for _, i in ranqueados]


def casar_prazos(prazos: list[dict], intimacoes: list[dict], cabecalho: dict, identificador: str,
                 *, so_ativas: bool = True) -> list[int | None]:
    """Para cada prazo do MEU Painel do Procurador, o índice da intimação da
    triagem que é ele (um-para-um — a mesma intimação nunca serve a dois
    prazos), ou None quando nenhuma casa."""
    usadas: set[int] = set()
    out: list[int | None] = []
    for p in prazos:
        cands = candidatos_do_prazo(p, intimacoes, cabecalho, identificador,
                                    so_ativas=so_ativas, ignorar=usadas)
        if cands:
            usadas.add(cands[0])
            out.append(cands[0])
        else:
            out.append(None)
    return out
