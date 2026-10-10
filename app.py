"""Painel web do workspace Oficio Geral — dashboard, detalhe de PAJ, chat, docgen.

Auto-gerencia processo: mata qualquer python na porta antes de iniciar,
grava PID file. Evita duplicatas.
"""

import contextlib
import logging
import os
import re
import subprocess
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routes.busca import router as busca_router
from routes.chat import router as chat_router
from routes.chat_livre import router as chat_livre_router
from routes.cofre import router as cofre_router
from routes.acoes_eproc import router as acoes_eproc_router
from routes.acoes_sisdpu import router as acoes_sisdpu_router
from routes.agenda import router as agenda_router
from routes.analise_pretensao import router as analise_pretensao_router
from routes.configuracoes import router as configuracoes_router
from routes.calcular_cnis import router as calcular_cnis_router
from routes.calcular_qs import router as calcular_qs_router
from routes.consulta_rpv import router as consulta_rpv_router
from routes.dossie_sd import router as dossie_sd_router
from routes.dossie_cadunico import router as dossie_cadunico_router
from routes.dashboard import router as dashboard_router
from routes.docgen import router as docgen_router
from routes.files import router as files_router
from routes.historico import router as historico_router
from routes.intimacoes import router as intimacoes_router
from routes.minutas import router as minutas_router
from routes.movimentar_conjunto import router as movimentar_conjunto_router
from routes.oficio_sei import router as oficio_sei_router
from routes.sisdpu2_mcp import router as sisdpu2_mcp_router
from routes.peticao_inicial import router as peticao_inicial_router
from routes.paj import router as paj_router
from routes.eproc_consulta import router as eproc_consulta_router
from routes.esisrec import router as esisrec_router
from routes.eproc_mcp import router as eproc_mcp_router
from routes.pipeline_monitor import router as pipeline_monitor_router
from routes.prazos import router as prazos_router
from routes.retornos import router as retornos_router
from routes.sat import router as sat_router
from routes.seguranca import router as seguranca_router
from routes.sync import router as sync_router
from routes.valor_causa import router as valor_causa_router
from routes.watchlist import router as watchlist_router
from services.ambiente_service import verificar_ambiente
from config import PAINEL_NOME, PAINEL_PORT

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".server.pid"
LOGS_DIR = BASE_DIR / "logs"
PORT = PAINEL_PORT


def _configurar_logging() -> None:
    """Configura logging com rotacao diaria em logs/app.log (mantem 7 dias).

    Mantem stdout tambem (uvicorn ja usa). Print()s pre-existentes nao sao
    redirecionados — app continua imprimindo no console como antes."""
    LOGS_DIR.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "app.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Evita duplicar handlers se a funcao for chamada mais de uma vez (reload).
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


_configurar_logging()

# Versao do painel — fonte unica. Usada pelo FastAPI (OpenAPI/docs) e tambem
# exposta a todos os templates via Jinja globals (renderizada no rodape da
# sidebar). Para incrementar: mude aqui e so aqui.
APP_VERSION = "0.4.159"

app = FastAPI(title="oficio-geral-ui", version=APP_VERSION)
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)
# Expoe a versao a todos os templates sem precisar passar no render() de cada rota
app.state.jinja.globals["app_version"] = APP_VERSION

# Identificacao do Oficio/Defensor (aba Geral das Configuracoes) — Ofício,
# Unidade da DPU, Nome do defensor, Cargo do defensor. Substitui o antigo
# OFICIO_DESCRICAO fixo do .env (ainda usado só como migracao best-effort na
# 1a leitura, ver config_painel_service.get_identificacao_orgao). Registrada
# como FUNCAO (nao string fixa) nos globals do Jinja pra reler o
# painel_config.json a cada render — editar em Configuracoes tem efeito
# imediato, sem reiniciar o painel. `oficio_descricao()` compoe o mesmo
# texto "Ofício - Unidade" que aparecia embaixo de "Painel SISDPU" na
# sidebar (ver base.html); `identificacao_orgao()` expoe os 4 campos crus,
# injetados em toda pagina como `window._identificacaoOrgao` (ver base.html)
# pros placeholders [OFICIO]/[UNIDADE]/[DEFENSOR]/[CARGO] dos modelos de
# petição/despacho (ver static/js/acoes_eproc.js/acoes_sisdpu.js).
from services import config_painel_service  # noqa: E402


def _oficio_descricao_atual() -> str:
    """Ofício e Unidade em LINHAS SEPARADAS (pedido do usuário 2026-09-18 —
    antes vinha "Ofício - Unidade" numa linha só). O `\\n` literal funciona
    como quebra de linha visual porque o `<p>` na sidebar tem
    `white-space: pre-line` (ver base.html) — sem isso, HTML colapsaria a
    quebra de linha crua num espaço."""
    info = config_painel_service.get_identificacao_orgao()
    partes = [p for p in (info.get("oficio", ""), info.get("unidade", "")) if p]
    return "\n".join(partes) if partes else "Defensoria Pública da União"


app.state.jinja.globals["oficio_descricao"] = _oficio_descricao_atual
app.state.jinja.globals["painel_nome"] = PAINEL_NOME
app.state.jinja.globals["identificacao_orgao"] = config_painel_service.get_identificacao_orgao

# Mapeamento de exibicao para nomes de area (foro). Os valores armazenados em
# metadata.json vem sem acentos (parser do SISDPU); o front-end formata para
# ortografia portuguesa correta. Comparacoes/filtros continuam usando o valor
# cru — so o RENDER usa o filtro abaixo.
_AREA_DISPLAY = {
    "civel": "Cível",
    "previdenciario": "Previdenciário",
    "saude": "Saúde",
    "execucao": "Execução",
    "criminal": "Criminal",
    "familia": "Família",
    "tributario": "Tributário",
}


def _formatar_area(valor: str | None) -> str:
    if not valor:
        return ""
    return _AREA_DISPLAY.get(str(valor).lower(), valor)


# Pretensões cujo nome cadastrado no SISDPU vem como "Nome antigo (Nome atual)"
# (ex.: "AUXÍLIO-DOENÇA (BENEFÍCIO POR INCAPACIDADE TEMPORÁRIA)") ocupam espaço
# demais em colunas/badges. Exibe só o parêntese final (o nome vigente pós-Lei
# 13.846/2019); a comparação/filtro em qualquer tela continua usando o valor
# CRU (ex.: dashboard filtra por `paj.pretensaopura` original) — só o RENDER
# passa por este filtro. Mesma regra replicada em JS: static/js/app.js
# `formatarPretensao()` (dashboard usa Alpine, não Jinja, pra exibir).
#
# {4,} evita um falso positivo real: pretensões como "...RECEBIDO PELO(A)
# ASSISTIDO(A)" também terminam com parênteses, só que como marcador de
# gênero ("(A)") — sem o mínimo de tamanho, o regex mostrava só "A" no lugar
# da pretensão inteira (bug real, PAJ-2026-029-01393). Nomes atuais de
# pretensão sempre têm bem mais que 4 caracteres, então o corte é seguro.
_RE_PRETENSAO_PARENS = re.compile(r"\(([^()]{4,})\)\s*$")


def _formatar_pretensao(valor: str | None) -> str:
    if not valor:
        return valor or ""
    m = _RE_PRETENSAO_PARENS.search(str(valor))
    return m.group(1).strip() if m else valor


def _formatar_endereco_contato(e: dict | None) -> str:
    """Formata um endereço (extraído do Cadastro de Pessoa Física do SISDPU —
    ver ingestao/sisdpu_client.py) numa linha só, pra tooltip do módulo
    Contatos. `e` é {tipo, logradouro, numero, complemento, bairro,
    municipio, uf, cep}."""
    if not e:
        return ""
    partes = [
        f"{e.get('logradouro', '')}, {e.get('numero', '')}".strip(", "),
        e.get("complemento") or "",
        e.get("bairro") or "",
        e.get("municipio") or "",
        e.get("uf") or "",
        e.get("cep") or "",
    ]
    return ", ".join(p for p in partes if p)


# Formatadores de data — todas as datas exibidas ao usuario devem sair no
# padrao brasileiro DD/MM/AAAA (com hora HH:MM quando aplicavel). Os filtros
# sao tolerantes: aceitam ISO (YYYY-MM-DD[ THH:MM:SS...]), BR ja formatado,
# vazio, ou qualquer outra string (que e' devolvida intacta).
_RE_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_RE_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_RE_BR_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})")
_RE_BR_DT = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})")


def _data_br(valor) -> str:
    if not valor:
        return "—"
    s = str(valor).strip()
    m = _RE_BR_DATE.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = _RE_ISO_DATE.match(s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def _data_hora_br(valor) -> str:
    if not valor:
        return "—"
    s = str(valor).strip()
    m = _RE_ISO_DT.match(s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)} {m.group(4)}:{m.group(5)}"
    m = _RE_BR_DT.match(s)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)} {m.group(4)}:{m.group(5)}"
    return _data_br(s)


def _data_hora_arquivo(valor) -> str:
    """Formata um timestamp Unix (float, ex: `Path.stat().st_ctime`) como
    'DD/MM/AAAA HH:MM' — usado nas colunas "Criado em" das abas Peças/
    Despachos (metadado do sistema de arquivos, não string ISO/BR)."""
    if not valor:
        return "—"
    try:
        return datetime.fromtimestamp(float(valor)).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _oficio_sigla(valor) -> str:
    """Converte um nome de ofício na sigla padronizada (Tabela de Ofícios das
    Configurações). Tolerante a falhas — devolve o valor original se algo der errado."""
    try:
        from services.config_painel_service import sigla_de_oficio

        return sigla_de_oficio(valor)
    except Exception:
        return valor or ""


def _tipo_analise_pretensao(valor) -> str:
    """Pretensão do PAJ -> tipo de skill de diagnóstico cabível ('bpc', ...),
    ou '' se nenhuma. Usado pela página do PAJ para decidir se mostra o botão
    'Analisar PAJ de <tipo>'. Tolerante a falhas."""
    try:
        from services.analise_pretensao_service import tipo_aplicavel

        return tipo_aplicavel(valor)
    except Exception:
        return ""


app.state.jinja.filters["formatar_area"] = _formatar_area
app.state.jinja.filters["formatar_pretensao"] = _formatar_pretensao
app.state.jinja.filters["formatar_endereco_contato"] = _formatar_endereco_contato
app.state.jinja.filters["data_br"] = _data_br
app.state.jinja.filters["data_hora_br"] = _data_hora_br
app.state.jinja.filters["data_hora_arquivo"] = _data_hora_arquivo
app.state.jinja.filters["oficio_sigla"] = _oficio_sigla
app.state.jinja.filters["tipo_analise_pretensao"] = _tipo_analise_pretensao
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(acoes_eproc_router)
app.include_router(acoes_sisdpu_router)
app.include_router(agenda_router)
app.include_router(analise_pretensao_router)
app.include_router(calcular_cnis_router)
app.include_router(calcular_qs_router)
app.include_router(consulta_rpv_router)
app.include_router(dossie_sd_router)
app.include_router(dossie_cadunico_router)
app.include_router(dashboard_router)
app.include_router(paj_router)
app.include_router(files_router)
app.include_router(chat_router)
app.include_router(chat_livre_router)
app.include_router(configuracoes_router)
app.include_router(cofre_router)
app.include_router(docgen_router)
app.include_router(sync_router)
app.include_router(valor_causa_router)
app.include_router(busca_router)
app.include_router(prazos_router)
app.include_router(eproc_mcp_router)
app.include_router(eproc_consulta_router)
app.include_router(esisrec_router)
app.include_router(watchlist_router)
app.include_router(pipeline_monitor_router)
app.include_router(seguranca_router)
app.include_router(sat_router)
app.include_router(retornos_router)
app.include_router(historico_router)
app.include_router(minutas_router)
app.include_router(intimacoes_router)
app.include_router(movimentar_conjunto_router)
app.include_router(oficio_sei_router)
app.include_router(sisdpu2_mcp_router)
app.include_router(peticao_inicial_router)

# Healthcheck de dependencias externas — expoe em app.state.ambiente pra
# o dashboard renderizar banner quando algo falta.
app.state.ambiente = verificar_ambiente()
for _aviso in app.state.ambiente.get("avisos", []):
    print(f"[AMBIENTE] {_aviso}")


@app.on_event("startup")
async def _startup_cota() -> None:
    """Arma o re-disparo automatico por cota: le o estado persistido em disco e
    inicia o scheduler. Se o horario de re-disparo ja passou (servidor estava
    fora do ar), o proximo tick dispara assim que o servidor sobe."""
    with contextlib.suppress(Exception):
        from services import cota_service

        cota_service.carregar_e_rearmar()


@app.on_event("startup")
async def _startup_reconciliar_sessoes_interrompidas() -> None:
    """Corrige análises/organizações que ficaram com status "running" gravado
    em disco por uma sessão que o painel matou no meio (restart) — achado ao
    vivo no PAJ 2026/029-02667, 2026-09-30 (Felipe perguntou por que a coluna
    "Status" de "Análises e Iniciais" mostrava "running" sem nada realmente
    em processamento). `_sessions`/`_sessions` de cada serviço estão sempre
    vazios neste ponto (processo recém-subido), então qualquer "running"
    encontrado é necessariamente órfão. Ver
    services.analise_pretensao_service/organizar_ajuizamento_service.
    reconciliar_sessoes_interrompidas para o critério de correção
    (done/error). Silenciosa em qualquer falha — nunca impede o painel de
    subir."""
    with contextlib.suppress(Exception):
        from services import analise_pretensao_service

        n = analise_pretensao_service.reconciliar_sessoes_interrompidas()
        if n:
            print(f"[reconciliacao] {n} análise(s) com status 'running' órfão corrigido(s)")
    with contextlib.suppress(Exception):
        from services import organizar_ajuizamento_service

        n = organizar_ajuizamento_service.reconciliar_sessoes_interrompidas()
        if n:
            print(f"[reconciliacao] {n} organização(ões) com status 'running' órfão corrigida(s)")


@app.on_event("startup")
async def _startup_limpeza_docs_eproc() -> None:
    """Limpeza automática do cache de documentos baixados na página "Consultar
    processo" (dados/eproc_docs/) — mesmo espírito da rotação de logs
    (TimedRotatingFileHandler), mas para esse cache. Apaga o que não é
    acessado há mais dias que a retenção configurada (Configurações › e-proc,
    padrão 30). Tudo ali é re-baixável do e-proc a qualquer momento, então
    roda sem confirmação. Silenciosa em qualquer falha — nunca impede o
    painel de subir."""
    with contextlib.suppress(Exception):
        from ingestao import eproc_mcp_client

        res = eproc_mcp_client.limpar_documentos_antigos()
        if res.get("removidos"):
            print(f"[eproc-docs] limpeza automática: {res['removidos']} arquivo(s) "
                  f"removido(s), {res['bytes_liberados']} bytes liberados")


@app.on_event("shutdown")
async def _shutdown_fechar_sat() -> None:
    """Ao encerrar o painel (graciosamente), fecha os navegadores do SAT e do e-proc
    junto. NÃO roda em kill -9 (Stop-Process -Force), então reinícios de dev preservam
    as sessões."""
    with contextlib.suppress(Exception):
        from ingestao import sat_client

        sat_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import eproc_client

        eproc_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import rpv_precatorio_client

        rpv_precatorio_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import sd_client

        sd_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import cadunico_client

        cadunico_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import esisrec_client

        esisrec_client.fechar_navegador()


def _cleanup_port(port: int) -> None:
    """Mata TODOS processos LISTEN na porta informada (Windows-only)."""
    if os.name != "nt":
        return
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    except Exception:
        return
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4 or pid == os.getpid() or pid in seen:
            continue
        seen.add(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[app] killed stale process on port {port}: PID {pid}")


def _kill_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
            print(f"[app] killed previous server PID {old_pid}")
    except (ValueError, FileNotFoundError):
        pass
    with contextlib.suppress(Exception):
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    _kill_pid_file()
    _cleanup_port(PORT)
    import time

    time.sleep(1)  # aguarda SO liberar porta

    PID_FILE.write_text(str(os.getpid()))
    print(f"[app] servidor PID={os.getpid()} — http://127.0.0.1:{PORT}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)
    finally:
        with contextlib.suppress(Exception):
            PID_FILE.unlink(missing_ok=True)
        # backup do shutdown hook: garante fechar os navegadores do SAT e do e-proc ao sair
        with contextlib.suppress(Exception):
            from ingestao import sat_client

            sat_client.fechar_navegador()
        with contextlib.suppress(Exception):
            from ingestao import eproc_client

            eproc_client.fechar_navegador()
        with contextlib.suppress(Exception):
            from ingestao import rpv_precatorio_client

            rpv_precatorio_client.fechar_navegador()
        with contextlib.suppress(Exception):
            from ingestao import sd_client

            sd_client.fechar_navegador()
        with contextlib.suppress(Exception):
            from ingestao import cadunico_client

            cadunico_client.fechar_navegador()
        with contextlib.suppress(Exception):
            from ingestao import esisrec_client

            esisrec_client.fechar_navegador()
