"""Aba "Prazos e-proc".

Traz para dentro do painel os prazos/intimações da caixa do e-proc (Painel do
Procurador). Etapa atual: MOTOR DE SESSÃO + LOGIN AUTOMÁTICO.

- A janela do e-proc é um Chromium persistente (processo separado, conectado por
  CDP) — ver `ingestao.eproc_client`. Sobrevive a reinícios do painel.
- Login automático: as credenciais do e-proc ficam no COFRE UNIFICADO cifrado por
  senha-mestra (`services.cofre_service`, sistema "eproc"), que também guarda as
  credenciais do SISDPU e de sistemas futuros — uma senha-mestra desbloqueia tudo.
  A CONFIGURAÇÃO/DESBLOQUEIO fica no menu Configurações. Quando destrancado, o botão
  "Abrir e-proc / Login" preenche usuário/senha sozinho (2FA memorizado no perfil)
  e escolhe o perfil.

▶️ PRÓXIMA ETAPA: leitor do Painel do Procurador (extrai a tabela de prazos).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from services import cofre_service

router = APIRouter()


@router.get("/prazos", response_class=HTMLResponse)
async def pagina_prazos(request: Request):
    template = request.app.state.jinja.get_template("prazos.html")
    ambiente = getattr(request.app.state, "ambiente", {})
    html = template.render(request=request, ambiente=ambiente)
    return HTMLResponse(html)


# --- Sessão do e-proc (Chromium persistente via CDP) -----------------------
@router.get("/api/prazos/abrir", response_class=JSONResponse)
async def api_prazos_abrir():
    """Abre a janela do e-proc e tenta entrar sozinho. Usa as credenciais do cofre
    (se destrancado) para preencher o login; senão, o usuário loga na janela."""
    from ingestao import eproc_client
    usuario, senha = "", ""
    creds = cofre_service.get_credenciais("eproc")
    if creds:
        usuario, senha = creds
    return JSONResponse(await eproc_client.abrir(usuario=usuario, senha=senha))


@router.get("/api/prazos/sessao", response_class=JSONResponse)
async def api_prazos_sessao():
    """Verifica a sessão do e-proc navegando para a home (deixa a tela pronta)."""
    from ingestao import eproc_client
    return JSONResponse(await eproc_client.status())


@router.get("/api/prazos/sessao-leve", response_class=JSONResponse)
async def api_prazos_sessao_leve():
    """Status LEVE p/ polling automático: só lê a URL (não navega, não abre a
    janela)."""
    from ingestao import eproc_client
    return JSONResponse(await eproc_client.peek())


# --- Intimações/prazos (leitura autônoma do Painel do Procurador) -----------
@router.get("/api/prazos/sincronizar", response_class=JSONResponse)
async def api_prazos_sincronizar():
    """Lê TODAS as categorias de Citações/Intimações do e-proc (clica → lê →
    agrupa por processo) e persiste. Se a sessão não estiver ativa, tenta logar
    sozinho com as credenciais do cofre (mesmo fluxo do botão 'Abrir e-proc /
    Login') antes de desistir. Se o cofre estiver TRANCADO, devolve
    `cofre_trancado: true` — o front-end pede a senha-mestra, destranca e chama
    esta rota de novo, em vez de mandar o usuário logar manualmente na janela
    quando as credenciais já estão salvas (só faltou destrancar)."""
    from ingestao import eproc_client, eproc_intimacoes
    res = await eproc_intimacoes.ler_todos()
    if not res.get("ok"):
        if not cofre_service.destrancado():
            res["cofre_trancado"] = True
            return JSONResponse(res, status_code=400)
        creds = cofre_service.get_credenciais("eproc")
        if creds:
            usuario, senha = creds
            await eproc_client.abrir(usuario=usuario, senha=senha)
            res = await eproc_intimacoes.ler_todos()
    if res.get("ok"):
        eproc_intimacoes.salvar(res)
        return JSONResponse(eproc_intimacoes.carregar())
    return JSONResponse(res, status_code=400)


@router.get("/api/prazos/intimacoes", response_class=JSONResponse)
async def api_prazos_intimacoes():
    """Retorna o último resultado sincronizado (sem re-consultar o e-proc)."""
    from ingestao import eproc_intimacoes
    return JSONResponse(eproc_intimacoes.carregar())


@router.get("/api/prazos/abrir-processo", response_class=JSONResponse)
async def api_prazos_abrir_processo(url: str):
    """Abre o processo (Consulta Processual) em uma aba nova da janela do e-proc,
    usando o link capturado na sincronização."""
    from ingestao import eproc_client
    return JSONResponse(await eproc_client.abrir_processo(url))


@router.get("/api/prazos/abrir-processo-numero", response_class=JSONResponse)
async def api_prazos_abrir_processo_numero(numero: str):
    """Abre o processo (Consulta Processual) em uma aba nova da janela do e-proc,
    pesquisando DIRETO pelo número — não depende de um link capturado numa
    sincronização anterior. Usado pelo minimenu "Abrir no navegador" em qualquer
    tela que mostre um número de processo (ex.: página do PAJ)."""
    from ingestao import eproc_client
    return JSONResponse(await eproc_client.abrir_processo_por_numero(numero))


# --- Link do PAJ: abre o detalhamento no SISDPU (nova aba do navegador) ------
@router.get("/api/prazos/abrir-paj")
async def api_prazos_abrir_paj(paj: str):
    """Resolve a URL de detalhe do PAJ no SISDPU (via busca, com cache) e
    REDIRECIONA para ela — abre no navegador do usuário (já logado no SISDPU).
    A URL do detalhamento é stateless (só precisa do cookie de sessão)."""
    from services import paj_link_service
    r = await paj_link_service.resolver(paj)
    if r.get("ok") and r.get("url"):
        return RedirectResponse(r["url"])
    msg = r.get("erro") or "Não foi possível abrir o PAJ."
    return HTMLResponse(
        f"<div style='font-family:Segoe UI,sans-serif;padding:2rem;color:#b91c1c'>"
        f"<h3>PAJ {paj}</h3><p>{msg}</p>"
        f"<p style='color:#555'>Verifique se a sessão do SISDPU está ativa (cofre desbloqueado) "
        f"e tente novamente.</p></div>",
        status_code=404,
    )
