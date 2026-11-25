"""Relatório de Ações Judiciais Anteriores do assistido (Ações com IA › Consultas).

Fluxo (pedido do usuário, 2026-11-10):
  1. `consultar(paj_norm)`: busca no e-proc, pelo MCP, os processos em que o CPF do
     assistido aparece — 1ª instância (JFPR) e 2ª instância (TRF4) — e lê o cabeçalho
     de cada um (classe, competência, autuação, situação, juízo, magistrado, valor da
     causa, partes com representantes). Devolve o HTML do relatório para a PRÉVIA
     (modal de conferência). Nada é gravado.
  2. `movimentar(paj_norm, html)`: grava no SISDPU uma movimentação "Juntada de
     documento" com a descrição em HTML — sem PDF anexo — e passa pela ficha de
     aprovação padrão do SIS-DPU 2.0 (services/aprovacao_mcp_service.py).

Layout: o do relatório aprovado na sessão "MCP eproc" (uma informação por linha, uma
parte por linha). O assunto vem do quadro "Assuntos" da capa (parsers._parse_assuntos
do MCP, com o principal marcado); tutela não é exposta pela capa. O número de cada processo é um link para a consulta pública do
e-proc (funciona para qualquer servidor no SISDPU); dentro do painel, o clique abre o
minimenu "Consultar processo (MCP) / Abrir no navegador" (static/js/app.js).
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from services.dossie_sd_service import _dados_assistido

TITULO = "Relatório de Ações Judiciais do Assistido"
INSTANCIAS = {1: "1ª instância (JFPR)", 2: "2ª instância (TRF4)"}
_BASE_PUBLICA = {
    1: "https://eproc.jfpr.jus.br/eprocV2/",
    2: "https://eproc.trf4.jus.br/eproc2trf4/",
}


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _fmt_npu(num: str) -> str:
    d = _so_digitos(num)
    if len(d) != 20:
        return num or ""
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def link_publico(numero: str, instancia: int) -> str:
    """Consulta pública do e-proc (não depende de sessão/hash do usuário)."""
    return (f"{_BASE_PUBLICA.get(instancia, _BASE_PUBLICA[1])}externo_controlador.php"
            f"?acao=processo_seleciona_publica&num_processo={_so_digitos(numero)}")


def _data(autuacao: str) -> str:
    """'16/07/2015 13:48:50' → '16/07/2015'."""
    m = re.match(r"\d{2}/\d{2}/\d{4}", autuacao or "")
    return m.group(0) if m else (autuacao or "")


def _ordem_data(autuacao: str) -> tuple:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", autuacao or "")
    return (m.group(3), m.group(2), m.group(1)) if m else ("", "", "")


async def consultar(paj_norm: str) -> dict:
    """Consulta o e-proc pelo CPF do assistido do PAJ. Retorna
    {ok, n, html, processos, erros, assistido, cpf} — `erros` lista as instâncias em
    que a busca falhou (o relatório fica incompleto e a gravação é bloqueada na tela)."""
    from ingestao import eproc_mcp_client as eproc

    cpf, nome, paj_label = _dados_assistido(paj_norm)
    if len(_so_digitos(cpf)) != 11:
        return {"ok": False, "erro": "O PAJ não tem o CPF do assistido sincronizado."}
    srv = eproc._mcp_server()
    processos: list[dict] = []
    erros: list[str] = []
    vistos: set[str] = set()
    for inst in (1, 2):
        try:
            r = await srv.pesquisar_processos(instancia=inst, cpf=cpf)
        except Exception as e:  # noqa: BLE001
            r = {"erro": f"{type(e).__name__}: {e}"}
        if r.get("erro"):
            erros.append(f"{INSTANCIAS[inst]}: {r['erro']}")
            continue
        for item in r.get("resultados") or []:
            num = _so_digitos(item.get("numero", ""))
            if not num or num in vistos:
                continue
            vistos.add(num)
            cab: dict = {}
            try:
                cab = await eproc.cabecalho_completo(num, instancia=inst) or {}
            except Exception as e:  # noqa: BLE001
                cab = {"_erro": f"{type(e).__name__}: {e}"}
            processos.append(_montar_processo(item, cab, inst))
    processos.sort(key=lambda p: _ordem_data(p["autuacao"]), reverse=True)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    return {
        "ok": True, "n": len(processos), "processos": processos, "erros": erros,
        "assistido": nome, "cpf": cpf, "paj": paj_label,
        "html": montar_html(nome, cpf, processos, agora),
    }


# Papéis auxiliares que o e-proc lista junto com as partes — fora do relatório (o
# aprovado mostra só os polos: autor/réu, recorrente/recorrido, exequente…).
_PAPEIS_AUXILIARES = ("PERITO", "UNIDADE EXTERNA", "AGÊNCIA DA PREVIDÊNCIA", "AGENCIA DA PREVIDENCIA",
                      "MPF", "MINISTÉRIO PÚBLICO", "ASSISTENTE TÉCNICO", "LEILOEIRO", "TESTEMUNHA",
                      "TRADUTOR", "INTÉRPRETE", "CONTADORIA")


def _auxiliar(tipo: str) -> bool:
    t = (tipo or "").strip().upper()
    return any(t.startswith(a) for a in _PAPEIS_AUXILIARES)


def _montar_processo(item: dict, cab: dict, inst: int) -> dict:
    partes = []
    for p in cab.get("partes") or []:
        if _auxiliar(p.get("tipo", "")):
            continue
        reps = [r.get("nome", "") for r in (p.get("representantes") or []) if r.get("nome")]
        partes.append({"tipo": p.get("tipo", ""), "nome": p.get("nome", ""),
                       "proc": "; ".join(reps)})
    if not partes:  # cabeçalho indisponível: usa autor/réu da própria busca
        if item.get("autor"):
            partes.append({"tipo": "AUTOR", "nome": item["autor"], "proc": ""})
        if item.get("reu"):
            partes.append({"tipo": "RÉU", "nome": item["reu"], "proc": ""})
    numero = cab.get("numero") or _fmt_npu(item.get("numero", ""))
    return {
        "numero": numero,
        "instancia": inst,
        "instancia_rotulo": INSTANCIAS[inst],
        "link": link_publico(numero, inst),
        "situacao": cab.get("situacao", ""),
        "classe": cab.get("classe", ""),
        "assunto": "; ".join(
            a.get("descricao", "")
            + (f" – {a['tipo_acao']}" if a.get("tipo_acao") else "")
            + (" (principal)" if a.get("principal") else "")
            for a in (cab.get("assuntos") or []) if a.get("descricao")),
        "competencia": cab.get("competencia", ""),
        "autuacao": _data(cab.get("autuacao") or item.get("autuacao", "")),
        "juizo": cab.get("orgao_julgador") or cab.get("orgao_colegiado") or item.get("juizo", ""),
        "magistrado_rotulo": "Relator(a)" if cab.get("relator") else "Juiz(a)",
        "magistrado": cab.get("relator") or cab.get("juiz") or "",
        "valor": cab.get("valor", ""),
        "partes": partes,
        "aviso": cab.get("_erro", ""),
    }


def _linha(rotulo: str, valor: str) -> str:
    if not valor:
        return ""
    return f"<strong>{html.escape(rotulo)}:</strong> {html.escape(valor)}<br>"


_P = '<p style="margin:0 0 10px 0">'


def montar_html(nome: str, cpf: str, processos: list[dict], agora: str) -> str:
    """HTML da descrição da movimentação (o mesmo da prévia)."""
    out = [f"{_P}<strong>CONSULTA DE PROCESSOS DO ASSISTIDO</strong></p>",
           _P +
           f"<strong>Assistido:</strong> {html.escape(nome or '—')}<br>"
           f"<strong>CPF:</strong> {html.escape(cpf)}<br>"
           f"<strong>Instâncias consultadas:</strong> {INSTANCIAS[1]} e {INSTANCIAS[2]}<br>"
           f"<strong>Consulta em:</strong> {agora}"
           "</p>",
           f"{_P}<strong>RESULTADO:</strong> {html.escape(resultado(len(processos)))}</p>"]
    for i, p in enumerate(processos, start=1):
        cab = (f'<strong>{i}. <a href="{html.escape(p["link"])}" target="_blank" '
               f'class="processo-eproc" data-numero="{html.escape(p["numero"])}">'
               f'{html.escape(p["numero"])}</a></strong> ({html.escape(p["instancia_rotulo"])})')
        if p["situacao"]:
            cab += f" — <strong>{html.escape(p['situacao'])}</strong>"
        corpo = (_linha("Classe", p["classe"]) + _linha("Assunto", p.get("assunto", ""))
                 + _linha("Autuação", p["autuacao"])
                 + _linha("Juízo", p["juizo"]) + _linha(p["magistrado_rotulo"], p["magistrado"])
                 + _linha("Competência", p["competencia"]) + _linha("Valor da causa", p["valor"]))
        partes = "".join(
            f"{html.escape(pt['tipo'])}: {html.escape(pt['nome'])}"
            + (f" (proc.: {html.escape(pt['proc'])})" if pt.get("proc") else "") + "<br>"
            for pt in p["partes"])
        if partes:
            corpo += "<strong>Partes:</strong><br>" + partes
        if p.get("aviso"):
            corpo += f"<em>Cabeçalho do processo indisponível: {html.escape(p['aviso'])}</em><br>"
        # Linha separadora acima de cada processo (border-top; se o SISDPU descartar o
        # estilo, o bloco continua separado pela margem do parágrafo).
        out.append(f'<p style="margin:0 0 10px 0;border-top:1px solid #888;padding-top:10px">'
                   f"{cab}<br>{corpo.removesuffix('<br>')}</p>")
    out.append(_P + "<em>Relatório gerado automaticamente pelo Painel SISDPU a partir de consulta "
               "ao e-proc (JFPR e TRF4) pelo CPF do assistido.</em></p>")
    return "".join(out)


def resultado(n: int) -> str:
    if n == 0:
        return "Nenhum processo encontrado."
    return f"{n} processo{'s' if n > 1 else ''} encontrado{'s' if n > 1 else ''}."


async def movimentar(paj_norm: str, html_relatorio: str) -> dict:
    """Juntada de documento no SISDPU com o relatório em HTML (sem anexo). Passa pela
    ficha de aprovação padrão do SIS-DPU 2.0 — a requisição fica aguardando o
    Defensor confirmar no modal."""
    from ingestao import sisdpu_movtram as movtram
    from services import acoes_sisdpu_service, sat_service
    from services.juntada_dossie_service import id_processo_local, ler_meta

    if not (html_relatorio or "").strip():
        return {"ok": False, "detalhe": "Relatório vazio — refaça a consulta."}
    meta = ler_meta(paj_norm)
    paj_label = (meta.get("paj") or "").strip()
    id_proc, _eventos, erro = await acoes_sisdpu_service._resolver_id_processo(
        id_processo_local(meta), paj_label)
    if not id_proc:
        return {"ok": False, "detalhe": erro or "id do PAJ no SISDPU não encontrado."}
    pajs = [{"id": id_proc, "numero": paj_label}]
    mv, exc, _ev = await acoes_sisdpu_service.com_auto_reconexao(
        lambda: movtram.movimentar_paj(pajs, html_relatorio, sat_service.FASE_MOV, None,
                                       descricao_html=True))
    if exc is not None:
        return {"ok": False, "detalhe": f"{type(exc).__name__}: {exc}"}
    mv = mv or {}
    if mv.get("ok"):
        try:
            from services import historico_painel
            historico_painel.registrar({
                "paj": paj_label, "paj_norm": paj_norm,
                "url_detalhe": (meta.get("sisdpu_raw") or {}).get("url", ""),
                "assistido": meta.get("assistido_caixa") or "",
                "origem": "acoes_ia", "atividade": "Relatório de ações judiciais anteriores",
                "movimentacao": sat_service.FASE_MOV, "tramitado_para": "",
                "concluido": False, "detalhe": TITULO,
            })
        except Exception:  # noqa: BLE001
            pass
    return {"ok": bool(mv.get("ok")), "detalhe": mv.get("detalhe", ""), "mov_seq": mv.get("mov_seq")}
