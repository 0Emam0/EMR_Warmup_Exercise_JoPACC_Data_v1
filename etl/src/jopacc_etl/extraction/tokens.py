"""Turn a PDF page into positioned text tokens.

Roughly half the JoPACC corpus was exported with all glyphs converted to
vector outlines, so there is no text layer to read. Those pages have to be
rendered and read with OCR. Pages that do carry text are read directly.

Both paths return the same ``Token`` shape -- text plus a bounding box in PDF
points -- so everything downstream works identically regardless of which path
produced the page. Each token records how it was obtained and how confident
the reader was, because that provenance is what makes the output auditable.

The OCR path is not a single pass. JoPACC sets its key figures in white on
grey bars and in amber inside ring charts, and a plain Tesseract pass over the
rendered page reads the headings and drops exactly those numbers. Three
separately isolated colour layers recover them. Amber text additionally needs
connected-component filtering, because the ring drawn around a percentage is
itself amber and reads as punctuation that suppresses the digits.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, asdict, field as dc_field
from typing import List, Tuple

import numpy as np
import pymupdf
import pytesseract
from PIL import Image
from scipy import ndimage


@dataclass
class Token:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float
    method: str    # 'native' | 'ocr'
    layer: str     # which ensemble pass produced the winning reading
    orientation: str = "h"   # 'h' upright, 'v' rotated (poster axis labels)
    alternatives: List[str] = dc_field(default_factory=list)

    @property
    def has_conflict(self) -> bool:
        """True when another OCR pass read this position differently."""
        return bool(self.alternatives)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# native text
# --------------------------------------------------------------------------

def _native_tokens(page: "pymupdf.Page") -> List[Token]:
    """Read the text layer, keeping rotated runs whole.

    The quarterly poster labels its horizontal bars with rotated text -- a
    sideways ``Q1 2026`` beside each row. The word-level API splits those into
    ``Q1`` and ``2026`` at unrelated coordinates, which destroys both the label
    and any chance of matching it to its bar. Reading lines instead preserves
    the run, and the line's direction vector says which way it faces.
    """
    out: List[Token] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            vertical = abs(line["dir"][1]) > abs(line["dir"][0])
            if vertical:
                out.append(Token(text, x0, y0, x1, y1, 100.0, "native",
                                 "native", orientation="v"))
                continue
            # Horizontal lines are split back into words so that values and
            # their neighbouring labels stay separable.
            for span in line["spans"]:
                for word, box in _split_span(span):
                    out.append(Token(word, *box, 100.0, "native", "native"))
    return out


def _split_span(span: dict):
    """Approximate per-word boxes inside a span by character width."""
    text = span["text"]
    x0, y0, x1, y1 = span["bbox"]
    if not text.strip():
        return
    width_per_char = (x1 - x0) / max(len(text), 1)
    cursor = 0
    for word in text.split(" "):
        if word:
            wx0 = x0 + cursor * width_per_char
            wx1 = wx0 + len(word) * width_per_char
            yield word, (wx0, y0, wx1, y1)
        cursor += len(word) + 1


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

def _component_filter(mask: np.ndarray, cfg: dict) -> np.ndarray:
    """Keep glyph-sized blobs, drop rings, bars, banners and background art."""
    labelled, _ = ndimage.label(mask)
    keep = np.zeros_like(mask)
    for i, sl in enumerate(ndimage.find_objects(labelled), 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if not (cfg["min_h"] < h < cfg["max_h"] and cfg["min_w"] < w < cfg["max_w"]):
            continue
        if (labelled[sl] == i).sum() / float(h * w) < cfg["min_fill"]:
            continue
        keep |= labelled == i
    return keep


def render_dpi_for(page: "pymupdf.Page", target_px: int, lo: int, hi: int) -> int:
    """Pick a DPI that renders this page to roughly ``target_px`` wide.

    The three report types use very different canvases -- a 960pt slide, a
    1620pt poster, a 540pt portrait page -- so a fixed DPI would render text
    at wildly different pixel sizes and give inconsistent OCR quality.
    """
    inches = page.rect.width / 72.0
    return int(max(lo, min(hi, round(target_px / inches))))


def _ocr_read(source: Image.Image, layer: str, psm: int,
              scale_x: float, scale_y: float, min_conf: float) -> List[Token]:
    frame = pytesseract.image_to_data(
        source, lang="eng", config=f"--psm {psm}",
        output_type=pytesseract.Output.DATAFRAME,
    )
    frame = frame[(frame.conf > min_conf) & frame.text.notna()]
    out = []
    for row in frame.itertuples():
        text = str(row.text).strip()
        if not text:
            continue
        out.append(Token(
            text,
            row.left * scale_x, row.top * scale_y,
            (row.left + row.width) * scale_x, (row.top + row.height) * scale_y,
            float(row.conf), "ocr", layer,
        ))
    return out


def _merge_variants(tokens: List[Token], tol: float) -> List[Token]:
    """Collapse the ensemble's readings of the same glyph run into one token.

    Variants disagree in informative ways. A bar labelled ``1.74 M`` is read
    correctly by one variant and as ``174M`` by another -- a hundredfold
    error that no downstream check would catch on its own. The most confident
    reading wins, but any competing reading of the same position is recorded
    on the token so validation can surface it for review.
    """
    kept: List[Token] = []
    for tok in sorted(tokens, key=lambda t: -t.conf):
        for winner in kept:
            if _same_glyph_run(winner, tok, tol):
                if tok.text != winner.text and tok.text not in winner.alternatives:
                    winner.alternatives.append(tok.text)
                break
        else:
            kept.append(tok)
    return kept


def _same_glyph_run(a: Token, b: Token, tol: float) -> bool:
    """Do two readings cover the same ink?

    Readings of one run rarely agree on extent: ``1.74`` and ``174M`` start
    together but end differently. Containment of the smaller box in the larger
    identifies them as the same run, where comparing centres would not.
    """
    if abs(a.cy - b.cy) > tol:
        return False
    overlap_x = min(a.x1, b.x1) - max(a.x0, b.x0)
    if overlap_x <= 0:
        return False
    narrower = min(a.x1 - a.x0, b.x1 - b.x0)
    return narrower > 0 and overlap_x / narrower >= 0.6


def _ocr_variants(img: Image.Image, cfg: dict) -> List[tuple]:
    """The passes that make up the ensemble, as ``(name, mask_or_None)``.

    Plain greyscale carries the general reading. The binarised luminance
    layers recover type set on filled shapes, where contrast against the page
    is low. The amber layer needs component filtering because the ring drawn
    around a percentage is the same colour as the digits inside it and
    otherwise reads as punctuation that suppresses them.
    """
    arr = np.array(img).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    lum = arr.mean(axis=2)
    comp = cfg["component"]
    available = {
        "greyscale": None,
        "light": lum > cfg["light_min_lum"],
        "dark": lum < cfg["dark_max_lum"],
        "accent": _component_filter(
            (r > cfg["accent_min_r"]) & (b < g - cfg["accent_gb_gap"])
            & (g > cfg["accent_min_g"]), comp),
    }
    return [(name, available[name]) for name in cfg["layers"] if name in available]


def _ocr_tokens(page: "pymupdf.Page", cfg: dict) -> List[Token]:
    dpi = render_dpi_for(page, cfg["target_px"], cfg["min_dpi"], cfg["max_dpi"])
    img = Image.open(io.BytesIO(page.get_pixmap(dpi=dpi).tobytes("png"))).convert("RGB")
    scale_x = page.rect.width / img.width
    scale_y = page.rect.height / img.height

    tokens: List[Token] = []
    for name, mask in _ocr_variants(img, cfg):
        source = img.convert("L") if mask is None else \
            Image.fromarray(np.where(mask, 0, 255).astype("uint8"))
        for psm in cfg["psm_modes"]:
            tokens += _ocr_read(source, name, psm, scale_x, scale_y,
                                cfg["min_confidence"])
    merged = _merge_variants(tokens, cfg["dedupe_tolerance_pt"])

    if cfg.get("read_rotated_text", True):
        merged += _ocr_rotated(img, cfg, scale_x, scale_y,
                               page.rect.width, page.rect.height)
    return merged


def _ocr_rotated(img: Image.Image, cfg: dict, scale_x: float, scale_y: float,
                 page_w: float, page_h: float) -> List[Token]:
    """Second pass over the page turned upright, for sideways axis labels.

    Outlined posters carry the same rotated bar labels as the text-bearing
    ones, and Tesseract will not read them in place. Rotating the whole page
    and mapping the boxes back is cheaper and more reliable than trying to
    locate the rotated regions first.
    """
    turned = img.convert("L").rotate(-90, expand=True)
    found = _ocr_read(turned, "rotated", cfg["psm_modes"][0], 1.0, 1.0,
                      cfg["min_confidence"])
    out: List[Token] = []
    h_px = img.height
    for tok in found:
        # Undo the rotation: a point (x, y) on the turned image came from
        # (y, height - x) on the original.
        x0 = tok.y0 * scale_x
        x1 = tok.y1 * scale_x
        y0 = (h_px - tok.x1) * scale_y
        y1 = (h_px - tok.x0) * scale_y
        if not _plausible_rotated(tok.text):
            continue
        out.append(Token(tok.text, x0, y0, x1, y1, tok.conf, "ocr",
                         "rotated", orientation="v"))
    return out


_ROTATED_KEEP = __import__("re").compile(r"^(Q[1-4]\s*20\d{2}|20\d{2}|Q[1-4])$", 2)


def _plausible_rotated(text: str) -> bool:
    """Only keep rotated readings that look like axis labels.

    A full-page rotated OCR pass also re-reads upright text as nonsense. Since
    the only rotated content in these reports is period labels on bar charts,
    anything that is not one is discarded rather than trusted.
    """
    return bool(_ROTATED_KEEP.match(text.strip()))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def page_has_text_layer(page: "pymupdf.Page", min_chars: int) -> bool:
    return len(page.get_text().strip()) >= min_chars


def extract_tokens(page: "pymupdf.Page", cfg: dict) -> Tuple[List[Token], str]:
    """Return ``(tokens, method)`` for one page.

    ``method`` is the extraction route actually taken, recorded per page so a
    reviewer can tell how any given value was obtained.
    """
    if page_has_text_layer(page, cfg["text_layer"]["min_chars_per_page"]):
        tokens = _native_tokens(page)
        if tokens:
            return tokens, "native"
        # A text layer that yields no words is a structural surprise; fall
        # through to OCR rather than returning an empty page.
    return _ocr_tokens(page, cfg["ocr"]), "ocr"
