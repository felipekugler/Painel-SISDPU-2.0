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


async def _stream_baixar(paj_norm: str, cpf: str, nome: str, delay: float,
                         bloqueio: float, pretensao: str):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm))
    # resolve os tipos de documentos a baixar pela PRETENSÃO do PAJ (config por pretensão);
    # sem config → todos os tipos (comportamento padrão).
    tipos = None
    try:
        tipos = _cfg.resolver_sat_tipos(pretensao, sat_client.TIPOS_KEYS)  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        tipos = None
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao=pretensao):
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


@router.get("/api/sat/{paj_norm}/inventario", response_class=JSONResponse)
async def api_inventario(paj_norm: str):
    """Inventário dos documentos revelados no SAT (1º download) + marcas ja_baixado/
    ja_anexado + CPF — para o modal "Baixar mais". Inclui o catálogo p/ rótulos/grupos."""
    from ingestao import sat_client
    inv = sat_service.inventario_paj(paj_norm)
    inv["tipos"] = sat_client.TIPOS_DOCUMENTOS
    return JSONResponse(inv)


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
                 bloqueio: float = 45.0, pretensao: str = ""):
    """SSE do DOWNLOAD dos documentos do SAT (consulta + Grupos 1 e 2 + PAT). O usuário
    resolve o CAPTCHA na janela do SAT. Salva na pasta do PAJ. `delay` = tempo (s) entre
    consultas (padrão 5s); `bloqueio` = espera (s) ao detectar antirobô (padrão 45s);
    `pretensao` = define quais tipos de documentos baixar (config por pretensão)."""
    return EventSourceResponse(_stream_baixar(paj_norm, cpf, nome, delay, bloqueio, pretensao))


async def _stream_baixar_instituidor(paj_norm: str, cpf: str, nome: str,
                                     delay: float, bloqueio: float):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_instituidor(paj_norm))  # subpasta separada
    # seleção de docs do instituidor (config ou default) — sem filtro por espécie;
    # nomes prefixados com "INSTITUIDOR - ".
    tipos = None
    try:
        tipos = _cfg.get_sat_tipos_instituidor()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        tipos = None
    if tipos is None:
        tipos = list(sat_client.TIPOS_INSTITUIDOR_DEFAULT)
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao="", rotulo="INSTITUIDOR - "):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-instituidor")
async def baixar_instituidor(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                             bloqueio: float = 45.0):
    """SSE do DOWNLOAD dos documentos do INSTITUIDOR (2ª consulta, CPF do instituidor;
    novo CAPTCHA na janela). Usado em pretensões derivadas (pensão por morte / auxílio-
    reclusão). Salva na MESMA pasta do PAJ com prefixo 'INSTITUIDOR - '."""
    return EventSourceResponse(_stream_baixar_instituidor(paj_norm, cpf, nome, delay, bloqueio))


@router.get("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_get():
    """Catálogo de tipos de documentos + seleção salva (padrão + por pretensão)."""
    from ingestao import sat_client
    padrao = None
    por_pretensao: dict = {}
    instituidor = None
    try:
        padrao = _cfg.get_sat_tipos_padrao()  # type: ignore[name-defined]
        por_pretensao = _cfg.get_sat_tipos_por_pretensao()  # type: ignore[name-defined]
        instituidor = _cfg.get_sat_tipos_instituidor()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({
        "tipos": sat_client.TIPOS_DOCUMENTOS,
        "padrao": padrao,  # None = todos
        "por_pretensao": por_pretensao,
        "instituidor": instituidor,  # None = usar o default abaixo
        "instituidor_default": sat_client.TIPOS_INSTITUIDOR_DEFAULT,
    })


@router.post("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_set(request: Request):
    """Salva as keys de uma pretensão (pretensão vazia = PADRÃO)."""
    body = await request.json()
    pretensao = str(body.get("pretensao", "") or "")
    keys = body.get("keys") or []
    if not isinstance(keys, list):
        keys = []
    try:
        res = _cfg.set_sat_tipos(pretensao, [str(k) for k in keys])  # type: ignore[name-defined]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return JSONResponse({"ok": True, **res})


@router.get("/api/sat/{paj_norm}/processar")
async def processar(paj_norm: str, grupo: str = "", dry: int = 0, prazo: int = 0):
    """SSE do processamento (mov→tram→concluir). `dry=1` => simulação; `prazo` = dias
    de prazo de conclusão da tramitação (0 = sem prazo)."""
    return EventSourceResponse(_stream_processar(paj_norm, grupo, bool(dry), int(prazo)))


async def _stream_anexar_instituidor(paj_norm: str, id_processo: str, assistido: str):
    async for ev in sat_service.processar_instituidor_stream(paj_norm, id_processo, assistido):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/anexar-instituidor")
async def anexar_instituidor(paj_norm: str, id: str = "", assistido: str = ""):
    """SSE da anexação (SÓ movimentação) dos documentos do INSTITUIDOR ao PAJ. `id` =
    id_processo; não tramita nem conclui (registro separado no histórico)."""
    return EventSourceResponse(_stream_anexar_instituidor(paj_norm, id, assistido))


async def _stream_baixar_complementar(paj_norm: str, cpf: str, nome: str, delay: float,
                                      bloqueio: float, alvos_csv: str):
    import shutil
    from ingestao import sat_client
    pasta = sat_service.pasta_complementar(paj_norm)
    with __import__("contextlib").suppress(Exception):
        shutil.rmtree(pasta, ignore_errors=True)  # isola esta rodada
    alvos = [a for a in (alvos_csv or "").split(",") if a] or None  # "key|sub"
    tipos = sorted({a.split("|", 1)[0] for a in alvos}) if alvos else None
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, str(pasta), delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao="", rotulo="", alvos=alvos):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-complementar")
async def baixar_complementar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                              bloqueio: float = 45.0, alvos: str = ""):
    """SSE do DOWNLOAD COMPLEMENTAR (nova consulta ao SAT no PAJ já concluído). `alvos` =
    "key|sub" separados por vírgula (subitens escolhidos). Salva na subpasta
    'Complementar' (limpa antes)."""
    return EventSourceResponse(_stream_baixar_complementar(paj_norm, cpf, nome, delay, bloqueio, alvos))


async def _stream_anexar_complementar(paj_norm: str, id_processo: str, assistido: str):
    async for ev in sat_service.processar_complementar_stream(paj_norm, id_processo, assistido):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/anexar-complementar")
async def anexar_complementar(paj_norm: str, id: str = "", assistido: str = ""):
    """SSE da anexação (SÓ movimentação) do download complementar; move os PDFs para a
    pasta principal ao final. Registro separado no histórico (escopo='complementar')."""
    return EventSourceResponse(_stream_anexar_complementar(paj_norm, id, assistido))


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
