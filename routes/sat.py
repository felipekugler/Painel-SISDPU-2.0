"""Rotas da feature "Arquivos SAT".

- GET  /sat                         — página (tabela de pendências + histórico).
- GET  /api/sat/pendentes           — PAJs com o trâmite de juntada do SAT na caixa.
- GET  /api/sat/{paj_norm}/processar — SSE: movimenta → tramita → conclui (query
                                       `grupo`, `dry`).
- GET  /api/sat/historico           — registros dos PAJs já trabalhados.
- GET  /api/sat/grupos              — caixas de grupo da unidade + favoritas.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from ingestao import sisdpu_movtram as movtram
from services import historico_sat, sat_service

with __import__("contextlib").suppress(Exception):
    from services import config_painel_service as _cfg  # preferências (favoritas/prazo)

router = APIRouter()


@router.get("/sat", response_class=HTMLResponse)
async def pagina_sat(request: Request):
    template = request.app.state.jinja.get_template("sat.html")
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}))
    return HTMLResponse(html)


@router.get("/api/sat/pendentes", response_class=JSONResponse)
async def api_pendentes():
    return JSONResponse(await sat_service.listar_pendentes())


@router.get("/api/sat/abrir", response_class=JSONResponse)
async def api_sat_abrir():
    """Abre a janela do SAT na tela para o login (usuário/senha/2FA/CAPTCHA)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.abrir())


@router.get("/api/sat/sessao", response_class=JSONResponse)
async def api_sat_sessao():
    """Verifica se a sessão do SAT está pronta (logado, na página de consulta)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.status())


@router.get("/api/sat/sessao-leve", response_class=JSONResponse)
async def api_sat_sessao_leve():
    """Status LEVE p/ polling automático: só lê a URL (não navega, não abre a janela)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.peek())


@router.get("/api/sat/historico", response_class=JSONResponse)
async def api_historico():
    regs = historico_sat.listar()
    # enriquece com a URL do detalhamento do PAJ no SISDPU (link do nº do PAJ),
    # construída a partir do id_processo — cobre registros antigos e novos.
    for r in regs:
        if not r.get("url_detalhe") and r.get("id_processo"):
            r["url_detalhe"] = (f"{movtram.SISDPU_URL}/pages/atendimento/"
                                f"detalhamentoProcesso.xhtml?id={r['id_processo']}")
    return JSONResponse({"registros": regs})


async def _stream_processar(paj_norm: str, grupo: str, dry: bool, prazo: int):
    async for ev in sat_service.processar_paj_stream(
        paj_norm, grupo=grupo or None, prazo_dias=prazo, dry_run=dry
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


async def _stream_baixar(paj_norm: str, cpf: str, nome: str, delay: float, bloqueio: float):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm))
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/arquivos", response_class=JSONResponse)
async def api_arquivos(paj_norm: str):
    arqs = sat_service.listar_todos_arquivos_sat(paj_norm)
    return JSONResponse({"arquivos": [
        {"nome": a["nome"], "tamanho": a.get("tamanho", 0), "excluido": a.get("excluido", False)}
        for a in arqs]})


@router.get("/api/sat/{paj_norm}/arquivo")
async def abrir_arquivo(paj_norm: str, nome: str):
    """Serve um PDF do SAT (ativo ou na lixeira 'Excluídos') INLINE, para abrir em nova
    aba do navegador. `nome` é o nome do arquivo (basename; sem travessia de caminho)."""
    base = Path(nome).name  # anti path-traversal
    for pasta in (sat_service.pasta_arquivos_sat(paj_norm),
                  sat_service.pasta_excluidos(paj_norm)):
        p = pasta / base
        if p.exists() and p.is_file():
            return FileResponse(
                str(p), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{base}"'})
    return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)


@router.post("/api/sat/{paj_norm}/excluir-arquivo", response_class=JSONResponse)
async def api_excluir_arquivo(paj_norm: str, request: Request):
    """Move o documento para 'Excluídos' (soft delete)."""
    body = await request.json()
    return JSONResponse(sat_service.excluir_arquivo_sat(paj_norm, body.get("nome", "")))


@router.post("/api/sat/{paj_norm}/recuperar-arquivo", response_class=JSONResponse)
async def api_recuperar_arquivo(paj_norm: str, request: Request):
    """Traz o documento de volta de 'Excluídos' para a lista de anexação."""
    body = await request.json()
    return JSONResponse(sat_service.recuperar_arquivo_sat(paj_norm, body.get("nome", "")))


@router.get("/api/sat/{paj_norm}/narrativa", response_class=JSONResponse)
async def api_narrativa(paj_norm: str):
    """Narrativa do PAJ (texto) para a linha expansível da coluna Pretensão."""
    return JSONResponse(sat_service.narrativa_paj(paj_norm))


@router.get("/api/sat/{paj_norm}/log-download", response_class=JSONResponse)
async def log_download(paj_norm: str):
    """Log da última rotina de download (Baixar arquivos do SAT Central) do PAJ."""
    p = sat_service.pasta_arquivos_sat(paj_norm) / "_log_download.json"
    if not p.exists():
        return JSONResponse({"existe": False, "eventos": []})
    try:
        return JSONResponse({"existe": True, **json.loads(p.read_text(encoding="utf-8"))})
    except Exception:  # noqa: BLE001
        return JSONResponse({"existe": False, "eventos": []})


@router.get("/api/sat/{paj_norm}/baixar")
async def baixar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                 bloqueio: float = 45.0):
    """SSE do DOWNLOAD dos documentos do SAT (consulta + Grupos 1 e 2 + PAT). O usuário
    resolve o CAPTCHA na janela do SAT. Salva na pasta do PAJ. `delay` = tempo (s) entre
    consultas (padrão 5s); `bloqueio` = espera (s) ao detectar antirobô (padrão 45s)."""
    return EventSourceResponse(_stream_baixar(paj_norm, cpf, nome, delay, bloqueio))


@router.get("/api/sat/{paj_norm}/processar")
async def processar(paj_norm: str, grupo: str = "", dry: int = 0, prazo: int = 0):
    """SSE do processamento (mov→tram→concluir). `dry=1` => simulação; `prazo` = dias
    de prazo de conclusão da tramitação (0 = sem prazo)."""
    return EventSourceResponse(_stream_processar(paj_norm, grupo, bool(dry), int(prazo)))


@router.get("/api/sat/grupos", response_class=JSONResponse)
async def api_grupos(id: str = ""):
    """Lista caixas de grupo da unidade (+ favoritas de config). `id` = id_processo de
    referência p/ abrir a tela de tramitação; se ausente, usa o de uma pendência."""
    id_processo = id
    if not id_processo:
        pend = await sat_service.listar_pendentes()
        itens = pend.get("itens", []) if isinstance(pend, dict) else []
        id_processo = next((i.get("id_processo") for i in itens if i.get("id_processo")), "")
    grupos = await movtram.listar_grupos_unidade(id_processo) if id_processo else []
    favoritos: list[str] = []
    try:
        favoritos = _cfg.get_grupos_favoritos()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        favoritos = []
    return JSONResponse({"grupos": grupos, "favoritos": favoritos, "padrao": sat_service.GRUPO_PADRAO})
