"""Análise IA LEVE dos Atendimentos de Retorno (via Claude CLI, best-effort).

Recebe a narrativa do PAJ, as últimas movimentações e a descrição integral do
trâmite de "Atendimento de Retorno" que caiu na caixa de entrada, e pede ao
Claude:

  - resumo: explicação do atual andamento do PAJ e do contexto do retorno;
  - precisa_acao: se há (ou não) medida a ser tomada pelo defensor;
  - sugestao: mera conclusão do trâmite (quando nada a fazer) OU o que fazer,
    em texto editável pelo usuário.

Espelha o padrão do PAINEL COMUNICAÇÕES (services/analise_service.py): invoca o
`claude` local por subprocess, prompt via STDIN, `--output-format json`. Tarefa de
PURO TEXTO — sem ferramentas, sem bypassPermissions. Qualquer falha retorna None.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]

RETORNOS_ATIVA = os.getenv("RETORNOS_IA_ATIVA", "1") not in ("0", "false", "False", "")
RETORNOS_MODELO = os.getenv("RETORNOS_IA_MODELO", "sonnet")
RETORNOS_TIMEOUT_SEG = int(os.getenv("RETORNOS_IA_TIMEOUT_SEG", "180"))
# Nº de movimentações mais recentes enviadas à IA (o histórico guarda 30; aqui só
# limitamos o CONTEXTO do prompt — menos movs = prompt menor = inferência mais rápida).
RETORNOS_MOVS = int(os.getenv("RETORNOS_IA_MOVS", "15"))
# Análises simultâneas (processos `claude -p` concorrentes) na sincronização.
RETORNOS_CONCORRENCIA = max(1, int(os.getenv("RETORNOS_IA_CONCORRENCIA", "4")))


# ─── resolução do binário claude (portado do chat_service/analise_ia_service) ──
def _resolver_claude_cmd() -> list[str]:
    resolved = shutil.which("claude")
    if not resolved and sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        localapp = os.environ.get("LOCALAPPDATA", "")
        for cand in (
            Path(localapp) / "AnthropicClaude" / "claude.exe" if localapp else None,
            Path(appdata) / "npm" / "claude.cmd" if appdata else None,
        ):
            if cand and cand.exists():
                resolved = str(cand)
                break
    if not resolved:
        return ["claude"]
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]
    return [resolved]


CLAUDE_CMD: list[str] = _resolver_claude_cmd()


def claude_instalado() -> bool:
    return shutil.which("claude") is not None


def disponivel() -> bool:
    """True quando a IA pode ser usada: master switch (RETORNOS_IA_ATIVA) e o
    `claude` acessível no PATH. Sem toggle de usuário — a aba Retornos sempre
    tenta gerar a análise ao sincronizar (o usuário pode editar depois)."""
    return RETORNOS_ATIVA and claude_instalado()


# ─── prompt ────────────────────────────────────────────────────────────────
_ANTI_ALUCINACAO = (
    "REGRA ABSOLUTA E INEGOCIÁVEL — LEIA ANTES DE TUDO:\n"
    "NUNCA invente, presuma, deduza ou alucine qualquer informação. Use "
    "EXCLUSIVAMENTE os dados EXPLÍCITOS no material fornecido abaixo. NÃO crie "
    "nem 'complete' números de processo, chaves, datas, prazos, nomes, valores "
    "ou quaisquer fatos que não estejam escritos no material. Se uma informação "
    "necessária NÃO estiver clara e expressa, declare que não foi encontrada em "
    "vez de arriscar. Na dúvida, não preencha.\n\n"
)

_INSTRUCOES = (
    "Você auxilia defensores públicos federais na gestão de ATENDIMENTOS DE "
    "RETORNO de PAJs (Processos de Assistência Jurídica) na Defensoria Pública "
    "da União. Um atendimento de retorno é uma tramitação que retorna o PAJ ao "
    "defensor para que ele avalie se ainda há alguma providência a ser tomada ou "
    "se o trâmite pode ser simplesmente concluído.\n\n"
    "Você recebe: (A) a narrativa do PAJ, (B) as últimas movimentações e (C) a "
    "descrição integral do trâmite de atendimento de retorno.\n\n"
    "Faça DUAS tarefas e responda em JSON:\n"
    "1. \"resumo\": estruture em DOIS blocos separados por uma LINHA EM BRANCO "
    "(use \\n\\n entre eles):\n"
    "   - PRIMEIRO PARÁGRAFO (síntese): 1-2 frases que SINTETIZEM o que é ESTE "
    "atendimento de retorno e sua conclusão — por exemplo, se corresponde a um "
    "comparecimento meramente informativo, sem fato novo ou documento que exija "
    "análise/atuação do defensor, ou se traz alguma demanda/documento a tratar. É a "
    "conclusão objetiva sobre o retorno (ex.: 'O atendimento de retorno corresponde a "
    "um comparecimento de caráter meramente informativo, sem apresentação de fato novo "
    "ou documento que exija análise do defensor.').\n"
    "   - DEMAIS PARÁGRAFOS: exponha o restante — o andamento atual do PAJ, o que "
    "motivou o retorno e o contexto, a partir da narrativa e das últimas movimentações. "
    "Seja concreto. Se algo não puder ser identificado, diga que deve ser verificado "
    "manualmente.\n"
    "2. \"sugestao\": redija um DESPACHO DO DEFENSOR — escreva na 1ª pessoa, como se "
    "VOCÊ FOSSE O DEFENSOR dando o encaminhamento necessário a este PAJ. Este texto "
    "será usado LITERALMENTE como a descrição da movimentação do PAJ. Regras do "
    "despacho:\n"
    "   - Seja o MAIS SUCINTO POSSÍVEL quando não houver providência a tomar. Nesses "
    "casos, o despacho é apenas um registro de ciência e determinação de que se "
    "aguarde, e VOCÊ deve retornar \"precisa_acao\": false. Exemplos de despacho de "
    "mera conclusão (adapte ao caso concreto):\n"
    "       'Ciente das informações. Aguarde-se tramitação do PAJ.'\n"
    "       'Ciente das informações processuais passadas ao assistido. Aguarde-se "
    "tramitação do PAJ.'\n"
    "       'Ciente das orientações passadas ao assistido pelo setor de atendimento. "
    "Aguarde-se tramitação do PAJ.'\n"
    "   - Quando HOUVER providência concreta a tomar, retorne \"precisa_acao\": true "
    "e redija um DESPACHO começando com 'DESPACHO:' seguido do encaminhamento "
    "específico e, quando cabível, do prazo. Use verbos no imperativo impessoal "
    "(Aguarde-se, Oficie-se, Intime-se, Solicite-se, Expeça-se). Exemplos (adapte, "
    "usando APENAS dados reais do material):\n"
    "       'DESPACHO: Considerando a informação do último retorno, aguarde-se "
    "retorno do assistido com cópia do prontuário médico e com formulário de "
    "informações médicas da DPU. Prazo de 15 dias.'\n"
    "       'DESPACHO: Diante das informações prestadas no último retorno, oficie-se "
    "à Secretaria Municipal de Saúde de [município] para solicitar extrato com "
    "períodos de internação do assistido [NOME], a fim de instruir o PAJ. Prazo de "
    "15 dias.'\n"
    "   - NÃO invente prazos, nomes, órgãos ou dados: se um dado necessário ao "
    "despacho não estiver no material, deixe a lacuna evidente para o defensor "
    "preencher (ex.: '[verificar: município]') em vez de inventar.\n"
    "   - O campo \"precisa_acao\" (booleano) deve refletir a natureza do despacho: "
    "false para mera conclusão/ciência; true quando o despacho determina uma "
    "providência.\n\n"
    "SOMENTE TEXTO: não use emojis nem símbolos gráficos. Dentro dos VALORES do "
    "JSON, NUNCA use aspas duplas (\") — use aspas simples ('') se precisar citar. "
    "Aspas duplas não escapadas quebram o JSON.\n"
    "Responda APENAS com um objeto JSON válido, sem cercas de código, no formato: "
    '{"resumo": "...", "precisa_acao": true|false, "sugestao": "..."}\n\n'
)


# ─── helpers de saneamento / parsing (portados de analise_service) ───────────
def _colapsar(s: str) -> str:
    """Remove linhas em branco entre parágrafos e espaços supérfluos."""
    if not s:
        return ""
    linhas = [ln.strip() for ln in s.replace("\r", "").split("\n")]
    linhas = [ln for ln in linhas if ln != ""]
    return "\n".join(linhas).strip()


def _paragrafos(s: str) -> str:
    """Preserva a estrutura de parágrafos (síntese + detalhe do resumo): mantém UMA
    linha em branco entre blocos, colapsando 3+ quebras em 2 e aparando espaços."""
    if not s:
        return ""
    s = s.replace("\r", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = "\n".join(ln.rstrip() for ln in s.split("\n"))
    return s.strip()


def _remover_emojis(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in s if c.isprintable() or c in "\n\t").strip()


def _desescapar_json(s: str) -> str:
    return (
        s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "")
         .replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")
    ).strip()


def _extrair_campos_tolerante(t: str) -> dict | None:
    campos: dict = {}
    mr = re.search(r'"resumo"\s*:\s*"(.*?)"\s*,\s*"(?:precisa_acao|sugestao)"\s*:', t, re.DOTALL)
    if mr:
        campos["resumo"] = _desescapar_json(mr.group(1))
    mp = re.search(r'"precisa_acao"\s*:\s*(true|false)', t, re.IGNORECASE)
    if mp:
        campos["precisa_acao"] = mp.group(1).lower() == "true"
    ms = re.search(r'"sugestao"\s*:\s*"(.*)"\s*\}?\s*$', t, re.DOTALL)
    if ms:
        campos["sugestao"] = _desescapar_json(ms.group(1))
    return campos if (campos.get("resumo") or campos.get("sugestao")) else None


def _extrair_json(texto: str) -> dict | None:
    if not texto:
        return None
    t = texto.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t).strip()
    with contextlib.suppress(Exception):
        return json.loads(t)
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        with contextlib.suppress(Exception):
            return json.loads(m.group(0))
    return _extrair_campos_tolerante(t)


# ─── chamada ao Claude ───────────────────────────────────────────────────────
def _run_claude(prompt: str) -> str | None:
    prompt = _ANTI_ALUCINACAO + prompt
    cmd = [*CLAUDE_CMD, "-p", "--output-format", "json"]
    if RETORNOS_MODELO:
        cmd += ["--model", RETORNOS_MODELO]
    try:
        proc = subprocess.run(
            cmd, input=prompt.encode("utf-8"), capture_output=True,
            timeout=RETORNOS_TIMEOUT_SEG, cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        log.warning("[retornos-ia] timeout ao chamar o Claude")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("[retornos-ia] falha ao invocar claude: %s", e)
        return None
    saida = proc.stdout.decode("utf-8", errors="replace").strip()
    if not saida:
        return None
    try:
        env = json.loads(saida)
    except json.JSONDecodeError:
        env = {"result": saida}
    if env.get("is_error"):
        return None
    result = env.get("result")
    if isinstance(result, dict):
        result = result.get("text", "")
    result = (result or "").strip()
    if any(s in result.lower() for s in ("session limit", "usage limit", "hit your")):
        log.warning("[retornos-ia] cota de uso atingida")
        return None
    return result


def analisar_retorno(
    narrativa: str,
    movimentacoes: list[dict],
    descricao_retorno: str = "",
    assistido: str = "",
    pretensao: str = "",
    prompt_extra: str = "",
) -> dict | None:
    """Gera {resumo, precisa_acao, sugestao} para um atendimento de retorno.

    Retorna None quando a IA está indisponível ou a chamada falha (o painel
    exibe o retorno sem análise e permite gerar depois)."""
    if not disponivel():
        return None

    linhas_mov = []
    for m in movimentacoes[:RETORNOS_MOVS]:
        desc = (m.get("descricao_retorno") or m.get("descricao") or "").strip()[:1500]
        linhas_mov.append(
            f"[seq {m.get('seq', '')}] {m.get('data', '')} · {m.get('movimentacao', '')}"
            f"{' · ' + m['fases'] if m.get('fases') else ''}\n{desc}"
        )
    bloco_mov = "\n\n".join(linhas_mov) if linhas_mov else "(sem movimentações extraídas)"

    ctx = []
    if assistido:
        ctx.append(f"Assistido: {assistido}")
    if pretensao:
        ctx.append(f"Pretensão: {pretensao}")
    ctx_txt = ("\n".join(ctx) + "\n\n") if ctx else ""

    desc_ret = (descricao_retorno or "").strip()[:2500]
    bloco_ret = (
        f"(C) DESCRIÇÃO INTEGRAL DO ATENDIMENTO DE RETORNO:\n{desc_ret}\n\n"
        if desc_ret else
        "(C) DESCRIÇÃO INTEGRAL DO ATENDIMENTO DE RETORNO: (não localizada)\n\n"
    )

    narr = (narrativa or "").strip()[:4000]
    bloco_narr = (
        f"(A) NARRATIVA DO PAJ:\n{narr}\n\n" if narr
        else "(A) NARRATIVA DO PAJ: (não disponível)\n\n"
    )

    prompt = (
        _INSTRUCOES
        + ctx_txt
        + bloco_narr
        + bloco_ret
        + "(B) MOVIMENTAÇÕES (mais recentes primeiro):\n"
        + bloco_mov
    )
    extra = (prompt_extra or "").strip()
    if extra:
        prompt += (
            "\n\n=== INSTRUÇÕES ADICIONAIS DO DEFENSOR PARA ESTA REANÁLISE ===\n"
            "Estas instruções têm PRIORIDADE quando houver conflito com as gerais, "
            "mas MANTENHA o formato JSON de saída e o despacho na 1ª pessoa do defensor:\n"
            + extra
        )

    result = _run_claude(prompt)
    if result is None:
        return None
    obj = _extrair_json(result)
    if not obj:
        return {"resumo": result[:600], "precisa_acao": True, "sugestao": result}

    precisa = obj.get("precisa_acao")
    if isinstance(precisa, str):
        precisa = precisa.strip().lower() in ("true", "1", "sim", "yes")
    return {
        "resumo": _paragrafos((obj.get("resumo") or "").strip()),
        "precisa_acao": bool(precisa),
        "sugestao": _colapsar(_remover_emojis(obj.get("sugestao") or "")),
    }
