"""Botão "Preparar Petição Inicial no eproc" — dois fluxos do e-proc (ver
`Estrutura de Construção/APRENDIZADO_PETICAO_INICIAL_EPROC.md`):
- "bi": benefício por incapacidade (formulário próprio: NB + especialidade,
  INSS fixo, 4 etapas) — aula de 2026-10-08;
- "generico": outros assuntos (partes autoras + partes rés, 5 etapas) — aula
  de 2026-10-23, caso-escola BPC.

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

import contextlib
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

# Fluxo GENÉRICO (outros assuntos — aula de 2026-10-23, caso-escola BPC): o
# assunto é a sugestão da IA do e-proc que bate com o pedido (início do texto
# da sugestão). Sem regra conhecida, fica vazio e a automação aceita a 1ª
# sugestão da IA (o Defensor confere no modal e na tela).
ASSUNTO_BPC_DEFICIENCIA = "Pessoa com deficiência, Benefício Assistencial (Art. 203,V CF/88)"
ASSUNTO_BPC_IDOSO = "Idoso, Benefício Assistencial (Art. 203,V CF/88)"  # visto na IA do e-proc, 28/10/2026
_RE_BPC = r"prestacao continuada|\bbpc\b|\bloas\b|assistencial|amparo social"

# Representação processual (aula de 2026-10-28, PAJ 2026/029-02717 — curatela).
# No e-proc: o AUTOR recebe um tipo do grupo "Representados" e o
# representante entra como outra parte da tabela de autores, com um tipo do
# grupo "Representantes" (só aparece depois que há um representado) e
# "Atua também como parte?" = Não. Regras do Defensor.
REPRESENTACAO_AUTOR_CURATELA = "Civilmente Incapaz - Art. 110, 8.213/91"
REPRESENTACAO_CURADOR = "Curador"

# Opções da janela "Informações da Representação" do e-proc (JFPR, lidas em
# 28/10/2026) — dropdowns do modal. Grupo "Representados" (para o autor):
REPRESENTADOS_EPROC = [
    "Absolutamente Incapaz (Art. 3º CC)", "Assistente", "Chamante",
    "Civilmente Incapaz - Art. 110, 8.213/91", "Denunciante", "Em Liquidação Extrajudicial",
    "Em Liquidação Judicial", "Em Recuperação Judicial", "Espólio", "Insolvência", "Opoente",
    "Reconvinte", "Relativamente Incapaz (Art. 4º CC)", "Representado - art. 10, Lei 10.259/2001",
    "Representado Ação Coletiva", "Sucessão",
]
# Grupo "Representantes" (para o representante; só aparece depois que há um
# representado) — DEPENDE do tipo do representado (visto ao vivo, 03088,
# 30/10/2026): com "Absolutamente/Relativamente Incapaz" vêm Tutor, Pais,
# Curador e Representante; com "Civilmente Incapaz - Art. 110" vêm "Cônjuge,
# pai, mãe, tutor, curador ou herdeiro necessário" e Curador.
REPRESENTANTES_EPROC = ["Pais", "Tutor", "Curador", "Representante",
                        "Cônjuge, pai, mãe, tutor, curador ou herdeiro necessário"]

REU_INSS = "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS"

# Entidades (réus) do e-proc — a lista do botão "Listar todas as entidades"
# da etapa Partes (Réus), capturada pela automação a cada preparação
# (eproc_peticao_inicial._etapa4_reus_generico) e oferecida no card do réu
# do modal (pedido do Defensor, 30/10/2026). Sem captura ainda: só o INSS.
_ARQ_ENTIDADES = Path(__file__).resolve().parents[2] / "dados" / "eproc_entidades.json"


def entidades_eproc() -> dict:
    with contextlib.suppress(Exception):
        d = json.loads(_ARQ_ENTIDADES.read_text(encoding="utf-8"))
        if d.get("entidades"):
            return d
    return {"entidades": [REU_INSS], "capturado_em": ""}


def gravar_entidades_eproc(nomes: list[str]) -> None:
    """Mantém a ORDEM do e-proc: as mais usadas (INSS, União, CEF, MPF…) vêm
    primeiro, depois o resto em ordem alfabética (3.133 na JFPR, 30/10/2026)."""
    vistos, ordem = set(), []
    for n in nomes:
        n = re.sub(r"\s+", " ", n or "").strip()
        if n and n not in vistos:
            vistos.add(n)
            ordem.append(n)
    nomes = ordem
    if len(nomes) < 2:
        return
    from datetime import datetime
    _ARQ_ENTIDADES.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_ENTIDADES.write_text(json.dumps({"entidades": nomes, "capturado_em": datetime.now().isoformat(timespec="seconds")},
                                         ensure_ascii=False, indent=1), encoding="utf-8")

# Nacionalidade na qualificação ("FULANO, venezuelano, solteiro, ...") →
# opção do e-proc (cadastro de pessoa física). Brasileira é o padrão.
NACIONALIDADES = [
    (r"venezuelan", "Venezuelano"), (r"haitian", "Haitiano"), (r"boliviano|boliviana", "Boliviano"),
    (r"paraguai", "Paraguaio"), (r"argentin", "Argentino"), (r"colombian", "Colombiano"),
    (r"peruan", "Peruano"), (r"cuban", "Cubano"), (r"uruguai", "Uruguaio"), (r"chilen", "Chileno"),
    (r"angolan", "Angolano"), (r"senegales", "Senegalês"), (r"sirio|siria", "Sírio"),
    (r"afeg", "Afegão"), (r"ucranian", "Ucraniano"), (r"portugu", "Português"),
]

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
    # Inicial de OUTRO processo (ex. "Peticao Inicial Acao de Interdicao - autos ...") não é a desta ação
    (r"peticao inicial .*(acao de|autos|interdicao|processo)", "PETIÇÃO"),
    (r"peticao inicial", "PETIÇÃO INICIAL"),
    (r"curatela|interdicao", "TERMO DE CURATELA"),
    # Antes da identidade: "CTPS e RG" é a carteira de trabalho (pedido do Defensor, 13/10/2026)
    (r"carteira de trabalho|\bctps\b", "CARTEIRA DE TRABALHO - CTPS"),
    # Antes do CadÚnico: "PROCADM Revisao de BPC CadUnico" é o processo administrativo
    (r"processo administrativo|\bprocadm\b|^pa\b", "PROCESSO ADMINISTRATIVO"),
    # CadÚnico: folha resumo = EXTRATO; comprovante de cadastro = COMPROVANTES (23/10/2026)
    (r"cad ?unico|cadastro unico", None),
    (r"\brnm\b|\brne\b|estrangeir", "DOCUMENTO DE IDENTIFICAÇÃO"),
    (r"certidao de nascimento", "CERTIDÃO DE NASCIMENTO"),
    (r"certidao de casamento", "CERTIDÃO DE CASAMENTO"),
    (r"certidao de obito", "CERTIDÃO DE ÓBITO"),
    (r"\brg\b|\bcpf\b|\bcin\b|identidade|\bcnh\b|documento de identifica", "CARTEIRA DE IDENTIDADE"),
    (r"comprovante de residencia|comprovante de endereco", "COMPROVANTE DE RESIDÊNCIA"),
    (r"outorga|procuracao", "PROCURAÇÃO"),
    (r"insuficiencia|hipossuficiencia|pobreza", "DECLARAÇÃO DE HIPOSSUFICIÊNCIA/POBREZA"),
    (r"renuncia", "TERMO DE RENÚNCIA"),
    (r"carta de concessao", "CARTA DE CONCESSÃO"),
    (r"comunicacao de decisao|indeferimento|carta de decisao", "CARTA DE INDEFERIMENTO"),
    (r"pericia|laudo", "LAUDO"),
    (r"formulario|solicitacao de informacoes medicas", "FORMULÁRIO"),
    (r"exame", "EXAME MÉDICO"),
    (r"receituario|receita medica|prescric", "RECEITUÁRIO"),
    (r"\boficio\b", "OFÍCIO"),
    (r"prontuario", "PRONTUÁRIO"),
    (r"atestado|declaracao medica", "ATESTADO MÉDICO"),
    (r"documentos? medicos?|relatorios? medicos?", "LAUDO"),
    (r"holerite|contracheque", "CONTRACHEQUE"),
    (r"\bgps\b|guia da previdencia", "GUIA DA PREVIDÊNCIA SOCIAL - GPS"),
    (r"foto", "FOTO"),
    (r"\bcnis\b|extrato previdenciario", "CADASTRO NACIONAL DE INFORMAÇÕES SOCIAIS - CNIS"),
    (r"declaracao de beneficio|informacao de beneficio|historico de credito", "INFORMAÇÃO DE BENEFÍCIO"),
    (r"calculo", "CÁLCULOS"),
]
# Sem regra específica: tipo GENÉRICO pelo nome (pedido do Defensor,
# 30/10/2026) — certidões/declarações/termos → DECLARAÇÃO; comprovantes,
# recibos, extratos, guias → COMPROVANTES. Nem isso: OUTROS, que no e-proc
# abre o campo "Observação" (#txtObservacao_<n>, até 100 caracteres),
# preenchido pela automação com o nome do arquivo sem a extensão (aprendido
# ao vivo no PAJ 2026/029-03088, 30/10/2026). "ANEXOS DA PETIÇÃO INICIAL"
# não se usa (regra de 23/10/2026).
REGRAS_TIPO_GENERICO = [
    (r"certid|declara|termo|atestado|oficio|comunicad|carta", "DECLARAÇÃO"),
    (r"comprovante|recibo|nota fiscal|extrato|\bguia\b|boleto|fatura|conta de|contracheque|holerite", "COMPROVANTES"),
]
TIPO_FALLBACK = "OUTROS"


def _tipo_cadunico(rotulo: str) -> str:
    return "EXTRATO" if re.search(r"folha resumo|extrato", rotulo) else "COMPROVANTES"


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
    houver catálogo) — senão fica vazio, para escolher no modal."""
    rotulo = _norm(re.sub(r"^\d+\s*-\s*", "", Path(nome_arquivo).stem).replace("_", " "))
    cat_norm = {_norm(t): t for t in (catalogo or [])}
    for padrao, tipo in REGRAS_TIPO + REGRAS_TIPO_GENERICO:
        if re.search(padrao, rotulo):
            tipo = tipo or _tipo_cadunico(rotulo)
            if not cat_norm or _norm(tipo) in cat_norm:
                return cat_norm.get(_norm(tipo), tipo)
    return cat_norm.get(_norm(TIPO_FALLBACK), TIPO_FALLBACK) if (not cat_norm or _norm(TIPO_FALLBACK) in cat_norm) else ""


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


def _pdfs_peticao(paj_norm: str) -> list[Path]:
    """PDF(s) da petição inicial na pasta Ajuizamento — a versão FINAL e
    corrigida que vai pro e-proc (regra do Defensor, 30/10/2026). Casa o nome
    sem acento ("Petição Inicial_comValorDaCausa.pdf", "peticao_inicial.pdf")
    e ignora a inicial de OUTRO processo ("... Acao de Interdicao - autos").
    O de prefixo "00" vem primeiro."""
    pasta = _pasta(paj_norm) / "Ajuizamento"
    if not pasta.is_dir():
        return []
    achados = []
    for pdf in pasta.glob("*.pdf"):
        n = _norm(pdf.stem).replace("_", " ")
        if re.search(r"peticao ?inicial", n) and not re.search(r"acao de|autos|interdicao|processo", n):
            achados.append(pdf)
    return sorted(achados, key=lambda p: (not p.name.startswith("00"), p.name.lower()))


def _texto_peticao(paj_norm: str) -> tuple[str, str]:
    """Texto da petição inicial: SEMPRE o PDF da pasta Ajuizamento (versão
    final); o .docx de "Peças IA" só como último recurso — o plano avisa."""
    pasta = _pasta(paj_norm)
    for pdf in _pdfs_peticao(paj_norm):
        try:
            import pymupdf
            with pymupdf.open(pdf) as doc:
                txt = "\n".join(pg.get_text() for pg in doc)
            if not txt.strip():  # PDF só imagem: OCR do painel
                txt = _texto_pdf(pdf)
            if txt.strip():
                return txt, f"Ajuizamento/{pdf.name}"
        except Exception:  # noqa: BLE001
            pass
    docx_rel = _docx_peticao(paj_norm)
    if docx_rel:
        try:
            import docx as _docx
            d = _docx.Document(str(pasta / docx_rel))
            return "\n".join(par.text for par in d.paragraphs), docx_rel
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


def _fluxo(titulo_norm: str, pedidos_norm: str) -> str:
    """"bi" (formulário próprio do e-proc: NB + especialidade, INSS fixo, 4
    etapas) ou "generico" (partes autoras + rés, 5 etapas). Decide pelo
    título da ação; sem título, pelos pedidos."""
    alvo = titulo_norm or pedidos_norm
    bi = r"incapacidade|auxilio[- ]doenca|aposentadoria por invalidez|auxilio[- ]acidente"
    if re.search(_RE_BPC, alvo):
        return "generico"
    return "bi" if re.search(bi, alvo) else "generico"


def _assunto_generico(titulo_norm: str, texto_norm: str) -> str:
    if not re.search(_RE_BPC, titulo_norm or texto_norm):
        return ""
    trecho = titulo_norm + " " + texto_norm[:6000]
    if re.search(r"pessoa com deficiencia", trecho):
        return ASSUNTO_BPC_DEFICIENCIA
    if re.search(r"\bidos[oa]\b", trecho):
        return ASSUNTO_BPC_IDOSO
    return ""


def _docx_peticao(paj_norm: str) -> str:
    """.docx da inicial em "Peças IA" ("peticao_inicial.docx" ou "peticao
    inicial.docx" — os dois nomes aparecem)."""
    pasta = _pasta(paj_norm) / "Peças IA"
    if not pasta.is_dir():
        return ""
    cands = sorted(p for p in pasta.glob("*.docx") if re.fullmatch(r"peticao[ _]inicial", _norm(p.stem)))
    return f"Peças IA/{cands[0].name}" if cands else ""


_OCR_CACHE: dict[tuple[str, float], str] = {}


def _texto_pdf(pdf: Path) -> str:
    """Texto do PDF; OCR do painel se for só imagem (termo de curatela
    escaneado ~6 s — guardado em memória por arquivo/mtime)."""
    chave = (str(pdf), pdf.stat().st_mtime)
    if chave not in _OCR_CACHE:
        try:
            from ingestao import ocr
            _OCR_CACHE[chave] = ocr.extrair_texto(pdf)
        except Exception:  # noqa: BLE001
            _OCR_CACHE[chave] = ""
    return _OCR_CACHE[chave]


def _doc_formatado(d: str) -> str:
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def _representante(paj_norm: str, texto_inicial: str) -> dict:
    """Curador/representante do autor. Só quando a inicial fala em curatela,
    interdição ou representação. O nomeado e o CPF/CNPJ vêm do TERMO DE
    CURATELA da pasta Ajuizamento ("nomeou FULANO (CPF/CNPJ: ...)") — quem o
    termo nomeia é o curador, mesmo que a inicial cite a pessoa física que
    representa a instituição (regra do Defensor, 28/10/2026); senão, CPF
    perto de "curador" na própria inicial."""
    tn = _norm(texto_inicial)
    if not re.search(r"\bcurador|\bcuratela|\binterdi[cç]|\bcuratelad", tn):
        return {"presente": False}
    rep = {"presente": True, "nome": "", "documento": "", "tipo_pessoa": "PF",
           "tipo_autor": REPRESENTACAO_AUTOR_CURATELA, "tipo_representante": REPRESENTACAO_CURADOR,
           "principal": False, "atua_como_parte": False, "fonte": ""}
    pasta = _pasta(paj_norm) / "Ajuizamento"
    termos = [p for p in sorted(pasta.glob("*.pdf")) if re.search(r"curatela|curador", _norm(p.stem))] if pasta.is_dir() else []
    for pdf in termos:
        txt = re.sub(r"\s+", " ", _texto_pdf(pdf))
        m = re.search(r"nomeou\s+(?:o\s+|a\s+|o\(a\)\s+)?(?:sr\.?a?\s+)?(.+?)\s*\(\s*(?:RG[^)]*?\s+e\s+)?(?:CPF|CNPJ)\s*:?\s*"
                      r"([\d][\d\.\s/-]{9,20}\d)", txt, re.IGNORECASE)
        if m:
            rep["nome"] = m.group(1).strip(" ,")
            rep["documento"] = re.sub(r"\D", "", m.group(2))
            rep["fonte"] = f"Ajuizamento/{pdf.name}"
            break
    if not rep["documento"]:
        m = re.search(r"curador[a]?(?:\s+provis[oó]ri[oa])?\s*,?\s*(?:o\s+|a\s+|sr\.?a?\s+|pe\.\s+)?([A-ZÀ-Ú][^,]{3,80}?),"
                      r"[^.]{0,200}?(?:CPF|CNPJ)[^\d]{0,30}([\d\./-]{14,18})", texto_inicial)
        if m:
            rep["nome"], rep["documento"] = m.group(1).strip(), re.sub(r"\D", "", m.group(2))
            rep["fonte"] = "petição inicial"
    if len(rep["documento"]) == 14:
        rep["tipo_pessoa"] = "PJ"
    rep["documento"] = _doc_formatado(rep["documento"])
    return rep


def extrair_dados(paj_norm: str) -> dict:
    texto, fonte = _texto_peticao(paj_norm)
    t1 = re.sub(r"[ \t]+", " ", texto)
    tl = re.sub(r"\s+", " ", t1)  # linha única (quebras de linha do PDF no meio de números)
    tn = _norm(tl)
    avisos: list[str] = []
    if not fonte:
        avisos.append("Petição inicial não encontrada na pasta Ajuizamento (nem em Peças IA) — os dados não puderam ser lidos.")
    elif not fonte.startswith("Ajuizamento/"):
        avisos.append(f"Não há PDF da petição inicial na pasta Ajuizamento — os dados foram lidos de \"{fonte}\", "
                      "que pode não ser a versão final. Confira.")

    meta = {}
    with_meta = _pasta(paj_norm) / "metadata.json"
    if with_meta.exists():
        try:
            meta = json.loads(with_meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}

    # Valor da causa
    valor = ""
    # "Dá-se à causa o valor de R$ ..." ou "Valor da causa: R$ ..." (PAJ 2026/029-02717)
    m = (re.search(r"causa\s+o\s+valor\s+de\s+R\$\s*([\d\.]+,\d{2})", tl, re.IGNORECASE)
         or re.search(r"valor\s+da\s+causa\s*(?::|de|e\s+de)?\s*R\$\s*([\d\.]+,\d{2})", tl, re.IGNORECASE))
    if m:
        valor = m.group(1)
    else:
        avisos.append("Valor da causa não encontrado na inicial (\"Dá-se à causa o valor de R$ ...\" / \"Valor da causa: R$ ...\").")
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

    nacionalidade = ""
    m = re.search(r",\s*([a-z]+)\s*,", qual)  # "FULANO , venezuelano, solteiro, ..."
    if m:
        nacionalidade = next((n for pad, n in NACIONALIDADES if re.search(pad, m.group(1))), "")

    pedidos = re.sub(r"\s+", " ", _secao_pedidos(t1))
    titulo_acao = ""
    m = re.search(r"(?:ajuizar|propor)\s+(A[ÇC][ÃA]O\s+DE\s+.+?)\s+em\s+face", tl, re.IGNORECASE)
    if m and len(m.group(1)) < 200:
        titulo_acao = m.group(1)
    fluxo = _fluxo(_norm(titulo_acao), _norm(pedidos))

    # NB do pedido principal
    nb = ""
    m = re.search(r"NB\s*([\d\.\s]{9,15}-?\s*\d)", pedidos) or re.search(r"NB\s*([\d\.\s]{9,15}-?\s*\d)", tl)
    if m:
        nb = re.sub(r"\D", "", m.group(1))
    elif fluxo == "bi":
        avisos.append("NB do pedido principal não encontrado na inicial.")

    # Especialidade(s)
    especialidades: list[str] = []
    for mm in re.finditer(r"especialista\s+em\s+([^;:\.]{3,120})", _norm(pedidos) or tn):
        trecho = mm.group(1)
        for pad, esp in ESPECIALIDADES:
            if re.search(pad, trecho) and esp not in especialidades:
                especialidades.append(esp)
    if not especialidades and fluxo == "bi":
        avisos.append("Especialidade da perícia não indicada expressamente na inicial — escolha no modal.")

    tutela = bool(re.search(r"tutela\s+de\s+urgencia|tutela\s+antecipada|antecipacao\s+(dos\s+efeitos\s+)?da\s+tutela",
                            _norm(pedidos)))

    if fluxo == "bi":
        assunto = ASSUNTO_BI_URBANO
    else:
        assunto = _assunto_generico(_norm(titulo_acao), tn)
        if not assunto:
            avisos.append("Assunto não reconhecido pela pretensão — a automação aceita a 1ª sugestão da IA do e-proc; "
                          "confira (ou digite o início do assunto desejado).")

    titulo = "ação de benefício por incapacidade" + (", com pedido de tutela de urgência" if tutela else "")
    if titulo_acao:
        titulo = re.sub(r"\s*,\s*", ", ", titulo_acao.strip().rstrip(",")).lower()
    descricao_sisdpu = (f"Ajuizada {titulo}, em face do INSS.\n\n"
                        "Nº Processo: [PROCESSO]\nChave para Consulta: [CHAVE]")

    nome = ((meta.get("sisdpu_raw") or {}).get("assistidos_pf") or [{}])[0].get("nome") or meta.get("assistido_caixa") or ""
    docx_rel = _docx_peticao(paj_norm)
    representante = _representante(paj_norm, tl)
    if representante.get("presente") and not representante.get("documento"):
        avisos.append("A inicial menciona curatela/representação, mas o CPF/CNPJ do representante não foi achado "
                      "(nem no termo de curatela) — preencha no modal.")

    dados = {
        "fonte_peticao": fonte, "avisos": avisos,
        "localidade": localidade, "rito": rito, "area": "Previdenciária", "classe": classe,
        "sigilo": "Sem Sigilo (Nível 0)", "valor_causa": valor, "renuncia_60sm": jef,
        "teto_jef": _formatar_brl(teto), "salario_minimo": _formatar_brl(sm),
        "fluxo": fluxo, "assunto": assunto, "reu": REU_INSS,
        "autor": {"nome": nome, "cpf": cpf, "cep": cep, "numero": numero_end, "complemento": complemento,
                  "estado_civil": estado_civil, "sexo": sexo, "nacionalidade": nacionalidade,
                  "telefone": tel, "email": email},
        "representante": representante,
        "nb": nb, "especialidades": especialidades, "tutela": tutela,
        "docx_peticao": docx_rel, "descricao_sisdpu": descricao_sisdpu,
        "fonte_partes": "petição inicial (leitura do PDF)",
    }
    _aplicar_qualificacao(paj_norm, dados)
    dados["info_adicionais"] = _info_adicionais(paj_norm, dados, tn)
    dados["assunto_codigo"] = _codigo_do_assunto(assunto, dados["area"], classe)
    dados["assuntos_complementares"] = []
    return dados


# Opções da última etapa do e-proc ("Informações Adicionais"), editáveis no
# modal (seção Processo) — pedido do Defensor, 30/10/2026. (id, rótulo, fluxo:
# "" = os dois, "bi" = só no formulário do benefício por incapacidade).
INFO_ADICIONAIS = [
    ("chkCautAntecTutela", "Requer Liminar/Antecipação de Tutela", ""),
    ("chkCrianca", "Prioridade de tramitação - Criança e Adolescente", ""),
    ("chkIntervMinisterioPublico", "Intervenção do Ministério Público", ""),
    ("chkDoencaGrave", "Prioridade de tramitação - Doença grave", ""),
    ("chkDeficiente", "Prioridade de tramitação - Pessoa com deficiência", ""),
    ("chkPessoaComTEA", "Prioridade de tramitação - Pessoa com TEA", ""),
    ("chkPessoaLei14289", "Pessoa enquadrada na Lei 14.289", ""),
    ("chkJuizoDigital", "Opção por Juízo 100% Digital", ""),
    ("chkDemandaComplexa", "Processo estrutural e/ou complexo", ""),
    ("chkAtendeReqLei8213_91", "Atende ao art. 129-A, I, a-c, da Lei 8.213/91", "bi"),
    ("chkDesinteresseTA", "Desinteresse na Tramitação Ágil", "bi"),
]


# Tabela de assuntos do e-proc (aula de 2026-10-30, PAJ 2026/029-03088). Na
# etapa 2 há um jsTree (#divArvore), carregado sob demanda: ANTES do principal
# ele traz só os assuntos da área/classe (148 nós em Previdenciária + JEF);
# DEPOIS, a tabela inteira do CNJ para os "Demais Assuntos" (2.471 nós). O
# id de cada nó é o código; `incluirAssunto(código)` inclui (o 1º vira o
# principal). A automação captura as duas árvores; o modal as oferece.
_ARQ_ASSUNTOS = Path(__file__).resolve().parents[2] / "dados" / "eproc_assuntos.json"
COMPLEMENTARES_VALIDADE_DIAS = 30


def _chave_assuntos(area: str, classe: str) -> str:
    return f"{_norm(area)}|{_norm(classe)}"


def _ler_assuntos() -> dict:
    with contextlib.suppress(Exception):
        return json.loads(_ARQ_ASSUNTOS.read_text(encoding="utf-8"))
    return {}


def _itens_assuntos(nos: list[dict]) -> list[dict]:
    """[{id, parent, nome}] do jsTree → [{codigo, nome, caminho, folha}]."""
    por_id = {n["id"]: n for n in nos if n.get("id")}
    filhos = {n.get("parent") for n in nos}
    itens = []
    for n in nos:
        cam, p, guarda = [], n.get("parent"), 0
        while p and p != "#" and p in por_id and guarda < 12:
            cam.append(por_id[p]["nome"])
            p, guarda = por_id[p].get("parent"), guarda + 1
        itens.append({"codigo": n["id"], "nome": n["nome"], "caminho": " › ".join(reversed(cam)),
                      "folha": n["id"] not in filhos})
    return itens


def gravar_assuntos(tipo: str, nos: list[dict], area: str = "", classe: str = "") -> int:
    """tipo "principais" (por área+classe) ou "complementares" (tabela toda)."""
    if len(nos) < 2:
        return 0
    from datetime import datetime
    d = _ler_assuntos()
    reg = {"itens": _itens_assuntos(nos), "em": datetime.now().isoformat(timespec="seconds")}
    if tipo == "principais":
        d.setdefault("principais", {})[_chave_assuntos(area, classe)] = {**reg, "area": area, "classe": classe}
    else:
        d["complementares"] = reg
    _ARQ_ASSUNTOS.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_ASSUNTOS.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return len(reg["itens"])


def complementares_desatualizados() -> bool:
    from datetime import datetime, timedelta
    em = (_ler_assuntos().get("complementares") or {}).get("em") or ""
    with contextlib.suppress(Exception):
        return datetime.fromisoformat(em) < datetime.now() - timedelta(days=COMPLEMENTARES_VALIDADE_DIAS)
    return True


def assuntos_para_modal(area: str, classe: str) -> dict:
    d = _ler_assuntos()
    pr = (d.get("principais") or {}).get(_chave_assuntos(area, classe)) or {}
    co = d.get("complementares") or {}
    return {"principais": [i for i in pr.get("itens") or [] if i.get("folha")], "principais_em": pr.get("em", ""),
            "complementares": [i for i in co.get("itens") or [] if i.get("folha")], "complementares_em": co.get("em", "")}


def _codigo_do_assunto(nome_inicio: str, area: str, classe: str) -> str:
    """Código do assunto principal sugerido pela regra (início do nome), na
    árvore já capturada desta área/classe."""
    alvo = _norm(nome_inicio)
    if not alvo:
        return ""
    itens = assuntos_para_modal(area, classe)["principais"]
    for i in itens:  # 1º: nome + caminho (como a IA do e-proc escreve a sugestão)
        if _norm(i["nome"] + ", " + ", ".join(reversed(i["caminho"].split(" › ")))).startswith(alvo):
            return i["codigo"]
    nomes = [i for i in itens if _norm(i["nome"]) == alvo.split(",")[0].strip()]
    return nomes[0]["codigo"] if len(nomes) == 1 else ""  # 2º: só o nome, se não for ambíguo


# Opções vistas na tela a cada preparação (eproc_peticao_inicial.
# _etapa4_informacoes) — o e-proc pode criar novas; elas entram no modal
# desmarcadas (pedido do Defensor, 30/10/2026).
_ARQ_INFO_ADICIONAIS = Path(__file__).resolve().parents[2] / "dados" / "eproc_info_adicionais.json"


def gravar_info_adicionais_vistas(opcoes: list[dict], fluxo: str) -> list[str]:
    """`opcoes` = [{id, rotulo}] da tela; guarda em que fluxo cada uma
    aparece. Devolve os ids que ainda não eram conhecidos."""
    salvo = {}
    with contextlib.suppress(Exception):
        salvo = json.loads(_ARQ_INFO_ADICIONAIS.read_text(encoding="utf-8"))
    conhecidos = {c for c, _, _ in INFO_ADICIONAIS} | set(salvo)
    novos = []
    for o in opcoes:
        cid, rot = o.get("id") or "", re.sub(r"\s+", " ", o.get("rotulo") or "").strip()
        if not cid:
            continue
        if cid not in conhecidos:
            novos.append(cid)
        e = salvo.setdefault(cid, {"rotulo": rot, "fluxos": []})
        e["rotulo"] = rot or e.get("rotulo", "")
        if fluxo and fluxo not in e["fluxos"]:
            e["fluxos"].append(fluxo)
    _ARQ_INFO_ADICIONAIS.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_INFO_ADICIONAIS.write_text(json.dumps(salvo, ensure_ascii=False, indent=1), encoding="utf-8")
    return novos


def info_adicionais_opcoes() -> list[dict]:
    """As conhecidas (com rótulo curto) + as novas vistas na tela do e-proc."""
    salvo = {}
    with contextlib.suppress(Exception):
        salvo = json.loads(_ARQ_INFO_ADICIONAIS.read_text(encoding="utf-8"))
    ops = [{"id": c, "rotulo": r, "fluxo": f} for c, r, f in INFO_ADICIONAIS]
    conhecidos = {c for c, _, _ in INFO_ADICIONAIS}
    for cid, e in salvo.items():
        if cid in conhecidos or cid == "chkIdosoVisual":  # idoso: automático no e-proc
            continue
        fl = e.get("fluxos") or []
        ops.append({"id": cid, "rotulo": e.get("rotulo") or cid, "fluxo": fl[0] if len(fl) == 1 else "", "nova": True})
    return ops


def _autor_menor(paj_norm: str, dados: dict, texto_norm: str) -> bool:
    from services import qualificacao_service
    q = qualificacao_service.obter(paj_norm) or {}
    autor = next((p for p in q.get("pessoas") or [] if p.get("papel") == "autor"), None)
    if autor:
        idade = qualificacao_service._idade(autor.get("nascimento") or "")
        if autor.get("menor") or (idade is not None and idade < 18):
            return True
    rep = dados.get("representante") or {}
    if rep.get("presente") and re.search(r"absolutamente|relativamente", _norm(rep.get("tipo_autor") or "")):
        return True
    return bool(re.search(r"menor (im)?pubere", texto_norm[:3000]))


def _info_adicionais(paj_norm: str, dados: dict, texto_norm: str) -> dict:
    """Marcações padrão (regras do Defensor): tutela se a inicial pede; autor
    criança/adolescente → prioridade + intervenção do MP (30/10/2026); art.
    129-A mantido no BI; o resto desmarcado. Prioridade de idoso é automática
    no e-proc (pela data de nascimento) e não entra aqui."""
    menor = _autor_menor(paj_norm, dados, texto_norm)
    padrao = {o["id"]: False for o in info_adicionais_opcoes()}
    padrao.update({"chkCautAntecTutela": bool(dados.get("tutela")), "chkCrianca": menor,
                   "chkIntervMinisterioPublico": menor, "chkAtendeReqLei8213_91": True})
    return padrao


# ---------------------------------------------------------------------------
# Qualificação conferida ("Ver qualificação" — services/qualificacao_service)
# ---------------------------------------------------------------------------

def _estado_civil_eproc(v: str) -> str:
    n = _norm(v)
    for pad, rot in ((r"solteir", "Solteiro"), (r"casad", "Casado"), (r"divorciad", "Divorciado"),
                     (r"separad", "Separado"), (r"viuv", "Viúvo"), (r"uniao estavel", "União Estável"),
                     (r"companheir", "Companheiro")):
        if re.search(pad, n):
            return rot
    return "Não Informado"


def _nacionalidade_eproc(v: str) -> str:
    """"venezuelana" -> "Venezuelano" (as opções do e-proc são no masculino);
    brasileira/brasileiro -> "" (padrão do cadastro)."""
    n = (v or "").strip().lower()
    if not n or n.startswith("brasileir"):
        return ""
    if n.endswith("a") and not n.endswith("ia"):
        n = n[:-1] + "o"
    return n[:1].upper() + n[1:]


def _dados_cadastro(p: dict, end_padrao: dict) -> dict:
    """Pessoa da qualificação no formato que `_cadastrar_pessoa` (e-proc) usa."""
    e = (p.get("endereco") or {}) if (p.get("endereco") or {}).get("logradouro") else (end_padrao or {})
    return {"nome": p.get("nome", ""), "cpf": re.sub(r"\D", "", p.get("cpf") or ""),
            "cep": re.sub(r"\D", "", e.get("cep") or ""), "numero": e.get("numero") or "",
            "complemento": e.get("complemento") or "",
            "estado_civil": _estado_civil_eproc(p.get("estado_civil") or "") if not p.get("menor") else "Solteiro",
            "sexo": {"F": "Feminino", "M": "Masculino"}.get(p.get("sexo") or "", ""),
            "nacionalidade": _nacionalidade_eproc(p.get("nacionalidade") or ""),
            "telefone": re.sub(r"\D", "", (p.get("telefone") or "").split(",")[0]),
            "email": (p.get("email") or "").split(",")[0].strip()}


def _aplicar_qualificacao(paj_norm: str, dados: dict) -> None:
    """Com `qualificacao.json`, autor e representante saem DELA (fonte única,
    conferida pelo Defensor) — o PDF da inicial fica só para conferência: CPF
    divergente vira aviso."""
    from services import qualificacao_service
    q = qualificacao_service.obter(paj_norm)
    if not q:
        return
    pessoas = q.get("pessoas") or []
    autor = next((p for p in pessoas if p.get("papel") == "autor"), None)
    if not autor or not re.sub(r"\D", "", autor.get("cpf") or ""):
        return
    cpf_pdf = dados["autor"].get("cpf") or ""
    novo = _dados_cadastro(autor, {})
    if cpf_pdf and cpf_pdf != novo["cpf"]:
        dados["avisos"].append(f"O CPF do autor na inicial ({_doc_formatado(cpf_pdf)}) difere do da qualificação "
                               f"({autor.get('cpf')}) — usado o da qualificação; confira a inicial.")
    for k, v in novo.items():
        if v or k in ("nacionalidade",):
            dados["autor"][k] = v
    dados["fonte_partes"] = "qualificação conferida (Ver qualificação)"

    rep = next((p for p in pessoas if p.get("papel") == "representante"
                and p.get("representa") in ("", autor.get("id"))), None)
    repres = q.get("representacao") or {}
    if rep and repres.get("tipo") in ("menor", "curatela"):
        doc = re.sub(r"\D", "", rep.get("cpf") or "")
        dados["representante"] = {
            "presente": True, "nome": rep.get("nome", ""), "documento": _doc_formatado(doc),
            "tipo_pessoa": rep.get("tipo_pessoa") or ("PJ" if len(doc) == 14 else "PF"),
            "tipo_autor": repres.get("tipo_autor_eproc") or REPRESENTACAO_AUTOR_CURATELA,
            "tipo_representante": repres.get("tipo_representante_eproc") or REPRESENTACAO_CURADOR,
            "principal": False, "atua_como_parte": False, "fonte": "qualificação conferida",
            "cadastro": _dados_cadastro(rep, autor.get("endereco") or {}),
        }
        dados["avisos"] = [a for a in dados["avisos"] if "representante não foi achado" not in a]
    elif repres.get("tipo") == "nenhuma":
        dados["representante"] = {"presente": False}


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
            "representados": REPRESENTADOS_EPROC, "representantes": REPRESENTANTES_EPROC,
            "entidades": entidades_eproc(),
            "info_adicionais_opcoes": info_adicionais_opcoes(),
            "assuntos": assuntos_para_modal(dados["area"], dados["classe"]),
            "localidades": LOCALIDADES}
