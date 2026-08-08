"""Cliente do e-proc via MCP headless (HTTP puro, SEM navegador) — TRF4/JFPR.

Importa o projeto `C:\\Claude\\MCP\\eproc-trf4` como biblioteca (os mesmos módulos
que o servidor MCP registrado no Claude Code usa — `claude mcp add eproc-trf4`)
para consultas estruturadas e verificação/renovação de sessão sem depender da
janela do Chromium (`ingestao.eproc_client`) estar aberta nem logada: login
100% automático via TOTP (credenciais no Windows Credential Manager, chave
`mcp-eproc-trf4` — configurado uma vez via
`python C:\\Claude\\MCP\\eproc-trf4\\scripts\\setup_totp.py`; ver esse projeto
para detalhes do fluxo de login SSO Keycloak/PJe).

Este módulo COMPLEMENTA, não substitui, `eproc_client`/`eproc_intimacoes`:
  - Aqui (sem navegador): consulta pontual de processo/triagem estruturada,
    verificação de sessão em segundo plano, resolução do gate de "Acesso à
    Íntegra" (captcha) — capacidade nova, o fluxo do navegador não tinha isso.
  - `eproc_client`/`eproc_intimacoes` (navegador, Playwright): leitura da caixa
    de intimações (com estimativa de dias úteis/feriados e "lembrete" — sinais
    de UI que a leitura HTTP pura não vê) e peticionamento. Continuam como
    estão — decisão de arquitetura (conversa com o Felipe, 07/08/2026): o
    formulário real de petição varia por classe processual e tem anexos; clicar
    na tela de verdade é mais robusto e mais seguro (o usuário VÊ o que está
    sendo feito) do que recriar esse POST via HTTP cru.

Sessão própria, independente da sessão do navegador: cookies cacheados em
`MCP_DIR/.cookie_eproc_<inst>` (TTL 8h), relogin automático quando expira —
nunca pede intervenção manual (diferente do navegador, que depende do 2FA
"memorizado" no perfil do Chromium).
"""

from __future__ import annotations

import base64
import contextlib
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Projeto do MCP eproc-trf4 — importado como biblioteca. Ver
# C:\Claude\MCP\eproc-trf4\CLAUDE.md para o design completo (login SSO Keycloak,
# arquitetura MNI-first + raspador, tools disponíveis).
MCP_DIR = Path("C:/Claude/MCP/eproc-trf4")

if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# Pasta PERSISTENTE (fora do %TEMP%) onde os documentos baixados de processos
# consultados na página "Consultar processo" ficam guardados — mesmo padrão de
# `dados/` usado por eproc_intimacoes/paj_link_service (sibling de `Projeto/`).
# Escopada por NPU (dígitos), não por PAJ: esta página não exige um PAJ vinculado.
EPROC_DOCS_DIR = Path(__file__).resolve().parents[1].parent / "dados" / "eproc_docs"


def _mcp_server():
    """Import tardio do módulo `server` do eproc-trf4 — evita pagar o custo de
    import (curl_cffi, mcp, etc.) enquanto nenhuma função deste arquivo é usada."""
    import server as _server  # type: ignore  # módulo top-level do MCP_DIR

    return _server


def _mcp_client_mod():
    import client as _client  # type: ignore  # módulo top-level do MCP_DIR

    return _client


def _mcp_mni_mod():
    import mni as _mni  # type: ignore  # módulo top-level do MCP_DIR

    return _mni


def _normalizar_numero(numero: str) -> str:
    return re.sub(r"\D", "", numero or "")


def _pasta_docs(numero: str) -> Path:
    p = EPROC_DOCS_DIR / _normalizar_numero(numero)
    p.mkdir(parents=True, exist_ok=True)
    return p


# Nome de arquivo gravado pelo `baixar_documentos` do MCP: "ev{eid}_{nome}.{ext}".
_RE_NOME_EVENTO = re.compile(r"^ev(\d+)_(.+)$")


def status_documentos() -> dict[str, int]:
    """Tamanho/quantidade atuais de `EPROC_DOCS_DIR` (todos os processos) — para
    exibir em Configurações. Nunca falha (pasta ausente = zerado)."""
    n = 0
    total = 0
    if EPROC_DOCS_DIR.exists():
        for f in EPROC_DOCS_DIR.rglob("*"):
            if f.is_file():
                n += 1
                with contextlib.suppress(OSError):
                    total += f.stat().st_size
    return {"arquivos": n, "bytes": total}


def limpar_documentos_antigos(dias: Optional[int] = None) -> dict[str, int]:
    """Apaga, em TODA a `EPROC_DOCS_DIR` (todos os processos), os arquivos com
    mtime mais antigo que `dias` (usa mtime — o Windows nem sempre mantém atime
    atualizado). Remove também as pastas de processo que ficarem vazias.

    `dias=None` usa o valor configurado em Configurações (padrão 30). Chamado
    automaticamente no startup do painel (mesmo espírito da rotação de logs) —
    sem confirmação, porque tudo aqui é re-baixável do e-proc a qualquer
    momento; nada é perdido de verdade."""
    if dias is None:
        from services import config_painel_service as _cfg
        dias = _cfg.get_eproc_docs_retencao_dias()
    limite = time.time() - dias * 86400
    removidos = 0
    liberados = 0
    pastas_removidas = 0
    if not EPROC_DOCS_DIR.exists():
        return {"removidos": 0, "bytes_liberados": 0, "pastas_removidas": 0}
    for pasta_npu in EPROC_DOCS_DIR.iterdir():
        if not pasta_npu.is_dir():
            continue
        for f in list(pasta_npu.iterdir()):
            if not f.is_file():
                continue
            with contextlib.suppress(OSError):
                if f.stat().st_mtime < limite:
                    liberados += f.stat().st_size
                    f.unlink()
                    removidos += 1
        with contextlib.suppress(OSError):
            if not any(pasta_npu.iterdir()):
                pasta_npu.rmdir()
                pastas_removidas += 1
    return {"removidos": removidos, "bytes_liberados": liberados, "pastas_removidas": pastas_removidas}


def limpar_todos_documentos() -> dict[str, int]:
    """Apaga TODA a `EPROC_DOCS_DIR` (todos os processos) de uma vez — botão
    "Limpar tudo" das Configurações. Recria a pasta vazia em seguida."""
    st = status_documentos()
    if EPROC_DOCS_DIR.exists():
        shutil.rmtree(EPROC_DOCS_DIR, ignore_errors=True)
    EPROC_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    return {"removidos": st["arquivos"], "bytes_liberados": st["bytes"]}


def listar_arquivos_baixados(numero: str) -> dict[str, list[dict[str, Any]]]:
    """Varre a pasta persistente do processo e agrupa os arquivos já baixados
    por número de evento (a granularidade real de `baixar_documentos`: baixa
    TODOS os docs de um evento de uma vez, não um doc isolado).

    Documento HTML: `baixar_documentos` grava o `.html` original E, quando a
    extração de texto funciona, um `.txt` limpo IRMÃO (mesmo nome-base) — dois
    arquivos para o MESMO documento (no e-proc é só um). Aqui os dois são
    agrupados num item só (o `.html`, visualmente igual ao do e-proc); o `.txt`
    fica disponível em `arquivo_txt` — não vira item/badge separado, mas segue
    pronto para uma futura função que precise ler o texto (bem mais leve que o
    `.html` cru, sem a folha de estilo do CKEditor).

    Retorna {"<evento>": [{"nome_arquivo": ..., "arquivo": <nome do arquivo,
    usado por /api/eproc-mcp/arquivo>, "bytes": N, "arquivo_txt": <opcional>},
    ...]}. Pasta inexistente/vazia → {}."""
    pasta = EPROC_DOCS_DIR / _normalizar_numero(numero)
    if not pasta.exists():
        return {}

    # eid -> nome-base (sem extensão) -> {extensão: Path}
    por_evento: dict[str, dict[str, dict[str, Path]]] = {}
    for f in sorted(pasta.iterdir()):
        if not f.is_file():
            continue
        m = _RE_NOME_EVENTO.match(f.name)
        if not m:
            continue
        eid, nome = m.group(1), m.group(2)
        base = Path(nome).stem
        por_evento.setdefault(eid, {}).setdefault(base, {})[Path(nome).suffix.lower()] = f

    out: dict[str, list[dict[str, Any]]] = {}
    for eid, bases in por_evento.items():
        itens: list[dict[str, Any]] = []
        prefixo = f"ev{eid}_"
        for exts in bases.values():
            principal_ext = ".html" if ".html" in exts else next(iter(exts))
            principal = exts[principal_ext]
            item: dict[str, Any] = {
                "nome_arquivo": principal.name[len(prefixo):] if principal.name.startswith(prefixo) else principal.name,
                "arquivo": principal.name,
                "bytes": principal.stat().st_size,
            }
            txt = exts.get(".txt")
            if txt is not None and principal_ext == ".html":
                item["arquivo_txt"] = txt.name
            itens.append(item)
            # Extensões extras do mesmo nome-base além da principal/.txt não
            # deveriam ocorrer na prática, mas não descarta silenciosamente se
            # ocorrer. IMPORTANTE: pula `principal_ext` também — senão o mesmo
            # arquivo (ex.: um PDF isolado, sem par .html/.txt) entra 2x aqui,
            # uma via `principal` e outra por este loop (bug corrigido).
            for ext, f in exts.items():
                if ext in (principal_ext, ".txt"):
                    continue
                itens.append({
                    "nome_arquivo": f.name[len(prefixo):] if f.name.startswith(prefixo) else f.name,
                    "arquivo": f.name, "bytes": f.stat().st_size,
                })
        out[eid] = itens
    return out


async def _documentos_mni_por_evento(numero: str, instancia: int) -> dict[str, list[dict]]:
    """Busca só os METADADOS de documentos (sigla/nome/idDocumento) por evento,
    via uma chamada MNI dedicada com `incluir_documentos=True`.

    Existe porque `triagem_processo`/`listar_eventos` do MCP, no caminho MNI,
    NÃO pedem documentos ao SOAP por padrão (decisão de manter a triagem
    enxuta) — sem isso a tabela de Eventos do painel vinha sempre sem
    documentos para processos servidos via MNI (o caminho mais comum/rápido).
    Silenciosa em qualquer falha (retorna {}); nunca deve derrubar a consulta
    principal por causa disso."""
    try:
        mni_mod = _mcp_mni_mod()
        num = _normalizar_numero(numero)
        via = await mni_mod.ler_processo_mni(
            num, instancia, com_eventos=True, incluir_documentos=True
        )
        if not via:
            return {}
        return {
            str(ev.get("numero")): ev.get("documentos") or []
            for ev in via.get("eventos", [])
            if ev.get("documentos")
        }
    except Exception:  # noqa: BLE001 — best-effort, não é crítico para a triagem
        return {}


async def status(instancia: int = 1) -> dict[str, Any]:
    """Verifica/renova a sessão HTTP (headless) da instância indicada.

    Login 100% automático (TOTP) se a sessão cacheada expirou ou não existe.
    Retorna {"ok": True, "logado": True, "instancia": N} em sucesso, ou
    {"ok": False, "erro": "...", "instancia": N} em falha (credenciais/seed
    ausentes ou erradas no Windows Credential Manager, TRF fora do ar, etc.)."""
    client_mod = _mcp_client_mod()
    try:
        async with client_mod.EprocClient(instancia=instancia):
            pass  # __aenter__ já faz o login/renova a sessão cacheada
        return {"ok": True, "logado": True, "instancia": instancia}
    except Exception as e:  # noqa: BLE001 — qualquer falha de login/transporte
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:400], "instancia": instancia}


async def consultar_processo(numero: str, instancia: int = 1) -> dict[str, Any]:
    """Dados cadastrais do processo (classe, partes, juízo, valor) — via MNI ou
    raspador HTTP, sem navegador. Espelha a tool MCP `consultar_processo`."""
    return await _mcp_server().consultar_processo(numero, instancia=instancia)


async def triagem_processo(
    numero: str,
    instancia: int = 1,
    eventos: int = 30,
    todos_eventos: bool = False,
    desde_evento: Optional[int] = None,
    incluir_texto_referenciados: bool = False,
) -> dict[str, Any]:
    """Visão estruturada (cabeçalho + timeline + intimações com prazo/ciência
    ficta) — via raspador HTTP, sem navegador. Espelha a tool MCP
    `triagem_processo`.

    **Resolve o gate de "Acesso à Íntegra" automaticamente, em silêncio**: se a
    1ª consulta vier com `acesso_integral_pendente: true` (alguns eventos com
    documento oculto porque o usuário pessoal não está vinculado ao processo),
    chama `resolver_acesso_integra` por trás (no TRF4 é só o código TOTP, sem
    captcha) e refaz a consulta — o chamador recebe direto a versão completa,
    sem precisar tratar esse passo. Sinaliza no retorno:
      - `acesso_integral_resolvido_automaticamente: true` — se precisou
        desbloquear e conseguiu (a resposta já é a versão completa).
      - `acesso_integral_erro: "..."` — se precisou e NÃO conseguiu (a
        resposta é a mesma da 1ª consulta, ainda incompleta; `erro` explica
        o motivo). Caso raro — no TRF4 o desbloqueio é automático e confiável.

    NOTA: a estimativa de prazo aqui é a "ciência ficta crua" (Enviado em + 10
    dias corridos, sem calendário de feriados) — para a estimativa já com dias
    úteis/feriados (usada na aba "Prazos e-proc"), continue com
    `ingestao.eproc_intimacoes` (via navegador)."""
    server_mod = _mcp_server()
    kwargs = dict(
        instancia=instancia, eventos=eventos, todos_eventos=todos_eventos,
        desde_evento=desde_evento, incluir_texto_referenciados=incluir_texto_referenciados,
    )
    res = await server_mod.triagem_processo(numero, **kwargs)

    if res.get("acesso_integral_pendente"):
        desbloqueio = await resolver_acesso_integra(numero, instancia=instancia)
        if desbloqueio.get("ok") and not desbloqueio.get("precisa_captcha"):
            res = await server_mod.triagem_processo(numero, **kwargs)
            res["acesso_integral_resolvido_automaticamente"] = True
        else:
            res["acesso_integral_erro"] = (
                desbloqueio.get("erro")
                or "não foi possível liberar o acesso integral automaticamente"
            )

    eventos_out = res.get("eventos") or []
    if eventos_out:
        # 1) Preenche `documentos` (sigla/nome) dos eventos que vieram via MNI
        #    sem eles — ver `_documentos_mni_por_evento`. Eventos que já têm
        #    `documentos` (caminho raspador) não são tocados.
        if any("documentos" not in ev for ev in eventos_out):
            docs_mni = await _documentos_mni_por_evento(numero, instancia)
            if docs_mni:
                for ev in eventos_out:
                    if "documentos" not in ev:
                        docs = docs_mni.get(str(ev.get("numero")))
                        if docs:
                            ev["documentos"] = docs

        # 2) Marca quais eventos já têm os documentos baixados nesta pasta
        #    persistente (visitas anteriores) — granularidade por EVENTO, pois
        #    `baixar_documentos` baixa todos os docs de um evento de uma vez.
        baixados_por_evento = listar_arquivos_baixados(numero)
        if baixados_por_evento:
            for ev in eventos_out:
                arquivos = baixados_por_evento.get(str(ev.get("numero")))
                if arquivos:
                    ev["arquivos_baixados"] = arquivos

    return res


def decode_html_documento(conteudo: bytes) -> str:
    """Decodifica bytes de um documento HTML do e-proc respeitando o charset
    que ele mesmo declara (muitos despachos/decisões antigos vêm em
    ISO-8859-1, não UTF-8 — decodificar cru como UTF-8 corrompe acentuação).
    Espelha `server._decode_html_documento`, usado ao GERAR o .txt limpo;
    usada aqui para servir o .html ORIGINAL corretamente também, caso o
    usuário abra o arquivo de fidelidade em vez do .txt."""
    return _mcp_server()._decode_html_documento(conteudo)


async def baixar_documentos_evento(
    numero: str, instancia: int, evento_ids: list[str]
) -> dict[str, Any]:
    """Baixa (MNI-first, com fallback raspador) TODOS os documentos dos eventos
    pedidos, gravando na pasta PERSISTENTE do processo (`EPROC_DOCS_DIR`, não o
    `%TEMP%` padrão da tool) — para que fiquem disponíveis em visitas futuras à
    página "Consultar processo". Reusa a tool `baixar_documentos` do MCP por
    inteiro (mesma lógica de acesso — PDF direto, HTML convertido para .txt
    limpo, resolução de "Acesso à Íntegra" já ocorrida na triagem); só muda o
    destino.

    Retorna o dict cru da tool (`baixados`/`ignorados_sem_documento`/`erros`/
    `via`) + `arquivos_por_evento` (mesmo formato de `listar_arquivos_baixados`,
    já atualizado, pronto para o front-end aplicar direto sem re-consultar).

    IDEMPOTENTE: apaga primeiro qualquer arquivo já existente dos eventos
    pedidos. A tool `baixar_documentos` foi pensada pra pasta `%TEMP%` de uma
    sessão (nunca reescreve, sempre renomeia em colisão: "ev3_2_DESPADEC1.html",
    "ev3_3_..."); numa pasta PERSISTENTE revisitada isso acumula duplicatas
    a cada novo download do mesmo evento. Como o conteúdo de um evento já
    concluído no e-proc não muda, apagar-e-rebaixar é seguro."""
    server_mod = _mcp_server()
    destino = _pasta_docs(numero)
    for eid in evento_ids:
        prefixo = f"ev{eid}_"
        for f in destino.glob(f"{prefixo}*"):
            with contextlib.suppress(OSError):
                f.unlink()
    res = await server_mod.baixar_documentos(
        numero, instancia=instancia, evento_ids=evento_ids, diretorio_destino=str(destino),
    )
    res["arquivos_por_evento"] = listar_arquivos_baixados(numero)
    return res


async def resolver_acesso_integra(
    numero: str,
    instancia: int = 1,
    captcha_resposta: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve o gate "Acesso à Íntegra do Processo" (InfraCaptcha) para quando
    o usuário pessoal não está vinculado ao processo — sem navegador.
    Capacidade NOVA (o fluxo do navegador não tinha isso).

    Uso em 2 chamadas (a UI precisa mostrar a imagem entre elas):

      1ª sem `captcha_resposta` → devolve `precisa_captcha: True` +
         `captcha_b64` (PNG já em base64, pronto para
         `<img src="data:image/png;base64,...">`) + `instrucao`; ou
         `ja_tinha_acesso: True` se já dava para ver tudo.
      2ª com `captcha_resposta="Ab3Z"` (os 4 caracteres lidos na imagem) →
         `eventos_com_documento: N` se acertou, ou de novo `precisa_captcha`
         + `captcha_b64` (nova imagem) + `tentativa_errou: True` se errou —
         repita à vontade (2-4 tentativas costuma resolver)."""
    res = await _mcp_server().acessar_integra_processo(
        numero, instancia=instancia, captcha_resposta=captcha_resposta
    )
    if res.get("erro"):
        return {"ok": False, **res}
    if res.get("precisa_captcha"):
        png_path = Path(res.get("captcha_png_path", ""))
        if png_path.exists():
            res["captcha_b64"] = base64.b64encode(png_path.read_bytes()).decode("ascii")
    res["ok"] = True
    return res
