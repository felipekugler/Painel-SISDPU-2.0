"""Cálculo automático do valor da causa — sub-passo da geração da petição inicial.

Roda como pós-passo AUTOMÁTICO do fluxo de PI: quando a skill emite um
`valor_causa.json` com os parâmetros das parcelas, o painel:

  1. deriva o `tipo` (previdenciário/assistencial) da pretensão do PAJ;
  2. calcula deterministicamente dias/parcelas (o LLM não faz a aritmética);
  3. gera o PDF `CALC VALOR DA CAUSA.pdf` NA MESMA PASTA da petição;
  4. injeta o total no texto da PI (placeholder `{{VALOR_CAUSA}}`), no `.txt` e
     no `.docx`;
  5. grava `valor_causa_result.json` com totais, premissas e avisos.

Arquitetura: o cálculo em si (calc_valor_causa) exige python-docx + Word
(win32com), que o venv do painel NÃO tem de propósito. Por isso a geração do PDF
e a injeção no docx rodam num SUBPROCESS (`valor_causa_runner.py`) sob o Python
do sistema. Este módulo (no venv do painel) só deriva parâmetros — puro stdlib +
`indices_cm` (que é pure-python) — e orquestra o runner.

Determinismo: a contagem de dias do primeiro mês de atrasados e a data do
ajuizamento (= hoje, data do protocolo) são fixadas AQUI, não pela skill. Como o
fluxo é automático (sem modal), o resultado registra as premissas usadas e
avisos não-bloqueantes (valor presumido = salário mínimo; índice do mês ainda não
publicado) para conferência do Defensor.
"""

from __future__ import annotations

import calendar
import contextlib
import datetime as _dt
import json
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from config import PAJS_DIR, SALARIO_MINIMO, VALOR_CAUSA_DIR, VALOR_CAUSA_PYTHON

_PLACEHOLDER = "{{VALOR_CAUSA}}"
_JSON_SKILL = "valor_causa.json"          # emitido pela skill (parâmetros)
_JSON_RESULT = "valor_causa_result.json"  # gravado pelo painel (resultado)
_RUNNER = Path(__file__).parent / "valor_causa_runner.py"

# Pretensões assistenciais (BPC/LOAS) -> IPCA-E, sem 13º, valor = salário mínimo.
# Qualquer outra pretensão previdenciária -> INPC, com 13º.
_PALAVRAS_ASSISTENCIAL = ("BPC", "LOAS", "ASSISTENCIAL", "PRESTACAO CONTINUADA")

# Instrução TRANSVERSAL anexada a QUALQUER ação do painel que possa redigir uma
# petição inicial (Elaborar genérico, análise por pretensão, etc.). Manda a skill
# (1) emitir `valor_causa.json` com os PARÂMETROS das parcelas e (2) deixar o
# marcador {{VALOR_CAUSA}} no lugar do valor — o painel calcula deterministicamente,
# gera o PDF e substitui o marcador. A skill NÃO faz a aritmética (evita erro do LLM).
INSTRUCAO_VALOR_CAUSA = (
    "CÁLCULO DO VALOR DA CAUSA (automático pelo painel): se — agora ou depois — "
    "você redigir uma **petição inicial** que pleiteia benefício com parcelas "
    "devidas (BPC/LOAS, benefício por incapacidade, aposentadoria, "
    "restabelecimento etc.), faça DUAS coisas além da petição:\n"
    "  1. No corpo da petição, no ponto em que se atribui o valor da causa, "
    "escreva o marcador literal `{{VALOR_CAUSA}}` em vez de um número "
    "(ex.: \"Dá-se à causa o valor de {{VALOR_CAUSA}}.\") — tanto no `.txt` "
    "quanto no `.docx`, se você gerar o `.docx`. NÃO calcule o valor você mesmo; "
    "o painel calcula e substitui o marcador.\n"
    "  2. Salve, NA MESMA PASTA da petição, um arquivo `valor_causa.json` com os "
    "parâmetros que você extraiu dos documentos (é o painel que fará a conta):\n"
    "     {\n"
    "       \"nb\": \"NNN.NNN.NNN-N\",\n"
    "       \"especie\": \"ex.: BPC Deficiente (87) | Auxílio por Incapacidade (31)\",\n"
    "       \"nome\": \"nome do assistido\",\n"
    "       \"tipo\": \"assistencial | previdenciario  (opcional; o painel deriva da pretensão)\",\n"
    "       \"valor_mensal\": 0.00,  (RMI/último pagamento da mesma espécie; deixe null se BPC ou se nunca recebeu — o painel usa o salário mínimo)\n"
    "       \"valor_mensal_origem\": \"de onde tirou (ex.: Declaração de Benefícios; salário mínimo)\",\n"
    "       \"marco\": \"DER  (concessão)  |  DCB+1  (restabelecimento)\",\n"
    "       \"data_marco\": \"DD/MM/AAAA  (a DER, ou a DCB no restabelecimento)\",\n"
    "       \"meses_vincendos\": 12\n"
    "     }\n"
    "  Regras: valores em ponto decimal (1518.00), datas DD/MM/AAAA. Se algum "
    "dado não constar dos documentos, use null e explique em `valor_mensal_origem` "
    "ou num campo `observacao`. Não invente valores.\n\n"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sem_acento_upper(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).upper()


def _ler_json(caminho: Path) -> dict | None:
    with contextlib.suppress(Exception):
        return json.loads(caminho.read_text(encoding="utf-8"))
    return None


def _localizar_params(paj_dir: Path) -> tuple[dict, Path] | None:
    """Acha o valor_causa.json emitido pela skill. Prioriza 'Peças IA/', depois
    a raiz do PAJ, depois qualquer subpasta. Retorna (dados, pasta_do_json)."""
    candidatos = [paj_dir / "Peças IA" / _JSON_SKILL, paj_dir / _JSON_SKILL]
    candidatos += sorted(paj_dir.glob(f"**/{_JSON_SKILL}"))
    for c in candidatos:
        if c.exists():
            d = _ler_json(c)
            if d is not None:
                return d, c.parent
    return None


def _localizar_peticao(pasta: Path) -> dict[str, list[str]]:
    """Localiza arquivos da PI na pasta (txt e docx que contenham o placeholder
    ou pareçam petição inicial)."""
    txts: list[str] = []
    docxs: list[str] = []
    for f in pasta.iterdir():
        if not f.is_file():
            continue
        nome = _sem_acento_upper(f.name)
        parece_pi = ("PETICAO" in nome) or ("INICIAL" in nome) or nome.startswith("PI ")
        if f.suffix.lower() == ".txt":
            conteudo = ""
            with contextlib.suppress(Exception):
                conteudo = f.read_text(encoding="utf-8", errors="replace")
            if parece_pi or _PLACEHOLDER in conteudo or "EXCELENTISSIM" in _sem_acento_upper(conteudo):
                txts.append(str(f))
        elif f.suffix.lower() == ".docx" and parece_pi:
            docxs.append(str(f))
    return {"txt": txts, "docx": docxs}


def _nome_pdf(pasta: Path) -> str:
    """Se a pasta usa prefixo sequencial 'NN - ', dá o próximo número; senão nome simples."""
    import re

    maior, achou = 0, False
    for f in pasta.iterdir():
        m = re.match(r"^(\d{2}) - ", f.name)
        if m:
            achou = True
            maior = max(maior, int(m.group(1)))
    return f"{maior + 1:02d} - CALC VALOR DA CAUSA.pdf" if achou else "CALC VALOR DA CAUSA.pdf"


def _interpretador() -> str | None:
    """Python que roda o runner: precisa de python-docx + win32com. Usa
    VALOR_CAUSA_PYTHON (se setado) ou detecta um `python`/`py` do PATH com as libs."""
    candidatos = []
    if VALOR_CAUSA_PYTHON:
        candidatos.append(VALOR_CAUSA_PYTHON)
    candidatos += [shutil.which("python"), shutil.which("py"), sys.executable]
    for c in filter(None, candidatos):
        with contextlib.suppress(Exception):
            r = subprocess.run(
                [c, "-c", "import docx, win32com.client"],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0:
                return c
    return None


# ── Derivação de parâmetros (puro stdlib + indices_cm) ───────────────────────

def _tipo_da_pretensao(metadata: dict, override: str | None) -> str:
    if override in ("previdenciario", "assistencial"):
        return override
    p = _sem_acento_upper(
        (metadata.get("pretensaopura") or "") + " " + (metadata.get("pretensao") or "")
    )
    if any(kw in p for kw in _PALAVRAS_ASSISTENCIAL):
        return "assistencial"
    return "previdenciario"


def _data_inicio_atrasados(params: dict) -> str:
    if params.get("data_inicio_atrasados"):
        return params["data_inicio_atrasados"]
    data_marco = params.get("data_marco") or params.get("der") or params.get("dcb")
    if not data_marco:
        raise ValueError("valor_causa.json sem 'data_inicio_atrasados' nem 'data_marco'.")
    marco = _sem_acento_upper(params.get("marco") or "").replace(" ", "")
    d = _dt.datetime.strptime(data_marco, "%d/%m/%Y")
    if "DCB" in marco and "+1" in marco:
        d = d + _dt.timedelta(days=1)
    return d.strftime("%d/%m/%Y")


def _montar_dados_calc(paj_norm: str, params: dict) -> tuple[dict, dict]:
    metadata = _ler_json(PAJS_DIR / paj_norm / "metadata.json") or {}
    avisos: list[str] = []

    tipo = _tipo_da_pretensao(metadata, params.get("tipo"))

    valor_mensal = params.get("valor_mensal")
    origem_valor = params.get("valor_mensal_origem") or ""
    # piso_sm: benefício de 1 salário mínimo (BPC/assistencial, ou previdenciário
    # que nunca recebeu). Nesse caso o calc usa o mínimo VIGENTE em cada competência
    # (tabela salario_minimo.py), não um valor achatado. valor_mensal vira só um
    # fallback (usado se a tabela não carregar).
    piso_sm = False
    if not valor_mensal:
        piso_sm = True
        valor_mensal = SALARIO_MINIMO
        origem_valor = origem_valor or "1 salário mínimo por competência (tabela oficial)"
        avisos.append(
            "Benefício presumido = 1 salário mínimo (mínimo vigente por competência) — "
            "confira se há RMI própria."
        )
    valor_mensal = float(valor_mensal)

    data_inicio = _data_inicio_atrasados(params)
    d_ini = _dt.datetime.strptime(data_inicio, "%d/%m/%Y")
    dias_mes = calendar.monthrange(d_ini.year, d_ini.month)[1]
    dias_atrasados = dias_mes - d_ini.day + 1  # do dia do marco até o fim do mês

    hoje = _dt.date.today().strftime("%d/%m/%Y")
    data_ajuizamento = params.get("data_ajuizamento") or hoje
    if not params.get("data_ajuizamento"):
        avisos.append(f"Data do ajuizamento assumida = hoje ({hoje}); ajuste se protocolar noutra data.")

    dados = {
        "paj": params.get("paj") or metadata.get("paj") or paj_norm,
        "nb": params.get("nb", ""),
        "nome": params.get("nome") or metadata.get("assistido_caixa") or "",
        "especie": params.get("especie", ""),
        "tipo": tipo,
        "valor_mensal": valor_mensal,
        "data_inicio_atrasados": data_inicio,
        "dias_atrasados": dias_atrasados,
        "dias_mes": dias_mes,
        "data_ajuizamento": data_ajuizamento,
        "meses_vincendos": int(params.get("meses_vincendos", 12)),
        "piso_sm": piso_sm,
        # Repassados só para a tabela de identificação do PDF decidir o rótulo
        # (DER x DCB) e mostrar a data do marco sem o +1 dia do restabelecimento.
        "marco": params.get("marco") or "DER",
        "data_marco": params.get("data_marco") or params.get("der") or params.get("dcb") or data_inicio,
    }
    if params.get("data_fim_vincendos"):
        dados["data_fim_vincendos"] = params["data_fim_vincendos"]

    # aviso de manutenção da tabela de salário mínimo: se o cálculo alcança um
    # ano além do último mínimo cadastrado, as parcelas desse ano usam o último
    # valor conhecido (pode estar defasado).
    if piso_sm:
        with contextlib.suppress(Exception):
            if str(VALOR_CAUSA_DIR) not in sys.path:
                sys.path.insert(0, str(VALOR_CAUSA_DIR))
            from salario_minimo import ultimo_ano

            ano_alcance = _dt.datetime.strptime(data_ajuizamento, "%d/%m/%Y").year + 1  # vincendos
            if ano_alcance > ultimo_ano():
                avisos.append(
                    f"Tabela de salário mínimo vai até {ultimo_ano()}; competências de "
                    f"{ultimo_ano() + 1} em diante usam o último mínimo conhecido. "
                    "Atualize salario_minimo.py quando sair o novo decreto."
                )

    # aviso de índice pendente — indices_cm é pure-python (sem docx), seguro importar aqui
    with contextlib.suppress(Exception):
        if str(VALOR_CAUSA_DIR) not in sys.path:
            sys.path.insert(0, str(VALOR_CAUSA_DIR))
        from indices_cm import INPC, IPCA_E, ultimo_mes_publicado

        tabela = INPC if tipo == "previdenciario" else IPCA_E
        ult = ultimo_mes_publicado(tabela)
        d_aj = _dt.datetime.strptime(data_ajuizamento, "%d/%m/%Y")
        if (d_ini.year, d_ini.month) > ult and (d_ini.year, d_ini.month) < (d_aj.year, d_aj.month):
            avisos.append(
                f"Índice {'INPC' if tipo == 'previdenciario' else 'IPCA-E'} do período dos "
                f"atrasados ainda não publicado (último: {ult[1]:02d}/{ult[0]}) — parcelas "
                "exibidas sem atualização; recalcule quando sair."
            )

    premissas = {
        "tipo": tipo,
        "piso_sm": piso_sm,
        "valor_mensal": valor_mensal if not piso_sm else "salário mínimo vigente por competência",
        "valor_mensal_origem": origem_valor,
        "marco": params.get("marco", ""),
        "data_inicio_atrasados": data_inicio,
        "dias_atrasados": dias_atrasados,
        "dias_mes": dias_mes,
        "data_ajuizamento": data_ajuizamento,
        "meses_vincendos": dados["meses_vincendos"],
    }
    return dados, {"premissas": premissas, "avisos": avisos}


# ── Execução do runner ───────────────────────────────────────────────────────

def _rodar_runner(dados: dict, pasta_pi: Path, alvos: dict) -> dict:
    interp = _interpretador()
    if not interp:
        return {
            "ok": False,
            "erro": "Nenhum Python com python-docx + pywin32 (Word) encontrado. "
                    "Instale-os no Python do sistema ou defina VALOR_CAUSA_PYTHON no .env.",
        }
    pdf_path = pasta_pi / _nome_pdf(pasta_pi)
    job = {
        "calc_dir": str(VALOR_CAUSA_DIR),
        "dados": dados,
        "pdf_path": str(pdf_path),
        "placeholder": _PLACEHOLDER,
        "txt_files": alvos["txt"],
        "docx_files": alvos["docx"],
    }
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(job, tf, ensure_ascii=False)
        job_path = tf.name
    # PYTHONIOENCODING=utf-8: força o child (Word/calc imprime acentos) a emitir
    # utf-8; errors="replace" blinda a decodificação de qualquer resto em cp1252.
    import os as _os

    env = {**_os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(
            [interp, str(_RUNNER), job_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=env,
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
    with contextlib.suppress(Exception):
        out = json.loads(linha)
        out["pdf"] = pdf_path.name
        return out
    return {"ok": False, "erro": "Falha ao parsear a saída do runner."}


# ── API pública ──────────────────────────────────────────────────────────────

def processar(paj_norm: str, params: dict | None = None) -> dict:
    """Executa o cálculo do valor da causa. `params` None -> lê de valor_causa.json.
    Nunca levanta — erros vão em 'erro'."""
    paj_dir = PAJS_DIR / paj_norm
    if not paj_dir.exists():
        return {"ok": False, "erro": f"PAJ não encontrado: {paj_norm}"}

    # Atualiza os índices de CM do IBGE antes de calcular (best-effort, TTL-gated:
    # só vai à rede se o cache passou de 12h). Assim os atrasados recentes já saem
    # corrigidos. Falha de rede não bloqueia — cai no que houver em cache/tabela.
    with contextlib.suppress(Exception):
        from services import indices_service

        indices_service.atualizar()

    if params is not None:
        pasta_pi = paj_dir / "Peças IA" if (paj_dir / "Peças IA").exists() else paj_dir
    else:
        achado = _localizar_params(paj_dir)
        if not achado:
            return {"ok": False, "erro": "valor_causa.json não encontrado.", "skip": True}
        params, pasta_pi = achado

    try:
        dados, meta = _montar_dados_calc(paj_norm, params)
    except Exception as e:
        return {"ok": False, "erro": f"Parâmetros inválidos: {e}"}

    alvos = _localizar_peticao(pasta_pi)
    saida = _rodar_runner(dados, pasta_pi, alvos)
    if not saida.get("ok"):
        return saida

    if not saida.get("injetado_em"):
        meta["avisos"].append(
            f"Placeholder {_PLACEHOLDER} não encontrado na PI — valor não injetado no "
            "texto (só no PDF). Insira manualmente ou regenere a PI com o placeholder."
        )

    resultado = {
        "ok": True,
        "paj": paj_norm,
        "pdf": saida.get("pdf"),
        "pasta": str(pasta_pi),
        "total_nominal": saida.get("total_nominal"),
        "total_atualizado": saida.get("total_atualizado"),
        "valor_causa": saida.get("valor_causa"),
        "injetado_em": saida.get("injetado_em", []),
        "premissas": meta["premissas"],
        "avisos": meta["avisos"],
        "gerado_em": _dt.datetime.now().isoformat(timespec="minutes"),
    }
    with contextlib.suppress(Exception):
        (pasta_pi / _JSON_RESULT).write_text(
            json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return resultado


def processar_se_pi(paj_norm: str) -> dict:
    """Hook automático (transversal): só age se a skill emitiu valor_causa.json;
    senão retorna {'ok': True, 'skip': True} (ação que não gerou PI). Idempotente:
    se o resultado já é mais novo que os parâmetros, não recalcula."""
    paj_dir = PAJS_DIR / paj_norm
    if not paj_dir.exists():
        return {"ok": True, "skip": True}
    achado = _localizar_params(paj_dir)
    if not achado:
        return {"ok": True, "skip": True}
    _, pasta = achado
    with contextlib.suppress(Exception):
        pj = pasta / _JSON_SKILL
        rj = pasta / _JSON_RESULT
        if rj.exists() and rj.stat().st_mtime >= pj.stat().st_mtime:
            return {"ok": True, "skip": "já processado"}
    return processar(paj_norm)
