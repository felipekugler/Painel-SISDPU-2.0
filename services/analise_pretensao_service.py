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

# tipo -> {skill, label, palavras-chave da pretensão que habilitam o botão}.
# Adicionar "bi" aqui (mesmo padrão) quando a versão BPC estiver validada.
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
}

_STATE_FILE = "analise_pretensao.json"
_sessions: dict[str, ChatSession] = {}


def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).upper()


def tipo_aplicavel(pretensao: str) -> str:
    """'bpc' (ou o tipo cabível) se a pretensão bater com alguma skill de
    diagnóstico cadastrada; '' se nenhuma bater. Usado pelo filtro Jinja
    `tipo_analise_pretensao` para decidir se mostra o botão na página do PAJ."""
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
    if sess and sess.is_alive() and sess.status == "running":
        return {
            "status": "running",
            "last_action": sess.last_action,
            "summary": sess.summary,
            "error": "",
            "tipo": getattr(sess, "_tipo_analise", ""),
        }
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "last_action": "", "summary": "", "error": "", "tipo": ""}


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


def _montar_instrucao_diagnostico(paj_norm: str, skill_slug: str, prompt_max: str) -> str:
    pasta = PAJS_DIR / paj_norm
    prompt_max_limpo = prompt_max.replace(_TRAILER_GENERICO_PROMPT_MAX, "").rstrip()
    return (
        f"Use a skill `/{skill_slug}` para diagnosticar este PAJ.\n\n"
        "IMPORTANTE sobre a fonte dos dados: use o CONTEXTO DO PAJ anexado abaixo "
        "(é a opção de dados colados diretamente, prevista na própria skill) — "
        "NÃO abra o SISDPU no navegador nem procure 'Referencia PAJ.md' em "
        "C:\\Claude\\DPU\\PREV4\\; os dados já estão completos no contexto anexado.\n\n"
        "Siga integralmente as instruções e o formato de saída definidos pela "
        "própria skill — inclusive quanto a QUANDO parar (ela decide se deve "
        "terminar no diagnóstico ou seguir para outra etapa). Não me pergunte "
        "nada antes de produzir o diagnóstico; use a melhor hipótese razoável e "
        "sinalize lacunas no próprio relatório, como a skill já orienta.\n\n"
        "IGNORE qualquer instrução genérica de 'produzir o texto da peça/despacho "
        "ao final' que apareça no contexto do PAJ anexado abaixo — é um trecho "
        "padrão reaproveitado de outro fluxo do painel (o botão 'Elaborar'), não "
        "se aplica aqui. Para esta tarefa, a ÚNICA regra de quando produzir peça "
        "e parar é a da própria skill de diagnóstico citada acima.\n\n"
        f"Se em algum momento — agora ou numa mensagem futura minha — você "
        f"avançar para a redação de uma petição inicial (ou qualquer outra "
        f"peça), **salve o arquivo em `{pasta}\\Peças IA\\`** (crie essa subpasta "
        f"se não existir; ex.: `peticao_inicial.txt`) — dentro da pasta deste PAJ "
        f"no PAINEL SISDPU, não a estrutura de pastas do workspace teses-dpu/PREV4.\n\n"
        "---\n\n"
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
    if existing and existing.is_alive() and existing.status == "running":
        return {"ok": False, "erro": "Já há uma análise em andamento para este PAJ."}
    if existing:
        existing.stop()

    # skill_slug=None no construtor: usamos instrução própria (start_elaboracao),
    # não o wrapper genérico de _montar_instrucao — ver docstring do módulo.
    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session._tipo_analise = tipo
    _sessions[paj_norm] = session

    def _run() -> None:
        pasta = PAJS_DIR / paj_norm
        prompt_path = pasta / "PROMPT_MAX.md"
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
    if existing and existing.is_alive() and existing.status == "running":
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
        "pasta deste PAJ dentro do PAINEL SISDPU.)"
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
