"""Botão "Calcular Valor da Causa" (seção "Ações com IA" > "Cálculos" da
página do PAJ).

Fluxo completo (ver routes/calcular_valor_causa.py e
static/js/calcular_valor_causa.js):

  1. Defensor escolhe, num modal, a petição inicial já salva em "Peças IA".
  2. `iniciar()` dispara uma sessão real do Claude Code CLI (headless) que LÊ
     a petição e os documentos do PAJ para extrair os parâmetros do cálculo —
     essa sessão está EXPRESSAMENTE PROIBIDA de alterar a petição de qualquer
     forma (nem texto, nem formatação, nem salvar versão nova). Ela só grava
     `valor_causa_extraido.json` (NUNCA `valor_causa.json` — esse nome é o
     gatilho que `chat_service._rodar_valor_causa` dispara automaticamente ao
     fim de QUALQUER ChatSession, o que calcularia e injetaria na petição
     ORIGINAL antes mesmo da conferência abaixo; ver `_JSON_EXTRAIDO`).
     Campos que não conseguir determinar com segurança entram em `faltando`
     (nunca são adivinhados).
  3. Ao terminar, o painel abre um modal "Confira os parâmetros" com tudo que
     foi extraído, pré-preenchido e editável — os campos de `faltando` vêm
     destacados. O cálculo NÃO prossegue sem os campos mínimos preenchidos e
     confirmados explicitamente pelo Defensor (`confirmar()`).
  4. Confirmado, o motor determinístico (`valor_causa_service.processar`, com
     `injetar_na_peticao=False` — a petição original nunca é tocada aqui)
     calcula de verdade e gera o PDF `CALC VALOR DA CAUSA.pdf`.
  5. O painel abre um modal de resultado com as premissas usadas e o PDF
     gerado, para conferência do Defensor.
  6. Botão "Inserir Valor da Causa na Inicial" (`inserir()`), nesse modal:
     grava uma CÓPIA da petição (docx + pdf, nome fixo
     "Petição Inicial_comValorDaCausa") com o valor numérico e por extenso em
     NEGRITO no campo "Dá-se à causa o valor de..." — a petição original
     nunca é sobrescrita.

Arquitetura: os passos 4 e 6 dependem de python-docx + Word (win32com), que o
venv do painel não tem — o passo 4 reaproveita o mesmo runner em subprocess de
`valor_causa_service.py`; o passo 6 usa um runner próprio
(`inserir_valor_causa_runner.py`, em VALOR_CAUSA_DIR).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os as _os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from config import PAJS_DIR, TIMEOUT_TOTAL, VALOR_CAUSA_DIR
from services import historico
from services import valor_causa_service as vcs
from services.chat_service import ChatSession
from services.valor_causa_service import _ESQUEMA_JSON_VALOR_CAUSA, _PLACEHOLDER, _sem_acento_upper

_STATE_FILE = "calcular_valor_causa.json"
_RUNNER_INSERIR = Path(VALOR_CAUSA_DIR) / "inserir_valor_causa_runner.py"
_NOME_SAIDA_INSERCAO = "Petição Inicial_comValorDaCausa"

# NUNCA "valor_causa.json" aqui — esse nome é o gatilho que
# chat_service._rodar_valor_causa procura (disparado automaticamente ao fim de
# QUALQUER ChatSession com `encerrar_ao_concluir=True`, flag que esta sessão de
# extração também usa). Se a extração escrevesse nesse nome, o hook automático
# calcularia e INJETARIA NA PETIÇÃO ORIGINAL assim que a sessão terminasse —
# antes do Defensor sequer ver o modal de confirmação, violando a leitura
# somente-leitura e a exigência de confirmação explícita. `confirmar()` é quem
# grava o `valor_causa.json` de verdade, só depois da conferência — nesse
# ponto não há ChatSession nenhuma rodando, então o hook automático não entra
# em jogo (achado ao vivo: PAJ 2026/029-03500 teve a petição alterada por esse
# caminho antes desta correção).
_JSON_EXTRAIDO = "valor_causa_extraido.json"

# Estado em memória por PAJ — único "dono" da verdade enquanto o processo do
# painel está de pé; `_persistir` espelha no disco a cada mudança de fase.
# Campos: status, arquivo_peticao, parametros, faltando, error,
# resultado_calc, resultado_insercao, session_id, atualizado_em.
# `status` percorre: extraindo -> aguardando_confirmacao -> calculando ->
# aguardando_revisao  (erro pode interromper em qualquer fase).
_estado: dict[str, dict] = {}
# ChatSession ativa (só existe durante a fase "extraindo" — é a única fase que
# usa IA; as demais são código determinístico puro).
_chat_sessions: dict[str, ChatSession] = {}

_INSTRUCAO_IDIOMA = (
    "IMPORTANTE sobre idioma: toda a sua comunicação nesta tarefa deve ser em "
    "português do Brasil, inclusive comentários breves de passagem entre a "
    "leitura de um documento e outro. Não narre nem pense em voz alta em "
    "inglês em nenhum momento.\n\n"
)

# Nomes de campo usados tanto no JSON quanto no modal de confirmação do
# frontend — mantidos em sincronia de propósito (ver `_ESQUEMA_JSON_VALOR_CAUSA`
# em valor_causa_service.py, que define o mesmo vocabulário).
_CAMPOS_VALIDOS_FALTANDO = (
    "tipo", "valor_mensal", "marco", "data_marco", "meses_vincendos",
    "nb", "especie", "nome", "periodos", "recebido", "sem_13",
)


def listar_peticoes(paj_norm: str) -> list[dict]:
    """Candidatos a "petição inicial" em `Peças IA/` para o modal de seleção —
    mesma heurística de `valor_causa_service._localizar_peticao` (nome ou
    conteúdo sugerindo petição inicial), devolvendo metadados pro modal em vez
    de só os caminhos. Ordenados pelo mais recente primeiro.

    Exclui explicitamente a própria saída de `inserir()`
    (`_NOME_SAIDA_INSERCAO` — "Petição Inicial_comValorDaCausa") e o PDF do
    cálculo: o nome bate na heurística de "parece petição inicial" (contém
    "PETIÇÃO"/"INICIAL"), mas é um arquivo GERADO, não uma fonte — se
    escolhido por engano (ele some do topo da lista por ser o mais recente),
    `inserir()` tentaria reabrir uma cópia que pode já ter sido apagada/
    sobrescrita, em vez do original (achado ao vivo, PAJ 2026/029-03088)."""
    pasta = PAJS_DIR / paj_norm / "Peças IA"
    if not pasta.exists() or not pasta.is_dir():
        return []
    nome_saida_norm = _sem_acento_upper(_NOME_SAIDA_INSERCAO)
    candidatos: list[Path] = []
    for f in pasta.iterdir():
        if not f.is_file():
            continue
        nome = _sem_acento_upper(f.name)
        if nome.startswith(nome_saida_norm) or nome.startswith("CALC VALOR DA CAUSA"):
            continue
        parece_pi = ("PETICAO" in nome) or ("INICIAL" in nome) or nome.startswith("PI ")
        if f.suffix.lower() == ".txt":
            conteudo = ""
            with contextlib.suppress(Exception):
                conteudo = f.read_text(encoding="utf-8", errors="replace")
            if parece_pi or _PLACEHOLDER in conteudo or "EXCELENTISSIM" in _sem_acento_upper(conteudo):
                candidatos.append(f)
        elif f.suffix.lower() == ".docx" and parece_pi:
            candidatos.append(f)
    candidatos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [
        {
            "nome": f.name,
            "caminho": f"Peças IA/{f.name}",
            "tamanho": f.stat().st_size,
            "modificado": f.stat().st_mtime,
        }
        for f in candidatos
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
    # Prioriza o estado em memória (fonte da verdade enquanto o processo está
    # de pé) — evita a janela de "idle" fantasma entre uma fase terminar e o
    # disco ser gravado. Cai pro disco só se o painel foi reiniciado.
    em_memoria = _estado.get(paj_norm)
    if em_memoria:
        return em_memoria
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {
        "status": "idle", "arquivo_peticao": "", "parametros": None,
        "faltando": [], "error": "", "resultado_calc": None, "resultado_insercao": None,
    }


def _persistir(paj_norm: str) -> None:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return
    estado = dict(_estado.get(paj_norm) or {})
    estado["atualizado_em"] = _dt.datetime.now().isoformat(timespec="minutes")
    with contextlib.suppress(Exception):
        _arquivo_estado(paj_norm).write_text(
            json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with contextlib.suppress(Exception):
        resumo = {
            "extraindo": "lendo a petição e os documentos…",
            "aguardando_confirmacao": "aguardando confirmação dos parâmetros",
            "calculando": "calculando…",
            "aguardando_revisao": f"calculado: {(estado.get('resultado_calc') or {}).get('valor_causa', '')}",
            "erro": f"erro: {estado.get('error', '')}",
        }.get(estado.get("status"), estado.get("status", ""))
        historico.registrar(paj_norm, "calcular_valor_causa", status=estado.get("status", ""), resumo=resumo)

    if estado.get("status") == "aguardando_revisao":
        with contextlib.suppress(Exception):
            from services import lembretes_service

            lembretes_service.criar_lembrete(
                paj_norm, "Valor da causa calculado — confira os parâmetros e o PDF", "vermelho"
            )


def _ler_params_extraidos(paj_norm: str) -> dict | None:
    caminho = PAJS_DIR / paj_norm / "Peças IA" / _JSON_EXTRAIDO
    if not caminho.exists():
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        return None


def _montar_instrucao_extracao(paj_norm: str, arquivo_peticao: str) -> str:
    pasta = PAJS_DIR / paj_norm
    caminho_peticao = pasta / arquivo_peticao
    pasta_pecas_ia = pasta / "Peças IA"

    return (
        f"Leia a petição inicial do PAJ {paj_norm} para extrair os parâmetros "
        "do cálculo do valor da causa. Esta tarefa é SOMENTE LEITURA.\n\n"
        "## PROIBIÇÃO EXPRESSA — não altere a petição, em nenhuma hipótese\n\n"
        f"Você está TERMINANTEMENTE PROIBIDO de alterar, de qualquer forma, o "
        f"arquivo abaixo (ou qualquer outro arquivo já existente na pasta da "
        "petição) — nem o texto, nem a formatação, nem metadados, nem salvar "
        "uma versão nova dele. Não use nenhuma ferramenta de edição de "
        "arquivo nesse caminho; abra-o só para leitura:\n\n"
        f"    {caminho_peticao}\n\n"
        "A inserção do valor da causa na petição é uma etapa SEPARADA e "
        "POSTERIOR, feita pelo painel deterministicamente sobre uma CÓPIA "
        "nova — nunca por você, e nunca sobre o arquivo original.\n\n"
        "## O que mais ler\n\n"
        "Os documentos do PAJ necessários para os parâmetros do cálculo "
        "(Declaração de Benefícios, documentos do INSS, extrato/CNIS etc. — "
        "os mesmos que você consultaria se estivesse redigindo essa petição "
        "agora).\n\n"
        "## O que salvar\n\n"
        "Salve (ou sobrescreva, se já existir) SOMENTE o arquivo "
        f"`{_JSON_EXTRAIDO}` (esse nome exato — NÃO `valor_causa.json`) em "
        f"`{pasta_pecas_ia}\\` com os parâmetros que você extraiu:\n"
        + _ESQUEMA_JSON_VALOR_CAUSA
        + "## Campos que faltam — NÃO ADIVINHE\n\n"
        "Se algum parâmetro necessário não constar claramente dos documentos, "
        "ou você não tiver certeza suficiente, NÃO invente um valor: deixe o "
        'campo como `null` no JSON e acrescente o NOME EXATO do campo '
        '(usando as mesmas chaves do esquema acima — "tipo", "valor_mensal", '
        '"marco", "data_marco", "meses_vincendos", "nb", "especie", "nome", '
        '"periodos", "recebido" ou "sem_13") numa lista `"faltando"`:\n'
        '       "faltando": ["valor_mensal", "data_marco"]\n'
        "O Defensor vai preencher manualmente num formulário antes do "
        "cálculo prosseguir — por isso é sempre melhor sinalizar em "
        '"faltando" do que arriscar um valor errado.\n\n'
        + _INSTRUCAO_IDIOMA
        + "Não me pergunte nada pelo chat — registre toda incerteza em "
        "`faltando` ou no campo `observacao` do próprio JSON, nunca como "
        "pergunta.\n\n"
        "Ao final, me diga em 2-3 linhas quais parâmetros você extraiu e "
        "quais ficaram em `faltando`, se algum."
    )


def _aguardar_conclusao(session: ChatSession, timeout: int = TIMEOUT_TOTAL) -> None:
    inicio = time.time()
    while session.is_alive() and session.status == "running" and time.time() - inicio < timeout:
        time.sleep(0.5)


def iniciar(paj_norm: str, arquivo_peticao: str) -> dict:
    """`arquivo_peticao`: caminho relativo escolhido pelo Defensor no modal
    (ex.: "Peças IA/01 - PI.txt"), vindo de `listar_peticoes`. Dispara a FASE
    1 (extração, só leitura) — a fase 2 (cálculo) só roda depois de
    `confirmar()`."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}

    arquivo_peticao = (arquivo_peticao or "").strip().lstrip("/\\")
    if not arquivo_peticao:
        return {"ok": False, "erro": "Nenhuma petição selecionada."}
    # Trava simples contra path traversal — o arquivo tem que estar
    # literalmente dentro de "Peças IA/" desta pasta, sem ../ no meio.
    caminho_peticao = (pasta / arquivo_peticao).resolve()
    pasta_pecas_ia = (pasta / "Peças IA").resolve()
    if pasta_pecas_ia not in caminho_peticao.parents or not caminho_peticao.is_file():
        return {"ok": False, "erro": 'Petição inválida ou fora da pasta "Peças IA" deste PAJ.'}

    atual = _estado.get(paj_norm)
    if atual and atual.get("status") in ("extraindo", "calculando"):
        return {"ok": False, "erro": "Já há uma etapa em andamento para este PAJ."}
    existing = _chat_sessions.get(paj_norm)
    if existing:
        existing.stop()

    mtime_antes = caminho_peticao.stat().st_mtime

    session = ChatSession(paj_norm)
    session.encerrar_ao_concluir = True
    session.status = "running"
    session.last_action = "lendo a petição inicial e os documentos do PAJ…"
    _chat_sessions[paj_norm] = session
    _estado[paj_norm] = {
        "status": "extraindo", "arquivo_peticao": arquivo_peticao, "parametros": None,
        "faltando": [], "error": "", "resultado_calc": None, "resultado_insercao": None,
        "session_id": "",
    }
    _persistir(paj_norm)

    def _run() -> None:
        instrucao = _montar_instrucao_extracao(paj_norm, arquivo_peticao)
        estado = _estado[paj_norm]
        if not session.start_elaboracao(instrucao):
            estado["status"] = "erro"
            estado["error"] = session.error or "Falha ao iniciar a sessão de extração."
            _persistir(paj_norm)
            return
        _aguardar_conclusao(session)
        estado["session_id"] = session.session_id
        if session.status != "done":
            estado["status"] = "erro"
            estado["error"] = session.error or "Falha ao ler a petição/documentos."
            _persistir(paj_norm)
            return

        # Defesa em profundidade: a instrução proíbe expressamente mexer na
        # petição — se mesmo assim o mtime mudou, algo deu muito errado (ou a
        # IA ignorou a instrução) e é melhor parar tudo com um erro bem
        # visível do que seguir como se nada tivesse acontecido.
        with contextlib.suppress(Exception):
            if caminho_peticao.stat().st_mtime != mtime_antes:
                estado["status"] = "erro"
                estado["error"] = (
                    "A petição foi modificada durante a leitura — isso não deveria "
                    "acontecer (a instrução proíbe expressamente). Confira o "
                    "arquivo e tente novamente."
                )
                _persistir(paj_norm)
                return

        params = _ler_params_extraidos(paj_norm)
        if params is None:
            estado["status"] = "erro"
            estado["error"] = f"{_JSON_EXTRAIDO} não foi gerado pela sessão de extração."
            _persistir(paj_norm)
            return

        faltando = [c for c in (params.pop("faltando", None) or []) if c in _CAMPOS_VALIDOS_FALTANDO]
        estado["status"] = "aguardando_confirmacao"
        estado["parametros"] = params
        estado["faltando"] = faltando
        _persistir(paj_norm)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


def _validar_minimo(params: dict) -> str | None:
    """Confere o mínimo pra rodar o cálculo — mesma regra que o modal usa pra
    habilitar o botão "Calcular" no frontend; refeita aqui no servidor como
    defesa em profundidade (nunca prossegue sem o mínimo, mesmo se o JS
    falhar ou for contornado). Retorna a mensagem de erro, ou None se OK."""
    if not isinstance(params, dict):
        return "Parâmetros inválidos."
    if not params.get("tipo") in ("previdenciario", "assistencial"):
        return "Informe o tipo do benefício (previdenciário ou assistencial)."
    tem_valor = bool(params.get("valor_mensal")) or bool(params.get("piso_sm"))
    if not tem_valor:
        return 'Informe a RMI ou marque "usar salário mínimo vigente".'
    periodos = params.get("periodos") or []
    tem_periodos = bool(periodos) and all(p.get("inicio") and p.get("fim") for p in periodos)
    tem_marco = bool(params.get("marco")) and bool(params.get("data_marco"))
    if not (tem_marco or tem_periodos):
        return "Informe a DER/DCB (marco) ou ao menos um período (início e fim)."
    if params.get("meses_vincendos") in (None, ""):
        return "Informe o número de parcelas vincendas (pode ser 0)."
    return None


def confirmar(paj_norm: str, parametros: dict) -> dict:
    """Fase 2: o Defensor confirmou os parâmetros (preenchidos/editados no
    modal) — grava o `valor_causa.json` final e dispara o cálculo
    determinístico de verdade. `injetar_na_peticao=False`: a petição original
    nunca é tocada por este fluxo (ver `_montar_instrucao_extracao` e
    `inserir`, abaixo)."""
    atual = _estado.get(paj_norm)
    if not atual or atual.get("status") != "aguardando_confirmacao":
        return {"ok": False, "erro": "Não há parâmetros aguardando confirmação para este PAJ."}

    erro = _validar_minimo(parametros)
    if erro:
        return {"ok": False, "erro": erro}

    pasta = PAJS_DIR / paj_norm
    caminho_json = pasta / "Peças IA" / "valor_causa.json"
    with contextlib.suppress(Exception):
        caminho_json.write_text(json.dumps(parametros, ensure_ascii=False, indent=2), encoding="utf-8")

    atual["status"] = "calculando"
    atual["parametros"] = parametros
    _persistir(paj_norm)

    def _run() -> None:
        resultado = vcs.processar(paj_norm, params=parametros, injetar_na_peticao=False)
        atual["resultado_calc"] = resultado
        if resultado.get("ok"):
            atual["status"] = "aguardando_revisao"
        else:
            atual["status"] = "erro"
            atual["error"] = resultado.get("erro") or "Falha ao calcular."
        _persistir(paj_norm)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}


def cancelar(paj_norm: str) -> dict:
    """Botão "Cancelar" do modal "Confira os parâmetros" — desiste do cálculo
    sem rodar nada (volta pro estado inicial). Só faz sentido enquanto nada
    foi calculado de verdade ainda, ou depois de um erro."""
    atual = _estado.get(paj_norm)
    if not atual or atual.get("status") not in ("aguardando_confirmacao", "erro"):
        return {"ok": False, "erro": "Não há nada para cancelar nesta fase."}
    _estado[paj_norm] = {
        "status": "idle", "arquivo_peticao": "", "parametros": None,
        "faltando": [], "error": "", "resultado_calc": None, "resultado_insercao": None,
    }
    _persistir(paj_norm)
    return {"ok": True}


def fechar_revisao(paj_norm: str) -> dict:
    """Botão "Fechar" do modal de resultado — só marca a revisão como
    reconhecida (evita o modal reabrir sozinho a cada recarregamento de
    página); não desfaz nem recalcula nada."""
    atual = _estado.get(paj_norm)
    if not atual or atual.get("status") != "aguardando_revisao":
        return {"ok": False, "erro": "Não há revisão pendente para fechar."}
    atual["status"] = "concluido"
    _persistir(paj_norm)
    return {"ok": True}


def inserir(paj_norm: str) -> dict:
    """Fase 3 (opcional, botão próprio no modal de resultado): grava uma CÓPIA
    da petição ("Petição Inicial_comValorDaCausa.docx"/".pdf", em "Peças IA")
    com o valor numérico + por extenso em NEGRITO no campo "Dá-se à causa o
    valor de..." — a petição original nunca é sobrescrita. Síncrono (bloqueia
    no Word) — mesmo padrão de `valor_causa_service._rodar_runner`, chamado
    por uma rota `def` (não `async def`) que o FastAPI roda em threadpool."""
    atual = _estado.get(paj_norm)
    if not atual or atual.get("status") != "aguardando_revisao":
        return {"ok": False, "erro": "Não há um cálculo concluído aguardando revisão para este PAJ."}
    resultado_calc = atual.get("resultado_calc") or {}
    valor_str = resultado_calc.get("valor_causa")
    if not valor_str:
        return {"ok": False, "erro": "Valor da causa não disponível — recalcule antes de inserir."}

    pasta = PAJS_DIR / paj_norm
    pasta_pecas_ia = pasta / "Peças IA"
    caminho_peticao = pasta / (atual.get("arquivo_peticao") or "")
    # Localiza a versão .docx: o próprio arquivo, se já for .docx, senão um
    # .docx irmão com o mesmo nome-base (petição escolhida pode ter sido o
    # .txt companion).
    docx_path = caminho_peticao if caminho_peticao.suffix.lower() == ".docx" else caminho_peticao.with_suffix(".docx")
    if not docx_path.is_file():
        return {
            "ok": False,
            "erro": f'Não encontrei a versão .docx da petição ("{docx_path.name}") — gere o '
                    ".docx antes de inserir o valor da causa.",
        }

    interp = vcs._interpretador()
    if not interp:
        return {"ok": False, "erro": "Nenhum Python com python-docx + pywin32 (Word) encontrado."}

    saida_docx = pasta_pecas_ia / f"{_NOME_SAIDA_INSERCAO}.docx"
    saida_pdf = pasta_pecas_ia / f"{_NOME_SAIDA_INSERCAO}.pdf"
    job = {
        "docx_path": str(docx_path), "valor_str": valor_str,
        "saida_docx": str(saida_docx), "saida_pdf": str(saida_pdf),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(job, tf, ensure_ascii=False)
        job_path = tf.name
    env = {**_os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(
            [interp, str(_RUNNER_INSERIR), job_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, env=env,
        )
    finally:
        with contextlib.suppress(Exception):
            Path(job_path).unlink()

    linha = ""
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("RESULT_JSON:"):
            linha = ln[len("RESULT_JSON:"):]
    if not linha:
        return {"ok": False, "erro": f"Runner sem saída válida (rc={r.returncode}). {(r.stderr or '')[-300:]}"}
    try:
        saida = json.loads(linha)
    except Exception:
        return {"ok": False, "erro": "Falha ao interpretar a saída do runner de inserção."}

    if saida.get("ok"):
        atual["resultado_insercao"] = saida
        _persistir(paj_norm)
        with contextlib.suppress(Exception):
            historico.registrar(
                paj_norm, "calcular_valor_causa", status="inserido",
                resumo=f"Valor da causa inserido em \"{saida_docx.name}\"",
            )
    return saida
