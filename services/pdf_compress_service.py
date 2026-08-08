"""Compressao de PDF em memoria — mesmo algoritmo/parametros da skill pessoal
`PDF-Comprimir_Dividir` (~/.claude/skills/PDF-Comprimir_Dividir/scripts/compress_split.py),
portado de arquivo-a-arquivo para bytes-a-bytes pra rodar dentro do processo do
painel (usado pelo download consolidado do PAJ).

Reduz a resolucao das imagens internas pra ~150 DPI e recomprime em JPEG
qualidade 60 — mantem legibilidade plena pra leitura humana e por IA, com boa
reducao de tamanho.

IMPORTANTE — cobre QUALQUER formato de imagem embutida (DCTDecode/JPEG,
JPXDecode/JPEG2000, FlateDecode/raw sem perda, CCITTFax etc.), nao so
JPEG/JPEG2000 como na versao anterior. Usamos `PdfWriter.pages[i].images`
(pypdf) pra decodificar cada XObject de imagem — independente do filtro
original — pra um `PIL.Image`, e' assim que pegamos tambem imagens
lossless (tipicamente PNG/bitmap bruto reencodado como FlateDecode) que
vem embutidas SEM compressao nenhuma dentro de PDFs de origem (ex: um
anexo que e' na verdade uma foto de camera/scanner-app salva como PDF
com a imagem em FlateDecode) — essas passavam batido pela versao antiga
(que so' olhava DCTDecode/JPXDecode) e foram a causa principal de um PDF
consolidado de 1438 paginas ter saido com 247 MB mesmo apos esta
"compressao": um punhado de fotos em ~2160x3840/2304x4096px, lossless,
5-9 MB cada, ficavam intocadas.

So aplica a troca por JPEG quando o resultado realmente fica menor que o
original (`img.data`) — protege formatos ja eficientes pra conteudo P&B
(ex: CCITTFax de digitalizacao de texto) de virar JPEG maior por engano.
Remove metadados (titulo, autor, criador etc.) ao final. NAO divide em
partes — isso e' papel da skill, nao deste consolidado (aqui o usuario
baixa 1 arquivo so).
"""

from __future__ import annotations

import io

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf._page import ImageFile
from pypdf.constants import ImageAttributes
from pypdf.constants import PageAttributes as PG
from pypdf.constants import Resources as RES
from pypdf.generic import StreamObject
from pypdf.generic._image_xobject import _xobj_to_image

A4_WIDTH_INCHES = 8.27  # largura A4 em polegadas — mesma referencia da skill

DPI_ALVO_PADRAO = 150
QUALIDADE_PADRAO = 60


def _estimate_dpi(width_px: int) -> int:
    dpi = round(width_px / A4_WIDTH_INCHES)
    return max(dpi, 1)


def _iter_xobject_images(resources, seen_ids: set):
    """Enumera XObjects de imagem de um dict `/Resources` (recursando em
    Form XObjects, que e' como o `show_pdf_page` do PyMuPDF embute paginas
    inteiras) e ja' DECODIFICA cada uma pra PIL — SEM tokenizar o
    content-stream da pagina e SEM decodificar imagens ja' vistas antes
    (`seen_ids`, o MESMO set compartilhado entre todas as paginas pelo
    chamador em `comprimir_pdf_bytes`).

    Substitui `page.images` (pypdf) neste loop, por dois motivos medidos
    empiricamente num PAJ de 1517 paginas / 2098 imagens (das quais so'
    770 sao objetos distintos — as outras 1328 sao o MESMO objeto de
    imagem repetido, ex: cabecalho/carimbo compartilhado por varias
    paginas apos o `doc.tobytes(garbage=4)` fundir objetos identicos):

    1) A property `page.images` do pypdf, so' para LISTAR (antes de
       decodificar pixel algum), tokeniza o content-stream INTEIRO da
       pagina toda vez (`_parse_images_from_content_stream`, via
       `get_contents()` + `content.operations`) so' pra tambem detectar
       imagens inline (operadores BI/ID/EI) — e faz essa tokenizacao MAIS
       DE UMA VEZ por pagina, porque `list(page.images)` (um `Sequence`)
       chama a funcao de listagem de ids uma vez no `__len__` e de novo a
       CADA imagem no `__getitem__`.
    2) TANTO a property quanto o loop original decodificavam (`_xobj_to_
       image`/`img.image`) toda imagem encontrada ANTES de checar
       `seen_ids` — ou seja, pagavam o decode (JPEG/Flate -> PIL, o custo
       real dominante, nao a tokenizacao em si) de imagens que so' seriam
       descartadas logo em seguida por ja' terem sido processadas numa
       pagina anterior.

    Somados, isso fazia SO' a enumeracao/decode consumir ~69s de um total
    de ~140s (~50%) na geracao do consolidado — mais que a propria
    recompressao (resize+reencode JPEG, ~30s). Adiando a checagem de
    `seen_ids` pra ANTES do decode (aqui, via `stream_obj.indirect_
    reference` — disponivel sem decodificar pixel nenhum) elimina o
    decode redundante das imagens duplicadas.

    Como os anexos deste consolidado vem de PyMuPDF (`show_pdf_page`/
    `insert_pdf`/rasterizacao) ou de PDFs externos onde imagem inline
    (BI/ID/EI) e' rara na pratica, ler so' os XObjects de imagem via
    `/Resources` cobre o mesmo universo pratico de imagens sem pagar o
    custo da tokenizacao do content-stream. Pior caso, se uma pagina tiver
    de fato uma imagem inline "pura": ela so' deixa de ser recomprimida —
    a compressao inteira ja e' best-effort (ver `comprimir_pdf_bytes`),
    nunca corrompe o PDF nem quebra a geracao."""
    if resources is None or RES.XOBJECT not in resources:
        return
    try:
        x_object = resources[RES.XOBJECT].get_object()
    except Exception:
        return
    for nome in list(x_object.keys()):
        try:
            stream_obj = x_object[nome].get_object()
        except Exception:
            continue
        if not isinstance(stream_obj, StreamObject):
            continue
        ref = getattr(stream_obj, "indirect_reference", None)
        chave = getattr(ref, "idnum", None)
        if chave is not None:
            # Dedup ANTES do decode: cobre tanto o mesmo Form/Image
            # referenciado por mais de um nome/pagina (objeto compartilhado
            # apos o `garbage=4` do PyMuPDF) quanto recursao infinita em
            # Forms auto-referenciados (malformados).
            if chave in seen_ids:
                continue
            seen_ids.add(chave)
        subtype = stream_obj.get(ImageAttributes.SUBTYPE)
        if subtype == "/Image":
            try:
                extension, byte_stream, img = _xobj_to_image(stream_obj)
            except Exception:
                continue
            yield ImageFile(
                name=f"{nome[1:]}{extension}",
                data=byte_stream,
                image=img,
                indirect_reference=ref,
            )
        elif subtype == "/Form":
            sub_resources = stream_obj.get(PG.RESOURCES)
            if sub_resources is not None:
                try:
                    sub_resources = sub_resources.get_object()
                except Exception:
                    pass
                yield from _iter_xobject_images(sub_resources, seen_ids)


def _compress_page_images(page, seen_ids: set, target_dpi: int, quality: int) -> int:
    """Recomprime pra JPEG as imagens de uma pagina (qualquer filtro
    original). Retorna contagem de imagens efetivamente trocadas.

    O dedup entre paginas (mesmo objeto de imagem compartilhado por varias
    paginas) ja' acontece DENTRO de `_iter_xobject_images` (antes do
    decode) — aqui so' resta processar (resize/reencode) o que ela
    entregar."""
    count = 0
    try:
        imagens = _iter_xobject_images(page.get(PG.RESOURCES), seen_ids)
    except Exception:
        return 0

    for img in imagens:
        try:
            pil_img = img.image
            if pil_img is None:
                continue
            w, h = pil_img.size
            if w <= 0 or h <= 0:
                continue

            orig_dpi = _estimate_dpi(w)
            scale = target_dpi / orig_dpi
            if scale < 1.0:
                new_w = max(1, round(w * scale))
                new_h = max(1, round(h * scale))
                pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

            if pil_img.mode not in ("RGB", "L"):
                pil_img = pil_img.convert("RGB")

            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
            new_len = buf.tell()

            raw_len = len(img.data) if img.data else None
            if raw_len is not None and new_len >= raw_len:
                continue  # nao houve ganho — mantem o formato original

            img.replace(pil_img, quality=quality, optimize=True)
            count += 1
        except Exception:
            continue
    return count


def _clear_metadata(writer: PdfWriter) -> None:
    writer.add_metadata({
        "/Title": "", "/Author": "", "/Subject": "",
        "/Creator": "", "/Producer": "", "/Keywords": "",
    })
    if writer._info:
        for key in list(writer._info.keys()):
            del writer._info[key]


def comprimir_pdf_bytes(
    pdf_bytes: bytes,
    dpi: int = DPI_ALVO_PADRAO,
    quality: int = QUALIDADE_PADRAO,
) -> tuple[bytes, int]:
    """Comprime o PDF (bytes -> bytes). Retorna (pdf_comprimido, n_imagens_recomprimidas).

    Se a compressao falhar por qualquer motivo, ou nao reduzir o tamanho,
    devolve os bytes originais — o download nunca deve quebrar por causa da
    otimizacao."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        seen_ids: set = set()
        total_imgs = 0
        for page in writer.pages:
            total_imgs += _compress_page_images(page, seen_ids, dpi, quality)

        _clear_metadata(writer)

        out = io.BytesIO()
        writer.write(out)
        comprimido = out.getvalue()
    except Exception:
        return pdf_bytes, 0

    if len(comprimido) >= len(pdf_bytes):
        return pdf_bytes, 0
    return comprimido, total_imgs
