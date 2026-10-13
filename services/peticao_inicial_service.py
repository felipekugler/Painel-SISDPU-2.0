"""Botão "Preparar Petição Inicial no eproc" — fluxo de BENEFÍCIO POR
INCAPACIDADE (único mapeado até agora; outras pretensões têm etapas diferentes
no e-proc e serão ensinadas depois — ver
`Estrutura de Construção/APRENDIZADO_PETICAO_INICIAL_EPROC.md`).

Este módulo só LÊ: monta o "plano" que o modal mostra pro Defensor conferir/
ajustar antes de qualquer automação — arquivos da pasta `Ajuizamento/` com o
tipo de documento do e-proc SUGERIDO para cada um, e os dados do processo
(localidade, rito, classe, valor da causa, partes, NB, especialidade, tutela)
extraídos da petição inicial e do metadata do PAJ. A automação em si fica em
`ingestao.eproc_peticao_inicial`.

Regras (todas ditadas pelo Defensor na aula de 2026-10-08):
- Localidade: Curitiba por padrão (usa a subseção do endereçamento da inicial
  só se for outra localidade conhecida).
- Rito pelo valor da causa: até 60 SM → JUIZADO ESPECIAL FEDERAL + renúncia ao
  excedente; acima → RITO ORDINÁRIO (COMUM), sem renúncia.
- Classe pelo rito: JEF → PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL; ordinário →
  PROCEDIMENTO COMUM (mandado de segurança → MANDADO DE SEGURANÇA, não é BI).
- Valor da causa: o que consta na inicial ("Dá-se à causa o valor de R$ ...").
- NB: o do pedido PRINCIPAL da inicial, só dígitos.
- Especialidade: a(s) indicada(s) na inicial; se mais de uma, a automação
  escolhe a de data estimada de perícia mais próxima.
- Tutela: marcar se a inicial pedir tutela de urgência/antecipada.
- CPF/CEP da qualificação da inicial (ou PAJ); telefone/e-mail do PAJ.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import config

# Salário mínimo vigente por ano (para o teto de 60 SM do JEF). O cálculo do
# valor da causa do caso-escola (2026) usa R$ 1.621,00.
SALARIO_MINIMO = {2025: 1518.00, 2026: 1621.00}

ASSUNTO_BI_URBANO = ("Urbano (art. 60), Auxílio por Incapacidade Temporária, "
                     "Benefícios em Espécie, DIREITO PREVIDENCIÁRIO")

LOCALIDADES = ["Apucarana", "Arapongas", "Campo Mourão", "Cascavel", "Curitiba",
               "Foz do Iguaçu", "Francisco Beltrão", "Guaíra", "Guarapuava",
               "Ivaiporã", "Jacarezinho", "Londrina", "Maringá", "Paranaguá",
               "Paranavaí", "Pato Branco", "Pitanga", "Ponta Grossa",
               "Telêmaco Borba", "Toledo", "Umuarama", "União da Vitória",
               "Wenceslau Braz"]

ESTADOS_CIVIS = ["Solteiro", "Casado", "Divorciado", "Separado", "Viúvo",
                 "União Estável", "Companheiro", "Não Informado"]

# Termo da inicial ("médico especialista em ...") → opção do e-proc.
ESPECIALIDADES = [
    (r"ortoped", "ORTOPEDISTA"), (r"psiquiatr", "PSIQUIATRA"),
    (r"neurolog", "NEUROLOGISTA"), (r"cardiolog", "CARDIOLOGISTA"),
    (r"oftalmolog", "OFTALMOLOGISTA"), (r"oncolog", "ONCOLOGISTA"),
    (r"medicina do trabalho|medico do trabalho", "MEDICINA DO TRABALHO"),
    (r"reumatolog", "REUMATOLOGISTA"), (r"endocrinolog", "ENDOCRINOLOGISTA"),
    (r"pneumolog", "PNEUMOLOGISTA"), (r"nefrolog", "NEFROLOGISTA"),
    (r"otorrino", "OTORRINOLARINGOLOGISTA"), (r"dermatolog", "DERMATOLOGISTA"),
    (r"gastroenterolog", "GASTROENTEROLOGISTA"), (r"infectolog", "INFECTOLOGISTA"),
    (r"urolog", "UROLOGISTA"), (r"ginecolog", "GINECOLOGISTA"),
    (r"hematolog", "HEMATOLOGISTA"), (r"angiolog|vascular", "ANGIOLOGISTA"),
    (r"clinic[oa] geral", "CLÍNICO GERAL"), (r"clinica medica", "CLÍNICA MÉDICA"),
]

# Rótulo padronizado da skill SISDPU-OrganizarArquivos ("NN - Rótulo...") →
# tipo de documento do e-proc. ORDEM IMPORTA (primeira regra que casar vence).
REGRAS_TIPO = [
    (r"peticao inicial", "PETIÇÃO INICIAL"),
    # Antes da identidade: "CTPS e RG" é a carteira de trabalho (pedido do Defensor, 13/10/2026)
    (r"carteira de trabalho|\bctps\b", "CARTEIRA DE TRABALHO - CTPS"),
    (r"\brg\b|\bcpf\b|identidade|\bcnh\b|documento de identifica", "CARTEIRA DE IDENTIDADE"),
    (r"comprovante de residencia|comprovante de endereco", "COMPROVANTE DE RESIDÊNCIA"),
    (r"outorga|procuracao", "PROCURAÇÃO"),
    (r"insuficiencia|hipossuficiencia|pobreza", "DECLARAÇÃO DE HIPOSSUFICIÊNCIA/POBREZA"),
    (r"renuncia", "TERMO DE RENÚNCIA"),
    (r"comunicacao de decisao|indeferimento|carta de decisao", "CARTA DE INDEFERIMENTO"),
    (r"processo administrativo", "PROCESSO ADMINISTRATIVO"),
    (r"pericia|laudo", "LAUDO"),
    (r"formulario", "FORMULÁRIO"),
    (r"exame", "EXAME MÉDICO"),
    (r"receituario|receita medica", "RECEITUÁRIO"),
    (r"prontuario", "PRONTUÁRIO"),
    (r"atestado", "ATESTADO MÉDICO"),
    (r"\bcnis\b|extrato previdenciario", "CADASTRO NACIONAL DE INFORMAÇÕES SOCIAIS - CNIS"),
    (r"declaracao de beneficio|informacao de beneficio|historico de credito", "INFORMAÇÃO DE BENEFÍCIO"),
    (r"calculo", "CÁLCULOS"),
]
TIPO_FALLBACK = "ANEXOS DA PETIÇÃO INICIAL"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().lower()


def _pasta(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _catalogo_tipos() -> list[str]:
    from services import config_painel_service as cps
    try:
        prefs = cps.get_eproc_tipos_doc_prefs()
        return list(prefs.get("tipos_todos") or [])
    except Exception:  # noqa: BLE001
        return []


def sugerir_tipo(nome_arquivo: str, catalogo: list[str] | None = None) -> str:
    """Tipo do e-proc sugerido pelo NOME do arquivo (rótulo da skill de
    organização). Só sugere tipos que existam no catálogo importado (quando
    houver catálogo) — senão cai em "ANEXOS DA PETIÇÃO INICIAL"."""
    rotulo = _norm(re.sub(r"^\d+\s*-\s*", "", Path(nome_arquivo).stem))
    cat_norm = {_norm(t): t for t in (catalogo or [])}
    for padrao, tipo in REGRAS_TIPO:
        if re.search(padrao, rotulo):
            if not cat_norm or _norm(tipo) in cat_norm:
                return cat_norm.get(_norm(tipo), tipo)
    return cat_norm.get(_norm(TIPO_FALLBACK), TIPO_FALLBACK)


def listar_arquivos(paj_norm: str) -> list[dict]:
    pasta = _pasta(paj_norm) / "Ajuizamento"
    if not pasta.is_dir():
        return []
    catalogo = _catalogo_tipos()
    itens = []
    for p in sorted(pasta.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        itens.append({"nome": p.name, "caminho": f"Ajuizamento/{p.name}",
                      "tamanho": p.stat().st_size, "tipo": sugerir_tipo(p.name, catalogo)})
    return itens


def _texto_peticao(paj_norm: str) -> tuple[str, str]:
    """Texto da petição inicial efetivamente organizada para ajuizamento (PDF
    `Ajuizamento/00 - Peticao Inicial*.pdf`, que é a peça que vai pro e-proc);
    cai no .docx de "Peças IA" se o PDF não existir/não tiver texto."""
    pasta = _pasta(paj_norm)
    candidatos = sorted((pasta / "Ajuizamento").glob("*eticao*nicial*.pdf")) if (pasta / "Ajuizamento").is_dir() else []
    for pdf in candidatos:
        try:
            import fitz  # PyMuPDF
            with fitz.open(pdf) as doc:
                txt = "\n".join(pg.get_text() for pg in doc)
            if txt.strip():
                return txt, f"Ajuizamento/{pdf.name}"
        except Exception:  # noqa: BLE001
            pass
    docx = pasta / "Peças IA" / "peticao_inicial.docx"
    if docx.exists():
        try:
            import docx as _docx
            d = _docx.Document(str(docx))
            return "\n".join(par.text for par in d.paragraphs), "Peças IA/peticao_inicial.docx"
        except Exception:  # noqa: BLE001
            pass
    return "", ""


def _valor_brl(s: str) -> float | None:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _formatar_brl(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _secao_pedidos(texto: str) -> str:
    m = re.search(r"DOS\s+PEDIDOS", texto, re.IGNORECASE)
    return texto[m.end():] if m else texto


def extrair_dados(paj_norm: str) -> dict:
    texto, fonte = _texto_peticao(paj_norm)
    t1 = re.sub(r"[ \t]+", " ", texto)
    tl = re.sub(r"\s+", " ", t1)  # linha única (quebras de linha do PDF no meio de números)
    tn = _norm(tl)
    avisos: list[str] = []

    meta = {}
    with_meta = _pasta(paj_norm) / "metadata.json"
    if with_meta.exists():
        try:
            meta = json.loads(with_meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}

    # Valor da causa
    valor = ""
    m = re.search(r"causa\s+o\s+valor\s+de\s+R\$\s*([\d\.]+,\d{2})", tl, re.IGNORECASE)
    if m:
        valor = m.group(1)
    else:
        avisos.append("Valor da causa não encontrado na inicial (\"Dá-se à causa o valor de R$ ...\").")
    valor_num = _valor_brl(valor) if valor else None

    # Rito (60 SM)
    from datetime import date
    sm = SALARIO_MINIMO.get(date.today().year) or SALARIO_MINIMO[max(SALARIO_MINIMO)]
    teto = 60 * sm
    jef = True if valor_num is None else valor_num <= teto
    rito = "JUIZADO ESPECIAL FEDERAL" if jef else "RITO ORDINÁRIO (COMUM)"
    classe = "PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL" if jef else "PROCEDIMENTO COMUM"

    # Localidade
    localidade = "Curitiba"
    m = re.search(r"subse[cç][aã]o\s+judici[aá]ria\s+de\s+([A-Za-zÀ-ú ]+?)\s*/\s*PR", tl, re.IGNORECASE)
    if m:
        cand = m.group(1).strip()
        achada = next((l for l in LOCALIDADES if _norm(l) == _norm(cand)), None)
        if achada:
            localidade = achada

    # Qualificação
    cpf = ""
    m = re.search(r"CPF[^\d]{0,30}(\d{3}\.?\d{3}\.?\d{3}-?\d{2})", tl)
    if m:
        cpf = re.sub(r"\D", "", m.group(1))
    cep = ""
    m = re.search(r"CEP\s*(\d{2}\.?\d{3})\s*-\s*(\d{3})", tl)
    if m:
        cep = re.sub(r"\D", "", m.group(1) + m.group(2))
    numero_end, complemento = "", ""
    m = re.search(r"n[º°o]\.?\s*(\d+[A-Za-z]?)\s*,\s*(.*?),\s*Bairro", tl, re.IGNORECASE)
    if m:
        numero_end, complemento = m.group(1), m.group(2).strip()
        if re.search(r"CEP|Curitiba", complemento, re.IGNORECASE):
            complemento = ""
    else:
        m = re.search(r"n[º°o]\.?\s*(\d+[A-Za-z]?)\s*,", tl)
        if m:
            numero_end = m.group(1)

    qual = tn[:1500]
    estado_civil = "Não Informado"
    for pad, ec in [(r"\bsolteir", "Solteiro"), (r"\bcasad", "Casado"), (r"\bdivorciad", "Divorciado"),
                    (r"\bseparad", "Separado"), (r"\bviuv", "Viúvo"), (r"uniao estavel", "União Estável"),
                    (r"\bcompanheir", "Companheiro")]:
        if re.search(pad, qual):
            estado_civil = ec
            break
    sexo = ""
    if re.search(r"\bbrasileira\b|\binscrita\b|\bportadora\b|\bresidente e domiciliada\b", qual):
        sexo = "Feminino"
    elif re.search(r"\bbrasileiro\b|\binscrito\b|\bportador\b|\bdomiciliado\b", qual):
        sexo = "Masculino"

    telefones = meta.get("telefones") or []
    tel = ""
    for t in telefones:
        if re.search(r"requerente|assistid", (t.get("obs") or ""), re.IGNORECASE):
            tel = t.get("numero") or ""
            break
    if not tel and telefones:
        tel = telefones[0].get("numero") or ""
    tel = re.sub(r"\D", "", tel)
    emails = meta.get("emails") or []
    email = emails[0] if emails else ""
    if not cpf:
        cpf = re.sub(r"\D", "", ((meta.get("sisdpu_raw") or {}).get("cpf_assistido") or ""))
    if not cep:
        ends = meta.get("enderecos") or []
        if ends:
            cep = re.sub(r"\D", "", ends[0].get("cep") or "")

    # NB do pedido principal
    pedidos = re.sub(r"\s+", " ", _secao_pedidos(t1))
    nb = ""
    m = re.search(r"NB\s*([\d\.\s]{9,15}-?\s*\d)", pedidos) or re.search(r"NB\s*([\d\.\s]{9,15}-?\s*\d)", tl)
    if m:
        nb = re.sub(r"\D", "", m.group(1))
    else:
        avisos.append("NB do pedido principal não encontrado na inicial.")

    # Especialidade(s)
    especialidades: list[str] = []
    for mm in re.finditer(r"especialista\s+em\s+([^;:\.]{3,120})", _norm(pedidos) or tn):
        trecho = mm.group(1)
        for pad, esp in ESPECIALIDADES:
            if re.search(pad, trecho) and esp not in especialidades:
                especialidades.append(esp)
    if not especialidades:
        avisos.append("Especialidade da perícia não indicada expressamente na inicial — escolha no modal.")

    tutela = bool(re.search(r"tutela\s+de\s+urgencia|tutela\s+antecipada|antecipacao\s+(dos\s+efeitos\s+)?da\s+tutela",
                            _norm(pedidos)))

    titulo = "ação de benefício por incapacidade" + (", com pedido de tutela de urgência" if tutela else "")
    m = re.search(r"ajuizar\s+(A[ÇC][ÃA]O\s+DE\s+.+?)\s+em\s+face", tl, re.IGNORECASE)
    if m and len(m.group(1)) < 200:
        titulo = re.sub(r"\s*,\s*", ", ", m.group(1).strip().rstrip(",")).lower()
    descricao_sisdpu = (f"Ajuizada {titulo}, em face do INSS.\n\n"
                        "Nº Processo: [PROCESSO]\nChave para Consulta: [CHAVE]")

    nome = ((meta.get("sisdpu_raw") or {}).get("assistidos_pf") or [{}])[0].get("nome") or meta.get("assistido_caixa") or ""
    docx_rel = "Peças IA/peticao_inicial.docx" if (_pasta(paj_norm) / "Peças IA" / "peticao_inicial.docx").exists() else ""

    return {
        "fonte_peticao": fonte, "avisos": avisos,
        "localidade": localidade, "rito": rito, "area": "Previdenciária", "classe": classe,
        "sigilo": "Sem Sigilo (Nível 0)", "valor_causa": valor, "renuncia_60sm": jef,
        "teto_jef": _formatar_brl(teto), "salario_minimo": _formatar_brl(sm),
        "assunto": ASSUNTO_BI_URBANO,
        "autor": {"nome": nome, "cpf": cpf, "cep": cep, "numero": numero_end, "complemento": complemento,
                  "estado_civil": estado_civil, "sexo": sexo, "telefone": tel, "email": email},
        "nb": nb, "especialidades": especialidades, "tutela": tutela,
        "docx_peticao": docx_rel, "descricao_sisdpu": descricao_sisdpu,
    }


MARCADOR_AJUIZADA = "peticao_inicial_ajuizada.json"


def _ler_marcador(paj_norm: str) -> list[dict]:
    p = _pasta(paj_norm) / MARCADOR_AJUIZADA
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return d if isinstance(d, list) else [d]


def registrar_ajuizamento(paj_norm: str, resultado: dict) -> None:
    """Acrescenta o ajuizamento ao marcador da pasta do PAJ (lista — um caso
    pode ter mais de uma inicial, ex. após extinção sem mérito). Faz o modal
    avisar na hora, sem esperar a sincronização trazer a movimentação."""
    from datetime import datetime
    dados = {k: resultado.get(k, "") for k in ("numero", "chave", "classe", "magistrado")}
    dados["ajuizado_em"] = datetime.now().isoformat(timespec="seconds")
    lista = _ler_marcador(paj_norm) + [dados]
    (_pasta(paj_norm) / MARCADOR_AJUIZADA).write_text(
        json.dumps(lista, ensure_ascii=False, indent=2), encoding="utf-8")


def ajuizamento_anterior(paj_norm: str, movimentacoes: list[dict] | None = None) -> dict | None:
    """Dados do ajuizamento anterior, se a inicial deste PAJ já foi ajuizada:
    marcador do botão e/ou movimentação "Petição inicial" no SISDPU. None se
    nunca foi."""
    marc = _ler_marcador(paj_norm)
    if movimentacoes is None:
        from services import paj_service
        movimentacoes = (paj_service.ler_paj(paj_norm) or {}).get("movimentacoes") or []
    movs = [m for m in movimentacoes if "peticao inicial" in _norm(m.get("fases") or m.get("fase") or "")]
    if not marc and not movs:
        return None
    partes = []
    for m in marc:
        partes.append(f"Ajuizado pelo painel em {m.get('ajuizado_em', '')[:10]}: processo {m.get('numero')} (chave {m.get('chave')}).")
    if movs:
        datas = ", ".join(sorted({(m.get("data") or "")[:10] for m in movs}))
        partes.append(f"Movimentação \"Petição inicial\" no SISDPU em: {datas}.")
    # Nº do(s) processo(s) ajuizado(s): marcador do botão + "Nº Processo: ..."
    # na descrição da movimentação "Petição inicial" do SISDPU.
    processos: list[str] = [m.get("numero") for m in marc if m.get("numero")]
    for m in movs:
        texto = re.sub(r"<[^>]+>", " ", m.get("descricao") or "")
        for n in re.findall(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", texto):
            if n not in processos:
                processos.append(n)
    return {"detalhe": " ".join(partes), "ajuizamentos": marc, "n_movimentacoes": len(movs),
            "processos": processos}


def inicial_ajuizada(paj_norm: str, movimentacoes: list[dict] | None = None) -> bool:
    return ajuizamento_anterior(paj_norm, movimentacoes) is not None


def montar_plano(paj_norm: str) -> dict:
    pasta = _pasta(paj_norm)
    if not pasta.is_dir():
        return {"ok": False, "erro": "Pasta do PAJ não encontrada."}
    arquivos = listar_arquivos(paj_norm)
    if not arquivos:
        return {"ok": False, "erro": "Pasta \"Ajuizamento\" vazia ou inexistente — organize os documentos antes."}
    dados = extrair_dados(paj_norm)
    return {"ok": True, "arquivos": arquivos, "dados": dados, "ja_ajuizada": ajuizamento_anterior(paj_norm),
            "tipos_todos": _catalogo_tipos(), "estados_civis": ESTADOS_CIVIS,
            "especialidades_conhecidas": sorted({e for _, e in ESPECIALIDADES}),
            "localidades": LOCALIDADES}
