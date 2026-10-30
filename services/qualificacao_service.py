"""Qualificação dos envolvidos do PAJ — fonte ÚNICA (botão "Ver qualificação"
da seção "Assistidos e Representantes Legais", pedido do Defensor 30/10/2026).

Antes disto a qualificação era montada de três jeitos que não conversavam: a
IA da petição inicial redigia livremente a partir dos documentos, o
placeholder [QUALIFICACAO] (services/placeholders_service.py) usava só nome/
CPF/endereço, e a preparação no e-proc (services/peticao_inicial_service.py)
lia o PDF da inicial com regex. Aqui fica `PAJs/<paj>/qualificacao.json`:

  - pessoas: autor(es) e representante(s), cada campo com a FONTE ("SIS 2",
    "termo de curatela", "editado"). Campo editado pelo Defensor nunca é
    sobrescrito por "Atualizar do SIS 2".
  - representacao: nenhuma | menor | curatela, com os tipos já no vocabulário
    do e-proc (janela "Informações da Representação").
  - texto: a qualificação corrida para a petição inicial, gerada por REGRA
    (concordância, "menor impúbere", "neste ato representado por", "ambos
    residentes") — editável; editada, não é regenerada.

Fontes (fase 1): o cadastro do SIS-DPU 2.0 — `inicial_cadastro_para_protocolo`
(assistidos + representantes ligados, endereço com município, flag `menor`,
lista `faltando`) e `assistido_ficha` (nacionalidade, RG) — e, para curatela,
o termo de curatela da pasta Ajuizamento (curador nomeado, PF ou PJ). Estado
civil e profissão vêm só como CÓDIGO na ficha (sem tabela de descrição): ficam
para o Defensor (fase 2: completar pelos documentos com IA).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import config

ARQUIVO = "qualificacao.json"

CAMPOS_PESSOA = ["nome", "tipo_pessoa", "cpf", "rg", "orgao_rg", "nascimento", "sexo", "nacionalidade",
                 "estado_civil", "profissao", "nome_mae", "telefone", "email"]
CAMPOS_ENDERECO = ["logradouro", "numero", "complemento", "bairro", "municipio", "uf", "cep"]

VINCULOS = ["genitora", "genitor", "tutora", "tutor", "curadora", "curador", "guardiã", "guardião",
            "representante legal"]

# Vocabulário do e-proc (janela "Informações da Representação", JFPR 28/10/2026).
EPROC_AUTOR = {"menor_impubere": "Absolutamente Incapaz (Art. 3º CC)",
               "menor_pubere": "Relativamente Incapaz (Art. 4º CC)",
               "curatela": "Civilmente Incapaz - Art. 110, 8.213/91"}
# Representante: a lista do e-proc DEPENDE do tipo do representado — com
# "Absolutamente/Relativamente Incapaz" (menor) as opções são Tutor, Pais,
# Curador e Representante (visto ao vivo no PAJ 2026/029-03088, 30/10/2026).
EPROC_REP = {"menor": "Pais", "tutor": "Tutor", "curatela": "Curador"}
EPROC_REP_OPCOES = ["Pais", "Tutor", "Curador", "Representante",
                    "Cônjuge, pai, mãe, tutor, curador ou herdeiro necessário"]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _pasta(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _limpo(v) -> str:
    """Tira sobras do cadastro legado ("RUA X," / "AUGUSTA -" / "None")."""
    s = "" if v is None else str(v)
    s = re.sub(r"\s+", " ", s).strip()
    if s.lower() in ("none", "null"):
        return ""
    return re.sub(r"[\s,;\-–]+$", "", s).strip()


def _data_br(v) -> str:
    s = _limpo(v)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def _idade(nascimento_br: str, ref: date | None = None) -> int | None:
    with contextlib.suppress(Exception):
        dn = datetime.strptime(nascimento_br, "%d/%m/%Y").date()
        ref = ref or date.today()
        return ref.year - dn.year - ((ref.month, ref.day) < (dn.month, dn.day))
    return None


def _doc_fmt(doc: str) -> str:
    d = re.sub(r"\D", "", doc or "")
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return _limpo(doc)


def _titulo(s: str) -> str:
    """"RUA CIDADE DE ITAPETININGA" -> "Rua Cidade de Itapetininga"."""
    pequenas = {"de", "da", "do", "das", "dos", "e", "em", "na", "no"}
    palavras = _limpo(s).lower().split(" ")
    return " ".join(p if (i and p in pequenas) else p[:1].upper() + p[1:] for i, p in enumerate(palavras) if p)


# ---------------------------------------------------------------------------
# Leitura/gravação
# ---------------------------------------------------------------------------

def ler(paj_norm: str) -> dict | None:
    p = _pasta(paj_norm) / ARQUIVO
    if not p.exists():
        return None
    with contextlib.suppress(Exception):
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def salvar(paj_norm: str, q: dict) -> dict:
    """Grava o que veio do modal (campos já marcados "editado" pelo front),
    recalcula representação/texto (o texto só se não foi editado)."""
    q = _completar(q)
    q["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    (_pasta(paj_norm) / ARQUIVO).write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    return q


# ---------------------------------------------------------------------------
# SIS-DPU 2.0
# ---------------------------------------------------------------------------

def _pessoa_vazia(papel: str) -> dict:
    p = {c: "" for c in CAMPOS_PESSOA}
    p.update({"id": "", "papel": papel, "representa": "", "vinculo": "", "tipo_pessoa": "PF",
              "endereco": {c: "" for c in CAMPOS_ENDERECO}, "faltando": [], "fontes": {}, "menor": False})
    return p


def _de_cadastro(c: dict, papel: str, ficha: dict | None) -> dict:
    p = _pessoa_vazia(papel)
    fonte = "SIS 2"
    ficha = ficha or {}
    sexo = _limpo(c.get("sexo") or ficha.get("sexo")).upper()
    natural = ficha.get("naturalBrasil")
    rg = _limpo(ficha.get("rg"))
    valores = {
        "nome": _limpo(c.get("nome")),
        "cpf": _doc_fmt(c.get("cpf") or ""),
        "rg": rg,
        "orgao_rg": _limpo(ficha.get("orgaoEmissorRg")),
        "nascimento": _data_br(c.get("nascimento") or ficha.get("dataNascimento")),
        "sexo": sexo if sexo in ("M", "F") else "",
        "nome_mae": _limpo(c.get("nomeMae") or ficha.get("nomeMae")),
        "nacionalidade": "brasileira" if natural is True else "",
    }
    for k, v in valores.items():
        if v:
            p[k] = v
            p["fontes"][k] = fonte
    e = c.get("endereco") or {}
    if e:
        end = {"logradouro": _titulo(e.get("logradouro")), "numero": _limpo(e.get("numero")),
               "complemento": _limpo(e.get("complemento")).lower(), "bairro": _titulo(e.get("bairro")),
               "municipio": re.sub(r"\s*\(capital\)\s*$", "", _limpo(e.get("municipio")), flags=re.I),
               "uf": _limpo(e.get("uf")), "cep": _limpo(e.get("cep"))}
        p["endereco"].update({k: v for k, v in end.items() if v})
        if any(end.values()):
            p["fontes"]["endereco"] = fonte
    p["id"] = str(c.get("idPessoa") or "")
    p["menor"] = bool(c.get("menor"))
    p["faltando"] = list(c.get("faltando") or [])
    return p


def _fone_fmt(d: str) -> str:
    d = re.sub(r"\D", "", d or "")
    return f"({d[:2]}) {d[2:-4]}-{d[-4:]}" if len(d) in (10, 11) else d


def _contatos_do_paj(paj_norm: str) -> tuple[str, str]:
    """TODOS os telefones e e-mails cadastrados no PAJ, separados por ", "."""
    with contextlib.suppress(Exception):
        meta = json.loads((_pasta(paj_norm) / "metadata.json").read_text(encoding="utf-8"))
        tels = []
        for t in meta.get("telefones") or []:
            f = _fone_fmt(t.get("numero") or "")
            if f and f not in tels:
                tels.append(f)
        emails = []
        for e in meta.get("emails") or []:
            e = (e or "").strip().lower()
            if e and e not in emails:
                emails.append(e)
        return ", ".join(tels), ", ".join(emails)
    return "", ""


def _contatos_txt(pessoas: list[dict]) -> str:
    """", telefones (41) 99740-5907 e (41) 98744-6081, e-mail x@y" — dos
    envolvidos do bloco, sem repetir."""
    tels, emails = [], []
    for p in pessoas:
        for t in re.split(r"\s*[,;]\s*", p.get("telefone") or ""):
            if t and t not in tels:
                tels.append(t)
        for e in re.split(r"\s*[,;]\s*", p.get("email") or ""):
            if e and e not in emails:
                emails.append(e)

    def lista(xs):
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " e " + xs[-1]
    partes = []
    if tels:
        partes.append(("telefone " if len(tels) == 1 else "telefones ") + lista(tels))
    if emails:
        partes.append(("e-mail " if len(emails) == 1 else "e-mails ") + lista(emails))
    return (", " + ", ".join(partes)) if partes else ""


def _paj_label(paj_norm: str) -> str:
    m = re.match(r"PAJ-(\d{4})-(\d{3})-(\d{5})$", paj_norm)
    return f"{m.group(1)}/{m.group(2)}-{m.group(3)}" if m else paj_norm


def _curador_do_termo(paj_norm: str) -> dict:
    """Curador nomeado no termo de curatela da pasta Ajuizamento (mesma
    leitura da preparação no e-proc — OCR do painel, "nomeou X (CPF/CNPJ)")."""
    with contextlib.suppress(Exception):
        from services import peticao_inicial_service as pis
        rep = pis._representante(paj_norm, "curatela")
        if rep.get("documento"):
            return rep
    return {}


def _vinculo_por_cadastro(rep: dict, autor: dict) -> str:
    """Genitora quando o nome do representante é o "nome da mãe" do autor no
    cadastro; demais casos ficam para o Defensor escolher."""
    if rep.get("nome") and _norm(rep["nome"]) == _norm(autor.get("nome_mae")):
        return "genitora"
    return ""


async def atualizar_do_sis2(paj_norm: str) -> dict:
    """Lê o cadastro do PAJ no SIS-DPU 2.0 e mescla com o qualificacao.json
    existente (campos "editado" ficam). Poucas leituras: 1 cadastro + 1 ficha
    por pessoa."""
    from ingestao import sisdpu2_fonte, sisdpu2_movtram
    from ingestao import sisdpu2_mcp_client as sis2

    if not sis2.conectado():
        raise RuntimeError("O painel não está conectado ao SIS-DPU 2.0 — conecte pelo selo do SIS 2 e tente de novo.")
    idp = await sisdpu2_movtram._id_processo({"numero": _paj_label(paj_norm)})
    cad = await asyncio.to_thread(sis2.chamar, "inicial_cadastro_para_protocolo", {"idProcesso": int(idp)})
    if not isinstance(cad, dict):
        raise RuntimeError("O SIS-DPU 2.0 não devolveu o cadastro do PAJ.")

    novas: list[dict] = []
    for a in cad.get("assistidos") or []:
        ficha = await asyncio.to_thread(sisdpu2_fonte._ler_ficha, str(a.get("idPessoa"))) if a.get("idPessoa") else None
        autor = _de_cadastro(a, "autor", ficha)
        novas.append(autor)
        for r in a.get("representantes") or []:
            ficha_r = await asyncio.to_thread(sisdpu2_fonte._ler_ficha, str(r.get("idPessoa"))) if r.get("idPessoa") else None
            rep = _de_cadastro(r, "representante", ficha_r)
            rep["representa"] = autor["id"]
            v = _vinculo_por_cadastro(rep, autor)
            if v:
                rep["vinculo"] = v
                rep["fontes"]["vinculo"] = "SIS 2 (nome da mãe do autor)"
                if not rep["sexo"]:
                    rep["sexo"] = "F"
                    rep["fontes"]["sexo"] = "SIS 2 (genitora)"
            novas.append(rep)

    tel, email = _contatos_do_paj(paj_norm)
    principal =next((p for p in novas if p["papel"] == "representante"), None) or \
        next((p for p in novas if p["papel"] == "autor"), None)
    if principal:  # contato: o do PAJ vai para quem efetivamente atende (representante, se houver)
        if tel:
            principal["telefone"], principal["fontes"]["telefone"] = tel, "PAJ (contatos)"
        if email:
            principal["email"], principal["fontes"]["email"] = email, "PAJ (contatos)"

    # Curatela: termo na pasta Ajuizamento e nenhum representante no cadastro → curador do termo.
    autores = [p for p in novas if p["papel"] == "autor"]
    if autores and not any(p["papel"] == "representante" for p in novas):
        cur = _curador_do_termo(paj_norm)
        if cur:
            rep = _pessoa_vazia("representante")
            rep.update({"nome": cur.get("nome", ""), "cpf": cur.get("documento", ""),
                        "tipo_pessoa": cur.get("tipo_pessoa", "PF"), "representa": autores[0]["id"],
                        "vinculo": "curador"})
            rep["fontes"] = {"nome": "termo de curatela", "cpf": "termo de curatela", "vinculo": "termo de curatela"}
            novas.append(rep)

    q = _mesclar(ler(paj_norm), novas)
    q["sis2_em"] = datetime.now().isoformat(timespec="seconds")
    return salvar(paj_norm, q)


def _chave(p: dict) -> str:
    return p.get("id") or re.sub(r"\D", "", p.get("cpf") or "") or _norm(p.get("nome"))


def _mesclar(antigo: dict | None, novas: list[dict]) -> dict:
    """Pessoas do SIS 2 + o que o Defensor já tinha editado (fonte "editado")."""
    antigas = {_chave(p): p for p in (antigo or {}).get("pessoas") or []}
    saida = []
    for n in novas:
        a = antigas.pop(_chave(n), None)
        if a:
            for campo, fonte in (a.get("fontes") or {}).items():
                # editado: sempre fica; IA: fica enquanto o SIS 2 não tiver o dado
                vazio_no_sis2 = not ((n.get("endereco") or {}).get("logradouro") if campo == "endereco" else n.get(campo))
                if not (fonte == "editado" or (fonte.startswith("IA:") and vazio_no_sis2)):
                    continue
                if campo == "endereco":
                    n["endereco"] = a.get("endereco") or n["endereco"]
                else:
                    n[campo] = a.get(campo, "")
                n["fontes"][campo] = fonte
                if (a.get("evidencias") or {}).get(campo):
                    n.setdefault("evidencias", {})[campo] = a["evidencias"][campo]
        saida.append(n)
    # pessoa incluída à mão no modal (não existe no SIS 2): mantém
    saida += [p for p in antigas.values() if (p.get("fontes") or {}).get("nome") == "editado"]
    q = dict(antigo or {})
    q["pessoas"] = saida
    return q


# ---------------------------------------------------------------------------
# Representação + texto corrido
# ---------------------------------------------------------------------------

def _representacao(q: dict) -> dict:
    """Tipo da representação e os tipos do e-proc (editáveis no modal: um
    valor marcado "editado" fica)."""
    atual = q.get("representacao") or {}
    if atual.get("editado"):
        return atual
    pessoas = q.get("pessoas") or []
    autor = next((p for p in pessoas if p.get("papel") == "autor"), None)
    rep = next((p for p in pessoas if p.get("papel") == "representante"), None)
    if not autor or not rep:
        return {"tipo": "nenhuma", "tipo_autor_eproc": "", "tipo_representante_eproc": ""}
    idade = _idade(autor.get("nascimento") or "")
    curatela = _norm(rep.get("vinculo")).startswith("curador") or (idade is not None and idade >= 18)
    if curatela:
        return {"tipo": "curatela", "tipo_autor_eproc": EPROC_AUTOR["curatela"],
                "tipo_representante_eproc": EPROC_REP["curatela"]}
    pubere = idade is not None and idade >= 16
    tutor = _norm(rep.get("vinculo")).startswith("tutor")
    return {"tipo": "menor", "tipo_autor_eproc": EPROC_AUTOR["menor_pubere" if pubere else "menor_impubere"],
            "tipo_representante_eproc": EPROC_REP["tutor" if tutor else "menor"]}


def _g(sexo: str, m: str, f: str, n: str) -> str:
    return f if sexo == "F" else m if sexo == "M" else n


_LOGRADOUROS_MASC = ("largo", "beco", "conjunto", "condominio", "residencial", "loteamento", "jardim",
                     "parque", "sitio", "acesso", "anel", "contorno", "caminho", "trevo", "viaduto")


def _endereco_txt(e: dict) -> str:
    """"na Rua X, nº 1, casa, bairro Y, Curitiba/PR, CEP ..." (com a preposição)."""
    e = e or {}
    if not e.get("logradouro"):
        return ""
    prep = "no" if _norm(e["logradouro"]).split(" ")[0] in _LOGRADOUROS_MASC else "na"
    partes = [f"{prep} {e['logradouro']}"]
    if e.get("numero"):
        partes.append(f"nº {e['numero']}")
    if e.get("complemento"):
        partes.append(e["complemento"])
    if e.get("bairro"):
        partes.append(f"bairro {e['bairro']}")
    cidade = "/".join(x for x in (e.get("municipio"), e.get("uf")) if x)
    if cidade:
        partes.append(cidade)
    if e.get("cep"):
        partes.append(f"CEP {e['cep']}")
    return ", ".join(partes)


def _mesmo_endereco(a: dict, b: dict) -> bool:
    ea, eb = a.get("endereco") or {}, b.get("endereco") or {}
    if not eb.get("logradouro"):
        return True  # representante sem endereço cadastrado: reside com o representado
    return _norm(ea.get("logradouro")) == _norm(eb.get("logradouro")) and \
        _norm(ea.get("numero")) == _norm(eb.get("numero"))


def _pessoa_txt(p: dict, menor_txt: str = "") -> str:
    s = p.get("sexo") or ""
    if p.get("tipo_pessoa") == "PJ":
        partes = [p.get("nome", "").upper(), "pessoa jurídica"]
        if p.get("cpf"):
            partes.append(f"inscrita no CNPJ sob o nº {p['cpf']}")
        return ", ".join(x for x in partes if x)
    partes = [p.get("nome", "").upper()]
    if p.get("nacionalidade"):
        nac = p["nacionalidade"].lower()
        if nac.startswith("brasileir"):
            nac = _g(s, "brasileiro", "brasileira", "brasileiro(a)")
        partes.append(nac)
    if menor_txt:
        partes.append(menor_txt)
    else:
        if p.get("estado_civil"):
            partes.append(p["estado_civil"].lower())
        if p.get("profissao"):
            partes.append(p["profissao"].lower())
    if p.get("nascimento") and menor_txt:
        partes.append(f"{_g(s, 'nascido', 'nascida', 'nascido(a)')} em {p['nascimento']}")
    if p.get("rg"):
        rg = p["rg"] + (f" {p['orgao_rg']}" if p.get("orgao_rg") else "")
        partes.append(f"{_g(s, 'portador', 'portadora', 'portador(a)')} da cédula de identidade nº {rg}")
    if p.get("cpf"):
        partes.append(f"{_g(s, 'inscrito', 'inscrita', 'inscrito(a)')} no CPF sob o nº {p['cpf']}")
    return ", ".join(x for x in partes if x)


def texto_qualificacao(q: dict) -> str:
    pessoas = q.get("pessoas") or []
    autores = [p for p in pessoas if p.get("papel") == "autor"]
    if not autores:
        return ""
    blocos = []
    repres = q.get("representacao") or {}
    for autor in autores:
        sa = autor.get("sexo") or ""
        rep = next((p for p in pessoas if p.get("papel") == "representante"
                    and (p.get("representa") in ("", autor.get("id")))), None)
        menor_txt = ""
        if rep and repres.get("tipo") == "menor":
            idade = _idade(autor.get("nascimento") or "")
            menor_txt = "menor púbere" if idade is not None and idade >= 16 else "menor impúbere"
        txt = _pessoa_txt(autor, menor_txt)
        if rep:
            sr = rep.get("sexo") or ""
            vinc = (rep.get("vinculo") or "").strip()
            if vinc and vinc != "representante legal":
                artigo = "sua" if (vinc.endswith("a") or vinc.endswith("ã")) else "seu"
                if repres.get("tipo") == "curatela" and rep.get("tipo_pessoa") == "PJ":
                    vinc = "curadora"
                    artigo = "sua"
                quem = f"{artigo} {vinc}"
            else:
                quem = _g(sr, "seu representante legal", "sua representante legal", "seu(sua) representante legal")
            txt += f", neste ato {_g(sa, 'representado', 'representada', 'representado(a)')} por {quem} {_pessoa_txt(rep)}"
            if _mesmo_endereco(autor, rep):
                end = _endereco_txt(autor.get("endereco"))
                if end:
                    txt += f", ambos residentes e domiciliados {end}" if rep.get("tipo_pessoa") != "PJ" \
                        else f", {_g(sa, 'residente e domiciliado', 'residente e domiciliada', 'residente e domiciliado(a)')} {end}"
            else:
                end_a, end_r = _endereco_txt(autor.get("endereco")), _endereco_txt(rep.get("endereco"))
                if end_r:
                    mora = "com sede" if rep.get("tipo_pessoa") == "PJ" else \
                        _g(sr, "residente e domiciliado", "residente e domiciliada", "residente e domiciliado(a)")
                    txt += f", {mora} {end_r}"
                if end_a:
                    txt += f"; {_g(sa, 'o autor', 'a autora', 'o(a) autor(a)')} reside {end_a}"
        else:
            end = _endereco_txt(autor.get("endereco"))
            if end:
                txt += f", {_g(sa, 'residente e domiciliado', 'residente e domiciliada', 'residente e domiciliado(a)')} {end}"
        txt += _contatos_txt([autor] + ([rep] if rep else []))
        blocos.append(txt)
    return "; ".join(blocos)


def _completar(q: dict) -> dict:
    q = dict(q or {})
    q.setdefault("pessoas", [])
    q["representacao"] = _representacao(q)
    if not q.get("texto_editado"):
        q["texto"] = texto_qualificacao(q)
    # lacunas que pesam na petição/e-proc (além do "faltando" do SIS 2)
    for p in q["pessoas"]:
        lac = []
        for campo, rot in (("cpf", "CPF/CNPJ"), ("sexo", "sexo"), ("nacionalidade", "nacionalidade")):
            if not p.get(campo) and not (campo != "cpf" and p.get("tipo_pessoa") == "PJ"):
                lac.append(rot)
        if p.get("papel") == "autor" and not (p.get("endereco") or {}).get("logradouro"):
            lac.append("endereço")
        adulto_pf = p.get("tipo_pessoa") != "PJ" and not p.get("menor")
        if adulto_pf:  # menor não leva estado civil/profissão na qualificação
            for campo, rot in (("estado_civil", "estado civil"), ("profissao", "profissão")):
                if not p.get(campo):
                    lac.append(rot)
        p["lacunas"] = lac
    return q


def obter(paj_norm: str) -> dict | None:
    q = ler(paj_norm)
    return _completar(q) if q else None


# ---------------------------------------------------------------------------
# Fase 2 — completar pelos documentos do PAJ (IA), só as lacunas
# ---------------------------------------------------------------------------

# Documentos que costumam trazer dados de qualificação (nome do arquivo, sem
# acento). Ordem = prioridade no corte de tamanho.
_DOCS_QUALIFICACAO = [
    # Formulário socioeconômico da DPU: o PONTO DE PARTIDA — traz estado
    # civil, profissão, endereço, contatos e o grupo familiar (dica do
    # Defensor, 30/10/2026).
    r"socioeconomic",
    r"\brg\b|\bcin\b|identidade|\bcnh\b|habilitacao|\bcpf\b|documento de identifica",
    r"certidao.*(nascimento|casamento)|nascimento|casamento",
    r"comprovante de (residencia|endereco)|endereco|residencia",
    r"curatela|curador|interdi",
    r"formulario|ficha|narrativa|reducao a termo|termo de atendimento|veracidade",
    r"ctps|carteira de trabalho",
    r"peticao[ _]inicial|minuta de inicial|inicial",
]
_MAX_POR_DOC = 5000
_MAX_SOCIOECONOMICO = 15000
_MAX_TOTAL = 50000

CAMPOS_IA = ["sexo", "nacionalidade", "estado_civil", "profissao", "rg", "orgao_rg", "nascimento", "nome_mae"]


def _docs_para_ia(paj_norm: str) -> list[tuple[str, str]]:
    """(nome do arquivo, texto) dos documentos de qualificação: textos já
    extraídos em Arquivos/*.txt (sem OCR novo) + PDFs da pasta Ajuizamento
    que casem (o termo de curatela é imagem — OCR do painel, com cache)."""
    pasta = _pasta(paj_norm)
    cands: list[tuple[int, str, Path]] = []
    arquivos = pasta / "Arquivos"
    # Cada documento de Arquivos: o .txt já extraído pelo painel; sem ele (PAJ
    # cuja extração ainda não rodou — caso real 2026/029-04286, 30/10/2026),
    # o próprio PDF (texto nativo ou OCR do painel, com cache).
    vistos_stem: set[str] = set()
    for arq in sorted(arquivos.iterdir()) if arquivos.is_dir() else []:
        if arq.suffix.lower() not in (".txt", ".pdf") or arq.stem in vistos_stem:
            continue
        txt = arq.with_suffix(".txt")
        fonte = txt if txt.exists() else arq
        vistos_stem.add(arq.stem)
        n = _norm(re.sub(r"^seq\d+\s*-\s*\d+\s*-\s*", "", arq.stem)).replace("_", " ")
        for prio, pad in enumerate(_DOCS_QUALIFICACAO):
            if re.search(pad, n):
                cands.append((prio, fonte.name, fonte))
                break
    for pdf in sorted((pasta / "Ajuizamento").glob("*.pdf")) if (pasta / "Ajuizamento").is_dir() else []:
        n = _norm(re.sub(r"^\d+\s*-\s*", "", pdf.stem)).replace("_", " ")
        for prio, pad in enumerate(_DOCS_QUALIFICACAO[:4]):  # identidade, certidões, endereço, curatela
            if re.search(pad, n):
                cands.append((prio, f"Ajuizamento/{pdf.name}", pdf))
                break
    cands.sort(key=lambda c: c[0])
    docs, total, vistos = [], 0, set()
    # 1º o Formulário Socioeconômico Detalhado, achado pelo título (avulso ou
    # dentro de um compilado); o arquivo dele não entra de novo inteiro.
    rot, trecho, stem_form = _formulario_socioeconomico(pasta)
    if trecho:
        trecho = re.sub(r"[ \t]+", " ", trecho).strip()
        docs.append((rot, trecho))
        total += len(trecho)
        vistos.add(re.sub(r"\s+", "", trecho))
        # nem o arquivo dele de novo, nem outra via do mesmo formulário ("_assinado")
        cands = [c for c in cands if c[2].stem != stem_form and "socioecon" not in _norm(c[2].stem)]
    for _, nome, arq in cands:
        try:
            if arq.suffix.lower() == ".pdf":
                texto = _texto_documento(arq)
            else:
                texto = arq.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        limite = _MAX_SOCIOECONOMICO if re.search(r"socioeconomic", _norm(nome)) else _MAX_POR_DOC
        texto = re.sub(r"[ \t]+", " ", texto).strip()[:limite]
        # texto INTEIRO: documentos diferentes podem abrir com o mesmo cabeçalho
        # de assinatura digital (CIN e CNHs do PAJ 2026/029-04286)
        assinatura = re.sub(r"\s+", "", texto)
        if len(texto) < 40 or total + len(texto) > _MAX_TOTAL or assinatura in vistos:
            continue  # curto, estoura o limite ou cópia (Ajuizamento repete Arquivos)
        vistos.add(assinatura)
        docs.append((nome, texto))
        total += len(texto)
    return docs


_RE_SOCIOECONOMICO = re.compile(r"s[oó]cio[\s\-]*econ[oô]mic[oa]\s+detalhad[oa]|formul[aá]rio\s+s[oó]cio[\s\-]*econ[oô]mico",
                                re.IGNORECASE)


def _seq(arq: Path) -> int:
    m = re.match(r"seq(\d+)", arq.name, re.IGNORECASE)
    return int(m.group(1)) if m else 9999


def _formulario_socioeconomico(pasta: Path) -> tuple[str, str, str]:
    """Acha o "Formulário Socioeconômico Detalhado" da DPU PELO CONTEÚDO: pode
    vir avulso ou compilado com os demais documentos da instauração do PAJ
    (ex. "FORMULARIOS PAJ.pdf"), quase sempre nas primeiras sequências
    (Defensor, 30/10/2026). Devolve (rótulo, trecho a partir do título, stem
    do arquivo) ou ("", "", "")."""
    arquivos = pasta / "Arquivos"
    if not arquivos.is_dir():
        return "", "", ""
    pdfs_e_txts = {}
    for arq in arquivos.iterdir():
        if arq.suffix.lower() in (".pdf", ".txt"):
            pdfs_e_txts.setdefault(arq.stem, []).append(arq)
    seqs = sorted({_seq(a) for v in pdfs_e_txts.values() for a in v})[:3]

    def prioridade(stem: str) -> tuple:
        n = _norm(stem).replace("_", " ")
        nome_sugere = bool(re.search(r"socioecon|formulario|instaura|\bpaj\b|documentos|ficha", n))
        return (0 if "socioecon" in n else 1 if nome_sugere else 2, _seq(Path(stem)))

    cands = [s for s, v in pdfs_e_txts.items()
             if "socioecon" in _norm(s) or _seq(Path(s)) in seqs]
    for stem in sorted(cands, key=prioridade):
        arqs = pdfs_e_txts[stem]
        txt = next((a for a in arqs if a.suffix.lower() == ".txt"), None)
        pdf = next((a for a in arqs if a.suffix.lower() == ".pdf"), None)
        try:
            if txt:
                texto = txt.read_text(encoding="utf-8", errors="replace")
            elif pdf:
                import pymupdf
                with pymupdf.open(pdf) as d:
                    if d.page_count > 40:  # dossiê grande demais para OCR aqui
                        continue
                texto = _texto_documento(pdf)
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        m = _RE_SOCIOECONOMICO.search(texto or "")
        if m:
            nome = (txt or pdf).name
            return f"{nome} (Formulário Socioeconômico Detalhado)", texto[m.start():m.start() + _MAX_SOCIOECONOMICO], stem
    return "", "", ""


def _texto_documento(pdf: Path) -> str:
    """Texto de um PDF de documento pessoal. A CNH/CIN digital (SENATRAN) traz
    como texto só o carimbo de assinatura ("QR-CODE Documento assinado…") e os
    dados em IMAGEM — o OCR padrão não roda porque "já há texto". Texto curto
    + página com imagens = OCR da página (até 3), somado ao texto nativo."""
    from services import peticao_inicial_service as pis
    texto = pis._texto_pdf(pdf) or ""
    if len(re.sub(r"=== pagina \d+ ===|\s+", "", texto)) >= 700:
        return texto
    chave = (str(pdf) + "#paginas", pdf.stat().st_mtime)
    if chave not in pis._OCR_CACHE:
        extra = []
        with contextlib.suppress(Exception):
            import pymupdf
            from ingestao import ocr
            with pymupdf.open(pdf) as doc:
                for i, pag in enumerate(doc):
                    if i >= 3:
                        break
                    if pag.get_images():
                        t, _ = ocr._ocr_pagina(pag, 40)
                        if t.strip():
                            extra.append(f"=== pagina {i + 1} (OCR das imagens) ===\n{t}")
        pis._OCR_CACHE[chave] = "\n".join(extra)
    return (texto + "\n" + pis._OCR_CACHE[chave]).strip()


def _vazios(p: dict) -> list[str]:
    editados = {c for c, f in (p.get("fontes") or {}).items() if f == "editado"}
    campos = [c for c in CAMPOS_IA if not p.get(c) and c not in editados]
    if p.get("tipo_pessoa") == "PJ":
        campos = []
    elif p.get("menor"):
        campos = [c for c in campos if c not in ("estado_civil", "profissao")]
    end = p.get("endereco") or {}
    # representante sem endereço = mesmo endereço do autor (ver _mesmo_endereco)
    if p.get("papel") == "autor" and "endereco" not in editados and not end.get("logradouro"):
        campos.append("endereco")
    return campos


_PROMPT_IA = """Você vai completar a QUALIFICAÇÃO das partes de uma ação judicial da Defensoria Pública da União, usando SOMENTE os documentos abaixo.

Regras obrigatórias:
- Comece pelo FORMULÁRIO SOCIOECONÔMICO da DPU (arquivo com "SOCIOECONOMICA" no nome), quando houver: é a fonte preferencial de estado civil, profissão, endereço e contatos de cada membro do grupo familiar. Use os demais documentos (identidade, certidões, comprovante de residência) para confirmar ou completar o que ele não trouxer (RG, órgão emissor, filiação, nascimento).
- Preencha apenas os campos listados em "campos_vazios" de cada pessoa. Não altere nada além disso.
- Só preencha se o dado ESTIVER ESCRITO em algum documento e for, com segurança, daquela pessoa (confira nome/CPF). Na dúvida, não preencha.
- Nunca deduza o sexo pelo nome. Sexo só quando o documento disser (campo "sexo", certidão de nascimento) ou quando o próprio documento qualificar a pessoa no feminino/masculino ("brasileira", "solteira", "portadora").
- Formatos: sexo "M" ou "F"; nascimento "dd/mm/aaaa"; nacionalidade, estado civil e profissão em minúsculas, concordando com o sexo (ex.: "brasileira", "solteira", "estoquista"); rg só o número; orgao_rg como "SSP/PR"; nome_mae em maiúsculas.
- profissao: a ocupação declarada; situações como "desempregado(a)", "do lar", "aposentado(a)", "estudante" também valem como profissão na qualificação (concorde com o sexo: "desempregada").
- endereco: objeto {logradouro, numero, complemento, bairro, municipio, uf, cep}, logradouro com o tipo por extenso ("Rua", "Avenida").
- Para cada campo preenchido informe "fonte" (o nome do arquivo, exatamente como aparece no cabeçalho) e "trecho" (até 120 caracteres copiados do documento que comprovam o dado).

Responda SOMENTE com um JSON, sem texto antes ou depois, no formato:
{"pessoas": [{"ref": 0, "campos": {"estado_civil": {"valor": "solteira", "fonte": "arquivo.txt", "trecho": "..."}}}]}

PESSOAS (o que já se sabe e o que falta):
{pessoas}

DOCUMENTOS:
{docs}
"""


def _json_da_resposta(texto: str) -> dict | None:
    t = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", (texto or "").strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(t[i:j + 1])
    return None


async def completar_pelos_documentos(paj_norm: str) -> dict:
    """Preenche, por IA, só os campos vazios (e não editados) com dados dos
    documentos do PAJ; cada valor guarda a fonte ("IA: <arquivo>") e o
    trecho que o comprova (`evidencias`). Devolve a qualificação gravada +
    um resumo do que foi preenchido."""
    q = obter(paj_norm)
    if q is None:
        q = await atualizar_do_sis2(paj_norm)
    pessoas_ia = []
    for i, p in enumerate(q.get("pessoas") or []):
        vazios = _vazios(p)
        if not vazios:
            continue
        conhecido = {k: p.get(k) for k in ("papel", "vinculo", "nome", "cpf", "nascimento", "sexo") if p.get(k)}
        pessoas_ia.append({"ref": i, **conhecido, "campos_vazios": vazios})
    if not pessoas_ia:
        return {"qualificacao": q, "preenchidos": [], "detalhe": "Não há lacunas a completar."}
    docs = await asyncio.to_thread(_docs_para_ia, paj_norm)
    if not docs:
        return {"qualificacao": q, "preenchidos": [],
                "detalhe": "Nenhum documento de identificação/endereço/qualificação com texto foi encontrado no PAJ."}

    prompt = _PROMPT_IA.replace("{pessoas}", json.dumps(pessoas_ia, ensure_ascii=False, indent=1)).replace(
        "{docs}", "\n\n".join(f"=== ARQUIVO: {n} ===\n{t}" for n, t in docs))
    from services.acoes_sisdpu_service import _chamar_claude_texto_sintese
    saida, erro = await _chamar_claude_texto_sintese(prompt, timeout=300)
    if erro:
        raise RuntimeError(f"A IA não respondeu: {erro}"[:400])
    resp = _json_da_resposta(saida)
    if not resp:
        raise RuntimeError("A IA não devolveu o JSON esperado — tente de novo.")

    nomes_docs = {n for n, _ in docs}
    preenchidos = []
    for item in resp.get("pessoas") or []:
        with contextlib.suppress(Exception):
            p = q["pessoas"][int(item.get("ref"))]
            permitidos = set(_vazios(p))
            for campo, dado in (item.get("campos") or {}).items():
                if campo not in permitidos or not isinstance(dado, dict):
                    continue
                fonte = str(dado.get("fonte") or "")
                if fonte not in nomes_docs:  # fonte inventada: descarta
                    continue
                valor = dado.get("valor")
                if campo == "endereco":
                    if not isinstance(valor, dict) or not valor.get("logradouro"):
                        continue
                    p["endereco"] = {c: _limpo(valor.get(c)) for c in CAMPOS_ENDERECO}
                else:
                    valor = _limpo(valor)
                    if campo == "sexo":
                        valor = valor.upper()[:1]
                        if valor not in ("M", "F"):
                            continue
                    if not valor:
                        continue
                    p[campo] = valor
                p.setdefault("fontes", {})[campo] = "IA: " + re.sub(r"^seq\d+\s*-\s*\d+\s*-\s*", "", fonte)
                p.setdefault("evidencias", {})[campo] = _limpo(dado.get("trecho"))[:160]
                preenchidos.append(f"{p.get('nome', '?').split(' ')[0]}: {campo.replace('_', ' ')}")
    q["ia_em"] = datetime.now().isoformat(timespec="seconds")
    q = salvar(paj_norm, q)
    return {"qualificacao": q, "preenchidos": preenchidos,
            "detalhe": (f"{len(preenchidos)} campo(s) preenchido(s) pelos documentos — confira as fontes."
                        if preenchidos else "A IA não achou nos documentos os dados que faltam.")}
