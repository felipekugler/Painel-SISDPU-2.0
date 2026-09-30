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

import re


def meu_identificador() -> str:
    """Identificador do procurador logado no e-proc — reaproveita a própria
    credencial de LOGIN já cadastrada no cofre unificado (mesmo valor que
    aparece em Configurações como "cadastrado · <usuario>"), confirmado
    2026-09-23 que é o MESMO texto usado pelo e-proc como `identificador`
    de representante (ex.: "DPU551") — nenhum cadastro novo é necessário.

    Vazio quando o cofre está trancado ou sem credencial "eproc" salva —
    nesse caso NÃO há como saber a quais partes o procurador está
    associado. Os chamadores devem tratar isso como identidade
    indisponível e NUNCA arriscar mostrar/agir sobre uma parte adivinhada."""
    from services import cofre_service

    return (cofre_service.get_usuario("eproc") or "").strip()


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
    partes = (parte_intimada or "").split(" - ", 1)
    nome = partes[1] if len(partes) > 1 else partes[0]
    nome_norm = _sem_acento(_normalizar(nome))
    if "DEFENSORIA PUBLICA DA UNIAO" in nome_norm:
        return True
    if not minhas_partes:
        return False
    return _normalizar(nome) in minhas_partes
