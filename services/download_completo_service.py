"""Gera um PDF unico consolidando todos os documentos de um PAJ.

Layout inspirado no "download completo" do e-proc: capa com identificacao do
PAJ, pagina(s) com o historico de movimentacoes em ordem cronologica (tabela
formatada), a narrativa integral do PAJ, e depois uma pagina separadora + os
documentos de cada sequencia (seq) do SISDPU — UMA sequencia por movimentacao,
mesmo quando ela nao tem anexo algum. Toda pagina recebe uma tarja no
topo-esquerdo com PAJ / seq. / nome do documento / numero de pagina, para
navegacao rapida no PDF final.

Cada sequencia e' colorida (fundo da separadora + tarja das paginas dela)
conforme a MESMA classificacao usada na aba Movimentacoes do painel
(`corLinhaMov()` em templates/paj_detail.html): "Atendimento de retorno" fica
verde, movimentacao de Defensor fica vermelha, o resto fica amarelo (tom
padrao).

A lista de documentos de cada sequencia vem do proprio campo `documentos` da
movimentacao (mais confiavel que adivinhar pelo nome do arquivo baixado), e e'
casada com o anexo fisico em Arquivos/ pelo ID embutido no nome do arquivo
baixado (`seqNNN - <id> - <nome original>.<ext>"`, onde <id> e' o mesmo ID que
aparece na urlDownload do SISDPU). O "Formulario do PAJ" (o documento cuja
descricao comeca com "PAJ") e' sempre movido pro inicio da sua sequencia.

So PDF e imagem sao mesclados de fato; qualquer outro formato (docx, xlsx,
zip etc.) vira uma pagina informativa (nome, formato, tamanho) no lugar do
conteudo, ja que nao ha renderizador confiavel pra eles no ambiente. Um
documento listado na movimentacao mas que nao foi baixado localmente (sync
incompleto, limite de anexos etc.) tambem vira uma pagina informativa.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from html import unescape
from pathlib import Path

import fitz  # PyMuPDF

from services.paj_service import ler_paj
from services.pdf_compress_service import comprimir_pdf_bytes

log = logging.getLogger(__name__)

A4 = fitz.paper_rect("a4")
MARGEM = 40
FONTE = "helv"
FONTE_NEGRITO = "hebo"

# Colorizacao das sequencias — MESMA logica de `corLinhaMov()` em
# templates/paj_detail.html (aba Movimentacoes do painel): prioriza
# "Atendimento de retorno" (verde), depois movimentacao de Defensor
# (aqui vermelho, ao inves do laranja usado na tela), e o restante fica no
# tom padrao (amarelo). Cada categoria tem um tom claro (fundo da pagina
# separadora) e um tom escuro (tarja de identificacao no topo das paginas
# daquela sequencia).
COR_PADRAO_CLARA = (0.99, 0.93, 0.68)
COR_PADRAO_ESCURA = (0.80, 0.63, 0.10)
COR_RETORNO_CLARA = (0.85, 0.95, 0.85)
COR_RETORNO_ESCURA = (0.40, 0.68, 0.36)
COR_DEFENSOR_CLARA = (0.97, 0.82, 0.80)
COR_DEFENSOR_ESCURA = (0.76, 0.28, 0.24)

CORES_CATEGORIA = {
    "retorno": (COR_RETORNO_CLARA, COR_RETORNO_ESCURA),
    "defensor": (COR_DEFENSOR_CLARA, COR_DEFENSOR_ESCURA),
    "padrao": (COR_PADRAO_CLARA, COR_PADRAO_ESCURA),
}


def _categoria_mov(mov: dict | None) -> str:
    """Classifica a movimentacao pra colorir a pagina separadora e a tarja
    das paginas da sua sequencia — mesma prioridade do painel: retorno antes
    de defensor."""
    if not mov:
        return "padrao"
    fases = mov.get("fases") or ""
    usuario = mov.get("usuario") or ""
    if "atendimento de retorno" in fases.strip().lower():
        return "retorno"
    if fases and usuario and ("DEFENSOR" in usuario or "(d)" in usuario):
        return "defensor"
    return "padrao"

IMAGENS_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif"}

# Nome padrao gerado pelo sincronizador: "seqNNN - <id> - <nome original>.<ext>"
_RE_ANEXO = re.compile(r"^seq(\d+)\s*-\s*(\S+)\s*-\s*(.+)$", re.IGNORECASE)
_RE_ID_URL = re.compile(r"/(\d{3,})/[^/]+/?$")


def _sem_acento(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _parse_anexo(nome_arquivo: str) -> tuple[int | None, str | None, str]:
    """Extrai (seq, id, nome_original) do nome padronizado do anexo baixado.

    Retorna (None, None, stem) se o nome nao seguir o padrao esperado (anexo
    legado/renomeado manualmente)."""
    stem = Path(nome_arquivo).stem
    m = _RE_ANEXO.match(stem)
    if not m:
        return None, None, stem
    seq, id_, resto = m.groups()
    return int(seq), id_, resto.strip() or stem


def _extrair_id_url(url: str) -> str | None:
    m = _RE_ID_URL.search(url or "")
    return m.group(1) if m else None


_RE_TEM_LETRA_OU_NUMERO = re.compile(r"[A-Za-z0-9À-ÿ]")


def _texto_significativo(texto: str | None) -> str | None:
    """None se o texto nao tiver nenhuma letra/numero (ex: '.', '-', vazio).

    O SISDPU as vezes grava 'descricao': '.' em vez de deixar vazio — como
    isso e' verdadeiro em Python, um `or` simples NAO caía no nome do arquivo
    como alternativa, e a tarja acabava mostrando '.' no lugar do nome do
    documento."""
    if not texto or not _RE_TEM_LETRA_OU_NUMERO.search(texto):
        return None
    return texto


_RE_EXTENSAO = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _garantir_extensao(nome: str, *candidatos: str | None) -> str:
    """Garante que o nome exibido na tag termine com a extensao do arquivo
    (ex: '.pdf') — a `descricao` do SISDPU (ex: 'PAJ - SEM ASSINATURA') quase
    nunca vem com extensao, so' o `nomeArquivo`/nome fisico do anexo tem."""
    if _RE_EXTENSAO.search(nome):
        return nome
    for candidato in candidatos:
        ext = Path(candidato).suffix if candidato else ""
        if ext:
            return f"{nome}{ext}"
    return nome


def _e_formulario_paj(descricao: str) -> bool:
    """O 'Formulario/documentos do PAJ' — descricao do SISDPU comecando com
    'PAJ' (ex: 'PAJ - SEM ASSINATURA', 'PAJ - COM ASSINATURA') ou contendo
    'formulario do paj'. Esse documento deve vir sempre primeiro na sua
    sequencia."""
    t = _sem_acento(descricao).strip()
    return t.startswith("paj") or "formulario do paj" in t or "formulario paj" in t


def _nova_pagina(doc: fitz.Document) -> fitz.Page:
    return doc.new_page(width=A4.width, height=A4.height)


def _caixa(x0: float, y: float, x1: float, altura: float, fontsize: float) -> fitz.Rect:
    """Rect para insert_textbox com altura minima segura.

    O PyMuPDF DESCARTA o texto inteiro (sem desenhar nada, sem avisar) quando
    a caixa e' pouquissimo menor do que o necessario pra uma unica linha —
    o limiar real fica perto de fontsize*1.7. Usamos 2.2x de margem pra nao
    depender de acerto fino em cada chamada."""
    return fitz.Rect(x0, y, x1, y + max(altura, fontsize * 2.2))


def _texto(page: fitz.Page, x0, y, x1, altura, texto, fontsize, fontname=FONTE, color=None) -> None:
    page.insert_textbox(
        _caixa(x0, y, x1, altura, fontsize),
        texto,
        fontsize=fontsize,
        fontname=fontname,
        color=color,
    )


_TARJA_FONTSIZE = 5.5
_TARJA_PAD_X = 4
_TARJA_PAD_Y = 2


def _tarja(page: fitz.Page, texto: str, cor_fundo: tuple[float, float, float] = COR_PADRAO_ESCURA) -> None:
    """Escreve a tarja de identificacao no topo-esquerdo da pagina — do
    tamanho do texto (nao a largura toda da pagina, ao estilo do carimbo do
    e-proc), com fundo colorido (tom escuro da categoria da sequencia) e
    borda preta, pra nao se confundir visualmente com o conteudo do
    documento original.

    Usa `insert_text` (desenha a partir de um ponto, sem quebra de linha) em
    vez de `insert_textbox`: a largura real que caracteres acentuados
    ocupam varia conforme a pagina de origem (PDFs externos por vezes usam
    substituicao de fonte diferente da nossa estimativa via
    `get_text_length`), e `insert_textbox` DESCARTA o texto inteiro quando a
    caixa calculada fica um pouco curta — o mesmo nome de arquivo as vezes
    sumia so' porque tinha acentuacao. `insert_text` nunca descarta: na pior
    hipotese o texto ultrapassa visualmente a caixinha colorida por
    milimetros, o que e' inofensivo.

    Rotation-aware: `draw_rect`/`insert_text` desenham sempre no sistema de
    coordenadas BRUTO da pagina (o do MediaBox), que NAO acompanha o
    /Rotate — so' o tamanho relatado por `page.rect` acompanha. Numa pagina
    sem rotacao os dois sistemas coincidem e nada muda aqui. Numa pagina
    rotacionada (as que `_normalizar_pagina_pdf` vira pra encaixar anexo em
    paisagem dentro de A4 retrato), sem essa conversao a tarja acaba
    desenhada fora do canto visivel. `page.derotation_matrix` converte um
    ponto/retangulo do sistema visual (o que a gente ve na tela) pro
    sistema bruto de desenho; `rotate=page.rotation` no `insert_text` gira
    de volta os proprios caracteres pra saírem na horizontal."""
    largura_texto = min(
        fitz.get_text_length(texto, fontname=FONTE, fontsize=_TARJA_FONTSIZE) * 1.06 + 6,
        page.rect.width - 4 - 2 * _TARJA_PAD_X,
    )
    altura_texto = _TARJA_FONTSIZE * 1.3
    faixa_visual = fitz.Rect(2, 2, 2 + largura_texto + 2 * _TARJA_PAD_X, 2 + altura_texto + 2 * _TARJA_PAD_Y)
    origem_visual = fitz.Point(faixa_visual.x0 + _TARJA_PAD_X, faixa_visual.y1 - _TARJA_PAD_Y - 1)
    if page.rotation:
        drm = page.derotation_matrix
        faixa = faixa_visual * drm
        origem = origem_visual * drm
    else:
        faixa = faixa_visual
        origem = origem_visual
    page.draw_rect(faixa, color=(0, 0, 0), fill=cor_fundo, width=0.5, fill_opacity=0.7)
    page.insert_text(origem, texto, fontsize=_TARJA_FONTSIZE, fontname=FONTE, color=(0, 0, 0), rotate=page.rotation)


def _campo(page: fitz.Page, y: float, rotulo: str, valor: str) -> None:
    _texto(page, MARGEM, y, MARGEM + 140, 20, f"{rotulo}:", fontsize=10, fontname=FONTE_NEGRITO)
    _texto(page, MARGEM + 145, y, A4.width - MARGEM, 20, str(valor or "-"), fontsize=10)


def _escrever_capa(doc: fitz.Document, paj_norm: str, meta: dict) -> None:
    page = _nova_pagina(doc)
    y = MARGEM
    _texto(
        page, MARGEM, y, A4.width - MARGEM, 40,
        f"PAJ {meta.get('paj', paj_norm)}",
        fontsize=20, fontname=FONTE_NEGRITO,
    )
    y += 50
    det = meta.get("detalhes_sisdpu", {}) or {}
    campos = [
        ("Assistido", meta.get("assistido_caixa", "")),
        ("Ofício", meta.get("oficio_caixa", "")),
        ("Área", meta.get("foro_detectado", "")),
        ("Pretensão", meta.get("pretensaopura") or meta.get("classificacao", "")),
        ("Processo judicial", meta.get("processo_judicial", "")),
        ("Status no SIS", det.get("status_paj", "")),
        ("Data de abertura", meta.get("data_abertura", "")),
    ]
    for rotulo, valor in campos:
        _campo(page, y, rotulo, valor)
        y += 22
    y += 10
    _texto(
        page, MARGEM, y, A4.width - MARGEM, 40,
        "Documento gerado automaticamente pelo PAINEL SISDPU - consolida capa, "
        "histórico de movimentações e anexos baixados do SISDPU para este PAJ.",
        fontsize=8.5, color=(0.4, 0.4, 0.4),
    )
    _tarja(page, f"{paj_norm}  |  Capa")


def _escrever_resumo(page: fitz.Page, stats: dict) -> None:
    """Escreve o quadro-resumo da geração na capa (pagina 0), chamado no FINAL
    do processamento — depois de montar o resto do PDF — pois so' entao se
    conhece o total de paginas e os contadores de falha."""
    x0 = MARGEM
    y0 = 380
    largura = A4.width - 2 * MARGEM
    altura_caixa = 270
    caixa = fitz.Rect(x0, y0, x0 + largura, y0 + altura_caixa)
    page.draw_rect(caixa, color=(0.65, 0.65, 0.65), fill=(0.95, 0.98, 0.95), width=0.8)

    y = y0 + 16
    _texto(
        page, x0 + 16, y, x0 + largura - 16, 20,
        "DOCUMENTO COMPLETO GERADO COM SUCESSO.",
        fontsize=11, fontname=FONTE_NEGRITO, color=(0.09, 0.45, 0.09),
    )
    y += 30

    bloco_principal = [
        f"Número de documentos: {stats['total_documentos']}",
        f"Número de páginas separadoras (por sequencial): {stats['total_separadoras']}",
        f"Número total de páginas do arquivo final: {stats['total_paginas']}",
        f"Total de arquivos unificados: {stats['unificados']}",
        f"Data de início do processamento: {stats['inicio']}",
        f"Data de fim do processamento: {stats['fim']}",
    ]
    for linha in bloco_principal:
        _texto(page, x0 + 16, y, x0 + largura - 16, 18, linha, fontsize=9.5)
        y += 18
    y += 10

    for linha in [
        f"Arquivos não convertidos para PDF: {stats['nao_convertidos']}",
        f"Falhas na geração/conversão dos arquivos: {stats['nao_encontrados']}",
        f"Falhas na unificação dos arquivos: {stats['erros_unificacao']}",
    ]:
        _texto(page, x0 + 16, y, x0 + largura - 16, 18, linha, fontsize=9.5)
        y += 18 + 10


# (titulo, largura) — largura em pontos, soma ~514pt pra caber numa A4 retrato
# (595pt) com a MARGEM padrao (40pt) de cada lado.
COLUNAS_MOV = [
    ("Seq.", 26),
    ("Anexo", 42),
    ("Data/Hora", 76),
    ("Fases", 100),
    ("Descrição", 180),
    ("Usuário", 90),
]
ALTURA_LINHA_MOV = 62
ALTURA_CABECALHO_MOV = 22
LINHAS_MOV_POR_PAGINA = 11


def _truncar(texto: str, limite: int) -> str:
    texto = (texto or "").strip()
    return texto if len(texto) <= limite else texto[: limite - 3] + "..."


_RE_TAG_QUALQUER = re.compile(r"<[^>]+>")


def _texto_simples(texto: str | None) -> str:
    """Remove marcacao HTML que o SISDPU as vezes embute em campos de texto
    livre da movimentacao (descricao, descricao_retorno) — vem do editor
    rich-text do SISDPU (ex: '<span style="...">​<p>Remessa ao Cartório</p>
    </span>'), nao e' HTML de verdade pra renderizar, so' contaminacao do
    campo. Uso EXCLUSIVO deste gerador de PDF — o resto do painel (abas
    Narrativa/Movimentacoes na tela) continua exibindo via Jinja `| safe`,
    sem qualquer mudanca aqui."""
    if not texto:
        return ""
    limpo = _RE_TAG_QUALQUER.sub(" ", texto)  # tag vira espaco — evita colar palavras
    limpo = unescape(limpo).replace("\xa0", " ")
    return re.sub(r"\s+", " ", limpo).strip()


def _linha_tabela_mov(page: fitz.Page, x0: float, y: float, valores: list[str], cabecalho: bool = False) -> None:
    altura = ALTURA_CABECALHO_MOV if cabecalho else ALTURA_LINHA_MOV
    fontname = FONTE_NEGRITO if cabecalho else FONTE
    fill = (0.85, 0.85, 0.85) if cabecalho else None
    x = x0
    for (_titulo, largura), valor in zip(COLUNAS_MOV, valores):
        rect = fitz.Rect(x, y, x + largura, y + altura)
        page.draw_rect(rect, color=(0.6, 0.6, 0.6), fill=fill, width=0.5)
        _texto(page, x + 3, y + 2, x + largura - 3, altura - 4, valor, fontsize=7.5, fontname=fontname)
        x += largura


def _escrever_movimentacoes(doc: fitz.Document, paj_norm: str, movs: list[dict]) -> None:
    if not movs:
        return
    titulos_cabecalho = [t for t, _l in COLUNAS_MOV]
    for inicio in range(0, len(movs), LINHAS_MOV_POR_PAGINA):
        page = _nova_pagina(doc)
        y = MARGEM
        if inicio == 0:
            _texto(
                page, MARGEM, y, A4.width - MARGEM, 20,
                "Histórico de movimentações (ordem cronológica)",
                fontsize=12, fontname=FONTE_NEGRITO,
            )
            y += 26
        _linha_tabela_mov(page, MARGEM, y, titulos_cabecalho, cabecalho=True)
        y += ALTURA_CABECALHO_MOV
        for mov in movs[inicio : inicio + LINHAS_MOV_POR_PAGINA]:
            documentos = mov.get("documentos", []) or []
            valores = [
                str(mov.get("seq", "")),
                f"Sim ({len(documentos)})" if documentos else "-",
                mov.get("data_original") or mov.get("data") or "-",
                _truncar(_limpar_fase(mov.get("fases", "")), 75),
                _truncar(_texto_simples(mov.get("descricao")), 195),
                _truncar(mov.get("usuario", ""), 60),
            ]
            _linha_tabela_mov(page, MARGEM, y, valores)
            y += ALTURA_LINHA_MOV
        _tarja(page, f"{paj_norm}  |  Histórico de movimentações")


_RE_TAG_BLOCO = re.compile(r"</?(p|div|br|tr|li|h[1-6])\b[^>]*>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")


def _narrativa_para_texto(narrativa_html: str) -> str:
    """Converte a narrativa (HTML bruto do SISDPU) em texto simples,
    preservando quebras de paragrafo nos limites de tags de bloco (p/div/br/
    tr/li/h1-6) — sem isso vira uma unica linha ilegivel."""
    if not narrativa_html:
        return ""
    texto = _RE_TAG_BLOCO.sub("\n", narrativa_html)
    texto = _RE_TAG.sub("", texto)
    texto = unescape(texto)
    linhas = [re.sub(r"[ \t]+", " ", linha).strip() for linha in texto.splitlines()]
    return "\n".join(linha for linha in linhas if linha)


def _escrever_narrativa(doc: fitz.Document, paj_norm: str, narrativa_html: str) -> None:
    """Pagina inteira com o conteudo integral da narrativa do PAJ (a
    'qualificacao + relato dos fatos' preenchida no SISDPU), logo antes da
    Sequencia 1 — contexto que os documentos anexados sozinhos nao dao."""
    texto = _narrativa_para_texto(narrativa_html)
    paragrafos = texto.split("\n") if texto else []
    if not paragrafos:
        paragrafos = ["(narrativa não preenchida no SISDPU)"]

    largura_util = A4.width - 2 * MARGEM
    chars_por_linha = max(1, round(largura_util / 5))  # ~5pt/caractere em fontsize 9.5
    altura_linha = 13

    page = None
    y = MARGEM

    def _nova():
        nonlocal page, y
        page = _nova_pagina(doc)
        y = MARGEM
        _texto(page, MARGEM, y, A4.width - MARGEM, 28, "Narrativa", fontsize=14, fontname=FONTE_NEGRITO)
        y += 36

    _nova()
    for paragrafo in paragrafos:
        n_linhas = max(1, -(-len(paragrafo) // chars_por_linha))  # divisao com arredondamento pra cima
        altura_par = n_linhas * altura_linha + 8
        if y + altura_par > A4.height - MARGEM and y > MARGEM + 40:
            _tarja(page, f"{paj_norm}  |  Narrativa")
            _nova()
        _texto(page, MARGEM, y, A4.width - MARGEM, altura_par, paragrafo, fontsize=9.5)
        y += altura_par
    _tarja(page, f"{paj_norm}  |  Narrativa")


def _limpar_fase(fases: str) -> str:
    """Remove o marcador decorativo que o SISDPU antepoe a cada fase (o
    caractere varia — as vezes chega como '»', as vezes como '°' — por isso a
    limpeza remove qualquer simbolo/pontuacao no inicio, nao um char fixo)."""
    texto = re.sub(r"^\W+", "", (fases or "").strip())
    return texto.strip().rstrip(";").strip()


def _pagina_separadora(
    doc: fitz.Document,
    paj_norm: str,
    rotulo_seq: str,
    mov: dict | None,
    itens: list[dict],
) -> tuple[float, float, float]:
    """Desenha a pagina separadora e retorna o tom escuro da categoria da
    sequencia — o chamador repassa essa cor pra tarja de todos os documentos
    desta mesma sequencia, mantendo o esquema de cores consistente."""
    categoria = _categoria_mov(mov)
    cor_clara, cor_escura = CORES_CATEGORIA[categoria]
    page = _nova_pagina(doc)
    page.draw_rect(page.rect, color=None, fill=cor_clara)
    y = MARGEM
    _texto(page, MARGEM, y, A4.width - MARGEM, 36, f"SEQUÊNCIA {rotulo_seq}", fontsize=18, fontname=FONTE_NEGRITO)
    y += 46
    if mov:
        data_hora = mov.get("data_original") or mov.get("data") or ""
        fase = _limpar_fase(mov.get("fases", ""))
        usuario = mov.get("usuario", "") or ""
        desc = _truncar(_texto_simples(mov.get("descricao")), 380)
        for rotulo, valor, altura in [
            ("Data/Hora", data_hora, 20),
            ("Fase", fase, 20),
            ("Usuário", usuario, 20),
        ]:
            _texto(page, MARGEM, y, MARGEM + 90, altura, f"{rotulo}:", fontsize=10, fontname=FONTE_NEGRITO)
            _texto(page, MARGEM + 95, y, A4.width - MARGEM, altura, valor or "-", fontsize=10)
            y += altura + 4
        _texto(page, MARGEM, y, MARGEM + 90, 130, "Descrição:", fontsize=10, fontname=FONTE_NEGRITO)
        _texto(page, MARGEM + 95, y, A4.width - MARGEM, 130, desc or "-", fontsize=10)
        y += 140
        if categoria == "retorno":
            # Em "Atendimento de retorno" a "Descrição" costuma ser so' um
            # texto burocratico ("Fase incluída automaticamente..."); o
            # conteudo real do atendimento fica no campo `descricao_retorno`
            # do metadata — por isso e' obrigatorio mostra-lo aqui.
            desc_retorno = _truncar(_texto_simples(mov.get("descricao_retorno")), 380)
            _texto(page, MARGEM, y, MARGEM + 90, 130, "Descrição Retorno:", fontsize=10, fontname=FONTE_NEGRITO)
            _texto(page, MARGEM + 95, y, A4.width - MARGEM, 130, desc_retorno or "(não informado no SISDPU)", fontsize=10)
            y += 140
    else:
        _texto(page, MARGEM, y, A4.width - MARGEM, 20, "(sequência sem movimentação correspondente no histórico)", fontsize=9, color=(0.35, 0.35, 0.35))
        y += 28
    _texto(page, MARGEM, y, A4.width - MARGEM, 20, "Documentos desta sequência:", fontsize=10, fontname=FONTE_NEGRITO)
    y += 24
    if not itens:
        _texto(page, MARGEM + 10, y, A4.width - MARGEM, 18, "Nenhum documento anexado nesta sequência.", fontsize=9, color=(0.35, 0.35, 0.35))
        y += 18
    for item in itens:
        anexo = item.get("anexo")
        if anexo:
            complemento = f"({anexo['tipo'].upper()}, {anexo['tamanho'] / 1024:.1f} KB)"
        else:
            complemento = "(não baixado localmente)"
        _texto(page, MARGEM + 10, y, A4.width - MARGEM, 18, f"- {item['descricao']}  {complemento}", fontsize=9)
        y += 18
    _tarja(page, f"{paj_norm}  |  Seq. {rotulo_seq}  |  Página separadora", cor_escura)
    return cor_escura


_A4_TOLERANCIA = 3  # pontos — margem pra considerar uma pagina "ja e' A4 retrato"


def _normalizar_pagina_pdf(doc: fitz.Document, src: fitz.Document, indice: int) -> None:
    """Insere a pagina `indice` de `src` em `doc` ja padronizada em A4 retrato.

    A maioria dos anexos ja vem em A4 retrato — nesse caso a pagina e' so'
    copiada (`insert_pdf`), sem custo extra e preservando texto
    pesquisavel/vetores tal como sempre foi feito aqui.

    Quando a pagina de origem vem em paisagem, ou em qualquer tamanho
    diferente de A4 (Oficio, Letter, Legal, digitalizacoes fora de padrao
    etc.), cria-se uma pagina nova e o conteudo original e' desenhado
    dentro dela via `show_pdf_page` — que embute o proprio conteudo da
    pagina (nao rasteriza, mantendo texto/qualidade), sempre escalado
    mantendo a proporcao original (sem distorcer), centralizado na folha.
    E' o mesmo principio ja usado em `_anexar_imagem` pra imagens soltas,
    agora tambem pros PDFs anexados — que sao a maioria dos documentos
    reais do processo e, ate' aqui, entravam no consolidado exatamente
    como estivessem no arquivo de origem (paisagem, tamanho diferente
    etc.).

    IMPORTANTE — paginas com /Rotate proprio (rotation != 0): muitas
    certidoes/digitalizacoes guardam a orientacao exibida via atributo
    /Rotate da propria pagina (o MediaBox fisico e' uma coisa, a
    orientacao exibida e' outra). Testamos exaustivamente TODAS as
    combinacoes razoaveis de `rotate=`/`clip=` do `show_pdf_page` pra
    embutir esse tipo de pagina e todas cortam parte do conteudo (bug/
    limitacao da biblioteca ao combinar rotacao da pagina de origem com
    a insercao) — o corte relatado pelo usuario em certidoes rotacionadas
    veio exatamente disso. Pra essas paginas (rotation != 0) usamos por
    isso um caminho DIFERENTE, testado e confirmado sem cortes: rasteriza
    a pagina via `get_pixmap` (que respeita corretamente o /Rotate,
    diferente do `show_pdf_page`) e insere o resultado como imagem numa
    pagina A4 retrato nova, exatamente como `_anexar_imagem` ja faz pra
    imagens soltas. Perde-se o texto pesquisavel SO' dessa pagina
    especifica — na pratica irrelevante, porque uma pagina com /Rotate
    proprio quase sempre e' uma digitalizacao (imagem escaneada) sem
    texto selecionavel mesmo."""
    pagina_src = src[indice]
    rect = pagina_src.rect  # ja reflete o /Rotate proprio da pagina de origem
    largura, altura = rect.width, rect.height

    ja_e_a4_retrato = (
        largura > 0
        and altura > 0
        and altura >= largura
        and abs(largura - A4.width) <= _A4_TOLERANCIA
        and abs(altura - A4.height) <= _A4_TOLERANCIA
    )
    if ja_e_a4_retrato:
        doc.insert_pdf(src, from_page=indice, to_page=indice)
        return

    if largura <= 0 or altura <= 0:
        # Dimensoes invalidas (pagina corrompida) — melhor manter o
        # comportamento antigo (copia crua) do que dividir por zero.
        doc.insert_pdf(src, from_page=indice, to_page=indice)
        return

    if pagina_src.rotation:
        _normalizar_pagina_pdf_via_raster(doc, pagina_src)
        return

    paisagem = largura > altura
    # Pagina nova no MESMO formato fisico da origem (paisagem ou retrato),
    # sempre do tamanho de uma folha A4 — so' que com largura/altura
    # trocadas quando a origem for paisagem. Escala e' calculada nesse
    # mesmo sistema de coordenadas (sem trocar largura/altura), entao o
    # `show_pdf_page` nunca precisa girar nada. So' chegamos aqui quando a
    # pagina de origem NAO tem /Rotate proprio (ver bloco acima) — e' o
    # unico caso em que confirmamos que `show_pdf_page` embute o conteudo
    # inteiro sem cortar.
    largura_pagina, altura_pagina = (A4.height, A4.width) if paisagem else (A4.width, A4.height)

    escala = min(largura_pagina / largura, altura_pagina / altura)
    w_dest = largura * escala
    h_dest = altura * escala
    x0 = (largura_pagina - w_dest) / 2
    y0 = (altura_pagina - h_dest) / 2
    destino = fitz.Rect(x0, y0, x0 + w_dest, y0 + h_dest)

    pagina = doc.new_page(width=largura_pagina, height=altura_pagina)
    pagina.show_pdf_page(destino, src, indice)
    if paisagem:
        # So' agora, sobre a pagina nova (sem rotacao previa nenhuma),
        # aplicamos o giro — troca largura/altura exibidas de volta pra
        # A4 retrato, com o conteudo ja centralizado corretamente.
        pagina.set_rotation(90)


_RASTER_ZOOM = 2.5  # ~180dpi equivalente (72dpi base * 2.5) — de sobra pra digitalizacao


_RASTER_MAX_PIXELS = 2200  # limite pra maior dimensao do pixmap, em pixels


_RASTER_JPEG_QUALIDADE = 85


def _raster_zoom_adaptativo(largura_pts: float, altura_pts: float) -> float:
    """Escolhe o maior zoom que nao deixe a maior dimensao do pixmap
    resultante passar de `_RASTER_MAX_PIXELS`, mas sem nunca superar
    `_RASTER_ZOOM` (pra paginas em tamanho normal — A4-ish — o
    comportamento continua exatamente igual, zoom fixo 2.5x/~180dpi).

    Existe pra proteger contra paginas de origem com /MediaBox anomalo
    (ex: um scanner/software que grava o MediaBox em unidades erradas,
    relatando algo como 3000x4000 PONTOS em vez dos ~595x842 pontos
    normais de A4). Nesses casos o zoom fixo de 2.5x, aplicado cegamente
    sobre as dimensoes em pontos da pagina de origem, gera um pixmap
    proporcionalmente gigante (ex: 7500x10000px) — e uma imagem desse
    tamanho ainda pesa varios MB mesmo depois de comprimida em JPEG
    qualidade 85, so' pela quantidade de pixels. Ja aconteceu de 2 paginas
    assim, num unico PDF consolidado, contribuirem sozinhas com dezenas
    de MB."""
    maior_dimensao = max(largura_pts, altura_pts)
    if maior_dimensao <= 0:
        return _RASTER_ZOOM
    return min(_RASTER_ZOOM, _RASTER_MAX_PIXELS / maior_dimensao)


def _normalizar_pagina_pdf_via_raster(doc: fitz.Document, pagina_src: fitz.Page) -> None:
    """Fallback pra paginas com /Rotate proprio (ver docstring de
    `_normalizar_pagina_pdf`). `get_pixmap` sem nenhum parametro de
    rotacao extra ja respeita corretamente o /Rotate da pagina (a mesma
    forma como qualquer leitor de PDF exibe), entao o pixmap resultante
    sai sempre correto e completo — so' inserimos ele numa pagina A4
    retrato nova, do mesmo jeito que `_anexar_imagem` faz pra imagens.

    IMPORTANTE — `insert_image(rect, pixmap=pix, ...)` (passando o objeto
    `fitz.Pixmap` direto) faz o PyMuPDF embutir os pixels BRUTOS na pagina,
    sem nenhuma compressao JPEG (confirmado inspecionando o `/Filter` do
    XObject resultante). Pra uma digitalizacao tipica em ~180dpi isso da'
    varios MB por pagina — e page/certidoes maiores (scans em alta
    resolucao nativa) chegam a 200+ MB SO' naquela pagina. Foi essa a causa
    de um PDF consolidado de 202 paginas (13 delas rasterizadas) ter saido
    com 594 MB. Por isso convertemos o pixmap pra bytes JPEG explicitamente
    (`pix.tobytes('jpeg', ...)`) ANTES de inserir, usando `stream=` em vez
    de `pixmap=` — o restante da normalizacao (tamanho/posicao/proporcao
    dentro da pagina A4) fica exatamente igual.

    O ZOOM em si e' ADAPTATIVO (ver `_raster_zoom_adaptativo`) — protege
    contra paginas de origem com /MediaBox anomalo/gigante, onde o zoom
    fixo geraria um pixmap gigante independente da compressao JPEG
    (ver docstring daquela funcao)."""
    zoom = _raster_zoom_adaptativo(pagina_src.rect.width, pagina_src.rect.height)
    pix = pagina_src.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    jpeg_bytes = pix.tobytes("jpeg", jpg_quality=_RASTER_JPEG_QUALIDADE)
    pagina = _nova_pagina(doc)
    rect_destino = fitz.Rect(MARGEM, 30, A4.width - MARGEM, A4.height - MARGEM)
    pagina.insert_image(rect_destino, stream=jpeg_bytes, keep_proportion=True)


def _anexar_pdf(doc: fitz.Document, paj_norm: str, rotulo_seq: str, nome_doc: str, caminho: Path, cor_tarja) -> None:
    nome_tarja = _truncar(nome_doc, 70)
    with fitz.open(caminho) as src:
        n = src.page_count
        idx_inicial = doc.page_count
        for i in range(n):
            _normalizar_pagina_pdf(doc, src, i)
    for i in range(n):
        pagina = doc[idx_inicial + i]
        _tarja(pagina, f"{paj_norm}  |  Seq. {rotulo_seq}  |  {nome_tarja}  |  Página {i + 1} de {n}", cor_tarja)


def _anexar_imagem(doc: fitz.Document, paj_norm: str, rotulo_seq: str, nome_doc: str, caminho: Path, cor_tarja) -> None:
    # Valida o arquivo ANTES de criar a pagina — se o formato for invalido
    # (ex: anexo com extensao de imagem mas conteudo corrompido/HTML), a
    # excecao deve subir pro chamador SEM deixar uma pagina em branco perdida
    # no documento final.
    fitz.Pixmap(str(caminho))
    page = _nova_pagina(doc)
    rect = fitz.Rect(MARGEM, 30, A4.width - MARGEM, A4.height - MARGEM)
    page.insert_image(rect, filename=str(caminho), keep_proportion=True)
    _tarja(page, f"{paj_norm}  |  Seq. {rotulo_seq}  |  {_truncar(nome_doc, 70)}  |  Página 1 de 1", cor_tarja)


def _anexar_informativo(doc: fitz.Document, paj_norm: str, rotulo_seq: str, titulo: str, campos: list[tuple[str, str]], obs: str, cor_tarja) -> None:
    page = _nova_pagina(doc)
    y = MARGEM + 80
    _texto(page, MARGEM, y, A4.width - MARGEM, 28, titulo, fontsize=13, fontname=FONTE_NEGRITO)
    y += 40
    for rotulo, valor in campos:
        _campo(page, y, rotulo, valor)
        y += 22
    _texto(page, MARGEM, y + 10, A4.width - MARGEM, 40, obs, fontsize=9, color=(0.4, 0.4, 0.4))
    _tarja(page, f"{paj_norm}  |  Seq. {rotulo_seq}  |  {_truncar(campos[0][1], 70)}  |  Página 1 de 1", cor_tarja)


def _anexar_nao_encontrado(doc: fitz.Document, paj_norm: str, rotulo_seq: str, descricao: str, cor_tarja) -> None:
    _anexar_informativo(
        doc, paj_norm, rotulo_seq,
        "Documento não baixado localmente",
        [("Descrição", descricao)],
        "Este documento consta na movimentação do SISDPU, mas não foi encontrado na "
        "pasta de Arquivos deste PAJ (sync incompleto, limite de anexos ou falha no "
        "download). Sincronize novamente ou abra direto no SISDPU.",
        cor_tarja,
    )


def _anexar_erro(doc: fitz.Document, paj_norm: str, rotulo_seq: str, nome: str, exc: Exception, cor_tarja) -> None:
    page = _nova_pagina(doc)
    _texto(page, MARGEM, MARGEM + 60, A4.width - MARGEM, 80, f"Erro ao processar '{nome}':\n{exc}", fontsize=10)
    _tarja(page, f"{paj_norm}  |  Seq. {rotulo_seq}  |  {_truncar(nome, 70)}  |  Erro no processamento", cor_tarja)


def _montar_grupos(movs: list[dict], anexos: list[dict]) -> tuple[dict[int, list[dict]], dict[str, list[dict]]]:
    """Casa os `documentos` de cada movimentacao com os anexos fisicos
    baixados, garantindo que TODA movimentacao (seq) entre no resultado, tenha
    ou nao documento. Retorna (grupos_por_seq, grupos_orfaos_por_rotulo) — o
    segundo cobre anexos baixados que nao aparecem em nenhuma `documentos` de
    movimentacao (SISDPU as vezes diverge entre a listagem de eventos e os
    anexos reais)."""
    por_id: dict[tuple[int, str], dict] = {}
    por_nome: dict[tuple[int, str], dict] = {}
    # Fallback SEM seq: alguns anexos (imagens coladas direto na descricao da
    # movimentacao, sem link de download de verdade — urlDownload aponta pra
    # pagina do tramite, nao pro arquivo) sempre baixam com id=0 sob um seq
    # generico (na pratica, sempre "seq001"), que pode nao bater com o seq
    # real da movimentacao dona do documento. Sem esse fallback por nome
    # (ignorando seq), esses anexos ficam pra sempre marcados como "nao
    # baixado" mesmo apos sincronizacoes repetidas — o arquivo ja esta' no
    # disco, so' catalogado sob o seq errado.
    por_nome_qualquer_seq: dict[str, dict] = {}
    for a in anexos:
        seq_a, id_a, nome_orig = _parse_anexo(a["nome"])
        if seq_a is None:
            continue
        if id_a and id_a != "0":
            por_id[(seq_a, id_a)] = a
        if nome_orig:
            chave_nome = nome_orig.lower()
            por_nome[(seq_a, chave_nome)] = a
            por_nome_qualquer_seq.setdefault(chave_nome, a)

    grupos: dict[int, list[dict]] = {}
    usados: set[str] = set()

    for mov in movs:
        try:
            seq = int(mov.get("seq"))
        except (TypeError, ValueError):
            continue
        itens = []
        for doc_meta in mov.get("documentos", []) or []:
            id_doc = _extrair_id_url(doc_meta.get("urlDownload", ""))
            # Extensao removida pra bater com a chave de `por_nome` (que vem
            # do NOME DO ARQUIVO BAIXADO ja sem extensao, via `_parse_anexo`)
            # — comparar com extensao aqui nunca dava match.
            nome_doc = Path(doc_meta.get("nomeArquivo") or "").stem.lower()
            anexo = (
                por_id.get((seq, id_doc or ""))
                or por_nome.get((seq, nome_doc))
                or por_nome_qualquer_seq.get(nome_doc)
            )
            if anexo:
                usados.add(anexo["caminho"])
            descricao = (
                _texto_significativo(doc_meta.get("descricao"))
                or _texto_significativo(doc_meta.get("nomeArquivo"))
                or (anexo and _texto_significativo(_parse_anexo(anexo["nome"])[2]))
                or "documento"
            )
            descricao = _garantir_extensao(descricao, doc_meta.get("nomeArquivo"), anexo["nome"] if anexo else None)
            itens.append({"descricao": descricao, "anexo": anexo})
        # o formulario/documentos do PAJ sempre em primeiro na sua sequencia
        itens.sort(key=lambda it: 0 if _e_formulario_paj(it["descricao"]) else 1)
        grupos[seq] = itens

    # Anexos baixados localmente mas nao referenciados por nenhuma `documentos`
    # de movimentacao — nao podem ser perdidos do consolidado.
    orfaos: dict[str, list[dict]] = {}
    for a in anexos:
        if a["caminho"] in usados:
            continue
        seq_a, _id_a, nome_orig = _parse_anexo(a["nome"])
        descricao = _texto_significativo(nome_orig) or a["nome"]
        descricao = _garantir_extensao(descricao, a["nome"])
        item = {"descricao": descricao, "anexo": a}
        if seq_a is not None and seq_a in grupos:
            grupos[seq_a].append(item)
        else:
            rotulo = str(seq_a) if seq_a is not None else "sem sequência identificada"
            orfaos.setdefault(rotulo, []).append(item)

    return grupos, orfaos


# Status possiveis de _anexar_item — usados pra compor o quadro-resumo da capa.
STATUS_UNIFICADO = "unificado"
STATUS_NAO_CONVERTIDO = "nao_convertido"
STATUS_NAO_ENCONTRADO = "nao_encontrado"
STATUS_ERRO_UNIFICACAO = "erro_unificacao"


def _anexar_item(doc: fitz.Document, paj_norm: str, rotulo_seq: str, pasta: Path, item: dict, cor_tarja) -> str:
    anexo = item.get("anexo")
    nome_doc = item["descricao"]
    if not anexo:
        _anexar_nao_encontrado(doc, paj_norm, rotulo_seq, nome_doc, cor_tarja)
        return STATUS_NAO_ENCONTRADO
    caminho = pasta / anexo["caminho"]
    tipo = anexo["tipo"]
    try:
        if tipo == "pdf":
            _anexar_pdf(doc, paj_norm, rotulo_seq, nome_doc, caminho, cor_tarja)
            return STATUS_UNIFICADO
        elif tipo in IMAGENS_EXT:
            _anexar_imagem(doc, paj_norm, rotulo_seq, nome_doc, caminho, cor_tarja)
            return STATUS_UNIFICADO
        else:
            _anexar_informativo(
                doc, paj_norm, rotulo_seq,
                "Arquivo não incorporado neste PDF consolidado",
                [
                    ("Nome", anexo["nome"]),
                    ("Formato", tipo.upper()),
                    ("Tamanho", f"{anexo['tamanho'] / 1024:.1f} KB"),
                ],
                "Apenas PDF e imagens são mesclados automaticamente. Abra o arquivo "
                "original na aba de Anexos do PAJ, no painel.",
                cor_tarja,
            )
            return STATUS_NAO_CONVERTIDO
    except Exception as exc:  # noqa: BLE001 - anexo isolado nao pode derrubar o PDF inteiro
        _anexar_erro(doc, paj_norm, rotulo_seq, anexo["nome"], exc, cor_tarja)
        return STATUS_ERRO_UNIFICACAO


def verificar_sincronizacao_completa(paj_norm: str) -> dict | None:
    """Verifica se da' pra gerar o Download Completo sem faltar anexo.

    Reaproveita a MESMA logica de casamento documento-anexo do PDF
    (`_montar_grupos`) — se algum documento listado numa movimentacao nao
    tem anexo fisico baixado, ou se o sync foi marcado como incompleto
    (`meta.sync_incompleto`, setado quando o usuario cancela no meio do
    download de anexos), ha' anexos extras alem do limite ainda nao
    baixados (`meta.anexos_extras`), ou o PAJ nunca passou pela extracao de
    documentos por sequencia (`meta.n_documentos_indexados is None` — PAJ
    so' teve a listagem basica da caixa, nunca uma sincronizacao de verdade),
    a sincronizacao NAO esta' completa.

    None se o PAJ nao existir."""
    dados = ler_paj(paj_norm)
    if not dados:
        return None

    meta = dados["metadata"]
    movs = dados["movimentacoes"]
    anexos = dados["anexos_sisdpu"]
    grupos, orfaos = _montar_grupos(movs, anexos)

    total = 0
    faltando = 0
    for itens in list(grupos.values()) + list(orfaos.values()):
        for item in itens:
            total += 1
            if not item.get("anexo"):
                faltando += 1

    sync_incompleto = bool(meta.get("sync_incompleto"))
    tem_overflow = bool(meta.get("anexos_extras"))
    nunca_sincronizado = meta.get("n_documentos_indexados") is None or meta.get("n_anexos_sisdpu") is None

    return {
        "completo": not nunca_sincronizado and not sync_incompleto and not tem_overflow and faltando == 0,
        "total_documentos": total,
        "faltando": faltando,
        "sync_incompleto": sync_incompleto,
        "motivo_incompleto": meta.get("sync_incompleto_motivo"),
        "tem_anexos_extras": tem_overflow,
        "nunca_sincronizado": nunca_sincronizado,
    }


def gerar_pdf_consolidado(paj_norm: str) -> bytes | None:
    """Monta e retorna (em memoria) o PDF consolidado do PAJ. None se o PAJ
    nao existir."""
    dados = ler_paj(paj_norm)
    if not dados:
        return None

    meta = dados["metadata"]
    movs = dados["movimentacoes"]
    anexos = dados["anexos_sisdpu"]
    narrativa = dados.get("narrativa", "")
    pasta = Path(dados["pasta"])

    movs_por_seq = {
        int(m["seq"]): m for m in movs if str(m.get("seq", "")).strip().isdigit()
    }
    grupos, orfaos = _montar_grupos(movs, anexos)

    inicio = datetime.now()

    doc = fitz.open()
    _escrever_capa(doc, paj_norm, meta)  # pagina 0 — o resumo e' escrito nela no final
    _escrever_movimentacoes(doc, paj_norm, movs)
    _escrever_narrativa(doc, paj_norm, narrativa)  # logo antes da Sequencia 1

    contagem = {
        STATUS_UNIFICADO: 0,
        STATUS_NAO_CONVERTIDO: 0,
        STATUS_NAO_ENCONTRADO: 0,
        STATUS_ERRO_UNIFICACAO: 0,
    }

    for seq in sorted(grupos):
        itens = grupos[seq]
        rotulo_seq = str(seq)
        cor_tarja = _pagina_separadora(doc, paj_norm, rotulo_seq, movs_por_seq.get(seq), itens)
        for item in itens:
            status = _anexar_item(doc, paj_norm, rotulo_seq, pasta, item, cor_tarja)
            contagem[status] += 1

    for rotulo_seq, itens in orfaos.items():
        cor_tarja = _pagina_separadora(doc, paj_norm, rotulo_seq, None, itens)
        for item in itens:
            status = _anexar_item(doc, paj_norm, rotulo_seq, pasta, item, cor_tarja)
            contagem[status] += 1

    fim = datetime.now()
    formato_data = "%d/%m/%Y %H:%M:%S"
    stats = {
        "total_documentos": sum(contagem.values()),
        "total_separadoras": len(grupos) + len(orfaos),
        "total_paginas": doc.page_count,
        "unificados": contagem[STATUS_UNIFICADO],
        "nao_convertidos": contagem[STATUS_NAO_CONVERTIDO],
        "nao_encontrados": contagem[STATUS_NAO_ENCONTRADO],
        "erros_unificacao": contagem[STATUS_ERRO_UNIFICACAO],
        "inicio": inicio.strftime(formato_data),
        "fim": fim.strftime(formato_data),
    }
    _escrever_resumo(doc[0], stats)

    # garbage=4: funde objetos duplicados (fontes/imagens byte-a-byte
    # identicas embutidas separadamente por cada sub-PDF anexado — comum
    # quando o consolidado tem centenas de anexos, cada um carregando sua
    # propria copia de fontes como Arial/Calibri) e remove objetos nao
    # referenciados. deflate=True comprime os content streams das paginas.
    # Nenhum dos dois altera o conteudo visual — so' reduz redundancia
    # estrutural do PDF. Confirmado empiricamente: sozinho, ja reduziu um
    # consolidado de 247 MB pra 165 MB (antes de qualquer recompressao de
    # imagem) num PAJ com 307 anexos / 1438 paginas.
    pdf_bytes = doc.tobytes(garbage=4, deflate=True)
    doc.close()

    tamanho_antes = len(pdf_bytes)
    pdf_bytes, n_imgs_comprimidas = comprimir_pdf_bytes(pdf_bytes)
    tamanho_depois = len(pdf_bytes)
    if tamanho_depois < tamanho_antes:
        reducao = (1 - tamanho_depois / tamanho_antes) * 100
        log.info(
            "download-completo %s: comprimido %d KB -> %d KB (-%.1f%%, %d imagens)",
            paj_norm, tamanho_antes // 1024, tamanho_depois // 1024, reducao, n_imgs_comprimidas,
        )

    return pdf_bytes
