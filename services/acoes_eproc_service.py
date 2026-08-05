"""Módulo genérico "Ações no Eproc" (peticionamento no processo judicial vinculado a
um PAJ) — EM CONSTRUÇÃO. Reutilizável em qualquer página do painel, assim como
`acoes_sisdpu_service.py`.

Por ora só a listagem de prazos abertos/pendentes está implementada, a partir do
cache local já sincronizado pela aba "Prazos e-proc" (`ingestao/eproc_intimacoes.py`)
— não navega no e-proc, é instantânea. O restante (evento a lançar, petição, anexos,
execução real do protocolo) depende de inspecionar a tela de peticionamento do e-proc
ao vivo, e será implementado numa fase seguinte.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import config

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
