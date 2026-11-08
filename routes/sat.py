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
from services import cofre_service, historico_sat, sat_service

with __import__("contextlib").suppress(Exception):
    from services import config_painel_service as _cfg  # preferências (favoritas/prazo)


def _credenciais_sat() -> tuple[str, str]:
    """(usuario, senha) do GERID do cofre unificado (sistema "sat"); ("","") se o
    cofre estiver trancado ou sem credencial cadastrada — login fica manual."""
    creds = cofre_service.get_credenciais("sat")
    return creds if creds else ("", "")

router = APIRouter()


@router.get("/sat", response_class=HTMLResponse)
async def pagina_sat(request: Request):
    template = request.app.state.jinja.get_template("sat.html")
    # Modo EMBUTIDO (06/11/2026): `?embed=1&paj=PAJ-...` — a página Arquivos SAT reduzida a
    # UM PAJ, sem menu lateral, aberta num modal da página do PAJ (Consultas › Arquivos SAT
    # Central). Mesmas tabelas/ações; o PAJ entra na lista de manuais pelo metadata local.
    embed_paj = ""
    if request.query_params.get("embed") and request.query_params.get("paj"):
        embed_paj = request.query_params["paj"]
        with __import__("contextlib").suppress(Exception):
            sat_service.garantir_paj_na_lista(embed_paj)
    html = template.render(request=request, ambiente=getattr(request.app.state, "ambiente", {}),
                           embed_paj=embed_paj)
    return HTMLResponse(html)


@router.get("/api/sat/pendentes", response_class=JSONResponse)
async def api_pendentes():
    return JSONResponse(await sat_service.listar_pendentes())


@router.get("/api/sat/pendentes/manuais", response_class=JSONResponse)
async def api_pendentes_manuais():
    """SÓ os PAJs manuais salvos (sem ler a caixa) — pré-carga rápida ao abrir a aba."""
    return JSONResponse(sat_service.listar_pendentes_manuais())


@router.post("/api/sat/encerrar-fluxo", response_class=JSONResponse)
async def encerrar_fluxo():
    """Encerra a rotina do SAT em curso (download/levantamento) mantendo o já baixado."""
    from ingestao import sat_client
    return JSONResponse({"ok": sat_client.pedir_encerramento()})


@router.post("/api/sat/pendentes/manual", response_class=JSONResponse)
async def api_add_manual(request: Request):
    """Adiciona 1+ PAJs à lista de pendências, extraindo os dados AO VIVO do SISDPU.
    Body: {paj: "AAAA/UUU-NNNNN"} ou {pajs: [...]}. Retorna {ok, itens, erros}."""
    body = await request.json()
    entradas = body.get("pajs") or ([body.get("paj")] if body.get("paj") else [])
    itens, erros = [], []
    for txt in entradas:
        r = await sat_service.add_paj_manual(str(txt))
        if r.get("ok"):
            itens.append(r["item"])
        else:
            erros.append({"paj": str(txt), "erro": r.get("erro", "falha")})
    return JSONResponse({"ok": not erros, "itens": itens, "erros": erros})


@router.post("/api/sat/pendentes/manual/del", response_class=JSONResponse)
async def api_del_manual(request: Request):
    """Remove um PAJ manual da lista. Body: {paj_norm}."""
    body = await request.json()
    return JSONResponse(sat_service.del_paj_manual(body.get("paj_norm", "")))


@router.get("/api/sat/abrir", response_class=JSONResponse)
async def api_sat_abrir():
    """Abre a janela do SAT e tenta logar sozinho: usuário/senha do cofre
    (sistema "sat") + 2FA (seed TOTP). Se o cofre estiver trancado/sem
    credencial, login continua manual na janela; CAPTCHA é sempre manual."""
    from ingestao import sat_client
    usuario, senha = _credenciais_sat()
    return JSONResponse(await sat_client.abrir(usuario=usuario, senha=senha))


@router.post("/api/sat/consulta-manual/{paj_norm}", response_class=JSONResponse)
async def api_sat_consulta_manual(paj_norm: str):
    """Botão "Arquivos SAT Central" da página do PAJ: abre a janela do SAT na
    consulta pelo CPF do assistido (login automático se preciso) para o Defensor
    resolver o CAPTCHA e consultar à mão — não baixa nada, não muda de página."""
    from ingestao import sat_client
    cpf = sat_service._cpf_do_paj(paj_norm)
    if not cpf and sat_service._fonte_mcp():
        m = __import__("re").match(r"PAJ-(\d{4})-(\d{3})-(\d+)", paj_norm)
        if m:
            with __import__("contextlib").suppress(Exception):
                det = await sat_service._ler_paj_mcp(m.group(3), m.group(1), m.group(2))
                cpf = (det or {}).get("cpf_assistido", "") or ""
    if not cpf:
        return JSONResponse({"ok": False, "detalhe": "CPF do assistido não encontrado — sincronize o PAJ."})
    usuario, senha = _credenciais_sat()
    return JSONResponse(await sat_client.consulta_manual(cpf, usuario=usuario, senha=senha))


@router.get("/api/sat/sessao", response_class=JSONResponse)
async def api_sat_sessao():
    """Verifica se a sessão do SAT está pronta (logado, na página de consulta).
    Também tenta logar sozinho (ver `/api/sat/abrir`)."""
    from ingestao import sat_client
    usuario, senha = _credenciais_sat()
    return JSONResponse(await sat_client.status(usuario=usuario, senha=senha))


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


async def _stream_processar(paj_norm: str, grupo: str, dry: bool, prazo: int, manual: bool = False):
    async for ev in sat_service.processar_paj_stream(
        paj_norm, grupo=grupo or None, prazo_dias=prazo, dry_run=dry, manual=manual
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


def _parse_nbs(nbs: str) -> list[str]:
    """Divide a lista de NBs informada pelo usuário (separada por vírgula/espaço/;/quebra
    de linha) numa lista de strings; a normalização p/ dígitos é feita no sat_client."""
    import re as _re
    return [t for t in _re.split(r"[\s,;]+", (nbs or "").strip()) if t]


async def _stream_baixar(paj_norm: str, cpf: str, nome: str, delay: float,
                         bloqueio: float, pretensao: str, nbs: str = "", protocolos: str = "",
                         somente_pat: bool = False, delay_max: float | None = None):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm))
    if somente_pat:
        # Pedido explícito de baixar SÓ o(s) processo(s) administrativo(s) (PAT) — pula
        # Grupo 1 e Grupo 2 por completo, ignorando a config de tipos por pretensão.
        tipos = ["pat"]
    else:
        # resolve os tipos de documentos a baixar pela PRETENSÃO do PAJ (config por pretensão);
        # sem config → todos os tipos (comportamento padrão).
        tipos = None
        try:
            tipos = _cfg.resolver_sat_tipos(pretensao, sat_client.TIPOS_KEYS)  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            tipos = None
    # compilação por tipo (config "Arquivos SAT Central"); default ligado, limiar 3.
    compilar, compilar_limiar = True, 3
    try:
        _d = _cfg.get_sat_central_defaults()  # type: ignore[name-defined]
        compilar, compilar_limiar = bool(_d.get("compilar", True)), int(_d.get("compilar_limiar", 3))
    except Exception:  # noqa: BLE001
        pass
    # espécies de PAT a ignorar (lista configurável)
    pat_ignorar = []
    try:
        pat_ignorar = _cfg.get_sat_pat_ignorar()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        pat_ignorar = []
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
                tipos=tipos, pretensao=pretensao, nbs=_parse_nbs(nbs),
                protocolos=_parse_nbs(protocolos),
                compilar=compilar, compilar_limiar=compilar_limiar,
                pat_ignorar=pat_ignorar):
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
        {"nome": a["nome"], "tamanho": a.get("tamanho", 0), "excluido": a.get("excluido", False),
         "escopo": a.get("escopo", "assistido"), "referencia": a.get("referencia", "")}
        for a in arqs]})


@router.get("/api/sat/{paj_norm}/arquivo")
async def abrir_arquivo(paj_norm: str, nome: str):
    """Serve um PDF do SAT (ativo ou na lixeira 'Excluídos') INLINE, para abrir em nova
    aba do navegador. `nome` é o nome do arquivo (basename; sem travessia de caminho)."""
    base = Path(nome).name  # anti path-traversal
    # procura nas 4 pastas: assistido (principal + Excluídos) e instituidor (+ Excluídos)
    for pasta in (sat_service.pasta_arquivos_sat(paj_norm),
                  sat_service.pasta_instituidor(paj_norm),
                  sat_service.pasta_excluidos(paj_norm),
                  sat_service.pasta_excluidos_instituidor(paj_norm)):
        p = pasta / base
        if p.exists() and p.is_file():
            return FileResponse(
                str(p), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{base}"'})
    return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)


@router.post("/api/sat/{paj_norm}/excluir-arquivo", response_class=JSONResponse)
async def api_excluir_arquivo(paj_norm: str, request: Request):
    """Move o documento para 'Excluídos' (soft delete). `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.excluir_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


@router.post("/api/sat/{paj_norm}/recuperar-arquivo", response_class=JSONResponse)
async def api_recuperar_arquivo(paj_norm: str, request: Request):
    """Traz o documento de volta de 'Excluídos' para a lista. `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.recuperar_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


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
    # havendo instituidor, oferece também o inventário dele (mesma catálogo de tipos) para
    # que o modal "Baixar mais" permita baixar documentos complementares do instituidor.
    if sat_service.tem_instituidor(paj_norm):
        inv_i = sat_service.inventario_paj(paj_norm, escopo="instituidor")
        inv["instituidor"] = {"itens": inv_i.get("itens", []), "cpf": inv_i.get("cpf", "")}
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
                 bloqueio: float = 45.0, pretensao: str = "", nbs: str = "", protocolos: str = "",
                 somente_pat: int = 0, delay_max: float | None = None):
    """SSE do DOWNLOAD dos documentos do SAT (consulta + Grupos 1 e 2 + PAT). O usuário
    resolve o CAPTCHA na janela do SAT. Salva na pasta do PAJ. `delay` = tempo (s) entre
    consultas (padrão 5s) — mínimo; `delay_max` = máximo (cada pausa é sorteada entre os
    dois; ausente = intervalo fixo); `bloqueio` = espera (s) ao detectar antirobô (padrão 45s);
    `pretensao` = define quais tipos de documentos baixar (config por pretensão);
    `nbs` = filtro inicial por NB (Grupo 2 + PAT), separados por vírgula/espaço;
    `protocolos` = filtro inicial por protocolo (só PAT), separados por vírgula/espaço;
    `somente_pat` = 1 pula Grupo 1 e Grupo 2 por completo (baixa só o PAT, opcionalmente
    restrito pelos `protocolos` acima) — vale para QUALQUER consulta, com ou sem protocolo
    indicado."""
    return EventSourceResponse(
        _stream_baixar(paj_norm, cpf, nome, delay, bloqueio, pretensao, nbs, protocolos,
                        bool(somente_pat), delay_max))


async def _stream_baixar_instituidor(paj_norm: str, cpf: str, nome: str,
                                     delay: float, bloqueio: float,
                                     delay_max: float | None = None):
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
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
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
                             bloqueio: float = 45.0, delay_max: float | None = None):
    """SSE do DOWNLOAD dos documentos do INSTITUIDOR (2ª consulta, CPF do instituidor;
    novo CAPTCHA na janela). Usado em pretensões derivadas (pensão por morte / auxílio-
    reclusão). Salva na MESMA pasta do PAJ com prefixo 'INSTITUIDOR - '."""
    return EventSourceResponse(_stream_baixar_instituidor(paj_norm, cpf, nome, delay, bloqueio, delay_max))


@router.get("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_get():
    """Lista de espécies de processo administrativo (PAT) a ignorar (não baixar)."""
    try:
        itens = _cfg.get_sat_pat_ignorar()  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        itens = []
    return JSONResponse({"itens": itens})


@router.post("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_set(request: Request):
    """Salva a lista de espécies de PAT a ignorar."""
    body = await request.json()
    itens = body.get("itens") or []
    if not isinstance(itens, list):
        itens = []
    try:
        itens = _cfg.set_sat_pat_ignorar([str(x) for x in itens])  # type: ignore[name-defined]
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "itens": itens})


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
    pretensoes: list = []
    with __import__("contextlib").suppress(Exception):
        from services import rotina_dossie_sd_service
        pretensoes = rotina_dossie_sd_service.pretensoes_conhecidas()
    return JSONResponse({
        "tipos": sat_client.TIPOS_DOCUMENTOS,
        "padrao": padrao,  # None = todos
        "por_pretensao": por_pretensao,
        "instituidor": instituidor,  # None = usar o default abaixo
        "instituidor_default": sat_client.TIPOS_INSTITUIDOR_DEFAULT,
        "pretensoes": pretensoes,  # pretensões vistas nos PAJs (Configurações → Arquivos SAT)
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


@router.post("/api/sat/{paj_norm}/fichas-resumo", response_class=JSONResponse)
async def fichas_resumo(paj_norm: str, request: Request):
    """Revisão ÚNICA do "Anexar ao PAJ" (fonte SIS-DPU 2.0), padrão do painel:
    roda o processamento em dry-run e devolve as fichas de movimentação e
    tramitação que o SIS-DPU 2.0 montaria — nada é gravado. O front mostra tudo
    num modal só e, confirmado, registra as aprovações antecipadas
    (/api/sisdpu2/aprovacoes-antecipadas) antes de executar."""
    b = await request.json()
    return JSONResponse(await _coletar_fichas(sat_service.processar_paj_stream(
        paj_norm, grupo=b.get("grupo") or "", prazo_dias=int(b.get("prazo") or 0),
        dry_run=True, manual=bool(b.get("manual")))))


@router.post("/api/sat/{paj_norm}/fichas-resumo-anexar-mais", response_class=JSONResponse)
async def fichas_resumo_anexar_mais(paj_norm: str, request: Request):
    """Revisão ÚNICA do "Anexar mais" (fonte SIS-DPU 2.0): a ficha da movimentação
    complementar, em dry-run — nada é gravado."""
    b = await request.json()
    nomes = [n for n in (b.get("nomes") or []) if n]
    return JSONResponse(await _coletar_fichas(sat_service.anexar_mais_stream(
        paj_norm, b.get("rec") or "", b.get("id") or "", b.get("assistido") or "", nomes, dry_run=True)))


async def _coletar_fichas(gen) -> dict:
    """Roda um fluxo do SAT em dry-run recolhendo as fichas do SIS-DPU 2.0."""
    from ingestao import sisdpu2_movtram
    coleta: list = []
    erros: list[str] = []
    conclui = False
    t = sisdpu2_movtram.COLETA_FICHAS.set(coleta)
    try:
        async for ev in gen:
            if ev.get("done"):
                res = ev.get("resultado") or {}
                if not res.get("ok") and res.get("detalhe") and not erros:  # o passo já trouxe o motivo
                    erros.append(res["detalhe"])
            elif ev.get("level") == "erro":
                msg = (ev.get("msg") or "").strip().lstrip("✗").strip()
                if msg and not any(e in msg for e in erros):
                    erros.append(msg)
            elif "Conclusão do trâmite originário" in (ev.get("msg") or ""):
                conclui = True
    finally:
        sisdpu2_movtram.COLETA_FICHAS.reset(t)
    return {"ok": not erros, "fichas": coleta, "erros": erros[:5], "conclui": conclui}


@router.get("/api/sat/{paj_norm}/processar")
async def processar(paj_norm: str, grupo: str = "", dry: int = 0, prazo: int = 0, manual: int = 0):
    """SSE do processamento (mov→tram→concluir). `dry=1` => simulação; `prazo` = dias
    de prazo de conclusão da tramitação (0 = sem prazo); `manual=1` => PAJ adicionado à
    mão (movimenta e tramita, SEM conclusão — não há trâmite na caixa)."""
    return EventSourceResponse(_stream_processar(paj_norm, grupo, bool(dry), int(prazo), bool(manual)))


async def _stream_baixar_complementar(paj_norm: str, cpf: str, nome: str, delay: float,
                                      bloqueio: float, alvos_csv: str, escopo: str = "assistido",
                                      delay_max: float | None = None):
    import shutil
    from ingestao import sat_client
    paths = sat_service._escopo_paths(paj_norm, escopo)
    pasta = paths["complementar"]
    with __import__("contextlib").suppress(Exception):
        from services import exclusao_suave  # exclusão suave (Lixeira, fora do processo do painel) — services/exclusao_suave.py, 23/10/2026
        exclusao_suave.apagar_pasta(pasta, "sat:complementar")  # isola esta rodada
    alvos = [a for a in (alvos_csv or "").split(",") if a] or None  # "key|sub"
    tipos = sorted({a.split("|", 1)[0] for a in alvos}) if alvos else None
    # acervo = pasta do escopo (principal/instituidor): verificação prévia pula o que já foi
    # baixado (retomada de download interrompido, sem re-baixar os já presentes).
    acervo = str(paths["destino"])
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, str(pasta), delay_s=delay, bloqueio_s=bloqueio, delay_max_s=delay_max,
                tipos=tipos, pretensao="", rotulo=paths["rotulo"], alvos=alvos, pasta_acervo=acervo):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-complementar")
async def baixar_complementar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                              bloqueio: float = 45.0, alvos: str = "", escopo: str = "assistido",
                              delay_max: float | None = None):
    """SSE do DOWNLOAD COMPLEMENTAR (nova consulta ao SAT no PAJ já concluído). `alvos` =
    "key|sub" separados por vírgula (subitens escolhidos). `escopo` = assistido|instituidor
    (instituidor usa CPF do instituidor, rótulo 'INSTITUIDOR - ' e subpasta 'Instituidor').
    Salva na subpasta complementar do escopo (limpa antes)."""
    return EventSourceResponse(
        _stream_baixar_complementar(paj_norm, cpf, nome, delay, bloqueio, alvos, escopo, delay_max))


async def _stream_anexar_mais(paj_norm: str, rec: str, id_processo: str, assistido: str, nomes_csv: str):
    # filenames sanitizados não contêm '|' → separador seguro para a lista.
    nomes = [n for n in (nomes_csv or "").split("|") if n]
    async for ev in sat_service.anexar_mais_stream(paj_norm, rec, id_processo, assistido, nomes):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/anexar-mais")
async def anexar_mais(paj_norm: str, rec: str = "", id: str = "", assistido: str = "", nomes: str = ""):
    """SSE da ANEXAÇÃO COMPLEMENTAR (nova movimentação só de juntada, sem tramitar/concluir)
    num PAJ já concluído. `rec` = id do registro do histórico a complementar; `nomes` =
    nomes dos arquivos escolhidos, separados por '|'."""
    return EventSourceResponse(_stream_anexar_mais(paj_norm, rec, id, assistido, nomes))


@router.post("/api/sat/{paj_norm}/mesclar-complementar", response_class=JSONResponse)
async def mesclar_complementar(paj_norm: str, escopo: str = "assistido"):
    """Download complementar em PAJ pendente (ainda NÃO anexado): move os PDFs para a pasta
    do escopo (assistido → principal; instituidor → 'Instituidor') e mescla o log/inventário
    — SEM movimentação. Os arquivos entram no acervo para a futura 'Anexar ao PAJ'."""
    return JSONResponse(sat_service.mesclar_complementar_pendente(paj_norm, escopo))


@router.post("/api/sat/{paj_norm}/reiniciar", response_class=JSONResponse)
async def reiniciar_download(paj_norm: str):
    """"Reiniciar download": apaga (para a Lixeira, pela exclusão suave) tudo o que
    foi baixado/levantado do SAT para este PAJ pendente; o front recomeça o
    download completo em seguida."""
    return JSONResponse(sat_service.reiniciar_download(paj_norm))


async def _stream_levantar(paj_norm: str, cpf: str, nome: str, delay: float, bloqueio: float,
                           delay_max: float | None = None):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm))
    try:
        async for ev in sat_client.levantar_cidadao_stream(cpf, nome, pasta, delay_s=delay,
                                                           bloqueio_s=bloqueio, delay_max_s=delay_max):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/levantar")
async def levantar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                   bloqueio: float = 45.0, delay_max: float | None = None, substituir: int = 0):
    """SSE do LEVANTAMENTO: lista o que existe no SAT para o CPF, SEM baixar (o
    usuário resolve o CAPTCHA na janela). Grava `_levantamento.json` na pasta do
    PAJ; o "Baixar mais" passa a oferecer tudo o que foi encontrado.
    Já havendo levantamento completo, só roda com `substituir=1` (confirmado no
    modal da página)."""
    ja = sat_service.estado_download(paj_norm).get("levantamento_em")
    if ja and not substituir:
        async def _recusa():
            yield {"event": "done", "data": json.dumps(
                {"ok": False, "detalhe": f"Já existe levantamento completo deste PAJ ({ja}) — "
                                         "confirme na página para fazer outro."}, ensure_ascii=False)}
        return EventSourceResponse(_recusa())
    return EventSourceResponse(_stream_levantar(paj_norm, cpf, nome, delay, bloqueio, delay_max))


@router.post("/api/sat/{paj_norm}/apagar-arquivos-sat", response_class=JSONResponse)
async def apagar_arquivos_sat(paj_norm: str):
    """Remove a pasta 'Arquivos SAT' de um PAJ já concluído (libera espaço em disco) E
    remove o(s) registro(s) desse PAJ do histórico — a linha some de "PAJs Concluídos".
    NÃO desfaz a anexação já feita no SISDPU."""
    return JSONResponse(sat_service.apagar_arquivos_sat_paj(paj_norm))


@router.get("/api/sat/grupos", response_class=JSONResponse)
async def api_grupos(id: str = ""):
    """Lista caixas de grupo da unidade. `id` = id_processo de referência p/ abrir a
    tela de tramitação; se ausente, usa o de uma pendência. As caixas frequentes
    (badges de clique rápido) vêm de /api/config/sisdpu, não daqui."""
    if id and not sat_service._fonte_mcp():
        grupos = await movtram.listar_grupos_unidade(id)
    else:
        grupos = await sat_service.listar_grupos()  # MCP: catálogo de destinos
    return JSONResponse({"grupos": grupos, "padrao": sat_service.GRUPO_PADRAO})

# ---------------------------------------------------------------------------
# Consulta "Arquivos SAT Central" da página do PAJ (06/11/2026): o Defensor marca
# PESSOAS do PAJ (assistidos, representantes, instituidor, CPF avulso) e TIPOS de
# documento; o painel baixa para o ACERVO da página Arquivos SAT (cada pessoa no seu
# escopo) e põe o PAJ na lista de lá, onde segue "Anexar ao PAJ"/"Baixar mais".
# ---------------------------------------------------------------------------
_SAT_COMUNS = ["cnis_vinculos", "cnis_remuneracoes", "declaracao_beneficios", "crer",
               "carta_concessao", "laudo_medico", "laudo_social", "pat"]


@router.get("/api/sat/{paj_norm}/consulta-pessoas", response_class=JSONResponse)
async def api_consulta_pessoas(paj_norm: str):
    """Pessoas do PAJ + catálogo de documentos + sugeridos pela pretensão (fallback: comuns)."""
    from ingestao import sat_client
    meta = sat_service._ler_metadata(paj_norm)
    pret = meta.get("pretensaopura") or meta.get("pretensao", "")
    todos = sat_client.TIPOS_KEYS
    sugeridos = list(_SAT_COMUNS)
    with __import__("contextlib").suppress(Exception):
        sugeridos = [k for k in _cfg.resolver_sat_tipos(pret, todos) if k in todos]  # type: ignore[name-defined]
    return JSONResponse({"pessoas": sat_service.pessoas_do_paj(paj_norm),
                         "tipos": sat_client.TIPOS_DOCUMENTOS,
                         "sugeridos": sugeridos, "pretensao": pret})


async def _stream_baixar_selecao(paj_norm: str, pessoas: list[dict], tipos: list[str],
                                 nbs: str, delay: float, delay_max: float | None,
                                 bloqueio: float, hiscre_de: str = "", hiscre_ate: str = "",
                                 protocolos: str = ""):
    from ingestao import sat_client

    def log(msg, level="info"):
        return {"event": "log", "data": json.dumps({"level": level, "msg": msg}, ensure_ascii=False)}

    if not sat_service.garantir_paj_na_lista(paj_norm):
        r = await sat_service.add_paj_manual(sat_service._norm_to_num(paj_norm))
        if not r.get("ok"):
            yield log(f"Não consegui incluir o PAJ na página Arquivos SAT: {r.get('erro')}", "erro")
            yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": r.get("erro")}, ensure_ascii=False)}
            return
    resumo, encerrado = [], False
    for i, ps in enumerate(pessoas, 1):
        cpf, nome = ps.get("cpf", ""), (ps.get("nome") or "").strip()
        escopo = ps.get("escopo") or ""
        if not escopo.startswith(("assistido", "instituidor")):
            escopo = sat_service.registrar_pessoa(paj_norm, cpf, nome, ps.get("papel") or "Outra pessoa")
        paths = sat_service._escopo_paths(paj_norm, escopo)
        destino = paths["destino"]
        # acervo já existe → baixa na subpasta complementar (pula o já presente) e mescla
        # depois, sem sobrescrever o log/inventário do 1º download.
        complementar = (destino / "_log_download.json").exists()
        pasta = paths["complementar"] if complementar else destino
        if complementar:
            import shutil
            shutil.rmtree(pasta, ignore_errors=True)
        yield log(f"━━ {i}/{len(pessoas)} · {ps.get('papel') or 'Pessoa'}: {nome or cpf} (CPF {cpf}) ━━", "novo")
        res: dict = {}
        try:
            async for ev in sat_client.baixar_cidadao_stream(
                    cpf, nome, str(pasta), delay_s=delay, bloqueio_s=bloqueio,
                    delay_max_s=delay_max, tipos=tipos, rotulo=paths["rotulo"],
                    nbs=_parse_nbs(nbs), protocolos=_parse_nbs(protocolos),
                    pasta_acervo=str(destino), hiscre_de=hiscre_de, hiscre_ate=hiscre_ate):
                if ev.get("done"):
                    res = ev.get("resultado", {}) or {}
                else:
                    yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "detalhe": str(e)}
            yield log(str(e), "erro")
        if complementar:
            with __import__("contextlib").suppress(Exception):
                sat_service.mesclar_complementar_pendente(paj_norm, escopo)
        n = len(res.get("salvos") or [])
        resumo.append({"cpf": cpf, "nome": nome, "papel": ps.get("papel"), "baixados": n,
                       "ok": bool(res.get("ok")), "detalhe": res.get("detalhe", "")})
        yield log(f"  → {nome or cpf}: {n} documento(s) baixado(s)", "ok" if res.get("ok") else "erro")
        if res.get("encerrado"):
            encerrado = True
            break
    total = sum(r["baixados"] for r in resumo)
    yield {"event": "done", "data": json.dumps(
        {"ok": True, "encerrado": encerrado, "total": total, "pessoas": resumo,
         "detalhe": f"{total} documento(s) baixado(s) para o acervo do Arquivos SAT."},
        ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-selecao")
async def baixar_selecao(paj_norm: str, pessoas: str, tipos: str = "", nbs: str = "",
                         delay: float = 5.0, delay_max: float | None = None,
                         bloqueio: float = 45.0, hiscre_de: str = "", hiscre_ate: str = "",
                         protocolos: str = ""):
    """SSE: baixa os `tipos` (csv de keys) para cada pessoa de `pessoas` (JSON
    [{cpf, nome, papel, escopo}]) — uma consulta por CPF (CAPTCHA a cada uma).
    `hiscre_de`/`hiscre_ate` (MM/AAAA, opcionais) = período do HISCRE."""
    try:
        lst = [x for x in json.loads(pessoas) if len("".join(c for c in x.get("cpf", "") if c.isdigit())) == 11]
    except Exception:  # noqa: BLE001
        lst = []
    tks = [t for t in (tipos or "").split(",") if t]
    return EventSourceResponse(_stream_baixar_selecao(paj_norm, lst, tks, nbs, delay, delay_max, bloqueio,
                                                      hiscre_de, hiscre_ate, protocolos))
