"""Botão "Calcular Tempo de Contribuição" (seção "Ações com IA" da página do
PAJ) — calcula tempo de contribuição/carência/idade a partir de um extrato
CNIS específico, via sessão real do Claude Code CLI executando a skill
global `AnaliseCNIS`.

Por que um arquivo é escolhido EXPLICITAMENTE pelo Defensor (não "o CNIS mais
recente" ou "o único CNIS achado" por adivinhação da IA): um mesmo PAJ pode
ter mais de um extrato CNIS em `Arquivos/`, baixado em datas diferentes (o
segurado pode ter novos vínculos/competências entre uma extração e outra) —
calcular com o arquivo errado dá um resultado silenciosamente incorreto.
`listar_arquivos_cnis()` alimenta um modal de seleção (ver
`static/js/calcular_cnis.js`); `iniciar()` só aceita um caminho relativo que
já exista fisicamente em `Arquivos/`.

Mesmo padrão arquitetural de `services/organizar_ajuizamento_service.py`
(sessão `ChatSession` com instrução própria, persistência em JSON próprio na
pasta do PAJ) — não o padrão de `services/analise_pretensao_service.py`
(dropdown genérico de "tipo de análise"): este botão é deliberadamente
próprio, com seu próprio fluxo de seleção de arquivo, a pedido do usuário.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

from config import PAJS_DIR, TIMEOUT_TOTAL
from services import historico
from services.chat_service import ChatSession

SKILL_DIR = Path.home() / ".claude" / "skills" / "AnaliseCNIS"

_STATE_FILE = "calcular_cnis.json"
_sessions: dict[str, ChatSession] = {}


def _detectar_tipo_cnis(caminho: Path) -> str:
    """'completo' se a primeira página do PDF trouxer o título "Extrato
    Previdenciário" (CNIS com remunerações/contribuições por competência);
    'simples' se trouxer "Relações Previdenciárias" (só vínculos); 'simples'
    também como fallback se não conseguir abrir o PDF ou não reconhecer o
    título (mais seguro pedir o modo completo manualmente do que travar o
    modal). Ver skill AnaliseCNIS, Passo 1, para os mesmos dois títulos."""
    try:
        import fitz  # PyMuPDF

        with fitz.open(caminho) as doc:
            texto = doc[0].get_text() if doc.page_count else ""
        if "extrato previdenciário" in texto.lower():
            return "completo"
        return "simples"
    except Exception:
        return "simples"


def listar_arquivos_cnis(paj_norm: str) -> list[dict]:
    """Arquivos em `Arquivos/` cujo nome contenha 'cnis' (case-insensitive) —
    candidatos para o modal de seleção. Não filtra por extensão: o Defensor
    pode ter baixado o extrato em PDF ou, mais raramente, como imagem.

    Cada candidato já vem com `tipo` ('simples'/'completo') detectado pelo
    título da primeira página — o modal usa isso pra decidir se pergunta o
    modo (simples/completo) antes de iniciar."""
    pasta_arquivos = PAJS_DIR / paj_norm / "Arquivos"
    if not pasta_arquivos.exists() or not pasta_arquivos.is_dir():
        return []
    candidatos = []
    for f in pasta_arquivos.iterdir():
        if f.is_file() and "cnis" in f.stem.lower():
            candidatos.append(
                {
                    "nome": f.name,
                    "caminho": f"Arquivos/{f.name}",
                    "tamanho": f.stat().st_size,
                    "modificado": f.stat().st_mtime,
                    "tipo": _detectar_tipo_cnis(f),
                }
            )
    candidatos.sort(key=lambda c: c["modificado"], reverse=True)
    return candidatos


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


def get_status(paj_norm: str) -> dict:
    # Mesmo motivo de organizar_ajuizamento_service.get_status(): confiar na
    # sessão em memória enquanto ela existir (não só quando "running") evita
    # a janela de "idle" fantasma entre a thread leitora marcar sess.status
    # = "done" e a thread controladora persistir no disco.
    sess = _sessions.get(paj_norm)
    if sess:
        return {
            "status": sess.status,
            "log": sess.summary,
            "error": sess.error,
            "arquivo_cnis": getattr(sess, "_arquivo_cnis", ""),
            "modo": getattr(sess, "_modo", ""),
        }
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "log": "", "error": "", "arquivo_cnis": "", "modo": ""}


_INSTRUCAO_IDIOMA = (
    "IMPORTANTE sobre idioma: toda a sua comunicação nesta tarefa deve ser em "
    "português do Brasil, inclusive comentários breves de passagem entre a "
    "leitura de um documento e outro. Não narre nem pense em voz alta em "
    "inglês em nenhum momento.\n\n"
)


def _montar_instrucao(paj_norm: str, arquivo_cnis: str, modo: str) -> str:
    pasta = PAJS_DIR / paj_norm
    caminho_cnis = pasta / arquivo_cnis
    pasta_pecas_ia = pasta / "Peças IA"

    if modo == "completo":
        bloco_modo = (
            "## Modo escolhido: COMPLETO (com análise de remunerações)\n\n"
            "O Defensor escolheu o **modo completo** no modal — este CNIS tem "
            "remunerações/contribuições por competência. Siga também o Passo 2.5 da "
            "skill (Regra 1 e Regra 2 de competências abaixo do salário mínimo), "
            "além dos passos do cálculo simples.\n\n"
        )
    else:
        bloco_modo = (
            "## Modo escolhido: SIMPLES (sem análise de remunerações)\n\n"
            "O Defensor escolheu o **modo simples** no modal — mesmo que este CNIS "
            "tenha remunerações/contribuições por competência, NÃO leia essas "
            "subtabelas nem aplique o Passo 2.5 da skill; use só a tabela de "
            "vínculos, exatamente como no cálculo simples.\n\n"
        )

    return (
        f"Calcule o tempo de contribuição, a carência e a idade nos marcos "
        f"temporais do assistido do PAJ {paj_norm}, seguindo INTEGRALMENTE a "
        f"skill `AnaliseCNIS` (leia `{SKILL_DIR / 'SKILL.md'}` primeiro).\n\n"
        "## Arquivo do CNIS a utilizar (OBRIGATÓRIO — não procure outro)\n\n"
        f"O Defensor selecionou explicitamente este arquivo:\n\n"
        f"    {caminho_cnis}\n\n"
        "Este PAJ pode ter mais de um extrato CNIS em `Arquivos\\`, baixado "
        "em datas diferentes — **use exclusivamente o arquivo acima**, "
        "mesmo que outros arquivos com 'CNIS' no nome existam na mesma "
        "pasta. Não pule esta etapa nem assuma outro arquivo por parecer "
        "'mais recente' ou 'mais completo'.\n\n"
        + bloco_modo
        + "## Onde salvar o resultado\n\n"
        f"Salve o PDF gerado em `{pasta_pecas_ia}\\` (crie a subpasta se não "
        "existir) — é a pasta padrão de qualquer arquivo gerado por análise "
        "de IA neste painel, já varrida automaticamente para a aba "
        "\"Peças\" do PAJ. Nome do arquivo: "
        "`Analise_CNIS_<nome do segurado em maiúsculas>.pdf`.\n\n"
        "## Dados adicionais do caso\n\n"
        "Se precisar de data de nascimento, sexo ou DER e eles não "
        "estiverem claros no próprio CNIS, procure no CONTEXTO DO PAJ "
        "(narrativa/movimentações, se disponível) antes de assumir "
        "'não informado(a)'.\n\n"
        + _INSTRUCAO_IDIOMA
        + "Não me pergunte nada — decida autonomamente com base no exposto e "
        "na própria skill, e sinalize qualquer incerteza nas Observações do "
        "PDF (é para isso que elas existem), não como pergunta.\n\n"
        "Ao final, contando com o resumo que a skill já define no Passo 5 "
        "(uso dentro do painel), me diga em 2-3 linhas: tempo de "
        "contribuição e carência totais na DER (ou na data do cálculo, se a "
        "DER não estiver disponível), e se algum vínculo ficou marcado "
        "`[CONFERIR]`."
    )


def _persistir(paj_norm: str, session: ChatSession) -> None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return
    import datetime as _dt

    data = {
        "status": session.status,
        "log": session.summary,
        "error": session.error,
        "arquivo_cnis": getattr(session, "_arquivo_cnis", ""),
        "modo": getattr(session, "_modo", ""),
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
            "calcular_cnis",
            status=session.status,
            resumo=historico.primeira_linha_util(session.summary),
        )


def _aguardar_conclusao(session: ChatSession, timeout: int = TIMEOUT_TOTAL) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(paj_norm: str, arquivo_cnis: str, modo: str = "simples") -> dict:
    """`modo`: 'simples' ou 'completo' — escolhido no modal quando o arquivo
    detectado é um CNIS completo (ver calcular_cnis.js); irrelevante (mas
    inofensivo) se o CNIS for simples, que só sabe fazer o cálculo simples."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if not (SKILL_DIR / "SKILL.md").exists():
        return {"ok": False, "erro": f"Skill não encontrada em {SKILL_DIR}."}

    arquivo_cnis = (arquivo_cnis or "").strip().lstrip("/\\")
    if not arquivo_cnis:
        return {"ok": False, "erro": "Nenhum arquivo de CNIS selecionado."}
    modo = "completo" if modo == "completo" else "simples"
    # Trava simples contra path traversal — o arquivo tem que estar
    # literalmente dentro de Arquivos/ desta pasta, sem ../ no meio.
    caminho_cnis = (pasta / arquivo_cnis).resolve()
    pasta_arquivos = (pasta / "Arquivos").resolve()
    if pasta_arquivos not in caminho_cnis.parents or not caminho_cnis.is_file():
        return {"ok": False, "erro": "Arquivo de CNIS inválido ou fora da pasta do PAJ."}

    existing = _sessions.get(paj_norm)
    if existing and existing.status == "running":
        return {"ok": False, "erro": "Já há um cálculo em andamento para este PAJ."}
    if existing:
        existing.stop()

    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session._arquivo_cnis = arquivo_cnis
    session._modo = modo
    session.status = "running"
    session.last_action = "preparando o contexto (lendo o CNIS selecionado)…"
    _sessions[paj_norm] = session

    def _run() -> None:
        instrucao = _montar_instrucao(paj_norm, arquivo_cnis, modo)
        if not session.start_elaboracao(instrucao):
            _persistir(paj_norm, session)
            return
        _aguardar_conclusao(session)
        _persistir(paj_norm, session)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
