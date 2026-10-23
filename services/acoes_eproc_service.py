"""Módulo genérico "Ações no Eproc" (peticionamento no processo judicial vinculado a
um PAJ) — reutilizável em qualquer página do painel, assim como
`acoes_sisdpu_service.py`.

Listagem de prazos abertos/pendentes vem do cache local já sincronizado pela aba
"Prazos e-proc" (`ingestao/eproc_intimacoes.py`) — não navega no e-proc, é
instantânea. Rascunhos (evento, petição, anexos) ficam em
`dados/acoes_eproc_rascunhos/`. A execução real do protocolo (`executar_peticionamento`
+ `confirmar_peticionamento_real`) navega e preenche a tela "Movimentar/Peticionar"
de verdade via `ingestao.eproc_client` — ver essas duas funções pro fluxo em duas
etapas (preparar vs. protocolar de fato, irreversível)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import config

_log = logging.getLogger("acoes_eproc")

# Rascunhos de petição ("Gravar" p/ peticionar mais tarde) — mesma convenção de
# `dados/` na raiz do PAINEL SISDPU usada por `eproc_mcp_client.EPROC_DOCS_DIR`
# (cache/estado do painel, fora do workspace do Ofício Geral). Um JSON por PAJ
# (no máx. 1 rascunho por PAJ de cada vez) + subpasta p/ anexos "Externo" já
# enviados ao servidor (sem isso, o `File` do navegador não sobrevive a um
# reload — só existe na memória da aba enquanto ela ficar aberta).
RASCUNHOS_DIR = Path(__file__).resolve().parents[1].parent / "dados" / "acoes_eproc_rascunhos"


def _rascunho_json(paj_norm: str) -> Path:
    return RASCUNHOS_DIR / f"{paj_norm}.json"


def _rascunho_arquivos_dir(paj_norm: str) -> Path:
    return RASCUNHOS_DIR / paj_norm


def salvar_rascunho(paj_norm: str, dados: dict) -> dict:
    """Grava/sobrescreve o rascunho de petição do PAJ (1 por PAJ)."""
    if not paj_norm:
        return {"ok": False, "erro": "paj_norm vazio."}
    RASCUNHOS_DIR.mkdir(parents=True, exist_ok=True)
    doc = dict(dados)
    doc["paj_norm"] = paj_norm
    doc["salvo_em"] = datetime.now().isoformat(timespec="seconds")
    _rascunho_json(paj_norm).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "salvo_em": doc["salvo_em"]}


def obter_rascunho(paj_norm: str) -> dict | None:
    p = _rascunho_json(paj_norm)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def listar_rascunhos() -> list[dict]:
    """Todos os rascunhos salvos — alimenta a página "Petições pendentes".
    Enriquece com o nome do assistido lido do metadata.json do PAJ (não fica
    salvo no rascunho em si, pra não ficar desatualizado se o PAJ mudar).

    `par_sisdpu_paj_norm`: PAJ real (se houver) com um rascunho PENDENTE
    também em "Ações no SISDPU" — normalmente as duas nasceram juntas em
    "Movimentar Eproc + SISDPU" (movimentar_conjunto.html), mas a checagem
    vale pra qualquer par pendente do mesmo PAJ. Alimenta o badge "Eproc +
    SISDPU" da tabela de pendentes (pedido do usuário 2026-09-11 — sem ele,
    sair da página conjunta sem executar "perdia" o caminho de volta pra
    revisar os dois juntos). `paj_norm_vinculado` só existe em rascunhos
    gravados a partir de hoje — rascunhos MAIS ANTIGOS não têm esse campo,
    mas sempre usaram um PAJ de verdade como CHAVE quando havia um; cai pro
    formato dela (mesmo fallback do front, ver
    peticoes_pendentes.html:pajRealDe)."""
    if not RASCUNHOS_DIR.exists():
        return []
    from services import acoes_sisdpu_service

    itens = []
    for f in RASCUNHOS_DIR.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        chave = doc.get("paj_norm", "") or ""
        doc["assistido"] = _assistido_do_paj(chave)
        paj_real = doc.get("paj_norm_vinculado") or (chave if chave.startswith("PAJ-") else "")
        doc["par_sisdpu_paj_norm"] = (
            paj_real if paj_real and acoes_sisdpu_service.obter_rascunho(paj_real) else ""
        )
        itens.append(doc)
    itens.sort(key=lambda d: d.get("salvo_em", ""), reverse=True)
    return itens


def _assistido_do_paj(paj_norm: str) -> str:
    if not paj_norm:
        return ""
    meta_path = config.PAJS_DIR / paj_norm / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("assistido_caixa") or meta.get("assistido") or ""
    except Exception:
        return ""


def excluir_rascunho(paj_norm: str) -> dict:
    """Descarta o rascunho (e os anexos "Externo" já enviados pra ele)."""
    p = _rascunho_json(paj_norm)
    if p.exists():
        p.unlink()
    pasta = _rascunho_arquivos_dir(paj_norm)
    if pasta.exists():
        from services import exclusao_suave  # exclusão suave — services/exclusao_suave.py, 23/10/2026
        exclusao_suave.apagar_pasta(pasta, "rascunho-acoes-eproc")
    return {"ok": True}


async def salvar_arquivo_rascunho(paj_norm: str, nome_original: str, conteudo: bytes) -> dict:
    """Persiste os BYTES de um anexo "Externo" na pasta do rascunho — necessário
    porque um `File` do navegador não sobrevive a um reload da página; sem isso,
    o "Gravar" perderia justamente os anexos que não vieram do SISDPU."""
    if not paj_norm:
        return {"ok": False, "erro": "paj_norm vazio."}
    pasta = _rascunho_arquivos_dir(paj_norm)
    pasta.mkdir(parents=True, exist_ok=True)
    nome = re.sub(r"[^\w\-. ]", "_", nome_original or "arquivo")
    destino = pasta / nome
    # evita sobrescrever um anexo homônimo diferente (mesmo padrão de sufixo
    # numérico usado noutros pontos do painel)
    if destino.exists():
        base, ext = destino.stem, destino.suffix
        i = 2
        while (pasta / f"{base}_{i}{ext}").exists():
            i += 1
        destino = pasta / f"{base}_{i}{ext}"
    destino.write_bytes(conteudo)
    return {"ok": True, "nome_arquivo": destino.name, "tamanho": len(conteudo)}


def caminho_arquivo_rascunho(paj_norm: str, nome_arquivo: str) -> Path | None:
    """Resolve o caminho do arquivo garantindo que fique DENTRO da pasta do
    rascunho (nome_arquivo vem de um path param da URL — nunca confiar sem
    checar, mesmo já sendo sanitizado na gravação)."""
    pasta = _rascunho_arquivos_dir(paj_norm).resolve()
    p = (pasta / (nome_arquivo or "")).resolve()
    if pasta not in p.parents and p != pasta:
        return None
    return p if p.exists() and p.is_file() else None

# Script da skill global "PDF-Comprimir_Dividir" (mesma que o usuário já usa via
# `/PDF-Comprimir_Dividir`) — reduz DPI/qualidade JPEG e, se ainda faltar, divide em
# partes por página inteira. Reaproveitado aqui para resolver anexos do e-proc que
# excedem o limite de tamanho (só PDF — o script não trata áudio/imagem/vídeo).
_SKILL_COMPRESS_SPLIT = (
    Path.home() / ".claude" / "skills" / "PDF-Comprimir_Dividir" / "scripts" / "compress_split.py"
)
# Limite de "Documentos" do e-proc (PDF/HTM/HTML/KML) informado pelo usuário.
_EPROC_MAX_MB_DOCUMENTO = 11


def _normalizar_num_processo(s: str) -> str:
    """Só os dígitos do número CNJ (mesma normalização de `retornos_service`) —
    permite cruzar o formato do SISDPU (sem pontuação) com o do e-proc (com pontuação)."""
    return re.sub(r"\D", "", s or "")


async def importar_eventos(numero_processo: str) -> dict:
    """Consulta AO VIVO (via `eproc_client`) os eventos disponíveis para o processo
    de referência informado e persiste como a lista "todos" nas preferências de
    Ações no Eproc (Configurações). Preserva favoritos/frequentes já curados —
    descarta só os que sumirem da lista nova (mesmo padrão do "Importar do SISDPU")."""
    from ingestao import eproc_client
    from services import config_painel_service

    res = await eproc_client.listar_eventos_movimentacao(numero_processo)
    if not res.get("ok"):
        return res
    prefs = config_painel_service.set_eproc_eventos_todos(
        res.get("eventos", []), res.get("numero_processo", ""))
    return {"ok": True, "n_eventos": len(res.get("eventos", [])), "prefs": prefs}


async def importar_tipos_documento(numero_processo: str) -> dict:
    """Consulta AO VIVO os tipos de documento disponíveis (via `eproc_client`) para o
    processo de referência informado e persiste como a lista "todos" nas preferências
    de Ações no Eproc (Configurações). Cada anexo de uma petição precisa ser
    classificado num desses tipos — favoritos/frequentes agilizam essa escolha."""
    from ingestao import eproc_client
    from services import config_painel_service

    res = await eproc_client.listar_tipos_documento(numero_processo)
    if not res.get("ok"):
        return res
    prefs = config_painel_service.set_eproc_tipos_doc_todos(
        res.get("tipos", []), res.get("numero_processo", ""))
    return {"ok": True, "n_tipos": len(res.get("tipos", [])), "prefs": prefs}


async def importar_sigilos(numero_processo: str) -> dict:
    """Consulta AO VIVO os níveis de sigilo disponíveis por documento (via
    `eproc_client`) e persiste como a lista "todos" nas preferências de Ações no
    Eproc (Configurações). Lista curta e estável — sem favoritos/frequentes."""
    from ingestao import eproc_client
    from services import config_painel_service

    res = await eproc_client.listar_niveis_sigilo(numero_processo)
    if not res.get("ok"):
        return res
    prefs = config_painel_service.set_eproc_sigilos_todos(
        res.get("sigilos", []), res.get("numero_processo", ""))
    return {"ok": True, "n_sigilos": len(res.get("sigilos", [])), "prefs": prefs}


def listar_pecas_ia(paj_norm: str) -> list[dict]:
    """Arquivos da subpasta "Peças IA" do PAJ (petições/peças geradas pela análise
    de pretensão — services/analise_pretensao_service.py) — alimenta o modal
    "Adicionar Petições IA" em Ações no Eproc, mesmo padrão de "Adicionar do
    SISDPU" (anexos_sisdpu). Reaproveita `paj_service.ler_paj()`, que já separa
    "Peças IA/" do resto de `pecas_geradas` pelo prefixo do `caminho` — sem
    escanear a pasta de novo aqui."""
    from services.paj_service import ler_paj

    dados = ler_paj(paj_norm)
    if not dados:
        return []
    return [p for p in (dados.get("pecas_geradas") or []) if (p.get("caminho") or "").startswith("Peças IA/")]


async def comprimir_anexo(paj_norm: str, caminho: str) -> dict:
    """Comprime/divide um anexo em PDF de um PAJ, reaproveitando o SCRIPT da skill
    global `/PDF-Comprimir_Dividir` (`compress_split.py`: reduz DPI/qualidade JPEG e,
    se ainda faltar, divide em partes por página inteira), com o limite de tamanho do
    e-proc para "Documentos" (11MB) em vez do padrão da skill (9MB). O ORIGINAL nunca
    é apagado/sobrescrito.

    `anexos_sisdpu` NÃO é persistido em metadata.json — `paj_service.ler_paj()` o
    recalcula a cada chamada varrendo a pasta `Arquivos/` ao vivo. Como o script já
    grava o resultado NESSA MESMA pasta, basta rechamar `ler_paj()` depois e filtrar
    os arquivos novos — sem precisar (nem dever) escrever nada em metadata.json.

    Só funciona para PDF — os demais formatos aceitos pelo e-proc (áudio/imagem/
    vídeo) não têm uma rotina de compressão equivalente."""
    if not caminho.lower().endswith(".pdf"):
        return {"ok": False, "erro": "Compressão automática só está disponível para arquivos PDF."}
    if not _SKILL_COMPRESS_SPLIT.exists():
        return {"ok": False, "erro": "Skill de compressão (PDF-Comprimir_Dividir) não encontrada nesta máquina."}

    pasta_paj = Path(config.PAJS_DIR) / paj_norm
    arquivo = pasta_paj / caminho
    if not arquivo.exists():
        return {"ok": False, "erro": "Arquivo não encontrado no PAJ (caminho: " + caminho + ")."}

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(_SKILL_COMPRESS_SPLIT), str(arquivo),
        "--max-mb", str(_EPROC_MAX_MB_DOCUMENTO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detalhe = (stderr or stdout).decode("utf-8", errors="replace").strip()
        return {"ok": False, "erro": ("Falha ao comprimir: " + detalhe)[:500]}

    from services.paj_service import ler_paj

    base = arquivo.stem
    dados = ler_paj(paj_norm)
    if not dados:
        return {"ok": False, "erro": "PAJ não encontrado ao reler os anexos após a compressão."}
    novos = [
        a for a in dados.get("anexos_sisdpu", [])
        if a["nome"].startswith(base + " - comprimido") or a["nome"].startswith(base + " - parte ")
    ]
    if not novos:
        return {"ok": False, "erro": "A compressão rodou, mas nenhum arquivo de saída foi encontrado."}

    return {"ok": True, "novos": novos}


def _so_data(s: str) -> str:
    """Só o "dd/mm/aaaa" no início da string (ignora hora, se houver) — mesmo
    recorte usado por `_soData()` em `processo_judicial.js` pra casar prazo do
    Painel do Procurador com intimação da triagem MCP."""
    m = re.match(r"(\d{2}/\d{2}/\d{4})", s or "")
    return m.group(1) if m else ""


async def listar_prazos(numeros_processo: list[str]) -> list[dict]:
    """Prazos abertos/pendentes (cache de Prazos e-proc, escopado ao MEU Painel do
    Procurador — só intimações da DPU, nunca da parte contrária) para os processos
    informados — alimenta o checkbox "Selecione o(s) prazo(s) a ser(em) fechado(s)"
    da seção Ações no Eproc. Vazio quando nenhum dos processos aparece nas
    intimações já sincronizadas (ou a sincronização de Prazos e-proc nunca rodou).

    Usa `eproc_intimacoes.caso_e_prazos_mesclados()` — os prazos já vêm MESCLADOS/
    deduplicados entre categorias do Painel (uma mesma intimação pode estar listada
    em mais de uma categoria simultaneamente, ex. "pendente" e "pendente_urgente" ao
    mesmo tempo). **Bug real corrigido 2026-08-13** (PAJ 2026/029-01734): antes,
    somar prazos de TODAS as categorias sem dedup mostrava a MESMA intimação
    duas vezes (3 prazos exibidos pra fechar quando só 2 existiam de fato).

    Cada item ganha um `label` com o TEXTO OFICIAL do e-proc — mesmo padrão que
    aparece na própria tela de peticionamento (usuário 2026-08-12), ex.:
    "Expedida/certificada a intimação eletrônica - Sentença (21/07/2026 13:58:05)
    - Ref. Evento 22 - EVERALDO COLACO". Isso vem da triagem MCP (`eproc_mcp_client
    .triagem_processo`), que devolve por INTIMAÇÃO (não por processo) o `rotulo`
    exato, o nº do próprio evento de intimação (`evento_intimacao` — não confundir
    com o evento REFERENCIADO, ex. a Sentença) e a parte intimada com o papel na
    frente ("AUTOR - Fulano"). A triagem traz TODAS as partes (inclusive a
    contrária, ex. INSS) — o casamento usa `eproc_procurador_service` (cabeçalho
    de partes do processo + identificador do procurador logado) pra restringir
    às partes que EU realmente represento, sem adivinhar por polo: por processo,
    casa cada prazo do Painel com a intimação da triagem de MESMO DIA de envio,
    MESMO nº de dias e da MESMA parte associada a mim (um-para-um, via `usadas`,
    evita casar duas vezes o mesmo evento). Sem casamento (processo sem triagem
    disponível, identidade indisponível — cofre trancado —, ou nenhum evento
    bate — ex. cache desatualizado), `label` fica vazio e o front cai no formato
    aproximado antigo (evento + prazo + final)."""
    from ingestao import eproc_intimacoes, eproc_mcp_client
    from services import eproc_procurador_service

    alvo = [n for n in (numeros_processo or []) if n]
    if not alvo:
        return []

    # 1 chamada por processo distinto, já mesclada/deduplicada entre categorias
    # (mesma fonte usada por "Intimações pendentes" na página do PAJ).
    por_processo: dict[str, dict] = {}  # num -> {"processo": str, "prazos": [...]}
    for numero in alvo:
        num = _normalizar_num_processo(numero)
        if num in por_processo:
            continue
        m = eproc_intimacoes.caso_e_prazos_mesclados(numero)
        if m.get("encontrado"):
            por_processo[num] = {
                "processo": (m.get("caso") or {}).get("processo") or numero,
                "prazos": m.get("prazos") or [],
            }
    if not por_processo:
        return []

    meu_id = eproc_procurador_service.meu_identificador()

    # 1 triagem por PROCESSO distinto (não por prazo) — barata o bastante (só os
    # processos deste PAJ, tipicamente 1).
    triagens: dict[str, tuple[list[dict], set[str]]] = {}
    for num, entry in por_processo.items():
        intimacoes: list[dict] = []
        minhas_partes: set[str] = set()
        for instancia in (1, 2):
            try:
                res = await eproc_mcp_client.triagem_processo(entry["processo"], instancia=instancia)
            except Exception:
                res = None
            if res and not res.get("erro"):
                intimacoes = res.get("intimacoes") or []
                minhas_partes = eproc_procurador_service.partes_do_procurador(
                    res.get("cabecalho") or {}, meu_id)
                break
        triagens[num] = (intimacoes, minhas_partes)

    itens: list[dict] = []
    for num, entry in por_processo.items():
        intimacoes, minhas_partes = triagens.get(num) or ([], set())
        usadas: set[int] = set()
        for idx, p in enumerate(entry["prazos"]):
            dia_p = _so_data(p.get("data_envio", ""))
            dias_p = str(p.get("prazo_dias") or "")
            # Junta TODOS os candidatos do mesmo dia+dias que sejam de uma parte
            # associada a mim (ver eproc_procurador_service) — pode haver mais de
            # um só quando eu represento mais de uma parte no MESMO ato (ex.
            # litisconsórcio); nunca escolhe pelo primeiro da lista.
            candidatos = []
            for i, it in enumerate(intimacoes):
                if i in usadas or not it.get("ativa"):
                    continue
                if not eproc_procurador_service.parte_intimada_e_minha(
                        it.get("parte_intimada", ""), minhas_partes):
                    continue
                if _so_data(it.get("data_hora", "")) != dia_p:
                    continue
                if dias_p and str(it.get("prazo_dias") or "") != dias_p:
                    continue
                candidatos.append((i, it))
            label = ""
            if candidatos:
                i, it = candidatos[0]
                usadas.add(i)
                nome = (it.get("parte_intimada") or "").split(" - ", 1)
                nome = nome[1] if len(nome) > 1 else nome[0]
                label = (f'{it.get("rotulo", "")} ({it.get("data_hora", "")}) '
                         f'- Ref. Evento {it.get("evento_intimacao", "")} - {nome}').strip()
            final_real = p.get("final_prazo") or ""
            itens.append({
                "id": f"{num}:{idx}",
                "processo": entry["processo"],
                "evento": p.get("evento", ""),
                "prazo_dias": p.get("prazo_dias", ""),
                "data_final": final_real or p.get("final_estimado") or "",
                "estimado": not bool(final_real),
                "label": label,
            })
    return itens


async def resolver_classe_processo(numero: str) -> dict:
    """Classe processual (ex.: "Cumprimento de Sentença contra Fazenda Pública
    (JEF)") de UM processo, via `/api/eproc-mcp/consultar` (MNI/raspador HTTP —
    sem abrir aba no navegador do e-proc, mesma tool usada em "Consultar
    processo"). Alimenta o rótulo do <select> "Processo judicial a movimentar"
    quando o PAJ tem mais de um processo vinculado (pedido do usuário
    2026-09-29 — distinguir os processos só por número/juízo era pouco pra
    escolher o certo).

    Tenta 1ª e 2ª instância nesta ordem, mesma convenção já usada em
    `listar_prazos`/`calcular_residual_prazos` (nenhum campo local guarda a
    instância de cada processo do PAJ) — para no primeiro que resolver."""
    from ingestao import eproc_mcp_client

    for instancia in (1, 2):
        try:
            res = await eproc_mcp_client.consultar_processo(numero, instancia=instancia)
        except Exception:
            res = None
        if res and not res.get("erro") and res.get("classe"):
            return {"ok": True, "classe": res["classe"]}
    return {"ok": False, "classe": ""}


def resolver_prazo_por_id(prazo_id: str) -> dict | None:
    """Devolve o registro CRU do prazo (com `data_envio`, usado pra casar contra
    a triagem MCP — ver `peticao_ia_service`) + o `caso` (processo/assunto/
    partes) a que pertence. `prazo_id` é o formato de `listar_prazos`
    (`"<numero_sem_pontuacao>:<indice>"` — índice da lista MESCLADA/deduplicada
    entre categorias, ver `eproc_intimacoes.caso_e_prazos_mesclados`). None se
    não achar (cache desatualizado desde a seleção do prazo na tela, p.ex.)."""
    from ingestao import eproc_intimacoes

    partes = (prazo_id or "").split(":")
    if len(partes) != 2:
        return None
    num_alvo, idx_str = partes
    try:
        idx = int(idx_str)
    except ValueError:
        return None

    m = eproc_intimacoes.caso_e_prazos_mesclados(num_alvo)
    if not m.get("encontrado"):
        return None
    prazos = m.get("prazos") or []
    if not (0 <= idx < len(prazos)):
        return None
    return {"prazo": prazos[idx], "caso": m.get("caso") or {}}


# --- "Executar no e-proc" (peticionamento real) -------------------------------
# Mesma normalização/regra de "evento sem anexo" do front (`acoes_eproc.js:
# eventoSemAnexo`) — refeita aqui porque o servidor é quem de fato decide o que
# entra na chamada a `eproc_client.preparar_peticionamento`, não confia soment
# no que o JS filtrou antes de mandar o rascunho.
def _normalizar_texto(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode("ascii").lower().strip()


def _evento_sem_anexo(evento: str) -> bool:
    return _normalizar_texto(evento) == _normalizar_texto("Ciência, com renúncia ao prazo")


def _caminho_anexo(paj_norm: str, anexo: dict) -> Path | None:
    """Resolve o caminho em disco de um anexo do rascunho — 'paj' vem da pasta
    do PAJ (`docPajKey`, relativo), 'rascunho'/'upload' já persistido vêm da
    pasta do rascunho (`nomeArquivo`). None se a origem for 'upload' sem upload
    ainda persistido (não deveria chegar aqui — `salvar_rascunho` já normaliza
    'upload' pendente pra 'rascunho' só depois do envio, ver acoes_eproc.js)."""
    origem = anexo.get("origem")
    if origem == "paj":
        p = Path(config.PAJS_DIR) / paj_norm / (anexo.get("docPajKey") or "")
        return p if p.exists() else None
    if origem in ("rascunho", "upload"):
        nome = anexo.get("nomeArquivo")
        if not nome:
            return None
        p = _rascunho_arquivos_dir(paj_norm) / nome
        return p if p.exists() else None
    return None


def _anexo_relativo_ao_paj(paj_norm: str, caminho_absoluto: str) -> str | None:
    """Caminho relativo à pasta do PAJ (resolvível por `paj_service.ler_arquivo`/
    `/files/<paj>/<caminho>`) se `caminho_absoluto` já estiver DENTRO dela —
    None se estiver fora (ex.: anexo "Externo" salvo só na pasta de rascunhos
    de Ações no Eproc, `dados/acoes_eproc_rascunhos/`, fora de `PAJS_DIR`)."""
    pasta_paj = (Path(config.PAJS_DIR) / paj_norm).resolve()
    p = Path(caminho_absoluto).resolve()
    try:
        return str(p.relative_to(pasta_paj)).replace("\\", "/")
    except ValueError:
        return None


# Subpasta dedicada, DENTRO da pasta do PAJ, para onde um anexo "Externo" da
# petição (só existia na pasta de rascunhos de Ações no Eproc até aqui) é
# copiado antes de virar uma referência 'paj' válida pro SISDPU — ver
# `preparar_documentos_para_movimentacao_sisdpu`.
_PASTA_ANEXOS_EPROC_NO_PAJ = "Anexos e-proc"


def preparar_documentos_para_movimentacao_sisdpu(paj_norm: str, documentos: list[dict]) -> dict:
    """Monta a lista de anexos pronta pra "Anexos da movimentação" de Ações no
    SISDPU a partir dos documentos JÁ PROTOCOLADOS numa petição do e-proc (ver
    `montar_resumo_execucao` -> `documentos`, cada um com `caminho` ABSOLUTO já
    resolvido — capturado em memória pelo front ANTES de confirmar o
    peticionamento, porque o rascunho de Ações no Eproc já foi descartado a
    esta altura).

    TODOS os documentos desta petição entram, cada um com Tipo de Arquivo
    pré-estabelecido como "Petições" (pedido do usuário 2026-08-19 — antes só
    a petição em si entrava aqui; os demais anexos, embora já protocolados no
    e-proc, ficavam de fora e o usuário tinha que reclassificar um por um se
    quisesse registrá-los também no SISDPU).

    Cada documento vira uma referência 'paj' (caminho relativo dentro da pasta
    do PAJ) — a maioria já mora lá (Arquivos/, Peças IA/, ou a própria raiz, no
    caso da petição gerada por IA). Um anexo "Externo" que só existia na pasta
    de rascunhos de Ações no Eproc (fora de `PAJS_DIR`) é COPIADO pra dentro da
    pasta do PAJ (subpasta "Anexos e-proc") antes — sem isso, o `ref` não
    resolveria e `acoes_sisdpu_service.executar_stream` descartaria o anexo
    EM SILÊNCIO na hora de executar.

    Cada documento é processado em bloco try/except PRÓPRIO — uma falha ao
    copiar UM arquivo (permissão, antivírus/firewall bloqueando escrita — ver
    [[painel-sisdpu-fortinet-subprocess-workaround]]) não pode derrubar a
    rota inteira com um 500 puro: o chamador (`api_preparar_anexos_sisdpu`)
    receberia "Internal Server Error" (texto, não JSON), e o front, que só
    tentava fazer `.json()` daquilo, falhava dentro de um `catch` silencioso —
    o sintoma real relatado pelo usuário (2026-09-16): a petição em texto
    chegava na movimentação, o PDF protocolado não, sem erro nenhum visível.
    Retorna também `falhas` (nome + motivo) pro chamador avisar o usuário em
    vez de só ficar quieto com uma lista de anexos incompleta."""
    resultado: list[dict] = []
    falhas: list[dict] = []
    for doc in documentos or []:
        caminho_abs = doc.get("caminho") or ""
        nome = doc.get("nome") or (Path(caminho_abs).name if caminho_abs else "documento")
        if not caminho_abs:
            falhas.append({"nome": nome, "motivo": "sem caminho de origem informado"})
            continue
        try:
            rel = _anexo_relativo_ao_paj(paj_norm, caminho_abs)
            if rel is None:
                origem_arquivo = Path(caminho_abs)
                if not origem_arquivo.exists():
                    falhas.append({"nome": nome, "motivo": "arquivo não encontrado em disco"})
                    continue
                pasta_destino = Path(config.PAJS_DIR) / paj_norm / _PASTA_ANEXOS_EPROC_NO_PAJ
                pasta_destino.mkdir(parents=True, exist_ok=True)
                destino = pasta_destino / origem_arquivo.name
                # Sempre sobrescreve (nunca reaproveita um destino já existente) —
                # o nome sozinho não garante mesmo conteúdo entre petições
                # diferentes (ex.: duas provas homônimas "documento.pdf" em datas
                # distintas); a fonte aqui É o documento recém-protocolado, sempre
                # a verdade.
                shutil.copyfile(origem_arquivo, destino)
                rel = f"{_PASTA_ANEXOS_EPROC_NO_PAJ}/{destino.name}"
        except Exception as e:  # noqa: BLE001 — 1 doc não pode derrubar o lote inteiro
            falhas.append({"nome": nome, "motivo": f"{type(e).__name__}: {e}"[:200]})
            continue
        resultado.append({
            "origem": "paj", "ref": rel, "nome": nome, "tamanho": doc.get("tamanho") or 0,
            "descricao": nome, "tipoArquivo": "Petições",
        })
    return {"anexos": resultado, "falhas": falhas}


async def montar_resumo_execucao(paj_norm: str) -> dict:
    """Resolve o rascunho salvo do PAJ num resumo pronto pra revisão do usuário
    ANTES de preparar de verdade no e-proc — evento, processo, a lista de
    anexos que serão de fato enviados (já excluindo os ignorados por
    `eventoSemAnexo`), cada um com o caminho em disco resolvido, e os PRAZOS
    marcados no checkbox "Selecione o(s) prazo(s)..." do Painel (rótulo oficial
    do e-proc, mesmo formato usado pela tela real "Movimentar/Peticionar" —
    ver `eproc_client._marcar_prazos_fechar`). Erro claro se o rascunho não
    existir, o processo/evento estiver vazio, ou algum anexo não for
    encontrado em disco (arquivo movido/apagado desde que foi adicionado).

    Prazo selecionado no rascunho que não resolve mais pra um `label` (cache
    de Prazos e-proc desatualizado desde a seleção, ou o prazo já fechou)
    entra em `prazos_nao_resolvidos` — não é omitido silenciosamente, o
    chamador decide o que fazer (o front mostra aviso antes de "Preparar")."""
    rascunho = obter_rascunho(paj_norm)
    if not rascunho:
        return {"ok": False, "erro": "Nenhum rascunho salvo para este PAJ — grave o rascunho antes de executar."}
    processo = (rascunho.get("processo") or "").strip()
    evento = (rascunho.get("evento") or "").strip()
    if not processo:
        return {"ok": False, "erro": "Rascunho sem processo judicial selecionado."}
    if not evento:
        return {"ok": False, "erro": "Rascunho sem evento a ser lançado selecionado."}

    sem_anexo = _evento_sem_anexo(evento)
    documentos = []
    if not sem_anexo:
        for i, anexo in enumerate(rascunho.get("anexos") or [], start=1):
            caminho = _caminho_anexo(paj_norm, anexo)
            if caminho is None:
                nome = anexo.get("docLabelCache") or anexo.get("nomeArquivo") or f"anexo {i}"
                return {"ok": False, "erro": f'Anexo "{nome}" não encontrado em disco — remova-o do '
                                             f"rascunho e adicione de novo antes de executar."}
            if not anexo.get("tipo"):
                nome = anexo.get("docLabelCache") or anexo.get("nomeArquivo") or f"anexo {i}"
                return {"ok": False, "erro": f'Anexo "{nome}" sem Tipo selecionado.'}
            documentos.append({
                "caminho": str(caminho), "tipo": anexo["tipo"], "sigilo": anexo.get("sigilo") or "",
                "nome": anexo.get("docLabelCache") or anexo.get("nomeArquivo") or caminho.name,
                "tamanho": anexo.get("tamanho") or 0,
            })

    ids_selecionados = [pid for pid, sel in (rascunho.get("prazos_selecionados") or {}).items() if sel]
    prazos_labels: list[str] = []
    prazos_nao_resolvidos: list[str] = []
    prazos_raw_resolvidos: dict[str, dict] = {}
    if ids_selecionados:
        itens = await listar_prazos([processo])
        por_id = {it["id"]: it for it in itens}
        for pid in ids_selecionados:
            it = por_id.get(pid)
            if it and it.get("label"):
                prazos_labels.append(it["label"])
                # Resolve e GRAVA o dado CRU (evento/data_envio/prazo_dias) já
                # AGORA, no mesmo instante em que soubemos que este id ainda é
                # válido — não deixa pra uma segunda consulta independente na
                # hora de confirmar (`confirmar_peticionamento_real`), que já
                # rodou numa janela de tempo diferente e falhou em resolver 1
                # de 2 prazos selecionados no mesmo peticionamento (bug real
                # 2026-09-26, PAJ 2026/029-01734, processo
                # 5035343-61.2026.4.04.7000 — causa exata não reconstruída,
                # mas o padrão "2 consultas independentes ao mesmo cache em
                # momentos diferentes" é o suspeito óbvio). Persistido no
                # PRÓPRIO rascunho (abaixo) pra sobreviver até a confirmação.
                resolvido = resolver_prazo_por_id(pid)
                if resolvido:
                    prazos_raw_resolvidos[pid] = resolvido["prazo"]
            else:
                prazos_nao_resolvidos.append(pid)
        if prazos_raw_resolvidos:
            salvar_rascunho(paj_norm, {**rascunho, "prazos_raw_resolvidos": {
                **(rascunho.get("prazos_raw_resolvidos") or {}), **prazos_raw_resolvidos,
            }})

    return {
        "ok": True, "processo": processo, "evento": evento,
        "evento_sem_anexo": sem_anexo, "documentos": documentos,
        "prazos": prazos_labels, "prazos_nao_resolvidos": prazos_nao_resolvidos,
        # Configuração de Gerenciamento de Processo (ver seção mais abaixo) — capturada
        # AQUI (resumo, antes de preparar/confirmar) porque `confirmar_peticionamento_real`
        # descarta o rascunho em caso de sucesso; o front guarda isto em memória
        # (`execResumo.gerenciamento`) pra ainda ter os parâmetros na mão depois de
        # peticionar, na hora de perguntar Sim/Não.
        "gerenciamento": rascunho.get("gerenciamento") or {},
    }


async def executar_peticionamento(paj_norm: str) -> dict:
    """Prepara de verdade a tela "Movimentar/Peticionar" do e-proc a partir do
    rascunho salvo — evento + anexos (upload, Tipo, Sigilo), terminando em
    "Confirmar seleção de documentos". NÃO clica em "Peticionar" (ver
    `confirmar_peticionamento_real`, chamada só depois de o usuário confirmar
    explicitamente o resumo devolvido aqui)."""
    from ingestao import eproc_client

    resumo = await montar_resumo_execucao(paj_norm)
    if not resumo.get("ok"):
        return resumo
    res = await eproc_client.preparar_peticionamento(
        resumo["processo"], resumo["evento"], resumo["documentos"],
        prazos_labels=resumo.get("prazos") or [])
    if not res.get("ok"):
        return res
    return {**resumo, **res}


async def importar_procuradores_gerenciamento(numero_processo: str) -> dict:
    """Consulta AO VIVO (via `eproc_client`) os procuradores/unidades disponíveis
    como destino no Gerenciamento de Processo do processo de referência informado
    e persiste como a lista "todos" nas preferências de Ações no Eproc
    (Configurações). Preserva favoritos/frequentes já curados — descarta só os
    que sumirem da lista nova (mesmo padrão de `importar_eventos`)."""
    from ingestao import eproc_client
    from services import config_painel_service

    res = await eproc_client.listar_procuradores_gerenciamento(numero_processo)
    if not res.get("ok"):
        return res
    prefs = config_painel_service.set_eproc_procuradores_todos(
        res.get("procuradores", []), res.get("numero_processo", ""))
    return {"ok": True, "n_procuradores": len(res.get("procuradores", [])), "prefs": prefs}


# --- "Gerenciamento de Processo" (substabelecimento) ---------------------------
# Sempre a ÚLTIMA coisa feita no processo, depois de confirmado o peticionamento/
# movimento — ver `acoes_eproc.js:confirmarPeticionamentoReal`, que pergunta
# (Sim/Não, com os parâmetros) se o usuário quer executar isto logo após
# protocolar, mesmo idioma já usado ali para "movimentar o SISDPU agora".
#
# Recebe processo/tipo/procurador_valor DIRETO do chamador (não relê o rascunho
# do disco) porque `confirmar_peticionamento_real` já descartou o rascunho a
# esta altura (sucesso do peticionamento) — o front captura esses parâmetros em
# `execResumo.gerenciamento` (ver `montar_resumo_execucao`, chamado ANTES do
# peticionamento) e os guarda em memória até a hora de perguntar Sim/Não.
async def _prazo_residual_no_meu_painel(processo: str) -> dict:
    """Confere se sobra alguma intimação ainda ATIVA (agora, ao vivo) entre os
    prazos que o cache do Painel do Procurador reconhece como MEUS pra este
    processo — nunca "qualquer coisa da DPU": o processo pode ter outro
    procurador da DPU cadastrado, inclusive em polo adverso, com prazo próprio
    que não me diz respeito. Restringir ao Painel do Procurador (em vez de
    adivinhar por texto de parte/polo) já resolve isso sem heurística — é o
    e-proc quem escopa por procurador registrado, não eu.

    Existe porque Gerenciamento de Processo (substabelecimento) transfere o
    processo INTEIRO pro novo procurador, inclusive qualquer prazo ainda
    aberto nele — seja um coautor esquecido no MESMO ato simultâneo (falha
    parcial ao fechar), seja um ato AUTÔNOMO diferente ainda pendente. Pedido
    explícito do usuário (2026-09-XX): isso nunca pode acontecer por acidente,
    então o gerenciamento fica bloqueado enquanto sobrar qualquer prazo meu
    ativo neste processo — não interessa de qual ato ele veio.

    Cruza o CACHE do Painel (só confirma QUAIS prazos são meus) com a
    TRIAGEM AO VIVO (confirma se aquele prazo específico ainda está aberto
    agora — o cache só atualiza por sincronização manual, pode estar
    desatualizado por minutos/horas). Decisão explícita do usuário
    (2026-09-XX): aceitar esse gap de frescor em vez de forçar uma
    sincronização completa antes de cada gerenciamento — mais rápido, ao
    custo de um prazo novíssimo (chegado depois da última sincronização)
    poder escapar até a próxima. Se a triagem ao vivo falhar (MCP
    indisponível), assume o pior e bloqueia — nunca libera às cegas.

    Retorna {"tem_residual": bool, "quantidade": int}."""
    from ingestao import eproc_intimacoes, eproc_mcp_client

    meus = eproc_intimacoes.prazos_processo(processo).get("prazos") or []
    if not meus:
        return {"tem_residual": False, "quantidade": 0}

    intimacoes: list[dict] = []
    for instancia in (1, 2):
        try:
            res = await eproc_mcp_client.triagem_processo(processo, instancia=instancia)
        except Exception:
            res = None
        if res and not res.get("erro"):
            intimacoes = res.get("intimacoes") or []
            break
    else:
        return {"tem_residual": True, "quantidade": len(meus)}

    residuais = 0
    for p in meus:
        dia_p = _so_data(p.get("data_envio", ""))
        dias_p = str(p.get("prazo_dias") or "")
        for it in intimacoes:
            if not it.get("ativa"):
                continue
            if _so_data(it.get("data_hora", "")) != dia_p:
                continue
            if dias_p and str(it.get("prazo_dias") or "") != dias_p:
                continue
            residuais += 1
            break
    return {"tem_residual": residuais > 0, "quantidade": residuais}


async def executar_gerenciamento_real(processo: str, tipo: str, procurador_valor: str = "") -> dict:
    """Executa de fato o Gerenciamento de Processo (substituir/associar/
    desassociar procurador) — IRREVERSÍVEL, mesmo efeito de um substabelecimento
    real no e-proc. Só chamar depois que o usuário confirmar explicitamente
    tipo + procurador destino.

    Registra no Histórico do painel (pedido do usuário 2026-09-14 — gerenciamento
    nunca deixava rastro nenhum ali, nem disparado automaticamente após
    peticionar nem "puro", pelo modal avulso do minimenu — ver
    `static/js/app.js:gerenciarProcessoApp`; os DOIS casos passam por esta MESMA
    função, então um só ponto de registro cobre os dois). Registra sempre que o
    automação de fato rodou no e-proc (`res["ok"]`), mesmo quando o resultado
    não é um sucesso claro (`provavel_sucesso=False`/partes com erro) — o
    `detalhe` reflete o que de fato aconteceu; um gerenciamento que falhou
    parcialmente é MAIS importante de aparecer no Histórico, não menos. Tenta
    achar o PAJ deste processo (`paj_processo_link_service`) só pra enriquecer
    o registro — nunca exige um (o modal avulso não pede PAJ nenhum); com 0 ou
    mais de 1 candidato, registra sem PAJ (ambíguo não é resolvido às cegas).

    BLOQUEIA se sobrar prazo residual no MEU Painel do Procurador pra este
    processo (`_prazo_residual_no_meu_painel`) — gerenciar transfere o
    processo INTEIRO pro novo procurador, inclusive qualquer prazo ainda
    aberto (coautor esquecido no mesmo ato simultâneo, ou ato autônomo
    diferente ainda pendente); pedido explícito do usuário (2026-09-XX), isso
    nunca pode acontecer por acidente. `dar_mera_ciencia` (mera_ciencia_service)
    já trata esse bloqueio como um passo PULADO, não um erro fatal — fecha o
    prazo do ato atual e a movimentação do SISDPU normalmente, só não
    gerencia ainda."""
    from ingestao import eproc_client
    from ingestao.eproc_client import TIPOS_GERENCIAMENTO

    processo = (processo or "").strip()
    tipo = (tipo or "").strip()
    if not processo:
        return {"ok": False, "erro": "processo é obrigatório."}
    if tipo not in TIPOS_GERENCIAMENTO:
        return {"ok": False, "erro": f"Tipo de gerenciamento inválido: {tipo!r}."}

    from services import historico_painel, paj_processo_link_service

    tipo_label = TIPOS_GERENCIAMENTO.get(tipo, tipo)
    paj_norm, paj_label = "", ""
    try:
        candidatos = paj_processo_link_service.resolver_pajs_de_processo(processo)
        if len(candidatos) == 1:
            paj_norm = candidatos[0].get("paj_norm") or ""
            paj_label = candidatos[0].get("paj_label") or ""
    except Exception:  # noqa: BLE001
        pass

    def _registrar(detalhe: str) -> None:
        historico_painel.registrar({
            "paj": paj_label, "paj_norm": paj_norm,
            "origem": "eproc", "atividade": "Gerenciamento de processo (substabelecimento)",
            "movimentacao": tipo_label, "tramitado_para": procurador_valor or "",
            "concluido": False, "detalhe": detalhe[:300],
        })

    residual = await _prazo_residual_no_meu_painel(processo)
    if residual["tem_residual"]:
        # Bloqueio ANTES de tentar de verdade no e-proc (nem chega a abrir a
        # tela) — mesmo assim precisa aparecer no Histórico: sem isso, um
        # gerenciamento configurado numa movimentação conjunta simplesmente
        # sumia sem deixar rastro nenhum (bug real 2026-09-22, PAJ
        # 2026/029-01856 — o bloqueio em si estava correto, só o silêncio
        # total ao redor dele não).
        detalhe = f"Processo {processo} — NÃO executado: prazo residual em aberto ({residual['quantidade']}) no Painel do Procurador"
        _registrar(detalhe)
        return {
            "ok": False, "bloqueado_por_residual": True,
            "quantidade_residual": residual["quantidade"],
            "erro": (f"Gerenciamento bloqueado: ainda há {residual['quantidade']} prazo(s) em aberto no seu "
                     f"Painel do Procurador para este processo. Gerenciar agora transferiria esse(s) "
                     f"prazo(s) para o novo procurador — feche todos antes de gerenciar."),
        }

    res = await eproc_client.gerenciar_processo(processo, tipo, procurador_valor or "")

    if res.get("ok"):
        partes_marcadas = res.get("partes_marcadas") or []
        partes_com_erro = res.get("partes_com_erro") or []
        if res.get("provavel_sucesso"):
            detalhe = f"Processo {processo} — {len(partes_marcadas)} parte(s) gerenciada(s) com sucesso"
        elif partes_com_erro:
            detalhe = f"Processo {processo} — ATENÇÃO: {len(partes_com_erro)} parte(s) com erro, confira"
        else:
            detalhe = f"Processo {processo} — resultado não confirmado automaticamente, confira a aba do e-proc"
    else:
        # Falha real na automação (não o bloqueio por residual, já tratado
        # acima) — também precisa ficar visível no Histórico, não só a tela.
        detalhe = f"Processo {processo} — NÃO executado: {res.get('erro') or 'erro desconhecido'}"
    _registrar(detalhe)

    return res


async def confirmar_peticionamento_real(paj_norm: str, processo: str) -> dict:
    """Clica "Peticionar" de fato na aba já preparada por `executar_peticionamento`
    — IRREVERSÍVEL, protocolo real na vara. Em caso de sucesso, descarta o
    rascunho (mesmo efeito de `excluir_rascunho` — não há mais o que retomar
    depois de protocolado).

    Lê o `evento` do rascunho ANTES de descartá-lo e repassa a
    `eproc_client.confirmar_peticionamento` — necessário pra identificar a
    linha CERTA na tabela de eventos do e-proc quando o evento lançado dispara
    um evento automático em cascata (ver docstring de `confirmar_peticionamento`,
    bug real corrigido 2026-08-13: recurso inominado gerando sozinho a
    intimação da contraparte pra contrarrazões, com nº de evento maior)."""
    from ingestao import eproc_client

    rascunho = obter_rascunho(paj_norm) or {}
    evento = (rascunho.get("evento") or "").strip()
    res = await eproc_client.confirmar_peticionamento(processo, evento=evento)
    if res.get("ok") and res.get("provavel_sucesso"):
        # Baixa LOCAL dos prazos marcados pra fechar nesta petição — sem isso
        # a tabela de Prazos e-proc (e tudo que dela deriva: Agenda, badge de
        # intimação pendente, checkbox desta mesma tela, página Intimações)
        # só refletiria o prazo fechado na PRÓXIMA "Sincronizar intimações"
        # (pedido do usuário 2026-09-19). Ver `services.prazo_baixa_service`
        # — é otimista/temporário, a próxima sincronização real sempre vence.
        from services import prazo_baixa_service

        ids_selecionados = [pid for pid, sel in (rascunho.get("prazos_selecionados") or {}).items() if sel]
        prazos_raw_resolvidos = rascunho.get("prazos_raw_resolvidos") or {}
        for pid in ids_selecionados:
            # Prefere o dado CRU já resolvido e GRAVADO em `montar_resumo_execucao`
            # (no momento em que o resumo foi montado pra revisão, ver lá) —
            # só faz uma consulta nova se por algum motivo não tiver sido
            # persistido (rascunho antigo, ou fluxo que pulou o resumo). Evita
            # uma SEGUNDA consulta independente ao cache de `eproc_intimacoes`
            # aqui, que é exatamente onde o bug de 2026-09-26 aconteceu.
            prazo_bruto = prazos_raw_resolvidos.get(pid)
            if not prazo_bruto:
                resolvido = resolver_prazo_por_id(pid)
                prazo_bruto = resolvido["prazo"] if resolvido else None
            if prazo_bruto:
                prazo_baixa_service.marcar_baixa(
                    processo, prazo_bruto, origem="eproc-peticionamento",
                    detalhe=f"{paj_norm} — {evento}" if evento else paj_norm)
            else:
                # NUNCA pode passar em silêncio — sem isso, um prazo genuinamente
                # fechado no e-proc (via _marcar_prazos_fechar, que já rodou com
                # sucesso nesta mesma chamada) ficava com a baixa LOCAL perdida
                # sem deixar rastro nenhum: a tabela de Prazos e-proc continuava
                # mostrando "pendente" até a próxima sincronização real, e
                # ninguém sabia o motivo. Bug real 2026-09-26 (PAJ
                # 2026/029-01734, processo 5035343-61.2026.4.04.7000): 2 prazos
                # selecionados no mesmo peticionamento, só 1 registrou baixa —
                # o outro (`pid` aqui) não resolveu mais contra o cache de
                # `eproc_intimacoes` no momento da confirmação (cache pode ter
                # mudado entre a seleção do checkbox e a confirmação real).
                _log.warning(
                    "Baixa local de prazo NAO registrada: id=%s nao resolveu contra o cache atual "
                    "(processo=%s, paj=%s, evento=%s) — prazo pode ter fechado no e-proc mas a "
                    "tabela local so refletira isso na proxima sincronizacao real.",
                    pid, processo, paj_norm, evento)
        # Registro no Histórico do painel — próprio pra este ato (nunca existia
        # antes: peticionar no e-proc não deixava rastro nenhum ali, só a
        # movimentação SISDPU consequente, quando executada em algum console
        # que já registrasse — pedido do usuário 2026-09-14). Só quando há um
        # PAJ REAL por trás ("PAJ-..." — petição avulsa, "PROC-...", não tem
        # PAJ pra registrar contra nessa tela, que é organizada por PAJ).
        if paj_norm.startswith("PAJ-"):
            from services import historico_painel

            numero_evento = res.get("numero_evento") or ""
            detalhe = f"Processo {processo}" + (f" — Evento {numero_evento}" if numero_evento else "")
            historico_painel.registrar({
                "paj": rascunho.get("paj_label", ""), "paj_norm": paj_norm,
                "origem": "eproc", "atividade": "Peticionamento no e-proc",
                "movimentacao": evento or "—", "tramitado_para": "",
                "concluido": False, "detalhe": detalhe[:300],
            })
        excluir_rascunho(paj_norm)
    return res
