"""Servico de sincronizacao da caixa SISDPU.

MODELO (desde 2026-08-28): sincronizacoes de longa duracao (SSE) rodam como
`asyncio.Task` DESACOPLADA de qualquer conexao HTTP — `iniciar_em_segundo_
plano()` so' dispara o trabalho e retorna na hora; quem quiser acompanhar o
log chama `acompanhar()` (gera as linhas ja acumuladas + as que ainda vao
chegar). Isso permite ao usuario navegar pra outra pagina do painel enquanto
uma sincronizacao roda — antes, o processo vivia DENTRO do generator
consumido pela conexao SSE, entao fechar a aba/trocar de pagina cancelava a
sincronizacao no meio (o EventSource fechado derrubava a conexao, Starlette
cancelava o generator, e o `finally` cancelava a task). Ver `_SyncSession`.

Usa asyncio.Lock global para garantir que uma unica sincronizacao roda por vez
(todas compartilham a mesma sessao Playwright — ver ingestao/sisdpu_client.py).

Cancelamento: `solicitar_cancelamento()` sinaliza o worker corrente via um
asyncio.Event. O sincronizador consulta `deve_cancelar()` entre PAJs e aborta
de forma limpa (fechando a sessao Playwright).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

from config import SISDPU_PASSWORD, SISDPU_USERNAME, TIMEOUT_TOTAL
from ingestao import sincronizador
from ingestao.sisdpu_client import CredenciaisInvalidas
from services import watchlist_service
from services.paj_service import invalidar_cache_listagem


def _paj_identificador_para_norm(paj_identificador: str) -> str:
    """'2021/044-00635' ou 'PAJ-2021-044-00635' -> 'PAJ-2021-044-00635'.

    Devolve string vazia se nao bater o formato esperado.
    """
    s = (paj_identificador or "").strip()
    if s.startswith("PAJ-"):
        return s
    # Formato '2021/044-00635' (com / e -)
    if "/" in s:
        return "PAJ-" + s.replace("/", "-")
    return ""


class _SyncSession:
    """Estado de UMA sincronizacao — vive independente de qualquer conexao
    HTTP aberta. `log` acumula TODAS as linhas desde o inicio (pra quem
    conectar atrasado, de outra pagina, ver o historico completo); cada
    observador ativo recebe as linhas novas por uma `asyncio.Queue` propria.
    """

    _proximo_id = 0

    def __init__(self, titulo: str, meta: dict | None = None) -> None:
        _SyncSession._proximo_id += 1
        self.id = _SyncSession._proximo_id
        self.titulo = titulo
        self.meta: dict = meta or {}
        self.log: list[str] = []
        self.done = False
        self.ok: bool | None = None
        self._subscribers: list[asyncio.Queue] = []

    def emitir(self, linha: str) -> None:
        self.log.append(linha)
        for q in list(self._subscribers):
            with contextlib.suppress(Exception):
                q.put_nowait(linha)

    def finalizar(self, ok: bool) -> None:
        self.done = True
        self.ok = ok
        for q in list(self._subscribers):
            with contextlib.suppress(Exception):
                q.put_nowait(None)  # sentinela de fim

    def inscrever(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def cancelar_inscricao(self, q: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)


_sessao: _SyncSession | None = None
_sync_lock = asyncio.Lock()
_cancel_event: asyncio.Event | None = None


def solicitar_cancelamento() -> bool:
    """Sinaliza o worker em execucao pra parar na proxima oportunidade.

    Retorna True se havia uma sync rodando (sinal enviado), False caso contrario.
    """
    global _cancel_event
    if _cancel_event is None or _cancel_event.is_set():
        return False
    _cancel_event.set()
    return True


def deve_cancelar() -> bool:
    """Consultado pelo sincronizador entre PAJs. True => abortar laco."""
    return _cancel_event is not None and _cancel_event.is_set()


def esta_rodando() -> bool:
    return _sync_lock.locked()


def status_atual() -> dict:
    """Snapshot do estado da sincronizacao atual (ou mais recente, se ja
    terminou) — chamado por QUALQUER pagina ao carregar pra decidir se deve
    mostrar o widget flutuante ja aberto e reconectar ao log via
    `acompanhar()`. Nao depende de ter sido esta pagina/aba quem iniciou.

    Retorna {rodando, titulo, meta, log, done, ok}. `rodando=False` e
    `titulo=None` quando nunca houve nenhuma sincronizacao nesta execucao do
    painel."""
    if _sessao is None:
        return {"id": 0, "rodando": False, "titulo": None, "meta": {}, "log": [], "done": True, "ok": None}
    return {
        "id": _sessao.id,
        "rodando": not _sessao.done,
        "titulo": _sessao.titulo,
        "meta": _sessao.meta,
        "log": list(_sessao.log),
        "done": _sessao.done,
        "ok": _sessao.ok,
    }


async def acompanhar() -> AsyncGenerator[str, None]:
    """Observa o log da sincronizacao ATUAL — NAO inicia nada, so' assiste.
    Repete o log ja acumulado primeiro (quem conecta atrasado ve o historico
    inteiro) e depois segue em tempo real ate o fim. Desconectar daqui
    (fechar a aba, navegar pra outra pagina) NAO cancela a sincronizacao —
    e' exatamente essa a diferenca em relacao ao modelo antigo."""
    sessao = _sessao
    if sessao is None:
        return
    for linha in list(sessao.log):
        yield linha
    if sessao.done:
        return
    q = sessao.inscrever()
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            yield item
    finally:
        sessao.cancelar_inscricao(q)


async def iniciar_em_segundo_plano(
    titulo: str,
    coro_factory: Callable[[Callable[[str], None]], Coroutine[Any, Any, Any]],
    meta: dict | None = None,
) -> None:
    """Dispara uma sincronizacao como task independente e retorna na hora
    (nao espera terminar). `coro_factory` e' chamada com a funcao `emit` de
    log e deve devolver a coroutine a executar.

    Levanta RuntimeError se ja houver uma sincronizacao em andamento, ou se
    faltar credenciais SISDPU — o chamador (rota) deve capturar e devolver
    isso como erro HTTP imediato (nada foi iniciado)."""
    global _sessao, _cancel_event

    if _sync_lock.locked():
        raise RuntimeError("já existe uma sincronização em andamento — aguarde terminar")
    if not SISDPU_USERNAME or not SISDPU_PASSWORD:
        raise RuntimeError(
            "credenciais SISDPU ausentes — preencha SISDPU_USERNAME e "
            "SISDPU_PASSWORD no arquivo .env e reinicie o painel"
        )

    await _sync_lock.acquire()
    sessao = _SyncSession(titulo, meta)
    _sessao = sessao
    _cancel_event = asyncio.Event()

    def _emit(linha: str) -> None:
        with contextlib.suppress(Exception):
            sessao.emitir(linha)

    async def _worker() -> None:
        global _cancel_event
        ok = False
        try:
            await asyncio.wait_for(coro_factory(_emit), timeout=TIMEOUT_TOTAL)
            ok = True
        except TimeoutError:
            _emit(f"[ERRO] timeout de {TIMEOUT_TOTAL}s atingido")
        except asyncio.CancelledError:
            _emit("[CANCELADO] sincronização interrompida pelo usuário")
        except CredenciaisInvalidas as e:
            _emit(f"[ERRO] credenciais SISDPU rejeitadas: {e}")
            _emit("[DICA] ajuste SISDPU_USERNAME/SISDPU_PASSWORD no .env e reinicie o painel")
        except Exception as e:
            _emit(f"[ERRO FATAL] {type(e).__name__}: {e}")
        finally:
            sessao.finalizar(ok)
            _cancel_event = None
            _sync_lock.release()
            # Sync mexeu nos metadata.json — invalida o cache de listagem
            # pra que a proxima chamada do dashboard veja o estado atual.
            invalidar_cache_listagem()

    asyncio.create_task(_worker())


async def rodar_sync(
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool | None = None,
) -> None:
    """Inicia a sincronização da caixa inteira em segundo plano.

    `baixar_anexos=False` pula o download/OCR dos anexos (sync rapido).
    `extrair_documentos`: None (padrao) segue `baixar_anexos`; passar True
    com `baixar_anexos=False` da o nivel LEVE (investiga a relacao de
    documentos por seq sem baixar) — ver `sincronizador.rodar()`.
    `rodar_ocr=False` (padrao) baixa os anexos mas pula o OCR simples —
    use "Melhorar OCR" na aba Anexos SISDPU pra texto de qualidade.
    """
    await iniciar_em_segundo_plano(
        "Sincronizar caixa SISDPU",
        lambda emit: sincronizador.rodar(
            emit,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
            extrair_documentos=extrair_documentos,
        ),
        meta={"tipo": "caixa"},
    )


async def rodar_sync_paj(
    paj_identificador: str,
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
    extrair_documentos: bool | None = None,
) -> None:
    """Inicia a sincronização de UM PAJ em segundo plano (teste/refresh pontual).

    `extrair_documentos`: None (padrao) segue `baixar_anexos`; passar True com
    `baixar_anexos=False` da o nivel LEVE (investiga a relacao de documentos
    por seq sem baixar) — mesmas 3 hipoteses do botao "Sincronizar caixa" do
    dashboard, ver `ingestao.sincronizador.rodar()`.

    Reutiliza o mesmo lock global — evita que uma sync da caixa inteira colida
    com uma sync pontual, e vice-versa.

    NAO ha restricao de escopo aqui: sincronizar um PAJ avulso funciona esteja
    ele na caixa de entrada ou nao (concluido, watchlist, incluido a mao). O
    escopo "so' o que esta na caixa" e' exclusivo do botao "Sincronizar caixa"
    do dashboard (`rodar_sync`).

    Despacha pela watchlist so' por eficiencia: PAJs marcados pelo defensor
    ja entram direto pelo campo de pesquisa global do SIS-DPU
    (`rodar_paj_via_busca_global`), pulando a leitura da caixa. Os demais
    entram por `rodar_paj_unico`, que tenta a caixa primeiro (unico caminho
    que traz etiqueta/`em_caixa_atual` frescos) e, se o PAJ nao estiver la,
    cai sozinho na busca global reaproveitando a sessao aberta.
    """
    paj_norm = _paj_identificador_para_norm(paj_identificador)
    na_watchlist = bool(paj_norm) and watchlist_service.is_watched(paj_norm)
    titulo = f"Sincronizar {paj_norm or paj_identificador}"
    meta = {"tipo": "paj", "paj_norm": paj_norm}

    if na_watchlist:
        await iniciar_em_segundo_plano(
            titulo,
            lambda emit: sincronizador.rodar_paj_via_busca_global(
                paj_identificador,
                emit,
                deve_cancelar=deve_cancelar,
                baixar_anexos=baixar_anexos,
                rodar_ocr=rodar_ocr,
                extrair_documentos=baixar_anexos if extrair_documentos is None else extrair_documentos,
            ),
            meta=meta,
        )
        return

    await iniciar_em_segundo_plano(
        titulo,
        lambda emit: sincronizador.rodar_paj_unico(
            paj_identificador,
            emit,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
            extrair_documentos=baixar_anexos if extrair_documentos is None else extrair_documentos,
        ),
        meta=meta,
    )


async def rodar_sync_movimentacoes(paj_identificador: str) -> None:
    """Inicia, em segundo plano, a sincronização SÓ das movimentações de UM
    PAJ — sincronização LEVÍSSIMA (ver services/sync_service.py, glossário no
    topo do módulo, se/quando adicionado): extrai todos os dados do PAJ
    (tabela de movimentações por inteiro — o SISDPU nao expoe consulta de
    delta), mas NÃO abre o popup de anexo de cada movimentação
    (`extrair_documentos=False`) e não baixa nada (`baixar_anexos=False`).
    Usado pelo ícone de atualizar da tabela de movimentações (one-click,
    disponível em qualquer lugar do painel onde a tabela aparece) — precisa
    ser rápido, só pra conferir visualmente se houve alteração recente no
    PAJ; não é o lugar pra investigar documentos por sequencial (isso é
    "sincronização leve" — ex.: rodar_sync_prazos_abertos/rodar_paj_via_
    busca_global chamados de outros fluxos — ou "completa", que também
    baixa os arquivos).

    Reusa o mesmo despacho de `rodar_sync_paj` (caixa -> busca global via
    watchlist ou fallback automatico), so' fixando `baixar_anexos=False,
    extrair_documentos=False, esperas_curtas=True`.
    """
    paj_norm = _paj_identificador_para_norm(paj_identificador)
    na_watchlist = bool(paj_norm) and watchlist_service.is_watched(paj_norm)
    titulo = f"Atualizar movimentações — {paj_norm or paj_identificador}"
    meta = {"tipo": "movimentacoes", "paj_norm": paj_norm}

    if na_watchlist:
        await iniciar_em_segundo_plano(
            titulo,
            lambda emit: sincronizador.rodar_paj_via_busca_global(
                paj_identificador,
                emit,
                deve_cancelar=deve_cancelar,
                baixar_anexos=False,
                extrair_documentos=False,
                esperas_curtas=True,
            ),
            meta=meta,
        )
        return

    await iniciar_em_segundo_plano(
        titulo,
        lambda emit: _leitura_incremental_mcp(sincronizador.rodar_paj_unico(
            paj_identificador,
            emit,
            deve_cancelar=deve_cancelar,
            baixar_anexos=False,
            extrair_documentos=False,
            esperas_curtas=True,
        )),
        meta=meta,
    )


async def _leitura_incremental_mcp(coro):
    """Com a fonte SIS-DPU 2.0, "atualizar movimentações" (ex.: logo depois de
    gravar) lê só as movimentações novas, não o histórico inteiro — cada
    movimentação lida conta no teto diário do MCP. Sem efeito na fonte legada."""
    from ingestao import sisdpu2_fonte
    t = sisdpu2_fonte.MODO.set("paj")
    try:
        return await coro
    finally:
        sisdpu2_fonte.MODO.reset(t)


async def rodar_sync_watchlist(
    baixar_anexos: bool = True,
    rodar_ocr: bool = False,
) -> None:
    """Inicia, em segundo plano, a sincronização de TODOS os PAJs ativos da
    watchlist.

    Cada PAJ entra pela "Pesquisa Rapida" do SIS-DPU — funciona mesmo para
    os ja concluidos (fora da caixa). Reusa o mesmo lock global das demais
    syncs, entao nao colide com sync da caixa ou de PAJ unico.
    """
    await iniciar_em_segundo_plano(
        "Sincronizar watchlist",
        lambda emit: sincronizador.rodar_watchlist(
            emit,
            deve_cancelar=deve_cancelar,
            baixar_anexos=baixar_anexos,
            rodar_ocr=rodar_ocr,
        ),
        meta={"tipo": "watchlist"},
    )


async def rodar_sync_prazos_abertos() -> None:
    """Inicia, em segundo plano, a sincronização LEVE (sem anexos) de todos
    os PAJs com prazo em aberto nas intimações do e-proc (aba Prazos) —
    postura proativa disparada em sequência pelo front-end logo após
    "Sincronizar caixa" terminar.

    Fonte: última sincronização de intimações já salva (não força releitura
    do e-proc). Reusa o mesmo lock global — não colide com outra sync.
    """
    await iniciar_em_segundo_plano(
        "Sincronizar prazos em aberto",
        lambda emit: sincronizador.rodar_prazos_abertos(emit, deve_cancelar=deve_cancelar),
        meta={"tipo": "prazos_abertos"},
    )


async def atualizar_caixa() -> dict:
    """Verifica a caixa de entrada e remove do dashboard os PAJs que saíram
    dela — sem reabrir cada PAJ (bem mais rápido que `rodar_sync`).

    Reusa o lock global das demais syncs (compartilham o browser Playwright).
    Sem SSE — e' uma operacao curta (poucos segundos, 1 pagina) que retorna
    o resultado direto.

    Retorna {ok, total_caixa, removidos, mensagem, log}.
    """
    if _sync_lock.locked():
        return {
            "ok": False,
            "total_caixa": 0,
            "removidos": 0,
            "mensagem": "já existe uma sincronização em andamento — tente novamente em alguns segundos",
            "log": [],
        }
    if not SISDPU_USERNAME or not SISDPU_PASSWORD:
        return {
            "ok": False,
            "total_caixa": 0,
            "removidos": 0,
            "mensagem": "credenciais SISDPU ausentes — preencha SISDPU_USERNAME/SISDPU_PASSWORD no .env",
            "log": [],
        }

    logs: list[str] = []

    async with _sync_lock:
        try:
            resultado = await asyncio.wait_for(
                sincronizador.atualizar_caixa(logs.append),
                timeout=60,
            )
        except TimeoutError:
            resultado = {
                "ok": False,
                "total_caixa": 0,
                "removidos": 0,
                "mensagem": "timeout de 60s atingido ao abrir a caixa",
            }
        except CredenciaisInvalidas as e:
            resultado = {
                "ok": False,
                "total_caixa": 0,
                "removidos": 0,
                "mensagem": f"credenciais SISDPU rejeitadas: {e}",
            }
        except Exception as e:
            resultado = {
                "ok": False,
                "total_caixa": 0,
                "removidos": 0,
                "mensagem": f"erro inesperado: {type(e).__name__}: {e}",
            }
        finally:
            invalidar_cache_listagem()
        resultado["log"] = logs
        return resultado


async def atualizar_tramitacoes_paj(paj_norm: str) -> dict:
    """Atualiza `tramitacoes_caixa` de UM ÚNICO PAJ sob demanda (lê a caixa
    inteira — o SISDPU não expõe consulta por PAJ — mas grava só este).

    Usado pelo botão "Concluir PAJ da Caixa de Entrada" ANTES de decidir entre
    o caminho simples e o modal de escolha múltipla — ver docstring de
    `sincronizador.atualizar_tramitacoes_paj` pro motivo (esse campo nunca é
    tocado pelos caminhos de sync avulso/busca global, só pela sync completa
    da caixa, então pode estar `None`/desatualizado mesmo num PAJ já
    sincronizado há pouco por um desses caminhos).

    Reusa o lock global das demais syncs. Sem SSE — operação curta (poucos
    segundos, 1 página). Retorna {ok, tramitacoes_caixa, mensagem}.
    """
    if _sync_lock.locked():
        return {
            "ok": False, "tramitacoes_caixa": [],
            "mensagem": "já existe uma sincronização em andamento — tente novamente em alguns segundos",
        }
    from ingestao import sisdpu2_fonte

    # Fonte SIS-DPU 2.0: lê só os trâmites deste PAJ pelo MCP — não usa a senha do legado.
    if not sisdpu2_fonte.fonte_mcp_ativa() and (not SISDPU_USERNAME or not SISDPU_PASSWORD):
        return {
            "ok": False, "tramitacoes_caixa": [],
            "mensagem": "credenciais SISDPU ausentes — preencha SISDPU_USERNAME/SISDPU_PASSWORD no .env",
        }

    logs: list[str] = []
    async with _sync_lock:
        try:
            tramitacoes = await asyncio.wait_for(
                sincronizador.atualizar_tramitacoes_paj(paj_norm, logs.append),
                timeout=60,
            )
            return {"ok": True, "tramitacoes_caixa": tramitacoes, "mensagem": ""}
        except TimeoutError:
            return {"ok": False, "tramitacoes_caixa": [], "mensagem": "timeout de 60s atingido ao ler a caixa"}
        except CredenciaisInvalidas as e:
            return {"ok": False, "tramitacoes_caixa": [], "mensagem": f"credenciais SISDPU rejeitadas: {e}"}
        except Exception as e:
            return {"ok": False, "tramitacoes_caixa": [], "mensagem": f"erro inesperado: {type(e).__name__}: {e}"}
        finally:
            invalidar_cache_listagem()


async def buscar_resumo_paj(paj_identificador: str) -> dict:
    """Versao leve da sincronizacao: busca um PAJ via campo de pesquisa do
    SIS-DPU e grava apenas o metadata.json com dados de cabecalho. Sem SSE —
    e' uma operacao curta (~poucos segundos) que retorna o resultado direto.

    Pega o mesmo lock global das outras syncs para nao colidir com elas
    (compartilham o browser Playwright). Se houver sync em andamento, retorna
    `ok=False` para nao prender o request.

    Retorna {paj_norm, ok, mensagem}.
    """
    if _sync_lock.locked():
        return {
            "paj_norm": "",
            "ok": False,
            "mensagem": "ja existe uma sincronizacao em andamento — tente novamente em alguns segundos",
        }
    if not SISDPU_USERNAME or not SISDPU_PASSWORD:
        return {
            "paj_norm": "",
            "ok": False,
            "mensagem": "credenciais SISDPU ausentes — preencha SISDPU_USERNAME/SISDPU_PASSWORD no .env",
        }

    async with _sync_lock:
        try:
            resultado = await asyncio.wait_for(
                sincronizador.buscar_resumo_via_busca(paj_identificador),
                timeout=60,
            )
        except TimeoutError:
            resultado = {
                "paj_norm": "",
                "ok": False,
                "mensagem": "timeout de 60s atingido na consulta ao SIS-DPU",
            }
        except CredenciaisInvalidas as e:
            resultado = {
                "paj_norm": "",
                "ok": False,
                "mensagem": f"credenciais SIS-DPU rejeitadas: {e}",
            }
        except Exception as e:
            resultado = {
                "paj_norm": "",
                "ok": False,
                "mensagem": f"erro inesperado: {type(e).__name__}: {e}",
            }
        finally:
            invalidar_cache_listagem()
        return resultado


async def rodar_sync_anexos_desde_data(
    paj_identificador: str,
    data_inicio,
    rodar_ocr: bool = False,
) -> None:
    """Inicia, em segundo plano, o download de anexos de um PAJ a partir de
    uma data. `data_inicio` deve ser `datetime.date`.

    Reusa o lock global pra nao colidir com outras sincronizacoes em curso.
    """
    paj_norm = _paj_identificador_para_norm(paj_identificador)
    await iniciar_em_segundo_plano(
        f"Completar anexos — {paj_norm or paj_identificador}",
        lambda emit: sincronizador.rodar_anexos_desde_data(
            paj_identificador,
            data_inicio,
            emit,
            deve_cancelar=deve_cancelar,
            rodar_ocr=rodar_ocr,
        ),
        meta={"tipo": "anexos_desde_data", "paj_norm": paj_norm},
    )


async def rodar_sync_baixar_documentos(
    paj_norm: str,
    ids: list[str],
) -> None:
    """Inicia, em segundo plano, o download de documentos ESPECIFICOS (por
    ID) de um PAJ ja sincronizado.

    Bem mais leve que as demais syncs: le as URLs direto do metadata.json
    existente, sem reabrir a caixa nem reextrair movimentacoes/documentos.
    Reusa o lock global pra nao colidir com outras sincronizacoes em curso.
    """
    await iniciar_em_segundo_plano(
        f"Baixar documento(s) — {paj_norm}",
        lambda emit: sincronizador.rodar_baixar_documentos(
            paj_norm,
            ids,
            emit,
            deve_cancelar=deve_cancelar,
        ),
        meta={"tipo": "baixar_documentos", "paj_norm": paj_norm},
    )


async def rodar_sync_documentos_seq(paj_identificador: str, seq: str) -> None:
    """Inicia, em segundo plano, a extração da relação de documentos de UM
    seq específico de um PAJ — sem baixar nada, sem tocar nas demais
    movimentações. Ver `sincronizador.rodar_documentos_seq` (minimenu do
    clipe de anexo na coluna Documentos)."""
    paj_norm = _paj_identificador_para_norm(paj_identificador)
    await iniciar_em_segundo_plano(
        f"Obter documentos — {paj_norm or paj_identificador} (seq {seq})",
        lambda emit: sincronizador.rodar_documentos_seq(
            paj_identificador,
            seq,
            emit,
            deve_cancelar=deve_cancelar,
        ),
        meta={"tipo": "documentos_seq", "paj_norm": paj_norm, "seq": seq},
    )


async def rodar_sync_baixar_documentos_seq(paj_norm: str, seq: str) -> None:
    """Inicia, em segundo plano, o download dos documentos de UM seq
    específico — obtém a relação primeiro se ainda não for conhecida, e já
    encadeia para o download. Ver `sincronizador.rodar_baixar_documentos_seq`
    (minimenu do clipe de anexo na coluna Documentos)."""
    await iniciar_em_segundo_plano(
        f"Baixar documentos — {paj_norm} (seq {seq})",
        lambda emit: sincronizador.rodar_baixar_documentos_seq(
            paj_norm,
            seq,
            emit,
            deve_cancelar=deve_cancelar,
        ),
        meta={"tipo": "baixar_documentos_seq", "paj_norm": paj_norm, "seq": seq},
    )
