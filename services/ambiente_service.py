"""Healthcheck de dependencias externas (Tesseract, Chromium).

Rodado no startup de app.py — resultado fica em `app.state.ambiente` pra ser
exibido no dashboard como banner amarelo quando algo falta.

Barato (cache em memoria), nao refaz check durante a vida do processo.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from config import validar_paths

_log = logging.getLogger(__name__)


_cache: dict | None = None


def _tesseract_ok() -> tuple[bool, str]:
    """Retorna (ok, versao_ou_motivo)."""
    exe = shutil.which("tesseract")
    if not exe:
        return (False, "tesseract nao encontrado no PATH")
    # Tenta executar --version sem travar
    try:
        import subprocess

        out = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        linha_1 = (
            (out.stdout or out.stderr or "").splitlines()[0] if (out.stdout or out.stderr) else ""
        )
        return (True, linha_1.strip() or "ok")
    except Exception as e:
        return (False, f"erro ao executar: {type(e).__name__}: {e}")


def _ocrmypdf_ok() -> tuple[bool, str]:
    """Retorna (ok, versao_ou_motivo).

    ocrmypdf e' o OCR de 1a linha (deskew/clean/rotate + Tesseract via
    Ghostscript) — melhora muito o texto de escaneados. E' OPCIONAL: se ausente,
    o painel cai no Tesseract cru pagina-a-pagina (comportamento antigo).
    """
    exe = shutil.which("ocrmypdf")
    if not exe:
        return (False, "ocrmypdf nao encontrado no PATH")
    try:
        import subprocess

        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        linhas = (out.stdout or out.stderr or "").strip().splitlines()
        return (True, (linhas[0].strip() if linhas else "ok"))
    except Exception as e:
        return (False, f"erro ao executar: {type(e).__name__}: {e}")


def _chromium_ok() -> tuple[bool, str]:
    """Retorna (ok, motivo).

    Checa se o Chromium do Playwright foi instalado via
    `python -m playwright install chromium` — procura o diretorio de cache
    padrao de cada SO (Windows: `~/AppData/Local/ms-playwright`;
    macOS: `~/Library/Caches/ms-playwright`; Linux: `~/.cache/ms-playwright`)
    e respeita o override `PLAYWRIGHT_BROWSERS_PATH`.
    """
    candidatos = []
    # Override explicito tem prioridade (Playwright honra PLAYWRIGHT_BROWSERS_PATH).
    env_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        candidatos.append(Path(env_path))
    candidatos += [
        Path.home() / "AppData" / "Local" / "ms-playwright",  # Windows
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path.home() / ".cache" / "ms-playwright",  # Linux
    ]
    for c in candidatos:
        if not c.exists():
            continue
        # Precisa ter pelo menos um sub-diretorio chromium-*
        try:
            for sub in c.iterdir():
                if sub.is_dir() and (
                    sub.name.lower().startswith("chromium_headless_shell")
                    or sub.name.lower().startswith("chromium")
                ):
                    return (True, f"encontrado em {sub}")
        except Exception:
            continue
    # Chromium ausente — tenta instalar automaticamente antes de desistir.
    # Roda no startup (chamada sincrona em app.py), portanto bloqueia ate
    # concluir. Tipicamente leva 30-90s na primeira vez.
    print("[AMBIENTE] Chromium ausente — instalando automaticamente (aguarde)...")
    _log.info("playwright install chromium: iniciando auto-instalacao")
    try:
        env = os.environ.copy()
        # Propaga PLAYWRIGHT_BROWSERS_PATH para o subprocesso (pode ter sido
        # definido via .env para um caminho fora do AppData).
        _pbp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if _pbp:
            env["PLAYWRIGHT_BROWSERS_PATH"] = _pbp
        res = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if res.returncode == 0:
            # Verifica novamente apos instalacao
            for c in candidatos:
                if not c.exists():
                    continue
                try:
                    for sub in c.iterdir():
                        if sub.is_dir() and (
                            sub.name.lower().startswith("chromium_headless_shell")
                            or sub.name.lower().startswith("chromium")
                        ):
                            _log.info("playwright install chromium: OK — %s", sub)
                            print(f"[AMBIENTE] Chromium instalado em {sub}")
                            return (True, f"auto-instalado em {sub}")
                except Exception:
                    continue
            return (False, "playwright install chromium executou mas diretorio nao encontrado")
        else:
            detalhe = ((res.stderr or res.stdout) or "").strip()[-300:]
            _log.warning("playwright install chromium falhou (rc=%d): %s", res.returncode, detalhe)
            return (False, f"auto-instalacao falhou (rc={res.returncode}): {detalhe}")
    except Exception as exc:
        _log.warning("playwright install chromium: excecao: %s: %s", type(exc).__name__, exc)
        return (False, f"auto-instalacao excecao: {type(exc).__name__}: {exc}")


def verificar_ambiente(forcar: bool = False) -> dict:
    """Retorna dict com status das dependencias externas.

    {
        "tesseract_ok": bool,
        "tesseract_info": str,
        "chromium_ok": bool,
        "chromium_info": str,
        "avisos": [str],  # lista pra UI
    }

    Cacheia em memoria — so refaz se `forcar=True`.
    """
    global _cache
    if _cache is not None and not forcar:
        return _cache

    t_ok, t_info = _tesseract_ok()
    o_ok, o_info = _ocrmypdf_ok()
    c_ok, c_info = _chromium_ok()
    erros_path = validar_paths()

    avisos: list[str] = []
    for erro in erros_path:
        avisos.append(f"Workspace ausente — {erro}. Ajuste OFICIO_GERAL no .env.")
    if not t_ok:
        avisos.append(
            f"Tesseract ausente — OCR desligado. {t_info}. "
            "Instale pelo README (UB-Mannheim) e reinicie o painel."
        )
    # ocrmypdf e' OPCIONAL (ha fallback Tesseract) — aviso brando, so' informativo.
    if t_ok and not o_ok:
        avisos.append(
            "ocrmypdf ausente — OCR usando Tesseract cru (qualidade menor em "
            "escaneados). Opcional: instale `ocrmypdf` (brew/pip) p/ melhorar."
        )
    if not c_ok:
        avisos.append(f"Chromium do Playwright ausente — sync SISDPU vai falhar. {c_info}.")

    _cache = {
        "tesseract_ok": t_ok,
        "tesseract_info": t_info,
        "ocrmypdf_ok": o_ok,
        "ocrmypdf_info": o_info,
        "chromium_ok": c_ok,
        "chromium_info": c_info,
        "avisos": avisos,
    }
    return _cache
