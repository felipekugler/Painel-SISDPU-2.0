"""Rotas do e-proc via MCP headless (HTTP puro, SEM navegador) — TRF4/JFPR.

Ver `ingestao.eproc_mcp_client` para o design completo. Resumo: complementa (não
substitui) as rotas de `prazos.py`/`acoes_eproc.py` (Playwright, navegador
visível) — aqui é tudo sem janela, com login 100% automático via TOTP.

- `/api/eproc-mcp/status`: verifica/renova a sessão HTTP em segundo plano.
- `/api/eproc-mcp/consultar`: dados cadastrais do processo (JSON estruturado).
- `/api/eproc-mcp/triagem`: cabeçalho + timeline + intimações (JSON estruturado).
- `/api/eproc-mcp/acesso-integra`: resolve o gate de "Acesso à Íntegra" — no
  TRF4/JFPR é automático (código TOTP, sem imagem); no TRF6 pediria captcha
  (a UI trataria `precisa_captcha`/`captcha_b64`, hoje não exercitado aqui).
- `/api/eproc-mcp/baixar-documentos`: baixa os documentos de um ou mais eventos
  (PDF direto, HTML já convertido pra .txt limpo) numa pasta persistente por
  processo (`ingestao.eproc_mcp_client.EPROC_DOCS_DIR`).
- `/api/eproc-mcp/arquivo`: serve um documento já baixado dessa pasta.
- `/api/eproc-mcp/lembretes`: lembretes NATIVOS do e-proc (não os do painel),
  lidos do cache já sincronizado pela aba Prazos — somente leitura.
"""

from __future__ import annotations

import contextlib
import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

router = APIRouter()


@router.get("/api/eproc-mcp/status", response_class=JSONResponse)
async def api_eproc_mcp_status(instancia: int = 1):
    """Verifica/renova a sessão HTTP (headless) da instância — sem navegador,
    sem depender do 2FA "memorizado" no perfil do Chromium."""
    from ingestao import eproc_mcp_client as emc

    res = await emc.status(instancia=instancia)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/eproc-mcp/consultar", response_class=JSONResponse)
async def api_eproc_mcp_consultar(numero: str, instancia: int = 1):
    """Dados cadastrais do processo (classe, partes, juízo, valor) — sem abrir
    aba no navegador do e-proc."""
    from ingestao import eproc_mcp_client as emc

    res = await emc.consultar_processo(numero, instancia=instancia)
    return JSONResponse(res, status_code=200 if "erro" not in res else 400)


@router.get("/api/eproc-mcp/triagem", response_class=JSONResponse)
async def api_eproc_mcp_triagem(
    numero: str,
    instancia: int = 1,
    eventos: int = 30,
    todos_eventos: bool = False,
    desde_evento: int | None = None,
    incluir_texto: bool = False,
):
    """Cabeçalho + timeline + intimações (com ciência ficta CRUA, sem calendário
    de feriados — para a estimativa com dias úteis, ver a aba "Prazos e-proc")."""
    from ingestao import eproc_mcp_client as emc

    res = await emc.triagem_processo(
        numero, instancia=instancia, eventos=eventos, todos_eventos=todos_eventos,
        desde_evento=desde_evento, incluir_texto_referenciados=incluir_texto,
    )
    return JSONResponse(res, status_code=200 if "erro" not in res else 400)


@router.post("/api/eproc-mcp/acesso-integra", response_class=JSONResponse)
async def api_eproc_mcp_acesso_integra(request: Request):
    """Resolve o gate "Acesso à Íntegra do Processo" sem navegador. No TRF4/JFPR
    é automático (código TOTP do mesmo autenticador do login — sem imagem, sem
    `captcha_resposta`). Corpo esperado: {"numero": "...", "instancia": 1,
    "captcha_resposta": null}."""
    from ingestao import eproc_mcp_client as emc

    body = await request.json()
    numero = (body.get("numero") or "").strip()
    if not numero:
        return JSONResponse({"ok": False, "erro": "numero é obrigatório."}, status_code=400)
    instancia = int(body.get("instancia") or 1)
    captcha_resposta = body.get("captcha_resposta") or None
    res = await emc.resolver_acesso_integra(numero, instancia=instancia, captcha_resposta=captcha_resposta)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/eproc-mcp/baixar-documentos", response_class=JSONResponse)
async def api_eproc_mcp_baixar_documentos(request: Request):
    """Baixa os documentos de um ou mais eventos (MNI-first, fallback raspador)
    numa pasta persistente do processo. Corpo esperado: {"numero": "...",
    "instancia": 1, "evento_ids": ["37", "40"]}."""
    from ingestao import eproc_mcp_client as emc

    body = await request.json()
    numero = (body.get("numero") or "").strip()
    evento_ids = [str(e).strip() for e in (body.get("evento_ids") or []) if str(e).strip()]
    if not numero or not evento_ids:
        return JSONResponse(
            {"erro": "numero e evento_ids (não vazio) são obrigatórios."}, status_code=400
        )
    instancia = int(body.get("instancia") or 1)
    res = await emc.baixar_documentos_evento(numero, instancia=instancia, evento_ids=evento_ids)
    return JSONResponse(res, status_code=200 if "erro" not in res else 400)


_ARQUIVO_HTML_SHELL = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>{titulo}</title>
<style>
  body {{ margin: 0; padding: 2.5rem 3rem; background: #f5f5f5; }}
  .folha {{ max-width: 52rem; margin: 0 auto; background: #fff; padding: 2.5rem 3rem;
            border-radius: 4px; box-shadow: 0 2px 10px rgba(0,0,0,.12);
            font-family: 'Times New Roman', Georgia, Times, serif; font-size: 15px;
            line-height: 1.6; color: #1a1a1a; }}
  .folha pre {{ white-space: pre-wrap; word-break: break-word; font-family: inherit;
                margin: 0; }}
</style></head><body><div class="folha"><pre>{conteudo}</pre></div></body></html>"""


def _escapar_html(texto: str) -> str:
    return (texto.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))


@router.get("/api/eproc-mcp/arquivo")
async def api_eproc_mcp_arquivo(numero: str, arquivo: str, bruto: bool = False):
    """Serve um documento já baixado da pasta persistente do processo.

    `arquivo` deve ser exatamente um nome de arquivo já existente naquela
    pasta (validado por listagem, não por path) — bloqueia travessia de
    diretório (`../`, caminhos absolutos, subpastas).

    `.html`: muitos documentos do e-proc (despachos/decisões) vêm em
    ISO-8859-1, não UTF-8 — servidos com `charset=utf-8` fixo (como antes)
    ficavam ilegíveis (acentuação virava caracteres estranhos). Aqui o
    conteúdo é lido respeitando o charset que o PRÓPRIO documento declara e
    reservido já como UTF-8 correto.
    `.txt` (a versão limpa, sem a folha de estilo do CKEditor): por padrão
    vem envolvido numa folha simples (fonte serifada, largura de leitura) —
    bem mais legível que texto puro sem formatação nenhuma. `?bruto=true`
    devolve o .txt cru (para copiar/colar sem a folha)."""
    from ingestao import eproc_mcp_client as emc

    pasta = emc.EPROC_DOCS_DIR / emc._normalizar_numero(numero)
    nomes_validos = {f.name for f in pasta.iterdir()} if pasta.exists() else set()
    if arquivo not in nomes_validos:
        return PlainTextResponse("Arquivo não encontrado.", status_code=404)

    caminho = pasta / arquivo
    sufixo = caminho.suffix.lower()

    # "Toca" o arquivo (e o .txt irmão, se houver) a cada visualização — a
    # limpeza automática por retenção (Configurações › e-proc) considera
    # "não acessado há N dias", não só "baixado há N dias".
    with contextlib.suppress(OSError):
        os.utime(caminho, None)
        irmao_txt = caminho.with_suffix(".txt")
        if irmao_txt.exists():
            os.utime(irmao_txt, None)

    if sufixo == ".pdf":
        return FileResponse(caminho, media_type="application/pdf", filename=caminho.name,
                             content_disposition_type="inline")

    if sufixo == ".html":
        texto = emc.decode_html_documento(caminho.read_bytes())
        return HTMLResponse(texto)

    if sufixo == ".txt":
        texto = caminho.read_text(encoding="utf-8", errors="replace")
        if bruto:
            return PlainTextResponse(texto, media_type="text/plain; charset=utf-8")
        html = _ARQUIVO_HTML_SHELL.format(titulo=_escapar_html(caminho.name), conteudo=_escapar_html(texto))
        return HTMLResponse(html)

    return FileResponse(caminho, media_type="application/octet-stream", filename=caminho.name,
                         content_disposition_type="inline")


@router.get("/api/eproc-mcp/docs-cache", response_class=JSONResponse)
async def api_eproc_mcp_docs_cache_status():
    """Status do cache de documentos (Configurações › e-proc): retenção
    configurada + tamanho/quantidade atuais em disco."""
    from ingestao import eproc_mcp_client as emc
    from services import config_painel_service

    return JSONResponse({
        "retencao_dias": config_painel_service.get_eproc_docs_retencao_dias(),
        **emc.status_documentos(),
    })


@router.post("/api/eproc-mcp/docs-cache/retencao", response_class=JSONResponse)
async def api_eproc_mcp_docs_cache_retencao(request: Request):
    """Salva a retenção (dias) do cache de documentos. Corpo: {"dias": 30}."""
    from services import config_painel_service

    body = await request.json()
    dias = config_painel_service.set_eproc_docs_retencao_dias(body.get("dias"))
    return JSONResponse({"ok": True, "retencao_dias": dias})


@router.post("/api/eproc-mcp/docs-cache/limpar-tudo", response_class=JSONResponse)
async def api_eproc_mcp_docs_cache_limpar_tudo():
    """Apaga TODOS os documentos baixados de TODOS os processos (botão "Limpar
    tudo" das Configurações). Sem confirmação no backend — a UI já confirma;
    tudo aqui é re-baixável do e-proc a qualquer momento."""
    from ingestao import eproc_mcp_client as emc

    return JSONResponse(emc.limpar_todos_documentos())


@router.get("/api/eproc-mcp/lembretes", response_class=JSONResponse)
async def api_eproc_mcp_lembretes(numero: str):
    """Lembretes NATIVOS do e-proc (post-it da listagem de citações/intimações)
    para este processo — somente leitura, a partir do cache já sincronizado
    pela aba "Prazos" ("Sincronizar intimações"). Não há como criar/alterar um
    lembrete NO e-proc a partir do painel; e só acha algo se o processo já
    tiver passado por aquela sincronização."""
    from ingestao import eproc_intimacoes

    return JSONResponse(eproc_intimacoes.buscar_lembretes(numero))


@router.get("/api/eproc-mcp/prazos-procurador", response_class=JSONResponse)
async def api_eproc_mcp_prazos_procurador(numero: str):
    """Prazos do MEU Painel do Procurador (cache já sincronizado pela aba
    Prazos) para este NPU — usado pra confirmar quais intimações da triagem
    MCP (que lê o processo inteiro, todas as partes) pertencem à DPU, sem
    depender de casar nome do assistido (frágil: representante legal,
    litisconsórcio, nome cadastrado diferente entre SISDPU e e-proc)."""
    from ingestao import eproc_intimacoes

    return JSONResponse(eproc_intimacoes.prazos_processo(numero))
