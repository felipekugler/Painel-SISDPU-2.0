"""Geração de minuta jurídica via IA (Claude Code CLI, chamada única/"one-shot")
pro módulo "Gerar Minuta" da aba Processo Judicial — usado quando o PAJ já tem
processo judicial ajuizado e chegou intimação pra manifestação sobre uma peça
técnica já nos autos (laudo pericial, avaliação social etc.).

Diferente de `peticao_ia_service.py` (que responde a um PRAZO selecionado, com
o "ato referenciado" resolvido automaticamente via casamento data+dias), aqui
o Defensor escolhe manualmente OS DOCUMENTOS-FONTE (modal com 2 abas: eventos
do processo + anexos do PAJ) — não há heurística de "adivinhar qual é o
laudo". Ver `routes/minutas.py`.

Cada "tipo de minuta" do submenu "Petição com IA" é um registro cadastrado
pelo usuário (Configurações › e-proc › Petição com IA — ver
`config_painel_service.get_minutas_ia_tipos`/`set_minutas_ia_tipos`): rótulo +
instrução livre pra IA + arquivo(s) de referência fixos opcionais. ATÉ
2026-10-02 cada tipo era um dict fixo no código apontando pra uma ÁREA do
monorepo `teses-dpu` (playbook + modelos + CLAUDE.md) — só eu conseguia criar
um tipo novo (editando código). Migrado pra cadastro editável pelo próprio
usuário, sem precisar de código: na 1ª leitura do cadastro vazio (nunca
inicializado), `_semear_tipos_padrao()` pré-popula com os 3 tipos que já
existiam, usando o conteúdo ATUAL dos arquivos teses-dpu como texto inicial
(ver `_ler_area_texto_migracao`) — os arquivos em teses-dpu continuam no
disco, só deixam de ser lidos por este fluxo a partir daí.

Motivo de existir dessa decisão (bug real, PAJ 2026/029-01460, 18/08/2026):
a primeira versão apontava `cwd` do subprocesso pra dentro de `teses-dpu` e
instruía o Claude a "ir ler" os arquivos da área + rodar o pipeline completo
de `analise_pretensao_service.py` (`anti-alucinacao` -> `formatacao-docx` ->
`checagem-final`). Isso trouxe DOIS problemas:
  1. Rodar dentro de `teses-dpu` carrega automaticamente o `CLAUDE.md` RAIZ
     do monorepo — que manda "peça real é completa, detalhada e minuciosa,
     NUNCA enxuta" (regra certa pra petição inicial, errada pra uma
     manifestação simples). A peça saiu com tópicos numerados (I/II/III/IV)
     longos, bem diferente do texto corrido e curto que o Project do
     Defensor sempre gerou em segundos.
  2. Cada etapa do pipeline (ler N arquivos, rodar anti-alucinação, escrever
     e rodar o script de formatação pesado com barra lateral/timbre, rodar
     checagem-final, converter pra PDF) é uma ida-e-volta separada com o
     modelo — pra uma manifestação de 1 página isso levou mais de 6 minutos,
     contra menos de 1 minuto no Project (que só lê os PDFs e devolve o
     texto, sem nenhuma dessas etapas).
Corrigido: motor de formatação simples (`peticao_base.py`, o mesmo que
"Ações no Eproc" já usa), sem anti-alucinação/checagem-final, critérios da
área embutidos como texto (não como arquivos pra ir abrir), e conversão pra
PDF feita em Python (Word/COM) depois que o subprocesso termina — não pelo
próprio Claude.

Fluxo em 2 etapas (pedido do Defensor, 18/08/2026 — mesmo padrão de "Gerar
petição com IA" + "Incluir petição nos anexos" em `peticao_ia_service.py`):
  1. `iniciar()` roda a IA e devolve o texto em HTML (`peticao_html`) — NENHUM
     arquivo fica salvo ainda; é só um rascunho pro Defensor revisar/editar
     na caixa de texto do painel.
  2. `formatar_e_salvar()` pega o HTML JÁ REVISADO (com os ajustes manuais do
     Defensor) e SÓ ENTÃO gera o `.docx`/`.pdf` de verdade, na subpasta
     `Peças IA/` do PAJ (MESMA subpasta que `analise_pretensao_service.py`
     já usa pra petição inicial) — aparece na aba "Peças" da página do PAJ.
     Não chama IA — é formatação pura via `peticao_base.py`.

Estado em memória (`_status`), 1 geração por PAJ por vez — mesmo padrão de
`peticao_ia_service.py`.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from config import PAJS_DIR
from services import config_painel_service
from services.chat_service import CLAUDE_CMD
from services.peticao_ia_service import (
    MODELOS_DIR, _REGRAS_FORMATO, _docx_para_html, _docx_para_pdf, _gerar_docx_sem_viuva,
)

# Só usada por `_ler_area_texto_migracao` (migração ÚNICA do cadastro antigo,
# ver `_semear_tipos_padrao`) — não faz mais parte do fluxo normal de
# geração, que lê o cadastro em `config_painel_service` (editável pelo
# usuário em Configurações › e-proc › Petição com IA).
TESES_DPU_DIR = Path(r"C:\Claude\teses-dpu")


def _tipos_cadastrados() -> list[dict]:
    """Cadastro de "Petição com IA" (ver config_painel_service), semeando os
    3 tipos antigos na 1ª leitura (cadastro nunca inicializado)."""
    if not config_painel_service.minutas_ia_tipos_cadastrado():
        return _semear_tipos_padrao()
    return config_painel_service.get_minutas_ia_tipos()


def _tipo_por_id(tipo_id: str) -> dict | None:
    return next((t for t in _tipos_cadastrados() if t.get("id") == tipo_id), None)


def listar_tipos() -> list[dict]:
    """Catálogo ENXUTO pro submenu "Petição com IA" da página do PAJ — só
    {slug, label}, na ordem cadastrada. Ver `listar_tipos_completos()` pra
    versão cheia (Configurações › e-proc › Petição com IA)."""
    return [{"slug": t["id"], "label": t["label"]} for t in _tipos_cadastrados()]


def listar_tipos_completos() -> list[dict]:
    """Cadastro completo (id/label/instrucao/instrucao_estilo/
    documentos_sugeridos/documentos_obrigatorios/arquivos_referencia) pra
    tela de Configurações › e-proc › Petição com IA (CRUD)."""
    return _tipos_cadastrados()


def _ler_area_texto_migracao(area_dir: Path) -> str:
    """SÓ usada por `_semear_tipos_padrao` (migração única do cadastro antigo)
    — lê CLAUDE.md + playbook/*.md + modelos/*.md da área teses-dpu, mantendo
    o NOME de cada arquivo como cabeçalho (pedido do usuário, 2026-10-02: numa
    caixa de texto só — sem mais arquivos separados — os cabeçalhos
    preservam alguma organização visual pra revisão manual depois)."""
    partes: list[str] = []
    claude_md = area_dir / "CLAUDE.md"
    if claude_md.exists():
        partes.append(f"## CLAUDE.md\n\n{claude_md.read_text(encoding='utf-8')}")
    for sub, titulo in (("playbook", "Playbook"), ("modelos", "Modelos")):
        pasta = area_dir / sub
        if pasta.exists():
            for f in sorted(pasta.glob("*.md")):
                partes.append(f"## {titulo} — {f.name}\n\n{f.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(partes)


# Mesma instrução de estilo que "Contrarrazões de Recurso" já usava fixa no
# código — vira o valor inicial do campo `instrucao_estilo` desse tipo na
# migração (ver `_semear_tipos_padrao`), editável dali em diante.
_INSTRUCAO_ESTILO_CONTRARRAZOES_MIGRACAO = (
    "## ATENÇÃO — estrutura desta peça\n"
    "Contrarrazões de recurso são MAIS substanciais que uma manifestação simples — "
    "NÃO se limite a 3-6 parágrafos. Siga os blocos descritos nos critérios acima "
    "(seção 2 do playbook): abertura, síntese da decisão recorrida, síntese do "
    "recurso, preliminar (só se cabível), rebate de CADA ponto do recurso "
    "separadamente (um ou mais parágrafos por ponto, na mesma ordem do recurso), "
    "pedido final. Mesmo assim, em texto corrido — SEM tópicos numerados formais "
    "(nada de \"I – DOS FATOS\", \"II – DO DIREITO\" etc.), mantendo o estilo pessoal "
    "de parágrafos bem delimitados por bloco/ponto rebatido, não títulos "
    "romanos.\n\n"
)


def _semear_tipos_padrao() -> list[dict]:
    """1ª inicialização do cadastro (nunca rodado antes) — pré-popula com os
    3 tipos que até 2026-10-02 eram fixos em código/arquivos teses-dpu,
    usando o texto de HOJE como ponto de partida editável (pedido do usuário:
    "podemos já deixar pré-cadastradas... com as orientações que já estão
    cadastradas")."""
    tipos = [
        {
            "label": "Petição genérica",
            "instrucao": _ler_area_texto_migracao(TESES_DPU_DIR / "areas" / "peticao-generica"),
            "documentos_sugeridos": "os documentos relevantes ao propósito descrito na "
                                    "Orientação Diretiva",
            # Único tipo em que documento é OPCIONAL — a Orientação Diretiva
            # sozinha pode bastar. Os demais exigem ao menos 1 documento
            # (default True — ver `iniciar()`/`_gerar_worker`).
            "documentos_obrigatorios": False,
        },
        {
            "label": "Manifestação sobre laudo médico pericial (BI)",
            "instrucao": _ler_area_texto_migracao(TESES_DPU_DIR / "areas" / "beneficio-incapacidade"),
            "documentos_sugeridos": "laudo pericial e petição inicial",
        },
        {
            "label": "Contrarrazões de Recurso",
            "instrucao": _ler_area_texto_migracao(TESES_DPU_DIR / "areas" / "contrarrazoes-recurso"),
            "instrucao_estilo": _INSTRUCAO_ESTILO_CONTRARRAZOES_MIGRACAO,
            "documentos_sugeridos": "recurso interposto, decisão/sentença recorrida, "
                                    "petição inicial e/ou contestação",
        },
    ]
    return config_painel_service.set_minutas_ia_tipos(tipos)


# paj_norm -> {status: idle|running|done|error, erro, arquivos, iniciado_em}
_status: dict[str, dict] = {}
_lock = threading.Lock()


def status(paj_norm: str) -> dict:
    return _status.get(paj_norm, {"status": "idle"})


def usar_modelo_padrao(paj_norm: str, peticao_html: str) -> dict:
    """"Petição padrão" — o front já resolveu os placeholders do modelo
    escolhido (mesmo mecanismo de "Usar modelo" de Ações no Eproc/Ações no
    SISDPU — ver static/js/rich_editor.js) e manda aqui o HTML pronto. Grava
    direto em `_status` como "done", no mesmo formato que `_gerar_worker`
    deixaria ao terminar — sem chamar IA nenhuma — pra reaproveitar a MESMA
    caixa de edição e os botões "Gravar"/"Gerar arquivo (DOC/PDF)" já
    prontos, sem duplicar nada desse fluxo."""
    with _lock:
        _status[paj_norm] = {
            "status": "done", "erro": "", "peticao_html": peticao_html,
            "concluido_em": datetime.now().isoformat(timespec="seconds"),
        }
    return {"ok": True}


def salvar_rascunho(paj_norm: str, peticao_html: str) -> dict:
    """Botão "Gravar" da caixa de edição — persiste o texto ATUAL (já editado
    manualmente) em `_status`, sem gerar arquivo nenhum. Sem isso, a edição
    só existia na memória do navegador (Quill) — um reload da página trazia
    de volta o rascunho ORIGINAL da IA, perdendo o que o Defensor tivesse
    ajustado (bug real, 18/08/2026)."""
    atual = _status.get(paj_norm)
    if not atual or atual.get("status") != "done":
        return {"ok": False, "erro": "Não há rascunho gerado pra este PAJ ainda."}
    with _lock:
        _status[paj_norm]["peticao_html"] = peticao_html
    return {"ok": True}


def _falhar(paj_norm: str, erro: str) -> None:
    with _lock:
        _status[paj_norm] = {"status": "error", "erro": erro}


def iniciar(paj_norm: str, processo: str, tipo_slug: str, documentos: list[dict], orientacao: str = "") -> dict:
    """Valida e dispara a geração em background. `documentos`: lista de
    {origem: "processo", ...} ou {origem: "paj", caminho: "Arquivos/xxx.pdf"}
    — já resolvida pelo front a partir das 2 abas do modal de seleção.
    `orientacao`: texto opcional da caixa "Orientação diretiva para análise
    da IA" (compartilhada com "Analisar"/"Calcular Tempo de Contribuição"/
    "Calcular Qualidade de Segurado" — ver paj_detail.html), lida pelo front
    da mesma variável Alpine e enviada aqui (ver static/js/minuta_ia.js).
    Retorna {ok, status, erro?}."""
    cfg = _tipo_por_id(tipo_slug)
    if not cfg:
        return {"ok": False, "erro": f"Tipo de minuta desconhecido: {tipo_slug!r}."}
    documentos = [d for d in (documentos or []) if d.get("origem")]
    if not documentos:
        if cfg.get("documentos_obrigatorios", True):
            return {"ok": False, "erro": "Selecione ao menos um documento (processo ou PAJ) antes de gerar a minuta."}
        if not (orientacao or "").strip():
            return {"ok": False, "erro": "Preencha a Orientação Diretiva ou selecione ao menos um documento "
                                          "antes de gerar a minuta."}

    atual = _status.get(paj_norm)
    if atual and atual.get("status") == "running":
        return {"ok": True, "status": "running"}

    with _lock:
        _status[paj_norm] = {"status": "running", "erro": "", "iniciado_em": datetime.now().isoformat(timespec="seconds")}

    t = threading.Thread(target=_gerar_worker, args=(paj_norm, processo, tipo_slug, documentos, orientacao), daemon=True)
    t.start()
    return {"ok": True, "status": "running"}


def _gerar_worker(paj_norm: str, processo: str, tipo_slug: str, documentos: list[dict], orientacao: str = "") -> None:
    try:
        cfg = _tipo_por_id(tipo_slug)
        if not cfg:
            _falhar(paj_norm, f"Tipo de minuta desconhecido: {tipo_slug!r}.")
            return
        pasta_paj = PAJS_DIR / paj_norm
        pasta_pecas_ia = pasta_paj / "Peças IA"
        pasta_pecas_ia.mkdir(parents=True, exist_ok=True)

        caminhos = _resolver_documentos(paj_norm, processo, documentos)
        # Falha só se HAVIA documento(s) selecionado(s) e nenhum resolveu (erro
        # real — download/arquivo sumido). Sem seleção nenhuma (permitido só
        # pra tipos com documentos_obrigatorios=False, ex.: "peticao-generica")
        # segue em frente com `caminhos` vazio — a peça se apoia só na
        # Orientação Diretiva/conhecimento geral. NÃO depende dos arquivos de
        # referência do tipo (resolvidos à parte, abaixo) — esses são fixos
        # do cadastro, nunca "selecionados" pelo Defensor nesta tela.
        if documentos and not caminhos:
            _falhar(paj_norm, "Não foi possível localizar nenhum dos documentos selecionados "
                               "(falha ao baixar do e-proc, ou arquivo do PAJ não existe mais em disco).")
            return

        texto_referencia, caminhos_referencia = _resolver_arquivos_referencia(cfg)
        criterio_area = (cfg.get("instrucao") or "") + texto_referencia
        paj_exibicao = _paj_exibicao(paj_norm)
        antes = _listar_arquivos(pasta_pecas_ia)
        prompt = _montar_prompt(paj_exibicao, processo, criterio_area, caminhos, pasta_pecas_ia,
                                 orientacao=orientacao, cfg=cfg, caminhos_referencia=caminhos_referencia)

        # Prompt por STDIN (não argumento) — mesmo motivo de peticao_ia_service.py:
        # o texto dos documentos-fonte facilmente passa do limite de linha de
        # comando do Windows. cwd = MODELOS_DIR (NÃO teses-dpu — ver docstring
        # do módulo: rodar dentro de teses-dpu carregaria o CLAUDE.md raiz
        # daquele monorepo, com a regra "peça sempre extensa", errada aqui).
        cmd = [*CLAUDE_CMD, "-p", "--permission-mode", "bypassPermissions"]
        proc = subprocess.run(
            cmd, cwd=str(MODELOS_DIR), input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        depois = _listar_arquivos(pasta_pecas_ia)
        novos = sorted(depois - antes)
        docx_novos = [n for n in novos if n.lower().endswith(".docx")]
        if not docx_novos:
            saida = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            _falhar(paj_norm, f"A IA não gerou nenhum arquivo .docx novo em 'Peças IA'. Saída: {saida[-1500:] or '(vazia)'}")
            return

        # NÃO converte pra PDF nem deixa o(s) .docx do RASCUNHO em "Peças IA" —
        # é só um rascunho pra virar texto editável na caixa do painel (o
        # Defensor revisa/ajusta ANTES de gerar o arquivo final de verdade,
        # via `formatar_e_salvar`, mesmo padrão de "Incluir petição nos
        # anexos" em peticao_ia_service.py). Extrai o HTML do primeiro .docx
        # gerado e descarta o(s) arquivo(s)-rascunho.
        docx_path = pasta_pecas_ia / docx_novos[0]
        peticao_html = ""
        with contextlib.suppress(Exception):
            peticao_html = _docx_para_html(docx_path)
        for nome in docx_novos:
            with contextlib.suppress(Exception):
                (pasta_pecas_ia / nome).unlink()

        if not peticao_html:
            _falhar(paj_norm, "A IA gerou o rascunho, mas não foi possível extrair o texto dele.")
            return

        with _lock:
            _status[paj_norm] = {
                "status": "done", "erro": "", "peticao_html": peticao_html,
                "concluido_em": datetime.now().isoformat(timespec="seconds"),
            }
    except subprocess.TimeoutExpired:
        _falhar(paj_norm, "A IA excedeu o tempo limite (5 min) redigindo a minuta.")
    except Exception as e:  # noqa: BLE001
        _falhar(paj_norm, f"{type(e).__name__}: {e}"[:500])


def _listar_arquivos(pasta: Path) -> set[str]:
    if not pasta.exists():
        return set()
    return {f.name for f in pasta.iterdir() if f.is_file()}


_MESES_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _data_por_extenso() -> str:
    """"18 de agosto de 2026" — calculada em Python (não deixada pro modelo
    "adivinhar" a data atual), pro fecho da peça (ver assinatura() com
    termos=False/data=... em peticao_base.py — pedido do Defensor,
    18/08/2026: peça pronta pra uso imediato deve ter a data real por
    extenso, não o placeholder 'data do protocolo eletrônico' usado no fluxo
    de "Ações no Eproc" — lá a peça só é protocolada depois, e o e-proc
    carimba a data real na hora)."""
    agora = datetime.now()
    return f"{agora.day} de {_MESES_PT[agora.month - 1]} de {agora.year}"


def _paj_exibicao(paj_norm: str) -> str:
    """Formato humano do PAJ ("2026/029-01460") pra citar DENTRO da peça —
    `paj_norm` ("PAJ-2026-029-01460", com prefixo e hifens) é só o nome da
    pasta em disco, nunca deveria aparecer literalmente no texto da peça.
    Bug real (18/08/2026): o prompt citava `paj_norm` direto, saindo
    "PAJ PAJ-2026-029-01460" (prefixo duplicado + formato de pasta) em vez de
    "PAJ 2026/029-01460". Lê `metadata.json` (campo "paj", já no formato
    humano) — cai pro próprio `paj_norm` só se o arquivo faltar."""
    import contextlib as _contextlib
    import json as _json

    with _contextlib.suppress(Exception):
        meta = _json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
        if meta.get("paj"):
            return meta["paj"]
    return paj_norm


def _resolver_arquivos_referencia(cfg: dict) -> tuple[str, list[Path]]:
    """Arquivos de referência fixos cadastrados pro tipo (Configurações ›
    e-proc › Petição com IA — ver config_painel_service.
    salvar_arquivo_referencia_minuta_ia), à parte dos documentos do CASO
    selecionados pelo Defensor na hora. `.md`/`.txt` são lidos e embutidos
    como texto (mesmo tratamento que CLAUDE.md/playbook/modelos já tinham
    antes de virarem campo editável); qualquer outra extensão (`.pdf`/
    `.docx` etc.) vira um caminho a mais — a IA já sabe abrir esses formatos,
    mesmo mecanismo dos documentos do caso."""
    texto_extra = ""
    caminhos_extra: list[Path] = []
    tipo_id = cfg.get("id", "")
    for arq in (cfg.get("arquivos_referencia") or []):
        nome = arq.get("nome") or ""
        if not nome:
            continue
        caminho = config_painel_service.caminho_arquivo_referencia_minuta_ia(tipo_id, nome)
        if not caminho:
            continue
        if caminho.suffix.lower() in (".md", ".txt"):
            with contextlib.suppress(Exception):
                texto_extra += f"\n\n---\n\n## Referência — {nome}\n\n{caminho.read_text(encoding='utf-8')}"
        else:
            caminhos_extra.append(caminho)
    return texto_extra, caminhos_extra


def _resolver_documentos(paj_norm: str, processo: str, documentos: list[dict]) -> list[Path]:
    """Garante no disco e devolve os caminhos de cada documento selecionado.
    Documentos do PROCESSO ainda não baixados são baixados agora (MCP e-proc);
    documentos do PAJ já estão sincronizados em `Arquivos/`."""
    import asyncio

    return asyncio.run(_resolver_async(paj_norm, processo, documentos))


async def _resolver_async(paj_norm: str, processo: str, documentos: list[dict]) -> list[Path]:
    """Traduz a seleção feita nas 2 abas do modal (tabela de eventos do
    processo / tabela de movimentações do PAJ — ambas em "modo seleção", ver
    static/js/eventos_processo.js e static/js/movimentacoes.js) em caminhos
    reais no disco.

    Cada documento chega em um destes 3 formatos:
      - {origem: "processo", arquivo: "<NOME do arquivo>"} — documento do
        evento que já estava baixado no momento da seleção (badge já
        clicável na tabela). IMPORTANTE: `arquivo` aqui é só o NOME do
        arquivo (é o que `listar_arquivos_baixados`/`arquivos_baixados` do
        e-proc devolvem — ver `ingestao/eproc_mcp_client.py`), não o caminho
        completo — precisa juntar com a pasta do processo em
        `EPROC_DOCS_DIR`.
      - {origem: "processo", evento_numero: "1", sigla: "INIC1"} — documento
        do evento ainda NÃO baixado (só existia a descrição, com a `sigla`
        que o e-proc já devolve por documento antes de baixar). A API do
        e-proc só baixa por EVENTO INTEIRO (não existe download de 1
        documento isolado) — baixa o evento, mas depois filtra pelo par
        evento+sigla em `res["baixados"]` (que também vem com `sigla` por
        item, ao contrário de `arquivos_por_evento`) pra incluir só o(s)
        arquivo(s) realmente selecionado(s), não o evento inteiro. Sem
        `sigla` (fallback defensivo, não deveria acontecer com o front atual)
        inclui todos os arquivos do evento.
      - {origem: "paj", caminho: "Arquivos/xxx.pdf"} — anexo já sincronizado
        do PAJ (só documento já local é selecionável nessa aba — ver
        toggleSelecaoIA em movimentacoes.js).
    """
    from ingestao import eproc_mcp_client as mcp

    pasta_paj = PAJS_DIR / paj_norm
    numero_limpo = re.sub(r"\D", "", processo or "")
    pasta_docs_processo = (mcp.EPROC_DOCS_DIR / numero_limpo) if numero_limpo else None
    caminhos: list[Path] = []

    # 1) Documentos do processo já baixados no momento da seleção — só o
    #    NOME do arquivo (não o caminho completo), reconstrói com a pasta
    #    persistente do processo.
    if pasta_docs_processo:
        for d in documentos:
            if d.get("origem") == "processo" and d.get("arquivo"):
                caminho = pasta_docs_processo / d["arquivo"]
                if caminho.exists():
                    caminhos.append(caminho)

    # 2) Documentos do processo ainda pendentes (evento_numero + sigla) —
    #    baixa cada evento distinto UMA vez, depois filtra por sigla.
    pendentes = [d for d in documentos
                if d.get("origem") == "processo" and d.get("evento_numero") and not d.get("arquivo")]
    eventos_a_baixar = sorted({str(d["evento_numero"]) for d in pendentes})
    if eventos_a_baixar and numero_limpo:
        with contextlib.suppress(Exception):
            res = await mcp.baixar_documentos_evento(numero_limpo, 1, eventos_a_baixar)
            baixados = res.get("baixados") or []  # [{evento, sigla, nome, arquivo (caminho completo), ...}]
            for d in pendentes:
                numero_evento = str(d["evento_numero"])
                sigla = d.get("sigla")
                if sigla:
                    encontrados = [b for b in baixados
                                   if str(b.get("evento")) == numero_evento and b.get("sigla") == sigla]
                else:
                    # Sem sigla (não deveria ocorrer com o front atual) —
                    # inclui todos os arquivos baixados deste evento.
                    encontrados = [b for b in baixados if str(b.get("evento")) == numero_evento]
                for b in encontrados:
                    caminho = b.get("arquivo")
                    if caminho and Path(caminho).exists():
                        caminhos.append(Path(caminho))

    # 3) Anexos do PAJ (já sincronizados do SISDPU, em Arquivos/).
    for d in documentos:
        if d.get("origem") == "paj" and d.get("caminho"):
            caminho = pasta_paj / d["caminho"]
            if caminho.exists():
                caminhos.append(caminho)

    # Remove duplicatas preservando ordem (o mesmo arquivo pode ter sido
    # selecionado por mais de uma via — ex.: já baixado E também bate com
    # outra seleção pendente resolvida agora).
    vistos: set[str] = set()
    unicos: list[Path] = []
    for c in caminhos:
        chave = str(c.resolve())
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(c)
    return unicos


_INSTRUCAO_ESTILO_PADRAO = (
    "## ATENÇÃO — isto é uma MANIFESTAÇÃO CURTA, não uma petição inicial\n"
    "3 a 6 parágrafos em texto corrido, no total. NÃO estruture em tópicos numerados "
    "(nada de \"I – DOS FATOS\", \"II – DO DIREITO\" etc.) e não invente seções que os "
    "critérios acima não pedirem. Onde os critérios listarem itens a)/b)/c) como "
    "conteúdo a abordar, isso é só a ORGANIZAÇÃO do que considerar — no texto final "
    "viram parágrafos corridos, sem letras/números aparecendo na peça.\n\n"
)


def _montar_prompt(paj_exibicao: str, processo: str, criterio_area: str, caminhos: list[Path], pasta_destino: Path,
                    orientacao: str = "", cfg: dict | None = None, caminhos_referencia: list[Path] | None = None) -> str:
    # Sem documento nenhum (só permitido pra tipos com documentos_obrigatorios=
    # False, ex.: "peticao-generica") — a peça se apoia só na Orientação
    # Diretiva/conhecimento geral; deixa isso EXPLÍCITO pro modelo em vez de
    # uma seção "Documentos-fonte" vazia e muda esperando conteúdo ali.
    # Material de referência do TIPO (fixo, cadastrado em Configurações) vai
    # separado dos documentos do CASO (selecionados pelo Defensor agora) —
    # ver `_resolver_arquivos_referencia`.
    caminhos_referencia = caminhos_referencia or []
    blocos_docs = []
    if caminhos_referencia:
        blocos_docs.append("### Material de referência desta área (cadastrado no tipo, vale pra qualquer caso)\n"
                            + "\n".join(f"- `{c}`" for c in caminhos_referencia))
    if caminhos:
        blocos_docs.append("### Documentos do caso selecionados pelo Defensor\n"
                            + "\n".join(f"- `{c}`" for c in caminhos))
    lista_docs = ("\n\n".join(blocos_docs) if blocos_docs
                  else "(nenhum documento foi selecionado — baseie-se na Orientação Diretiva "
                       "abaixo e no que for de conhecimento geral pertinente)")
    nome_sugerido = pasta_destino / f"manifestacao-{datetime.now().strftime('%d-%m-%Y')}.docx"
    cfg = cfg or {}

    # Orientação diretiva do Defensor (caixa compartilhada com "Analisar"/
    # "Calcular Tempo de Contribuição"/"Calcular Qualidade de Segurado" — ver
    # paj_detail.html/static/js/minuta_ia.js). Mesmo texto/posicionamento de
    # analise_pretensao_service.py — logo no início, antes até dos critérios
    # da área, pra pesar desde o começo do raciocínio.
    bloco_orientacao = ""
    if orientacao:
        bloco_orientacao = (
            "## ORIENTAÇÃO DIRETIVA DO DEFENSOR (leia com atenção — é uma decisão já "
            "tomada pelo Defensor sobre o caso, não uma sugestão a ser questionada)\n"
            f'"{orientacao}"\n\n'
            "Direcione a redação conforme essa orientação, evitando desenvolver ou "
            "sugerir abordagens que o Defensor já descartou. Se algo nos documentos "
            "contradizer claramente a orientação, sinalize isso ENTRE COLCHETES no "
            "texto da peça em vez de simplesmente ignorar ou seguir cegamente.\n\n"
        )

    # Instrução de tamanho/estrutura da peça — cada tipo pode substituir a
    # padrão (peça curta, sem tópicos — certa pra uma manifestação reativa
    # simples) por uma própria, via o campo "instrucao_estilo" do cadastro
    # (Configurações › e-proc › Petição com IA), quando a peça exigir outra
    # estrutura (ex.: Contrarrazões de Recurso, que rebate vários pontos e
    # se beneficia de alguma estruturação).
    instrucao_estilo = cfg.get("instrucao_estilo") or _INSTRUCAO_ESTILO_PADRAO

    return (
        f"Redija uma manifestação sobre os documentos abaixo, no processo judicial "
        f"{processo or '(ver documentos)'} (PAJ {paj_exibicao} — referência interna da DPU, "
        "não aparece pro juízo).\n\n"
        "## Documentos-fonte selecionados pelo Defensor (leia todos antes de redigir)\n"
        f"{lista_docs}\n\n"
        + bloco_orientacao +
        "## Critérios desta área — LEIA COM ATENÇÃO (define a classificação do caso e o "
        "conteúdo jurídico da peça; já embutido aqui, NÃO precisa abrir nenhum arquivo "
        "pra ler isso)\n"
        f"{criterio_area}\n\n"
        "## Classificação do caso\n"
        "Classifique o caso conforme os critérios acima e prossiga direto com o tipo "
        "identificado — não pergunte nada; registre a classificação e o motivo em 1 "
        "parágrafo no INÍCIO da sua resposta de texto (fora do arquivo gerado). Lacunas/"
        "dúvidas vão sinalizadas ENTRE COLCHETES dentro do próprio texto da peça, não "
        "como pergunta.\n\n"
        + instrucao_estilo
        + _REGRAS_FORMATO +
        "\n---\n\n"
        "## Como gerar o arquivo\n"
        f"Use a biblioteca `peticao_base.py` (já está em `{MODELOS_DIR}` — rode a partir "
        "dessa pasta, ou faça `sys.path.insert(0, r'" + str(MODELOS_DIR) + "')` antes do "
        "import). Ela já traz as REGRAS DE ESTILO obrigatórias da DPU no próprio "
        "docstring do módulo — leia-o primeiro e siga-as TAMBÉM, além das regras acima.\n\n"
        "Estrutura ESPERADA, NESTA ORDEM (adapte o conteúdo, mas siga a sequência de "
        "chamadas):\n"
        "1. `novo_documento()` para abrir o doc.\n"
        "2. `enderecamento(doc, texto_endereco, num_processo)` — ANTES da qualificação. "
        "`texto_endereco` é algo como 'EXCELENTÍSSIMO(A) SENHOR(A) JUIZ(A) FEDERAL' (ajuste "
        "o cargo/vara conforme a instância/classe do processo, se souber pelos documentos); "
        f"`num_processo` é '{processo}'.\n"
        f"3. Logo após, insira uma linha adicional (mesmo padrão de `par()`/`r()`, alinhado à "
        f"esquerda, sem recuo de 1ª linha) com a referência do PAJ, em NEGRITO: "
        f"'PAJ {paj_exibicao}' (EXATAMENTE este texto — não use nenhum outro identificador de "
        "PAJ). Logo depois desta linha, 3 parágrafos em branco (`blank()`) antes da "
        "qualificação.\n"
        "4. `qualificacao(doc, nome_da_parte, '', texto_pre=<frase>, texto_pos='')` — nome "
        "em maiúsculas (a função já faz isso). `texto_pre` deve ser algo como ', qualificada "
        "nos autos, representada pela Defensoria Pública da União, comparece, perante Vossa "
        "Excelência, para se manifestar sobre [o laudo pericial / a avaliação social / o que "
        "for objeto desta manifestação, conforme os documentos].' — ajuste o final da frase "
        "ao conteúdo real sendo respondido.\n"
        "5. Parágrafos com `par()`/`r()` — texto corrido, conciso, seguindo as regras de "
        "estilo acima. Poucos parágrafos (3 a 6) — isto NÃO é petição inicial.\n"
        "6. Se houver pedido, encerre o corpo com o requerimento (sem usar dois-pontos "
        "antes da lista).\n"
        f"7. `assinatura(doc, termos=False, data='{_data_por_extenso()}')` — chame-a UMA "
        "ÚNICA VEZ, IMEDIATAMENTE após o último parágrafo do pedido, e NÃO escreva você "
        "mesmo nenhuma linha de fecho antes dela (nem 'Termos em que pede deferimento' — "
        "NÃO usar essa linha nesta peça —, nem a data, nem o nome/cargo do defensor) — "
        "`assinatura()` já gera TODAS essas linhas sozinha, EXATAMENTE nesses parâmetros "
        "(não altere `termos` nem `data`).\n"
        f"8. `salvar(doc, r'{nome_sugerido}')` — pode ajustar só o nome-base do arquivo pra "
        "algo mais descritivo do tipo de peça, mas mantenha a pasta exatamente essa.\n\n"
        "NÃO gere PDF nem rode conversão nenhuma — a conversão pra PDF é feita "
        "automaticamente pelo painel, em Python, depois que o `.docx` for salvo.\n\n"
        "IMPORTANTE sobre idioma: toda comunicação nesta tarefa em português do Brasil, "
        "inclusive comentários de passagem entre a leitura de um anexo e outro.\n\n"
        "Escreva e execute o script diretamente (você tem acesso a Bash/Write) — não "
        "peça confirmação, não mostre o texto antes de salvar. Ao final, confirme em 1-2 "
        "linhas que o arquivo foi salvo e a classificação do caso adotada."
    )


def formatar_e_salvar(paj_norm: str, tipo_slug: str, peticao_html: str) -> dict:
    """"Gerar arquivo (DOC/PDF)" — pega o texto ATUAL da caixa de edição (já
    revisado/ajustado manualmente pelo Defensor, ou o rascunho da IA sem
    alteração) e o formata no padrão DPU (`peticao_base.py`, via
    `_gerar_docx_sem_viuva` — mesma função de "Incluir petição nos anexos" em
    `peticao_ia_service.py`, já com o ajuste anti-viúva do fecho embutido),
    sem chamar IA — rápido e síncrono (mas quem chama deve rodar em thread —
    conversão via Word/COM é bloqueante). Salva em `Peças IA/` (docx + pdf),
    sobrescrevendo se já existir um do mesmo tipo no mesmo dia."""
    texto_limpo = re.sub(r"<[^>]+>", "", peticao_html or "").strip()
    if not texto_limpo:
        return {"ok": False, "erro": "A caixa de texto está vazia — escreva ou gere o texto antes de gerar o arquivo."}

    pasta_pecas_ia = PAJS_DIR / paj_norm / "Peças IA"
    pasta_pecas_ia.mkdir(parents=True, exist_ok=True)
    nome = f"minuta-{tipo_slug}-{datetime.now().strftime('%d-%m-%Y')}"
    docx_path = pasta_pecas_ia / f"{nome}.docx"
    pdf_path = pasta_pecas_ia / f"{nome}.pdf"

    try:
        # termos=False + data por extenso (não o placeholder "data do
        # protocolo eletrônico" do fluxo de Ações no Eproc) — minuta é peça
        # pronta pra uso imediato, não vai ser protocolada por aqui.
        _gerar_docx_sem_viuva(peticao_html, docx_path, termos=False, data=_data_por_extenso())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Falha ao formatar a minuta: {type(e).__name__}: {e}"[:400]}

    with contextlib.suppress(Exception):
        if pdf_path.exists():
            pdf_path.unlink()
    _docx_para_pdf(docx_path, pdf_path)
    if not pdf_path.exists():
        return {"ok": False, "erro": "A minuta foi formatada (.docx), mas a conversão pra PDF falhou "
                                      "(verifique se o Microsoft Word está instalado nesta máquina)."}

    return {"ok": True, "arquivos": [docx_path.name, pdf_path.name]}
