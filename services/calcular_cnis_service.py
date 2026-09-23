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
import datetime as _dt
import json
import re
import threading
import time
from pathlib import Path

from config import PAJS_DIR, TIMEOUT_TOTAL
from services import historico
from services.chat_service import ChatSession

SKILL_DIR = Path.home() / ".claude" / "skills" / "AnaliseCNIS"

_STATE_FILE = "calcular_cnis.json"
_sessions: dict[str, ChatSession] = {}

# Data/hora de emissao impressa no cabecalho do CNIS, ex.: "14/01/2026 18:08:48"
# (logo abaixo de "Extrato Previdenciário"/"Relações Previdenciárias"). So'
# procuramos nos primeiros ~600 caracteres da 1a pagina pra nao acertar por
# engano uma data de dentro da tabela de vinculos/competencias.
_REGEX_DATA_EMISSAO = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})")


# Abaixo disso de caracteres na camada de texto NATIVA da 1a pagina, tratamos
# o PDF como escaneado (imagem) mesmo que o fitz devolva algum lixo residual
# (metadado solto, marca d'água) — um cabeçalho de CNIS nativo sempre tem
# muito mais que isso (algumas centenas de caracteres só no cabeçalho).
_MIN_CHARS_TEXTO_NATIVO = 40


def _texto_nativo_pagina1(caminho: Path) -> str:
    """Só a camada de texto NATIVA da 1a página (PyMuPDF), sem fallback de
    OCR — usada para decidir se o PDF é "cru" (baixado direto do INSS/SAT,
    com texto extraível de verdade) ou escaneado (imagem, ilegível por
    programa). PDF escaneado pode ter página faltando, legibilidade
    prejudicada por anotação/resolução etc. — não deve entrar no cálculo
    (pedido explícito do usuário, 2026-09-19)."""
    texto = ""
    with contextlib.suppress(Exception):
        import fitz  # PyMuPDF

        with fitz.open(caminho) as doc:
            texto = doc[0].get_text() if doc.page_count else ""
    return texto


def _e_pdf_nativo(texto_nativo: str) -> bool:
    return len(texto_nativo.strip()) >= _MIN_CHARS_TEXTO_NATIVO


def _texto_pagina1(caminho: Path, texto_nativo: str | None = None) -> str:
    """Texto da 1a página para fins de EXIBIÇÃO (tipo/data de emissão) —
    usa o nativo se houver; senão cai pro `.txt` de OCR companion que o
    sincronizador grava ao lado do PDF (mesmo stem). Mostrar a data/tipo de
    um CNIS escaneado ainda é útil (o Defensor entende POR QUE aquele
    arquivo está desabilitado), mesmo que ele não conte como "nativo"."""
    texto = texto_nativo if texto_nativo is not None else _texto_nativo_pagina1(caminho)
    if texto.strip():
        return texto
    txt_companion = caminho.with_suffix(".txt")
    if txt_companion.exists():
        with contextlib.suppress(Exception):
            conteudo = txt_companion.read_text(encoding="utf-8", errors="replace")
            return conteudo.split("=== pagina 2 ===")[0]
    return ""


def _detectar_tipo_cnis(texto: str) -> str:
    """'completo' se o texto trouxer o título "Extrato Previdenciário" (CNIS
    com remunerações/contribuições por competência); 'simples' se trouxer
    "Relações Previdenciárias" (só vínculos) ou se o texto não deixar claro
    (mais seguro pedir o modo completo manualmente do que travar o modal).
    Ver skill AnaliseCNIS, Passo 1, para os mesmos dois títulos."""
    return "completo" if "extrato previdenciário" in texto.lower() else "simples"


def _data_emissao_cnis(texto: str) -> _dt.datetime | None:
    """Data/hora de emissão do extrato, lida do próprio cabeçalho do CNIS —
    é ela que deve balizar qual arquivo é o mais atual, NÃO a data de
    modificação do arquivo em disco (um CNIS desatualizado pode ter sido
    regravado/resincronizado hoje, o que faria a data do arquivo mentir
    sobre a atualidade do extrato — pedido explícito do usuário, achado no
    PAJ 2025/029-04162, 2026-09-19)."""
    m = _REGEX_DATA_EMISSAO.search(texto[:600])
    if not m:
        return None
    with contextlib.suppress(ValueError):
        return _dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d/%m/%Y %H:%M:%S")
    return None


def listar_arquivos_cnis(paj_norm: str) -> list[dict]:
    """Arquivos em `Arquivos/` cujo nome contenha 'cnis' (case-insensitive) —
    candidatos para o modal de seleção. Não filtra por extensão de documento
    (o Defensor pode ter baixado o extrato em PDF ou, mais raramente, como
    imagem) — mas EXCLUI `.txt`: é o companion de OCR que o sincronizador
    grava ao lado de cada PDF com o MESMO stem (mesmo padrão de
    `paj_service.py`, seção "Anexos SISDPU" — "contamos só o arquivo-fonte,
    não o OCR companion"). Sem essa exclusão, cada CNIS aparecia DUAS vezes
    no modal (o `.pdf` e o `.txt` do seu OCR, com datas de modificação
    diferentes) — achado pelo usuário no PAJ 2025/029-04162 (2026-09-19).

    Cada candidato vem com `tipo` ('simples'/'completo'), `data_emissao`
    (lida do cabeçalho do próprio CNIS, ver `_data_emissao_cnis`) e `nativo`
    (bool — False se o PDF for escaneado/imagem, sem texto extraível de
    verdade: pode ter página faltando, legibilidade prejudicada por
    anotação/resolução etc., então NÃO deve ser usado no cálculo — pedido
    explícito do usuário, 2026-09-19; arquivos baixados direto do SAT
    Central são o exemplo de arquivo "cru" aceitável). A lista é ORDENADA
    pela data de emissão mais recente primeiro (não pela data de
    modificação do arquivo, que não reflete a atualidade real do extrato).
    Arquivos sem data de emissão identificável vão para o final, com
    `data_emissao` nula. O primeiro candidato NATIVO da lista (não
    necessariamente o índice 0, se ele for escaneado) vem marcado
    `mais_recente: true` — é esse que o modal deve destacar/pré-favorecer,
    já que um escaneado não pode ser escolhido de qualquer forma."""
    pasta_arquivos = PAJS_DIR / paj_norm / "Arquivos"
    if not pasta_arquivos.exists() or not pasta_arquivos.is_dir():
        return []
    candidatos = []
    for f in pasta_arquivos.iterdir():
        if f.is_file() and f.suffix.lower() != ".txt" and "cnis" in f.stem.lower():
            texto_nativo = _texto_nativo_pagina1(f)
            nativo = _e_pdf_nativo(texto_nativo)
            texto = _texto_pagina1(f, texto_nativo)
            data_emissao = _data_emissao_cnis(texto)
            candidatos.append(
                {
                    "nome": f.name,
                    "caminho": f"Arquivos/{f.name}",
                    "tamanho": f.stat().st_size,
                    "modificado": f.stat().st_mtime,
                    "data_emissao": data_emissao.isoformat() if data_emissao else None,
                    "data_emissao_fmt": data_emissao.strftime("%d/%m/%Y %H:%M:%S") if data_emissao else None,
                    "_data_emissao_ord": data_emissao,
                    "tipo": _detectar_tipo_cnis(texto),
                    "nativo": nativo,
                }
            )
    candidatos.sort(key=lambda c: (c["_data_emissao_ord"] is None, -(c["_data_emissao_ord"].timestamp() if c["_data_emissao_ord"] else 0)))
    for c in candidatos:
        del c["_data_emissao_ord"]
        c["mais_recente"] = False
    primeiro_nativo = next((c for c in candidatos if c["nativo"]), None)
    if primeiro_nativo is not None:
        primeiro_nativo["mais_recente"] = True
    return candidatos


def dados_assistido(paj_norm: str) -> tuple[str, str]:
    """(cpf, nome) do assistido deste PAJ, lidos de `metadata.json` — usados
    pelo botão "Baixar CNIS Atualizado do SAT" (ver routes/calcular_cnis.py)
    para acionar `ingestao.sat_client.baixar_cidadao_stream` sem precisar
    que o Defensor digite o CPF de novo."""
    p = PAJS_DIR / paj_norm / "metadata.json"
    if not p.exists():
        return "", ""
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    raw = meta.get("sisdpu_raw") or {}
    cpf = (raw.get("cpf_assistido") or "").strip()
    nome = (meta.get("assistido_caixa") or raw.get("assistido") or "").strip()
    return cpf, nome


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
            "sexo": getattr(sess, "_sexo", ""),
            "der": getattr(sess, "_der", ""),
            "orientacao": getattr(sess, "_orientacao", ""),
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


def _montar_instrucao(
    paj_norm: str, arquivo_cnis: str, modo: str, sexo: str, der: str, orientacao: str = ""
) -> str:
    pasta = PAJS_DIR / paj_norm
    caminho_cnis = pasta / arquivo_cnis
    pasta_pecas_ia = pasta / "Peças IA"

    if modo == "completo":
        bloco_modo = (
            "## Modo escolhido: COMPLETO (com análise de remunerações)\n\n"
            "O Defensor escolheu o **modo completo** no modal — este CNIS tem "
            "remunerações/contribuições por competência. Siga também os Passos 2.5 e 2.6 "
            "da skill (Regra 1 e Regra 2 de competências abaixo do salário mínimo, "
            "incluindo o cruzamento obrigatório com \"Valores Consolidados por Ano "
            "Civil\" para a Regra 1), além dos passos do cálculo simples. O Passo 2.4 "
            "(Regra 3 — intercalação de benefício por incapacidade) se aplica sempre, "
            "independente do modo.\n\n"
        )
    else:
        bloco_modo = (
            "## Modo escolhido: SIMPLES (sem análise de remunerações)\n\n"
            "O Defensor escolheu o **modo simples** no modal — mesmo que este CNIS "
            "tenha remunerações/contribuições por competência, NÃO leia essas "
            "subtabelas nem aplique os Passos 2.5/2.6 da skill; use só a tabela de "
            "vínculos, exatamente como no cálculo simples. O Passo 2.4 (Regra 3 — "
            "intercalação de benefício por incapacidade) continua se aplicando "
            "normalmente mesmo no modo simples — não pule esse passo.\n\n"
        )

    # Orientação diretiva do Defensor (caixa compartilhada ao lado do botão
    # "Analisar", ver paj_detail.html) — integrada também a este botão a
    # pedido do usuário (2026-09-21). Mesma força/redação de
    # analise_pretensao_service.py, MAS com uma ressalva que aquele não
    # precisa: este cálculo tem regras matemáticas inafastáveis (Regra 1/2,
    # escopo restrito ao CNIS + campos do modal) que a orientação NÃO pode
    # sobrescrever — ela serve pra guiar interpretação/foco/vínculos CTPS,
    # não pra alterar o resultado do motor de cálculo.
    bloco_orientacao = ""
    if orientacao:
        bloco_orientacao = (
            "## Orientação diretiva do Defensor (leia com atenção — é uma decisão já "
            "tomada pelo Defensor sobre o caso, não uma sugestão a ser questionada)\n\n"
            f'"{orientacao}"\n\n'
            "Direcione o cálculo/parecer conforme essa orientação (ex.: foco em um "
            "vínculo específico, vínculo extra a considerar via CTPS, ênfase da "
            "conclusão). **Ela não pode sobrescrever as regras matemáticas "
            "inafastáveis da skill** (Regras 1/2 de competência abaixo do salário "
            "mínimo, escopo restrito ao CNIS + Sexo/DER informados no modal) — se a "
            "orientação pedir algo que contradiga essas regras, siga as regras e "
            "sinalize o conflito nas Observações do PDF.\n\n"
        )

    return (
        f"Calcule o tempo de contribuição, a carência e a idade nos marcos "
        f"temporais do assistido do PAJ {paj_norm}, seguindo INTEGRALMENTE a "
        f"skill `AnaliseCNIS` (leia `{SKILL_DIR / 'SKILL.md'}` primeiro).\n\n"
        + bloco_orientacao
        + "## Arquivo do CNIS a utilizar (OBRIGATÓRIO — não procure outro)\n\n"
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
        f"`Calculo TC - {modo} - <nome do segurado em maiúsculas>.pdf`.\n\n"
        f'Inclua `"paj_norm": "{paj_norm}"` no JSON de entrada do script (Passo 4 da '
        "skill) — aparece no cabeçalho do PDF como \"Número do PAJ\".\n\n"
        "## Sexo e DER — ÚNICA fonte válida para esses dois dados (ver skill, "
        "Passo 3)\n\n"
        "Os campos abaixo, informados pelo Defensor na seção \"Dados complementares\" "
        "do modal, são a ÚNICA fonte aceitável para sexo e DER. NÃO leia o CNIS para "
        "extrair sexo ou DER, mesmo que o arquivo traga essa informação de forma clara "
        "— use exclusivamente o que está informado aqui.\n\n"
        + (
            f"- **Sexo**: {sexo} (o Defensor selecionou esta opção no modal).\n"
            if sexo
            else "- **Sexo**: o Defensor deixou o campo em branco — use "
            '"Não informado".\n'
        )
        + (
            f"- **DER**: {der} (o Defensor digitou esta data no modal) — use "
            "`der_informada: true`.\n\n"
            if der
            else "- **DER**: o Defensor deixou o campo em branco — use "
            "`der_informada: false` (DER = data do cálculo).\n\n"
        )
        + "Data de nascimento continua vindo sempre do próprio CNIS (isso não mudou). "
        "NÃO busque sexo nem DER em nenhuma outra fonte — nem no CNIS, nem na "
        "narrativa, nem nas movimentações, nem em qualquer outra peça do PAJ.\n\n"
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
        "sexo": getattr(session, "_sexo", ""),
        "der": getattr(session, "_der", ""),
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
            "calcular_cnis",
            status=session.status,
            resumo=historico.primeira_linha_util(session.summary),
        )

    # Lembrete vermelho ao concluir com sucesso — mesmo padrão de
    # organizar_ajuizamento_service (o cálculo roda em background e pode
    # levar minutos; sem isso não há sinal nenhum na tela se o Defensor não
    # ficou acompanhando o polling). Best-effort: nunca deve derrubar a
    # persistência do resultado principal.
    if session.status == "done":
        with contextlib.suppress(Exception):
            from services import lembretes_service

            lembretes_service.criar_lembrete(
                paj_norm, "Cálculo de TC salvo na pasta", "vermelho"
            )


def _aguardar_conclusao(session: ChatSession, timeout: int = TIMEOUT_TOTAL) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(
    paj_norm: str,
    arquivo_cnis: str,
    modo: str = "simples",
    sexo: str = "",
    der: str = "",
    orientacao: str = "",
) -> dict:
    """`modo`: 'simples' ou 'completo' — escolhido no modal quando o arquivo
    detectado é um CNIS completo (ver calcular_cnis.js); irrelevante (mas
    inofensivo) se o CNIS for simples, que só sabe fazer o cálculo simples.

    `sexo`/`der`: informados objetivamente pelo Defensor nos campos do modal
    (dropdown Sexo, caixa de texto DER) — única fonte além do próprio CNIS
    que a skill aceita para esses dois dados (pedido do usuário, 2026-09-20,
    depois de eliminarmos qualquer busca da IA fora do CNIS: em vez de a IA
    procurar em outro lugar, o Defensor informa direto). Vazios = "não
    informado(a)", mesmo comportamento de antes desta feature.

    `orientacao`: texto opcional da caixa "Orientação diretiva para análise
    da IA" (compartilhada com o botão "Analisar" — ver paj_detail.html),
    integrada aqui a pedido do usuário (2026-09-21). Guia interpretação, mas
    não sobrescreve as regras matemáticas da skill (ver `_montar_instrucao`)."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if not (SKILL_DIR / "SKILL.md").exists():
        return {"ok": False, "erro": f"Skill não encontrada em {SKILL_DIR}."}

    arquivo_cnis = (arquivo_cnis or "").strip().lstrip("/\\")
    if not arquivo_cnis:
        return {"ok": False, "erro": "Nenhum arquivo de CNIS selecionado."}
    modo = "completo" if modo == "completo" else "simples"

    sexo = (sexo or "").strip()
    if sexo not in ("", "Masculino", "Feminino"):
        return {"ok": False, "erro": "Sexo inválido."}
    der = (der or "").strip()
    if der:
        try:
            _dt.datetime.strptime(der, "%d/%m/%Y")
        except ValueError:
            return {"ok": False, "erro": "DER inválida — use o formato DD/MM/AAAA."}
    orientacao = (orientacao or "").strip()
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
    session._sexo = sexo
    session._der = der
    session._orientacao = orientacao
    session.status = "running"
    session.last_action = "preparando o contexto (lendo o CNIS selecionado)…"
    _sessions[paj_norm] = session

    def _run() -> None:
        instrucao = _montar_instrucao(paj_norm, arquivo_cnis, modo, sexo, der, orientacao)
        if not session.start_elaboracao(instrucao):
            _persistir(paj_norm, session)
            return
        _aguardar_conclusao(session)
        _persistir(paj_norm, session)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
