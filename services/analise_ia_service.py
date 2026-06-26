"""Análise IA de PAJ: lê arquivos sincronizados e gera relatório via Claude CLI."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from pathlib import Path

from config import ELABORACAO_EFFORT, ELABORACAO_MODELO, OFICIO_GERAL, PAJS_DIR
from services.chat_service import _resolver_claude_cmd


# ─── Estado em memória ───────────────────────────────────────────────────────

_sessoes: dict[str, dict] = {}


# ─── Disco ───────────────────────────────────────────────────────────────────

def ler_analise_disco(paj_norm: str) -> str | None:
    caminho = PAJS_DIR / paj_norm / "analise_ia.md"
    if caminho.exists():
        return caminho.read_text(encoding="utf-8")
    return None


def ler_meta_analise(paj_norm: str) -> dict:
    """Lê analise_ia_meta.json; retorna {} se não existir."""
    caminho = PAJS_DIR / paj_norm / "analise_ia_meta.json"
    if caminho.exists():
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ─── Status ──────────────────────────────────────────────────────────────────

def get_status(paj_norm: str) -> dict:
    sess = _sessoes.get(paj_norm)
    analise = ler_analise_disco(paj_norm)
    meta = ler_meta_analise(paj_norm)

    if sess and sess["status"] in ("running", "error"):
        return {
            "status": sess["status"],
            "last_action": sess.get("last_action", ""),
            "error": sess.get("error", ""),
            "analise": analise or "",
            "data_analise": meta.get("data", ""),
            "modelo_analise": meta.get("modelo", ""),
        }

    if analise:
        return {
            "status": "done",
            "last_action": "",
            "error": "",
            "analise": analise,
            "data_analise": meta.get("data", ""),
            "modelo_analise": meta.get("modelo", ""),
        }

    return {"status": "idle", "last_action": "", "error": "", "analise": "", "data_analise": "", "modelo_analise": ""}


# ─── Iniciar ─────────────────────────────────────────────────────────────────

def iniciar_analise(paj_norm: str) -> dict:
    sess = _sessoes.get(paj_norm)
    if sess and sess["status"] == "running":
        return {"ok": False, "msg": "Análise já em andamento."}

    _sessoes[paj_norm] = {"status": "running", "last_action": "Iniciando...", "error": ""}
    t = threading.Thread(target=_run, args=(paj_norm,), daemon=True)
    t.start()
    return {"ok": True, "msg": "Análise iniciada."}


# ─── Montagem do contexto ────────────────────────────────────────────────────

def _paj_norm_to_numero(paj_norm: str) -> str:
    """PAJ-2026-029-01789 → 2026/029-01789"""
    raw = paj_norm.removeprefix("PAJ-")
    parts = raw.split("-")
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}-{parts[2]}"
    return raw


def _build_prompt(paj_norm: str, pasta: Path) -> str:
    # sisdpu.txt — narrativa + movimentações
    sisdpu_path = pasta / "sisdpu.txt"
    sisdpu = (
        sisdpu_path.read_text(encoding="utf-8", errors="replace")
        if sisdpu_path.exists()
        else "(arquivo sisdpu.txt não encontrado — sincronize o PAJ primeiro)"
    )

    # metadata.json — campos estruturados
    meta: dict = {}
    meta_path = pasta / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    nome = meta.get("assistido_caixa") or ""
    pretensao = meta.get("pretensao") or meta.get("classificacao") or ""
    oficio = meta.get("oficio_caixa") or ""
    status_paj = (meta.get("detalhes_sisdpu") or {}).get("status_paj") or "Ativo"
    paj_num = _paj_norm_to_numero(paj_norm)

    # pecas/*.txt — documentos baixados com OCR
    pecas_dir = pasta / "pecas"
    pecas_content = ""
    if pecas_dir.exists():
        for txt in sorted(pecas_dir.glob("*.txt")):
            try:
                content = txt.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    trecho = content[:30_000]
                    if len(content) > 30_000:
                        trecho += f"\n\n[... documento truncado — {len(content)} chars total ...]"
                    pecas_content += f"\n\n---\n### Documento: {txt.name}\n\n{trecho}"
            except Exception:
                pass

    prompt = (
        f"Você é um assessor jurídico da Defensoria Pública Federal.\n"
        f"Analise integralmente o PAJ {paj_num} — {nome}.\n\n"
        f"## Metadados\n"
        f"- Assistido(a): {nome}\n"
        f"- Pretensão: {pretensao}\n"
        f"- Ofício: {oficio}\n"
        f"- Status: {status_paj}\n\n"
        f"## Conteúdo SISDPU (narrativa e movimentações)\n\n"
        f"{sisdpu}\n"
    )

    if pecas_content:
        prompt += f"\n## Documentos baixados (OCR){pecas_content}\n"

    prompt += (
        "\n---\n\n"
        f"Produza uma análise jurídica completa do PAJ {paj_num} com as seguintes seções:\n\n"
        f"## PAJ {paj_num} — {nome}\n\n"
        "### Situação atual\n"
        "Qual é o caso, qual o pedido, em que fase está, o que precisa ser feito agora.\n\n"
        "### Narrativa e contexto fático\n"
        "Resumo objetivo do quadro clínico/fático, situação do assistido, o que ele pede e por quê. "
        "Use dados concretos dos documentos.\n\n"
        "### Histórico das movimentações\n"
        "Em linguagem natural — sem listar datas nem números de movimentação. "
        "O que foi feito desde a abertura, o que aconteceu, situação atual.\n\n"
        "### Retornos e atendimentos do assistido\n"
        "O que o assistido relatou em cada atendimento de retorno: fatos novos, queixas, documentos.\n\n"
        "### Análises e manifestações anteriores\n"
        "Resumo de análises de estagiários, despachos e manifestações de Defensores Públicos. "
        "Destaque as conclusões e recomendações já registradas.\n\n"
        "### Análise jurídica\n"
        "Avaliação da pretensão: cabimento, requisitos legais, pontos favoráveis e desfavoráveis, "
        "tese recomendada. Seja juridicamente preciso.\n\n"
        "### Próximos passos recomendados\n"
        "O que deve ser feito agora: petição inicial, despacho, diligência, mais documentos, "
        "arquivamento — com justificativa.\n\n"
        "### Elementos para petição inicial (se cabível)\n"
        "- Espécie do benefício e NB (se houver indeferimento administrativo)\n"
        "- Quadro clínico resumido\n"
        "- Critérios específicos (qualidade de segurado / critério econômico / carência / incapacidade)\n"
        "- Documentos disponíveis para instrução\n\n"
        "Seja objetivo e juridicamente preciso. Não faça perguntas. "
        "Use os dados concretos dos documentos e do SISDPU.\n"
    )

    return prompt


# ─── Executor (thread) ───────────────────────────────────────────────────────

def _run(paj_norm: str) -> None:
    import os
    import sys
    import shutil

    pasta = PAJS_DIR / paj_norm
    try:
        if not pasta.exists():
            _sessoes[paj_norm].update(status="error", error=f"Pasta não encontrada: {pasta}")
            return

        _sessoes[paj_norm]["last_action"] = "Lendo arquivos do PAJ..."
        prompt = _build_prompt(paj_norm, pasta)

        _sessoes[paj_norm]["last_action"] = "Analisando com Claude..."

        # Resolve o caminho do Claude CLI com fallback robusto para Windows
        cmd_claude = _resolver_claude_cmd()

        # Se o fallback retornar apenas ["claude"], tenta resolver localmente
        if cmd_claude == ["claude"]:
            resolved = shutil.which("claude")
            if not resolved and sys.platform == "win32":
                # Tenta caminhos conhecidos de instalação do Claude no Windows
                appdata = os.environ.get("APPDATA", "")
                localapp = os.environ.get("LOCALAPPDATA", "")
                candidates = []

                if localapp:
                    candidates.append(Path(localapp) / "AnthropicClaude" / "claude.exe")
                if appdata:
                    candidates.append(Path(appdata) / "npm" / "claude.cmd")

                for cand in candidates:
                    if cand.exists():
                        resolved = str(cand)
                        break

            # Se encontrou, monta o comando corretamente
            if resolved:
                if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
                    cmd_claude = ["cmd.exe", "/c", resolved]
                else:
                    cmd_claude = [resolved]

        cmd = [
            *cmd_claude,
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
        ]
        modelo = ELABORACAO_MODELO
        effort = ELABORACAO_EFFORT
        if modelo:
            cmd += ["--model", modelo]
        if effort:
            cmd += ["--effort", effort]

        msg_json = (
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": prompt}},
                ensure_ascii=False,
            )
            + "\n"
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(OFICIO_GERAL),
        )

        # communicate() fecha stdin após enviar e lê stdout+stderr completos —
        # mais robusto no Windows com cmd.exe /c (evita race condition de BrokenPipe).
        # Timeout generoso: análise juridica pode demorar vários minutos.
        try:
            stdout_bytes, stderr_bytes = proc.communicate(
                input=msg_json.encode("utf-8"), timeout=1800
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            _sessoes[paj_norm].update(
                status="error", error="Timeout: Claude não respondeu em 30 min.", last_action="erro"
            )
            return

        stdout_txt = stdout_bytes.decode("utf-8", errors="replace")
        stderr_txt = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not stdout_txt.strip():
            _sessoes[paj_norm].update(
                status="error",
                error=f"Claude CLI saiu com código {proc.returncode}. Stderr: {stderr_txt[:400] or '(vazio)'}",
                last_action="erro",
            )
            return

        # Parseia eventos stream-json acumulados no stdout
        accumulated = ""
        final_text = ""

        for raw in stdout_txt.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
                etype = evt.get("type", "")

                if etype == "stream_event":
                    inner = evt.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            accumulated += delta.get("text", "")

                elif etype == "result":
                    result_field = evt.get("result", "")
                    if isinstance(result_field, dict):
                        result_field = result_field.get("text", "")
                    final_text = accumulated.strip() or (
                        result_field if isinstance(result_field, str) else ""
                    )
                    subtype = str(evt.get("subtype") or "")
                    falhou = bool(evt.get("is_error")) or subtype.startswith("error")
                    if falhou:
                        _sessoes[paj_norm].update(
                            status="error",
                            error=(final_text[:400] or f"Claude terminou com erro ({subtype})."),
                            last_action="erro",
                        )
                        return
                    break

            except json.JSONDecodeError:
                pass

        if not final_text:
            _sessoes[paj_norm].update(
                status="error",
                error=f"Claude não retornou resposta. Stdout: {stdout_txt[:200] or '(vazio)'} | Stderr: {stderr_txt[:200] or '(vazio)'}",
                last_action="erro",
            )
            return

        import datetime as _dt
        (pasta / "analise_ia.md").write_text(final_text, encoding="utf-8")
        (pasta / "analise_ia_meta.json").write_text(
            json.dumps({
                "data": _dt.datetime.now().isoformat(timespec="minutes"),
                "modelo": ELABORACAO_MODELO or "claude",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        _sessoes[paj_norm].update(status="done", last_action="", error="")

    except FileNotFoundError as e:
        # Captura o cmd_claude resolvido neste escopo para exibir na mensagem.
        # Se _run() falhou antes de definir cmd_claude, usa _resolver_claude_cmd() novamente.
        try:
            _cmd_diag = cmd_claude  # type: ignore[name-defined]
        except NameError:
            _cmd_diag = _resolver_claude_cmd()
        _sessoes[paj_norm].update(
            status="error",
            error=(
                f"Claude CLI não encontrado — {type(e).__name__}: {e} "
                f"(cmd: {' '.join(_cmd_diag)}). "
                "Verifique se o claude está instalado e acessível."
            ),
            last_action="erro",
        )
    except Exception as e:
        _sessoes[paj_norm].update(status="error", error=f"{type(e).__name__}: {e}", last_action="erro")
