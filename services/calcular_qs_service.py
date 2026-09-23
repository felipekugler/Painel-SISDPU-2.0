"""Botão "Calcular Qualidade de Segurado" (seção "Ações com IA" da página do
PAJ) — calcula período de graça e valida uma data-alvo/fato gerador contra
ele, a partir de um extrato CNIS específico, via sessão real do Claude Code
CLI executando a skill global `AnáliseQS` (generalizada em 2026-09-21 para
cobrir qualquer benefício, não só incapacidade).

Deliberadamente SEPARADO do botão "Calcular Tempo de Contribuição"
(calcular_cnis_service.py / skill AnaliseCNIS): aquele lê o CNIS competência
a competência para somar tempo de contribuição e carência (lento, correto
para qualquer marco temporal); este só precisa de 4 fatos simples (última
competência, tipo de filiação, total de contribuições, desemprego) para
calcular o período de graça (art. 15, LBPS) e checar se uma data cai dentro
dele — é rápido e cobre a maioria dos indeferimentos por "perda de qualidade
de segurado" sem precisar do cálculo pesado.

Quando a data-alvo cai FORA do período de graça e o histórico contributivo
é relevante, a própria skill AnáliseQS apenas SUGERE (nunca executa) a
verificação de direito adquirido a aposentadoria — cabe ao Defensor decidir
se vale rodar, à parte, o botão "🧮 Calcular Tempo de Contribuição" (pedido
explícito do usuário, 2026-09-21: essa segunda análise é rara e demorada, não
deve rodar automaticamente na maioria dos casos).

Reaproveita `services.calcular_cnis_service.listar_arquivos_cnis` para a
detecção/seleção de arquivo CNIS (mesmo modal, mesmo critério de arquivo
nativo/escaneado) — não duplicado aqui.

Mesmo padrão arquitetural de `services/calcular_cnis_service.py` (sessão
`ChatSession` com instrução própria, persistência em JSON próprio na pasta
do PAJ)."""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import threading
import time
from pathlib import Path

from config import PAJS_DIR, TIMEOUT_TOTAL
from services import historico
from services.chat_service import ChatSession

SKILL_DIR = Path.home() / ".claude" / "skills" / "AnáliseQS"

_STATE_FILE = "calcular_qs.json"
_sessions: dict[str, ChatSession] = {}

# Rótulo e nome do parâmetro `data_alvo_rotulo` (só usado quando
# tipo_beneficio == "outro") por tipo de benefício — espelha a tabela
# "Data-alvo por tipo de benefício" da skill AnáliseQS. Mantido aqui só para
# validação e para montar a instrução em português natural; a skill é quem
# de fato decide o texto final do parecer.
_ROTULOS_BENEFICIO = {
    "bi": "Data de Início da Incapacidade (DII)",
    "pensao_morte": "Data do óbito do instituidor",
    "salario_maternidade": "Data do parto, da adoção ou da guarda judicial para adoção",
    "auxilio_reclusao": "Data do recolhimento à prisão do segurado",
    "outro": None,  # usa data_alvo_rotulo informado pelo Defensor
}


def listar_tipos_beneficio() -> list[dict]:
    """Alimenta o dropdown do modal — ver static/js/calcular_qs.js."""
    return [
        {"valor": "bi", "rotulo": "Benefício por Incapacidade (DII)"},
        {"valor": "pensao_morte", "rotulo": "Pensão por Morte (data do óbito)"},
        {"valor": "salario_maternidade", "rotulo": "Salário-Maternidade (parto/adoção/guarda)"},
        {"valor": "auxilio_reclusao", "rotulo": "Auxílio-Reclusão (data da prisão)"},
        {"valor": "outro", "rotulo": "Outro (informar a data-alvo manualmente)"},
    ]


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
    # Mesmo motivo de calcular_cnis_service.get_status(): confiar na sessão
    # em memória enquanto ela existir evita a janela de "idle" fantasma entre
    # a thread leitora marcar sess.status = "done" e a persistência no disco.
    sess = _sessions.get(paj_norm)
    if sess:
        return {
            "status": sess.status,
            "log": sess.summary,
            "error": sess.error,
            "arquivo_cnis": getattr(sess, "_arquivo_cnis", ""),
            "tipo_beneficio": getattr(sess, "_tipo_beneficio", ""),
            "data_alvo": getattr(sess, "_data_alvo", ""),
            "orientacao": getattr(sess, "_orientacao", ""),
        }
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "log": "", "error": "", "arquivo_cnis": "", "tipo_beneficio": "", "data_alvo": ""}


_INSTRUCAO_IDIOMA = (
    "IMPORTANTE sobre idioma: toda a sua comunicação nesta tarefa deve ser em "
    "português do Brasil, inclusive comentários breves de passagem entre a "
    "leitura de um documento e outro. Não narre nem pense em voz alta em "
    "inglês em nenhum momento.\n\n"
)


def _montar_instrucao(
    paj_norm: str,
    arquivo_cnis: str,
    tipo_beneficio: str,
    data_alvo_rotulo: str,
    data_alvo: str,
    data_alvo_fonte: str,
    idade_instituidor: str,
    orientacao: str = "",
) -> str:
    pasta = PAJS_DIR / paj_norm
    caminho_cnis = pasta / arquivo_cnis
    pasta_pecas_ia = pasta / "Peças IA"

    rotulo_efetivo = _ROTULOS_BENEFICIO.get(tipo_beneficio) or data_alvo_rotulo or "Data-alvo"

    bloco_idade = (
        f"- **Idade do titular do vínculo (instituidor/segurado) na data-alvo**: {idade_instituidor} anos "
        "(informada pelo Defensor — use só para refinar a sugestão do Passo 3.5 da skill, nunca para outra finalidade).\n\n"
        if idade_instituidor
        else ""
    )

    # Orientação diretiva do Defensor (caixa compartilhada ao lado do botão
    # "Analisar", ver paj_detail.html) — integrada também a este botão a
    # pedido do usuário (2026-09-21). Mesma ressalva de
    # calcular_cnis_service._montar_instrucao: guia interpretação, não
    # sobrescreve as regras do período de graça nem os dados informados
    # objetivamente (tipo de benefício, data-alvo).
    bloco_orientacao = ""
    if orientacao:
        bloco_orientacao = (
            "## Orientação diretiva do Defensor (leia com atenção — é uma decisão já "
            "tomada pelo Defensor sobre o caso, não uma sugestão a ser questionada)\n\n"
            f'"{orientacao}"\n\n'
            "Direcione a análise/parecer conforme essa orientação. **Ela não pode "
            "sobrescrever as regras da skill** (cálculo do período de graça, tipo de "
            "benefício e data-alvo informados no modal) — se a orientação pedir algo "
            "que contradiga essas regras, siga as regras e sinalize o conflito no "
            "parecer.\n\n"
        )

    return (
        f"Calcule a qualidade de segurado do titular do vínculo previdenciário no CNIS "
        f"abaixo, do PAJ {paj_norm}, seguindo INTEGRALMENTE a skill `AnáliseQS` "
        f"(leia `{SKILL_DIR / 'SKILL.md'}` primeiro).\n\n"
        + bloco_orientacao
        + "## Arquivo do CNIS a utilizar (OBRIGATÓRIO — não procure outro)\n\n"
        f"O Defensor selecionou explicitamente este arquivo:\n\n"
        f"    {caminho_cnis}\n\n"
        "Este PAJ pode ter mais de um extrato CNIS em `Arquivos\\`, baixado em datas "
        "diferentes — **use exclusivamente o arquivo acima**. Extraia dele apenas os 4 "
        "dados que a skill precisa (última competência com contribuição, tipo de "
        "filiação predominante/mais recente, total de contribuições mensais, e se há "
        "indício de desemprego involuntário após o último vínculo) — **não** faça o "
        "cálculo completo de tempo de contribuição/carência (isso é o botão \"Calcular "
        "Tempo de Contribuição\", uma tarefa separada e mais lenta).\n\n"
        "## Tipo de benefício e data-alvo (fato gerador) — informados pelo Defensor\n\n"
        f"- **Tipo de benefício (`tipo_beneficio`)**: `{tipo_beneficio}` — {rotulo_efetivo}.\n"
        f"- **Data-alvo a validar**: {data_alvo} (formato DD/MM/AAAA).\n"
        f"- **Fonte da data-alvo**: {data_alvo_fonte or 'não informada — mencione isso no parecer'}.\n\n"
        + bloco_idade
        + "Use exclusivamente esses valores informados pelo Defensor para tipo de "
        "benefício, data-alvo e sua fonte — não pesquise nem infira essas informações "
        "em nenhum outro documento do PAJ.\n\n"
        "## Onde salvar o resultado\n\n"
        f"Salve o PDF gerado em `{pasta_pecas_ia}\\` (crie a subpasta se não existir) — "
        "pasta padrão de qualquer arquivo gerado por análise de IA neste painel, já "
        "varrida automaticamente para a aba \"Peças\" do PAJ. Nome do arquivo: "
        f"`Qualidade de Segurado - {tipo_beneficio}.pdf`.\n\n"
        "## Sugestão de direito adquirido a aposentadoria (Passo 3.5 da skill)\n\n"
        "Se a data-alvo cair FORA do período de graça, siga o Passo 3.5 da skill para "
        "decidir se cabe SUGERIR a verificação de direito adquirido a aposentadoria — "
        "e deixe claro no parecer que essa verificação, se sugerida, **não foi feita "
        "aqui** e deve ser acionada separadamente pelo botão \"🧮 Calcular Tempo de "
        "Contribuição\" deste mesmo PAJ (skill `AnaliseCNIS`), só se o Defensor decidir "
        "que vale a pena para este caso. Nunca calcule tempo de contribuição/carência "
        "nesta tarefa, mesmo que pareça rápido para este CNIS específico.\n\n"
        + _INSTRUCAO_IDIOMA
        + "Não me pergunte nada — decida autonomamente com base no exposto e na própria "
        "skill, e sinalize qualquer incerteza nas Observações do PDF (é para isso que "
        "elas existem), não como pergunta.\n\n"
        "Ao final, me diga em 2-3 linhas: se há qualidade de segurado na data-alvo "
        "informada, até quando vai o período de graça, e se a verificação de direito "
        "adquirido a aposentadoria foi sugerida (e por quê)."
    )


def _persistir(paj_norm: str, session: ChatSession) -> None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return

    data = {
        "status": session.status,
        "log": session.summary,
        "error": session.error,
        "arquivo_cnis": getattr(session, "_arquivo_cnis", ""),
        "tipo_beneficio": getattr(session, "_tipo_beneficio", ""),
        "data_alvo": getattr(session, "_data_alvo", ""),
        "orientacao": getattr(session, "_orientacao", ""),
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
            "calcular_qs",
            status=session.status,
            resumo=historico.primeira_linha_util(session.summary),
        )

    # Lembrete vermelho ao concluir com sucesso — mesmo padrão de
    # calcular_cnis_service (o cálculo roda em background).
    if session.status == "done":
        with contextlib.suppress(Exception):
            from services import lembretes_service

            lembretes_service.criar_lembrete(
                paj_norm, "Análise de qualidade de segurado salva na pasta", "vermelho"
            )


def _aguardar_conclusao(session: ChatSession, timeout: int = TIMEOUT_TOTAL) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(
    paj_norm: str,
    arquivo_cnis: str,
    tipo_beneficio: str,
    data_alvo_rotulo: str = "",
    data_alvo: str = "",
    data_alvo_fonte: str = "",
    idade_instituidor: str = "",
    orientacao: str = "",
) -> dict:
    """`tipo_beneficio`: bi | pensao_morte | salario_maternidade |
    auxilio_reclusao | outro — define o rótulo da data-alvo no parecer (ver
    skill AnáliseQS). `data_alvo_rotulo` só é obrigatório se
    tipo_beneficio == "outro".

    `data_alvo`/`data_alvo_fonte`/`idade_instituidor`: informados
    objetivamente pelo Defensor no modal — mesma regra de calcular_cnis
    (sexo/DER): a IA nunca pesquisa esses dados em outra fonte do PAJ.

    `orientacao`: texto opcional da caixa "Orientação diretiva para análise
    da IA" (compartilhada com o botão "Analisar"), integrada aqui a pedido
    do usuário (2026-09-21)."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if not (SKILL_DIR / "SKILL.md").exists():
        return {"ok": False, "erro": f"Skill não encontrada em {SKILL_DIR}."}

    arquivo_cnis = (arquivo_cnis or "").strip().lstrip("/\\")
    if not arquivo_cnis:
        return {"ok": False, "erro": "Nenhum arquivo de CNIS selecionado."}

    tipo_beneficio = (tipo_beneficio or "").strip()
    if tipo_beneficio not in _ROTULOS_BENEFICIO:
        return {"ok": False, "erro": "Tipo de benefício inválido."}
    data_alvo_rotulo = (data_alvo_rotulo or "").strip()
    if tipo_beneficio == "outro" and not data_alvo_rotulo:
        return {"ok": False, "erro": 'Informe o rótulo da data-alvo (obrigatório para "Outro").'}

    data_alvo = (data_alvo or "").strip()
    if not data_alvo:
        return {"ok": False, "erro": "Informe a data-alvo (fato gerador)."}
    try:
        _dt.datetime.strptime(data_alvo, "%d/%m/%Y")
    except ValueError:
        return {"ok": False, "erro": "Data-alvo inválida — use o formato DD/MM/AAAA."}

    data_alvo_fonte = (data_alvo_fonte or "").strip()

    idade_instituidor = (idade_instituidor or "").strip()
    if idade_instituidor:
        if not idade_instituidor.isdigit() or not (0 < int(idade_instituidor) < 130):
            return {"ok": False, "erro": "Idade do instituidor inválida."}

    orientacao = (orientacao or "").strip()

    # Trava simples contra path traversal — mesmo padrão de calcular_cnis_service.
    caminho_cnis = (pasta / arquivo_cnis).resolve()
    pasta_arquivos = (pasta / "Arquivos").resolve()
    if pasta_arquivos not in caminho_cnis.parents or not caminho_cnis.is_file():
        return {"ok": False, "erro": "Arquivo de CNIS inválido ou fora da pasta do PAJ."}

    existing = _sessions.get(paj_norm)
    if existing and existing.status == "running":
        return {"ok": False, "erro": "Já há uma análise de qualidade de segurado em andamento para este PAJ."}
    if existing:
        existing.stop()

    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session._arquivo_cnis = arquivo_cnis
    session._tipo_beneficio = tipo_beneficio
    session._data_alvo = data_alvo
    session._orientacao = orientacao
    session.status = "running"
    session.last_action = "preparando o contexto (lendo o CNIS selecionado)…"
    _sessions[paj_norm] = session

    def _run() -> None:
        instrucao = _montar_instrucao(
            paj_norm, arquivo_cnis, tipo_beneficio, data_alvo_rotulo, data_alvo,
            data_alvo_fonte, idade_instituidor, orientacao,
        )
        if not session.start_elaboracao(instrucao):
            _persistir(paj_norm, session)
            return
        _aguardar_conclusao(session)
        _persistir(paj_norm, session)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
