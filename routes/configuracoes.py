"""Rota de Configurações do Painel — toggles de comportamento da sincronização."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import cofre_service, config_painel_service

router = APIRouter()


@router.get("/configuracoes", response_class=HTMLResponse)
async def pagina_configuracoes(request: Request):
    config = config_painel_service.ler_config()
    tmpl = request.app.state.jinja.get_template("configuracoes.html")
    return HTMLResponse(tmpl.render(request=request, config=config))


@router.post("/api/configuracoes")
async def salvar_configuracoes(request: Request):
    body = await request.json()
    salvo = config_painel_service.salvar_config(body)
    return JSONResponse({"ok": True, "config": salvo})


# --- Valores padrão da aba "Arquivos SAT Central" ---
@router.get("/api/config/sat-central")
async def get_sat_central():
    return JSONResponse(config_painel_service.get_sat_central_defaults())


@router.post("/api/config/sat-central")
async def set_sat_central(request: Request):
    body = await request.json()
    defaults = config_painel_service.set_sat_central_defaults(body)
    return JSONResponse({"ok": True, "defaults": defaults})


# --- Preferências SISDPU: fases de movimentação + caixas de tramitação ---
@router.get("/api/config/sisdpu", response_class=JSONResponse)
async def get_config_sisdpu():
    """Listas importadas + favoritas (dropdown) + frequentes (badges)."""
    return JSONResponse(config_painel_service.get_sisdpu_prefs())


@router.post("/api/config/sisdpu", response_class=JSONResponse)
async def set_config_sisdpu(request: Request):
    """Salva a curadoria (favoritas/frequentes de fases e caixas)."""
    body = await request.json()
    return JSONResponse({"ok": True, "prefs": config_painel_service.set_sisdpu_prefs(body)})


@router.post("/api/config/sisdpu/importar", response_class=JSONResponse)
async def importar_config_sisdpu():
    """Importa do SISDPU a lista COMPLETA de fases de movimentação e caixas de
    tramitação (navegação Playwright) e cacheia em painel_config.json."""
    from ingestao import sisdpu_movtram as movtram
    from services import retornos_service
    idp = await retornos_service.algum_id_processo()
    if not idp:
        return JSONResponse(
            {"ok": False, "erro": "Nenhum PAJ na caixa para abrir as telas de fase/caixa. "
                                  "Sincronize os retornos e tente de novo."}, status_code=400)
    fases = await movtram.listar_fases_movimentacao(idp)
    caixas = await movtram.listar_grupos_unidade(idp)
    prefs = config_painel_service.set_sisdpu_listas_todas(fases, caixas)
    return JSONResponse({"ok": True, "prefs": prefs,
                         "n_fases": len(fases), "n_caixas": len(caixas)})


# --- Preferências e-proc: eventos de peticionamento/movimentação ---
@router.get("/api/config/eproc-eventos", response_class=JSONResponse)
async def get_config_eproc_eventos():
    """Lista importada + favoritos (dropdown) + frequentes (badges)."""
    return JSONResponse(config_painel_service.get_eproc_eventos_prefs())


@router.post("/api/config/eproc-eventos", response_class=JSONResponse)
async def set_config_eproc_eventos(request: Request):
    """Salva a curadoria (favoritos/frequentes/padrão de eventos)."""
    body = await request.json()
    return JSONResponse({"ok": True, "prefs": config_painel_service.set_eproc_eventos_prefs(body)})


@router.post("/api/config/eproc-eventos/importar", response_class=JSONResponse)
async def importar_config_eproc_eventos(request: Request):
    """Importa do e-proc a lista COMPLETA de eventos disponíveis para peticionar,
    usando um PROCESSO DE REFERÊNCIA informado pelo usuário (a lista depende da
    classe judicial do processo). Requer a janela do e-proc aberta e logada."""
    from services import acoes_eproc_service
    body = await request.json()
    numero = (body.get("numero_processo") or "").strip()
    if not numero:
        return JSONResponse({"ok": False, "erro": "Informe o número do processo de referência."},
                            status_code=400)
    res = await acoes_eproc_service.importar_eventos(numero)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status_code)


# --- Preferências e-proc: tipos de documento (classificação de anexos) ---
@router.get("/api/config/eproc-tipos-documento", response_class=JSONResponse)
async def get_config_eproc_tipos_documento():
    """Lista importada + favoritos (dropdown) + frequentes (badges)."""
    return JSONResponse(config_painel_service.get_eproc_tipos_doc_prefs())


@router.post("/api/config/eproc-tipos-documento", response_class=JSONResponse)
async def set_config_eproc_tipos_documento(request: Request):
    """Salva a curadoria (favoritos/frequentes/padrão de tipos de documento)."""
    body = await request.json()
    return JSONResponse({"ok": True, "prefs": config_painel_service.set_eproc_tipos_doc_prefs(body)})


@router.post("/api/config/eproc-tipos-documento/importar", response_class=JSONResponse)
async def importar_config_eproc_tipos_documento(request: Request):
    """Importa do e-proc a lista COMPLETA de tipos de documento (classificação de
    anexos), usando um PROCESSO DE REFERÊNCIA informado pelo usuário. Requer a
    janela do e-proc aberta e logada."""
    from services import acoes_eproc_service
    body = await request.json()
    numero = (body.get("numero_processo") or "").strip()
    if not numero:
        return JSONResponse({"ok": False, "erro": "Informe o número do processo de referência."},
                            status_code=400)
    res = await acoes_eproc_service.importar_tipos_documento(numero)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status_code)


# --- Preferências e-proc: níveis de sigilo por documento ---
@router.get("/api/config/eproc-sigilo", response_class=JSONResponse)
async def get_config_eproc_sigilo():
    """Lista importada + padrão (lista curta/estável — sem favoritos/frequentes)."""
    return JSONResponse(config_painel_service.get_eproc_sigilo_prefs())


@router.post("/api/config/eproc-sigilo", response_class=JSONResponse)
async def set_config_eproc_sigilo(request: Request):
    """Salva só o padrão (a lista em si só muda por importação)."""
    body = await request.json()
    prefs = config_painel_service.set_eproc_sigilo_padrao(body.get("sigilo_padrao", ""))
    return JSONResponse({"ok": True, "prefs": prefs})


@router.post("/api/config/eproc-sigilo/importar", response_class=JSONResponse)
async def importar_config_eproc_sigilo(request: Request):
    """Importa do e-proc os níveis de sigilo disponíveis por documento, usando um
    PROCESSO DE REFERÊNCIA informado pelo usuário. Requer a janela do e-proc aberta
    e logada."""
    from services import acoes_eproc_service
    body = await request.json()
    numero = (body.get("numero_processo") or "").strip()
    if not numero:
        return JSONResponse({"ok": False, "erro": "Informe o número do processo de referência."},
                            status_code=400)
    res = await acoes_eproc_service.importar_sigilos(numero)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status_code)


# --- Preferências e-proc: procuradores destino do Gerenciamento de Processo ---
@router.get("/api/config/eproc-procuradores", response_class=JSONResponse)
async def get_config_eproc_procuradores():
    """Lista importada + favoritos (dropdown) + frequentes (badges)."""
    return JSONResponse(config_painel_service.get_eproc_procuradores_prefs())


@router.post("/api/config/eproc-procuradores", response_class=JSONResponse)
async def set_config_eproc_procuradores(request: Request):
    """Salva a curadoria (favoritos/frequentes/padrão de procuradores destino)."""
    body = await request.json()
    return JSONResponse({"ok": True, "prefs": config_painel_service.set_eproc_procuradores_prefs(body)})


@router.post("/api/config/eproc-procuradores/importar", response_class=JSONResponse)
async def importar_config_eproc_procuradores(request: Request):
    """Importa do e-proc a lista COMPLETA de procuradores/unidades disponíveis
    como destino no Gerenciamento de Processo (substabelecimento), usando um
    PROCESSO DE REFERÊNCIA informado pelo usuário. Requer a janela do e-proc
    aberta e logada."""
    from services import acoes_eproc_service
    body = await request.json()
    numero = (body.get("numero_processo") or "").strip()
    if not numero:
        return JSONResponse({"ok": False, "erro": "Informe o número do processo de referência."},
                            status_code=400)
    res = await acoes_eproc_service.importar_procuradores_gerenciamento(numero)
    status_code = 200 if res.get("ok") else 400
    return JSONResponse(res, status_code=status_code)


# --- Tabela de Ofícios: nome completo -> sigla (padronização de exibição) ---
@router.get("/api/config/oficios", response_class=JSONResponse)
async def get_config_oficios():
    return JSONResponse({"itens": config_painel_service.get_oficios_siglas()})


@router.post("/api/config/oficios", response_class=JSONResponse)
async def set_config_oficios(request: Request):
    body = await request.json()
    itens = config_painel_service.set_oficios_siglas(body.get("itens") or [])
    return JSONResponse({"ok": True, "itens": itens})


# --- Acesso ao e-proc (compat): delega ao COFRE UNIFICADO (sistema "eproc") ----
# Endpoints legados mantidos para a tela de Configurações atual continuar
# funcionando. As credenciais do e-proc agora vivem no cofre unificado
# (services.cofre_service) — uma única senha-mestra desbloqueia SISDPU + e-proc.
# A API canônica nova é /api/cofre/* (routes.cofre). Estes serão aposentados.
@router.get("/api/config/eproc/status", response_class=JSONResponse)
async def eproc_cred_status():
    return {"ok": True,
            "configurado": cofre_service.configurado(),
            "destrancado": cofre_service.destrancado(),
            "tem_credencial": cofre_service.tem_credencial("eproc"),
            "usuario": cofre_service.get_usuario("eproc")}


@router.post("/api/config/eproc/credenciais", response_class=JSONResponse)
async def eproc_cred_salvar(payload: dict = Body(...)):
    """Define/atualiza as credenciais do e-proc no cofre unificado. Se o cofre já
    existe, a senha-mestra deve conferir com ele (uma única senha-mestra)."""
    p = payload or {}
    usuario = (p.get("usuario") or "").strip()
    senha = p.get("senha") or ""
    senha_mestra = p.get("senha_mestra") or ""
    confirma = p.get("senha_mestra_confirma") or ""
    if not usuario or not senha or not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Preencha usuário, senha do e-proc e senha-mestra."},
                            status_code=400)
    if len(senha_mestra) < 6:
        return JSONResponse({"ok": False, "erro": "A senha-mestra deve ter ao menos 6 caracteres."},
                            status_code=400)
    if senha_mestra != confirma:
        return JSONResponse({"ok": False, "erro": "A confirmação da senha-mestra não confere."},
                            status_code=400)
    try:
        cofre_service.salvar_sistema("eproc", usuario, senha, senha_mestra)
    except cofre_service.SenhaMestraIncorreta:
        return JSONResponse(
            {"ok": False, "erro": "A senha-mestra não confere com o cofre já existente. "
                                  "Use a mesma senha-mestra do cofre."}, status_code=401)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return {"ok": True}


@router.post("/api/config/eproc/desbloquear", response_class=JSONResponse)
async def eproc_cred_desbloquear(payload: dict = Body(...)):
    senha_mestra = (payload or {}).get("senha_mestra", "")
    if not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Informe a senha-mestra."}, status_code=400)
    if cofre_service.destrancar(senha_mestra):
        return {"ok": True, "usuario": cofre_service.get_usuario("eproc")}
    return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)


@router.post("/api/config/eproc/bloquear", response_class=JSONResponse)
async def eproc_cred_bloquear():
    """Esquece as credenciais da memória (tranca o cofre inteiro)."""
    cofre_service.bloquear()
    return {"ok": True}


@router.delete("/api/config/eproc/credenciais", response_class=JSONResponse)
async def eproc_cred_remover(payload: dict = Body(default=None)):
    """Remove apenas o e-proc do cofre (mantém os demais sistemas). Requer a
    senha-mestra no corpo para poder re-cifrar o cofre."""
    senha_mestra = (payload or {}).get("senha_mestra", "") if payload else ""
    if not senha_mestra:
        return JSONResponse(
            {"ok": False, "erro": "Informe a senha-mestra para remover a credencial do e-proc."},
            status_code=400)
    try:
        cofre_service.remover_sistema("eproc", senha_mestra)
    except cofre_service.SenhaMestraIncorreta:
        return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)
    return {"ok": True}
