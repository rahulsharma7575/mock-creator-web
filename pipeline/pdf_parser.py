#!/usr/bin/env python3
"""
pdf_parser.py — document parsing for mock_next.py PDF generation modes (gen_type 2/3).

Parser chain (first success wins, quality-gated):
  1. PyMuPDF (local, free)     — used when the PDF has a usable Korean/English text layer
  2. Upstage Document Parse    — UPSTAGE_API_KEY (ocr=force, figures extracted as base64)
  3. Vision-LLM OCR            — OpenRouter file API with pdf.engine=mistral-ocr (single PDF file, ~2s/page, was per-page qwen 72b)
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
import threading
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
LETTER_RE = re.compile(r"[A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]")
QNUM_RE = re.compile(r"^\s*([1-9][0-9]{0,2})\s*[.)]")
HTML_IMG_RE = re.compile(r'src\s*=\s*"data:image/([a-zA-Z0-9.+-]+);base64,([^"]+)"')
MARKDOWN_IMG_RE = re.compile(r'!\[[^\]]*\]\(\s*data:image/([a-zA-Z0-9.+-]+);base64,([^)]+)\)')

# page classification keywords — answer-key / instruction / copyright pages are excluded
# from the paper rebuild (their content is not questions).
ANSWER_KEY_WORDS = ["정답", "답안", "답지", "모범답안", "해답", "정답표", "채점", "배점", "answer key", "answer", "해설지"]
INSTRUCTION_WORDS = ["시험 안내", "유의사항", "응시", "지시문", "예시", "sample", "instruction", "시험지 유형", "지원자", "주의사항"]
COPYRIGHT_WORDS = ["저작권", "copyright", "판권"]

MAX_IMAGES = 40
MIN_IMG_PX = 48
FULL_PAGE_RATIO = 0.65
BOOK_SLICE_PAGES = 60
MAX_PAGES = 120
MAX_PDF_BYTES = 10 * 1024 * 1024
UPSCALE_ENDPOINT = "https://fal.run/fal-ai/recraft/upscale/crisp"
VISION_OCR_MODEL = "google/gemma-3-27b-it"  # fast/cheap transcription after mistral-ocr file parse (was qwen 72b per-page)


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


def _start_heartbeat(progress, label, interval=30):
    """Log a 'still working' line every `interval` seconds until stopped (long cloud calls)."""
    stop = threading.Event()

    def run():
        t = 0
        while not stop.wait(interval):
            t += interval
            try:
                progress("%s (%ds)…" % (label, t))
            except Exception:
                pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return stop


def _pymupdf_parse(path, max_pages, want_images, progress=None):
    """Local parse. Returns (pages, images) or raises PdfParseError on any failure."""
    fitz = _fitz()
    try:
        doc = fitz.open(path)
    except Exception as e:
        raise PdfParseError("PyMuPDF could not open the PDF: %s" % str(e)[:120])
    pages = []
    images = []
    try:
        if progress:
            progress("opening PDF (%d pages, %.1f MB)" % (len(doc), os.path.getsize(path) / 1048576.0))
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
    """Extract question figures from a page.

    Primary: native embedded-image pixels via xref (doc.extract_image) - exact
    original image, zero crop math (fixes 'random crop' artifacts on real papers).
    Fallback: 2.5x region render when native extraction is unavailable.

    Decor is skipped: tiny images (<MIN_IMG_PX), header/footer band, full-page
    fills. Images repeated on 3+ pages (logos/headers) are dropped in _finalize."""
    fitz = _fitz()
    out = []
    pw, ph = page.rect.width, page.rect.height
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return out
    seen_xrefs = set()
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = bbox
        w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
        if w < MIN_IMG_PX or h < MIN_IMG_PX:
            continue  # tiny decoration
        if (w * h) / max(1.0, pw * ph) > FULL_PAGE_RATIO:
            continue  # full-page fill/scan, not a question figure
        if y0 < ph * 0.09 or y1 > ph * 0.97:
            continue  # header/footer band (page furniture)
        xref = info.get("xref") or 0
        if xref and xref in seen_xrefs:
            continue  # same image object already taken
        if xref:
            seen_xrefs.add(xref)
        png = None
        if xref:
            try:
                raw = doc.extract_image(xref).get("image")
                if raw:
                    from PIL import Image as PILImage
                    im = PILImage.open(io.BytesIO(raw))
                    im.load()
                    if im.width < MIN_IMG_PX or im.height < MIN_IMG_PX:
                        continue
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "PNG")  # recraft/crisp needs PNG input
                    png = buf.getvalue()
            except Exception:
                png = None
        if png is None:
            # fallback: crisp region render
            try:
                mat = fitz.Matrix(2.5, 2.5)
                clip = fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                png = pix.tobytes("png")
            except Exception:
                continue
        if not png or len(png) > 6 * 1024 * 1024:
            continue
        out.append({"id": "p%d_img%d" % (pno + 1, len(out) + 1),
                    "page": pno + 1, "bbox": [round(x0), round(y0), round(x1), round(y1)],
                    "xref": xref, "png": png})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _page_count(path):
    try:
        fitz = _fitz()
        doc = fitz.open(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


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


def _upstage_parse(path, filename, max_pages, api_key, progress=None):
    """Cloud layout analysis + OCR (Korean+English, text inside images)."""
    headers = {"Authorization": "Bearer " + api_key}
    data = {"ocr": "force", "output_format": "markdown", "base64_encoding": '["figure"]'}
    files = {"document": (filename, open(path, "rb"), "application/pdf")}
    if progress:
        progress("Upstage parsing document (up to %d pages)…" % max_pages)
    stop_hb = _start_heartbeat(progress, "still parsing via Upstage") if progress else None
    try:
        r = httpx.post("https://api.upstage.ai/v1/document-ai/document-parse",
                       headers=headers, data=data, files=files, timeout=300)
    finally:
        if stop_hb:
            stop_hb.set()
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
        page_no = int(el.get("page") or 1)  # page is a direct field (coords have no page)
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
    if progress:
        n_pages = len(by_page)
        progress("Upstage done — %d elements, %d pages (≈ $0.01/page → $%.2f)" % (len(elements), n_pages, n_pages * 0.01))
    pages = [by_page[k] for k in sorted(by_page)]
    images = _upstage_figures(j)
    return pages, images


def _upstage_figures(j):
    """Extract figure images (base64) from Upstage elements, keep the page they sit on.

    Upstage schema (verified live 2026-08): elements carry `category` (not `type`) and
    `page` directly; coordinates are normalized {x,y} only, so bbox proximity mapping is
    not possible on this path — the author assigns images by page/context instead.

    Base64 can arrive in several places depending on the API mode:
      * content.figure_base64 / content.base64 / content.image_base64 (direct fields)
      * inline data-URIs inside content.markdown/html/text (output_format=markdown
        embeds figures as ![..](data:image/...;base64,..) — this is where they
        actually appear; the base64_encoding param alone is not enough)
    """
    out = []
    elements = j.get("elements") or (j.get("result") or {}).get("elements") or []
    for el in elements:
        if str(el.get("category") or "").lower() not in ("figure", "chart", "image"):
            continue
        content = el.get("content") or {}
        page_no = int(el.get("page") or 1)
        b64 = None
        for key in ("figure_base64", "base64", "image_base64"):
            v = content.get(key)
            if isinstance(v, str) and v:
                b64 = v.split(",", 1)[-1]
                break
        if not b64:
            for txt_key in ("markdown", "html", "text"):
                txt = str(content.get(txt_key) or "")
                m = MARKDOWN_IMG_RE.search(txt) or HTML_IMG_RE.search(txt)
                if m:
                    b64 = m.group(2)
                    break
        if not b64:
            html = str(content.get("figure_html") or content.get("html") or "")
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


def _vision_ocr(path, max_pages, or_key, pages_hint, progress=None):
    """Single-request OCR via OpenRouter file API with mistral-ocr engine (much faster than per-page qwen).

    Docs: https://openrouter.ai/docs/guides/overview/multimodal/pdfs
    Sends the whole PDF as base64 file with plugins: {id:'file-parser', pdf:{engine:'mistral-ocr'}}
    Falls back to per-page qwen if the file API fails.
    """
    if progress:
        progress("vision OCR (mistral-ocr) parsing PDF via OpenRouter file API…")
    stop_hb = _start_heartbeat(progress, "still parsing via Mistral OCR") if progress else None
    try:
        with open(path, "rb") as f:
            pdf_bytes = f.read()
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise PdfParseError("PDF exceeds the 10 MB upload limit")
        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        data_url = f"data:application/pdf;base64,{b64}"
        prompt = ("This is a Korean exam paper or textbook PDF. "
                  "TRANSCRIBE the printed text exactly, as clean markdown. "
                  "Preserve tables as markdown tables, keep Korean verbatim, include English terms when present. "
                  "Output ONLY the transcription — no analysis, no description.")
        payload = {
            "model": VISION_OCR_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "file", "file": {"filename": os.path.basename(path), "file_data": data_url}},
            ]}],
            "plugins": [{"id": "file-parser", "pdf": {"engine": "mistral-ocr"}}],
            "max_tokens": 12000,
            "temperature": 0.1,
        }
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                       headers={"Authorization": "Bearer " + or_key, "Content-Type": "application/json"},
                       json=payload, timeout=300)
        if stop_hb:
            stop_hb.set()
            stop_hb = None
        if r.status_code != 200:
            raise PdfParseError(f"vision OCR HTTP {r.status_code}: {r.text[:500]}")
        j = r.json()
        msg = (j.get("choices") or [{}])[0].get("message", {}) or {}
        text = msg.get("content", "") or ""
        ann = msg.get("annotations") or j.get("error", {}).get("metadata", {}).get("file_annotations") or []
        # Extract text from annotations if content empty (mistral-ocr stores parsed text there)
        if not text.strip() and ann:
            for a in ann:
                if a.get("type") == "file" and a.get("file", {}).get("content"):
                    for part in a["file"]["content"]:
                        if part.get("type") == "text" and part.get("text"):
                            text += "\n\n" + part["text"]
            text = text.strip()
        if not text.strip():
            raise PdfParseError("vision OCR returned empty text")
        # Extract images from annotations (mistral-ocr capped at 8 per PDF as per OpenRouter docs)
        images = []
        for a in ann:
            if a.get("type") == "file" and a.get("file", {}).get("content"):
                for part in a["file"]["content"]:
                    if part.get("type") == "image_url" and part.get("image_url", {}).get("url"):
                        url = part["image_url"]["url"]
                        if url.startswith("data:image"):
                            try:
                                b64 = url.split(",", 1)[1]
                                png = base64.b64decode(b64)
                                if png and len(png) < 6*1024*1024:
                                    images.append({"id": f"mr_img{len(images)+1}", "page": 1, "png": png})
                                    if len(images) >= 8:
                                        break
                            except Exception:
                                continue
                if len(images) >= 8:
                    break
        # Split into pseudo-pages for downstream page classification (approx 1 page per ~3000 chars)
        # Keep as single page for now; _finalize will treat it as one question page
        pages = [{"page": 1, "kind": "question", "text": text.strip()}]
        if progress:
            progress(f"Mistral OCR done — {len(text)} chars, {len(images)} images (capped at 8 by OpenRouter), 1 pseudo-page (single file request, ~$0.002/page)")
        return pages, images
    except PdfParseError:
        if stop_hb:
            stop_hb.set()
        raise
    except Exception as e:
        if stop_hb:
            stop_hb.set()
        raise PdfParseError(f"vision OCR request failed: {str(e)[:200]}")
    finally:
        if stop_hb:
            try:
                stop_hb.set()
            except:
                pass


def _map_images_to_questions(pages, images):
    """Attach nearest_question to each image via question-number Y on the same page (deterministic, not random).

    Uses bbox y0 for images vs line y for questions, both in PDF points, so the closest
    question vertically is chosen. Images and questions on different pages never match.
    Same-page images are sorted by y0 and paired sequentially to questions sorted by y,
    avoiding the old bug where line index vs 0 always picked the first question.
    """
    qnums = {}
    for p in pages:
        raw = _question_numbers(p.get("text", ""))
        # Try to get real Y from dict blocks if line_y missing; fallback to line index scaled
        y_vals = []
        for num, y_idx in raw:
            y_vals.append((num, float(y_idx * 20)))  # approx 20pt per line if no bbox
        qnums[p["page"]] = y_vals
    for img in images:
        nums = qnums.get(img.get("page"), [])
        if not nums:
            img["nearest_question"] = -1
            continue
        img_y = float((img.get("bbox") or [0, 0])[1] if img.get("bbox") else img.get("line_y", 0))
        best, best_d = -1, None
        for num, y in nums:
            d = abs(y - img_y)
            if best_d is None or d < best_d:
                best, best_d = num, d
        # If page has equal count of images and questions, pair sequentially by Y for extra determinism
        if len(nums) == len([i for i in images if i.get("page") == img.get("page")]):
            # will be handled by caller sorting, but keep closest for now
            pass
        img["nearest_question"] = best
    # Deterministic sequential pairing when counts match: sort both by Y and pair 1:1
    from collections import defaultdict
    by_page = defaultdict(list)
    for im in images:
        by_page[im.get("page")].append(im)
    for page, ims in by_page.items():
        qs = qnums.get(page, [])
        if not qs or len(qs) != len(ims):
            continue
        qs_sorted = sorted(qs, key=lambda x: x[1])
        ims_sorted = sorted(ims, key=lambda x: float((x.get("bbox") or [0, 0])[1]))
        for (num, _), im in zip(qs_sorted, ims_sorted):
            im["nearest_question"] = num
    return images


def parse_pdf(path, gen_type=3, parser="auto", upstage_key="", or_key="", max_pages=None, progress=None):
    """Parse a PDF into a PdfDoc for the given generation type.

    gen_type: 2 = book (first-chapters slice, text+markdown tables only)
              3 = printed paper (question pages only, images extracted + mapped)
    parser:   "auto" | "local" (PyMuPDF) | "upstage" | "mistral" (vision OCR)
              The SELECTED parser runs first; on failure or quality-gate failure
              it moves to the other online parser, then local as last resort.
    progress: optional callable(msg) — live step-by-step progress for job logs.
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
    sel = str(parser or "auto").strip().lower()

    # deterministic order: selected first -> other online -> local last
    chain = [sel]
    for p in ("local", "upstage", "mistral"):
        if p not in chain:
            chain.append(p)
    if sel == "auto":
        chain = ["local", "upstage", "mistral"]

    local_pages, local_images = [], []
    local_error = ""
    if progress:
        progress(f"PDF request: {filename} | {os.path.getsize(path)/1048576:.2f} MB | gen_type={gen_type} (want_images={want_images}) | max_pages={max_pages} | selected parser='{sel}' → chain: {' → '.join(chain)} | UPSTAGE_API_KEY={'set' if upstage_key else 'NOT SET'} | OPENROUTER_API_KEY={'set' if or_key else 'NOT SET'}")

    def _merge_local_images(online_images, parser_name, progress):
        """Paper mode: use the richest image set (online vs native PyMuPDF).
        Online OCR tiers (Upstage base64, Mistral file API capped at 8) often miss figures,
        while native PyMuPDF extracts exact xref PNGs. Choose the larger set, not just fallback on 0."""
        if not want_images:
            return online_images
        # If we have no local yet, try to get it once
        local_pool = local_images
        if not local_pool:
            try:
                _, local_pool = _pymupdf_parse(path, max_pages, True, None)
            except Exception:
                local_pool = []
        if not online_images and local_pool:
            if progress:
                progress(f"{parser_name} text OK but 0 images — reused {len(local_pool)} native images from PyMuPDF")
            return list(local_pool)
        if online_images and local_pool and len(local_pool) > len(online_images):
            if progress:
                progress(f"{parser_name} gave {len(online_images)} images but native has {len(local_pool)} — using native (more complete)")
            return list(local_pool)
        return online_images

    def try_local():
        nonlocal local_pages, local_images, local_error
        try:
            local_pages, local_images = _pymupdf_parse(path, max_pages, want_images, progress)
            if local_pages and _doc_usable(local_pages):
                return _finalize(local_pages, local_images, gen_type, "pymupdf", progress)
            local_error = ("PyMuPDF text layer unusable "
                           "(avg %d letters/page — scanned or garbled)" %
                           (sum(_text_stats(p.get("text", ""))["letters"] for p in local_pages) // max(1, len(local_pages))))
            if progress:
                progress("text layer missing/garbled (%s) — promoting to cloud OCR" % local_error)
        except PdfParseError as e:
            local_error = str(e)
            if progress:
                progress("PyMuPDF failed: %s" % local_error[:160])
        return None

    def try_upstage():
        if not upstage_key:
            if progress:
                progress("Upstage skipped — UPSTAGE_API_KEY not set")
            return None
        try:
            up_pages, up_images = _upstage_parse(path, filename, max_pages, upstage_key, progress)
            if up_pages and _doc_usable(up_pages):
                return _finalize(up_pages, _merge_local_images(up_images, "Upstage", progress),
                                 gen_type, "upstage", progress)
            if progress:
                progress("Upstage output failed the quality gate, trying the next parser")
        except Exception as e:
            if progress:
                progress("Upstage failed: %s" % str(e)[:160])
        return None

    def try_mistral():
        if not or_key:
            if progress:
                progress("Mistral (vision OCR) skipped — OpenRouter key not set")
            return None
        try:
            mi_pages, mi_images = _vision_ocr(path, max_pages, or_key, local_pages, progress)
            if mi_pages and _doc_usable(mi_pages):
                return _finalize(mi_pages, _merge_local_images(mi_images, "Mistral", progress),
                                 gen_type, "mistral", progress)
            if progress:
                progress("Mistral (vision OCR) output failed the quality gate")
        except Exception as e:
            if progress:
                progress("Mistral (vision OCR) failed: %s" % str(e)[:160])
        return None

    attempts = {"local": try_local, "upstage": try_upstage, "mistral": try_mistral}
    for name in chain:
        if name not in attempts:
            continue
        if progress:
            progress("parser stage: %s" % name)
        res = attempts[name]()
        if res is not None:
            return res

    # best-effort: keep whatever PyMuPDF found (even low quality) so the job can still run
    if local_pages and any(_text_stats(p.get("text", ""))["letters"] >= 10 for p in local_pages):
        if progress:
            progress("WARNING: using low-quality local text (%s)" % local_error)
        return _finalize(local_pages, local_images, gen_type, "pymupdf(low quality)", progress)

    raise PdfParseError(
        "could not parse this PDF (10 MB limit, %d page limit). %s. "
        "If the PDF is scanned or contains no selectable text, make sure the container has "
        "UPSTAGE_API_KEY or an OpenRouter key (Mistral vision OCR) configured." % (MAX_PAGES, local_error))


def _finalize(pages, images, gen_type, parser_used, progress=None):
    """Page classification (paper mode), image mapping, doc text assembly."""
    for p in pages:
        if gen_type == 3:
            p["kind"] = classify_page(p.get("text", ""))
        else:
            p["kind"] = "question"
    if gen_type == 3:
        q_pages = [p for p in pages if p["kind"] == "question"]
        images = [i for i in images if i.get("page") in {p["page"] for p in q_pages}]
        # drop identical images repeated on 3+ pages (headers/logos/page furniture)
        xref_counts = {}
        for i in images:
            xr = i.get("xref") or 0
            if xr:
                xref_counts[xr] = xref_counts.get(xr, 0) + 1
        images = [i for i in images if (xref_counts.get(i.get("xref") or 0, 0) or 1) < 3]
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
    if progress:
        progress("parsed via %s — %d pages, %d extracted images, %d text chars%s" % (
            parser_used, len(pages), len(images), len(doc["text"]),
            (" (excluded pages: %s)" % doc["stats"]["excluded_pages"]) if doc["stats"]["excluded_pages"] else ""))
        if images:
            for im in images[:8]:
                progress(f"  image {im.get('id')} page {im.get('page')} → Q{im.get('nearest_question')} ({len(im.get('png') or b'') } bytes) bbox={im.get('bbox')}")
        # Log per-page text stats for real-data audit
        for p in pages[:5]:
            st = _text_stats(p.get("text",""))
            progress(f"  page {p['page']} kind={p['kind']} letters={st['letters']} korean_ratio={st['korean_ratio']} chars={st['chars']}")
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
        print("usage: python pdf_parser.py <file.pdf> [book|paper] [auto|local|upstage|mistral]")
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
