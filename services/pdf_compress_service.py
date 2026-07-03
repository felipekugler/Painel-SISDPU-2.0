"""Compressao de PDF em memoria — mesmo algoritmo/parametros da skill pessoal
`PDF-Comprimir_Dividir` (~/.claude/skills/PDF-Comprimir_Dividir/scripts/compress_split.py),
portado de arquivo-a-arquivo para bytes-a-bytes pra rodar dentro do processo do
painel (usado pelo download consolidado do PAJ).

Reduz a resolucao das imagens internas pra ~150 DPI e recomprime em JPEG
qualidade 60 — mantem legibilidade plena pra leitura humana e por IA, com boa
reducao de tamanho. So mexe em imagens JPEG/JPEG2000 (DCTDecode/JPXDecode);
outros formatos (ex: imagens sem perda) ficam como estao. Remove metadados
(titulo, autor, criador etc.) ao final. NAO divide em partes — isso e' papel
da skill, nao deste consolidado (aqui o usuario baixa 1 arquivo so).
"""

from __future__ import annotations

import io

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

A4_WIDTH_INCHES = 8.27  # largura A4 em polegadas — mesma referencia da skill

DPI_ALVO_PADRAO = 150
QUALIDADE_PADRAO = 60


def _estimate_dpi(width_px: int) -> int:
    dpi = round(width_px / A4_WIDTH_INCHES)
    return max(dpi, 1)


def _recompress_image(raw: bytes, orig_w: int, orig_h: int, target_dpi: int, quality: int) -> tuple[bytes, int, int] | None:
    """Redimensiona e recomprime uma imagem JPEG. None se nao houve ganho."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L", "CMYK"):
            img = img.convert("RGB")

        orig_dpi = _estimate_dpi(orig_w)
        scale = target_dpi / orig_dpi

        if scale < 1.0:
            new_w = max(1, round(orig_w * scale))
            new_h = max(1, round(orig_h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = orig_w, orig_h

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        result = out.getvalue()
        return (result, new_w, new_h) if len(result) < len(raw) else None
    except Exception:
        return None


def _compress_page_images(page, seen_ids: set, target_dpi: int, quality: int) -> int:
    """Recomprime XObjects de imagem de uma pagina. Retorna contagem."""
    count = 0
    if "/Resources" not in page:
        return 0
    res = page["/Resources"]
    if "/XObject" not in res:
        return 0
    xobjs = res["/XObject"].get_object()
    for key in list(xobjs.keys()):
        xobj_ref = xobjs.raw_get(key)
        obj_id = getattr(xobj_ref, "idnum", None)
        if obj_id and obj_id in seen_ids:
            continue
        xobj = xobjs[key].get_object()
        if xobj.get("/Subtype") != "/Image":
            continue
        filt = xobj.get("/Filter")
        if filt not in ("/DCTDecode", "/JPXDecode"):
            continue

        raw = xobj.get_data()
        w = int(xobj.get("/Width", 0))
        h = int(xobj.get("/Height", 0))
        resultado = _recompress_image(raw, w, h, target_dpi, quality)
        if resultado:
            new_raw, new_w, new_h = resultado
            xobj._data = new_raw
            xobj[NameObject("/Filter")] = NameObject("/DCTDecode")
            xobj[NameObject("/Length")] = NumberObject(len(new_raw))
            xobj[NameObject("/Width")] = NumberObject(new_w)
            xobj[NameObject("/Height")] = NumberObject(new_h)
            if "/DecodeParms" in xobj:
                del xobj["/DecodeParms"]
            if obj_id:
                seen_ids.add(obj_id)
            count += 1
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
