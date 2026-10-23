"""Gravações no SISDPU pelo MCP do SIS-DPU 2.0 — mesmo contrato do sisdpu_movtram.

`sisdpu_movtram.movimentar_paj` / `tramitar_paj` delegam para cá quando a fonte
de dados é "mcp" (Configurações › SISDPU › Fonte de dados). Mesma assinatura e
mesmo dict de retorno, para todas as rotinas que gravam (console de ações,
conjunta, SAT, dossiês, CRPS, RPV, mera ciência) seguirem iguais.

Cada gravação: monta os ids (fase, tipo, grupo) a partir dos NOMES que o
painel usa → envia os anexos → PREPARA (o servidor devolve a ficha) → espera a
aprovação do Defensor no modal padrão (services/aprovacao_mcp_service.py) →
CONFIRMA com o conteúdo idêntico. Sem aprovação, nada é gravado.

Limites do próprio servidor (vedações):
  - não existe concluir trâmite: `concluir_paj` continua no SISDPU legado;
  - só tramita para caixa de GRUPO da própria unidade (sem usuários);
  - a descrição nunca vai para o acompanhamento público.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import html
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

from ingestao import sisdpu2_mcp_client as sis2
from services import aprovacao_mcp_service as aprov

TIPO_MOV_PADRAO = "MOVIMENTAÇÃO MANUAL"
# O servidor recusa "conteudoBase64" acima de 400.000 caracteres por chamada
# (14/10/2026); 288 KB viram 393.216 caracteres em base64.
_PEDACO_BYTES = 288 * 1024

# Dry-run com coleta: o resumo da movimentação conjunta roda a ação inteira
# sem gravar e recolhe aqui as fichas, para mostrá-las ANTES do e-proc.
COLETA_FICHAS: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("coleta_fichas", default=None)

_catalogos: dict[tuple, tuple[float, object]] = {}  # (ferramenta, idProcesso) -> (quando, resposta)
_CATALOGO_VALIDADE_SEG = 4 * 3600


def _catalogo(nome: str, args: dict):
    """movimentacao_opcoes / tramite_destinos: listas de opções que mudam
    raramente — reaproveitadas por 4 h em vez de pedidas a cada gravação/resumo."""
    chave = (nome, args.get("idProcesso"))
    feito = _catalogos.get(chave)
    if feito and time.time() - feito[0] < _CATALOGO_VALIDADE_SEG:
        return feito[1]
    r = sis2.chamar(nome, args)
    _catalogos[chave] = (time.time(), r)
    return r


_envios: dict[tuple, tuple[float, list[int]]] = {}  # (caminho, tamanho, mtime) -> (quando, ids)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[°º;]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")


def _casar(nome: str, opcoes: list[dict], campo: str = "descricao") -> Optional[dict]:
    """Nome usado no painel -> opção do SIS 2 (exato, depois contido)."""
    alvo = _norm(nome)
    if not alvo:
        return None
    for o in opcoes:
        if _norm(o.get(campo, "")) == alvo:
            return o
    candidatos = [o for o in opcoes if alvo in _norm(o.get(campo, "")) or _norm(o.get(campo, "")) in alvo]
    return candidatos[0] if len(candidatos) == 1 else None


async def _id_processo(paj: dict) -> int:
    if str(paj.get("id") or "").isdigit():
        return int(paj["id"])
    r = await asyncio.to_thread(sis2.chamar, "processo_resolver", {"numero": paj.get("numero")})
    if not isinstance(r, dict) or not r.get("idProcesso"):
        raise RuntimeError(f"PAJ {paj.get('numero')} não encontrado no SIS-DPU 2.0")
    return int(r["idProcesso"])


MIN_DESCRICAO = 10  # o SIS-DPU 2.0 recusa descrição de movimentação mais curta
# O SIS-DPU 2.0 exige a descrição da tramitação (o legado aceitava em branco):
# em branco, vai só um ponto — decisão do Defensor (11/10/2026), aparece na ficha.
DESCRICAO_TRAMITACAO_VAZIA = "."


def _texto_da_descricao(descricao: str, descricao_html: bool) -> str:
    if not descricao_html:
        return (descricao or "").strip()
    return html.unescape(re.sub(r"<[^>]+>", "", descricao or "")).replace("\xa0", " ").strip()


def aviso_descricao_curta(descricao: str, descricao_html: bool) -> str:
    """Mensagem para o Defensor quando a descrição não chega ao mínimo do SIS-DPU
    2.0 — o painel nunca completa nem troca o texto digitado."""
    n = len(_texto_da_descricao(descricao, descricao_html))
    if n >= MIN_DESCRICAO:
        return ""
    return (f"A descrição da movimentação tem {n} caractere(s); o SIS-DPU 2.0 exige no mínimo "
            f"{MIN_DESCRICAO}. Complete o texto da descrição.")


def _html_da_descricao(descricao: str, descricao_html: bool) -> str:
    """Vai ao SIS-DPU 2.0 exatamente o texto do Defensor (HTML do editor como está)."""
    if descricao_html:
        return descricao
    return "<p>" + html.escape(descricao.strip()).replace("\n", "<br>") + "</p>"


def _enviar_anexo(caminho: Path, descricao: str) -> list[int]:
    """Envia um arquivo em pedaços e devolve os ids (o servidor divide em
    anexos de até 9 MB). Reaproveita o envio recente do MESMO arquivo — o
    resumo da conjunta e a gravação real veem os mesmos ids na ficha."""
    st = caminho.stat()
    chave = (str(caminho), st.st_size, st.st_mtime)
    feito = _envios.get(chave)
    if feito and time.time() - feito[0] < 3600:
        return feito[1]
    dados = caminho.read_bytes()
    pedacos = [dados[i:i + _PEDACO_BYTES] for i in range(0, len(dados), _PEDACO_BYTES)] or [b""]
    args = {"nome": caminho.name, "descricao": descricao or caminho.stem}
    if len(pedacos) == 1:
        r = sis2.chamar_escrita("movimentacao_anexo_guardar",
                                {**args, "conteudoBase64": base64.b64encode(pedacos[0]).decode()})
    else:
        chave_envio = None
        for n, p in enumerate(pedacos, start=1):
            a = {**args, "conteudoBase64": base64.b64encode(p).decode(), "parte": n, "totalPartes": len(pedacos)}
            if chave_envio:
                a["chaveEnvio"] = chave_envio
            r = sis2.chamar_escrita("movimentacao_anexo_guardar", a)
            chave_envio = chave_envio or (r or {}).get("chaveEnvio")
    ids = [int(i) for i in (r or {}).get("idsArquivos") or []] or (
        [int(r["idArquivo"])] if isinstance(r, dict) and r.get("idArquivo") else [])
    if not ids:
        raise RuntimeError(f"o SIS-DPU 2.0 não devolveu id para o anexo {caminho.name}: {str(r)[:200]}")
    _envios[chave] = (time.time(), ids)
    return ids


async def _aprovar(tipo: str, paj_num: str, ficha: str, contexto: dict) -> Optional[str]:
    coleta = COLETA_FICHAS.get()
    if coleta is not None:
        coleta.append({"tipo": tipo, "paj": paj_num, "ficha": aprov.ficha_para_exibir(ficha),
                       "contexto": contexto})
        return None
    return await aprov.solicitar(tipo, paj_num, ficha, contexto)


def _invalidar_caches(id_processo: int) -> None:
    """Depois de gravar: o painel do PAJ e a caixa voltam a ler do SIS-DPU 2.0."""
    from ingestao import sisdpu2_fonte
    sisdpu2_fonte.invalidar_tramites(id_processo)
    sisdpu2_fonte.invalidar_cache_caixa()


async def _gravar_chave(vinc: dict, chave: str) -> dict:
    """Grava a chave de consulta no campo "Observações (Chave)" do vínculo
    (liberado no MCP em 20/10/2026 — antes só entrava pela tela, pelo
    cartório). A chave aparece no cartão do cadastro na revisão unificada do
    pós-ajuizamento (peticao_inicial_modal.html), e a confirmação do Defensor
    ali vale para ela — é dado do cadastro, não movimenta o PAJ. Nunca
    sobrescreve o que já estiver no campo."""
    chave = (chave or "").strip()
    if not chave:
        return {"chave_gravada": False, "chave_detalhe": "Chave de consulta não informada pelo e-proc."}
    atual = str(vinc.get("observacao") or "").strip()
    if atual:
        if re.sub(r"\s", "", atual) == re.sub(r"\s", "", chave):
            return {"chave_gravada": True, "chave_detalhe": "Chave de consulta já estava cadastrada no PAJ."}
        return {"chave_gravada": False,
                "chave_detalhe": f"O campo da chave já tem outro conteúdo ({atual[:60]}) — não sobrescrevi; confira."}
    args = {"idVinculo": int(vinc["id"]), "chave": chave}
    ficha = await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_observacao_preparar", args)
    ficha_txt = ficha if isinstance(ficha, str) else str(ficha)
    token = re.search(r"Token de confirma\w*:\s*([0-9a-f]+)", ficha_txt, re.I)
    if not token:
        return {"chave_gravada": False,
                "chave_detalhe": f"O SIS-DPU 2.0 recusou a chave: {aprov.ficha_para_exibir(ficha_txt)[:300]}"}
    await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_observacao_confirmar",
                            {**args, "token": token.group(1), "confirmacao": aprov.RESPOSTA_CLIQUE})
    return {"chave_gravada": True, "chave_detalhe": f"Chave de consulta {chave} gravada no PAJ."}


async def cadastrar_processo_judicial(paj_num: str, numero: str, instancia: int = 1, chave: str = "") -> dict:
    """Depois da petição inicial (pedido do Defensor, 13/10/2026): vincula o
    processo judicial ao PAJ e sincroniza com o tribunal — o SIS-DPU 2.0 grava
    número, órgão julgador (juízo), autuação, classe, valor e partes. Cada passo
    com a SUA ficha no modal de confirmação. Se o processo já estiver vinculado,
    só sincroniza. Com `chave`, grava também a chave de consulta no campo
    "Observações (Chave)" do vínculo (20/10/2026 — `_gravar_chave`), o que antes
    o cartório fazia à mão pela tela."""
    base = {"fonte": "mcp-sisdpu2", "cadastrado": False, "sincronizado": False}
    digitos = re.sub(r"\D", "", numero or "")
    if len(digitos) != 20:
        return {**base, "ok": False, "detalhe": f"Número de processo inválido: {numero!r}"}
    try:
        idp = await _id_processo({"numero": paj_num})

        async def _vinculo():
            vs = await asyncio.to_thread(sis2.chamar, "paj_processos_judiciais", {"idProcesso": idp})
            return next((v for v in (vs or []) if re.sub(r"\D", "", str(v.get("numeroProcessoJudicial"))) == digitos), None)

        vinc = await _vinculo()
        if not vinc:
            args = {"idProcesso": idp, "numero": numero, "instancia": int(instancia)}
            ficha = await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_cadastrar_preparar", args)
            ficha_txt = ficha if isinstance(ficha, str) else str(ficha)
            token = re.search(r"Token de confirma\w*:\s*([0-9a-f]+)", ficha_txt, re.I)
            if not token:
                return {**base, "ok": False,
                        "detalhe": f"O SIS-DPU 2.0 recusou o cadastro: {aprov.ficha_para_exibir(ficha_txt)[:600]}"}
            resposta = await _aprovar("processo_judicial", paj_num, ficha_txt, {})
            if COLETA_FICHAS.get() is not None:  # resumo (dry-run): só a ficha do cadastro
                return {**base, "ok": True, "status": "dry_run", "detalhe": "Ficha do cadastro preparada (nada gravado)."}
            if not resposta:
                return {**base, "ok": False, "detalhe": "Cadastro do processo judicial NÃO confirmado pelo Defensor."}
            await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_cadastrar_confirmar",
                                    {**args, "token": token.group(1), "confirmacao": resposta})
            base["cadastrado"] = True
            vinc = await _vinculo()
            if not vinc:
                return {**base, "ok": False, "detalhe": "Cadastro confirmado, mas o vínculo não apareceu no PAJ — confira."}

        if COLETA_FICHAS.get() is not None:  # resumo (dry-run): já vinculado, nada a mostrar
            return {**base, "ok": True, "status": "dry_run", "ja_vinculado": True,
                    "detalhe": "Processo já vinculado ao PAJ — só será sincronizado."}
        ficha = await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_sincronizar_preparar",
                                        {"idVinculo": int(vinc["id"])})
        ficha_txt = ficha if isinstance(ficha, str) else str(ficha)
        token = re.search(r"Token de confirma\w*:\s*([0-9a-f]+)", ficha_txt, re.I)
        if not token:
            return {**base, "ok": False,
                    "detalhe": f"O SIS-DPU 2.0 recusou a sincronização: {aprov.ficha_para_exibir(ficha_txt)[:600]}"}
        # Sincronização = mera atualização dos dados do processo vindos do
        # tribunal: autorizada SEMPRE, sem modal — a confirmação do cadastro
        # vale para ela (decisão do Defensor, 16/10/2026).
        resposta = aprov.RESPOSTA_CLIQUE
        r = await asyncio.to_thread(sis2.chamar_escrita, "processo_judicial_sincronizar_confirmar",
                                    {"idVinculo": int(vinc["id"]), "token": token.group(1), "confirmacao": resposta})
        vinc = await _vinculo() or vinc
        try:
            base.update(await _gravar_chave(vinc, chave))
        except Exception as e:  # noqa: BLE001 — a chave não derruba o cadastro já feito
            base.update({"chave_gravada": False, "chave_detalhe": f"Falha ao gravar a chave: {type(e).__name__}: {e}"[:300]})
        _invalidar_caches(idp)
        msg = (r or {}).get("mensagem") if isinstance(r, dict) else str(r)
        return {**base, "ok": True, "sincronizado": bool((r or {}).get("sincronizado", True)) if isinstance(r, dict) else True,
                "juizo": vinc.get("juizo"), "id_vinculo": vinc.get("id"),
                "detalhe": msg or "Processo judicial sincronizado com o tribunal."}
    except sis2.Sisdpu2LoginNecessario:
        return {**base, "ok": False, "detalhe": "Sessão do SIS-DPU 2.0 encerrada — reconecte e repita o cadastro do processo."}
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "detalhe": f"{type(e).__name__}: {e}"[:400]}


async def _total_movimentacoes(id_processo: int) -> Optional[int]:
    try:
        capa = await asyncio.to_thread(sis2.chamar, "paj_capa", {"idProcesso": id_processo})
        return int(capa.get("totalDeMovimentacoes")) if isinstance(capa, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def movimentar_paj(pajs: list[dict], descricao: str, fase: str, anexos: Optional[list[dict]] = None,
                         tipo_arquivo: str = "Documentos do Assistido", dry_run: bool = False,
                         descricao_html: bool = False, log: Optional[Callable] = None,
                         tipo_ficha: str = "movimentacao", **_kw) -> dict:
    numeros = [p.get("numero") for p in pajs]
    if len(pajs) != 1:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": "O SIS-DPU 2.0 grava UM PAJ por vez (uma ficha e uma aprovação por PAJ)."}
    aviso = aviso_descricao_curta(descricao, descricao_html)
    if aviso:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros, "detalhe": aviso}
    paj = pajs[0]
    try:
        idp = await _id_processo(paj)
        op = await asyncio.to_thread(_catalogo, "movimentacao_opcoes", {"idProcesso": idp})
        tipo = _casar(TIPO_MOV_PADRAO, op.get("tiposMovimentacao") or [])
        fase_o = _casar(fase, (op.get("todasFases") or []) + (op.get("fasesMaisUtilizadas") or []))
        if not tipo:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"Tipo '{TIPO_MOV_PADRAO}' não disponível no SIS-DPU 2.0 para este PAJ."}
        if not fase_o:
            nomes = [f.get("descricao") for f in op.get("todasFases") or []]
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"Fase '{fase}' não encontrada no SIS-DPU 2.0 (opções={nomes})"}
        ids_arquivos, nomes_anexos, anexos_ficha = [], [], []
        for a in anexos or []:
            caminho = Path(a.get("caminho") or "")
            if not caminho.is_file():
                continue
            ids_arquivos += await asyncio.to_thread(_enviar_anexo, caminho, a.get("descricao") or "")
            nomes_anexos.append(a.get("nome") or caminho.name)
            # para a tabela de anexos do modal (descrição dada pelo Defensor a cada um)
            anexos_ficha.append({"nome": caminho.name, "descricao": a.get("descricao") or "",
                                 "bytes": caminho.stat().st_size})
        args = {"idProcesso": idp, "idTipoMovimentacao": int(tipo["id"]), "idsFases": [int(fase_o["id"])],
                "descricao": _html_da_descricao(descricao, descricao_html)}
        if ids_arquivos:
            args["idsArquivos"] = ids_arquivos
        ficha = await asyncio.to_thread(sis2.chamar_escrita, "movimentacao_preparar", args)
        ficha_txt = ficha if isinstance(ficha, str) else str(ficha)
        token = re.search(r"Token de confirma\w*:\s*([0-9a-f]+)", ficha_txt, re.I)
        if not token:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"O SIS-DPU 2.0 recusou o preparo: {aprov.ficha_para_exibir(ficha_txt)[:600]}"}
        if dry_run and COLETA_FICHAS.get() is None:
            return {"fonte": "mcp-sisdpu2", "ok": True, "status": "dry_run", "pajs": numeros, "anexos": nomes_anexos,
                    "detalhe": "Ficha conferida pelo SIS-DPU 2.0 (nada gravado).",
                    "ficha": aprov.ficha_para_exibir(ficha_txt)}
        resposta = await _aprovar(tipo_ficha, paj.get("numero") or "", ficha_txt,
                                  {"fase": fase, "anexos": anexos_ficha})
        if COLETA_FICHAS.get() is not None:
            return {"fonte": "mcp-sisdpu2", "ok": True, "status": "dry_run", "pajs": numeros, "anexos": nomes_anexos,
                    "detalhe": "Ficha preparada para o resumo (nada gravado)."}
        if not resposta:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": "Movimentação NÃO gravada: a ficha não foi confirmada pelo Defensor."}
        r = await asyncio.to_thread(sis2.chamar_escrita, "movimentacao_confirmar",
                                    {**args, "token": token.group(1), "confirmacao": resposta})
        if not isinstance(r, dict) or not r.get("idsGerados"):
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"Resultado inesperado do SIS-DPU 2.0 — CONFIRA no PAJ antes de repetir: {str(r)[:300]}"}
        _invalidar_caches(idp)
        seq = await _total_movimentacoes(idp)
        return {"fonte": "mcp-sisdpu2", "ok": True, "status": "movimentado", "pajs": numeros, "anexos": nomes_anexos, "mov_seq": seq,
                "detalhe": "Movimentação gravada no SIS-DPU 2.0" + (f" · Mov {seq}" if seq else "")}
    except sis2.Sisdpu2LoginNecessario:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": "Painel não conectado ao SIS-DPU 2.0 — conecte em Configurações › SISDPU › Fonte de dados."}
    except Exception as e:  # noqa: BLE001
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": f"SIS-DPU 2.0: {e}" + ("" if "NADA foi gravado" in str(e) else " — CONFIRA no PAJ antes de repetir.")}


def _iso(data_br: Optional[str]) -> Optional[str]:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", data_br or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


async def tramitar_paj(pajs: list[dict], grupo: Optional[str] = None, usuarios: Optional[list] = None,
                       descricao: Optional[str] = None, prazo_data: Optional[str] = None,
                       prazo_desc: Optional[str] = None, dry_run: bool = False, **_kw) -> dict:
    numeros = [p.get("numero") for p in pajs]
    if len(pajs) != 1:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": "O SIS-DPU 2.0 tramita UM PAJ por vez."}
    if usuarios:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": "O SIS-DPU 2.0 não tramita para usuários, só para caixa de grupo da unidade."}
    paj = pajs[0]
    try:
        idp = await _id_processo(paj)
        destinos = await asyncio.to_thread(_catalogo, "tramite_destinos", {})
        lista = destinos if isinstance(destinos, list) else (destinos or {}).get("itens") or (destinos or {}).get("grupos") or []
        campo = "descricao" if lista and "descricao" in lista[0] else "nome"
        g = _casar(grupo or "", lista, campo)
        if not g:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"Caixa '{grupo}' não encontrada entre os destinos do SIS-DPU 2.0 "
                               f"(opções={[x.get(campo) for x in lista]})"}
        args = {"idProcesso": idp, "idGrupoDestino": int(g.get("id") or g.get("idGrupo")),
                "descricaoTramite": (descricao or "").strip() or DESCRICAO_TRAMITACAO_VAZIA}
        if _iso(prazo_data):
            args["dataPrazo"] = _iso(prazo_data)
            if prazo_desc:
                args["descricaoPrazo"] = prazo_desc
        ficha = await asyncio.to_thread(sis2.chamar_escrita, "tramite_interno_preparar", args)
        ficha_txt = ficha if isinstance(ficha, str) else str(ficha)
        token = re.search(r"Token de confirma\w*:\s*([0-9a-f]+)", ficha_txt, re.I)
        if not token:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": f"O SIS-DPU 2.0 recusou o preparo: {aprov.ficha_para_exibir(ficha_txt)[:600]}"}
        if dry_run and COLETA_FICHAS.get() is None:
            return {"fonte": "mcp-sisdpu2", "ok": True, "status": "dry_run", "pajs": numeros,
                    "detalhe": "Ficha conferida pelo SIS-DPU 2.0 (nada gravado).",
                    "ficha": aprov.ficha_para_exibir(ficha_txt)}
        resposta = await _aprovar("tramitacao", paj.get("numero") or "", ficha_txt, {"grupo": grupo})
        if COLETA_FICHAS.get() is not None:
            return {"fonte": "mcp-sisdpu2", "ok": True, "status": "dry_run", "pajs": numeros,
                    "detalhe": "Ficha preparada para o resumo (nada gravado)."}
        if not resposta:
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                    "detalhe": "Tramitação NÃO gravada: a ficha não foi confirmada pelo Defensor."}
        r = await asyncio.to_thread(sis2.chamar_escrita, "tramite_interno_confirmar",
                                    {**args, "token": token.group(1), "confirmacao": resposta})
        if isinstance(r, str) and re.search(r"recusad|erro|falh", r, re.I):
            return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros, "detalhe": f"SIS-DPU 2.0: {r[:300]}"}
        _invalidar_caches(idp)
        return {"fonte": "mcp-sisdpu2", "ok": True, "status": "tramitado", "pajs": numeros,
                "detalhe": f"Tramitado no SIS-DPU 2.0 para {g.get(campo)}"}
    except sis2.Sisdpu2LoginNecessario:
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": "Painel não conectado ao SIS-DPU 2.0 — conecte em Configurações › SISDPU › Fonte de dados."}
    except Exception as e:  # noqa: BLE001
        return {"fonte": "mcp-sisdpu2", "ok": False, "status": "falha", "pajs": numeros,
                "detalhe": f"SIS-DPU 2.0: {e}" + ("" if "NADA foi gravado" in str(e) else " — CONFIRA no PAJ antes de repetir.")}


async def listar_grupos_destino() -> list[str]:
    """Nomes das caixas de grupo para tramitação (mesma lista que `tramitar_paj`
    casa pelo nome) — catálogo `tramite_destinos`, guardado por 4 h."""
    destinos = await asyncio.to_thread(_catalogo, "tramite_destinos", {})
    lista = destinos if isinstance(destinos, list) else (destinos or {}).get("itens") or (destinos or {}).get("grupos") or []
    if not lista:
        return []
    campo = "descricao" if "descricao" in lista[0] else "nome"
    return [str(g.get(campo)).strip() for g in lista if str(g.get(campo) or "").strip()]


def ativo() -> bool:
    from ingestao import sisdpu2_fonte
    return sisdpu2_fonte.fonte_mcp_ativa()
