"""Análise de PAJ por pretensão específica (BPC, BI, ...) via skill dedicada de
diagnóstico (SISDPU-AnalisarPAJ-BPC / SISDPU-AnalisarPAJ-BI).

Reusa o motor de sessão do `chat_service` (ChatSession) mas com instrução
PRÓPRIA — sem o wrapper genérico "produza a peça pronta" de
`chat_service._montar_instrucao`. As skills de diagnóstico terminam no
diagnóstico por padrão (só avançam para petição se o usuário pedir
explicitamente na mensagem seguinte); usar o wrapper genérico ("OBRIGATORIO:
produzir o TEXTO da peça... não basta dizer o que fazer") contradiria essa
regra da própria skill.

Persistência: `analise_pretensao.json` na pasta do PAJ — separado do
`elaboracao.json` usado pelo botão "Elaborar" genérico do cabeçalho, para as
duas ações não colidirem/se sobrescreverem.

Reenvio ("ajustar a análise" / "redigir a petição inicial"): retoma a MESMA
sessão do Claude via `--resume` (session_id capturado no diagnóstico) — o
contexto completo (dados do PAJ, diagnóstico já produzido) é preservado, em
vez de reconstruído a cada mensagem.

Nota sobre o catálogo de skills: as skills de diagnóstico vivem no perfil
GLOBAL do usuário (`~/.claude/skills/`), não no catálogo do workspace Oficio
Geral (`services/skills_catalog.py`, que só lê `OFICIO_GERAL/.claude/skills/`).
Por isso não passam pela validação `skill_valida()` do `chat_service` — aqui a
skill é citada só pelo nome na instrução; o Claude Code CLI descobre skills
globais independente do cwd do subprocess.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import unicodedata
from pathlib import Path

from config import PAJS_DIR
from services import historico
from services.chat_service import ChatSession
from services.estilo_pecas_service import INSTRUCAO_ESTILO_PECA_DPU
from services.valor_causa_service import INSTRUCAO_VALOR_CAUSA

# tipo -> {skill, label, palavras-chave da pretensão que pré-selecionam esse
# tipo no dropdown "Analisar" da página do PAJ}.
#
# "generica" (sem skill/palavras-chave): rota de diagnóstico SEM playbook de
# área — usada quando a pretensão do PAJ não tem skill dedicada, OU quando o
# PAJ está cadastrado no SISDPU com pretensão ERRADA (ex.: um caso de BPC
# cadastrado como "Aposentadoria por Idade") e o Defensor escolhe manualmente
# a rota certa no dropdown em vez de depender do auto-match por palavra-chave.
# `palavras_pretensao` vazio garante que `tipo_aplicavel()` NUNCA a escolhe
# sozinha — só entra por escolha explícita no dropdown (ver template
# `paj_detail.html`, variável `tipo_ia_padrao`).
CONFIG_TIPOS: dict[str, dict] = {
    "bpc": {
        "skill": "SISDPU-AnalisarPAJ-BPC",
        "label": "BPC",
        "palavras_pretensao": (
            "BPC",
            "LOAS",
            "BENEFICIO ASSISTENCIAL",
            "PRESTACAO CONTINUADA",
        ),
    },
    "bi": {
        "skill": "SISDPU-AnalisarPAJ-BI",
        "label": "Benefício por Incapacidade",
        "palavras_pretensao": (
            "AUXILIO-DOENCA",
            "AUXILIO DOENCA",
            "AUXILIO POR INCAPACIDADE",
            "BENEFICIO POR INCAPACIDADE",
            "INCAPACIDADE TEMPORARIA",
            "APOSENTADORIA POR INVALIDEZ",
            "APOSENTADORIA POR INCAPACIDADE PERMANENTE",
            "AUXILIO-ACIDENTE",
            "AUXILIO ACIDENTE",
        ),
    },
    "generica": {
        "skill": "",
        "label": "Análise ampla",
        "palavras_pretensao": (),
    },
}

_STATE_FILE = "analise_pretensao.json"
_sessions: dict[str, ChatSession] = {}


def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).upper()


def tipo_aplicavel(pretensao: str) -> str:
    """'bpc' (ou o tipo cabível) se a pretensão bater com alguma skill de
    diagnóstico cadastrada; '' se nenhuma bater (nunca retorna "generica" —
    ela não tem palavras-chave, é só escolha manual). Usado pelo filtro Jinja
    `tipo_analise_pretensao` só para PRÉ-SELECIONAR o dropdown "Analisar" na
    página do PAJ (`tipo_ia_padrao`) — o botão em si é sempre visível."""
    p = _sem_acento(pretensao or "")
    for tipo, cfg in CONFIG_TIPOS.items():
        if any(_sem_acento(kw) in p for kw in cfg["palavras_pretensao"]):
            return tipo
    return ""


def _arquivo_estado(paj_norm: str) -> Path:
    return PAJS_DIR / paj_norm / _STATE_FILE


def ler_disco(paj_norm: str) -> dict | None:
    f = _arquivo_estado(paj_norm)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _persistir(paj_norm: str, session: ChatSession, tipo: str) -> None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return
    import datetime as _dt

    data = {
        "tipo": tipo,
        "skill": CONFIG_TIPOS.get(tipo, {}).get("skill", ""),
        "status": session.status,
        "summary": session.summary,
        "last_action": session.last_action,
        "error": session.error,
        "session_id": session.session_id,
        "atualizado_em": _dt.datetime.now().isoformat(timespec="minutes"),
    }
    with contextlib.suppress(Exception):
        _arquivo_estado(paj_norm).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with contextlib.suppress(Exception):
        historico.registrar(
            paj_norm,
            "analise_pretensao",
            skill=data["skill"],
            status=session.status,
            resumo=historico.primeira_linha_util(session.summary),
        )


def get_status(paj_norm: str) -> dict:
    sess = _sessions.get(paj_norm)
    # Confia na sessao em memoria sempre que ela existir — qualquer status
    # (running/done/error), nao so "running" (mesmo padrao do
    # `chat.py: /api/elaborar/status`, que tambem nunca cai pro disco enquanto
    # a sessao em memoria existe).
    #
    # Ate aqui so retornava direto da sessao quando "running" — e' o motivo do
    # comentario antigo sobre nao exigir is_alive() (is_alive() so vira True
    # depois que o subprocess abre em _start_subprocess(), mas iniciar() ja
    # marca "running" antes disso, durante o pre-processamento OCR-LLM/
    # PROMPT_MAX). Mas isso deixava uma race MAIOR e mais grave: ao concluir,
    # a THREAD LEITORA do subprocess marca sess.status = "done" IMEDIATAMENTE
    # (dentro de _parse_event, ao processar o evento "result"); quem grava
    # analise_pretensao.json no disco e' a thread controladora (_run ->
    # _aguardar_conclusao, que so' percebe a mudanca no proximo polling —
    # granularidade de ate 0.5s — e so' ENTAO chama _persistir()). Nessa
    # janela, o status ja nao era "running" (pulava o `if` antigo) e o
    # arquivo ainda nao existia no disco (pulava o `ler_disco` abaixo) —
    # caia no fallback "idle". O polling do frontend (fetchStatus a cada 2s)
    # via esse "idle" fantasma, parava de acompanhar (analise_pretensao.js so
    # recarrega a pagina quando status === "done") e NUNCA recarregava — a
    # analise ficava com cara de "falha silenciosa" mesmo tendo terminado com
    # sucesso. Confiar sempre na sessao em memoria (enquanto ela existir)
    # fecha essa janela.
    if sess:
        return {
            "status": sess.status,
            "last_action": sess.last_action,
            "summary": sess.summary,
            "error": sess.error,
            "tipo": getattr(sess, "_tipo_analise", ""),
        }
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "last_action": "", "summary": "", "error": "", "tipo": ""}


def get_status_todos() -> dict[str, dict]:
    """Status de todas as analises por pretensao — usado pelo dashboard pra
    exibir um spinner na coluna 'Análise IA' enquanto o diagnostico roda em
    background (mesmo padrao de `chat.py: /api/elaborar/status`).

    So sessoes EM MEMORIA importam aqui: a coluna do dashboard ja mostra
    SIM/NAO com base no `analise_pretensao.json` do disco (via
    `paj_service.tem_analise_ia`), entao so precisamos sinalizar quais PAJs
    tem uma sessao ativa agora (status 'running')."""
    result: dict[str, dict] = {}
    for paj_norm, sess in _sessions.items():
        if sess.status == "running":
            result[paj_norm] = {
                "status": "running",
                "last_action": sess.last_action,
                "tipo": getattr(sess, "_tipo_analise", ""),
            }
    return result


# Trailer genérico que services.prompt_builder.gerar_prompt_max() sempre anexa
# ao FINAL de todo PROMPT_MAX.md (pensado para o botão "Elaborar" genérico —
# "produza o TEXTO pronto da peça"). Ao reaproveitar o PROMPT_MAX como contexto
# aqui, esse trailer contradiz a regra da própria skill de diagnóstico ("pare
# no diagnóstico, só avance com pedido explícito") — removido antes de montar
# a instrução (visto ao vivo: sem essa remoção, o Claude "atendeu" às duas
# instruções em conflito e gravou um despacho não solicitado).
_TRAILER_GENERICO_PROMPT_MAX = (
    "Siga o `CLAUDE.md` do workspace. Ao final, proponha o proximo passo "
    "(despacho SISDPU, peticao, recurso, manifestacao) e produza o TEXTO "
    "pronto da peca/despacho na pasta do PAJ."
)


# A instrução transversal do cálculo do valor da causa (INSTRUCAO_VALOR_CAUSA) vem
# de services.valor_causa_service — mesma usada pelo "Elaborar" genérico, para o
# comportamento ser uniforme em todo o painel.


# Instrução do sub-passo "redação da peça" — só entra em jogo se/quando a sessão
# avançar do diagnóstico para a petição (a própria skill decide isso; aqui só
# garantimos que, SE avançar, ela use o pipeline certo e o estilo do Defensor.
#
# Motivo de existir (bug real, PAJ 2026/029-01486, 05/08/2026): a primeira vez
# que a skill avançou para a redação, ela montou o .docx "à mão" com
# python-docx (fonte/layout genéricos) em vez de seguir o próprio Passo 5 da
# skill (adaptar modelo de teses-dpu/areas/<area>/modelos/ + rodar o pipeline
# `comum/skills/` — anti-alucinacao -> formatacao-docx -> checagem-final). E
# mesmo depois de corrigido isso (reenvio do usuário), a peça ainda saiu fora
# do padrão de redação pessoal do Defensor (travessão, mistura de
# miserabilidade/deficiência nos Fatos, colchete de conferência sem fonte
# vermelha) — o item 2 (estilo pessoal) agora vem de `estilo_pecas_service`,
# COMPARTILHADO com `chat_service.py` (botão "Elaborar"), pra não depender de
# qual botão o Defensor clicou.
_INSTRUCAO_PIPELINE_E_ESTILO_PECA = (
    "SE avançar para a redação de uma petição (ou outra peça) — agora ou numa "
    "mensagem futura minha —, siga ESTRITAMENTE o seguinte:\n\n"
    "1. PIPELINE OBRIGATÓRIO (é o que a própria skill manda no Passo 5 — NÃO "
    "monte o `.docx` \"à mão\" com python-docx genérico):\n"
    "   a. Adapte o corpo a partir do modelo mais próximo em "
    "`C:\\Claude\\teses-dpu\\areas\\<area>\\modelos\\` (ex.: `bpc-loas` para "
    "BPC/LOAS), escrevendo o `.txt` nos MARCADORES do formatador "
    "(`## Título`, `### Subtítulo`, `> citação`, `~ item de lista`, "
    "`%%doença%% (CID)`, `__No caso concreto__`, `**negrito**`) — NÃO use "
    "markdown tradicional (`**` sem ser esses marcadores não é reconhecido).\n"
    "   b. Rode a skill `anti-alucinacao` (`teses-dpu/comum/skills/anti-alucinacao/"
    "SKILL.md`) ANTES de finalizar — só cite precedente/norma com origem "
    "rastreável na base de jurisprudência do playbook.\n"
    "   c. Gere o `.docx` com o ENGINE oficial "
    "`teses-dpu/comum/skills/formatacao-docx/formatar_peca.py` "
    "(`criar_peticao_padrao()` para petição inicial) — nunca com um script "
    "python-docx improvisado.\n"
    "   d. Rode a `checagem-final` (`teses-dpu/comum/skills/checagem-final/"
    "CHECKLIST.md`) antes de dar a peça por pronta.\n\n"
    "2. " + INSTRUCAO_ESTILO_PECA_DPU +
    "Se o modelo do teses-dpu ou o engine de formatação conflitarem com as "
    "regras do item 2, as regras do item 2 prevalecem — são o padrão pessoal "
    "deste Defensor, mais específico que o padrão genérico do repositório "
    "público.\n\n"
)


# Instrução de diagnóstico para a rota "generica" (sem skill de playbook
# dedicada) — mesma função de uma skill como a de BPC (chegar a um diagnóstico
# estruturado e PARAR), só que raciocinando direto sobre o PROMPT_MAX em vez
# de seguir os passos fixos de um playbook de área. Usada quando `skill_slug`
# vem vazio em `_montar_instrucao_diagnostico` (tipo "generica" ou qualquer
# tipo futuro sem skill cadastrada).
_INSTRUCAO_DIAGNOSTICO_GENERICO = (
    "Faça um DIAGNÓSTICO JURÍDICO deste PAJ. Não há uma skill de playbook "
    "dedicada à pretensão deste caso (ou o PAJ pode estar cadastrado no "
    "SISDPU com a pretensão errada) — analise diretamente com base no "
    "CONTEXTO DO PAJ anexado abaixo (cabeçalho, narrativa, movimentações e "
    "anexos OCR já transcritos).\n\n"
    "Produza o diagnóstico neste formato:\n\n"
    "## Diagnóstico — <identificação do caso: nome do assistido / PAJ>\n\n"
    "### Situação\n"
    "[1-2 parágrafos: qual é a pretensão real deste caso (mesmo que diferente "
    "da pretensão cadastrada no SISDPU — sinalize se notar divergência), "
    "fase/via (administrativa ou judicial) e o fundamento do indeferimento ou "
    "da questão controvertida]\n\n"
    "### Teses cabíveis\n"
    "| Tese | Fundamento (lei/jurisprudência) | Aplicação a este caso |\n"
    "|---|---|---|\n\n"
    "### Documentação ainda faltante\n"
    "- [ ] <documento> — <para qual tese serve>\n\n"
    "### Lacunas para fechar o diagnóstico\n"
    "[perguntas objetivas, se faltar dado para confirmar alguma tese]\n\n"
    "Termine no diagnóstico — só avance para redigir petição/despacho/recurso "
    "se eu pedir explicitamente numa mensagem seguinte.\n\n"
)


# O painel captura e exibe TODO texto que o Claude emite durante o turno —
# inclusive comentários de passagem entre uma leitura de anexo e outra (ex.:
# "Let me first confirm the folder structure...") — não só a resposta final
# (ver chat_service._parse_event: todo text_delta do turno vai pro mesmo
# buffer `accumulated_text`, que vira `session.summary` e é renderizado
# inteiro na seção "Análise feita pela IA"). Sem instrução explícita, essa
# narração de passagem tende a sair em inglês mesmo com o restante da
# instrução em português (visto ao vivo: PAJ 2026/029-02299, narração em
# inglês antes do parecer em português) — por isso reforçamos aqui.
_INSTRUCAO_IDIOMA = (
    "IMPORTANTE sobre idioma: toda a sua comunicação nesta tarefa deve ser em "
    "português do Brasil — inclusive comentários breves de passagem entre uma "
    "leitura de anexo/ferramenta e outra (ex.: \"vou ler os documentos "
    "médicos agora\"). Não narre nem pense em voz alta em inglês em nenhum "
    "momento; toda essa narração é exibida ao Defensor exatamente como "
    "escrita.\n\n"
)


def _montar_instrucao_diagnostico(paj_norm: str, skill_slug: str, prompt_max: str) -> str:
    pasta = PAJS_DIR / paj_norm
    prompt_max_limpo = prompt_max.replace(_TRAILER_GENERICO_PROMPT_MAX, "").rstrip()
    if skill_slug:
        abertura = (
            f"Use a skill `/{skill_slug}` para diagnosticar este PAJ.\n\n"
            "Siga integralmente as instruções e o formato de saída definidos pela "
            "própria skill — inclusive quanto a QUANDO parar (ela decide se deve "
            "terminar no diagnóstico ou seguir para outra etapa). Não me pergunte "
            "nada antes de produzir o diagnóstico; use a melhor hipótese razoável e "
            "sinalize lacunas no próprio relatório, como a skill já orienta.\n\n"
        )
    else:
        abertura = _INSTRUCAO_DIAGNOSTICO_GENERICO
    return (
        abertura
        + _INSTRUCAO_IDIOMA
        + "IMPORTANTE sobre a fonte dos dados: use o CONTEXTO DO PAJ anexado abaixo "
        "(dados colados diretamente) — NÃO abra o SISDPU no navegador nem procure "
        "'Referencia PAJ.md' em C:\\Claude\\DPU\\PREV4\\; os dados já estão "
        "completos no contexto anexado.\n\n"
        "IGNORE qualquer instrução genérica de 'produzir o texto da peça/despacho "
        "ao final' que apareça no contexto do PAJ anexado abaixo — é um trecho "
        "padrão reaproveitado de outro fluxo do painel (o botão 'Elaborar'), não "
        "se aplica aqui. Para esta tarefa, a ÚNICA regra de quando produzir peça "
        "e parar é a definida acima.\n\n"
        f"Se em algum momento — agora ou numa mensagem futura minha — você "
        f"avançar para a redação de uma petição inicial (ou qualquer outra "
        f"peça), **salve o arquivo em `{pasta}\\Peças IA\\`** (crie essa subpasta "
        f"se não existir; ex.: `peticao_inicial.txt`) — dentro da pasta deste PAJ "
        f"no PAINEL SISDPU, não a estrutura de pastas do workspace teses-dpu/PREV4.\n\n"
        + _INSTRUCAO_PIPELINE_E_ESTILO_PECA
        + INSTRUCAO_VALOR_CAUSA
        + "---\n\n"
        f"{prompt_max_limpo}"
    )


def _aguardar_conclusao(session: ChatSession, timeout: int = 1800) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(paj_norm: str, tipo: str) -> dict:
    cfg = CONFIG_TIPOS.get(tipo)
    if not cfg:
        return {"ok": False, "erro": f"Tipo de análise desconhecido: {tipo}"}
    skill_slug = cfg["skill"]

    existing = _sessions.get(paj_norm)
    # NAO exigir existing.is_alive() (mesmo motivo do get_status(): is_alive()
    # só vira True depois que o subprocess é aberto — enquanto uma sessão
    # anterior ainda está no pré-processamento OCR-LLM/PROMPT_MAX, is_alive()
    # é False, e sem esse ajuste um segundo clique nessa janela passaria
    # direto e spawnaria uma sessão DUPLICADA, com dois threads escrevendo no
    # mesmo PROMPT_MAX.md/analise_pretensao.json ao mesmo tempo.
    if existing and existing.status == "running":
        return {"ok": False, "erro": "Já há uma análise em andamento para este PAJ."}
    if existing:
        existing.stop()

    # skill_slug=None no construtor: usamos instrução própria (start_elaboracao),
    # não o wrapper genérico de _montar_instrucao — ver docstring do módulo.
    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session._tipo_analise = tipo
    # Marca "running" JÁ AQUI (síncrono, antes da thread) — não só quando
    # start_elaboracao() abre o subprocess. Sem isso, get_status() reportava
    # "idle" durante o pré-processamento (OCR-LLM + regeração do PROMPT_MAX,
    # que pode levar bem mais que os 2s do intervalo de polling em PAJs com
    # muitos anexos de OCR fraco) e o polling do frontend sincronizava com
    # esse falso "idle", fazendo o indicador de "analisando..." desaparecer
    # logo após o clique em "Analisar BPC".
    session.status = "running"
    session.last_action = "preparando o contexto (lendo anexos, gerando PROMPT_MAX)…"
    _sessions[paj_norm] = session

    def _run() -> None:
        pasta = PAJS_DIR / paj_norm
        prompt_path = pasta / "PROMPT_MAX.md"
        # Fallback OCR-LLM: mesmo cuidado do botao "Elaborar" generico
        # (chat_service.start) — ANTES de montar o PROMPT_MAX, transcreve com
        # Sonnet (por visao) os anexos cujo OCR local ficou fraco/ausente, pra
        # o diagnostico nao ser feito com texto incompleto. Idempotente e
        # best-effort; pula se a cota esta esgotada.
        with contextlib.suppress(Exception):
            import services.cota_service as _cota_chk

            if not _cota_chk.esta_pausado():
                from services.ocr_llm_service import melhorar_ocr_paj

                melhorar_ocr_paj(paj_norm)
        with contextlib.suppress(Exception):
            from services.prompt_builder import gerar_prompt_max

            gerar_prompt_max(paj_norm)
        prompt_content = (
            prompt_path.read_text(encoding="utf-8", errors="replace")
            if prompt_path.exists()
            else ""
        )
        instrucao = _montar_instrucao_diagnostico(paj_norm, skill_slug, prompt_content)
        if not session.start_elaboracao(instrucao):
            _persistir(paj_norm, session, tipo)
            return
        _aguardar_conclusao(session)
        _persistir(paj_norm, session, tipo)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


def reenviar(paj_norm: str, mensagem: str) -> dict:
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return {"ok": False, "erro": "Mensagem vazia."}

    disco = ler_disco(paj_norm) or {}
    existing = _sessions.get(paj_norm)
    if existing and existing.status == "running":
        return {"ok": False, "erro": "Já há uma operação em andamento para este PAJ."}

    session_id = (existing.session_id if existing else "") or disco.get("session_id", "")
    tipo = (getattr(existing, "_tipo_analise", "") if existing else "") or disco.get("tipo", "")
    if not session_id or tipo not in CONFIG_TIPOS:
        return {
            "ok": False,
            "erro": "Nenhuma análise anterior encontrada para retomar. Inicie a análise primeiro.",
        }

    pasta = PAJS_DIR / paj_norm
    mensagem_final = (
        f"{mensagem}\n\n"
        f"(Lembrete: se gerar ou atualizar algum arquivo — ex.: petição inicial —, "
        f"salve em `{pasta}\\Peças IA\\` — crie essa subpasta se não existir; é a "
        "pasta deste PAJ dentro do PAINEL SISDPU.)\n"
        "(Se redigir uma petição inicial com parcelas devidas: deixe o marcador "
        "`{{VALOR_CAUSA}}` no ponto do valor da causa e salve `valor_causa.json` "
        "na mesma pasta com os parâmetros — o painel calcula e substitui; não "
        "calcule o valor você mesmo.)\n\n"
        + _INSTRUCAO_PIPELINE_E_ESTILO_PECA
    )

    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session.resume_session_id = session_id
    session._tipo_analise = tipo
    _sessions[paj_norm] = session

    def _run() -> None:
        if not session._start_subprocess():
            _persistir(paj_norm, session, tipo)
            return
        session.status = "running"
        session.last_action = "processando…"
        session.accumulated_text = ""
        session.send_message(mensagem_final)
        _aguardar_conclusao(session)
        _persistir(paj_norm, session, tipo)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
