"""Ação real "Dar mera ciência" — dispara, para UM único processo por vez, as
3 ações reais e sempre iguais que fecham uma intimação já classificada (pela
Triagem Inteligente de Intimações, `services.intimacao_ia_service`) como mera
ciência:
  1. e-proc: petição de "Ciência, com renúncia ao prazo" (fecha o(s) prazo(s)
     desta intimação — e de eventuais coautores em litisconsórcio ativo,
     casados pelo(s) nº(s) do evento de intimação).
  2. e-proc: Gerenciamento de Processo (substabelecimento) — sempre o padrão
     já configurado em Configurações › e-proc (mesmo default do console
     manual "Ações no e-proc").
  3. SISDPU: movimentação (SEM tramitar/concluir) com a fase fixa "Petição
     simples, De mera ciência ou por cota nos autos" e a descrição que o
     Defensor reviu/editou na própria tela ANTES de clicar — nunca gerada e
     disparada sem revisão.

Substitui por completo `services.lote_ciencia_service` (removido, junto de
`routes/lote_ciencia.py`, `templates/lote_ciencia.html` e
`static/js/lote_ciencia.js`) — pedido explícito do usuário (2026-08-29): "não
me senti seguro em fazer em lote". Mesmas 3 ações reais, mesmo evento/fase
fixos, mesma ordem parar-no-primeiro-erro do antigo `confirmar_um` — só muda
o disparo: 1 caso de cada vez, 1 único botão ("Dar mera ciência", com
confirm() no front antes de chamar este módulo), sem tela de revisão
separada nem fila em 2 fases.

⚠️ IRREVERSÍVEL — protocola de verdade na vara e move o PAJ no SISDPU. Ao
contrário de `intimacao_service`/`intimacao_ia_service` (que NUNCA tocam
e-proc/SISDPU de verdade), este módulo existe justamente pra isso.

**Fallback do passo 3 (2026-08-29, caso real 5031818-71.2026.4.04.7000)**:
se o `id` do PAJ no SISDPU não puder ser resolvido (metadata.json sem URL de
detalhamento — PAJ sincronizado há muito tempo, ou nunca teve o
detalhamento salvo), a essa altura os passos 1 e 2 (ciência + Gerenciamento)
JÁ foram protocolados de verdade — não dá mais pra tratar como se nada
tivesse sido feito. Em vez de só devolver um erro sem saída, `dar_mera_ciencia`
grava um RASCUNHO de movimentação pendente (mesmo mecanismo de "Movimentações
pendentes" — `services.acoes_sisdpu_service.salvar_rascunho`) com a fase e a
descrição já prontas, pra o Defensor só abrir "Ações no SISDPU" e clicar
Executar, sem digitar nada de novo. Ainda marca o caso como "erro" (a
execução automática de fato não completou os 3 passos), mas com a
movimentação pronta esperando, não perdida."""

from __future__ import annotations

from datetime import datetime

# Evento e fase SEMPRE os mesmos neste fluxo — nunca escolhidos por caso nem
# pela IA (mesma regra do antigo lote_ciencia_service: "no eproc o evento
# será sempre igual", "no sisdpu a movimentação será sempre igual").
EVENTO_EPROC = "Ciência, com renúncia ao prazo"
FASE_SISDPU = "Petição simples, De mera ciência ou por cota nos autos"
TIPO_GERENCIAMENTO = "S"  # Substituir procurador — mesmo default do console manual


def _id_processo_sisdpu(paj_norm: str) -> str:
    """Mesma extração de `services.paj_service` (linha ~617): o `id` do PAJ no
    SISDPU vem embutido na URL de detalhamento gravada pelo último sync,
    dentro de `sisdpu_raw.url` do metadata.json — duplicado aqui (função
    pequena, mesma convenção de reimplementar em vez de importar uma peça de
    uma função bem maior, já usada entre os módulos deste painel)."""
    import json
    import re

    import config

    meta_path = config.PAJS_DIR / paj_norm / "metadata.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    url = ((metadata.get("sisdpu_raw") or {}).get("url")) or ""
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else ""


async def dar_mera_ciencia(processo: str, ato: str, descricao_mov: str, log_cb,
                            eventos_selecionados: list[str] | None = None,
                            gerenciamento_tipo: str | None = None,
                            gerenciamento_procurador_valor: str | None = None,
                            gerenciamento_procurador_rotulo: str | None = None,
                            gerenciar: bool = True) -> dict:
    """Roda as 3 ações reais em sequência — parando no primeiro erro, sem
    tentar os próximos passos — pro ATO dado (`ato` identifica o sub-caso,
    ver módulo `intimacao_service` — mudança 2026-09-XX: "Dar mera ciência"
    fecha SÓ este ato; um processo com outro ato autônomo ainda pendente
    continua intocado, o Defensor repete a ação nele separadamente).
    `descricao_mov` é o texto já revisado/editado pelo Defensor na tela
    (nunca gerado às cegas aqui).

    `gerenciamento_tipo`/`gerenciamento_procurador_valor` (pedido do usuário
    2026-09-24): quando o Defensor precisa de um tipo/destino diferente do
    padrão configurado — mesmos dropdowns "Gerenciar processo depois de
    peticionar (substabelecer)?" já usados em "Ações no Eproc" — sobrepõem
    `TIPO_GERENCIAMENTO`/o procurador padrão de Configurações › e-proc.
    `None` (padrão, compat com o chamador antigo da página Intimações) usa
    o comportamento de sempre.

    `eventos_selecionados` (pedido do usuário 2026-09-24): quando um ato tem
    litisconsórcio (mais de uma parte associada ao procurador no MESMO
    evento/dia/prazo, ver `intimacao_ia_service._candidatos_do_ato`), o
    Defensor pode desmarcar, no checklist do modal, algum(ns) evento(s) que
    NÃO devem ser fechados agora — `None` (padrão, compat com o chamador
    antigo da página Intimações) fecha TODOS os eventos identificados
    automaticamente; uma lista explícita restringe aos eventos nela
    contidos (nunca aos que faltarem — permite fechar só uma parte do
    litisconsórcio de cada vez, mantendo o restante genuinamente aberto).

    Todo log emitido aqui também fica gravado em `mera_ciencia.log` no
    próprio registro de Intimações (`intimacao_service.salvar_estado`) —
    mesmo espírito do antigo `log_confirmacao` do lote: quem só vê o badge
    "Concluído" na tabela não tinha como saber depois o que de fato rodou
    sem abrir os 3 sistemas um por um."""
    from ingestao import eproc_client
    from services import acoes_eproc_service, acoes_sisdpu_service, config_painel_service, intimacao_service

    chave = intimacao_service.chave_caso(processo, ato)

    descricao_mov = (descricao_mov or "").strip()
    if not descricao_mov:
        msg = "Descrição da movimentação no SISDPU não pode ficar vazia."
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}

    # Fonte SIS-DPU 2.0: descrição curta seria recusada no SISDPU — barra ANTES do
    # e-proc (irreversível), nunca depois.
    from ingestao import sisdpu2_movtram
    if sisdpu2_movtram.ativo():
        aviso = sisdpu2_movtram.aviso_descricao_curta(descricao_mov, False)
        if aviso:
            log_cb("erro", aviso)
            return {"ok": False, "erro": aviso}

    caso = intimacao_service.obter_caso(processo, ato)
    if not caso:
        msg = "Ato não encontrado em Intimações (pode já ter saído da caixa de Prazos e-proc)."
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}
    paj_norm = caso.get("paj_norm") or ""
    if not paj_norm:
        msg = "Caso sem PAJ vinculado — não é possível dar mera ciência."
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}
    # SEMPRE recalcula ao vivo (nunca confia no `evento_referenciado` já
    # persistido de uma análise anterior, mesmo que exista) — mesmo casamento
    # que já popula o badge "Ato referenciado" independente de análise
    # (`obter_evento_referenciado_leve`), com os prazos DESTE ato específico
    # (`caso["prazos"]`, já resolvido por `intimacao_service._agrupar_prazos_
    # por_ato` em `obter_caso` acima). Antes (até 2026-09-23) só recalculava
    # quando o caso NUNCA tinha sido analisado — pedido original do usuário
    # 2026-08-29: "quero que o botão esteja disponível sempre, independente
    # de análise" — só que isso deixava um caso JÁ analisado (antes da
    # correção de `_casar_evento_referenciado` que trocou a heurística de
    # polo por casamento por parte associada ao procurador, ver
    # `eproc_procurador_service`) preso ao evento ERRADO gravado em disco por
    # uma análise antiga, sem nunca reconferir — uma ação IRREVERSÍVEL
    # (fecha prazo de verdade no e-proc) não pode confiar em cache assim.
    from services import intimacao_ia_service

    leve = await intimacao_ia_service.obter_evento_referenciado_leve(processo, caso.get("prazos") or [])
    evento_intimacao = (leve or {}).get("evento_intimacao") or ""
    coautores = (leve or {}).get("evento_intimacao_coautores") or []
    if not evento_intimacao:
        msg = ("Não foi possível identificar automaticamente o evento de intimação deste "
               "processo — não é possível marcar o prazo a fechar no e-proc.")
        log_cb("erro", msg)
        return {"ok": False, "erro": msg}

    todos_disponiveis = [e for e in ([evento_intimacao] + list(coautores)) if e]
    if eventos_selecionados is not None:
        selecionados_str = {str(e) for e in eventos_selecionados if e}
        todos_disponiveis = [e for e in todos_disponiveis if str(e) in selecionados_str]
        if not todos_disponiveis:
            msg = "Nenhum evento de intimação selecionado para fechamento."
            log_cb("erro", msg)
            return {"ok": False, "erro": msg}

    log: list[dict] = []

    def registrar(level: str, msg: str) -> None:
        log.append({"level": level, "msg": msg, "ts": datetime.now().isoformat(timespec="seconds")})
        log_cb(level, msg)

    resultado = {"eproc_confirmado": False, "gerenciamento_ok": False, "sisdpu_ok": False}

    # Litisconsórcio ativo — outro(s) coautor(es) intimado(s) no MESMO evento/
    # dia/prazo (ver `intimacao_ia_service._casar_evento_referenciado`), cada
    # um com seu próprio nº de evento que TAMBÉM precisa ser fechado — a
    # menos que o Defensor tenha desmarcado algum no checklist do modal (ver
    # `eventos_selecionados` acima).
    todos_eventos = todos_disponiveis
    prazos_labels = [f"Ref. Evento {e}" for e in todos_eventos]

    # 1a) e-proc — abre a aba de peticionamento e marca o(s) checkbox(es) de
    # prazo, sem protocolar ainda.
    registrar("info", f"{processo}: abrindo a tela de peticionamento no e-proc "
                      f'(evento fixo "{EVENTO_EPROC}")…')
    if len(todos_eventos) > 1:
        registrar("muted", f"{processo}: litisconsórcio ativo — fechando {len(todos_eventos)} "
                           f"prazos juntos (eventos {', '.join(todos_eventos)}).")
    res_prep = await eproc_client.preparar_peticionamento(processo, EVENTO_EPROC, [], prazos_labels=prazos_labels)
    if not res_prep.get("ok"):
        msg = res_prep.get("erro") or "Falha ao preparar o peticionamento."
        registrar("erro", f"{processo}: {msg}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg)
        return {"ok": False, "erro": msg}

    marcados = res_prep.get("prazos_marcados") or []
    faltando = [e for e in todos_eventos if f"Ref. Evento {e}" not in marcados]
    if faltando:
        registrar("muted", f"{processo}: prazo do(s) evento(s) {', '.join(faltando)} não apareceu(ram) "
                          "entre os marcados — a intimação pode já não estar mais pendente no e-proc.")

    # 1b) e-proc — confirma de fato (IRREVERSÍVEL a partir daqui).
    registrar("info", f"{processo}: confirmando a ciência de verdade no e-proc (irreversível)…")
    res_conf = await eproc_client.confirmar_peticionamento(processo, evento=EVENTO_EPROC)
    if not res_conf.get("ok") or not res_conf.get("provavel_sucesso"):
        msg = res_conf.get("erro") or "Falha ao confirmar o peticionamento (sem sucesso detectado)."
        registrar("erro", f"{processo}: {msg}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg,
                                         mera_ciencia={"resultado": resultado, "log": log})
        return {"ok": False, "erro": msg, "resultado": resultado}
    resultado["eproc_confirmado"] = True
    registrar("ok", f"{processo}: ciência protocolada no e-proc.")

    # Baixa LOCAL dos prazos REALMENTE fechados agora (inclusive coautores em
    # litisconsórcio ativo) — sem isso a tabela de Prazos e-proc (e tudo que
    # dela deriva) só refletiria o fechamento na PRÓXIMA "Sincronizar
    # intimações". Também é o que permite o Gerenciamento de Processo logo
    # abaixo liberar sozinho quando este era o último prazo residual, sem
    # esperar sincronização.
    #
    # Marca só `len(todos_eventos)` ocorrências (nunca `caso["prazos"]`
    # inteiro) — os prazos crus do Painel são BYTE-A-BYTE idênticos entre si
    # num litisconsórcio simultâneo (mesmo evento+data_envio+prazo_dias pra
    # cada coautor, é por isso que `prazo_baixa_service` conta por
    # OCORRÊNCIA em vez de por chave booleana), então não há como apontar
    # qual ocorrência crua corresponde a qual evento fechado — mas dá pra
    # garantir a CONTAGEM certa: se o Defensor desmarcou 1 de 2 eventos no
    # checklist, só 1 ocorrência é baixada, deixando a outra genuinamente
    # visível (o prazo do coautor NÃO fechado continua realmente aberto no
    # e-proc). Bug real corrigido 2026-09-24: antes baixava sempre TODAS as
    # ocorrências, mesmo quando só parte do litisconsórcio foi fechada.
    from services import prazo_baixa_service

    for p in (caso.get("prazos") or [])[:len(todos_eventos)]:
        prazo_baixa_service.marcar_baixa(processo, p, origem="mera-ciencia", detalhe=paj_norm)

    # 2) e-proc — Gerenciamento de Processo (substabelecimento). Usa o
    # tipo/procurador EXPLÍCITO do checklist do modal quando informado
    # (`gerenciamento_tipo`/`gerenciamento_procurador_valor`); senão cai no
    # padrão de sempre (Substituir procurador + padrão de Configurações ›
    # e-proc).
    # Gerenciamento é OPCIONAL (checkbox no modal da página do PAJ, pedido do
    # Defensor 11/10/2026); a página Intimações segue gerenciando sempre.
    if not gerenciar:
        registrar("muted", f"{processo}: Gerenciamento de Processo não solicitado — pulado.")
    tipo_gerenciamento = gerenciamento_tipo or TIPO_GERENCIAMENTO
    if gerenciamento_procurador_valor is not None:
        procurador_valor = gerenciamento_procurador_valor
        procurador_rotulo = gerenciamento_procurador_rotulo or procurador_valor
    else:
        procurador_padrao = (config_painel_service.get_eproc_procuradores_prefs() or {}).get("procurador_padrao") or {}
        procurador_valor = procurador_padrao.get("valor") or ""
        procurador_rotulo = procurador_padrao.get("rotulo") or procurador_valor
    # "Desassociar procurador" (tipo "D") não tem procurador DESTINO — só os
    # demais tipos exigem um valor escolhido (mesma regra de "Ações no Eproc").
    if gerenciar and tipo_gerenciamento != "D" and not procurador_valor:
        msg = ("Ciência protocolada, mas não há procurador padrão configurado em Configurações › "
               "e-proc — Gerenciamento de Processo e movimentação no SISDPU NÃO foram executados. "
               "Configure o procurador padrão e finalize este caso manualmente.")
        registrar("erro", f"{processo}: {msg}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg,
                                         mera_ciencia={"resultado": resultado, "log": log})
        return {"ok": False, "erro": msg, "resultado": resultado}

    if gerenciar:
        registrar("info", f"{processo}: executando Gerenciamento de Processo "
                          f'({tipo_gerenciamento}{" → " + procurador_rotulo if tipo_gerenciamento != "D" else ""})…')
        res_ger = await acoes_eproc_service.executar_gerenciamento_real(processo, tipo_gerenciamento, procurador_valor)
    else:
        res_ger = {"ok": True, "pulado": True}
    if res_ger.get("pulado"):
        pass
    elif not res_ger.get("ok"):
        if res_ger.get("bloqueado_por_residual"):
            # NÃO é erro fatal — outro ato (simultâneo ou autônomo) deste
            # processo ainda tem prazo aberto no meu Painel do Procurador, e
            # gerenciar agora transferiria esse prazo pro novo procurador.
            # Pedido explícito do usuário (2026-09-XX): nunca gerenciar com
            # prazo residual. Segue pro passo 3 normalmente (a ciência JÁ foi
            # protocolada, isso não depende do gerenciamento) — o Defensor
            # repete "Dar mera ciência" no(s) ato(s) restante(s); quando não
            # sobrar mais nenhum, o gerenciamento libera sozinho.
            msg = res_ger.get("erro") or "Gerenciamento bloqueado: prazo residual neste processo."
            registrar("muted", f"{processo}: {msg} Gerenciamento NÃO executado agora — "
                              "trate o(s) prazo(s) restante(s) e repita depois.")
        else:
            msg = res_ger.get("erro") or "Falha no Gerenciamento de Processo."
            registrar("erro", f"{processo}: {msg} (ciência já protocolada — movimentação no SISDPU NÃO "
                              "executada; finalize manualmente).")
            intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg,
                                             mera_ciencia={"resultado": resultado, "log": log})
            return {"ok": False, "erro": msg, "resultado": resultado}
    else:
        resultado["gerenciamento_ok"] = True
        registrar("ok", f"{processo}: Gerenciamento de Processo concluído.")

    # 3) SISDPU — movimentação simples (sem tramitar/concluir), com a
    # descrição já revisada/editada pelo Defensor.
    id_processo = _id_processo_sisdpu(paj_norm)
    if not id_processo:
        # Fallback (pedido explícito do usuário, 2026-08-29, caso real
        # 5031818-71.2026.4.04.7000): a ciência e o Gerenciamento JÁ foram
        # protocolados de verdade nesse ponto — IRREVERSÍVEL — então não dá
        # mais pra tratar a falta do id do PAJ como se nada tivesse
        # acontecido. Em vez de só devolver um erro-beco-sem-saída ("finalize
        # manualmente do zero"), grava um RASCUNHO de movimentação pendente
        # no store de "Ações no SISDPU" (mesmo mecanismo de "Movimentações
        # pendentes" já usado pela ponte pós-peticionamento do e-proc — ver
        # `acoes_sisdpu_service.salvar_rascunho`/`listar_rascunhos`) com a
        # fase e a descrição já prontas — o Defensor só abre "Ações no
        # SISDPU" (ou a página "Movimentações pendentes") e clica Executar,
        # sem digitar nada de novo.
        erro_id = ("Ciência e Gerenciamento concluídos, mas não encontrei o id do PAJ no SISDPU "
                   "(metadata.json sem URL de detalhamento) — movimentação NÃO executada "
                   "automaticamente.")
        registrar("muted", f"{processo}: {erro_id} Gravando rascunho de movimentação pendente "
                           "pra finalizar manualmente…")
        try:
            import html as _html_mod

            descricao_html = "<p>" + _html_mod.escape(descricao_mov).replace("\n", "<br>") + "</p>"
            res_rascunho = acoes_sisdpu_service.salvar_rascunho(paj_norm, {
                "paj_label": caso.get("paj") or "",
                "fase": FASE_SISDPU,
                "grupo": "", "prazo": "", "prazo_data": "", "concluir": False,
                "descricao_mov_html": descricao_html,
                "descricao_tram": "",
                "anexos": [],
                "origem": "mera_ciencia_fallback_sem_id_sisdpu",
            })
        except Exception as e:  # noqa: BLE001
            res_rascunho = {"ok": False, "erro": f"{type(e).__name__}: {e}"}

        if res_rascunho.get("ok"):
            msg = (f"{erro_id} Um RASCUNHO com a fase e a descrição já preenchidas foi salvo em "
                   '"Ações no SISDPU" (também aparece em "Movimentações pendentes") — abra e '
                   "clique Executar pra concluir, sem precisar digitar nada de novo.")
            registrar("ok", f"{processo}: rascunho de movimentação pendente salvo com sucesso.")
            resultado["sisdpu_rascunho_criado"] = True
        else:
            # A tentativa de fallback em si falhou (ex.: disco) — não finge
            # que deu certo; volta pra mensagem antiga, sem rascunho.
            msg = (f"{erro_id} Além disso, a gravação do rascunho de fallback também falhou "
                   f'({res_rascunho.get("erro") or "erro desconhecido"}) — finalize manualmente '
                   'pelo console "Ações no SISDPU".')
            registrar("erro", f"{processo}: falha ao gravar o rascunho de fallback: "
                              f'{res_rascunho.get("erro")}')

        registrar("erro", f"{processo}: {msg}")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=msg,
                                         mera_ciencia={"resultado": resultado, "log": log})
        return {"ok": False, "erro": msg, "resultado": resultado}

    registrar("info", f'{processo}: movimentando o SISDPU (mera ciência, sem tramitar/concluir) — '
                      f'descrição: "{descricao_mov}"')
    sisdpu_erro = None
    async for ev in acoes_sisdpu_service.executar_stream(
        id_processo, paj=caso.get("paj") or "", paj_norm=paj_norm,
        fase=FASE_SISDPU, descricao_mov=descricao_mov,
    ):
        if ev.get("done"):
            r = ev.get("resultado") or {}
            if not r.get("ok"):
                sisdpu_erro = r.get("detalhe") or "Falha ao movimentar o SISDPU."
        else:
            registrar(ev.get("level") or "info", f'{processo}: {ev.get("msg") or ev}')
    if sisdpu_erro:
        registrar("erro", f"{processo}: {sisdpu_erro} (ciência e Gerenciamento já concluídos — "
                          "finalize a movimentação manualmente).")
        intimacao_service.salvar_estado(chave, status_geral="erro", erro=sisdpu_erro,
                                         mera_ciencia={"resultado": resultado, "log": log})
        return {"ok": False, "erro": sisdpu_erro, "resultado": resultado}
    resultado["sisdpu_ok"] = True
    registrar("ok", f"{processo}: movimentação registrada no SISDPU. Caso concluído.")

    intimacao_service.salvar_estado(
        chave, status_geral="concluido", erro=None,
        mera_ciencia={"resultado": resultado, "log": log, "descricao_mov": descricao_mov,
                      "concluido_em": datetime.now().isoformat(timespec="seconds")},
    )
    return {"ok": True, "resultado": resultado}
