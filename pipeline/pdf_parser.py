#!/usr/bin/env python3
"""
pdf_parser.py — document parsing for mock_next.py PDF generation modes (gen_type 2/3).

Parser chain (first success wins, quality-gated):
  1. PyMuPDF (local, free)     — used when the PDF has a usable Korean/English text layer
  2. Upstage Document Parse    — UPSTAGE_API_KEY (ocr=force, figures extracted as base64)
  3. Vision-LLM OCR            — rendered page images read by a vision model on OpenRouter
     (Mistral OCR is NOT hosted on OpenRouter - verified live 2026-08; this tier
     replaces it and needs no extra keys)
  4. PyMuPDF best-effort       — whatever text exists (warned)
All fail → PdfParseError (the job fails with this clear message).

Output: PdfDoc
  text         : markdown text (book mode: first-chapters slice; paper mode: question pages only)
  pages        : [{page, kind, text}]  (kind: question | answer_key | instruction | other)
  images       : [ImageRef] (paper mode only)
  parser_used  : which parser won
  stats        : per-document quality numbers

ImageRef: {id, page, nearest_question, png}  (png = raw PNG bytes)

Upscale helper for extracted images: upscale_image(url, key) via fal-ai/recraft/upscale/crisp.
"""
import base64
import io
import os
import re
import sys

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
LETTER_RE = re.compile(r"[A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]")
QNUM_RE = re.compile(r"^\s*([1-9][0-9]{0,2})\s*[.)]")
HTML_IMG_RE = re.compile(r'src\s*=\s*"data:image/([a-zA-Z0-9.+-]+);base64,([^"]+)"')

# page classification keywords — answer-key / instruction / copyright pages are excluded
# from the paper rebuild (their content is not questions).
ANSWER_KEY_WORDS = ["정답", "답안", "답지", "모범답안", "해답", "정답표", "채점", "배점", "answer key", "answer", "해설지"]
INSTRUCTION_WORDS = ["시험 안내", "유의사항", "응시", "지시문", "예시", "sample", "instruction", "시험지 유형", "지원자", "주의사항"]
COPYRIGHT_WORDS = ["저작권", "copyright", "판권"]

MAX_IMAGES = 20
MIN_IMG_PX = 48
FULL_PAGE_RATIO = 0.65
BOOK_SLICE_PAGES = 60
MAX_PAGES = 120
MAX_PDF_BYTES = 10 * 1024 * 1024
UPSCALE_ENDPOINT = "https://fal.run/fal-ai/recraft/upscale/crisp"
VISION_OCR_MODEL = "qwen/qwen2.5-vl-72b-instruct"  # vision OCR tier (Mistral OCR is not on OpenRouter)


class PdfParseError(RuntimeError):
    """Raised when every parser fails (job fails with this message)."""


def _korean_ratio(text):
    letters = LETTER_RE.findall(text or "")
    if not letters:
        return 0.0
    return len(HANGUL_RE.findall(text or "")) / len(letters)


def _text_stats(text):
    text = text or ""
    chars = len(text)
    letters = len(LETTER_RE.findall(text))
    return {
        "chars": chars,
        "letters": letters,
        "korean_ratio": round(_korean_ratio(text), 3),
        "letter_fraction": round(letters / chars, 3) if chars else 0.0,
    }


def _doc_usable(pages):
    """Quality gate: enough real text per page (catches scanned AND garbled).

    Korean papers are digit/option-marker heavy, so letter fraction alone is a bad
    judge — instead require a meaningful amount of Korean (or substantial English)."""
    stats = [_text_stats(p.get("text", "")) for p in pages]
    avg = sum(s["letters"] for s in stats) / max(1, len(stats))
    kr = sum(s["korean_ratio"] for s in stats) / max(1, len(stats))
    return avg >= 25 and (kr >= 0.1 or avg >= 80)


def classify_page(text):
    t = (text or "").lower()
    hits = []
    for w in ANSWER_KEY_WORDS:
        if w.lower() in t:
            hits.append(w)
    if hits and len(hits) >= 1:
        return "answer_key"
    for w in INSTRUCTION_WORDS:
        if w.lower() in t:
            return "instruction"
    for w in COPYRIGHT_WORDS:
        if w.lower() in t:
            return "other"
    if QNUM_RE.search(t) or _korean_ratio(t) > 0.1:
        return "question"
    return "other"


def _question_numbers(page_text):
    """(number, y) pairs for question-number lines on a page (used for image mapping)."""
    out = []
    for i, line in enumerate((page_text or "").splitlines()):
        m = QNUM_RE.match(line)
        if m:
            out.append((int(m.group(1)), i))
    return out


def _fitz():
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
        return fitz
    except ImportError:
        import fitz
        return fitz


def _pymupdf_parse(path, max_pages, want_images):
    """Local parse. Returns (pages, images) or raises PdfParseError on any failure."""
    fitz = _fitz()
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise PdfParseError("PyMuPDF could not open the PDF: %s" % str(e)[:120])
    pages = []
    images = []
    try:
        for pno in range(min(len(doc), max_pages)):
            page = doc[pno]
            txt = page.get_text("text") or ""
            md = [txt]
            try:
                tables = page.find_tables()
                for tb in tables.tables:
                    md.append("\n\n" + (tb.to_markdown() or ""))
            except Exception:
                pass
            page_text = "\n\n".join([m for m in md if m]).strip()
            pages.append({"page": pno + 1, "kind": "question", "text": page_text})
            if want_images:
                images += _pymupdf_images(doc, page, pno)
    except Exception as e:
        raise PdfParseError("PyMuPDF parse failed: %s" % str(e)[:120])
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return pages, images


def _pymupdf_images(doc, page, pno):
    """Extract embedded raster images as crisp 2.5x region renders (PNG bytes)."""
    fitz = _fitz()
    out = []
    pw, ph = page.rect.width, page.rect.height
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return out
    seen = set()
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = bbox
        w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
        if w < MIN_IMG_PX or h < MIN_IMG_PX:
            continue
        if (w * h) / max(1.0, pw * ph) > FULL_PAGE_RATIO:
            continue  # full-page scan/background, not a question figure
        key = (round(x0), round(y0), round(x1), round(y1))
        if key in seen:
            continue
        seen.add(key)
        try:
            mat = fitz.Matrix(2.5, 2.5)
            clip = fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2)
            pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
            png = pix.tobytes("png")
            if len(png) > 6 * 1024 * 1024:
                continue
            out.append({"id": "p%d_img%d" % (pno + 1, len(out) + 1),
                        "page": pno + 1, "bbox": [round(x0), round(y0), round(x1), round(y1)],
                        "png": png})
        except Exception:
            continue
        if len(out) >= MAX_IMAGES:
            break
    return out


def _render_page_png(path, page_no, zoom=2.0):
    """Render one page to PNG bytes (used for the vision-OCR tier)."""
    fitz = _fitz()
    doc = fitz.open(path)
    try:
        page = doc[page_no]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def _upstage_parse(path, filename, max_pages, api_key):
    """Cloud layout analysis + OCR (Korean+English, text inside images)."""
    headers = {"Authorization": "Bearer " + api_key}
    data = {"ocr": "force", "output_format": "markdown", "base64_encoding": '["figure"]'}
    files = {"document": (filename, open(path, "rb"), "application/pdf")}
    try:
        r = httpx.post("https://api.upstage.ai/v1/document-ai/document-parse",
                       headers=headers, data=data, files=files, timeout=300)
    finally:
        try:
            files["document"][1].close()
        except Exception:
            pass
    if r.status_code != 200:
        raise PdfParseError("Upstage HTTP %s: %s" % (r.status_code, r.text[:200]))
    j = r.json()
    elements = j.get("elements") or j.get("result", {}).get("elements") or []
    if not elements:
        raise PdfParseError("Upstage returned no elements")
    by_page = {}
    for el in elements:
        coords = el.get("coordinates") or []
        page_no = 1
        if coords:
            page_no = int(coords[0].get("page", 1) or 1)
        if page_no > max_pages:
            continue
        content = el.get("content") or {}
        text = ""
        if isinstance(content, str):
            text = content
        else:
            text = (content.get("markdown") or content.get("text") or
                    content.get("html") or content.get("table_html") or "")
        page = by_page.setdefault(page_no, {"page": page_no, "kind": "question", "text": ""})
        if text:
            page["text"] += "\n\n" + str(text).strip()
    if not by_page:
        raise PdfParseError("Upstage parsed no pages")
    pages = [by_page[k] for k in sorted(by_page)]
    images = _upstage_figures(j)
    return pages, images


def _upstage_figures(j):
    """Extract figure images (base64) from Upstage elements, keep the page they sit on."""
    out = []
    elements = j.get("elements") or []
    for el in elements:
        if (el.get("type") or "") not in ("figure", "chart"):
            continue
        content = el.get("content") or {}
        page_no = 1
        coords = el.get("coordinates") or []
        if coords:
            page_no = int(coords[0].get("page", 1) or 1)
        b64 = None
        for key in ("figure_base64", "base64", "image_base64"):
            v = content.get(key)
            if isinstance(v, str) and v:
                b64 = v.split(",", 1)[-1]
                break
        if not b64:
            html = content.get("figure_html") or ""
            m = HTML_IMG_RE.search(html)
            if m:
                b64 = m.group(2)
        if not b64:
            continue
        try:
            png = base64.b64decode(b64)
        except Exception:
            continue
        if len(png) > 6 * 1024 * 1024 or not png:
            continue
        out.append({"id": "up_img%d" % (len(out) + 1), "page": page_no, "png": png})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _vision_ocr(path, max_pages, or_key, pages_hint):
    """Per-page OCR via a vision LLM on OpenRouter (rendered page PNGs, Korean+English).

    Mistral OCR is not hosted on OpenRouter (verified 2026-08), so this tier reads the
    rendered pages with a vision model — same role, no extra keys needed."""
    model = VISION_OCR_MODEL
    pages = []
    for pno in range(max_pages):
        try:
            png = _render_page_png(path, pno)
        except Exception as e:
            if pno == 0:
                raise PdfParseError("could not render pages for OCR: %s" % str(e)[:120])
            break
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        prompt = ("This is a scanned page of a Korean exam paper or textbook. "
                  "TRANSCRIBE the printed text exactly, as clean markdown. "
                  "Preserve tables as markdown tables, keep Korean verbatim, include "
                  "English terms when present. Output ONLY the transcription — no analysis, "
                  "no description, no commentary, no character counting.")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]}],
            "max_tokens": 6000,
            "temperature": 0.1,
        }
        try:
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                           headers={"Authorization": "Bearer " + or_key,
                                    "Content-Type": "application/json"},
                           json=payload, timeout=240)
        except Exception as e:
            raise PdfParseError("vision OCR request failed: %s" % str(e)[:120])
        if r.status_code != 200:
            raise PdfParseError("vision OCR HTTP %s: %s" % (r.status_code, r.text[:200]))
        j = r.json()
        text = (j.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        pages.append({"page": pno + 1, "kind": "question", "text": text.strip()})
    if not pages:
        raise PdfParseError("vision OCR returned no pages")
    return pages, []  # figure extraction not available on this path -> fresh generation


def _map_images_to_questions(pages, images):
    """Attach nearest_question to each image via question-number lines on the same page."""
    qnums = {}
    for p in pages:
        qnums[p["page"]] = _question_numbers(p.get("text", ""))
    for img in images:
        nums = qnums.get(img.get("page"), [])
        if not nums:
            img["nearest_question"] = -1
            continue
        best, best_d = -1, None
        for num, y in nums:
            d = abs(y - img.get("line_y", 0))
            if best_d is None or d < best_d:
                best, best_d = num, d
        img["nearest_question"] = best
    return images


def parse_pdf(path, gen_type=3, parser="auto", upstage_key="", or_key="", max_pages=None):
    """Parse a PDF into a PdfDoc for the given generation type.

    gen_type: 2 = book (first-chapters slice, text+markdown tables only)
              3 = printed paper (question pages only, images extracted + mapped)
    parser:   "auto" (full chain) | "local" (PyMuPDF only)
    Raises PdfParseError with a clear message when nothing usable was extracted.
    """
    path = str(path)
    if not os.path.exists(path):
        raise PdfParseError("PDF file not found on the worker: %s" % path)
    if os.path.getsize(path) > MAX_PDF_BYTES:
        raise PdfParseError("PDF exceeds the 10 MB upload limit")
    if not max_pages:
        max_pages = MAX_PAGES if gen_type == 3 else BOOK_SLICE_PAGES
    max_pages = min(max_pages, MAX_PAGES)
    want_images = gen_type == 3
    filename = os.path.basename(path)

    local_error = ""
    pages, images = [], []
    try:
        pages, images = _pymupdf_parse(path, max_pages, want_images)
        if pages and _doc_usable(pages):
            return _finalize(pages, images, gen_type, "pymupdf")
        local_error = ("PyMuPDF text layer unusable "
                       "(avg %d letters/page — scanned or garbled)" %
                       (sum(_text_stats(p.get("text", ""))["letters"] for p in pages) // max(1, len(pages))))
    except PdfParseError as e:
        local_error = str(e)

    if parser == "local":
        raise PdfParseError("PDF has no usable text layer (%s) and PDF parser is set to 'Local only' — "
                            "set it to Auto or add UPSTAGE_API_KEY / OpenRouter key for OCR" % local_error)

    if upstage_key:
        try:
            up_pages, up_images = _upstage_parse(path, filename, max_pages, upstage_key)
            if up_pages and _doc_usable(up_pages):
                return _finalize(up_pages, up_images, gen_type, "upstage")
            print("[pdf] Upstage output failed the quality gate, trying vision OCR", flush=True)
        except PdfParseError as e:
            print("[pdf] Upstage failed: %s" % str(e)[:160], flush=True)

    if or_key:
        try:
            mi_pages, mi_images = _vision_ocr(path, max_pages, or_key, pages)
            if mi_pages and _doc_usable(mi_pages):
                return _finalize(mi_pages, mi_images, gen_type, "vision-ocr")
            print("[pdf] vision OCR output failed the quality gate", flush=True)
        except PdfParseError as e:
            print("[pdf] vision OCR failed: %s" % str(e)[:160], flush=True)

    # best-effort: keep whatever PyMuPDF found (even low quality) so the job can still run
    if pages and any(_text_stats(p.get("text", ""))["letters"] >= 10 for p in pages):
        print("[pdf] WARNING: using low-quality local text (%s)" % local_error, flush=True)
        return _finalize(pages, images, gen_type, "pymupdf(low quality)")

    raise PdfParseError(
        "could not parse this PDF (10 MB limit, %d page limit). %s. "
        "If the PDF is scanned or contains no selectable text, make sure the container has "
        "UPSTAGE_API_KEY or an OpenRouter key (vision OCR) configured." % (MAX_PAGES, local_error))


def _finalize(pages, images, gen_type, parser_used):
    """Page classification (paper mode), image mapping, doc text assembly."""
    for p in pages:
        if gen_type == 3:
            p["kind"] = classify_page(p.get("text", ""))
        else:
            p["kind"] = "question"
    if gen_type == 3:
        q_pages = [p for p in pages if p["kind"] == "question"]
        images = [i for i in images if i.get("page") in {p["page"] for p in q_pages}]
        _map_images_to_questions(q_pages, images)
    else:
        images = []
    text_parts = []
    for p in pages:
        if p["kind"] != "question" or not p.get("text", "").strip():
            continue
        text_parts.append("--- Page %d ---\n%s" % (p["page"], p["text"].strip()))
    doc = {
        "text": "\n\n".join(text_parts),
        "pages": pages,
        "images": images,
        "parser_used": parser_used,
        "stats": {"pages": len(pages), "images": len(images),
                  "excluded_pages": [p["page"] for p in pages if p["kind"] != "question"]},
    }
    return doc


def upscale_image(url, key, timeout=240):
    """Upscale one PNG via fal-ai/recraft/upscale/crisp. Returns PNG bytes (raise on failure)."""
    r = httpx.post(UPSCALE_ENDPOINT,
                   headers={"Authorization": "Key " + key, "Content-Type": "application/json"},
                   json={"image_url": url}, timeout=timeout)
    if r.status_code != 200:
        raise PdfParseError("upscale HTTP %s: %s" % (r.status_code, r.text[:200]))
    j = r.json()
    img = j.get("image") or {}
    u = img.get("url") or ""
    if not u:
        raise PdfParseError("upscale response missing image url")
    data = httpx.get(u, timeout=120)
    data.raise_for_status()
    return data.content


def png_to_bytes(buf):
    return buf


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python pdf_parser.py <file.pdf> [book|paper] [auto|local]")
        sys.exit(1)
    mode = sys.argv[2] if len(sys.argv) > 2 else "paper"
    parser = sys.argv[3] if len(sys.argv) > 3 else "auto"
    gen = 2 if mode == "book" else 3
    try:
        d = parse_pdf(sys.argv[1], gen_type=gen, parser=parser,
                      upstage_key=os.environ.get("UPSTAGE_API_KEY", ""),
                      or_key=os.environ.get("OPENROUTER_API_KEY", ""))
        print("parser:", d["parser_used"])
        print("pages:", d["stats"]["pages"], "images:", d["stats"]["images"],
              "excluded:", d["stats"]["excluded_pages"])
        print("text chars:", len(d["text"]))
        for i in d["images"][:5]:
            print("  img", i["id"], "page", i["page"], "-> Q", i.get("nearest_question"),
                  "png", len(i["png"]), "bytes")
        print("---- first 600 chars ----")
        print(d["text"][:600])
    except PdfParseError as e:
        print("ERROR:", e)
        sys.exit(2)
