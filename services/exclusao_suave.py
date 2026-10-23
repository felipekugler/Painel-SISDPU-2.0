"""Exclusão "suave" de arquivos e pastas (23/10/2026, pedido do Defensor).

Por quê: o FortiEDR (antivírus/EDR corporativo) bloqueou o processo do painel
depois de exclusões em lote — "File Delete blocked" e, em seguida, "File Write
blocked", travando TODA gravação do painel (concluir, rascunhos, sincronização)
até reiniciá-lo. O mesmo já tinha acontecido em 28/08 com a purga de casos da
Triagem (ver routes/prazos.py).

Como, a partir daqui, o painel apaga:
  1. NUNCA pelo processo principal (o servidor, com rede). Os caminhos entram
     numa FILA (`dados/_fila_exclusao.json`, sobrevive a reinícios) e um
     processo AUXILIAR de vida curta (services/_lixeira_worker.py) os manda
     para a LIXEIRA do Windows — se a proteção reagir, só ele é afetado.
  2. Numa única operação do Windows por lote (a mesma do Explorer), nunca
     arquivo a arquivo; lotes pequenos (`_LOTE`) com pausa entre eles.
  3. Pasta que precisa sumir NA HORA (ex.: "Reiniciar download", que recria a
     pasta em seguida) é antes RENOMEADA — uma única operação, que não apaga
     nada — para `dados/_exclusao_pendente/`, e só então entra na fila.

Na Lixeira, o "local original" dos itens renomeados é essa pasta de exclusão
pendente (o nome do item é mantido, com data/hora na frente).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger("exclusao_suave")

_PROJETO = Path(__file__).resolve().parents[1]
DADOS_DIR = _PROJETO.parent / "dados"
PENDENTE_DIR = DADOS_DIR / "_exclusao_pendente"
_FILA = DADOS_DIR / "_fila_exclusao.json"
_WORKER = _PROJETO / "services" / "_lixeira_worker.py"

_LOTE = 15            # caminhos por operação de Lixeira
_PAUSA_S = 4.0        # pausa entre lotes
_OCIOSO_S = 20.0      # espera quando a fila está vazia
_MAX_TENTATIVAS = 6   # depois disso o item sai da fila (fica em disco; registrado no log)

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _raizes() -> list[Path]:
    return [Path(config.OFICIO_GERAL).resolve(), DADOS_DIR.resolve(), (_PROJETO / "logs").resolve()]


def _permitido(caminho: Path) -> bool:
    """Só apaga dentro das pastas do painel — e nunca a própria raiz."""
    try:
        alvo = caminho.resolve()
    except Exception:  # noqa: BLE001
        return False
    for raiz in _raizes():
        if alvo != raiz and raiz in alvo.parents:
            return True
    return False


def _ler_fila() -> list[dict]:
    try:
        dados = json.loads(_FILA.read_text(encoding="utf-8"))
        return dados if isinstance(dados, list) else []
    except Exception:  # noqa: BLE001
        return []


def _gravar_fila(fila: list[dict]) -> None:
    try:
        _FILA.parent.mkdir(parents=True, exist_ok=True)
        _FILA.write_text(json.dumps(fila, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("não gravou a fila de exclusão: %s", e)


def _enfileirar(caminhos: list[Path], origem: str) -> int:
    if not caminhos:
        return 0
    agora = datetime.now().isoformat(timespec="seconds")
    with _lock:
        fila = _ler_fila()
        ja = {it.get("caminho") for it in fila}
        novos = [{"caminho": str(c), "origem": origem, "em": agora, "tentativas": 0}
                 for c in caminhos if str(c) not in ja]
        fila.extend(novos)
        _gravar_fila(fila)
    _garantir_thread()
    return len(novos)


def apagar_pasta(pasta, origem: str = "") -> bool:
    """Tira a pasta do lugar NA HORA (renomeia para a área de exclusão
    pendente — uma única operação) e a põe na fila da Lixeira. True se a pasta
    não está mais no lugar (ou já não existia)."""
    p = Path(pasta)
    if not p.exists():
        return True
    if not _permitido(p):
        log.warning("exclusão recusada (fora das pastas do painel): %s", p)
        return False
    alvo = p
    try:
        PENDENTE_DIR.mkdir(parents=True, exist_ok=True)
        destino = PENDENTE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{p.name}"
        os.rename(p, destino)
        alvo = destino
    except OSError as e:
        # Sem renomear (outro volume, arquivo aberto…): fica no lugar e a fila
        # tenta mandá-la para a Lixeira assim mesmo.
        log.info("não renomeou %s para a exclusão pendente (%s) — vai para a fila no lugar", p, e)
    _enfileirar([alvo], origem)
    return not p.exists()


def apagar_arquivos(arquivos, origem: str = "") -> int:
    """Põe arquivos na fila da Lixeira (saem do lugar em instantes, aos
    poucos). Devolve quantos entraram na fila."""
    ok = [Path(a) for a in arquivos if Path(a).exists() and _permitido(Path(a))]
    return _enfileirar(ok, origem)


def pendentes() -> int:
    return len(_ler_fila())


# ---------------------------------------------------------------------------
# Laço de processamento (thread do painel → processo auxiliar)
# ---------------------------------------------------------------------------

def _processar_lote() -> bool:
    """Um lote. True se havia algo a fazer."""
    with _lock:
        fila = _ler_fila()
    if not fila:
        return False
    lote = fila[:_LOTE]
    caminhos = [it["caminho"] for it in lote]
    restantes: set[str] = set(c for c in caminhos if Path(c).exists())
    if restantes:
        try:
            r = subprocess.run([sys.executable, str(_WORKER)],
                               input=json.dumps(sorted(restantes), ensure_ascii=False).encode("utf-8"),
                               capture_output=True, cwd=str(_PROJETO), timeout=300)
            saida = json.loads((r.stdout or b"{}").decode("utf-8", "replace") or "{}")
            if saida.get("codigo"):
                log.info("Lixeira devolveu código %s para um lote de %d", saida.get("codigo"), len(restantes))
        except Exception as e:  # noqa: BLE001
            log.warning("processo da Lixeira falhou: %s", e)
        restantes = set(c for c in caminhos if Path(c).exists())
    with _lock:
        fila = _ler_fila()
        nova = []
        for it in fila:
            c = it.get("caminho")
            if c not in caminhos:
                nova.append(it)
                continue
            if c not in restantes:
                continue  # foi para a Lixeira
            it["tentativas"] = int(it.get("tentativas", 0)) + 1
            if it["tentativas"] >= _MAX_TENTATIVAS:
                log.warning("desisti de mandar para a Lixeira após %d tentativas: %s", it["tentativas"], c)
                continue
            nova.append(it)
        # itens que falharam vão para o fim da fila (não travam os demais)
        nova.sort(key=lambda it: it.get("tentativas", 0))
        _gravar_fila(nova)
    return True


def _laco() -> None:
    while True:
        try:
            trabalhou = _processar_lote()
        except Exception as e:  # noqa: BLE001
            log.warning("laço de exclusão: %s", e)
            trabalhou = False
        time.sleep(_PAUSA_S if trabalhou else _OCIOSO_S)


def _garantir_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_laco, name="exclusao-suave", daemon=True)
        _thread.start()


def iniciar() -> None:
    """Na partida do painel: retoma a fila e recolhe o que ficou na área de
    exclusão pendente sem estar na fila (ex.: painel fechado no meio)."""
    sobras = []
    if PENDENTE_DIR.is_dir():
        na_fila = {it.get("caminho") for it in _ler_fila()}
        sobras = [p for p in PENDENTE_DIR.iterdir() if str(p) not in na_fila]
    if sobras:
        _enfileirar(sobras, "retomada")
    elif _ler_fila():
        _garantir_thread()
