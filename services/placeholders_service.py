"""Dados de um PAJ para os placeholders dos modelos (petição padrão, "Usar
modelo" de Ações no e-proc/SISDPU) — 19/10/2026.

O catálogo de placeholders (com as categorias mostradas em Configurações) fica
em static/js/rich_editor.js (`PLACEHOLDERS_CATALOGO`); aqui só os VALORES.

Tudo o que já está no metadata do PAJ sai na hora. Dois grupos dependem de
consulta e só são buscados quando o modelo usa algum placeholder deles
(`extras`):
  - "pessoa": ficha do assistido principal no SIS-DPU 2.0 (`assistido_ficha`)
    — data de nascimento, idade, nome da mãe, RG, nacionalidade e o sexo (para
    concordar a [QUALIFICACAO]). Consulta a dado pessoal: fica na trilha do SIS 2
    e conta no teto diário — por isso só sob demanda.
  - "processo": cabeçalho do processo pelo MCP do e-proc (TRF4 ou JMU, pelo
    número) — vara/órgão julgador, classe, autor(es), réu(s).
Estado civil e profissão NÃO entram: a ficha só traz códigos, sem a descrição.

Dado ausente devolve "" — o front mantém o placeholder literal no texto.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import date, datetime

from config import PAJS_DIR


def _meta(paj_norm: str) -> dict:
    with contextlib.suppress(Exception):
        return json.loads((PAJS_DIR / paj_norm / "metadata.json").read_text(encoding="utf-8"))
    return {}


def formatar_cnj(numero: str) -> str:
    d = re.sub(r"\D", "", numero or "")
    if len(d) != 20:
        return numero or ""
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def _juntar(nomes: list[str]) -> str:
    nomes = [n for n in nomes if n]
    if len(nomes) <= 1:
        return nomes[0] if nomes else ""
    return ", ".join(nomes[:-1]) + " e " + nomes[-1]


def _data_br(valor: str) -> str:
    v = (valor or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.match(r"(\d{2}/\d{2}/\d{4})", v)
    return m.group(1) if m else ""


def _processo_e_juizo(meta: dict) -> tuple[str, str]:
    from services.peticao_ia_service import _numero_processo_unico
    numero = _numero_processo_unico(meta)
    juizo = ""
    alvo = re.sub(r"\D", "", numero)
    for p in meta.get("processos_judiciais") or []:
        if alvo and re.sub(r"\D", "", str(p.get("processo_judicial") or "")) == alvo:
            juizo = (p.get("juizo") or "").strip()
            break
    if not juizo and alvo:
        juizo = (meta.get("juizo") or "").strip()
    return formatar_cnj(numero), juizo


def _pessoa(meta: dict) -> dict:
    """Ficha do assistido principal no SIS-DPU 2.0 (só sob demanda)."""
    raw = meta.get("sisdpu_raw") or {}
    principal = next((a for a in raw.get("assistidos_pf") or [] if a.get("principal")),
                     (raw.get("assistidos_pf") or [None])[0])
    id_pessoa = (principal or {}).get("idPessoa")
    if not id_pessoa:
        return {}
    from ingestao import sisdpu2_mcp_client as sis2
    r = sis2.chamar("assistido_ficha", {"idPessoa": int(id_pessoa)})
    txt = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return {}
    ficha = json.loads(txt[i:j + 1])
    sexo = (ficha.get("sexo") or "").upper()
    nasc = ficha.get("dataNascimento") or ""
    idade = ""
    with contextlib.suppress(Exception):
        dn = datetime.strptime(nasc[:10], "%Y-%m-%d").date()
        hoje = date.today()
        idade = str(hoje.year - dn.year - ((hoje.month, hoje.day) < (dn.month, dn.day)))
    rg = (ficha.get("rg") or "").strip()
    if rg and (ficha.get("orgaoEmissorRg") or "").strip():
        rg = f"{rg} ({ficha['orgaoEmissorRg'].strip()})"
    natural = ficha.get("naturalBrasil")
    nacionalidade = ""
    if natural is True:
        nacionalidade = "brasileira" if sexo == "F" else "brasileiro" if sexo == "M" else "brasileiro(a)"
    elif natural is False:
        nacionalidade = "estrangeira" if sexo == "F" else "estrangeiro" if sexo == "M" else "estrangeiro(a)"
    return {
        "sexo": sexo,
        "data_nascimento": _data_br(nasc),
        "idade": f"{idade} anos" if idade else "",
        "nome_mae": (ficha.get("nomeMae") or "").strip(),
        "rg": rg,
        "nacionalidade": nacionalidade,
    }


async def _processo_mcp(numero: str) -> dict:
    """Cabeçalho do processo pelo MCP do e-proc (TRF4 ou JMU, pelo número)."""
    if not numero:
        return {}
    from ingestao import eproc_mcp_client as emc
    c = await emc.cabecalho_completo(numero, instancia=1)
    if not isinstance(c, dict) or c.get("erro"):
        return {}
    partes = c.get("partes") or []
    polo_ativo = ("AUTOR", "REQUERENTE", "EXEQUENTE", "IMPETRANTE", "RECORRENTE", "APELANTE", "EMBARGANTE")
    polo_passivo = ("REU", "RÉU", "REQUERIDO", "EXECUTADO", "IMPETRADO", "RECORRIDO", "APELADO", "EMBARGADO", "ACUSADO")

    def nomes(chaves):
        return _juntar([p.get("nome", "") for p in partes
                        if any((p.get("tipo") or "").upper().startswith(k) for k in chaves)])
    return {
        "vara": (c.get("orgao_julgador") or c.get("juizo") or "").strip(),
        "classe": (c.get("classe") or "").strip(),
        "autor": nomes(polo_ativo),
        "reu": nomes(polo_passivo),
    }


def _prazo(numero: str) -> dict:
    """Prazo mais próximo deste processo no Painel do Procurador sincronizado
    (TRF4 ou JMU) — evento da intimação e data final (real ou estimada)."""
    if not numero:
        return {}
    from ingestao import eproc_intimacoes
    prazos = (eproc_intimacoes.prazos_processo(numero) or {}).get("prazos") or []

    def chave(p):
        d = _data_br(p.get("final_prazo") or "") or _data_br(p.get("final_estimado") or "")
        return (d[6:] + d[3:5] + d[:2]) if d else "99999999"
    if not prazos:
        return {}
    p = sorted(prazos, key=chave)[0]
    final = _data_br(p.get("final_prazo") or "")
    if not final and _data_br(p.get("final_estimado") or ""):
        final = _data_br(p["final_estimado"]) + " (estimado)"
    evento = re.sub(r"\s+", " ", p.get("evento") or "").strip()
    return {"evento_intimacao": evento, "prazo_final": final}


def _qualificacao(base: dict, pessoa: dict) -> str:
    nome, cpf = base.get("nome"), base.get("cpf")
    if not nome:
        return ""
    sexo = (pessoa or {}).get("sexo", "")
    o = "a" if sexo == "F" else "o" if sexo == "M" else "o(a)"
    partes = [nome]
    if (pessoa or {}).get("nacionalidade"):
        partes.append(pessoa["nacionalidade"])
    if cpf:
        partes.append(f"inscrit{o} no CPF sob o nº {cpf}")
    if base.get("representante"):
        rep = f"representad{o} por {base['representante']}"
        if base.get("cpf_representante"):
            rep += f" (CPF nº {base['cpf_representante']})"
        partes.append(rep)
    end = (base.get("enderecos") or "").split(" | ")[0]
    if end:
        partes.append(f"residente e domiciliad{o} em {end}")
    return ", ".join(partes)


async def dados(paj_norm: str, extras: set[str] | None = None) -> dict:
    """Valores dos placeholders de dados do PAJ. `extras`: {"pessoa", "processo"}
    — consultas sob demanda (ver docstring do módulo)."""
    from services.peticao_ia_service import obter_qualificacao_principal

    extras = extras or set()
    meta = _meta(paj_norm)
    raw = meta.get("sisdpu_raw") or {}
    base = dict(obter_qualificacao_principal(paj_norm) or {})
    processo, juizo = _processo_e_juizo(meta)
    reps = raw.get("representantes_legais") or []
    base.update({
        "processo": processo,
        "juizo": juizo,
        "representante": _juntar([r.get("nome", "") for r in reps]),
        "cpf_representante": _juntar([r.get("cpf", "") for r in reps]),
        "assistidos": _juntar([a.get("nome", "") for a in raw.get("assistidos_pf") or []]) or base.get("nome", ""),
        "pretensao": (meta.get("pretensaopura") or meta.get("pretensao") or "").strip(),
        "data_abertura_paj": _data_br(meta.get("data_abertura") or raw.get("data_abertura") or ""),
    })
    base.update(_prazo(processo))

    pessoa: dict = {}
    if "pessoa" in extras:
        with contextlib.suppress(Exception):
            pessoa = _pessoa(meta)
        base.update({k: v for k, v in pessoa.items() if k != "sexo"})
    if "processo" in extras and processo:
        with contextlib.suppress(Exception):
            base.update(await _processo_mcp(processo))
    base["qualificacao"] = _qualificacao(base, pessoa)
    return base
