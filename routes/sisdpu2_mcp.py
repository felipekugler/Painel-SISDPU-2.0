"""Conexão do painel com o MCP do SIS-DPU 2.0 (OAuth) — ver ingestao/sisdpu2_mcp_client.py."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from ingestao import sisdpu2_mcp_client as sis2

router = APIRouter()


_cache_sessao: dict = {"ts": 0.0, "texto": None}


@router.get("/api/sisdpu2/status", response_class=JSONResponse)
async def api_sisdpu2_status():
    """Estado da conexão. Com token, confirma no servidor de quem é a sessão —
    no máximo a cada 5 min (cada consulta conta no teto diário do servidor)."""
    import time
    if not sis2.conectado():
        _cache_sessao.update(ts=0.0, texto=None)
        return {"conectado": False}
    if _cache_sessao["texto"] and time.monotonic() - _cache_sessao["ts"] < 300:
        return {"conectado": True, "sessao": _cache_sessao["texto"], "minutos": sis2.minutos_restantes()}
    try:
        quem = await run_in_threadpool(sis2.chamar, "auth_situacao")
    except sis2.Sisdpu2LoginNecessario:
        return {"conectado": False}
    except Exception as e:  # noqa: BLE001
        return {"conectado": True, "erro": str(e), "minutos": sis2.minutos_restantes()}
    _cache_sessao.update(ts=time.monotonic(), texto=quem)
    return {"conectado": True, "sessao": quem, "minutos": sis2.minutos_restantes()}


@router.get("/api/sisdpu2/status-selo", response_class=JSONResponse)
async def api_sisdpu2_status_selo():
    """Selo "MCP" dentro do selo do SISDPU (base.html). Não chama o servidor:
    só olha o token guardado — o selo é consultado a cada página e cada
    consulta ao MCP conta no teto diário."""
    if not sis2.conectado():
        return {"ok": False, "erro": "Painel não conectado ao MCP do SIS-DPU 2.0 "
                                     "(Configurações › SISDPU › Fonte de dados)."}
    c = sis2.consumo_hoje()
    mins = sis2.minutos_restantes()
    if c["teto_atingido"]:
        # NÃO é sessão vencida (a sessão segue válida) — a tela não pode mandar
        # "reconectar", que não resolve: o limite volta no dia seguinte.
        return {"ok": False, "teto": True, "minutos": mins,
                "erro": "Limite diário de leitura do SIS-DPU 2.0 atingido hoje — volta amanhã."}
    from services import config_painel_service
    return {"ok": True, "fonte": config_painel_service.get_fonte_dados(), "minutos": mins}


@router.get("/api/sisdpu2/fonte", response_class=JSONResponse)
async def api_sisdpu2_fonte():
    from services import config_painel_service
    return {"fonte": config_painel_service.get_fonte_dados()}


@router.post("/api/sisdpu2/fonte", response_class=JSONResponse)
async def api_sisdpu2_fonte_salvar(request: Request):
    from services import config_painel_service
    body = await request.json()
    try:
        return {"ok": True, "fonte": config_painel_service.set_fonte_dados(str(body.get("fonte") or ""))}
    except ValueError as e:
        return {"ok": False, "erro": str(e)}


@router.get("/api/sisdpu2/download-ultimas-movs", response_class=JSONResponse)
async def api_download_ultimas_movs():
    from services import config_painel_service
    return {"n": config_painel_service.get_mcp_download_ultimas_movs()}


@router.post("/api/sisdpu2/download-ultimas-movs", response_class=JSONResponse)
async def api_download_ultimas_movs_salvar(request: Request):
    from services import config_painel_service
    body = await request.json()
    try:
        return {"ok": True, "n": config_painel_service.set_mcp_download_ultimas_movs(body.get("n"))}
    except (TypeError, ValueError):
        return {"ok": False, "erro": "Informe um número inteiro (0 = não baixar nada sozinho)."}


@router.get("/api/sisdpu2/consumo", response_class=JSONResponse)
async def api_sisdpu2_consumo():
    return {"hoje": sis2.consumo_hoje(), "historico": sis2.historico_consumo(30),
            "teto": sis2.TETO_DIARIO}


@router.get("/api/sisdpu2/ao-vivo/estado", response_class=JSONResponse)
async def api_ao_vivo_estado():
    from services import ao_vivo_service
    return ao_vivo_service.estado()


@router.post("/api/sisdpu2/ao-vivo/paj/{paj_norm}", response_class=JSONResponse)
async def api_ao_vivo_paj(paj_norm: str, forcar: bool = False):
    from services import ao_vivo_service
    return await ao_vivo_service.atualizar_paj(paj_norm, forcar=forcar)


@router.post("/api/sisdpu2/ao-vivo/paj/{paj_norm}/anexos", response_class=JSONResponse)
async def api_ao_vivo_paj_anexos(paj_norm: str):
    from services import ao_vivo_service
    return await ao_vivo_service.baixar_anexos_paj(paj_norm)


@router.post("/api/sisdpu2/processo-judicial/cadastrar", response_class=JSONResponse)
async def api_sisdpu2_processo_judicial_cadastrar(request: Request):
    """Corpo: {paj_label, numero, instancia?}. Bloqueia até as fichas serem
    respondidas no modal de confirmação (cadastro e sincronização)."""
    from ingestao import sisdpu2_movtram
    b = await request.json()
    return await sisdpu2_movtram.cadastrar_processo_judicial(b.get("paj_label") or "", b.get("numero") or "",
                                                             int(b.get("instancia") or 1))


@router.post("/api/sisdpu2/processo-judicial/ficha", response_class=JSONResponse)
async def api_sisdpu2_processo_judicial_ficha(request: Request):
    """Dry-run do cadastro do processo judicial (NADA é gravado): devolve a
    ficha que o SIS-DPU 2.0 montaria — para o modal unificado pós-ajuizamento.
    Corpo: {paj_label, numero, instancia?}. Já vinculado: sem ficha (só a
    sincronização, que é autorizada sem modal)."""
    from ingestao import sisdpu2_movtram
    b = await request.json()
    coleta: list = []
    t = sisdpu2_movtram.COLETA_FICHAS.set(coleta)
    try:
        r = await sisdpu2_movtram.cadastrar_processo_judicial(b.get("paj_label") or "", b.get("numero") or "",
                                                              int(b.get("instancia") or 1))
    finally:
        sisdpu2_movtram.COLETA_FICHAS.reset(t)
    return {"ok": bool(r.get("ok")), "fichas": coleta, "ja_vinculado": bool(r.get("ja_vinculado")),
            "erros": [] if r.get("ok") else [r.get("detalhe") or "Falha ao preparar o cadastro."]}


@router.post("/api/sisdpu2/ao-vivo/paj/{paj_norm}/anexos/interromper", response_class=JSONResponse)
async def api_ao_vivo_paj_anexos_interromper(paj_norm: str):
    from services import ao_vivo_service
    return ao_vivo_service.interromper_anexos(paj_norm)


@router.get("/api/sisdpu2/ao-vivo/paj/{paj_norm}/anexos", response_class=JSONResponse)
async def api_ao_vivo_paj_anexos_progresso(paj_norm: str):
    from services import ao_vivo_service
    return ao_vivo_service.progresso_anexos(paj_norm)


@router.post("/api/sisdpu2/ao-vivo/caixa", response_class=JSONResponse)
async def api_ao_vivo_caixa(forcar: bool = False):
    from services import ao_vivo_service
    return await ao_vivo_service.atualizar_caixa(forcar=forcar)


# --- Gravação: modal padrão de aprovação da ficha (services/aprovacao_mcp_service) ---

@router.get("/api/sisdpu2/aprovacoes/pendentes", response_class=JSONResponse)
async def api_aprovacoes_pendentes():
    from services import aprovacao_mcp_service
    return {"pendentes": aprovacao_mcp_service.listar_pendentes()}


@router.post("/api/sisdpu2/aprovacoes/{ident}", response_class=JSONResponse)
async def api_aprovacao_responder(ident: str, request: Request):
    from services import aprovacao_mcp_service
    body = await request.json()
    ok = aprovacao_mcp_service.responder(ident, bool(body.get("aprovado")))
    return {"ok": ok, "erro": "" if ok else "Esta ficha não está mais aguardando (venceu ou já foi respondida)."}


@router.post("/api/sisdpu2/aprovacoes-antecipadas", response_class=JSONResponse)
async def api_aprovacoes_antecipadas(request: Request):
    """O Defensor aprovou no resumo da movimentação conjunta as fichas que
    lhe foram mostradas (devolvidas por /escrita/fichas-resumo)."""
    from services import aprovacao_mcp_service
    body = await request.json()
    fichas = [f for f in body.get("fichas") or [] if f.get("ficha") and f.get("paj") and f.get("tipo")]
    return {"ok": True, "registradas": aprovacao_mcp_service.registrar_antecipadas(fichas)}


@router.post("/api/sisdpu2/escrita/fichas-resumo", response_class=JSONResponse)
async def api_fichas_resumo(request: Request):
    """Roda a ação do console SEM GRAVAR (dry-run) e devolve as fichas que o
    SIS-DPU 2.0 montaria — para o resumo da movimentação conjunta mostrar a
    ficha integral ANTES de executar o e-proc. Corpo: {paj, id_processo,
    paj_norm} + (os campos de uma execução única) ou {itens: [...]} (fila)."""
    from ingestao import sisdpu2_movtram
    from services import acoes_sisdpu_service as svc
    b = await request.json()
    coleta: list = []
    t = sisdpu2_movtram.COLETA_FICHAS.set(coleta)
    erros: list[str] = []
    try:
        if b.get("itens"):
            gen = svc.executar_fila_stream(b.get("id_processo") or "", paj=b.get("paj") or "",
                                           paj_norm=b.get("paj_norm") or "", itens=b["itens"], dry_run=True)
        else:
            gen = svc.executar_stream(
                b.get("id_processo") or "", paj=b.get("paj") or "", paj_norm=b.get("paj_norm") or "",
                fase=b.get("fase") or "", descricao_mov=b.get("descricao_mov") or "",
                descricao_mov_html=bool(int(b.get("descricao_mov_html") or 0)), grupo=b.get("grupo") or None,
                descricao_tram=b.get("descricao_tram") or "", prazo_dias=int(b.get("prazo") or 0),
                prazo_data=b.get("prazo_data") or "", id_tramites=b.get("id_tramites") or [],
                dry_run=True, anexos=b.get("anexos") or [], etapa=b.get("etapa") or "")
        item = 0  # item da fila de cada ficha (o resumo agrupa movimentação + tramitação por item)
        async for ev in gen:
            for f in coleta:
                f.setdefault("item", item)
            if ev.get("item_done"):
                item = int(ev.get("index", item)) + 1
            if ev.get("level") == "erro":
                erros.append(ev.get("msg", ""))
            if ev.get("done"):
                res = ev.get("resultado") or {}
                detalhe = res.get("detalhe") or ""
                if not res.get("ok") and detalhe:
                    # o log já trouxe a mesma mensagem com prefixo ("Movimentação: …") — fica só uma
                    erros = [e for e in erros if detalhe not in e] + [detalhe]
        for f in coleta:
            f.setdefault("item", item)
    finally:
        sisdpu2_movtram.COLETA_FICHAS.reset(t)
    return {"ok": not erros, "fichas": coleta, "erros": erros[:5]}


@router.get("/api/sisdpu2/login", response_class=JSONResponse)
async def api_sisdpu2_login():
    try:
        return {"ok": True, "url": await run_in_threadpool(sis2.iniciar_login)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Não foi possível iniciar o login no SIS-DPU 2.0: {e}"}


@router.post("/api/sisdpu2/desconectar", response_class=JSONResponse)
async def api_sisdpu2_desconectar():
    await run_in_threadpool(sis2.desconectar)
    return {"ok": True}


_PAGINA_RETORNO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Painel SISDPU 2.0 — SIS-DPU 2.0</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;background:#f5f5f5}}div{{background:#fff;padding:32px 40px;border-radius:12px;
box-shadow:0 2px 12px #0002;text-align:center;max-width:460px}}h1{{font-size:20px;margin:0 0 8px}}</style>
</head><body><div><h1>{titulo}</h1><p>{texto}</p></div>
<script>try{{window.opener&&window.opener.postMessage('sisdpu2-login-{evento}','*')}}catch(e){{}}
setTimeout(function(){{window.close()}},{fechar});</script></body></html>"""


@router.get("/api/sisdpu2/oauth/callback", response_class=HTMLResponse)
async def api_sisdpu2_oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SIS-DPU 2.0 não concluído",
            texto=f"Motivo: {error or 'sem código de autorização'}.", evento="erro", fechar=15000))
    try:
        await run_in_threadpool(sis2.concluir_login, code, state)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_PAGINA_RETORNO.format(
            titulo="Login no SIS-DPU 2.0 não concluído", texto=str(e), evento="erro", fechar=15000))
    return HTMLResponse(_PAGINA_RETORNO.format(
        titulo="✅ Painel conectado ao SIS-DPU 2.0", texto="Pode fechar esta aba e voltar ao painel.",
        evento="ok", fechar=2500))


# --- Documento do PAJ sob demanda (clique na tabela de movimentações) ----------
# Abrir o PAJ não baixa mais os documentos (pedido do Defensor, 11/10/2026): o
# clique num documento ainda não baixado chama esta rota, que baixa SÓ ele pelo
# MCP, grava em Arquivos/ com o nome de sempre ("seq075 - 43202969 - nome.pdf")
# e já devolve o arquivo — igual à consulta processual do e-proc.
@router.get("/api/sisdpu2/documento/{paj_norm}")
async def api_sisdpu2_documento(paj_norm: str, id: str, seq: str = "", nome: str = ""):
    import mimetypes
    import re
    from pathlib import Path

    from fastapi.responses import FileResponse

    from config import PAJS_DIR
    from ingestao import sisdpu2_fonte as fonte

    if not re.fullmatch(r"PAJ-\d{4}-\d{3}-\d+", paj_norm or "") or not re.fullmatch(r"\d{1,12}", id or ""):
        return JSONResponse({"ok": False, "erro": "Parâmetros inválidos."}, status_code=400)
    nome_limpo = re.sub(r'[\\/:*?"<>|]+', "_", (nome or "arquivo").strip()) or "arquivo"
    try:
        seq_fmt = f"{int(seq):03d}"
    except ValueError:
        seq_fmt = re.sub(r"\D", "", seq) or "000"
    pasta = Path(PAJS_DIR) / paj_norm / "Arquivos"
    destino = pasta / f"seq{seq_fmt} - {id} - {nome_limpo}"
    if not (destino.exists() and destino.stat().st_size > 0):
        existente = next((f for f in pasta.glob(f"seq* - {id} - *") if f.stat().st_size > 0), None) if pasta.is_dir() else None
        if existente:
            destino = existente
        else:
            if not sis2.conectado():
                return HTMLResponse("<p>Painel não conectado ao SIS-DPU 2.0 — reconecte para baixar o documento.</p>",
                                    status_code=409)
            try:
                dados, _info = await run_in_threadpool(fonte.baixar_arquivo_mcp, int(id))
            except Exception as e:  # noqa: BLE001
                return HTMLResponse(f"<p>Não foi possível baixar o documento pelo SIS-DPU 2.0: {e}</p>", status_code=502)
            pasta.mkdir(parents=True, exist_ok=True)
            await run_in_threadpool(fonte._gravar_binario, destino, dados)
    tipo = mimetypes.guess_type(destino.name)[0] or "application/octet-stream"
    return FileResponse(destino, media_type=tipo, filename=destino.name, content_disposition_type="inline")
