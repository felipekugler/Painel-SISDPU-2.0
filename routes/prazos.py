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
from fastapi.responses import HTMLResponse, JSONResponse

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
