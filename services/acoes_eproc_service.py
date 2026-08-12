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
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import config

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
    salvo no rascunho em si, pra não ficar desatualizado se o PAJ mudar)."""
    if not RASCUNHOS_DIR.exists():
        return []
    itens = []
    for f in RASCUNHOS_DIR.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        doc["assistido"] = _assistido_do_paj(doc.get("paj_norm", ""))
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
        shutil.rmtree(pasta, ignore_errors=True)
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


def listar_prazos(numeros_processo: list[str]) -> list[dict]:
    """Prazos abertos/pendentes (cache de Prazos e-proc) para os processos informados —
    alimenta o checkbox "Selecione o(s) prazo(s) a ser(em) fechado(s)" da seção Ações no
    Eproc. Cada prazo vira um item com `id` estável (chave do checkbox), o processo a
    que pertence, o evento que o gerou e a data final (real ou estimada). Vazio quando
    nenhum dos processos aparece nas intimações já sincronizadas (ou a sincronização de
    Prazos e-proc nunca rodou)."""
    from ingestao import eproc_intimacoes

    alvo = {_normalizar_num_processo(n) for n in (numeros_processo or []) if n}
    if not alvo:
        return []

    dados = eproc_intimacoes.carregar()
    itens: list[dict] = []
    for categoria, casos in (dados.get("secoes") or {}).items():
        for caso in casos:
            num = _normalizar_num_processo(caso.get("processo", ""))
            if num not in alvo:
                continue
            for idx, p in enumerate(caso.get("prazos") or []):
                final_real = p.get("final_prazo") or ""
                itens.append({
                    "id": f"{num}:{categoria}:{idx}",
                    "processo": caso.get("processo", ""),
                    "categoria": categoria,
                    "evento": p.get("evento", ""),
                    "prazo_dias": p.get("prazo_dias", ""),
                    "data_final": final_real or p.get("final_estimado") or "",
                    "estimado": not bool(final_real),
                    "urgente": bool(caso.get("urgente")),
                })
    return itens


def resolver_prazo_por_id(prazo_id: str) -> dict | None:
    """Devolve o registro CRU do prazo (com `data_envio`, usado pra casar contra
    a triagem MCP — ver `peticao_ia_service`) + o `caso` (processo/assunto/
    partes) a que pertence. `prazo_id` é o formato de `listar_prazos`
    (`"<numero_sem_pontuacao>:<categoria>:<indice>"`). None se não achar (cache
    desatualizado desde a seleção do prazo na tela, p.ex.)."""
    from ingestao import eproc_intimacoes

    partes = (prazo_id or "").split(":")
    if len(partes) != 3:
        return None
    num_alvo, categoria, idx_str = partes
    try:
        idx = int(idx_str)
    except ValueError:
        return None

    dados = eproc_intimacoes.carregar()
    for caso in (dados.get("secoes") or {}).get(categoria, []):
        if _normalizar_num_processo(caso.get("processo", "")) != num_alvo:
            continue
        prazos = caso.get("prazos") or []
        if 0 <= idx < len(prazos):
            return {"prazo": prazos[idx], "caso": caso}
    return None


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


def montar_resumo_execucao(paj_norm: str) -> dict:
    """Resolve o rascunho salvo do PAJ num resumo pronto pra revisão do usuário
    ANTES de preparar de verdade no e-proc — evento, processo e a lista de
    anexos que serão de fato enviados (já excluindo os ignorados por
    `eventoSemAnexo`), cada um com o caminho em disco resolvido. Erro claro se
    o rascunho não existir, o processo/evento estiver vazio, ou algum anexo não
    for encontrado em disco (arquivo movido/apagado desde que foi adicionado)."""
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

    return {
        "ok": True, "processo": processo, "evento": evento,
        "evento_sem_anexo": sem_anexo, "documentos": documentos,
    }


async def executar_peticionamento(paj_norm: str) -> dict:
    """Prepara de verdade a tela "Movimentar/Peticionar" do e-proc a partir do
    rascunho salvo — evento + anexos (upload, Tipo, Sigilo), terminando em
    "Confirmar seleção de documentos". NÃO clica em "Peticionar" (ver
    `confirmar_peticionamento_real`, chamada só depois de o usuário confirmar
    explicitamente o resumo devolvido aqui)."""
    from ingestao import eproc_client

    resumo = montar_resumo_execucao(paj_norm)
    if not resumo.get("ok"):
        return resumo
    res = await eproc_client.preparar_peticionamento(
        resumo["processo"], resumo["evento"], resumo["documentos"])
    if not res.get("ok"):
        return res
    return {**resumo, **res}


async def confirmar_peticionamento_real(paj_norm: str, processo: str) -> dict:
    """Clica "Peticionar" de fato na aba já preparada por `executar_peticionamento`
    — IRREVERSÍVEL, protocolo real na vara. Em caso de sucesso, descarta o
    rascunho (mesmo efeito de `excluir_rascunho` — não há mais o que retomar
    depois de protocolado)."""
    from ingestao import eproc_client

    res = await eproc_client.confirmar_peticionamento(processo)
    if res.get("ok") and res.get("provavel_sucesso"):
        excluir_rascunho(paj_norm)
    return res
