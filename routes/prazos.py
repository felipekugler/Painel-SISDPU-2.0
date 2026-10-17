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
    usuario, senha = eproc_client.credenciais_login()  # MCP do e-proc primeiro; cofre de reserva
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
    agrupa por processo) e persiste — login automático sozinho com as
    credenciais do cofre se a sessão não estiver ativa (mesmo fluxo do botão
    'Abrir e-proc / Login'); `cofre_trancado: true` se não tiver como (o
    front-end pede a senha-mestra e chama esta rota de novo).

    Serializada internamente (`eproc_intimacoes.sincronizar`, single-flight):
    um clique duplicado ou qualquer chamada concorrente NUNCA dispara uma 2ª
    tentativa disputando a mesma aba do e-proc com a que já está rodando — só
    espera essa terminar e reaproveita o mesmo resultado. Sem isso, duas
    tentativas simultâneas de login/leitura na MESMA aba controlada por CDP
    deixavam a janela presa tentando logar indefinidamente (bug real relatado
    2026-09-16).

    A purga de casos já revisados (`intimacao_service.purgar_casos_
    resolvidos`) NÃO roda mais aqui: DESATIVADA como automação em 2026-08-28
    (poucos minutos após ligar) — ela apaga em lote, via `shutil.rmtree`, as
    pastas de documentos de casos que saíram da caixa, e essa exclusão
    RECURSIVA EM LOTE por um processo de rede (python.exe na porta 8001) bateu
    na proteção antirransomware do antivírus corporativo (FortiClient: "File
    Delete blocked for python.exe") — confirmado ao vivo: a 1ª sincronização
    após ligar a purga já travou TODO salvamento posterior de análise de
    intimação (mesmo sem nenhuma minuta envolvida), e voltou a travar a cada
    sincronização seguinte, em instâncias novas do painel. Movida para disparo
    MANUAL no botão "Atualizar lista" da página Intimações
    (`routes.intimacoes.api_listar_casos`) — clique explícito do Defensor, bem
    mais espaçado que uma sincronização automática."""
    from ingestao import eproc_intimacoes, eproc_jmu
    res = await eproc_intimacoes.sincronizar()
    # JMU (17/10/2026): lida em seguida pelo MCP próprio; falha dela não
    # derruba a do TRF4 — vira aviso e a tabela mantém a última leitura da JMU.
    erro_jmu = ""
    if eproc_jmu.configurado():
        rj = await eproc_jmu.sincronizar()
        if not rj.get("ok"):
            erro_jmu = rj.get("erro") or "falha desconhecida"
    if res.get("ok"):
        res = eproc_jmu.mesclar(res, erro_jmu=erro_jmu)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/prazos/intimacoes", response_class=JSONResponse)
async def api_prazos_intimacoes():
    """Retorna o último resultado sincronizado (sem re-consultar o e-proc) —
    já com a baixa LOCAL de prazos cumpridos dentro do painel aplicada (ver
    `services.prazo_baixa_service`), pra a tabela não continuar mostrando um
    prazo que o Defensor já resolveu por dentro do painel (peticionou, deu
    mera ciência) até a próxima "Sincronizar intimações".

    Também tenta a busca secundária de vínculo de PAJ pra processos sem
    nenhum PAJ reconhecido no lembrete (`intimacao_service.tentar_vincular_
    pajs_pendentes` — pedido do usuário 2026-09-25) e PREENCHE `c["paj"]`
    com qualquer vínculo já resolvido (`intimacao_service.obter_vinculo`) —
    sem isso, um PAJ achado pela busca secundária só aparecia na Triagem de
    Intimações (que já mescla o vínculo em `listar_casos()`), nunca aqui:
    esta tabela lê `carregar_visivel()` puro, sem passar por lá. Best-effort
    (só leitura local): falha não pode impedir a listagem normal."""
    from ingestao import eproc_intimacoes
    from services import intimacao_service

    try:
        intimacao_service.tentar_vincular_pajs_pendentes()
    except Exception:
        pass

    dados = eproc_intimacoes.carregar_visivel()
    try:
        for casos in (dados.get("secoes") or {}).values():
            for c in casos or []:
                if c.get("paj") or not c.get("processo"):
                    continue
                vinc = intimacao_service.obter_vinculo(c["processo"])
                if vinc.get("paj"):
                    c["paj"] = vinc["paj"]
    except Exception:
        pass
    from ingestao import eproc_jmu
    return JSONResponse(eproc_jmu.mesclar(dados))


@router.get("/api/prazos/count", response_class=JSONResponse)
async def api_prazos_count():
    """Contagem pro badge do submenu "Prazos" — sem re-consultar o e-proc.

    A própria página NÃO mostra um total único (mostra 7 badges, um por
    categoria — prazo_aberto/urgente/pendente/etc., ver CATEGORIAS em
    ingestao/eproc_intimacoes.py) — então não há um "N" pra simplesmente
    copiar. Conta PROCESSOS DISTINTOS entre todas as categorias (um mesmo
    processo pode aparecer em mais de uma categoria; contar bruto inflaria o
    número) — mesmo espírito da deduplicação já feita por
    `services.intimacao_service.listar_casos()` sobre este mesmo cache,
    só que sem o cruzamento com o índice de análise (não é o que a página
    Prazos usa). Usa a leitura visível (baixa local aplicada) — mesmo
    espírito de `api_prazos_intimacoes`."""
    from ingestao import eproc_intimacoes, eproc_jmu
    dados = eproc_jmu.mesclar(eproc_intimacoes.carregar_visivel())
    processos = {
        caso.get("processo")
        for secao in (dados.get("secoes") or {}).values()
        for caso in secao
        if caso.get("processo")
    }
    return {"count": len(processos)}


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
