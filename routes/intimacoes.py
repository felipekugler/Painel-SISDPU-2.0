"""Aba "Intimações" — Triagem Inteligente de Intimações.

Cruza as intimações já sincronizadas pelo Painel do Procurador (aba "Prazos
e-proc") com o PAJ vinculado, resume a intimação, confronta com o estágio do
PAJ e classifica o caso em 1 de 4 rótulos, gerando minuta (petição ou
despacho) quando cabível — ver `services.intimacao_ia_service`.

Disparo SOMENTE sob demanda (botão por caso + "Analisar todos os pendentes"),
nunca atrelado à sincronização da aba Prazos.

⚠️ Este módulo é diagnóstico/preparo — nunca protocola no e-proc nem
movimenta/tramita/conclui no SISDPU por conta própria — EXCETO pela rota
`/{processo}/dar-mera-ciencia` (2026-08-29): dispara, para UM único processo
por vez e só depois de revisão explícita do Defensor (confirm() no front +
descrição da movimentação já editada), as 3 ações reais e sempre iguais que
fecham uma intimação de mera ciência — ver `services.mera_ciencia_service`.
Fora essa exceção, a execução continua exclusiva dos consoles "Ações no
e-proc"/"Ações no SISDPU" já existentes, acionados manualmente pelo Defensor
a partir da página do PAJ (link "Abrir PAJ" nesta tela é só um atalho de
navegação, nunca uma execução direta).

Os dois endpoints de análise usam SSE (`EventSourceResponse`) via GET — não
POST — porque `EventSource` (usado no front) só suporta GET; mesmo padrão já
adotado por `/api/acoes-sisdpu/executar` e `/api/retornos/sincronizar`."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from services import intimacao_ia_service, intimacao_service, mera_ciencia_service

router = APIRouter()


@router.get("/intimacoes", response_class=HTMLResponse)
async def pagina_intimacoes(request: Request):
    template = request.app.state.jinja.get_template("intimacoes.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    return HTMLResponse(template.render(request=request, ambiente=ambiente))


@router.get("/api/intimacoes/casos", response_class=JSONResponse)
async def api_listar_casos():
    """Além de listar, dispara a purga de TUDO que não está mais na caixa de
    Prazos e-proc sincronizada (`intimacao_service.purgar_casos_resolvidos`,
    semântica trocada em 2026-09-16 — ver docstring lá: agora ignora
    `status_geral`, espelha a caixa sempre) — só o registro de análise +
    documentos baixados de trabalho, NUNCA a minuta em "Peças IA". Gatilho
    MANUAL — só quando o Defensor clica "Atualizar lista" nesta página — e
    não mais automático a cada sincronização de Prazos e-proc: essa
    automação foi desativada (2026-08-28) por ter disparado, sozinha e em
    lote, um bloqueio real do antivírus corporativo (FortiClient: exclusão
    recursiva de pastas por um processo de rede foi lida como possível
    ransomware). Best-effort: falha na purga não pode impedir a listagem
    normal. Também tenta a busca secundária de vínculo de PAJ pra processos
    sem nenhum PAJ reconhecido (`intimacao_service.tentar_vincular_pajs_
    pendentes` — pedido do usuário 2026-09-25), mesmo espírito best-effort:
    só lê arquivos locais, mas uma falha aqui também não pode impedir a
    listagem."""
    try:
        await intimacao_service.purgar_casos_resolvidos()
    except Exception:
        pass
    try:
        intimacao_service.tentar_vincular_pajs_pendentes()
    except Exception:
        pass
    return JSONResponse({"ok": True, "itens": intimacao_service.listar_casos()})


@router.get("/api/intimacoes/casos/count", response_class=JSONResponse)
async def api_casos_count():
    """Contagem pro badge do submenu "Intimações" — mesmo total exibido no
    cabeçalho da própria página quando nenhum filtro de tipo está aplicado.

    Chama `listar_casos()` DIRETO — de propósito, sem passar por
    `purgar_casos_resolvidos()` (ver docstring de `api_listar_casos` acima):
    esse endpoint é consultado a cada N segundos pela sidebar, e a purga é um
    efeito colateral real (exclusão em lote de pastas de documentos) que já
    causou incidente com antivírus corporativo — não deve virar automático
    de novo por um polling de badge."""
    return {"count": len(intimacao_service.listar_casos())}


@router.post("/api/intimacoes/{processo}/vincular-paj", response_class=JSONResponse)
async def api_vincular_paj(processo: str, request: Request):
    body = await request.json()
    paj = (body.get("paj") or "").strip()
    if not paj:
        return JSONResponse({"ok": False, "erro": "Informe o PAJ."}, status_code=400)
    res = intimacao_service.definir_vinculo_manual(processo, paj)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/intimacoes/{processo}/descartar", response_class=JSONResponse)
async def api_descartar(processo: str, ato: str):
    """`ato` identifica o SUB-CASO (ver módulo `intimacao_service` — mudança
    2026-09-XX: descartar afeta só ESTE ato, nunca outro ato autônomo do
    mesmo processo)."""
    return JSONResponse(intimacao_service.descartar_caso(processo, ato))


@router.get("/api/intimacoes/{processo}/atos", response_class=JSONResponse)
async def api_atos_do_processo(processo: str):
    """Todos os atos (1 por `chave_caso`, ver `intimacao_service.listar_casos`)
    de UM processo, cada um com seus `prazos` crus (evento/data_envio/
    prazo_dias) e o vínculo de PAJ — usado por telas que já sabem de um
    prazo cru (ex. "Intimações pendentes" da página do PAJ,
    `static/js/processo_judicial.js`) e precisam descobrir a qual
    `chave_caso`/`ato` ele corresponde, sem reimplementar `_agrupar_prazos_
    por_ato`/`_hash_ato` no front: o chamador casa localmente por
    (evento, data_envio, prazo_dias)."""
    itens = intimacao_service.casos_do_processo(processo)
    casos = [{
        "chave_caso": item["chave_caso"], "ato": item["chave_caso"].split(":", 1)[-1],
        "paj_norm": item.get("paj_norm") or "", "vinculo_status": item.get("vinculo_status") or "",
        "prazos": [
            {"evento": p.get("evento", ""), "data_envio": p.get("data_envio", ""),
             "prazo_dias": p.get("prazo_dias", "")}
            for p in item.get("prazos") or []
        ],
    } for item in itens]
    return JSONResponse({"ok": True, "casos": casos})


@router.get("/api/intimacoes/{processo}/evento-referenciado", response_class=JSONResponse)
async def api_evento_referenciado(processo: str, ato: str):
    """Devolve o(s) evento(s)/ato(s) do processo judicial referenciado(s)
    por ESTE ATO (`ato` — ver módulo `intimacao_service`) — SEM nenhuma
    chamada de IA (só MCP, leitura) — pedido explícito do usuário
    (2026-08-29): "esse ato referenciado deve estar disponível direto,
    independentemente de qualquer análise". Mesmo dado (documentos do
    evento, já baixados ou não) que a página do PAJ já mostra em
    "Intimações pendentes" (`templates/partials/processo_judicial.html` +
    `static/js/processo_judicial.js`) — aqui pra popular o mesmo badge na
    listagem de Intimações, ANTES de rodar "Analisar".

    CACHEADO EM DISCO por ato (`ato_referenciado_cache`, 2026-09-25 — pedido
    explícito do usuário: "a intimação é estática, não faria sentido
    descartar essa informação pra ter que reler de novo num acesso futuro
    da página"): a identidade de um ato referenciado (qual evento, qual
    parte) não muda enquanto o ato existir — abrir a Triagem de Intimações
    toda vez disparava até 40 consultas ao vivo ao e-proc (uma por linha,
    com um teto de 6 simultâneas pra não sobrecarregar o site deles), o que
    tornava o carregamento perceptivelmente lento a cada visita. Validade do
    cache: até o registro deste ATO ser apagado — o que só acontece quando
    ele sai da caixa de Prazos e-proc sincronizada (`purgar_casos_
    resolvidos`, chamada em toda listagem) OU quando alguma ferramenta do
    painel realmente cumpre o prazo e o ato deixa de existir — nenhuma das
    duas precisa de lógica de expiração própria aqui, o ciclo de vida do
    registro já resolve isso sozinho. NUNCA cacheia um resultado `None`
    (casamento não encontrado) — pode ser falha TRANSITÓRIA (cofre
    trancado, e-proc fora do ar), não um fato permanente sobre o ato; nesse
    caso tenta de novo ao vivo na próxima vez, sempre."""
    from services import intimacao_ia_service
    caso = intimacao_service.obter_caso(processo, ato)
    if not caso:
        return JSONResponse({"ok": False, "erro": "Caso não encontrado."}, status_code=404)

    if "ato_referenciado_cache" in caso:
        return JSONResponse({"ok": True, "dados": caso["ato_referenciado_cache"]})

    dados = await intimacao_ia_service.obter_evento_referenciado_leve(processo, caso.get("prazos") or [])
    if dados is not None:
        intimacao_service.salvar_estado(caso["chave_caso"], ato_referenciado_cache=dados)
    return JSONResponse({"ok": True, "dados": dados})


@router.get("/api/intimacoes/{processo}/eventos-do-ato", response_class=JSONResponse)
async def api_eventos_do_ato(processo: str, ato: str):
    """Todos os eventos de intimação (candidatos ao vivo, já filtrados por
    identidade — ver `intimacao_ia_service._candidatos_do_ato`) que casam
    com ESTE ato, cada um com um rótulo pronto — alimenta o checklist
    "Selecione o(s) prazo(s) a ser(em) fechado(s)" do modal de "Dar mera
    ciência" na página do PAJ (pedido do usuário 2026-09-24, mesmo estilo já
    usado em "Ações no Eproc"), deixando o Defensor escolher quais eventos
    fechar quando há litisconsórcio (mais de um candidato no mesmo ato)."""
    from services import intimacao_ia_service
    caso = intimacao_service.obter_caso(processo, ato)
    if not caso:
        return JSONResponse({"ok": False, "erro": "Caso não encontrado."}, status_code=404)
    dados = await intimacao_ia_service.listar_eventos_do_ato(processo, caso.get("prazos") or [])
    return JSONResponse({"ok": True, **dados})


# Trava simples contra 2 análises CONCORRENTES do MESMO ato — bug real
# encontrado durante teste manual (2026-08-27): disparar "/analisar" mais de
# uma vez pro mesmo ato (reclique acidental no botão, ou reteste manual
# como neste caso) fazia 2 pipelines rodarem em paralelo se pisando um no
# outro (o último `salvar_estado` a terminar vencia, produzindo um estado
# final inconsistente entre `resumo_intimacao`/`escopo_verificacao`/
# `confronto` — cada campo podia vir de uma rodada diferente). Chave =
# `intimacao_service.chave_caso(processo, ato)` ("<processo_digits>:<ato>"),
# reaproveitada de lá (nunca reimplementada aqui) — 2 atos AUTÔNOMOS do
# MESMO processo têm chaves diferentes e podem rodar em paralelo sem
# problema, só o MESMO ato duas vezes é que é recusado.
#
# Continua existindo mesmo com `_JOBS` abaixo: cobre o caso de um ato estar
# sendo trabalhado DENTRO de uma fila (`_worker_fila`), que não tem um `_Job`
# individual próprio — só serve pra recusar um "/analisar" avulso nesse
# meio-tempo, não pra acompanhar nada ao vivo.
_EM_ANDAMENTO: set[str] = set()


class _Job:
    """Uma análise (individual ou fila) rodando DESACOPLADA de qualquer
    conexão SSE específica — pedido explícito do usuário (2026-08-29):
    "torna isso à prova de navegação". A tarefa (`task`) fica guardada AQUI,
    num registro global (`_JOBS`), nunca dentro do gerador que serve uma
    conexão SSE puntual — fechar a aba, navegar pra outra página do painel,
    ou simplesmente perder a conexão NUNCA cancela a análise: ela só para de
    ser ACOMPANHADA ao vivo por aquela conexão específica, e continua
    rodando/gravando o resultado de qualquer forma.

    `log` guarda TODO o histórico de eventos já emitidos — quem conectar
    (ou reconectar) recebe esse histórico primeiro (replay), depois passa a
    receber ao vivo junto dos demais assinantes. Reconectar no MESMO job
    (ex.: clicar "Analisar" de novo no mesmo processo enquanto ainda roda,
    ou voltar pra página no meio de uma fila) reata na mesma transmissão em
    vez de recusar ou duplicar o trabalho."""

    __slots__ = ("chave", "processo", "ato", "log", "done", "resultado", "subscribers", "task")

    def __init__(self, chave: str, processo: str | None = None, ato: str | None = None):
        self.chave = chave
        self.processo = processo  # nº ORIGINAL (com máscara) — só p/ job individual; None na fila
        self.ato = ato  # identifica o sub-caso (ver intimacao_service) — só p/ job individual
        self.log: list[dict] = []
        self.done = False
        self.resultado: dict | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None

    def emitir(self, evento: dict) -> None:
        self.log.append(evento)
        for fila in list(self.subscribers):
            fila.put_nowait(evento)

    def concluir(self, resultado: dict) -> None:
        self.done = True
        self.resultado = resultado
        self.emitir({"__done__": True, "resultado": resultado})


# Jobs "acompanháveis" via SSE em andamento — chave = processo (individual)
# ou `_FILA_JOB_KEY` (fila "todos"/"selecionados", só 1 de cada vez). Um job
# se remove SOZINHO daqui ao concluir (ver `_Job.concluir` + `.pop()` logo
# depois, nos workers abaixo) — não sobrevive a um restart do painel (só em
# memória, como sempre), mas isso é uma limitação conhecida e aceita
# separada (reiniciar o processo DERRUBA de fato qualquer subprocesso em
# andamento) — o que este mecanismo resolve é só o lado do NAVEGADOR.
_JOBS: dict[str, _Job] = {}
_FILA_JOB_KEY = "__fila__"


async def _tail_job(job: _Job):
    """Gera os eventos SSE pra ACOMPANHAR um job já em andamento (ou recém-
    concluído) — nunca cria nem cancela `job.task`, só assina/desassina essa
    conexão específica na lista de assinantes. Reproduz (`replay`) o
    histórico já emitido antes de começar a receber eventos novos, pra quem
    conecta no meio (ou reconecta depois de ter saído) ver o quadro
    completo, não só o que aconteceu dali pra frente."""
    fila: asyncio.Queue = asyncio.Queue()
    for evento in job.log:
        fila.put_nowait(evento)
    if job.done:
        fila.put_nowait({"__done__": True, "resultado": job.resultado})
    else:
        job.subscribers.add(fila)
    try:
        while True:
            item = await fila.get()
            if item.get("__done__"):
                yield {"event": "done", "data": json.dumps(item.get("resultado", {}), ensure_ascii=False)}
                return
            yield {"event": "log", "data": json.dumps(item, ensure_ascii=False)}
    finally:
        # NUNCA `job.task.cancel()` aqui — essa é a diferença-chave do
        # design antigo (gerador dono da task): perder esta conexão só para
        # de ACOMPANHAR, a análise em si segue intacta.
        job.subscribers.discard(fila)


async def _worker_individual(job: _Job, processo: str, ato: str) -> None:
    chave = job.chave

    def log_cb(level: str, msg: str) -> None:
        job.emitir({"level": level, "msg": msg})

    try:
        resultado = await intimacao_ia_service.rodar_pipeline(processo, ato, log_cb)
    except Exception as e:  # noqa: BLE001 — nunca deixa o job sem "done"
        resultado = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
        log_cb("erro", f"Falha inesperada: {e}")
    finally:
        _EM_ANDAMENTO.discard(chave)
        job.concluir(resultado)
        _JOBS.pop(chave, None)


@router.get("/api/intimacoes/{processo}/analisar")
async def api_analisar(processo: str, ato: str):
    """SSE: roda o pipeline completo (resumo -> confronto -> minuta, se
    aplicável) para este ATO (`ato` — identifica o sub-caso, ver módulo
    `intimacao_service`; um processo com atos autônomos tem 1 pipeline por
    ato, independentes) — a análise em si roda DESACOPLADA desta conexão
    (`_Job`/`_JOBS`, ver lá): navegar pra outra página do painel ou
    perder a conexão nunca cancela nada. Uma 2ª chamada pro MESMO ato
    enquanto a 1ª ainda roda REATA na mesma transmissão ao vivo (com replay
    do que já aconteceu) em vez de recusar ou duplicar; só recusa se o
    ato estiver ocupado por uma FILA em andamento (sem job individual
    próprio pra reatar)."""
    chave = intimacao_service.chave_caso(processo, ato)
    job = _JOBS.get(chave)
    if job is not None:
        return EventSourceResponse(_tail_job(job))
    if chave in _EM_ANDAMENTO:
        async def _ocupado_por_fila():
            yield {"event": "log", "data": json.dumps(
                {"level": "erro", "msg": f"Já há uma análise em andamento para {processo} (dentro de "
                                         "uma fila) — aguarde terminar antes de disparar de novo."},
                ensure_ascii=False)}
            yield {"event": "done", "data": json.dumps(
                {"ok": False, "erro": "Análise já em andamento para este processo."}, ensure_ascii=False)}
        return EventSourceResponse(_ocupado_por_fila())

    _EM_ANDAMENTO.add(chave)
    job = _Job(chave, processo=processo, ato=ato)
    _JOBS[chave] = job
    job.task = asyncio.create_task(_worker_individual(job, processo, ato))
    return EventSourceResponse(_tail_job(job))


# Namespace de chave PRÓPRIO ("mc:" + chave_caso). Sem isso, um `_JOBS.get(chave)`
# batendo pro MESMO ato reataria (por engano) na análise em vez de em "Dar
# mera ciência" (ou vice-versa) — são disparos INDEPENDENTES, cada um com
# seu próprio job, mesmo quando é o mesmo ato.
def _chave_mera_ciencia(processo: str, ato: str) -> str:
    return "mc:" + intimacao_service.chave_caso(processo, ato)


async def _worker_mera_ciencia(job: _Job, processo: str, ato: str, descricao_mov: str,
                                eventos_selecionados: list[str] | None,
                                gerenciamento_tipo: str | None, gerenciamento_procurador_valor: str | None,
                                gerenciamento_procurador_rotulo: str | None, gerenciar: bool = True) -> None:
    chave = job.chave

    def log_cb(level: str, msg: str) -> None:
        job.emitir({"level": level, "msg": msg})

    try:
        resultado = await mera_ciencia_service.dar_mera_ciencia(
            processo, ato, descricao_mov, log_cb, eventos_selecionados=eventos_selecionados,
            gerenciamento_tipo=gerenciamento_tipo,
            gerenciamento_procurador_valor=gerenciamento_procurador_valor,
            gerenciamento_procurador_rotulo=gerenciamento_procurador_rotulo, gerenciar=gerenciar)
    except Exception as e:  # noqa: BLE001 — nunca deixa o job sem "done"
        resultado = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
        log_cb("erro", f"Falha inesperada: {e}")
    finally:
        job.concluir(resultado)
        _JOBS.pop(chave, None)


@router.get("/api/intimacoes/{processo}/dar-mera-ciencia")
async def api_dar_mera_ciencia(processo: str, ato: str, descricao: str = "", eventos: str = "",
                                gerenciamento_tipo: str = "", gerenciamento_procurador_valor: str = "",
                                gerenciamento_procurador_rotulo: str = "", gerenciar: str = "1"):
    """SSE — IRREVERSÍVEL: dispara, para ESTE ATO (`ato` — identifica o
    sub-caso, ver módulo `intimacao_service`), as 3 ações reais e sempre
    iguais que fecham uma intimação de mera ciência (ciência com renúncia ao
    prazo no e-proc + Gerenciamento de Processo + movimentação no SISDPU) —
    ver `services.mera_ciencia_service`. Fecha SÓ este ato — outro ato
    autônomo do mesmo processo continua intocado. `descricao` é o texto da
    movimentação já revisado/editado pelo Defensor na caixa de texto ao lado
    do botão, ANTES do clique (o front também exige um `confirm()` explícito
    antes de chamar esta rota — ver `static/js/intimacoes.js:darMeraCiencia`).

    `eventos` (opcional, pedido do usuário 2026-09-24): nºs de evento
    separados por vírgula, pra restringir QUAIS eventos fechar quando o ato
    tem litisconsórcio — ver `services.mera_ciencia_service.dar_mera_
    ciencia`'s `eventos_selecionados`. Vazio (padrão, usado pela página
    Intimações) fecha TODOS os eventos identificados automaticamente.

    `gerenciamento_tipo`/`gerenciamento_procurador_valor`/`_rotulo` (opcional,
    pedido do usuário 2026-09-24): mesmos dropdowns "Gerenciar processo
    depois de peticionar (substabelecer)?" de "Ações no Eproc", pré-
    preenchidos com o padrão configurado mas editáveis no modal — sobrepõem
    o tipo/procurador padrão quando informados. Vazio usa o comportamento de
    sempre (Substituir procurador + padrão de Configurações › e-proc).

    Mesmo padrão de job desacoplado (`_Job`/`_JOBS`) do "/analisar": navegar
    pra outra página nunca cancela a execução em andamento; uma 2ª chamada
    pro MESMO ato REATA na mesma transmissão em vez de disparar duas
    vezes (todos os parâmetros são ignorados nesse caso — só valem na 1ª
    chamada, que cria o job)."""
    chave = _chave_mera_ciencia(processo, ato)
    job = _JOBS.get(chave)
    if job is not None:
        return EventSourceResponse(_tail_job(job))
    eventos_selecionados = [e.strip() for e in eventos.split(",") if e.strip()] or None
    # `gerenciamento_tipo` presente = o front mandou o bloco INTEIRO de
    # override (mesmo quando o procurador vem vazio de propósito — tipo "D"
    # não tem destino). Só nesse caso repassa `gerenciamento_procurador_
    # valor` como string (mesmo vazia) — nunca convertido pra `None`, senão
    # `dar_mera_ciencia` cairia de volta no padrão configurado achando que
    # não houve override nenhum.
    tipo_override = gerenciamento_tipo or None
    valor_override = gerenciamento_procurador_valor if tipo_override else None
    rotulo_override = (gerenciamento_procurador_rotulo or None) if tipo_override else None
    job = _Job(chave, processo=processo, ato=ato)
    _JOBS[chave] = job
    job.task = asyncio.create_task(
        _worker_mera_ciencia(job, processo, ato, descricao, eventos_selecionados,
                              tipo_override, valor_override, rotulo_override,
                              gerenciar=gerenciar not in ("0", "false", "nao")))
    return EventSourceResponse(_tail_job(job))


@router.get("/api/intimacoes/jobs-ativos", response_class=JSONResponse)
async def api_jobs_ativos():
    """Lista os jobs (análise individual, fila, ou "Dar mera ciência") em
    andamento AGORA — usado pelo front pra REATAR sozinho ao carregar/voltar
    pra esta página (ex.: depois de navegar pra outra aba do painel e
    voltar), mostrando de novo o card de acompanhamento sem precisar clicar
    em nada — pedido explícito do usuário (2026-08-29): "torna isso à prova
    de navegação". Só lista o que ainda está rodando (`not job.done`) — um
    job concluído já se removeu de `_JOBS` sozinho (ver `_worker_individual`/
    `_worker_fila`/`_worker_mera_ciencia`)."""
    itens = []
    for chave, job in _JOBS.items():
        if job.done:
            continue
        if chave == _FILA_JOB_KEY:
            itens.append({"tipo": "fila"})
        elif chave.startswith("mc:"):
            itens.append({"tipo": "mera_ciencia", "processo": job.processo or chave, "ato": job.ato})
        else:
            itens.append({"tipo": "individual", "processo": job.processo or chave, "ato": job.ato})
    return JSONResponse({"ok": True, "jobs": itens})


async def _worker_fila(job: _Job, casos: list[dict], avisos: list[str] | None) -> None:
    """Roda o pipeline em fila SEQUENCIAL (nunca paralelo — respeita o rate
    limit do SISDPU/e-proc já estabelecido no projeto) para a lista de casos
    dada. Reaproveitado tanto por "Analisar todos os pendentes" quanto por
    "Analisar selecionados" (checkboxes) — só muda como a lista é montada."""
    def log_cb(level: str, msg: str) -> None:
        job.emitir({"level": level, "msg": msg})

    for msg in (avisos or []):
        log_cb("muted", msg)

    total = len(casos)
    if not total:
        log_cb("muted", "Nenhum caso para analisar.")
        job.concluir({"ok": True, "processados": 0})
        _JOBS.pop(job.chave, None)
        return

    processados = 0
    for i, c in enumerate(casos, start=1):
        processo = c["processo"]
        chave = c["chave_caso"]
        ato = chave.split(":", 1)[-1]
        if chave in _EM_ANDAMENTO:
            log_cb("muted", f"[{i}/{total}] {processo}: já está sendo analisado em outra "
                            "chamada — pulado nesta fila.")
            continue
        _EM_ANDAMENTO.add(chave)
        log_cb("novo", f"[{i}/{total}] Analisando {processo}…")
        try:
            await intimacao_ia_service.rodar_pipeline(processo, ato, log_cb)
        except Exception as e:  # noqa: BLE001
            log_cb("erro", f"[{i}/{total}] Falha inesperada em {processo}: {e}")
        finally:
            _EM_ANDAMENTO.discard(chave)
        processados += 1
    job.concluir({"ok": True, "processados": processados})
    _JOBS.pop(job.chave, None)


def _iniciar_ou_reatar_fila(casos: list[dict], avisos: list[str] | None = None) -> _Job:
    """Só 1 fila "todos"/"selecionados" por vez — uma 2ª chamada enquanto a
    1ª ainda roda REATA na mesma transmissão (mesmo espírito do "/analisar"
    individual acima), em vez de tentar montar uma 2ª fila concorrente com a
    primeira. Nota: se a 2ª chamada pretendia uma seleção DIFERENTE da 1ª,
    ela é ignorada enquanto a fila em andamento não terminar — cenário raro
    (o botão já fica desabilitado no front enquanto uma fila roda; só
    aconteceria num recarregamento de página no meio do processamento)."""
    job = _JOBS.get(_FILA_JOB_KEY)
    if job is not None:
        return job
    job = _Job(_FILA_JOB_KEY)
    _JOBS[_FILA_JOB_KEY] = job
    job.task = asyncio.create_task(_worker_fila(job, casos, avisos))
    return job


@router.get("/api/intimacoes/analisar-todos")
async def api_analisar_todos():
    """SSE: roda o pipeline em fila SEQUENCIAL (nunca paralelo) para todos os
    casos com vínculo de PAJ resolvido (auto ou manual) e status "novo" ou
    "erro" — pula descartados, já concluídos e ainda pendentes de vínculo."""
    casos = [
        c for c in intimacao_service.listar_casos()
        if c.get("vinculo_status") != "pendente" and c.get("status_geral") in ("novo", "erro")
    ]
    job = _iniciar_ou_reatar_fila(casos)
    return EventSourceResponse(_tail_job(job))


@router.get("/api/intimacoes/analisar-selecionados")
async def api_analisar_selecionados(casos_sel: str = ""):
    """SSE: roda o pipeline em fila SEQUENCIAL só para os ATOS escolhidos
    manualmente pelo Defensor (checkboxes da tabela — `casos_sel` é uma
    lista de `chave_caso` separada por vírgula; NUNCA número de processo —
    um processo pode ter mais de um ato/item selecionável, ver módulo
    `intimacao_service`). Diferente de "Analisar todos": aqui a escolha é
    explícita, então um caso já "aguardando_revisao"/"concluído" TAMBÉM entra
    na fila se foi marcado (reanálise deliberada) — só o vínculo de PAJ
    pendente continua bloqueando (mesma trava do botão "Analisar" individual;
    esses ficam de fora, com aviso no log)."""
    alvos = list(dict.fromkeys(p.strip() for p in casos_sel.split(",") if p.strip()))
    if not alvos:
        async def _vazio():
            yield {"event": "log", "data": json.dumps(
                {"level": "erro", "msg": "Nenhum caso selecionado."}, ensure_ascii=False)}
            yield {"event": "done", "data": json.dumps(
                {"ok": False, "erro": "Nenhum caso selecionado."}, ensure_ascii=False)}
        return EventSourceResponse(_vazio())

    todos = intimacao_service.listar_casos()
    por_chave = {c["chave_caso"]: c for c in todos}
    casos: list[dict] = []
    avisos: list[str] = []
    for chave_sel in alvos:
        c = por_chave.get(chave_sel)
        if not c:
            avisos.append(f"{chave_sel}: não encontrado na lista atual — pulado.")
        elif c.get("vinculo_status") == "pendente":
            avisos.append(f"{c.get('processo')}: sem PAJ vinculado — pulado (vincule manualmente antes).")
        else:
            casos.append(c)
    job = _iniciar_ou_reatar_fila(casos, avisos=avisos)
    return EventSourceResponse(_tail_job(job))


# ⚠️ Esta rota (`GET /api/intimacoes/{processo}`) tem que vir DEPOIS de
# `analisar-todos`/`analisar-selecionados` acima — bug real (2026-08-28):
# FastAPI casa rotas na ORDEM em que são registradas, e `{processo}` (um
# path param genérico) casa com QUALQUER segmento literal, incluindo
# "analisar-todos"/"analisar-selecionados". Com ela ANTES (posição original),
# um GET pra `/api/intimacoes/analisar-selecionados` nunca chegava no handler
# de verdade — caía aqui com `processo="analisar-selecionados"`, devolvia
# `404 Processo não encontrado` (JSON puro, não SSE), e o `EventSource` do
# front, recebendo uma resposta que não é `text/event-stream`, disparava
# `onerror` — exatamente o "erro de conexão com o servidor" relatado pelo
# usuário. "Analisar" individual não tinha esse problema (`/{processo}/
# analisar`, formato de path diferente, não colide). Não mover pra cima de
# novo sem também mover as duas rotas literais.
@router.get("/api/intimacoes/{processo}", response_class=JSONResponse)
async def api_obter_caso(processo: str, ato: str | None = None):
    """`ato` identifica o sub-caso (ver módulo `intimacao_service`) — omitir
    só é seguro quando o chamador já sabe, por outro motivo, que o processo
    tem 1 ato só; o front sempre passa o `ato` do item que está exibindo."""
    caso = intimacao_service.obter_caso(processo, ato)
    if not caso:
        return JSONResponse({"ok": False, "erro": "Caso não encontrado."}, status_code=404)
    return JSONResponse({"ok": True, "caso": caso})
