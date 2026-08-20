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

import contextlib
import json
import re
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
    padrão de `acoes_eproc_service.listar_rascunhos`."""
    if not RASCUNHOS_DIR.exists():
        return []
    itens = []
    for f in RASCUNHOS_DIR.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        doc["assistido"] = _assistido_do_paj(doc.get("paj_norm", ""))
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
    Retorna {ok, ref, nome, tamanho, tipo} ou {ok:False, detalhe}."""
    paj_norm = (paj_norm or "").strip()
    nome_original = (nome_original or "").strip()
    if not paj_norm or not nome_original:
        return {"ok": False, "detalhe": "paj_norm ou nome do arquivo ausente."}
    pasta_paj = PAJS_DIR / paj_norm
    if not pasta_paj.exists():
        return {"ok": False, "detalhe": "PAJ não encontrado localmente (sincronize antes)."}
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
    já o usou com sucesso (ver executar_stream)."""
    caminho = _resolver_anexo_externo(paj_norm, ref)
    if caminho:
        with contextlib.suppress(Exception):
            caminho.unlink()
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
    depois que a movimentação o usa com sucesso."""
    fase = (fase or "").strip()
    descricao_mov = (descricao_mov or "").strip()
    descricao_tram = (descricao_tram or "").strip()
    if not id_processo:
        yield _ev("erro", "id_processo ausente.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "id_processo ausente."}}
        return

    ids_concluir = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids_concluir and concluir and id_tramite:
        ids_concluir = [id_tramite]  # compat: par legado (Retornos)
    concluir_efetivo = bool(ids_concluir)
    if not fase and not grupo and not concluir_efetivo:
        yield _ev("erro", "Selecione ao menos uma ação: movimentar, tramitar ou concluir.")
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
