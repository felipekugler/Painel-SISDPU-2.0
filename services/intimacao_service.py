"""Índice local da "Triagem Inteligente de Intimações".

Funde os casos já sincronizados pela aba "Prazos e-proc" (cache
`ingestao.eproc_intimacoes`, `dados/_eproc_intimacoes.json`) com o estado
LOCAL desta feature: vínculo de PAJ (automático via lembrete, ou manual),
resumo da intimação, confronto com o estágio do PAJ (classificação em 1 de 4
rótulos) e a minuta gerada.

⚠️ Este módulo é PURAMENTE leitura/preparo (índice em disco) — quem faz as
chamadas de IA/consulta mais pesadas é `services.intimacao_ia_service`; quem
eventualmente MOVIMENTA/TRAMITA/CONCLUI de verdade continua sendo,
exclusivamente, os consoles "Ações no e-proc"/"Ações no SISDPU" já
existentes (ou, pra "Dar mera ciência", `services.mera_ciencia_service` —
ver lá), acionados manualmente pelo Defensor. `purgar_casos_resolvidos()`
(2026-09-16: espelha SEMPRE a caixa de Prazos e-proc sincronizada, sem
reconfirmação ao vivo — pedido explícito do usuário) só lê o cache já
sincronizado em disco, nenhuma chamada de rede própria.

Persistência: `dados/_intimacoes_analise.json` (sibling de `Projeto/`, mesmo
nível de `_eproc_intimacoes.json` — ver `ingestao/eproc_intimacoes.py:_STORE`),
escrita atômica (arquivo `.tmp` + `os.replace`, com retentativas em
`PermissionError`) — mesmo padrão de `services.config_painel_service._salvar_raw`,
adaptado para um path fora da raiz do projeto.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

DADOS_DIR = Path(__file__).resolve().parents[1].parent / "dados"
_STORE = DADOS_DIR / "_intimacoes_analise.json"
_PROJETO_DIR = Path(__file__).resolve().parents[1]
_ARQUIVO_WORKER = _PROJETO_DIR / "services" / "_arquivo_worker.py"

# Serializa as sequências leitura-modifica-grava (RMW) do arquivo inteiro
# (`salvar_estado` e a etapa final de `purgar_casos_resolvidos`) — sem isso,
# uma análise em andamento (fila do "Analisar todos") gravando seu status e
# uma purga disparada por uma sincronização de Prazos e-proc concorrente
# (ver `routes/prazos.py`) podem se atropelar: o `PermissionError` no
# `.tmp` (bug real 2026-08-28) já era sintoma disso, e mesmo sem erro
# visível o "perdedor" da corrida teria sua gravação silenciosamente
# sobrescrita (lost update). O lock só protege o trecho ler+montar+gravar em
# memória (rápido) — nunca fica preso durante chamadas ao MCP/rede.
_LOCK = threading.RLock()

# Os 4 rótulos possíveis do confronto intimação × estágio do PAJ (ver
# services.intimacao_ia_service.confrontar_com_paj).
CLASSIFICACOES = (
    "CUMPRIMENTO_IMEDIATO_CIENCIA",
    "CUMPRIMENTO_IMEDIATO_PETICIONAR",
    "DILIGENCIA_NECESSARIA",
    "REQUER_ANALISE_HUMANA",
    # 5º rótulo, fora do confronto genérico (b) de `intimacao_ia_service.py` —
    # atribuído diretamente pelo fluxo dedicado de laudo pericial médico
    # (`gerar_impugnacao_laudo_pericial`), nunca por `_parse_confronto`.
    "IMPUGNACAO_LAUDO_PERICIAL",
    # 6º/7º rótulos, também fora do confronto genérico — fluxo dedicado de
    # conferência de RPV/precatório (`gerar_conferencia_rpv`, 2026-08-28):
    # a RPV expedida bate com o que foi acordado/decidido no processo
    # (nenhuma minuta) ou não bate (gera petição simples de retificação).
    "CIENCIA_RPV",
    "RETIFICACAO_RPV",
    # 8º/9º/10º rótulos, também fora do confronto genérico — fluxo dedicado de
    # conferência de sentença homologatória de acordo (`gerar_conferencia_
    # sentenca_homologatoria`, 2026-08-28, reestruturado 2026-08-29 com
    # orientação de 3 vias em vez de CIENCIA/REQUER_ANALISE_HUMANA): a
    # sentença bate com a proposta original (CIENCIA), o batimento não pôde
    # ser concluído com segurança (ANALISAR — nome novo do antigo
    # REQUER_ANALISE_HUMANA nesse fluxo específico), ou há divergência clara
    # o bastante para apontar uma providência, sem chegar a redigi-la
    # (PETICIONAR — nunca gera minuta própria, pedido explícito do usuário:
    # só sinalizar, não redigir).
    "CIENCIA_SENTENCA_HOMOLOGATORIA",
    "SENTENCA_HOMOLOGATORIA_ANALISAR",
    "SENTENCA_HOMOLOGATORIA_PETICIONAR",
)

STATUS_GERAL = ("novo", "analisando", "aguardando_revisao", "concluido", "descartado", "erro", "gravado_lote")

_PAJ_NORM_RE = re.compile(r"^PAJ-\d{4}-\d{3}-\d{5}$")
_PAJ_BARRA_RE = re.compile(r"^(\d{4})/(\d{3})-(\d{5})$")


def _normalizar_num_processo(s: str) -> str:
    """Só os dígitos do número CNJ — mesma normalização usada em
    `services.retornos_service._normalizar_num_processo` e
    `services.acoes_eproc_service._normalizar_num_processo` (reimplementada
    aqui, mesmo padrão de duplicação já adotado entre esses dois módulos, pra
    não criar um import cruzado só por uma linha de regex)."""
    return re.sub(r"\D", "", s or "")


def paj_para_norm(paj: str | None) -> str | None:
    """'2026/029-01010' ou 'PAJ-2026-029-01010' -> 'PAJ-2026-029-01010'.
    None se `paj` for vazio ou não bater nenhum dos dois formatos aceitos
    (mesma tolerância de `services.sync_service._paj_identificador_para_norm`)."""
    s = (paj or "").strip()
    if not s:
        return None
    if _PAJ_NORM_RE.match(s):
        return s
    m = _PAJ_BARRA_RE.match(s)
    if m:
        return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _ler_raw() -> dict:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _gravar_via_subprocesso(destino: Path, txt: str) -> bool:
    """Tenta gravar `txt` em `destino` através de `_arquivo_worker.py`, um
    processo Python de VIDA CURTA e SEM socket de rede — medida provisória
    (2026-08-28) enquanto o TI não libera uma exceção no FortiClient, que
    vem bloqueando o PROCESSO DO SERVIDOR (`app.py`, porta 8001 aberta) de
    gravar/substituir arquivos (mesmo um arquivo novo, nunca visto antes —
    ver `services._arquivo_worker` e conversa com o usuário). Um teste
    isolado confirmou que um processo assim, sem rede, grava/apaga
    livremente na mesma pasta.

    Devolve True em sucesso; False em qualquer falha (worker ausente, erro
    de execução, timeout) — o chamador cai de volta pro método antigo
    (escrita direta + retentativas), então uma falha aqui nunca impede a
    gravação, só a torna mais lenta."""
    if not _ARQUIVO_WORKER.exists():
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(_ARQUIVO_WORKER), str(destino)],
            input=txt.encode("utf-8"), capture_output=True,
            cwd=str(_PROJETO_DIR), timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _salvar_raw(data: dict) -> None:
    """Grava de forma ATÔMICA (arquivo temp + os.replace, com retentativas em
    PermissionError) — mesmo padrão de `services.config_painel_service._salvar_raw`,
    só muda o destino (aqui é em `dados/`, fora da raiz do projeto).

    Tenta primeiro via subprocesso (`_gravar_via_subprocesso` — ver docstring
    lá); só cai no método antigo (escrita direta pelo próprio processo do
    servidor, com retentativas) se o subprocesso falhar por qualquer motivo
    alheio ao bloqueio do antivírus."""
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(data, indent=2, ensure_ascii=False)

    if _gravar_via_subprocesso(_STORE, txt):
        return

    tmp = str(_STORE) + ".tmp"
    ultimo_erro: Exception | None = None
    for _ in range(6):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(txt)
            os.replace(tmp, str(_STORE))  # troca atômica
            return
        except PermissionError as e:  # sharing violation transitória -> espera e retenta
            ultimo_erro = e
            time.sleep(0.2)
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            raise
    # esgotou as tentativas atômicas: escrita direta como último recurso
    try:
        _STORE.write_text(txt, encoding="utf-8")
    except Exception:
        if ultimo_erro:
            raise ultimo_erro
        raise


def _registro_vazio() -> dict:
    """Registro local de UM ATO (não mais de um processo inteiro — ver
    `_agrupar_prazos_por_ato`). Não carrega `paj`/`paj_norm`/`vinculo_status`:
    vínculo de PAJ é por PROCESSO (pedido explícito do usuário, 2026-09-XX,
    compartilhado por todos os atos dele — ver `salvar_vinculo`/`_chave_vinculo`),
    nunca duplicado/divergente entre os registros de ato do mesmo processo."""
    return {
        "evento_referenciado": None,
        "resumo_intimacao": None,
        "escopo_verificacao": None,
        "confronto": None,
        "minuta": None,
        "status_geral": "novo",
        "erro": None,
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Agrupamento por ATO REFERENCIADO — unidade real da Triagem Inteligente
# --------------------------------------------------------------------------
# Pedido explícito do usuário (2026-09-XX, "inegociável"): o foco da triagem é
# a INTIMAÇÃO (o ato referenciado), nunca o processo — o processo é só o
# container que agrupa. Um processo com 2 intimações do MESMO ato (ex.:
# litisconsórcio ativo — o mesmo Ofício Requisitório intima 2 cocredores)
# forma 1 SÓ sub-caso ("simultâneos", tratados em conjunto); um processo com
# 2 atos DIFERENTES (ex.: Sentença + RPV, cada um com seu próprio prazo) forma
# 2 sub-casos INDEPENDENTES ("autônomos" — análise, classificação, minuta e
# "Dar mera ciência" separados, sem um interferir no outro). Mesma lógica de
# agrupamento (e o mesmo motivo de descoberta) de `prazos.html:_expandirCaso`.

def _chave_ato_str(p: dict) -> str:
    """Identifica o ATO referenciado por um prazo raspado do Painel do
    Procurador — evento (texto) + data de envio + nº de dias. NÃO inclui a
    parte intimada de propósito: o Painel só mostra o 1º autor do processo
    na legenda, nunca o nome do co-litigante específico daquela linha (achado
    real 2026-08-24/2026-09-17 — ver `prazos.html`), então "parte" não serve
    pra distinguir nada aqui; é exatamente essa identidade (mesmo ato) que
    define "simultâneos"."""
    return "|".join([p.get("evento") or "", p.get("data_envio") or "", str(p.get("prazo_dias") or "")])


def _hash_ato(chave_str: str) -> str:
    """Hash curto e ESTÁVEL da chave do ato — vira o sufixo da chave de
    persistência (`<processo_digits>:<hash>`). Nunca um índice posicional: se
    um ato fechar e outro permanecer, a posição de cada um numa lista
    ordenada por prazo pode mudar de uma leitura pra outra — usar posição
    como chave faria a análise de UM ato ser lida/gravada por engano como se
    fosse de OUTRO. O hash deriva só do CONTEÚDO do ato (evento+data+dias),
    que não muda enquanto o ato existir."""
    import hashlib
    return hashlib.sha1(chave_str.encode("utf-8")).hexdigest()[:12]


def _agrupar_prazos_por_ato(prazos: list[dict]) -> list[list[dict]]:
    """Agrupa os prazos (já raspados pelo Painel do Procurador, um processo)
    por ato referenciado (`_chave_ato_str`) — na ordem em que aparecem.
    Prazos do MESMO grupo = mesmo ato intimando mais de uma parte
    representada ao mesmo tempo ("simultâneos", 1 sub-caso só); grupos
    DIFERENTES = atos autônomos (1 sub-caso cada)."""
    grupos: dict[str, list[dict]] = {}
    ordem: list[str] = []
    for p in (prazos or []):
        k = _chave_ato_str(p)
        if k not in grupos:
            grupos[k] = []
            ordem.append(k)
        grupos[k].append(p)
    return [grupos[k] for k in ordem]


def _data_br_para_iso(data_br: str) -> str:
    """'DD/MM/AAAA' -> 'AAAA-MM-DD' (ordenável lexicograficamente); '' se não
    bater o formato esperado — nunca lança, só devolve vazio pra quem for
    ordenar cair no fallback "sem data"."""
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", (data_br or "").strip())
    if not m:
        return ""
    dia, mes, ano = m.groups()
    return f"{ano}-{mes}-{dia}"


_BASE_ROTULO_INTIMACAO = "Expedida/certificada a intimação eletrônica"
_RE_SUFIXO_DIAS = re.compile(r"\s+\d+\s+dias?\s*$", re.IGNORECASE)


def _tipo_intimacao_de_evento(evento_texto: str) -> str:
    """Extrai o TIPO da intimação a partir do texto bruto do evento já
    raspado pela sincronização de Prazos e-proc (`ingestao.eproc_intimacoes`)
    — ex.: "Expedida/certificada a intimação eletrônica - Sentença 10 dias"
    -> "Sentença"; "...- Requisição de Pagamento Expedida para Conferência
    5 dias" -> "Requisição de Pagamento Expedida para Conferência"; sem
    qualificador reconhecido (só "...intimação eletrônica N dias") -> "Geral".

    Existe pra permitir agrupar/filtrar a lista de Intimações por tipo ANTES
    de qualquer análise — o dado já vem pronto da sincronização de Prazos
    e-proc (raspador HTTP), não depende de nenhuma chamada extra ao MCP/IA
    (pedido do usuário 2026-08-28: "facilitar a triagem e o disparo de
    análises de casos semelhantes")."""
    texto = _RE_SUFIXO_DIAS.sub("", (evento_texto or "").strip()).strip()
    if not texto or texto == _BASE_ROTULO_INTIMACAO:
        return "Geral"
    prefixo = _BASE_ROTULO_INTIMACAO + " - "
    if texto.startswith(prefixo):
        return texto[len(prefixo):].strip() or "Geral"
    # Formato inesperado (rótulo de evento diferente do padrão conhecido) —
    # melhor mostrar o texto cru do que rotular errado como "Geral".
    return texto


def _prazo_final_mais_proximo(prazos: list[dict]) -> tuple[str, str, str]:
    """Devolve `(exibicao, ordenacao, tipo_intimacao)` do prazo final mais
    próximo — `exibicao` no formato brasileiro de sempre ('DD/MM/AAAA', pra
    mostrar na tela sem mudar nada), `ordenacao` em ISO ('AAAA-MM-DD', pra
    comparar/ordenar corretamente), `tipo_intimacao` extraído do texto bruto
    do evento associado a esse mesmo prazo (`_tipo_intimacao_de_evento`).

    Bug real corrigido: comparar as strings 'DD/MM/AAAA' diretamente (era
    `min(finais)`) ordena pelo DIA primeiro, não pela data — ex.:
    '01/09/2026' vem ANTES de '31/08/2026' numa comparação de string, mesmo
    sendo uma data posterior (01/set é depois de 31/ago). Qualquer
    comparação/ordenação de prazo deve usar SEMPRE a forma ISO."""
    candidatos = []
    for p in (prazos or []):
        final = p.get("final_prazo") or p.get("final_estimado") or ""
        if not final:
            continue
        tipo = _tipo_intimacao_de_evento(p.get("evento") or "")
        candidatos.append((_data_br_para_iso(final) or "9999-99-99", final, tipo))
    if not candidatos:
        return "", "", "Geral"
    candidatos.sort(key=lambda t: t[0])
    return candidatos[0][1], candidatos[0][0], candidatos[0][2]


def _chave_vinculo(processo_chave: str) -> str:
    return f"vinculo:{processo_chave}"


def chave_caso(processo: str, ato: str) -> str:
    """Monta `'<processo_digits>:<ato>'` — a MESMA chave de `item["chave_caso"]`
    em `listar_casos()`. Helper público: todo o pipeline de IA
    (`intimacao_ia_service`) e `mera_ciencia_service` usam isso pra chamar
    `salvar_estado`/`obter_caso` com a chave certa, em vez de montar a
    string na mão em cada função."""
    return f"{_normalizar_num_processo(processo)}:{ato}"


def listar_casos() -> list[dict]:
    """Funde os casos do cache de Prazos e-proc (todas as categorias,
    deduplicados por processo) com o estado local deste índice. NÃO
    sincroniza nada — só lê os dois caches já existentes em disco.

    Devolve UM ITEM POR ATO REFERENCIADO (não por processo — ver
    `_agrupar_prazos_por_ato`, mudança 2026-09-XX pedida explicitamente pelo
    usuário: "o foco é sempre a intimação, e não o processo"). Um processo
    com 2 atos autônomos gera 2 itens independentes, cada um com sua própria
    análise/classificação/minuta/status (`item["chave_caso"]` = identidade
    estável do sub-caso, ver `_hash_ato`); um processo com 2 intimações do
    MESMO ato (litisconsórcio ativo) gera 1 item só ("simultâneos").
    `item["n_simultaneos"]` (>1 = mesmo ato, mais de uma parte) e
    `item["n_atos_processo"]` (>1 = este processo tem outro(s) ato(s)
    autônomo(s) além deste item) alimentam os badges "N Simultâneos"/"N
    Autônomos" (mesma linguagem visual de `prazos.html`).

    Vínculo de PAJ (`paj`/`paj_norm`/`vinculo_status`) é por PROCESSO — um
    único registro (`vinculo:<processo>`) compartilhado por todos os itens
    dele, nunca duplicado. Casos novos (ainda sem vínculo local) usam o PAJ
    já reconhecido pelo lembrete do e-proc ("auto"); sem isso, "pendente"
    até vínculo manual.

    Só devolve processos que estão NA CAIXA agora (no cache de Prazos
    e-proc) — pedido explícito do usuário (2026-09-16): "elimine tudo o que
    não estiver na caixa do procurador... a atualização sempre faça que a
    página de intimações reflita a página de prazos sincronizada". Um
    registro local cujo ato já não aparece mais no cache simplesmente não
    entra na lista (é apagado do disco por `purgar_casos_resolvidos()`,
    chamada antes desta função em `GET /api/intimacoes/casos` — mas mesmo
    que a purga falhe/atrase, esta função já filtra por conta própria, nunca
    sintetiza um item "fora da caixa"). Usa a leitura VISÍVEL (`carregar_
    visivel`) — um ato já baixado localmente dentro do painel (peticionado,
    mera ciência — ver `services.prazo_baixa_service`) desaparece daqui na
    hora, sem esperar nova sincronização."""
    from ingestao import eproc_intimacoes

    cache = eproc_intimacoes.carregar_visivel()
    raw = _ler_raw()
    vistos_processo: set[str] = set()
    itens: list[dict] = []
    for casos in (cache.get("secoes") or {}).values():
        for c in casos or []:
            processo = c.get("processo") or ""
            chave_proc = _normalizar_num_processo(processo)
            if not chave_proc or chave_proc in vistos_processo:
                continue
            vistos_processo.add(chave_proc)

            vinc = raw.get(_chave_vinculo(chave_proc)) or {}
            paj = vinc.get("paj") or c.get("paj") or None
            paj_norm = vinc.get("paj_norm") or paj_para_norm(paj)
            vinculo_status = vinc.get("vinculo_status") or ("auto" if paj_norm else "pendente")

            grupos = _agrupar_prazos_por_ato(c.get("prazos") or [])
            total_atos = len(grupos)
            for grupo in grupos:
                chave_ato = _hash_ato(_chave_ato_str(grupo[0]))
                chave_completa = f"{chave_proc}:{chave_ato}"
                local = raw.get(chave_completa)
                if local is None and total_atos == 1:
                    # Migração de registro LEGADO (chave antiga = só o
                    # processo, de antes desta mudança — 2026-09-XX): só
                    # migra quando não há ambiguidade (processo com 1 ato
                    # só agora), pra nunca atribuir por engano o histórico
                    # de análise de um ato a outro diferente. Processo com
                    # 2+ atos e registro legado começa do zero pros dois —
                    # a análise antiga, de qualquer forma, só cobria o ato
                    # mais próximo (o bug que motivou esta mudança), então
                    # não há dado bom pra preservar pro outro ato.
                    candidato = raw.get(chave_proc)
                    if candidato is not None:
                        local = candidato
                # "gravado_lote": valor LEGADO do extinto "Lote de Mera
                # Ciência" (removido em 2026-08-29) — continua somindo da
                # lista incondicionalmente, mesmo com a intimação ainda
                # ativa no cache. Casos novos de mera ciência concluída
                # usam "concluido", que já some sozinho ao saír da caixa.
                if local is not None and local.get("status_geral") == "gravado_lote":
                    continue
                item = dict(local) if local is not None else _registro_vazio()
                item["processo"] = processo
                item["chave_caso"] = chave_completa
                item["paj"] = paj
                item["paj_norm"] = paj_norm
                item["vinculo_status"] = vinculo_status
                item["juizo"] = c.get("juizo") or ""
                item["assunto"] = c.get("assunto") or ""
                item["partes"] = c.get("partes") or ""
                item["prazos"] = grupo
                item["prazo_final"], item["prazo_final_ordenacao"], item["tipo_intimacao"] = \
                    _prazo_final_mais_proximo(grupo)
                # Lembrete NATIVO do e-proc (post-it da listagem do Painel do
                # Procurador) — puramente informativo, mesmo texto de onde o
                # vínculo automático de PAJ já é extraído.
                item["lembrete_itens"] = c.get("lembrete_itens") or []
                item["n_simultaneos"] = len(grupo)
                item["n_atos_processo"] = total_atos
                itens.append(item)

    itens.sort(key=lambda it: it.get("prazo_final_ordenacao") or "9999-99-99")
    return itens


def obter_caso(processo: str, ato: str | None = None) -> dict | None:
    """`ato` = sufixo de `item["chave_caso"]` (depois dos dois-pontos) —
    identifica o sub-caso específico dentro do processo. `None` devolve o
    1º item encontrado pra este processo (só usado onde o chamador já sabe,
    por outro motivo, que só existe 1 ato — nunca em rotas por-ato)."""
    chave = _normalizar_num_processo(processo)
    if not chave:
        return None
    for item in listar_casos():
        if _normalizar_num_processo(item.get("processo") or "") != chave:
            continue
        if ato is None or (item.get("chave_caso") or "").split(":", 1)[-1] == ato:
            return item
    return None


_CAMPOS_AO_VIVO = ("juizo", "assunto", "partes", "prazos", "prazo_final", "paj", "paj_norm", "vinculo_status")


def _parse_chave_caso(chave_caso: str) -> tuple[str, str]:
    """'<processo_digits>:<ato_hash>' -> (processo_digits, ato_hash). Todo
    chamador de `salvar_estado` monta essa chave a partir de
    `item["chave_caso"]` de um item já listado (`listar_casos`/`obter_caso`)
    — nunca inventa uma na mão."""
    if ":" not in (chave_caso or ""):
        raise ValueError(f"chave_caso inválida (esperado 'processo:ato'): {chave_caso!r}")
    proc, ato = chave_caso.split(":", 1)
    return proc, ato


def salvar_estado(chave_caso: str, **campos) -> dict:
    """Merge parcial ATÔMICO no registro do ATO `chave_caso`
    ('<processo_digits>:<ato_hash>', ver `item["chave_caso"]` em
    `listar_casos`) — só as chaves explicitamente passadas em `campos` são
    alteradas; as demais permanecem intocadas (nunca apagadas por omissão —
    mesmo espírito defensivo do sync do SISDPU, ver `ingestao/sincronizador.py`).
    Para limpar um campo (ex.: `erro`), passe-o explicitamente como `None`.

    Nunca grava `paj`/`paj_norm`/`vinculo_status` — isso é vínculo por
    PROCESSO, ver `salvar_vinculo` (2026-09-XX, pedido explícito do usuário).

    Na 1ª gravação de um ato que ainda não tem entrada persistida em
    `_STORE`, parte da visão já MESCLADA com o cache de Prazos (`obter_caso`)
    em vez de um registro vazio — evita perder dado já computado ao vivo na
    primeira gravação local (ex.: "descartar" antes de qualquer análise)."""
    proc_digits, ato = _parse_chave_caso(chave_caso)
    with _LOCK:
        raw = _ler_raw()
        if chave_caso in raw:
            atual = dict(raw[chave_caso])
        else:
            mesclado = obter_caso(proc_digits, ato)
            atual = dict(mesclado) if mesclado else _registro_vazio()
            for campo in _CAMPOS_AO_VIVO:
                atual.pop(campo, None)
            atual.pop("chave_caso", None)
        atual.update(campos)
        atual["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        raw[chave_caso] = atual
        _salvar_raw(raw)
    return atual


def salvar_vinculo(processo: str, **campos) -> dict:
    """Grava o vínculo de PAJ deste PROCESSO — compartilhado por TODOS os
    atos (autônomos ou simultâneos) dele, nunca duplicado/divergente entre
    eles (pedido explícito do usuário, 2026-09-XX: "vínculo de PAJ continua
    por processo"). Mesmo padrão de merge parcial atômico de `salvar_estado`,
    só que chaveado por `vinculo:<processo_digits>` em vez de `<processo>:
    <ato>`."""
    chave_proc = _normalizar_num_processo(processo)
    if not chave_proc:
        raise ValueError(f"processo vazio/inválido: {processo!r}")
    chave = _chave_vinculo(chave_proc)
    with _LOCK:
        raw = _ler_raw()
        atual = dict(raw.get(chave) or {})
        atual.update(campos)
        atual["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        raw[chave] = atual
        _salvar_raw(raw)
    return atual


def definir_vinculo_manual(processo: str, paj: str) -> dict:
    """Vincula manualmente um PAJ a este processo — usado quando o lembrete
    do e-proc não trouxe PAJ reconhecido (ou trouxe um errado). Valida o
    formato antes de gravar (mesmo formato de `services.paj_service.PAJ_NORM_REGEX`,
    aceitando também 'AAAA/UUU-NNNNN'). Vale pra TODOS os atos deste
    processo (vínculo é por processo, não por ato)."""
    paj_norm = paj_para_norm(paj)
    if not paj_norm:
        return {"ok": False, "erro": f"Formato de PAJ inválido: {paj!r} "
                                      "(use AAAA/UUU-NNNNN ou PAJ-AAAA-UUU-NNNNN)."}
    salvar_vinculo(processo, paj=paj.strip(), paj_norm=paj_norm, vinculo_status="manual")
    return {"ok": True, "caso": obter_caso(processo)}


def descartar_caso(processo: str, ato: str) -> dict:
    """Marca o ATO como descartado — NÃO apaga o histórico (resumo/confronto/
    minuta permanecem no registro local), só tira da fila de pendentes
    ("Analisar todos os pendentes" ignora status_geral="descartado"). Não
    toca no e-proc nem no SISDPU. Descartar um ato NÃO afeta outros atos
    (autônomos) do mesmo processo."""
    chave_caso = f"{_normalizar_num_processo(processo)}:{ato}"
    registro = salvar_estado(chave_caso, status_geral="descartado")
    return {"ok": True, "caso": registro}


# --------------------------------------------------------------------------
# Purga de casos fora da caixa — pedido explícito do usuário (2026-09-16):
# "elimine tudo o que não estiver na caixa do procurador, pois não são mais
# intimações vigentes" + "crie um mecanismo para que essa limpeza seja feita
# sempre que for clicado em 'atualizar', de modo que a atualização sempre
# faça que a página de 'intimações' reflita a página de 'prazos'
# sincronizada". Chamada automaticamente em TODA listagem (`GET /api/
# intimacoes/casos`, ver `routes/intimacoes.py`) — abrir a página ou clicar
# "Atualizar lista" tem o mesmo efeito.
#
# SUBSTITUI a antiga `purgar_casos_resolvidos()` (2026-08-28): aquela só
# apagava casos JÁ REVISADOS (`STATUS_JA_REVISADO`) e ainda reconfirmava ao
# vivo no MCP antes de apagar (`_intimacao_ainda_ativa`, removida — não é
# mais chamada por ninguém). Essa dupla cautela foi exatamente o que deixou
# lixo se acumular sem limite: um caso resetado pra "novo" (ex.: durante um
# teste manual) nunca entrava nesse filtro, e mesmo saindo da caixa de
# Prazos e-proc há SEMANAS, nunca era limpo (achado real 2026-09-16: 26
# registros "novo" órfãos, todos com o mesmo timestamp de um reset manual
# anterior). Pedido explícito do usuário: a sincronização de Prazos e-proc
# já É a fonte da verdade — não interessa mais o `status_geral`, só se o
# processo ainda está na caixa agora.
#
# NUNCA apaga a minuta (.docx/.pdf) já gerada em "Peças IA" do PAJ — é
# produto de trabalho que pode já ter sido usado/revisado pelo Defensor,
# fora do controle desta lista (pedido explícito: "os arquivos de petição/
# despacho gerados não devem ser tocados"). Só apaga: o REGISTRO da análise
# neste índice local (resumo/confronto/evento_referenciado/escopo — tudo
# "dado de análise", sem utilidade depois que a intimação fechou) e os
# DOCUMENTOS BAIXADOS de trabalho (`dados/eproc_docs/<processo>/` — os PDFs/
# HTML/txt que o Claude leu pra produzir a minuta, também sem utilidade
# depois de pronta).
#
# Única guarda de segurança mantida: só roda se o cache de Prazos e-proc já
# foi sincronizado ALGUMA vez (`sincronizado_em` não vazio) — evita zerar a
# lista inteira de Intimações só porque o cache está vazio/inexistente (ex.:
# painel recém-instalado, arquivo corrompido), que é diferente de "sincronizado
# e de fato sem nenhum processo pendente" (esse caso legítimo purga tudo,
# de propósito).
# --------------------------------------------------------------------------

def _apagar_documentos_baixados(processo: str) -> None:
    """Remove a pasta de documentos de TRABALHO baixados do e-proc pra este
    processo (`dados/eproc_docs/<processo>/`) — nunca a minuta gerada em
    'Peças IA' do PAJ, que fica sempre intacta. Idempotente/best-effort: se
    a pasta não existir ou a remoção falhar por qualquer motivo, não
    interrompe a purga dos outros casos."""
    from ingestao import eproc_mcp_client

    pasta = eproc_mcp_client.EPROC_DOCS_DIR / _normalizar_num_processo(processo)
    if pasta.exists():
        with contextlib.suppress(Exception):
            shutil.rmtree(pasta)


async def purgar_casos_resolvidos() -> dict:
    """Apaga TODO registro local (`_intimacoes_analise.json`) cujo processo
    não aparece em NENHUMA seção do cache atual de Prazos e-proc
    (`ingestao.eproc_intimacoes.carregar()`) — independentemente de
    `status_geral` ("novo", "aguardando_revisao", "concluído", "descartado",
    "gravado_lote", tanto faz). Nome mantido (era `purgar_casos_resolvidos`)
    só pra não precisar atualizar o único chamador (`routes/intimacoes.py`)
    — ver o banner acima pra semântica nova completa.

    Devolve `{"verificados": N, "purgados": N, "mantidos_por_seguranca": N}`
    — o 3º campo só é > 0 quando a guarda de segurança (cache nunca
    sincronizado) impede a purga inteira; fora isso, `purgados == verificados`
    sempre (não há mais critério de "confirmar antes")."""
    from ingestao import eproc_intimacoes

    cache = eproc_intimacoes.carregar()
    if not cache.get("sincronizado_em"):
        return {"verificados": 0, "purgados": 0, "mantidos_por_seguranca": 0}

    # processo_digits -> {ato_hash, ...} ATUALMENTE ativos (ver
    # `_agrupar_prazos_por_ato` — mesmo critério de `listar_casos`).
    atos_vivos: dict[str, set[str]] = {}
    vistos_proc: set[str] = set()
    for casos in (cache.get("secoes") or {}).values():
        for c in casos or []:
            chave_proc = _normalizar_num_processo(c.get("processo") or "")
            if not chave_proc or chave_proc in vistos_proc:
                continue
            vistos_proc.add(chave_proc)
            grupos = _agrupar_prazos_por_ato(c.get("prazos") or [])
            atos_vivos[chave_proc] = {_hash_ato(_chave_ato_str(g[0])) for g in grupos}

    raw = _ler_raw()
    a_remover: list[str] = []
    for chave in raw:
        if chave.startswith("vinculo:"):
            proc = chave.split(":", 1)[1]
            if proc not in atos_vivos:
                a_remover.append(chave)
            continue
        if ":" in chave:
            proc, ato = chave.split(":", 1)
        else:
            # registro LEGADO (chave = só o processo, de antes de 2026-09-XX).
            # Se o processo já tem registro no formato novo pra algum ato
            # vivo, o legado é lixo morto (já migrado) — remove; senão, só
            # remove se o processo inteiro saiu da caixa (mesmo critério dos
            # demais).
            proc, ato = chave, None
        if proc not in atos_vivos:
            a_remover.append(chave)
        elif ato is not None and ato not in atos_vivos[proc]:
            a_remover.append(chave)
        elif ato is None and any(f"{proc}:{h}" in raw for h in atos_vivos.get(proc, ())):
            a_remover.append(chave)  # legado já migrado pro formato novo

    if not a_remover:
        return {"verificados": 0, "purgados": 0, "mantidos_por_seguranca": 0}

    # Documentos de trabalho baixados (`dados/eproc_docs/<processo>/`) são
    # POR PROCESSO, não por ato — só apaga quando NENHUM ato dele sobrevive
    # (processo saiu da caixa por completo); um ato fechado com outro(s)
    # ainda pendente(s) no MESMO processo não deve apagar documentos que o(s)
    # outro(s) ainda possa(m) precisar.
    processos_removidos = {
        (chave.split(":", 1)[0] if ":" in chave else chave)
        for chave in a_remover if not chave.startswith("vinculo:")
    }
    for proc in processos_removidos:
        if proc not in atos_vivos:
            _apagar_documentos_baixados(proc)

    with _LOCK:
        raw_atual = _ler_raw()
        for chave in a_remover:
            raw_atual.pop(chave, None)
        _salvar_raw(raw_atual)

    return {"verificados": len(a_remover), "purgados": len(a_remover), "mantidos_por_seguranca": 0}
