"""Módulo genérico "Ações no SISDPU": console de despacho (movimentar → tramitar →
concluir) reutilizável em QUALQUER página do painel — não depende da Retornos nem de
nenhum store específico. O chamador informa o `id_processo` do SISDPU e o texto do
despacho; quando houver um trâmite específico da caixa de entrada a concluir (ex.: um
Atendimento de Retorno), informa também `id_tramite` — sem ele, a etapa de conclusão é
ignorada (nada a concluir).

Extraído de `retornos_service.py` (que hoje é o único consumidor, e passou a delegar
a execução para cá mantendo seus efeitos exclusivos — histórico e remoção do próprio
store). Futuras páginas (PAJ detail, Watchlist etc.) podem chamar este módulo direto.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import unicodedata
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncIterator

from config import PAJS_DIR
from ingestao import sisdpu_client
from ingestao import sisdpu_movtram as movtram
from services import paj_service

# Mesma lista que o SISDPU aceita nos anexos de MOVIMENTAÇÃO (sisdpu_movtram.
# _TIPOS_ANEXO_OK) — fora disso, o anexo seria silenciosamente pulado lá dentro;
# validamos aqui pra avisar o usuário na hora, em vez de deixar sumir sem aviso.
TIPOS_ANEXO_MOV_OK = {"doc", "docx", "odt", "png", "jpg", "jpeg", "gif", "pdf"}

# Rascunhos de movimentação ("Gravar" p/ executar mais tarde) — MESMA convenção
# de `dados/` na raiz do PAINEL SISDPU usada por `acoes_eproc_service.
# RASCUNHOS_DIR` (cache/estado do painel, fora do workspace do Ofício Geral).
# Um JSON por PAJ (no máx. 1 rascunho de cada vez). Além do "Gravar" manual,
# `acoes_eproc.js` grava um rascunho AQUI automaticamente depois de um
# "Peticionar" real confirmado no e-proc (se o usuário topar) — ver
# `_receberPeticaoEproc` em `static/js/acoes_sisdpu.js`.
RASCUNHOS_DIR = Path(__file__).resolve().parents[1].parent / "dados" / "acoes_sisdpu_rascunhos"


def _rascunho_json(paj_norm: str) -> Path:
    return RASCUNHOS_DIR / f"{paj_norm}.json"


_RE_NUMERO_PAJ = re.compile(r"^(\d{4})/(\d{3})-(\d{5})$")


def normalizar_numero_paj(numero: str) -> str | None:
    """'2026/029-01593' -> 'PAJ-2026-029-01593'; None se o formato não bater
    (AAAA/UUU-NNNNN). Usado por "Adicionar movimentação" (Movimentações
    Pendentes) pra criar um rascunho a partir só do número digitado, SEM
    precisar de nenhuma sincronização prévia — mesma regra de normalização já
    duplicada em ingestao/sincronizador.py, services/retornos_service.py,
    services/sat_service.py, services/sync_service.py, services/
    intimacao_service.py (nenhuma delas centralizada em config.py; replicada
    aqui em vez de importar uma daquelas pra não criar acoplamento novo)."""
    numero = (numero or "").strip()
    if not _RE_NUMERO_PAJ.match(numero):
        return None
    return "PAJ-" + numero.replace("/", "-")


def salvar_rascunho(paj_norm: str, dados: dict) -> dict:
    """Grava/sobrescreve o rascunho de movimentação do PAJ (1 por PAJ) — fase,
    despachos (HTML da movimentação, texto puro da tramitação), anexos staged,
    prazo/grupo, conclusão. Mesmo padrão de `acoes_eproc_service.salvar_rascunho`."""
    from datetime import datetime

    if not paj_norm:
        return {"ok": False, "erro": "paj_norm vazio."}
    RASCUNHOS_DIR.mkdir(parents=True, exist_ok=True)
    doc = dict(dados)
    doc["paj_norm"] = paj_norm
    doc["salvo_em"] = datetime.now().isoformat(timespec="seconds")
    _rascunho_json(paj_norm).write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "salvo_em": doc["salvo_em"]}


def obter_rascunho(paj_norm: str) -> dict | None:
    p = _rascunho_json(paj_norm)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def excluir_rascunho(paj_norm: str) -> dict:
    """Descarta o rascunho — chamado depois que a movimentação for de fato
    executada com sucesso (nada mais a retomar), ou quando o usuário desistir."""
    p = _rascunho_json(paj_norm)
    if p.exists():
        p.unlink()
    return {"ok": True}


def listar_rascunhos() -> list[dict]:
    """Todos os rascunhos salvos — alimenta a página "Movimentações pendentes".
    Enriquece com o nome do assistido lido do metadata.json do PAJ (não fica
    salvo no rascunho em si, pra não ficar desatualizado se o PAJ mudar) — mesmo
    padrão de `acoes_eproc_service.listar_rascunhos`. Também enriquece com
    `sincronizado` (bool) — False quando o PAJ ainda não tem `metadata.json`
    local (criado via "Adicionar movimentação" enquanto o SISDPU estava fora
    do ar, ou pelo fallback de mera_ciencia_service) — a UI usa isso pro selo
    "PAJ ainda não sincronizado".

    `par_eproc_processo`: número do processo de uma petição PENDENTE no
    e-proc do MESMO PAJ, se houver (rascunho em `acoes_eproc_rascunhos/`
    guardado sob esta mesma chave de PAJ) — normalmente as duas nasceram
    juntas em "Movimentar Eproc + SISDPU" (movimentar_conjunto.html), mas a
    checagem vale pra qualquer par pendente do mesmo PAJ, criado como for.
    Alimenta o badge "Eproc + SISDPU" da tabela de pendentes (pedido do
    usuário 2026-09-11 — sem ele, sair da página conjunta sem executar
    "perdia" o caminho de volta pra revisar os dois juntos)."""
    if not RASCUNHOS_DIR.exists():
        return []
    from services import acoes_eproc_service

    itens = []
    for f in RASCUNHOS_DIR.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        paj_norm = doc.get("paj_norm", "")
        doc["assistido"] = _assistido_do_paj(paj_norm)
        doc["sincronizado"] = (PAJS_DIR / paj_norm / "metadata.json").exists() if paj_norm else False
        par_eproc = acoes_eproc_service.obter_rascunho(paj_norm) if paj_norm else None
        doc["par_eproc_processo"] = (par_eproc or {}).get("processo") or ""
        itens.append(doc)
    itens.sort(key=lambda d: d.get("salvo_em", ""), reverse=True)
    return itens


def _assistido_do_paj(paj_norm: str) -> str:
    if not paj_norm:
        return ""
    meta_path = PAJS_DIR / paj_norm / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("assistido_caixa") or meta.get("assistido") or ""
    except Exception:
        return ""


def listar_pecas_ia(paj_norm: str) -> list[dict]:
    """Arquivos da subpasta "Peças IA" do PAJ (petições/peças geradas pela análise
    de pretensão — services/analise_pretensao_service.py) — alimenta o modal
    "Adicionar Petições IA" em Ações no SISDPU. MESMA função de
    acoes_eproc_service.listar_pecas_ia (duplicada aqui de propósito: cada módulo
    "Ações no X" é autocontido/não depende do outro — ver docstring no topo deste
    arquivo). Reaproveita `paj_service.ler_paj()`, que já separa "Peças IA/" do
    resto de `pecas_geradas` pelo prefixo do `caminho` — sem escanear a pasta de
    novo aqui."""
    from services.paj_service import ler_paj

    dados = ler_paj(paj_norm)
    if not dados:
        return []
    return [p for p in (dados.get("pecas_geradas") or []) if (p.get("caminho") or "").startswith("Peças IA/")]


def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _pasta_anexos_externos(paj_norm: str) -> Path:
    """Pasta temporária DENTRO da pasta do PAJ pra anexos externos (upload) ainda não
    usados numa movimentação. Nome com "_" na frente: já é ignorada pelas listagens de
    peças/despachos do PAJ (paj_service.ler_paj só itera ARQUIVOS na raiz, pastas são
    puladas de qualquer forma — mas o prefixo deixa claro que é área de rascunho)."""
    return PAJS_DIR / paj_norm / "_anexos_externos_tmp"


def salvar_anexo_externo(paj_norm: str, nome_original: str, conteudo: bytes) -> dict:
    """Salva um arquivo ENVIADO PELO USUÁRIO (upload) numa pasta temporária dentro da
    pasta do PAJ — a automação (Playwright) só consegue anexar um arquivo que já esteja
    em disco no servidor, não um File do navegador direto. Fica ali até a movimentação
    de fato usá-lo (aí é removido — ver executar_stream) ou até o usuário desistir e
    remover da lista antes de executar (ver remover_anexo_externo).

    A pasta do PAJ é criada aqui SE AINDA NÃO EXISTIR (PAJ nunca sincronizado
    localmente — caso do fluxo "Adicionar movimentação" em Movimentações
    Pendentes, pra registrar um trabalho enquanto o SISDPU está fora do ar).
    Uma pasta "esqueleto" (sem metadata.json) é inofensiva: toda varredura de
    PAJS_DIR no projeto já ignora pastas sem metadata.json válido (dashboard,
    agenda, busca, chat, sincronizador — conferido antes de fazer esta
    mudança). Quando a sincronização real acontecer depois, ela reusa a MESMA
    pasta normalmente (idempotente, ver `_processar_paj_pos_detalhamento`).
    Retorna {ok, ref, nome, tamanho, tipo} ou {ok:False, detalhe}."""
    paj_norm = (paj_norm or "").strip()
    nome_original = (nome_original or "").strip()
    if not paj_norm or not nome_original:
        return {"ok": False, "detalhe": "paj_norm ou nome do arquivo ausente."}
    if not re.match(r"^PAJ-\d{4}-\d{3}-\d{5}$", paj_norm):
        return {"ok": False, "detalhe": f"paj_norm em formato inválido: {paj_norm!r}."}
    pasta_paj = PAJS_DIR / paj_norm
    pasta_paj.mkdir(parents=True, exist_ok=True)
    ext = Path(nome_original).suffix.lstrip(".").lower()
    if ext not in TIPOS_ANEXO_MOV_OK:
        aceitos = ", ".join(sorted(t.upper() for t in TIPOS_ANEXO_MOV_OK))
        return {"ok": False, "detalhe": f'Formato ".{ext or "?"}" não é aceito pelo SISDPU nos anexos de movimentação. Aceitos: {aceitos}.'}
    pasta = _pasta_anexos_externos(paj_norm)
    pasta.mkdir(parents=True, exist_ok=True)
    ref = f"{uuid.uuid4().hex[:12]}_{nome_original}"
    caminho = pasta / ref
    caminho.write_bytes(conteudo)
    return {"ok": True, "ref": ref, "nome": nome_original, "tamanho": caminho.stat().st_size, "tipo": ext}


def _resolver_anexo_externo(paj_norm: str, ref: str) -> Path | None:
    """Resolve um `ref` de anexo externo pro caminho absoluto — mesma proteção contra
    path traversal do download de arquivos (paj_service.ler_arquivo)."""
    pasta = _pasta_anexos_externos(paj_norm)
    caminho = pasta / (ref or "")
    try:
        caminho.resolve().relative_to(pasta.resolve())
    except (ValueError, OSError):
        return None
    return caminho if caminho.exists() and caminho.is_file() else None


def remover_anexo_externo(paj_norm: str, ref: str) -> dict:
    """Apaga um anexo externo staged — best-effort. Chamado quando o usuário remove da
    lista ANTES de executar (desiste dele), ou automaticamente depois que a movimentação
    já o usou com sucesso (ver executar_stream).

    Também remove a pasta `_anexos_externos_tmp` em si, SE ficar vazia depois —
    sem isso ela nunca desaparecia (só o arquivo era apagado), deixando uma
    pasta fantasma na raiz do PAJ para sempre, mesmo depois de usado/descartado
    todo anexo externo (relatado pelo usuário 2026-09-14 — investigado e
    confirmado: NÃO é a rotina de organizar documentos para ajuizamento, é
    esta, "Ações no SISDPU" / upload de anexo externo). `rmdir()` só remove
    diretório vazio (levanta OSError se sobrar algo — outro anexo staged em
    paralelo, por exemplo — daí o `contextlib.suppress`, idempotente e
    seguro)."""
    caminho = _resolver_anexo_externo(paj_norm, ref)
    if caminho:
        with contextlib.suppress(Exception):
            caminho.unlink()
        with contextlib.suppress(Exception):
            caminho.parent.rmdir()
    return {"ok": True}


def _normalizar(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _iso_to_br(s: str) -> str:
    """'2026-08-15' -> '15/08/2026'. Aceita já-BR ou vazio (devolve intacto)."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def _parece_problema_sessao(resultado, excecao: Exception | None) -> bool:
    """True quando a falha indica SESSÃO indisponível (conflito, deslogado, cofre
    bloqueado) — casos em que vale reconectar e tentar de novo, em vez de desistir."""
    if isinstance(resultado, dict) and resultado.get("fonte") == "mcp-sisdpu2":
        # Gravação pelo MCP: nunca "reconectar e repetir" — repetir exigiria nova
        # ficha/aprovação, e a falha pode ter acontecido depois de gravar.
        return False
    if excecao is not None:
        return True
    if isinstance(resultado, dict) and not resultado.get("ok"):
        det = _normalizar(str(resultado.get("detalhe") or ""))
        return any(t in det for t in ("conflito de sessao", "sessao", "login", "cofre bloqueado"))
    return False


async def com_auto_reconexao(chamada):
    """Executa `chamada()` (coroutine factory); se falhar por sessão indisponível,
    RECONECTA automaticamente (login fresco no SISDPU) e tenta a MESMA ação mais uma
    vez. Retorna (resultado, excecao, eventos_sse) — eventos_sse já prontos para `yield`."""
    eventos: list[dict] = []
    try:
        resultado = await chamada()
        excecao = None
    except Exception as e:  # noqa: BLE001
        resultado, excecao = None, e

    if _parece_problema_sessao(resultado, excecao):
        eventos.append(_ev("muted", "Sessão do SISDPU indisponível — reconectando automaticamente…"))
        try:
            rec_conn = await sisdpu_client.reconectar()
        except Exception as e:  # noqa: BLE001
            rec_conn = {"ok": False, "detalhe": str(e)[:300]}
        eventos.append(_ev("ok" if rec_conn.get("ok") else "erro",
                           f"Reconexão automática: {rec_conn.get('detalhe', '')}"))
        if rec_conn.get("ok"):
            try:
                resultado = await chamada()
                excecao = None
            except Exception as e:  # noqa: BLE001
                resultado, excecao = None, e
    return resultado, excecao, eventos


async def _resolver_id_processo(id_processo: str, paj: str) -> tuple[str | None, list[dict], str]:
    """Resolve `id_processo` pelo NÚMERO do PAJ (`paj`) quando ele vem vazio — caso do
    rascunho criado via "Adicionar movimentação" (Movimentações Pendentes) enquanto o
    SISDPU estava fora do ar/inacessível: resolve AGORA, na hora de executar (quando o
    SISDPU presumivelmente já está de volta), pela busca global do SISDPU — mesmo
    mecanismo já usado em produção pra abrir um PAJ direto no navegador sem
    sincronizar (`services/paj_link_service.resolver`, com cache próprio em
    dados/_paj_urls.json). Sem id E sem número, não há como prosseguir de jeito nenhum.

    Retorna (id_processo_resolvido_ou_None, eventos_para_yield, detalhe_erro_se_falhou)
    — mesmo formato de retorno de `com_auto_reconexao` (tupla + eventos já prontos),
    pra quem chama só fazer `for ev in eventos: yield ev`."""
    id_processo = (id_processo or "").strip()
    if id_processo:
        return id_processo, [], ""
    if not paj:
        return None, [_ev("erro", "id_processo ausente.")], "id_processo ausente."
    eventos = [_ev("info", f"Buscando {paj} no SISDPU pelo número…")]
    from services import paj_link_service

    try:
        res_link = await paj_link_service.resolver(paj)
    except Exception as e:  # noqa: BLE001
        res_link = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
    if not res_link.get("ok"):
        erro_busca = res_link.get("erro") or "PAJ não localizado no SISDPU."
        eventos.append(_ev("erro", f"Não localizei o PAJ {paj} no SISDPU: {erro_busca}"))
        return None, eventos, erro_busca
    m_id_resolvido = re.search(r"[?&]id=(\d+)", res_link.get("url", "") or "")
    if not m_id_resolvido:
        eventos.append(_ev("erro", "PAJ localizado, mas não consegui extrair o id interno do SISDPU da URL."))
        return None, eventos, "id_processo não encontrado na URL resolvida."
    id_resolvido = m_id_resolvido.group(1)
    eventos.append(_ev("ok", f"PAJ localizado no SISDPU — id interno {id_resolvido}."))
    return id_resolvido, eventos, ""


async def executar_stream(
    id_processo: str,
    paj: str = "",
    paj_norm: str = "",
    fase: str = "",
    descricao_mov: str = "",
    descricao_mov_html: bool = False,
    grupo: str | None = None,
    descricao_tram: str = "",
    prazo_dias: int = 0,
    prazo_data: str = "",
    id_tramite: str | None = None,
    concluir: bool = False,
    id_tramites: list[str] | None = None,
    dry_run: bool = False,
    anexos: list[dict] | None = None,
    etapa: str = "",
) -> AsyncIterator[dict]:
    """Executa mov→tram→conclui (cada etapa opcional) em UM PAJ do SISDPU identificado
    por `id_processo`. Emite eventos SSE-like: {level,msg} de log; evento final
    {done: True, resultado: {...}}.

    Conclusão — DOIS modelos coexistem (compat):
    - `id_tramites` (NOVO — lista): 0, 1 ou N trâmites da caixa de entrada
      selecionados pelo usuário (checkboxes) para concluir de uma vez só, na MESMA
      sessão do Playwright que já fez a movimentação/tramitação (evita reabrir/logar
      de novo). Usado pela página do PAJ, que mostra todas as tramitações abertas.
    - `id_tramite` + `concluir` (LEGADO — singular): mantido para quem ainda não
      passa a lista completa (ex.: a Retornos, cujo trâmite não pertence a
      `tramitacoes_caixa` — ver ingestao.sincronizador._atualizar_tramitacoes_caixa).
      Só tem efeito quando ambos informados. Se `id_tramites` vier preenchido, ele
      tem PRECEDÊNCIA sobre o par legado.

    Movimentar/tramitar/concluir são AÇÕES INDEPENDENTES — nenhuma delas depende de
    outra ter sido selecionada; o "Executar" só fica bloqueado quando as TRÊS
    estiverem vazias (ver `concluir_efetivo` abaixo).

    `descricao_mov` e `descricao_tram` são independentes — o SISDPU aceita ambas
    (ou qualquer uma delas) em branco, então não há validação de obrigatoriedade aqui.
    `descricao_mov_html=True` quando `descricao_mov` é HTML de verdade (editor rico do
    módulo — Quill) em vez de texto puro (ex.: a Sugestão IA da Retornos): grava direto
    no CKEditor da movimentação em vez de escapar como texto. SÓ a movimentação suporta
    isso — a tramitação no SISDPU é um campo de texto puro, sem CKEditor.
    `anexos`: lista de {"origem": "paj"|"externo", "ref": "...", "descricao": "...",
    "tipo_arquivo": "..."} a incluir na MOVIMENTAÇÃO — descricao/tipo_arquivo são POR
    ANEXO (escolhidos na tela; tipo_arquivo é o catálogo real do SISDPU — "Não
    informado", "Ofício", "Correspondência" etc.) — requer `paj_norm` para resolver;
    ignorado se `fase` estiver vazio
    (nada a anexar sem movimentação). "paj": `ref` é o caminho RELATIVO de um documento
    já baixado ("Anexos SISDPU"). "externo": `ref` é o token devolvido por
    salvar_anexo_externo() (upload do usuário) — removido do disco automaticamente
    depois que a movimentação o usa com sucesso.

    2026-09-11: a lógica de UM item (mov→tram→conclui) foi extraída para
    `_executar_um_item_stream` — esta função virou um wrapper fino (resolve id_processo
    + reduz o par legado id_tramite/concluir a uma lista + delega) pra poder ser
    reaproveitada por `executar_fila_stream` (várias movimentações em sequência, MESMA
    sessão) sem duplicar a orquestração Playwright. Comportamento EXTERNO desta função
    (assinatura, eventos, resultado final) permanece idêntico ao de antes da extração."""
    id_resolvido, eventos_id, erro_id = await _resolver_id_processo(id_processo, paj)
    for ev in eventos_id:
        yield ev
    if not id_resolvido:
        yield {"done": True, "resultado": {"ok": False, "detalhe": erro_id}}
        return

    ids_concluir = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids_concluir and concluir and id_tramite:
        ids_concluir = [id_tramite]  # compat: par legado (Retornos)

    async for ev in _executar_um_item_stream(
        id_resolvido, paj, paj_norm, fase, descricao_mov, descricao_mov_html,
        grupo, descricao_tram, prazo_dias, prazo_data, ids_concluir, dry_run, anexos, etapa,
    ):
        if ev.get("done") and (ev.get("resultado") or {}).get("ok") and not dry_run:
            async for e2 in _pasta_para_lixeira_se_arquivado(paj_norm, [fase], ev["resultado"]):
                yield e2
        yield ev


async def _pasta_para_lixeira_se_arquivado(paj_norm: str, fases: list, resultado: dict) -> AsyncIterator[dict]:
    """Arquivamento gravado (fase "Arquivado…") → pasta do PAJ para a Lixeira do
    Windows, por ÚLTIMO (depois de movimentar/tramitar/concluir) — decisão do
    Defensor 11/10/2026, ver services/paj_lixeira_service. Marca o resultado com
    `paj_na_lixeira` para a tela não reler nem reabrir o PAJ (iria recriar a pasta)."""
    from services import paj_lixeira_service

    if not paj_norm or not any(paj_lixeira_service.fase_de_arquivamento(f) for f in fases):
        return
    r = await asyncio.to_thread(paj_lixeira_service.enviar_paj_para_lixeira, paj_norm)
    yield _ev("ok" if r.get("ok") else "erro", f"PAJ arquivado: {r.get('detalhe', '')}")
    if r.get("ok"):
        resultado["paj_na_lixeira"] = True


async def _executar_um_item_stream(
    id_processo: str,
    paj: str,
    paj_norm: str,
    fase: str,
    descricao_mov: str,
    descricao_mov_html: bool,
    grupo: str | None,
    descricao_tram: str,
    prazo_dias: int,
    prazo_data: str,
    ids_concluir: list[str],
    dry_run: bool,
    anexos: list[dict] | None,
    etapa: str = "",
) -> AsyncIterator[dict]:
    """UM ciclo mov→tram→conclui — corpo original de `executar_stream` (movido daqui
    2026-09-11 pra virar reutilizável por `executar_fila_stream`). `id_processo` já
    vem RESOLVIDO (não tenta buscar pelo número aqui) e a conclusão já vem reduzida a
    `ids_concluir` (lista simples — quem chama já decidiu entre o par legado e a lista
    nova). Mesmos eventos/resultado final de antes."""
    fase = (fase or "").strip()
    descricao_mov = (descricao_mov or "").strip()
    descricao_tram = (descricao_tram or "").strip()
    concluir_efetivo = bool(ids_concluir)
    etapa = (etapa or "").strip()
    if not fase and not grupo and not concluir_efetivo and not etapa:
        yield _ev("erro", "Selecione ao menos uma ação: movimentar, tramitar, etapa ou concluir.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Nenhuma ação selecionada."}}
        return

    # prazo: data explícita (calendário) tem precedência sobre dias
    prazo_data_final = None
    if grupo:
        if prazo_data:
            prazo_data_final = _iso_to_br(prazo_data)
        elif prazo_dias and int(prazo_dias) > 0:
            prazo_data_final = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")

    pajs = [{"id": id_processo, "numero": paj}]
    pref = "[dry-run] " if dry_run else ""
    label = paj or id_processo
    yield _ev("novo", f"{pref}Executando {label}…")

    # 1) MOVIMENTAR (opcional — carrega o despacho como descrição da movimentação)
    mv: dict = {}
    if fase:
        # Resolve os anexos (mistura de documentos já no PAJ + uploads externos) para
        # caminhos ABSOLUTOS. "paj" usa o mesmo helper seguro do download de arquivos
        # (paj_service.ler_arquivo — barra path traversal); "externo" usa o equivalente
        # pra pasta de uploads (_resolver_anexo_externo). Ignora silenciosamente
        # qualquer item que não resolva (arquivo removido/renomeado entre a listagem e
        # a execução).
        anexos_ctx: list[dict] = []
        refs_externos_usados: list[str] = []
        if anexos and paj_norm:
            for item in anexos:
                origem = (item or {}).get("origem") or "paj"
                ref = (item or {}).get("ref") or ""
                if not ref:
                    continue
                # Descrição/Tipo de Arquivo são escolhidos POR ANEXO na tela (dropdown
                # com o catálogo real do SISDPU, padrão "Não informado") — repassa como
                # veio; sem eles, movtram cai no nome do arquivo / "Documentos do
                # Assistido" (compatibilidade com quem não manda esses campos).
                descricao_item = ((item or {}).get("descricao") or "").strip()
                tipo_arquivo_item = ((item or {}).get("tipo_arquivo") or "").strip()
                if origem == "externo":
                    arq = _resolver_anexo_externo(paj_norm, ref)
                    if arq:
                        nome = arq.name.split("_", 1)[-1] if "_" in arq.name else arq.name
                        anexos_ctx.append({"nome": nome, "caminho": str(arq), "tipo": arq.suffix.lstrip(".").lower(),
                                            "descricao": descricao_item or nome, "tipo_arquivo": tipo_arquivo_item})
                        refs_externos_usados.append(ref)
                else:
                    arq, _ct = paj_service.ler_arquivo(paj_norm, ref)
                    if arq:
                        anexos_ctx.append({"nome": arq.name, "caminho": str(arq),
                                            "tipo": arq.suffix.lstrip(".").lower(),
                                            "descricao": descricao_item or arq.name, "tipo_arquivo": tipo_arquivo_item})
            if anexos_ctx:
                yield _ev("info", f"Anexando {len(anexos_ctx)} documento(s) à movimentação…")

        yield _ev("info", f"Movimentando (fase: {fase})…")
        mv, exc, eventos = await com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, descricao_mov, fase, anexos_ctx, "Não informado",
                                           dry_run, descricao_html=descricao_mov_html))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na movimentação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Verifique a conexão do SISDPU (selo/Reconectar) e tente de novo.")
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if mv.get("ok") else "erro", f"Movimentação: {mv.get('detalhe', '')}")
        if not mv.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
            return
        # Movimentação confirmada: os anexos externos já foram entregues ao SISDPU —
        # remove as cópias temporárias (se a movimentação tivesse falhado, ficariam
        # pra uma nova tentativa não exigir reenviar o arquivo).
        if not dry_run:
            for ref in refs_externos_usados:
                remover_anexo_externo(paj_norm, ref)

    # 2) TRAMITAR (opcional)
    tram_caixa = ""
    if grupo:
        prazo_txt = f"prazo até {prazo_data_final}" if prazo_data_final else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})…")
        tr, exc, eventos = await com_auto_reconexao(
            lambda: movtram.tramitar_paj(pajs, grupo=grupo, descricao=descricao_tram,
                                         prazo_data=prazo_data_final, dry_run=dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na tramitação ({type(exc).__name__}): {str(exc)[:200]}. "
                              "A movimentação já foi registrada; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        yield _ev("ok" if tr.get("ok") else "erro", f"Tramitação: {tr.get('detalhe', '')}")
        if not tr.get("ok"):
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": tr.get("detalhe", "")}}
            return
        tram_caixa = grupo

    # 2b) ETAPA DO PAJ (opcional) — movimentação "Etapa: …" própria, depois da
    # tramitação e antes da conclusão (desenho aprovado pelo Defensor 15/10/2026).
    if etapa:
        from services import etapa_paj_service as etapas
        if etapa not in {e[0] for e in etapas.ETAPAS}:
            yield _ev("erro", f"Etapa do PAJ desconhecida: {etapa}")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": "Etapa desconhecida."}}
            return
        rot = etapas.info(etapa)["rotulo"]
        yield _ev("info", f"Registrando a etapa do PAJ ({rot})…")
        et, exc, eventos = await com_auto_reconexao(
            lambda: movtram.movimentar_paj(pajs, etapas.descricao_movimentacao(etapa), etapas.FASE_ETAPA,
                                           None, "Não informado", dry_run, tipo_ficha="etapa"))
        for ev in eventos:
            yield ev
        if exc is not None or not (et or {}).get("ok"):
            det = str(exc)[:300] if exc is not None else (et or {}).get("detalhe", "")
            yield _ev("erro", f"Etapa do PAJ: {det}")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": det}}
            return
        yield _ev("ok", f"Etapa do PAJ: {et.get('detalhe', '')}")
        if et.get("mov_seq"):
            mv = {**mv, "mov_seq": et["mov_seq"]}

    # 3) CONCLUIR (opcional — só quando houver ao menos um id em ids_concluir)
    concluido = False
    ja_ausente = False
    ids_para_remover_caixa: list[str] = []  # concluídos agora OU já ausentes — em
    # ambos os casos o trâmite não está mais na caixa, então some do cache local
    # (tramitacoes_caixa) independente de quantos foram selecionados.
    if concluir_efetivo:
        label_concl = "o trâmite" if len(ids_concluir) == 1 else f"{len(ids_concluir)} trâmites"
        yield _ev("info", f"Concluindo {label_concl}…")
        cc, exc, eventos = await com_auto_reconexao(lambda: movtram.concluir_paj(ids_concluir, dry_run))
        for ev in eventos:
            yield ev
        if exc is not None:
            yield _ev("erro", f"Falha na conclusão ({type(exc).__name__}): {str(exc)[:200]}. "
                              "Mov./tramitação já podem ter sido registradas; verifique o PAJ.")
            yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": str(exc)[:300]}}
            return
        resultados_por_id = cc.get("resultados") or {}
        concluidos_agora = [i for i in ids_concluir if resultados_por_id.get(i, {}).get("concluido")]
        ja_ausentes_agora = [i for i in ids_concluir if resultados_por_id.get(i, {}).get("ja_ausente")]
        ids_para_remover_caixa = concluidos_agora + ja_ausentes_agora
        if cc.get("status") == "ja_ausente":
            # TODOS os ids selecionados já estavam fora da caixa antes desta execução.
            yield _ev("muted", f"Conclusão: {cc.get('detalhe', '')}")
            ja_ausente = True
        elif cc.get("status") == "parcial":
            # Alguns concluídos/já ausentes, outros não — não trava a execução (mov/tram
            # já valeram); o log já deixa claro o que não foi, e o que restou continua
            # na caixa pro usuário tentar de novo.
            yield _ev("erro", f"Conclusão parcial: {cc.get('detalhe', '')}")
            concluido = bool(concluidos_agora) and not dry_run
        else:
            yield _ev("ok" if cc.get("ok") else "erro", f"Conclusão: {cc.get('detalhe', '')}")
            if not cc.get("ok"):
                yield {"done": True, "resultado": {"ok": False, "status": "parcial", "detalhe": cc.get("detalhe", "")}}
                return
            concluido = not dry_run

    if ja_ausente:
        yield _ev("muted", f"{label} — o(s) trâmite(s) já não estava(m) na caixa antes desta "
                           "execução (provável conclusão manual anterior).")
        yield {"done": True, "resultado": {"ok": False, "status": "ja_ausente", "concluido": False,
                                           "tramitado_para": tram_caixa, "concluidos": ids_para_remover_caixa,
                                           "detalhe": "Trâmite(s) já estava(m) ausente(s) antes desta execução — "
                                                     "nada foi concluído agora."}}
        return

    yield _ev("ok", f"{label} — executado com sucesso.")
    yield {"done": True, "resultado": {"ok": True, "concluido": concluido,
                                       "tramitado_para": tram_caixa, "mov_seq": mv.get("mov_seq"),
                                       "concluidos": ids_para_remover_caixa}}


async def executar_fila_stream(
    id_processo: str,
    paj: str = "",
    paj_norm: str = "",
    itens: list[dict] | None = None,
    dry_run: bool = False,
) -> AsyncIterator[dict]:
    """Executa uma FILA de movimentações (cada uma: mov→tram→conclui) em SEQUÊNCIA,
    todas na MESMA sessão do SISDPU — resolve `id_processo` uma ÚNICA vez pra fila
    inteira (reaproveita `_resolver_id_processo`), depois chama
    `_executar_um_item_stream` uma vez por item, em ordem.

    Cada item de `itens` é um dict com as MESMAS chaves de `executar_stream` (exceto
    id_processo/paj/paj_norm, compartilhados por toda a fila): fase, descricao_mov,
    descricao_mov_html, grupo, descricao_tram, prazo, prazo_data, id_tramites, anexos.
    Fila é um modelo NOVO — só usa a lista de trâmites (`id_tramites`), sem o par
    legado id_tramite/concluir (não faz sentido pra vários itens de uma vez).

    PARA no primeiro item que falhar (não segue às cegas pros seguintes) — emite um
    evento {item_done: True, index, ok, resultado} depois de CADA item (sucesso ou
    falha), pra quem chama (a rota SSE) ir podando a fila persistida conforme cada um
    termina, e um evento final {done: True, resultado: {ok, itens_concluidos: [...]}}
    — `itens_concluidos` são os ÍNDICES (0-based) que realmente terminaram (sucesso OU
    'ja_ausente' — nesse caso não há mais nada a fazer com aquele item, então conta
    como resolvido mesmo sem um `ok` estrito); o que sobrar na fila do chamador deve
    ser só os índices FORA dessa lista."""
    itens = itens or []
    if not itens:
        yield _ev("erro", "Fila vazia — nada a executar.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Fila vazia.", "itens_concluidos": []}}
        return

    id_resolvido, eventos_id, erro_id = await _resolver_id_processo(id_processo, paj)
    for ev in eventos_id:
        yield ev
    if not id_resolvido:
        yield {"done": True, "resultado": {"ok": False, "detalhe": erro_id, "itens_concluidos": []}}
        return

    total = len(itens)
    concluidos: list[int] = []
    # Rastreia o último `mov_seq` visto entre os itens da fila — é o mesmo campo que
    # o resultado de `executar_stream` (execução única) usa pra sinalizar "uma
    # movimentação de fato foi registrada" (ver posExecucaoPaj() em app.js, que só
    # dispara a sincronização leve de Movimentações quando `resultado.mov_seq` é
    # truthy). O resultado FINAL da fila nunca tinha essa chave — a sincronização
    # leve e o reload nunca eram reaproveitados corretamente após uma fila (bug
    # relatado 2026-09-24: "atualizar movimentações" e refresh não rodavam).
    mov_seq_fila: int | None = None
    for i, item in enumerate(itens):
        item = item or {}
        yield _ev("novo", f"— Movimentação {i + 1}/{total} —")
        ids_concluir_item = [str(x).strip() for x in (item.get("id_tramites") or []) if str(x).strip()]
        resultado_item: dict = {}
        async for ev in _executar_um_item_stream(
            id_resolvido, paj, paj_norm,
            item.get("fase", ""), item.get("descricao_mov", ""), bool(item.get("descricao_mov_html")),
            item.get("grupo") or None, item.get("descricao_tram", ""),
            int(item.get("prazo") or 0), item.get("prazo_data", ""),
            ids_concluir_item, dry_run, item.get("anexos") or [], item.get("etapa") or "",
        ):
            if ev.get("done"):
                resultado_item = ev.get("resultado") or {}
            else:
                yield ev
        if resultado_item.get("mov_seq"):
            mov_seq_fila = resultado_item["mov_seq"]
        # 'ja_ausente' conta como "nada mais a fazer aqui" mesmo com ok=False — mesmo
        # critério usado no resto do módulo (não é uma falha real, o trâmite já tinha
        # saído da caixa antes desta execução).
        ok_item = bool(resultado_item.get("ok")) or resultado_item.get("status") == "ja_ausente"
        yield {"item_done": True, "index": i, "ok": ok_item, "resultado": resultado_item}
        if not ok_item:
            yield {"done": True, "resultado": {
                "ok": False,
                "detalhe": f"Movimentação {i + 1}/{total} falhou — execução da fila interrompida "
                           f"({resultado_item.get('detalhe', '')}).",
                "itens_concluidos": concluidos,
                "mov_seq": mov_seq_fila,
            }}
            return
        concluidos.append(i)

    yield _ev("ok", f"Fila concluída — {total}/{total} movimentação(ões) executada(s) com sucesso.")
    resultado_fila = {"ok": True, "itens_concluidos": concluidos, "mov_seq": mov_seq_fila}
    if not dry_run:
        async for e2 in _pasta_para_lixeira_se_arquivado(
                paj_norm, [(it or {}).get("fase", "") for it in itens], resultado_fila):
            yield e2
    yield {"done": True, "resultado": resultado_fila}


# ---------------------------------------------------------------------------
# "Gerar Síntese do PAJ" — botão ao lado de "Usar modelo" (descrição da
# movimentação): analisa o PROMPT_MAX do PAJ e gera, por IA, um texto corrido
# com a síntese cronológica da tramitação. Chamada one-shot ao Claude CLI —
# "motor simples" (mesmo padrão de services.intimacao_ia_service.
# _chamar_claude_texto / services.minuta_ia_service): prompt por STDIN,
# cwd fora de qualquer workspace de skills (o PROMPT_MAX já traz todo o fato
# relevante do caso embutido no próprio prompt — não precisa de acesso a
# CLAUDE.md/skills/bases pra essa tarefa de transformação de texto).
# ---------------------------------------------------------------------------

_SINTESE_PAJ_PROMPT = """Gere o tópico "SÍNTESE DA TRAMITAÇÃO DO PAJ": texto corrido, sem títulos internos, sem bullets, em ordem cronológica.

- 1º parágrafo: "Trata-se de pretensão de [benefício], requerida administrativamente por [assistido] (NB/DER se houver), em razão de [fato gerador, com data]. O INSS indeferiu o pedido sob o fundamento de que [motivo técnico do indeferimento]."
- Parágrafos seguintes: um por marco relevante (análise prévia, despacho, arquivamento, desarquivamento, documento decisivo), cada um iniciado por marcador temporal ("Em [data]..." / "Em despacho de [data] (mov. X)...") citando o nº da movimentação entre parênteses quando disponível.
- Ao mencionar documento/declaração nova, extrair o conteúdo concreto (datas, teor específico) — nunca resumir como "foram apresentados documentos".
- Não repetir fundamentação jurídica já exposta em parágrafo anterior; reportar só o fato/conclusão novo de cada marco.
- Não incluir frase de fechamento/transição para a análise atual — a síntese termina no último fato registrado no PAJ.
- Separar parágrafos com linha em branco. Tom técnico-direto, sem travessão."""


def _extrair_texto_puro(saida: str) -> str:
    """Remove cercas de código markdown, se o modelo insistir em envolver a
    resposta nelas apesar da instrução de responder só com o texto puro —
    mesma defesa de services.intimacao_ia_service._extrair_bloco_html."""
    texto = (saida or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)```\s*$", texto, re.DOTALL)
    return m.group(1).strip() if m else texto


async def _chamar_claude_texto_sintese(prompt: str, timeout: int = 180) -> tuple[str, str | None]:
    """Chamada one-shot ao Claude CLI — ver docstring da seção acima. Roda em
    thread (`asyncio.to_thread`) pra não bloquear o loop de eventos do FastAPI
    durante a chamada bloqueante. Retorna (texto, erro) — erro None em sucesso."""
    from services.chat_service import CLAUDE_CMD
    from services.peticao_ia_service import MODELOS_DIR

    cmd = [*CLAUDE_CMD, "-p", "--permission-mode", "bypassPermissions"]
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, cwd=str(MODELOS_DIR), input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"
    saida = (proc.stdout or "").strip()
    if not saida:
        erro_txt = (proc.stderr or "").strip()
        return "", f"Resposta vazia da IA.{(' ' + erro_txt[-500:]) if erro_txt else ''}"
    return saida, None


async def gerar_sintese_paj(paj_norm: str) -> dict:
    """Gera a "SÍNTESE DA TRAMITAÇÃO DO PAJ" por IA a partir do PROMPT_MAX do
    PAJ (cabeçalho/narrativa/movimentações já consolidados pelo painel) —
    botão "Gerar Síntese do PAJ" ao lado de "Usar modelo". O texto retornado é
    inserido no CURSOR do editor de descrição da movimentação pelo frontend
    (mesmo `insertHTML()` de usarModeloDespacho), sem apagar o que já
    estiver escrito."""
    pasta = PAJS_DIR / paj_norm
    if not pasta.exists():
        return {"ok": False, "detalhe": "PAJ não encontrado."}

    prompt_max_path = pasta / "PROMPT_MAX.md"
    if not prompt_max_path.exists():
        from services.prompt_builder import gerar_prompt_max

        with contextlib.suppress(Exception):
            gerar_prompt_max(paj_norm)
    prompt_max = ""
    if prompt_max_path.exists():
        with contextlib.suppress(Exception):
            prompt_max = prompt_max_path.read_text(encoding="utf-8", errors="replace")
    if not prompt_max.strip():
        return {"ok": False, "detalhe": "Não foi possível montar o contexto do PAJ (PROMPT_MAX)."}

    prompt = (
        "Contexto do PAJ (gerado pelo painel a partir do SISDPU):\n\n"
        "--- INICIO DO PROMPT_MAX ---\n"
        f"{prompt_max.strip()}\n"
        "--- FIM DO PROMPT_MAX ---\n\n"
        f"{_SINTESE_PAJ_PROMPT}\n\n"
        "Responda apenas com o texto solicitado — sem cercas de código markdown, "
        "sem comentários antes ou depois."
    )
    texto, erro = await _chamar_claude_texto_sintese(prompt)
    if erro:
        return {"ok": False, "detalhe": erro}
    return {"ok": True, "texto": _extrair_texto_puro(texto)}
