"""Executa a skill "SISDPU-OrganizarArquivos" sobre a pasta de um PAJ do painel.

Na prática, a skill é um script Python determinístico
(`organizar_ajuizamento.py`) que faz OCR/classificação/renomeação de
documentos para uma subpasta `Ajuizamento/`. Por ser determinístico (sem
julgamento que dependa de um modelo de linguagem), chamamos o script DIRETO
via subprocess em vez de pedir ao Claude Code CLI para invocar a skill — mais
rápido e confiável.

IMPORTANTE — layout de pastas: o script varre só a RAIZ da pasta recebida
como argumento (`os.listdir`, sem entrar em subpastas — confirmado lendo o
script). O layout original da skill (fluxo PREV4/teses-dpu) é um PAJ com
todos os documentos soltos na raiz; o PAINEL SISDPU guarda os documentos
baixados do SISDPU em `Arquivos/` (subpasta), não na raiz do PAJ. Por isso
passamos `Arquivos/` como argumento (é lá que os documentos de origem estão)
e, ao final, MOVEMOS a `Ajuizamento/` resultante (criada dentro de
`Arquivos/Ajuizamento/`) para a raiz do PAJ — mesma estrutura observada num
caso já organizado manualmente no painel (`Ajuizamento` como pasta-irmã de
`Arquivos`).

Persistência: `organizar_ajuizamento.json` na pasta do PAJ.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

from config import PAJS_DIR
from services import historico

# Script permanente da skill — vive no perfil GLOBAL do usuário (mesmo padrão
# das skills de diagnóstico, ver services/analise_pretensao_service.py).
SCRIPT_PATH = (
    Path.home() / ".claude" / "skills" / "SISDPU-OrganizarArquivos" / "organizar_ajuizamento.py"
)

_STATE_FILE = "organizar_ajuizamento.json"
_sessions: dict[str, dict] = {}  # paj_norm -> {status, log, error}


def _sem_acento(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).lower()


def tem_peticao_inicial(paj_norm: str) -> bool:
    """True se houver algum arquivo com 'peticao'/'petição' no nome — na
    subpasta `Peças IA/` (destino padrão da análise de pretensão, ver
    services/analise_pretensao_service.py) OU na raiz do PAJ (compatibilidade
    com petições geradas antes dessa subpasta existir). Usado para habilitar o
    botão 'Organizar Documentos para Ajuizamento'."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return False
    pastas_a_checar = [pasta, pasta / "Peças IA"]
    for p in pastas_a_checar:
        if not p.exists() or not p.is_dir():
            continue
        for f in p.iterdir():
            if f.is_file() and "peticao" in _sem_acento(f.stem):
                return True
    return False


def pasta_ajuizamento_existe(paj_norm: str) -> bool:
    return (PAJS_DIR / paj_norm / "Ajuizamento").exists()


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
    sess = _sessions.get(paj_norm)
    if sess and sess.get("status") == "running":
        return sess
    disco = ler_disco(paj_norm)
    if disco:
        return disco
    return {"status": "idle", "log": "", "error": ""}


def iniciar(paj_norm: str, forcar: bool = False) -> dict:
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    if not SCRIPT_PATH.exists():
        return {"ok": False, "erro": f"Script da skill não encontrado em {SCRIPT_PATH}."}

    pasta_arquivos = pasta / "Arquivos"
    if not pasta_arquivos.exists() or not any(pasta_arquivos.iterdir()):
        return {
            "ok": False,
            "erro": "Pasta 'Arquivos' (documentos baixados do SISDPU) vazia ou "
            "inexistente para este PAJ — sincronize com anexos antes de organizar.",
        }

    existing = _sessions.get(paj_norm)
    if existing and existing.get("status") == "running":
        return {"ok": False, "erro": "Já há uma organização em andamento para este PAJ."}

    pasta_ajuiz_destino = pasta / "Ajuizamento"
    if pasta_ajuiz_destino.exists() and not forcar:
        return {
            "ok": False,
            "erro": "A pasta Ajuizamento já existe para este PAJ.",
            "precisa_confirmar": True,
        }

    _sessions[paj_norm] = {"status": "running", "log": "", "error": ""}

    def _run() -> None:
        try:
            # Roda sobre Arquivos/ (onde os documentos de origem realmente
            # estão — ver nota no topo do módulo), não sobre a raiz do PAJ.
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(pasta_arquivos)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            log = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            ok = proc.returncode == 0

            # O script cria Arquivos/Ajuizamento/ — move pra raiz do PAJ
            # (pasta-irmã de Arquivos), layout esperado pelo painel.
            pasta_ajuiz_gerada = pasta_arquivos / "Ajuizamento"
            if ok and pasta_ajuiz_gerada.exists():
                if pasta_ajuiz_destino.exists():
                    shutil.rmtree(pasta_ajuiz_destino, ignore_errors=True)
                shutil.move(str(pasta_ajuiz_gerada), str(pasta_ajuiz_destino))
            elif ok:
                ok = False
                log += "\n[!] Script terminou sem erro mas não criou a pasta Ajuizamento esperada."

            _sessions[paj_norm] = {
                "status": "done" if ok else "error",
                "log": log,
                "error": "" if ok else (
                    f"Script terminou com código {proc.returncode}."
                    if proc.returncode != 0 else "Ver log — pasta Ajuizamento não foi gerada."
                ),
            }
        except subprocess.TimeoutExpired:
            _sessions[paj_norm] = {
                "status": "error",
                "log": "",
                "error": "Timeout (15 min) organizando os documentos.",
            }
        except Exception as e:  # noqa: BLE001
            _sessions[paj_norm] = {"status": "error", "log": "", "error": f"{type(e).__name__}: {e}"}

        import datetime as _dt

        data = {
            **_sessions[paj_norm],
            "atualizado_em": _dt.datetime.now().isoformat(timespec="minutes"),
        }
        with contextlib.suppress(Exception):
            _arquivo_estado(paj_norm).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        with contextlib.suppress(Exception):
            historico.registrar(
                paj_norm, "organizar_ajuizamento", status=_sessions[paj_norm]["status"]
            )

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True}
