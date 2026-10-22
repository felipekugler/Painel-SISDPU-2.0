"""Gravação de arquivos de texto (metadata.json, sisdpu.txt) protegida contra
o bloqueio do antivírus no processo do servidor.

Worker de vida curta e SEM rede usado como rota de fuga abaixo — mesmo
workaround provisório do FortiClient (ver services.intimacao_service.
_gravar_via_subprocesso, 2026-08-28): o antivírus bloqueia o PROCESSO DO
SERVIDOR (app.py, porta 8001 aberta) de gravar/substituir arquivo, mas um
processo Python de vida curta sem socket de rede grava livremente na mesma
pasta. Confirmado ao vivo 2026-10-05: sync avulso de PAJ-2025-029-01045
(disparado por abrirPajPainel() ao clicar no número do PAJ na tabela de
Intimações de Prazos) estourou `PermissionError` gravando sisdpu.txt mesmo
com as retentativas abaixo já esgotadas — esse caminho nunca tinha sido
migrado pro worker (só intimacao_service/intimacao_ia_service tinham sido).

Antes em ingestao/sincronizador.py; movido para cá (22/10/2026) para que
paj_service e foto_assistido_service, que também gravam metadata.json, usem
a mesma proteção sem importar funções privadas do sincronizador."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

_PROJETO_DIR = Path(__file__).resolve().parents[1]
_ARQUIVO_WORKER = _PROJETO_DIR / "services" / "_arquivo_worker.py"


def gravar_via_subprocesso(destino: Path, texto: str) -> bool:
    """Devolve True em sucesso; False em qualquer falha (worker ausente, erro
    de execução, timeout) — o chamador cai pro método antigo (escrita direta
    + retentativas), então uma falha aqui nunca impede a gravação, só a torna
    mais lenta."""
    if not _ARQUIVO_WORKER.exists():
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(_ARQUIVO_WORKER), str(destino)],
            input=texto.encode("utf-8"), capture_output=True,
            cwd=str(_PROJETO_DIR), timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


async def escrever_texto_com_retry(
    caminho: Path, texto: str, tentativas: int = 4, espera_inicial: float = 0.3
) -> None:
    """Grava texto num arquivo com retry/backoff — protege contra bloqueio
    TRANSITORIO do arquivo no Windows (antivirus escaneando um arquivo/pasta
    recem-criado, indexador, cliente de backup/nuvem), que pode disparar
    `PermissionError: [Errno 13]` mesmo sem NENHUMA outra execucao do painel
    disputando o arquivo — confirmado em producao: sync de um PAJ novo (pasta
    recem-criada por `pasta.mkdir()` linhas antes) falhou gravando sisdpu.txt
    com o lock global (`_sync_lock`) livre, ou seja, sem concorrencia real
    entre sincronizacoes — sinal de bloqueio externo passageiro, nao de uma
    segunda escrita simultanea.

    1ª tentativa é sempre via `gravar_via_subprocesso` (worker sem rede, ver
    acima) — escapa do bloqueio PERSISTENTE do FortiClient no processo do
    servidor, que as retentativas abaixo (pensadas pra bloqueio transitorio)
    nunca resolviam sozinhas. Só cai pra escrita direta+retry se o worker
    falhar por qualquer outro motivo.

    `await asyncio.sleep` (nao `time.sleep`) entre tentativas — isso roda
    dentro do event loop do FastAPI/uvicorn, um sleep sincrono travaria TODAS
    as requisicoes do painel durante a espera."""
    if await asyncio.to_thread(gravar_via_subprocesso, caminho, texto):
        return

    espera = espera_inicial
    for tentativa in range(1, tentativas + 1):
        try:
            caminho.write_text(texto, encoding="utf-8")
            return
        except PermissionError:
            if tentativa == tentativas:
                raise
            await asyncio.sleep(espera)
            espera *= 2


def escrever_texto_com_retry_sync(
    caminho: Path, texto: str, tentativas: int = 4, espera_inicial: float = 0.3
) -> None:
    """Versão síncrona de `escrever_texto_com_retry`, para as gravações de
    metadata.json feitas fora do event loop (funções síncronas). Mesmo caminho:
    worker sem rede primeiro, depois escrita direta com retentativas. Antes, o
    metadata.json era gravado direto e o bloqueio do antivírus no processo do
    servidor derrubava a atualização ao vivo (PAJ 2026/029-04239, 22/10/2026:
    `[Errno 13] Permission denied: ...metadata.json`).

    Aqui a escrita DIRETA vem primeiro e o worker só entra após o 1º
    `PermissionError`: `_atualizar_linha_caixa` (sincronizador) e as passadas
    de foto_assistido_service rodam em laço sobre a caixa inteira, e um
    subprocesso por PAJ travaria o event loop."""
    espera = espera_inicial
    for tentativa in range(1, tentativas + 1):
        try:
            caminho.write_text(texto, encoding="utf-8")
            return
        except PermissionError:
            if tentativa == 1 and gravar_via_subprocesso(caminho, texto):
                return
            if tentativa == tentativas:
                raise
            time.sleep(espera)
            espera *= 2
