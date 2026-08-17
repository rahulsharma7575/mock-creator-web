#!/usr/bin/env python3
"""
mock_next.py — ONE-CLICK full mock creator for Himal KB.

Does EVERYTHING for the next mock number (author -> proofread -> PocketBase records ->
listening audio -> TOPIK images -> uploads), so the user just runs Mock.cmd and relaxes.

    uv run --with httpx --with pillow python mock_next.py            # full run (next mock)
    uv run --with httpx --with pillow python mock_next.py --dry-run  # author + validate + save only
    uv run --with httpx --with pillow python mock_next.py --mock 9   # force a specific number

Pipeline:
  1. next mock number (scans mock<N>/ dirs + legacy mock<N>_questions.json)
  2. author 40 questions via LLM (EPS-TOPIK rules from mock-blueprint.md)
  3. LLM proofread pass (grammar, 띄어쓰기, naturalness, register) + strict validation
  4. save mock<N>/questions/mock<N>_questions.json
  5. create 40 records in PocketBase `questions` (subject = Korean Language UBT)
     -> capture record ids as pbId in the JSON
  6. run mock_audio_builder.py --mock N -> upload each mp3 to its record's audio field
  7. generate TOPIK images (z-image via Magnific MCP, 5 credits/img) -> WebP -> upload to image field
     (fallback: --img-model nano-banana via OpenRouter)
  8. summary report to mock<N>/extra/run_report.json
"""
import argparse
import base64
import concurrent.futures
import datetime
import io
import json
import os
import pathlib
from pathlib import Path
import random
import re
import subprocess
import sys
import time

import httpx

import magnific_mcp

SRC = pathlib.Path(__file__).resolve().parent
BASE = pathlib.Path(os.environ.get("MOCK_ROOT") or SRC)  # MOCK_ROOT override for container/web use

# ---------------------------------------------------------------------------
# Pipeline configuration — every dynamic value lives in DEFAULTS (classic
# Mock.cmd behavior). Override with:  --config <file.json>  or  $MOCK_CONFIG.
# The SaaS app mirrors these 1:1 into the PocketBase `mock_config` collection
# and materializes a JSON file per job; the GUI edits the same fields.
# ---------------------------------------------------------------------------

DEFAULTS = {
    # network / providers
    "or_api": "https://openrouter.ai/api/v1/chat/completions",
    "or_img": "https://openrouter.ai/api/v1/images/generations",
    "llm_timeout_s": 600,
    # push target (per client)
    "pb_base": "https://ubt.wts.com.np",
    "pb_email": "shiva@cld.com.np",
    "pb_pass": "",
    "subject_id": "illfosglou0e3j6",        # Korean Language UBT
    # exam shape
    "question_count": 40,
    "reading_count": 20,
    "listening_count": 20,
    "image_count": 22,                       # target number of random image questions
    "image_count_min": 18,
    "image_count_max": 26,
    "difficulty_profile": "creative+medium",   # fixed: balanced - mostly medium, few hard, never easy/very-hard
    "focus": "",                                  # free-text teacher guidance on which topics/types to prioritize
    "marks_per_question": 1,
    "negative_marks": 0,
    "is_active": True,
    "duration_minutes": 50,
    "pass_marks": 24,
    "shuffle_questions": True,
    "shuffle_options": False,
    "exam_type": "mock",                     # exam_type of the auto-created exam record
    "exam_status": "draft",                  # draft = review-then-publish | published = immediate
    "push_enabled": True,                    # False = generate locally, never upload anywhere
    "dry_mode": "questions",                 # questions | images | audio - set by worker for dry runs (MUST stay in DEFAULTS or load_config drops it)
    # duplicate avoidance against previous mocks (end-user app library)
    "dedup_enabled": True,                   # check last N mock exams and steer the author away from repeats
    "dedup_sets": 5,                         # how many of the latest mock exams to check
    # image provider - DYNAMIC: fal-ai/z-image/turbo (Fal.ai) | z-image (Magnific) | nano-banana (OpenRouter)
    "fal_api": "https://queue.fal.run/fal-ai/z-image/turbo",
    "fal_size": 512,                         # Fal.ai square image size (1:1)
    # question creator (author) — dual provider: Gemini primary, OpenRouter fallback
    "author_provider": "gemini",             # gemini | openrouter (gemini tried first, auto-fallback on quota/error)
    "gemini_model": "google/gemini-2.5-flash",  # PRIMARY author via direct Gemini API (google/ prefix auto-stripped)
    "gemini_api": "https://generativelanguage.googleapis.com/v1beta",
    "author_model": "google/gemini-2.5-flash",   # OpenRouter author (fallback when Gemini fails/quota exceeded)
    "author_retries": 3,
    "author_max_tokens": 32000,
    "author_timeout_s": 600,
    # proofreading — OpenRouter
    "proof_model": "qwen/qwen3.5-flash-02-23",
    "proof_retries": 2,
    "proof_max_tokens": 32000,
    "proof_temperature": 0.3,
    "proof_timeout_s": 600,
    # repair pass (always a strong model)
    "repair_model": "google/gemini-2.5-flash",
    "repair_max_tokens": 4000,
    # images — Magnific only (z-image confirmed; p-image-ideogram-1k auto-degrades
    # to z-image while Magnific does not expose it on the REST API)
    "img_model": "z-image",
    "img_fallback_model": "p-image-ideogram-1k",
    "img_workers": 3,
    "img_retries": 2,
    "img_fallback_retries": 2,
    "img_aspect": "1:1",
    "img_size": "square_hd",
    "img_quality": 80,
    "img_max_size": 1024,
    "img_timeout_s": 240,
    "image_style_prompt": (
        "Simple flat vector illustration in the standard Korean EPS-TOPIK test style, "
        "VIVID COLOURFUL palette (never black-and-white, never muted), minimal clean line art, "
        "plain solid white background, simple everyday scene, centered single main subject, "
        "clear silhouette, no gradients, no photorealism, "
        "do NOT write the text 'EPS-TOPIK' or any exam title/logo in the image, "
        "small incidental text is allowed only when the scene naturally requires it (e.g. storefront sign), "
        "no watermark, no border"
    ),
    "image_verify_after": True,
    # audio / TTS — OpenRouter
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "deepgram/flux-tts:free",
    "tts_fallback_voice": "",
    "tts_male_voice": "",                         # empty = auto per TTS model (fish free male / MAI ko-KR-InJoon)
    "tts_female_voice": "",                       # empty = auto per TTS model (fish free female / MAI ko-KR-Haena)
    "tts_fallback_male_voice": "",                # fallback model male speaker (empty = auto per fallback model)
    "tts_fallback_female_voice": "",              # fallback model female speaker (empty = auto per fallback model)
    "gen_type": 1,                      # 1 = random standard | 2 = book PDF | 3 = printed paper PDF
    "pdf_path": "",                     # worker-downloaded PDF for gen_type 2/3
    "pdf_parser": "auto",               # auto (PyMuPDF -> Upstage -> Mistral) | local (PyMuPDF only)
    "upscale_pdf_images": True,         # recraft/upscale/crisp on extracted paper images (fal credits)
    "tts_rate": 44100,
    "tts_gap_ms": 400,
    "tts_speed": 1.0,                    # speech speed (0.5-2.0); 1.0 = normal
    "tts_natural_pacing": False,         # relaxed 0.92 speed + >=450ms gaps + pause after ?/!
    "tts_polish": False,                 # loudnorm + highpass + fades on merged clips
    "tts_atempo_models": "x-ai/grok-voice-tts-1.0",  # model fragments where speed is forced via ffmpeg atempo
    "tts_male_speed": 0.0,               # per-voice speed (0 = follow tts_speed)
    "tts_female_speed": 0.0,
    "tts_fallback_male_speed": 0.0,
    "tts_fallback_female_speed": 0.0,
    "listening_blank_count": 5,          # resolved blank count (runtime-picked from BLANK_BAND)
    "listening_picture_count": 5,        # resolved picture count (see bands below; runtime-picked)
    "listening_picture_min": 5,          # EPS-TOPIK band: 5-8 picture listening questions
    "listening_picture_max": 8,
    "reading_image_count": 10,           # default # of the 20 reading questions that carry a single image
    "gemini_vision_scan": True,          # paper mode: feed rendered question pages to Gemini author (multimodal)
    "tts_voices": {},                          # optional speaker->voice map override
}

DIFFICULTY_PROFILES = {
    "creative+difficult": (
        "Difficulty: \"medium\", \"hard\", or \"very hard\" only (no easy) — lean toward hard. "
        "Make questions creative: tricky but fair distractors, complex real-life situations, "
        "nuanced grammar traps."),
    "creative+medium": (
        "Difficulty: mostly \"medium\" with a few \"hard\" (no easy). Creative everyday "
        "situations with natural, believable distractors."),
    "hard": (
        "Difficulty: \"hard\" or \"very hard\" only — complex grammar, idioms, multi-step "
        "comprehension, minimal \"medium\"."),
    "standard": (
        "Difficulty: balanced mix of \"medium\", \"hard\" and \"very hard\" (no easy), "
        "standard EPS-TOPIK UBT feel."),
}

CFG = dict(DEFAULTS)          # active config (after file/env overrides)
CFG_PATH = None               # config file path (propagated to subprocesses via MOCK_CONFIG)


def _coerce_value(default, raw):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(raw))
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    if isinstance(default, list) and raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            return default
    if isinstance(default, dict) and raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw


def load_config(path=None):
    """Merge <--config file> + $MOCK_CONFIG + individual $MOCK_* env vars over DEFAULTS.

    Returns True when a config file was applied (switches model selection from
    interactive menus to config values)."""
    global CFG, CFG_PATH, CONFIG_LOADED
    path = path or os.environ.get("MOCK_CONFIG")
    if path and os.path.exists(path):
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in DEFAULTS:
                CFG[k] = v
        CFG_PATH = str(path)
        CONFIG_LOADED = True
    for k in DEFAULTS:
        env = os.environ.get("MOCK_" + k.upper())
        if env is not None:
            CFG[k] = _coerce_value(DEFAULTS[k], env)
    return CFG_PATH is not None


def render_prompt(template):
    """Fill {placeholders} in a prompt template from the active config.

    The hard 'MUST' counts (picture listening / blank listening / images) are
    clamped to what THIS exam can actually contain. Dry runs use 3-question
    samples (e.g. dry images = 3 reading, 0 listening), so an unclamped
    "exactly 5 picture LISTENING questions" makes the model invent listening
    sections that fail validation and burn all retries."""
    out = template
    r = int(CFG.get("reading_count", 20))
    q = int(CFG.get("question_count", 40))
    listen_count = max(0, q - r)
    ls = r + 1
    pic = int(CFG.get("listening_picture_count") or 0)
    if pic > listen_count:
        pic = listen_count
    blank = int(CFG.get("listening_blank_count") or 0)
    if blank > listen_count:
        blank = listen_count
    if listen_count <= 0:
        section_order = (f'ALL questions are section "reading" (Q1-{q}) — '
                         f'there are NO listening questions in this exam, so NO picture/blank listening questions either')
    elif r <= 0:
        section_order = f'ALL questions are section "listening" (Q1-{q}) - no reading section'
    else:
        section_order = f'Q1-{r} section "reading", Q{ls}-{q} section "listening"'
    is_paper = int(CFG.get("gen_type", 1)) == 3
    if is_paper:
        # Printed-paper mode: the paper decides — configuration is for random generation only.
        picture_format_rule = ("PICTURE QUESTIONS: strictly follow the paper — convert EVERY listening "
                               "question that has 4 extracted photos (see PAPER PICTURE PHOTOS). Do NOT "
                               "create or drop picture questions beyond what the paper implies.")
        picture_topup_rule = ("The paper decides the picture-question count: convert EVERY question listed "
                              "under PAPER PICTURE PHOTOS (options [\"1\",\"2\",\"3\",\"4\"], correct_answer = "
                              "photo index matching your audio, option_images = the exact photo ids) — do NOT "
                              "create additional picture questions beyond the paper.")
    else:
        picture_format_rule = (f"PICTURE QUESTIONS: exactly {pic} LISTENING questions MUST be PICTURE "
                               f"questions (\"picture_options\": true, type \"listening_picture\"): FOUR SEPARATE images "
                               f"are the options - the student hears the audio and taps the photo that matches. options "
                               f'MUST be EXACTLY ["1","2","3","4"] (numbers = photo numbers), correct_answer is the photo '
                               f"index (0-3) whose scene matches the audio, requiresImage is false (no main image), and "
                               f'"option_images" holds the 4 image descriptions. The author writes ALL 4 descriptions '
                               f"(English): the photo at correct_answer matches the audio exactly; the other 3 are "
                               f"similar-but-wrong distractors (same setting, different action/object). Each description = "
                               f"a flat colourful EPS-TOPIK style scene, bold clean outlines, vivid colours, plain white "
                               f"background, NO numbers, NO labels, NO text. All 4 photos of one question share ONE "
                               f"consistent setting. The audioScript MUST clearly describe the correct photo. These "
                               f"questions carry NO other image.")
        picture_topup_rule = (f"If fewer than {pic} picture questions exist, create fresh ones to reach exactly {pic}: "
                              f"same format rules (options [\"1\",\"2\",\"3\",\"4\"], correct_answer = photo index matching "
                              f"your audio, 4 DISTINCT English option_images descriptions).")
    blank_nums = [int(x) for x in (BLANK_PLAN.get("numbers") or [])]
    if blank_nums and not is_paper:
        blank_rule = (
            f"BLANK AUDIO-ONLY QUESTIONS (numbers {', '.join(map(str, blank_nums))}) are handled by "
            f"the system with PRE-MADE content. For these numbers output ONLY a stub: number, "
            f"section \"listening\", \"blank\": true, question_text \"Q <number>. 다음을 듣고 알맞은 "
            f"것을 고르십시오.\", options [\"1\",\"2\",\"3\",\"4\"], correct_answer [\"1\"], "
            f"explanation \"듣기 문제입니다.\", requiresImage false, imagePrompt \"\". NO audioScript, "
            f"NO \"listening\" object, NO option_images. Do NOT make them picture questions.")
    else:
        blank_rule = ("BLANK AUDIO-ONLY QUESTIONS: the system adds pre-made blank questions to the "
                      "listening section - author EVERY listening question normally with a full "
                      "audioScript (scripts must be self-contained).")
    ctx = {"FORMAT_RULES": FORMAT_RULES,
           **CFG,
           "difficulty_note": DIFFICULTY_PROFILES.get(CFG.get("difficulty_profile"), ""),
           "focus_note": ("The teacher needs to test this specific area — give questions that match: " +
                          str(CFG.get("focus"))).strip() if str(CFG.get("focus") or "").strip() else "",
           "listening_start": ls,
           "section_order": section_order,
           "listening_picture_count": pic,
           "listening_blank_count": blank,
           "picture_format_rule": picture_format_rule,
           "picture_topup_rule": picture_topup_rule,
           "blank_rule": blank_rule}
    for k, v in ctx.items():
        if isinstance(v, (str, int, float)):
            out = out.replace("{" + k + "}", str(v))
    return out


CONFIG_LOADED = load_config()   # reads $MOCK_CONFIG at import time (before main)

# ---------------------------------------------------------------------------
# Pre-made blank (audio-only) listening questions.
# The blank pool lives in pipeline/blank_questions.json (ships in the Docker
# image). Every run picks BLANK_BAND random listening numbers + random pool
# entries BEFORE authoring (so the prompt can list the numbers and the author
# skips them). apply_premade_blanks() fills them in after all LLM passes.
# ---------------------------------------------------------------------------

BLANK_BAND = (5, 7)                       # fixed band: 5-7 pre-made blanks per exam
PRE_MADE_BLANKS_PATH = SRC / "blank_questions.json"
_PRE_MADE_POOL = []                        # [{man, woman:{1..4}, answer}, ...]
BLANK_PLAN = {"count": 0, "numbers": [], "entries": []}   # per-run assignment


def load_premade_blanks() -> int:
    """Load the pre-made blank pool. Priority:
      1. uploaded pool ($MOCK_BLANK_POOL - downloaded + verified by the worker)
      2. bundled pipeline/blank_questions.json
    No usable pool -> empty (legacy author-generated blanks run as fallback)."""
    global _PRE_MADE_POOL
    candidates = []
    env_path = os.environ.get("MOCK_BLANK_POOL") or ""
    if env_path:
        candidates.append(("uploaded pool", Path(env_path)))
    candidates.append(("bundled", PRE_MADE_BLANKS_PATH))
    for label, path in candidates:
        try:
            if not path.exists():
                if label == "uploaded pool":
                    print(f"[blank] uploaded pool missing ({path}) - checking bundled")
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(data, list) and data and
                    all(isinstance(x, dict) and x.get("man") and isinstance(x.get("woman"), dict)
                        and len(x.get("woman") or {}) == 4 and x.get("answer") in (1, 2, 3, 4)
                        for x in data)):
                _PRE_MADE_POOL = data
                print(f"[blank] pre-made blank pool loaded: {len(data)} questions "
                      f"({label}: {path.name})")
                return len(data)
            print(f"[blank] WARNING: {path.name} is malformed - trying next source")
        except Exception as e:
            print(f"[blank] WARNING: unreadable {path.name} ({e}) - trying next source")
    print("[blank] WARNING: no usable blank pool - falling back to author-generated blanks")
    _PRE_MADE_POOL = []
    return 0


def build_blank_plan(count, listen_available, reading_count, is_paper=False):
    """Pick the blank numbers (listening range) + pool entries for THIS exam,
    BEFORE authoring so the prompt can list the numbers. Paper mode: numbers
    are decided post-authoring (the paper decides structure), entries only."""
    global BLANK_PLAN
    count = max(0, int(count))
    if count <= 0 or not _PRE_MADE_POOL:
        BLANK_PLAN = {"count": count, "numbers": [], "entries": [], "detail": [], "applied": []}
        return BLANK_PLAN
    entries = random.sample(_PRE_MADE_POOL, min(count, len(_PRE_MADE_POOL)))
    numbers = []
    if not is_paper:
        lo = int(reading_count) + 1
        hi = lo + max(0, int(listen_available)) - 1
        if hi >= lo:
            numbers = random.sample(range(lo, hi + 1), min(count, hi - lo + 1))
    BLANK_PLAN = {"count": len(entries), "numbers": numbers, "entries": entries,
                  "detail": [], "applied": []}
    return BLANK_PLAN


def _premade_blank_ify(q, entry):
    """Fill a question with pre-made audio-only blank content.

    Two voices: a RANDOM asker (V1 male or V2 female) reads the question, the
    other voice reads the 4 options in file order (phrases only, no numbers).
    correct_answer is 0-based (["0"]..["3"]) like every pushed record."""
    num = q.get("number")
    q["blank"] = True
    q["picture_options"] = False
    q["question_text"] = "Q%s. 다음을 듣고 알맞은 것을 고르십시오." % num
    q["options"] = ["1", "2", "3", "4"]
    q["correct_answer"] = normalize_correct_answer(int(entry.get("answer")) - 1)
    q["difficulty"] = "medium"
    woman = entry.get("woman") or {}
    ans_txt = str(woman.get(str(entry.get("answer"))) or "").strip()
    q["explanation"] = f"정답은 {ans_txt}입니다." if ans_txt else "듣기 문제입니다."
    q["requiresImage"] = False
    q["imagePrompt"] = ""
    q["option_images"] = []
    asker = random.choice(["V1", "V2"])
    reader = "V2" if asker == "V1" else "V1"
    script = [{"voice": asker, "text": str(entry.get("man") or "").strip()}]
    for i in ("1", "2", "3", "4"):
        t = str(woman.get(i) or "").strip()
        if t:
            script.append({"voice": reader, "text": t})
    lis = q.get("listening")
    if not isinstance(lis, dict):
        lis = {}
    lis["audioScript"] = script
    lis["speakers"] = 2
    q["listening"] = lis
    return q


def _blank_entry_desc(e):
    """One-line description of a pool entry for the run log."""
    woman = e.get("woman") or {}
    ans = int(e.get("answer") or 1)
    ans_txt = str(woman.get(str(ans)) or "").strip()
    opts = " / ".join(str(woman.get(i) or "").strip() for i in ("1", "2", "3", "4") if str(woman.get(i) or "").strip())
    return (f"pool #{e.get('id') or '?'} ({e.get('type') or '?'}) | "
            f"question: \"{str(e.get('man') or '').strip()}\" | "
            f"options: {opts} | correct: {ans} ({ans_txt})")


def apply_premade_blanks(qs):
    """Replace listening questions with pre-made audio-only blanks from the pool.

    Numbers + entries come from BLANK_PLAN (chosen before authoring). If a
    pre-picked number is missing from the authored exam, fall back to any
    non-picture listening question. Paper mode (no pre-picked numbers): pick
    random non-picture listening questions here. Empty pool -> legacy
    author-generated blank conversion (audioScript kept)."""
    if not _PRE_MADE_POOL:
        return apply_blank_questions(qs)
    n = int(BLANK_PLAN.get("count") or 0)
    if n <= 0 or not isinstance(qs, list):
        return qs, 0
    listen = [q for q in qs if isinstance(q, dict) and q.get("section") == "listening"]
    if not listen:
        return qs, 0
    by_num = {}
    for q in listen:
        try:
            by_num[int(q.get("number"))] = q
        except (TypeError, ValueError):
            pass
    entries = [dict(e) for e in (BLANK_PLAN.get("entries") or [])]
    targets = []
    for num in (BLANK_PLAN.get("numbers") or []):
        q = by_num.get(int(num))
        if q is not None:
            targets.append(q)
        else:
            q = next((x for x in listen if not x.get("blank")
                      and not x.get("picture_options") and x not in targets), None)
            if q:
                targets.append(q)
    if not BLANK_PLAN.get("numbers"):
        pool = [x for x in listen if not x.get("blank") and not x.get("picture_options")]
        random.shuffle(pool)
        targets = pool[:n]
    done = 0
    detail = []
    for q in targets[:n]:
        entry = entries.pop(0) if entries else random.choice(_PRE_MADE_POOL)
        _premade_blank_ify(q, entry)
        num = q.get("number")
        asker = (q.get("listening") or {}).get("audioScript") or [{}]
        asker_v = asker[0].get("voice", "?")
        reader_v = asker[1].get("voice", "?") if len(asker) > 1 else "?"
        woman = entry.get("woman") or {}
        ans = int(entry.get("answer") or 1)
        ans_txt = str(woman.get(str(ans)) or "").strip()
        opts = " / ".join(str(woman.get(i) or "").strip() for i in ("1", "2", "3", "4")
                          if str(woman.get(i) or "").strip())
        print(f"[blank] Q{num} <- {_blank_entry_desc(entry)}", flush=True)
        print(f"[blank]   voices: asker={asker_v} reader={reader_v} | "
              f"correct={ans} ({ans_txt}) | options: {opts}", flush=True)
        detail.append({"number": int(num) if num is not None else None,
                       "pool_id": entry.get("id"),
                       "type": entry.get("type"),
                       "answer": ans,
                       "answer_text": ans_txt,
                       "asker": asker_v,
                       "reader": reader_v})
        done += 1
    BLANK_PLAN["detail"] = detail
    BLANK_PLAN["applied"] = [int(d["number"]) for d in detail if d["number"] is not None]
    if done:
        print(f"[blank] applied: {done}/{n} pre-made blank question(s) "
              f"at numbers {sorted(BLANK_PLAN['applied']) or '(none)'}", flush=True)
    return qs, done


load_premade_blanks()   # load the pool once at startup (empty pool = legacy fallback)


# Model choices for interactive selection (Mock.cmd asks the user)
AUTHOR_MODELS = [
    {"key": "1", "name": "gemini-2.5-flash", "slug": "google/gemini-2.5-flash",
     "price": "$0.30/$2.50 per M", "extra": {}, "default": True},
    {"key": "2", "name": "gemini-3.5-flash", "slug": "google/gemini-3.5-flash",
     "price": "$1.50/$9.00 per M (best quality)", "extra": {"reasoning_effort": "minimal"}},
    {"key": "3", "name": "deepseek-v4-flash", "slug": "deepseek/deepseek-v4-flash",
     "price": "$0.14/$0.28 per M (cheapest)", "extra": {"reasoning": {"enabled": False}}},
]
PROOF_MODELS = [
    {"key": "1", "name": "qwen/qwen3.5-flash-02-23", "slug": "qwen/qwen3.5-flash-02-23",
     "price": "$0.065/$0.26 per M (cheapest)", "extra": {"reasoning": {"enabled": False}},
     "default": True},
    {"key": "2", "name": "gemini-3.5-flash", "slug": "google/gemini-3.5-flash",
     "price": "$1.50/$9.00 per M (best quality)", "extra": {"reasoning_effort": "minimal"}},
]
IMG_MODEL_NANO = "black-forest-labs/flux.2-klein-4b"   # OpenRouter image provider (flux.2 klein)

# Provider fallback chain (switch PROVIDER, never a model of the same provider).
# Primary from config is tried first, then the other providers in order.
IMG_FALLBACK_PROVIDERS = ["z-image", "black-forest-labs/flux.2-klein-4b", "fal-ai/z-image/turbo"]


def img_chain(primary):
    chain = [primary] if primary else []
    for m in IMG_FALLBACK_PROVIDERS:
        if m not in chain:
            chain.append(m)
    return chain

def repair_cfg():
    """Repair pass model config (strong model; overridable via config)."""
    return slug_cfg(CFG["repair_model"], AUTHOR_MODELS)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def api_key():
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    env_file = SRC / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("ERROR: OPENROUTER_API_KEY not found in .env")


def gemini_api_key():
    """Google Gemini API key (direct Google API, not OpenRouter)."""
    key = os.environ.get("GEMINI_API_KEY") or ""
    return key.strip()


class GeminiQuotaError(RuntimeError):
    """429 / RESOURCE_EXHAUSTED - quota exceeded, switch to OpenRouter."""


class GeminiAuthError(RuntimeError):
    """Invalid / missing API key."""


class GeminiModelError(RuntimeError):
    """Model not found or disabled for the key."""


def gemini_status():
    """Check Gemini key validity + available models (with token limits)."""
    key = gemini_api_key()
    if not key:
        return {"ok": False, "valid": False, "models": [], "message": "GEMINI_API_KEY is not set in the container env"}
    try:
        r = httpx.get(CFG.get("gemini_api", "https://generativelanguage.googleapis.com/v1beta") + "/models",
                      params={"key": key}, timeout=30)
    except Exception as e:
        return {"ok": False, "valid": False, "models": [], "message": f"Gemini API unreachable: {str(e)[:120]}"}
    if r.status_code != 200:
        return {"ok": False, "valid": False, "models": [], "message": f"Gemini key rejected (HTTP {r.status_code})"}
    j = r.json()
    models = []
    for m in (j.get("models") or []):
        if "generateContent" in (m.get("supportedGenerationMethods") or []):
            models.append({
                "name": m.get("name", "").replace("models/", ""),
                "input_limit": m.get("inputTokenLimit"),
                "output_limit": m.get("outputTokenLimit"),
            })
    return {"ok": True, "valid": True, "models": models, "message": ""}


def gemini_author(system, user, model, vision=None):
    """Author via the DIRECT Google Gemini API. Raises GeminiQuotaError on quota,
    GeminiAuthError on bad key, GeminiModelError on bad model.

    Accepts OpenRouter-style names (google/gemini-3.5-flash) — the 'google/'
    prefix is stripped before the API call, which expects bare ids.
    vision (optional): list of PNG bytes of rendered PDF question pages. Gemini
    is multimodal, so the paper pages are fed as inline images alongside the
    prompt — the model then SEEs the source paper: it verifies image->question
    mapping, recognizes 4-photo listening questions even from a partial
    extraction, and fills missing photo descriptions coherently."""
    key = gemini_api_key()
    if not key:
        raise GeminiAuthError("GEMINI_API_KEY is not set")
    api = CFG.get("gemini_api", "https://generativelanguage.googleapis.com/v1beta")
    api_model = str(model).strip().split("/")[-1]
    parts = [{"text": user}]
    for png in (vision or [])[:20]:
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(png).decode("ascii")}})
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "maxOutputTokens": int(CFG.get("author_max_tokens", 32000)),
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }
    r = httpx.post(f"{api}/models/{api_model}:generateContent", params={"key": key},
                   json=body, timeout=int(CFG.get("llm_timeout_s", 600)))
    if r.status_code == 429 or "RESOURCE_EXHAUSTED" in r.text:
        raise GeminiQuotaError("Gemini quota exceeded (429)")
    if r.status_code == 404:
        raise GeminiModelError(f"Gemini model '{model}' not found (API id '{api_model}')")
    if r.status_code in (400, 401, 403):
        raise GeminiAuthError(f"Gemini key rejected (HTTP {r.status_code}): {r.text[:200]}")
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    cands = j.get("candidates") or []
    if not cands or not (cands[0].get("content") or {}).get("parts"):
        raise RuntimeError("Gemini returned no candidates: " + json.dumps(j, ensure_ascii=False)[:200])
    text = "".join(p.get("text", "") for p in cands[0]["content"]["parts"])
    usage = j.get("usageMetadata") or {}
    return text, usage


def next_mock_number() -> int:
    nums = []
    for p in BASE.iterdir():
        if p.is_dir() and re.fullmatch(r"mock\d+", p.name):
            nums.append(int(p.name[4:]))
    for p in BASE.glob("mock*_questions.json"):
        m = re.fullmatch(r"mock(\d+)_questions\.json", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        v = ""
    return v if v else default


def pick_model(choices, label):
    """Interactive model menu; Enter selects the marked default."""
    print(f"\n  {label}:")
    for c in choices:
        d = " (default)" if c.get("default") else ""
        print(f"    [{c['key']}] {c['name']:<24} {c['price']}{d}")
    valid = {c["key"]: c for c in choices}
    default_key = next(c["key"] for c in choices if c.get("default"))
    while True:
        k = ask(f"  Choose [{default_key}]: ", default=default_key)
        if k in valid:
            return valid[k]
        print("  Please enter " + " or ".join(f"'{c['key']}'" for c in choices))


def slug_cfg(slug, choices):
    """Resolve a model slug to a config dict (for web runs that skip the menu)."""
    for c in choices:
        if c["slug"] == slug:
            return c
    return {"key": "x", "name": slug, "slug": slug, "extra": {}, "default": False}


def chat_json(key, model, system, user, max_tokens=32000, temperature=0.7, extra=None):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra:
        payload.update(extra)
    r = httpx.post(CFG["or_api"], headers={"Authorization": f"Bearer {key}"}, json=payload,
                   timeout=int(CFG.get("llm_timeout_s", 600)))
    j = r.json()
    if "choices" not in j:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {json.dumps(j, ensure_ascii=False)[:300]}")
    content = j["choices"][0]["message"].get("content") or ""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
    return json.loads(content, strict=False), j.get("usage", {})


AUTHOR_SYSTEM = """You are a senior EPS-TOPIK UBT exam writer and psychometric expert. Output ONLY valid JSON."""

# Shared UBT mock exam format rules - IDENTICAL for every generation mode
# (random / book PDF / printed paper PDF). Both the author prompts and the repair
# pass must follow these so the exam format never drifts between modes.
FORMAT_RULES = """UBT MOCK EXAM FORMAT - IDENTICAL FOR EVERY GENERATION MODE
(standard random, book PDF, printed paper PDF all follow exactly this format):
- The exam is EXACTLY {question_count} questions: Q1-{reading_count} reading, Q{listening_start}-{question_count} listening.
- EVERY listening question's audioScript MUST be fully self-contained: the answer
  must be clear from the AUDIO ALONE, because some listening questions will be shown
  to the student as AUDIO-ONLY (no question text, options are just 1/2/3/4). Never
  write a script that references text, tables or images the student cannot see.
- CONVERSATION RULE: LONG scripts MUST be a natural TWO-SPEAKER conversation
  (V1 male + V2 female, alternating, 4-6 turns, "speakers": 2) — one speaker asks /
  raises the topic, the other replies. ONE speaker (V1 or V2 alone, "speakers": 1)
  is allowed ONLY for short announcements/monologues (2-4 sentences). Every turn
  MUST be at least ONE full Korean sentence — never a single word or a bare short
  reply (e.g. "응" or "한복" alone). The CORRECT ANSWER must be clearly stated in
  the dialogue by one of the speakers.
- The voice tags in audioScript MUST match the chosen speaker count and gender.
  V1 is always the male voice, V2 the female voice.
- {picture_format_rule}
- {blank_rule}"""

AUTHOR_USER = """Write a complete Korean EPS-TOPIK UBT mock exam: EXACTLY {question_count} questions as a JSON array.

HARD RULES:
- {section_order} (number 1..{question_count} unique, in order).
- {difficulty_note}
- {focus_note}
- Each question: number, section, difficulty, type (short English label), question_text (Korean,
  starts with "Q<N>. ", NO html), options (4 REAL Korean strings — natural, believable, similar
  length, only ONE best; options MUST be actual Korean words/phrases, NEVER numbers, digits,
  placeholders like "0 1 2 3", or empty), correct_answer (array with ONE option index string
  like ["2"]), marks {marks_per_question}, explanation (Korean, 1-2 sentences), requiresImage (bool), imagePrompt
  (English, detailed: people/objects/actions/environment/camera angle/key visual clues — ONLY
  when requiresImage true).
- NEVER reuse the exact same question_text for two questions in the exam — vary the stems
  (e.g. "그림을 보고 알맞은 것을 고르십시오." vs "그림을 보고 알맞지 않은 것을 고르십시오." vs
  "그림을 보고 무엇에 대한 그림인지 고르십시오.") — every question_text must be unique.
- EXACTLY {image_count} questions (the exam MUST stay between {image_count_min} and
  {image_count_max}, spread randomly through 1-{question_count}) MUST have requiresImage true
  plus a detailed imagePrompt (English: people/objects/actions/environment/camera angle/key clues).
- PICTURE LISTENING: exactly {listening_picture_count} LISTENING questions MUST be PICTURE
  questions ("picture_options": true, type "listening_picture"): FOUR SEPARATE photos are the
  options. options EXACTLY ["1","2","3","4"], correct_answer = the photo index (0-3) matching
  the audio, requiresImage false, "option_images" = 4 DISTINCT English descriptions: photo at
  correct_answer matches the audio, the other 3 are similar-but-wrong (same setting, different
  action/object); all 4 share ONE consistent setting, flat colourful EPS-TOPIK style, bold clean
  outlines, vivid colours, white background, NO numbers/labels/text in the images. The
  audioScript must clearly describe the correct photo (self-contained).
- Reading mixes: fill-in-blank, grammar in context, vocabulary, sentence completion, conversation
  completion, honorifics, idioms, connectors, sentence ordering, reading comprehension
  (안내문/공지/이메일/광고/편지/일기), situation judgment, sign/menu/schedule/map/notice interpretation.
- Listening (Q{listening_start}-{question_count}): each needs "listening": {"audioScript": [{"voice":"V1".."V4","text":"..."}],
  "durationSeconds", "speakers", "situation"}. SPEAKER COUNT IS RANDOMIZED: roughly half the
  listening questions are ONE speaker (a male V1 or a female V2 alone - announcement/monologue,
  2-4 sentences, all turns the SAME voice, "speakers": 1) and the rest are TWO speakers (any
  combination: V1+V2, V1+V1 or V2+V2, "speakers": 2). The voice tags in audioScript MUST match
  the speaker count and gender chosen.
  Situations rotate: phone, 방송, news, weather,
  office, hospital, shopping, transportation, restaurant, school, interview, customer service.
  Spoken-style Korean: polite endings (~주세요, ~드릴게요, ~거든요), natural contractions,
  반말 ONLY between close friends/family and never mixed with 존댓말. 2-4 speakers, turns ≤ 2 sentences.
- KOREAN QUALITY (zero mistakes): perfect particles and 띄어쓰기, natural native Korean, no
  textbook awkwardness, no English words/sentences in options or scripts, realistic Korean names
  (김민수, 이서연, 박지훈, 최은서, 정하늘, 한지우...) and places (지하철, 편의점, 동사무소, 병원,
  회사, 식당, 학교, 우체국, 백화점, 주말농장...). No politics/religion/sensitive content.
- VARIETY: never repeat grammar patterns, vocabulary roots, situations, names, or sentence
  structures across the exam.
REFERENCE EXAMPLES from a real passing exam — match this exact field set and style:

Reading (text): {"number": 3, "section": "reading", "difficulty": "hard", "type": "fill_in_the_blank", "question_text": "Q3. 빈칸에 들어갈 가장 알맞은 단어를 고르십시오.\n\n가: 오늘 퇴근하고 삼겹살에 소주 한잔 어때요?\n나: 미안해요. 이번 주는 건강 검진이 있어서 술을 (     ) 하고 있어요.", "options": ["금지", "자제", "중단", "방지"], "correct_answer": ["1"], "marks": 1, "explanation": "스스로 행동을 조절하여 참는 것을 뜻하는 단어는 '자제'가 가장 자연스럽습니다.", "requiresImage": false, "imagePrompt": ""}

Reading (image): {"number": 1, "section": "reading", "difficulty": "medium", "type": "vocabulary_image", "question_text": "Q1. 다음 그림을 보고 알맞은 단어를 고르십시오.", "options": ["굴착기", "지게차", "사다리차", "기중기"], "correct_answer": ["1"], "marks": 1, "explanation": "무거운 흙이나 자갈을 파내거나 옮길 때 사용하는 건설 기계는 굴착기입니다.", "requiresImage": true, "imagePrompt": "A yellow hydraulic excavator parked on a construction site with loose soil. Close-up, eye-level, metal tracks, cabin, bucket on the ground, natural daylight."}

Listening: {"number": 22, "section": "listening", "difficulty": "medium", "type": "listening_vocabulary_image", "question_text": "Q22. 대화를 듣고 남자가 지금 하고 있는 작업에 알맞은 그림을 고르십시오.", "options": ["사과를 상자에 담는 작업", "포도를 가위로 수확하는 작업", "밭에 비닐을 씌우는 작업", "배추에 비료를 주는 작업"], "correct_answer": ["1"], "marks": 1, "explanation": "대화에서 남자가 포도송이를 조심스럽게 가위로 잘라 바구니에 담는 수확 작업을 묘사하고 있습니다.", "requiresImage": true, "imagePrompt": "A farmer's hands with white cotton gloves using pruning shears to cut a bunch of purple grapes in an outdoor vineyard, golden hour sunlight.", "listening": {"audioScript": [{"voice": "V1", "text": "영수 씨, 지금 뭐 하고 있어요?"}, {"voice": "V2", "text": "아, 포도 수확하고 있어요. 가위로 조심스럽게 잘라야 해요."}, {"voice": "V1", "text": "힘들지 않아요? 제가 좀 도와줄까요?"}, {"voice": "V2", "text": "괜찮아요. 거의 다 했어요. 조금만 더 하면 돼요."}], "durationSeconds": 15, "speakers": 2, "situation": "포도밭에서 포도를 수확하는 작업"}}

Every question in the exam MUST follow the field set shown above (no extra/missing keys).
{FORMAT_RULES}
Return the JSON array ONLY — no markdown, no commentary."""


AUTHOR_USER_BOOK = """Write a complete Korean EPS-TOPIK UBT mock exam: EXACTLY {question_count} questions as a JSON array, ALL grounded in the uploaded BOOK PDF content below (its topics, grammar points, vocabulary and situations).

HARD RULES:
- {section_order} (number 1..{question_count} unique, in order).
- Difficulty: "medium" or "hard" ONLY, mostly "medium" — NEVER "easy" and NEVER "very hard".
- Each question: number, section, difficulty, type (short English label), question_text (Korean,
  starts with "Q<N>. ", NO html), options (4 REAL Korean strings — natural, believable, similar
  length, only ONE best; NEVER numbers/placeholders), correct_answer (["0"]..["3"]),
  marks {marks_per_question}, explanation (Korean, 1-2 sentences), requiresImage (bool), imagePrompt
  (English, detailed — ONLY when requiresImage true).
- NEVER reuse the exact same question_text for two questions — vary the stems.
- EXACTLY {image_count} questions (between {image_count_min} and {image_count_max}) MUST have
  requiresImage true plus a detailed English imagePrompt.
- PICTURE LISTENING: exactly {listening_picture_count} LISTENING questions MUST be PICTURE
  questions ("picture_options": true, type "listening_picture"): FOUR SEPARATE photos as the
  options. options EXACTLY ["1","2","3","4"], correct_answer = photo index (0-3) matching the
  audio, requiresImage false, "option_images" = 4 DISTINCT English descriptions (photo at
  correct_answer matches the audio; the other 3 similar-but-wrong, same setting, different
  action/object; all 4 share one consistent setting, flat colourful EPS-TOPIK, white background,
  no numbers/labels/text in the images). The audioScript must clearly describe the correct photo.
- Reading mixes: fill-in-blank, grammar in context, vocabulary, sentence completion, conversation
  completion, honorifics, idioms, connectors, sentence ordering, reading comprehension
  (안내문/공지/이메일/광고/편지/일기), situation judgment, sign/menu/schedule/map/notice interpretation.
- Listening (Q{listening_start}-{question_count}): each needs "listening": {"audioScript": [{"voice":"V1".."V4","text":"..."}],
  "durationSeconds", "speakers", "situation"}. SPEAKER COUNT IS RANDOMIZED: roughly half are ONE
  speaker (V1 male or V2 female alone - announcement/monologue, 2-4 sentences, same voice,
  "speakers": 1) and the rest are TWO speakers (any V1/V2 combination, "speakers": 2). Voice tags
  must match. Situations rotate: phone, 방송, news, weather,
  office, hospital, shopping, transportation, restaurant, school, interview, customer service.
  Spoken-style Korean: polite endings, natural contractions, 반말 ONLY between close friends.
  2-4 speakers, turns <= 2 sentences.
- KOREAN QUALITY (zero mistakes): perfect particles and 띄어쓰기, natural native Korean, realistic
  Korean names and places, no politics/religion/sensitive content. No English in options/scripts.
- VARIETY: never repeat grammar patterns, vocabulary roots, situations, names, or sentence
  structures across the exam.
- The book content may contain page numbers, TOC, ads or repeated headers — ignore them. Use the
  book's real material: its grammar explanations, example sentences, vocabulary and dialogues.
- Every question MUST trace back to something in the book (a grammar point, word, topic or situation).
- KOREAN ONLY (absolute): EVERYTHING the student reads — question_text, all 4 options,
  explanation and every listening audioScript turn — MUST be written in Korean Hangul.
  English is allowed ONLY in the "type" field and in "imagePrompt". English-heavy questions
  fail validation and the exam is regenerated.
- Use the exact same JSON field set as the REFERENCE EXAMPLES in AUTHOR_USER (number, section,
  difficulty, type, question_text, options, correct_answer, marks, explanation, requiresImage,
  imagePrompt, listening). {FORMAT_RULES}
Return the JSON array ONLY — no markdown, no commentary."""


AUTHOR_USER_PAPER = """Rebuild the old printed EPS-TOPIK question paper below into a fresh digital mock exam:
EXACTLY {question_count} questions as a JSON array ({section_order}).

HARD RULES:
- Follow the paper faithfully: same question types, topics, images and Korean wording wherever the
  paper is clear. Do NOT change or invent questions that are readable in the paper.
- ALWAYS 4 options + one correct_answer — every paper question must become the standard
  field set: number, section, difficulty ("medium"/"hard"/"very hard", never "easy"), type,
  question_text (Korean, starts "Q<N>. "), options (4 REAL Korean strings), correct_answer (["0"]..["3"]),
  marks {marks_per_question}, explanation (Korean), requiresImage (bool), imagePrompt (English, when
  requiresImage), listening (for Q{listening_start}+).
- Determine the correct answer with YOUR OWN judgement from the question content — ignore any
  printed answer key.
- Question missing, garbled or making no sense after OCR: DO NOT hallucinate the original. Create
  ONE new coherent question that matches the surrounding context/scenario and, where the paper's
  answer list hints at the intended answer, build the new question around that answer.
- Reading passages with 2-3 questions: inline the passage text into EVERY related question's
  question_text (each question stays standalone).
- Listening (Q{listening_start}-{question_count}): if the paper contains a readable transcript for a
  question, use it verbatim as the audioScript. If the transcript is missing/empty (typical — audio
  is never printed), write a natural TWO-SPEAKER Korean conversation (V1 male + V2 female
  alternating, 4-6 turns, "speakers": 2): one speaker asks/raises the topic, the other replies.
  ONE speaker (V1 or V2 alone) is allowed ONLY for short announcements/monologues (2-4 sentences,
  "speakers": 1). Every turn MUST be a full Korean sentence — never a single word or a bare short
  reply. The CORRECT ANSWER must be clearly stated in the dialogue (map it from correct_answer).
  Include "listening": {"audioScript": [...], "durationSeconds", "speakers", "situation"}.
- Images: the extracted paper images are listed under PAPER IMAGES with the question they belong to.
  For a question with a paper image set requiresImage true AND pdfImage "<id>" (exact id from the
  list). imagePrompt may describe the expected picture from the question/answer context. Questions
  that need a picture but have no paper image: requiresImage true + normal imagePrompt (they will be
  generated fresh). The image count follows the paper — do not add or remove image questions beyond
  what the paper implies.
- PICTURE QUESTIONS (old printed format = 4 separate photos per answer, exactly converted):
  The paper's listening questions that have FOUR extracted photos (listed under PAPER PICTURE
  PHOTOS) MUST become PICTURE questions ("picture_options": true, type "listening_picture"):
  options EXACTLY ["1","2","3","4"], requiresImage false, correct_answer = the photo index whose
  scene your audio describes, and "option_images" = the four EXACT photo ids from the PAPER
  PICTURE   PHOTOS list (order as listed). The photo captions tell you what each photo shows -
  write the audioScript to clearly describe the photo you choose (self-contained), and set
  correct_answer to that photo's index (0-3). Convert ALL questions listed under PAPER PICTURE
  PHOTOS. {picture_topup_rule}
- Four-photo answer grids in READING questions (Q1-{reading_count}): the app has no photo-option
  support for reading — convert each to a normal reading question: turn each photo into a short
  Korean text option (4 real Korean strings), requiresImage false, and do NOT use
  "picture_options" (picture questions are listening-only in this format).
- Ignore answer keys, instruction pages, scoring rules and anything that is not a question.
- KOREAN ONLY (absolute): EVERYTHING the student reads — question_text, all 4 options,
  explanation and every listening audioScript turn — MUST be written in Korean Hangul.
  NEVER translate the paper to English. English is allowed ONLY in the "type" field and
  in "imagePrompt". If a question comes out English-heavy it fails validation and the exam
  is regenerated.
- KOREAN QUALITY: fix nothing that is correct in the paper, but clean up any OCR artifacts
  (garbled characters, broken spacing) into natural Korean.
{FORMAT_RULES}
Return the JSON array ONLY — no markdown, no commentary."""


PROOF_SYSTEM = """You are a meticulous Korean-language examiner. Fix every grammar, particle, spelling and
띄어쓰기 error and any awkward/unnatural phrasing. Keep the JSON structure EXACTLY identical
(keys, numbers, options order, correct_answer, section, audioScript voices). Output ONLY the corrected JSON array."""


HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
LETTER_RE = re.compile(r"[A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ]")


def korean_issues(qs):
    """Return question numbers whose user-facing text is not predominantly Korean.

    All fields the student sees must be Hangul: question_text, options, explanation
    and listening scripts (imagePrompt + type stay English by design)."""
    issues = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        texts = [str(q.get("question_text") or "")]
        opts = q.get("options")
        if isinstance(opts, list):
            texts += [str(o) for o in opts]
        texts.append(str(q.get("explanation") or ""))
        lis = q.get("listening")
        if isinstance(lis, dict):
            script = lis.get("audioScript")
            if isinstance(script, list):
                texts += [str(t.get("text") or "") for t in script if isinstance(t, dict)]
        joined = " ".join(texts)
        letters = LETTER_RE.findall(joined)
        if not letters:
            continue
        if len(HANGUL_RE.findall(joined)) / len(letters) < 0.6:
            issues.append(q.get("number"))
    return issues


def _blank_ify(q):
    num = q.get("number")
    q["blank"] = True
    q["picture_options"] = False
    q["question_text"] = "Q%s. 다음을 듣고 알맞은 것을 고르십시오." % num
    q["options"] = ["1", "2", "3", "4"]
    q["explanation"] = "듣기 문제입니다."
    q["requiresImage"] = False
    q["imagePrompt"] = ""
    q["option_images"] = []
    return q


def _is_photo_id(v):
    return bool(re.fullmatch(r"(?:p\d+_img\d+|up_img\d+)", str(v or "").strip()))


def fix_numeric_option_questions(qs):
    """The author sometimes emits options ["1","2","3","4"] without the format
    flag. Auto-tag: listening with 4 option image descriptions -> picture
    question; listening without them -> blank (audio-only). Returns count."""
    fixed = 0
    for q in qs:
        opts = q.get("options") or []
        if not (isinstance(opts, list) and len(opts) == 4):
            continue
        if not all(re.fullmatch(r"[1-4]", str(o).strip()) for o in opts):
            continue
        if q.get("blank") or q.get("picture_options"):
            continue
        if q.get("section") != "listening":
            continue
        imgs = q.get("option_images") or []
        if isinstance(imgs, list) and len(imgs) >= 4 and all(str(x or "").strip() for x in imgs[:4]):
            q["picture_options"] = True
            q["type"] = "listening_picture"
            q["requiresImage"] = False
            q["imagePrompt"] = ""
            fixed += 1
        else:
            _blank_ify(q)
            fixed += 1
    return fixed


def paper_autofill(qs):
    """Paper-mode only: printed papers often omit the stem for photo-listening
    questions (the audio + 4 photos ARE the question). Fill the standard
    EPS-TOPIK stem so author-stage validation does not reject a faithful
    conversion of the paper."""
    n = 0
    for q in qs:
        if not isinstance(q, dict):
            continue
        if q.get("picture_options") and not str(q.get("question_text") or "").strip():
            q["question_text"] = "Q%s. 다음을 듣고 알맞은 것을 고르십시오." % q.get("number", "?")
            n += 1
    if n:
        print(f"[pdf] filled {n} default stem(s) for photo-listening questions")
    return qs


def picture_count_ok_paper(got, target):
    """Paper-mode picture gate: tolerance ±2 around the paper's count. LLMs
    cannot reliably hit an exact count; the paper's questions are still all
    converted because the prompt demands it. Random/book keep the exact gate."""
    return max(0, target - 2) <= got <= target + 2


def repair_picture_prompts(key, qs, paper_pics=None):
    """After proofread, restore picture questions whose option_images got dropped:
    listening pictures without 4 descriptions degrade to blank (audio-only);
    generated pictures get their 4 descriptions regenerated via the repair model.
    Paper pictures (photo ids) are restored from the extraction map when possible."""
    fixed = 0
    for q in qs:
        if not q.get("picture_options"):
            continue
        imgs = q.get("option_images") or []
        ok = isinstance(imgs, list) and len(imgs) >= 4 and all(str(x or "").strip() for x in imgs[:4])
        if ok:
            continue
        num = q.get("number")
        # paper mode: restore from the extracted photo map
        if paper_pics and num in paper_pics:
            q["option_images"] = list(paper_pics[num][:4])
            q["requiresImage"] = False
            q["imagePrompt"] = ""
            q["type"] = "listening_picture"
            fixed += 1
            print(f"[repair] Q{num}: paper picture photos restored", flush=True)
            continue
        # no usable descriptions at all -> audio-only blank
        if not any(str(x or "").strip() for x in imgs):
            _blank_ify(q)
            fixed += 1
            print(f"[repair] Q{num}: picture question without photos demoted to blank", flush=True)
            continue
        # regenerate the missing descriptions via the repair model
        user = (
            "다음 듣기 그림 문제를 위해 4개의 사진 설명(option_images)을 영어로 작성하세요. "
            "정답 사진(correct_answer 인덱스)은 음성이 설명하는 장면과 일치해야 하고, "
            "나머지 3개는 비슷하지만 틀린 장면(같은 장소, 다른 동작/사물)이어야 합니다. "
            "모두 동일한 flat colourful EPS-TOPIK 스타일, 흰 배경, 이미지 안 숫자/글자 없음. "
            'JSON만: {"option_images": ["photo1 desc", "photo2 desc", "photo3 desc", "photo4 desc"]}\n'
            f"문제: {q.get('question_text')}\n정답(인덱스): {q.get('correct_answer')}")
        try:
            fixed_json, _ = chat_json(key, repair_cfg()["slug"], REPAIR_SYSTEM, user,
                                      max_tokens=1200, temperature=0.3, extra=repair_cfg()["extra"])
            arr = (fixed_json or {}).get("option_images") or []
            if isinstance(arr, list) and len(arr) >= 4 and all(str(x or "").strip() for x in arr[:4]):
                q["option_images"] = [str(x).strip() for x in arr[:4]]
                q["requiresImage"] = False
                q["imagePrompt"] = ""
                q["type"] = "listening_picture"
                fixed += 1
                print(f"[repair] Q{num}: picture descriptions regenerated", flush=True)
        except Exception as e:
            print(f"[repair] picture prompts failed Q{num}: {str(e)[:100]}", flush=True)
    return fixed


def group_paper_pictures(pdf_doc, listen_start=None):
    """Group extracted paper images by their mapped question; return listening-range
    questions that have >=1 extracted photo as {qnum: [image ids in reading order]}
    (cap 4). Questions BELOW listen_start are the reading section — their single
    images are handled as pdfImage (main image), never as photo-options.

    No more '>=4 or drop': if the paper's 4-photo grid was only partially extracted
    (1-3 photos) the question is still returned here, and the caller tops it up to a
    full 4 photos by generating the missing slots."""
    from collections import defaultdict
    by_q = defaultdict(list)
    for im in pdf_doc["images"]:
        qn = im.get("nearest_question")
        if isinstance(qn, int) and qn > 0:
            if listen_start is not None and qn < listen_start:
                continue
            by_q[qn].append(im)
    out = {}
    for qn, v in by_q.items():
        if not v:
            continue
        v.sort(key=lambda i: (i.get("bbox") or [0, 0, 0, 0])[1] * 1000 + (i.get("bbox") or [0, 0, 0, 0])[0])
        out[qn] = [im["id"] for im in v[:4]]
    return out


def vision_caption(key, png_bytes):
    """One short English sentence describing a picture (OpenRouter vision model)."""
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    payload = {
        "model": "qwen/qwen2.5-vl-72b-instruct",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this picture in ONE short English sentence "
                                    "(who/what/action/location). No analysis, no commentary."},
            {"type": "image_url", "image_url": {"url": data_uri}}]}],
        "max_tokens": 120,
        "temperature": 0.1,
    }
    r = httpx.post("https://openrouter.ai/api/v1/chat/completions",
                   headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                   json=payload, timeout=120)
    r.raise_for_status()
    return ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()


def picture_count_ok(qs, target):
    return sum(1 for q in qs if isinstance(q, dict) and q.get("picture_options")) == target


def apply_blank_questions(qs):
    """Convert a random subset of listening questions to audio-only format:
    minimal stem + options ["1","2","3","4"] + blank flag (student marks the
    answer after hearing the audio). correct_answer + audioScript are kept.
    Questions already marked blank count toward the configured total."""
    n = int(CFG.get("listening_blank_count") or 0)
    if n <= 0 or not isinstance(qs, list):
        return qs, 0
    listen = [q for q in qs if isinstance(q, dict) and q.get("section") == "listening"]
    existing = sum(1 for q in listen if q.get("blank"))
    n = max(0, n - existing)
    random.shuffle(listen)
    done = 0
    converted = []
    for q in listen:
        if q.get("blank") or q.get("picture_options"):
            continue
        if done >= n:
            break
        _blank_ify(q)
        converted.append(q.get("number"))
        done += 1
    if converted:
        print(f"[blank] {len(converted)} author-generated blank(s) (pool unavailable) at "
              f"numbers {sorted(converted)} - audioScript kept", flush=True)
    return qs, existing + done


# Speaker cast patterns. V1 = the configured male voice, V2 = female voice.
# LONG scripts MUST be a two-speaker conversation (MF/FM alternating - the two
# configured voices interact); SHORT scripts may be a single speaker (M/F).
# The _gen() speed logic in the audio builder keys off V1/V2, so configured
# per-voice speeds are respected automatically for every cast.
SPEAKER_CASTS_SINGLE = ("M", "F")
SPEAKER_CASTS_PAIR = ("MF", "FM")
SCRIPT_MIN_TURNS = 4
SCRIPT_MIN_TOTAL_CHARS = 70
SCRIPT_MIN_TURN_CHARS = 6   # bare-word threshold: a turn under 6 chars is a single word


def _script_is_long(turns):
    total = sum(len(str(t.get("text", ""))) for t in turns)
    return len(turns) >= SCRIPT_MIN_TURNS or total >= SCRIPT_MIN_TOTAL_CHARS


def randomize_listening_speakers(qs):
    """Assign a speaker cast to every listening question's audioScript.

    Casts:
      M  — male speaker alone         (all turns V1, speakers 1)
      F  — female speaker alone       (all turns V2, speakers 1)
      MF — two speakers, male first   (V1,V2 alternating, speakers 2)
      FM — two speakers, female first (V2,V1 alternating, speakers 2)

    LONG scripts (>= 4 turns or >= 70 chars) are ALWAYS two-speaker (MF/FM) so
    the two configured voices interact; only short scripts may be single-speaker.
    The script TEXT is untouched — only the voice tags + speakers field are
    rewritten. Blank questions (pre-made pool) are skipped. Applies to ALL
    generators (random / book PDF / printed paper PDF)."""
    changed = 0
    for q in qs:
        if not isinstance(q, dict) or str(q.get("section", "")).strip().lower() != "listening":
            continue
        if q.get("blank"):
            continue  # pre-made blanks keep their fixed asker + option reader
        lis = q.get("listening")
        if not isinstance(lis, dict):
            continue
        script = lis.get("audioScript")
        if not isinstance(script, list):
            continue
        turns = [t for t in script if isinstance(t, dict) and str(t.get("text", "")).strip()]
        if not turns:
            continue
        if _script_is_long(turns):
            cast = random.choice(SPEAKER_CASTS_PAIR)
        else:
            cast = random.choice(SPEAKER_CASTS_SINGLE)
        if cast in ("M", "F"):
            voice = "V1" if cast == "M" else "V2"
            for t in turns:
                t["voice"] = voice
            lis["speakers"] = 1
        else:
            first = "V1" if cast == "MF" else "V2"
            second = "V2" if cast == "MF" else "V1"
            for i, t in enumerate(turns):
                t["voice"] = first if i % 2 == 0 else second
            lis["speakers"] = 2
        changed += 1
    if changed:
        print(f"[audio] {changed} listening scripts cast "
              f"(long -> two speakers MF/FM, short -> single M/F)")
    return changed


def expand_short_scripts(key, qs):
    """Rewrite listening audioScripts that are too short to be valid audio
    (single-word turns / ~1s clips) into natural two-speaker conversations
    (4-6 turns, every turn a full sentence) that clearly state the correct
    answer. Blank questions (pre-made pool) and picture questions are skipped.
    Returns the number of scripts expanded."""
    if not key:
        return 0
    model_cfg = repair_cfg()
    done = 0
    for q in qs:
        if not isinstance(q, dict) or str(q.get("section", "")).strip().lower() != "listening":
            continue
        if q.get("blank") or q.get("picture_options"):
            continue
        lis = q.get("listening")
        if not isinstance(lis, dict):
            continue
        script = lis.get("audioScript")
        if not isinstance(script, list) or not script:
            continue
        turns = [t for t in script if isinstance(t, dict) and str(t.get("text", "")).strip()]
        if not turns:
            continue
        total = sum(len(str(t.get("text", ""))) for t in turns)
        too_short = (len(turns) < SCRIPT_MIN_TURNS or total < SCRIPT_MIN_TOTAL_CHARS or
                     any(len(str(t.get("text", ""))) < SCRIPT_MIN_TURN_CHARS for t in turns))
        if not too_short:
            continue
        opts = q.get("options") or []
        ca = q.get("correct_answer") or []
        ans_idx = int(ca[0]) if ca and str(ca[0]) in ("0", "1", "2", "3") else None
        ans_text = str(opts[ans_idx]) if ans_idx is not None and ans_idx < len(opts) else ""
        user = (
            "아래 한국어 듣기 문제의 음성 스크립트가 너무 짧아(1초 이하의 오디오) 다시 작성합니다. "
            "남자(V1)와 여자(V2) 두 명의 자연스러운 대화로 4-6턴, 턴마다 한 문장 이상 "
            "(한 단어나 짧은 대답 금지), 한 명이 질문/화제를 제시하고 다른 한 명이 답하며 "
            "정답이 대화에서 분명히 드러나야 합니다. "
            'JSON만: {"audioScript": [{"voice": "V1"|"V2", "text": "..."}], '
            '"durationSeconds": N, "speakers": 2, "situation": "..."}\n'
            f"문제: {q.get('question_text')}\n선택지: {opts}\n"
            f"정답(인덱스): {ca} | 정답 내용: {ans_text}\n"
            f"현재 스크립트(너무 짧음): {json.dumps(script, ensure_ascii=False)}")
        try:
            fixed, _ = chat_json(key, model_cfg["slug"], REPAIR_SYSTEM, user,
                                 max_tokens=2500, temperature=0.5, extra=model_cfg["extra"])
            if isinstance(fixed, dict):
                new_script = fixed.get("audioScript")
                if isinstance(new_script, list) and new_script:
                    new_turns = [t for t in new_script if isinstance(t, dict) and str(t.get("text", "")).strip()]
                    new_total = sum(len(str(t.get("text", ""))) for t in new_turns)
                    if (new_total < SCRIPT_MIN_TOTAL_CHARS or len(new_turns) < SCRIPT_MIN_TURNS or
                            any(len(str(t.get("text", ""))) < SCRIPT_MIN_TURN_CHARS for t in new_turns)):
                        print(f"  script expand Q{q.get('number')}: LLM output still too short - keeping old",
                              flush=True)
                        continue
                    lis2 = dict(lis)
                    for k, v in fixed.items():
                        if k in ("audioScript", "durationSeconds", "speakers", "situation"):
                            lis2[k] = v
                    q["listening"] = lis2
                    done += 1
                    print(f"[audio] expanded Q{q.get('number')} script: "
                          f"{len(turns)} turns/{total} chars -> {len(new_turns)} turns/{new_total} chars",
                          flush=True)
        except Exception as e:
            print(f"  script expand failed Q{q.get('number')}: {str(e)[:100]}")
    if done:
        print(f"[audio] {done} short listening script(s) expanded to valid conversations", flush=True)
    return done


def normalize_exam(qs):
    """Coerce LLM quirks into the canonical schema (int answers, 보기 tags, script placement).

    Defensive: LLMs sometimes return a JSON *object* keyed by question number
    (iterating it yields string keys) or mix strings into the array — skip
    anything that is not a dict instead of crashing."""
    circle = {"①": "0", "②": "1", "③": "2", "④": "3",
              "1.": "0", "2.": "1", "3.": "2", "4.": "3",
              "a": "0", "b": "1", "c": "2", "d": "3",
              "A": "0", "B": "1", "C": "2", "D": "3"}
    dropped = 0
    clean = []
    for q in qs:
        if not isinstance(q, dict):
            dropped += 1
            continue
        clean.append(q)
        ca = q.get("correct_answer")
        def norm_one(c):
            s = str(c).strip()
            if s in circle:
                return circle[s]
            m = re.search(r"[0-3]", s)
            return m.group(0) if m else ""
        if isinstance(ca, list):
            q["correct_answer"] = [norm_one(c) for c in ca if norm_one(c)]
        elif ca is not None:
            q["correct_answer"] = [norm_one(ca)]
        if q.get("section") == "listening":
            lis = q.get("listening")
            if not isinstance(lis, dict):
                lis = {}
                q["listening"] = lis
            if not lis.get("audioScript") and q.get("audioScript"):
                lis["audioScript"] = q.pop("audioScript")
            if isinstance(lis.get("audioScript"), str):
                lis["audioScript"] = [{"voice": "V1", "text": t.strip()}
                                      for t in lis["audioScript"].splitlines() if t.strip()]
    if dropped:
        print(f"[exam] dropped {dropped} non-question entries from the LLM output")
    return clean


REPAIR_SYSTEM = "You are a Korean EPS-TOPIK exam writer. Output ONLY valid JSON."


def repair_exam(key, qs, stats=None):
    """Fix what the author missed: missing listening dialogues, invalid answers, low image count."""
    model_cfg = repair_cfg()
    stats = stats if stats is not None else {}
    stats.setdefault("scripts", 0)
    stats.setdefault("answers", 0)
    stats.setdefault("images", 0)
    for q in qs:
        script = (q.get("listening") or {}).get("audioScript") if q.get("section") == "listening" else None
        if q.get("section") == "listening" and (not script or (isinstance(script, list) and not all((t or {}).get("text", "").strip() for t in script))):
            print(f"[repair] writing listening script for Q{q['number']}...")
            opts = q.get("options") or []
            ca = q.get("correct_answer") or []
            ans_idx = int(ca[0]) if ca and str(ca[0]) in ("0", "1", "2", "3") else None
            ans_text = str(opts[ans_idx]) if ans_idx is not None and ans_idx < len(opts) else ""
            user = (
                "다음 한국어 듣기 문제를 위한 자연스러운 음성 스크립트를 작성하세요. "
                "기본 형식: 남자(V1)와 여자(V2) 두 명이 대화하는 4-6턴, 턴마다 한 문장 이상 "
                "(한 단어나 짧은 대답 금지, 예: \"한복\" 또는 \"응\" 단독 금지), 구어체, "
                "한 명이 질문/화제를 제시하고 다른 한 명이 답하며 정답이 대화에서 분명히 드러나야 합니다. "
                "짧은 안내방송(2-4문장)일 때만 화자 1명(V1 또는 V2, \"speakers\": 1)이 가능합니다. "
                "문제가 음성만 제시되는(blank) 문제라면 대화/안내만으로 정답이 분명히 드러나야 합니다. "
                'JSON만: {"audioScript": [{"voice": "V1"~"V2", "text": "..."}], '
                '"durationSeconds": 15, "speakers": 2, "situation": "..."}\n'
                f"문제: {q.get('question_text')}\n선택지: {opts}\n"
                f"정답(인덱스): {ca} | 정답 내용: {ans_text}\n"
                "대화 내용은 정답 선택지와 일치해야 합니다.")
            try:
                fixed, _ = chat_json(key, model_cfg["slug"], REPAIR_SYSTEM, user,
                                     max_tokens=2000, temperature=0.5, extra=model_cfg["extra"])
                lis = q.get("listening") or {}
                if isinstance(fixed, dict):
                    lis.update(fixed)
                q["listening"] = lis
                stats["scripts"] += 1
            except Exception as e:
                print(f"  repair failed Q{q['number']}: {str(e)[:100]}")
        ca = q.get("correct_answer") or []
        if not ca or ca[0] not in ("0", "1", "2", "3"):
            print(f"[repair] fixing answer index for Q{q['number']}...")
            user = (
                "선택지 중 정답이 되는 것을 고르고 인덱스(0~3)만 JSON으로: {\"index\": 0}\n"
                f"문제: {q.get('question_text')}\n선택지: {q.get('options')}\n"
                f"해설: {q.get('explanation', '')}")
            try:
                fixed, _ = chat_json(key, model_cfg["slug"], REPAIR_SYSTEM, user,
                                     max_tokens=500, temperature=0.2, extra=model_cfg["extra"])
                idx = str(fixed.get("index", ""))
                if idx in ("0", "1", "2", "3"):
                    q["correct_answer"] = [idx]
                    stats["answers"] += 1
            except Exception as e:
                print(f"  answer repair failed Q{q['number']}: {str(e)[:100]}")
    img = sum(1 for q in qs if q.get("requiresImage"))
    img_min, _ = image_bounds()
    if img < img_min:
        need = img_min - img
        candidates = [q for q in qs if q["section"] == "reading" and not q.get("requiresImage")]
        if candidates:
            print(f"[repair] converting {min(need, len(candidates))} reading questions to image questions...")
            sample = candidates[: need * 2]
            user = (
                f"아래 읽기 문제 중 {need}개를 '그림을 보고 알맞은 것을 고르십시오' 유형으로 변환하세요. "
                "각 변환 문제에 대해: 정답 선택지와 일치하도록 그림 장면을 상세히 묘사한 영어 imagePrompt와 "
                "새 해설(한국어)을 작성하세요. JSON 배열만: "
                '[{"number": N, "imagePrompt": "...", "explanation": "..."}]\n'
                + "\n".join(f"Q{q['number']}: {q['question_text']} | 선택지: {q['options']} | 정답: {q['correct_answer']}"
                            for q in sample))
            try:
                fixed, _ = chat_json(key, model_cfg["slug"], REPAIR_SYSTEM, user,
                                     max_tokens=4000, temperature=0.4, extra=model_cfg["extra"])
                done = 0
                for entry in fixed if isinstance(fixed, list) else []:
                    num = entry.get("number")
                    ip = str(entry.get("imagePrompt", "")).strip()
                    target = next((q for q in qs if q.get("number") == num), None)
                    if target and ip and done < need:
                        target["requiresImage"] = True
                        target["imagePrompt"] = ip
                        target["question_text"] = f"Q{num}. 그림을 보고 알맞은 것을 고르십시오."
                        if entry.get("explanation"):
                            target["explanation"] = entry["explanation"]
                        done += 1
                stats["images"] += done
                print(f"  converted {done} image questions")
            except Exception as e:
                print(f"  image repair failed: {str(e)[:120]}")
    return qs


def strip_html(text):
    """Remove stray HTML-like tags from LLM output, keeping <보기>/</보기> markers."""
    if not isinstance(text, str):
        return text
    out = re.sub(r"<(?!\/?보기>)[a-zA-Z/][^>]*>", "", text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", out).strip()


def sanitize_exam(qs):
    """Deterministic cleanup of model quirks: HTML tags + control chars in text fields."""
    if not isinstance(qs, list):
        return qs
    for q in qs:
        if not isinstance(q, dict):
            continue
        for field in ("question_text", "explanation", "imagePrompt"):
            if isinstance(q.get(field), str):
                q[field] = strip_html(q[field])
        opts = q.get("options")
        if isinstance(opts, list):
            q["options"] = [strip_html(o) if isinstance(o, str) else o for o in opts]
        listening = q.get("listening")
        if isinstance(listening, dict):
            script = listening.get("audioScript")
            if isinstance(script, list):
                for t in script:
                    if isinstance(t, dict) and isinstance(t.get("text"), str):
                        t["text"] = strip_html(t["text"])
    return qs


def image_bounds():
    """Effective image-question bounds, clamped to what the exam size can satisfy.

    Full exams (question_count == 2 * reading_count) keep the configured bounds.
    Small/sample exams (dry-runs: 2 questions, 1 reading) clamp the bounds so
    validation can never require more image questions than exist."""
    q_count = int(CFG.get("question_count", 40))
    r_count = int(CFG.get("reading_count", 20))
    lo = int(CFG.get("image_count_min", 18))
    hi = int(CFG.get("image_count_max", 26))
    if q_count >= r_count * 2 and r_count >= 10:
        return lo, hi
    if int(CFG.get("gen_type", 1)) == 3:
        return lo, hi  # paper mode: image count follows the paper - never clamp to the sample size
    hi = min(hi, r_count)
    lo = min(lo, r_count)
    return lo, hi


def validate_exam(qs, stage="final"):
    """stage='author' = structure only (repair pass fixes the rest); 'final' = everything.

    Sizes (question count, reading split, image bounds) come from the active config."""
    errs = []
    q_count = int(CFG.get("question_count", 40))
    r_count = int(CFG.get("reading_count", 20))
    img_min, img_max = image_bounds()
    if not isinstance(qs, list) or len(qs) != q_count:
        errs.append(f"need exactly {q_count} questions, got {len(qs) if isinstance(qs, list) else type(qs)}")
        return errs
    nums = [q.get("number") for q in qs]
    if sorted(nums) != list(range(1, q_count + 1)):
        errs.append(f"numbers must be 1..{q_count} unique")
    secs = [q.get("section") for q in qs]
    if secs[:r_count] != ["reading"] * r_count or secs[r_count:] != ["listening"] * (q_count - r_count):
        errs.append(f"Q1-{r_count} reading, Q{r_count + 1}-{q_count} listening required")
    img = 0
    seen_texts = {}
    for q in qs:
        n = q.get("number")
        qt = q.get("question_text") or ""
        if not qt:
            errs.append(f"Q{n}: bad question_text")
        elif re.search(r"<(?!/*보기>)[a-zA-Z/]", qt):
            errs.append(f"Q{n}: unexpected html-like tag in question_text")
        elif seen_texts.get(qt):
            errs.append(f"Q{n}: question_text duplicates Q{seen_texts[qt]}")
        else:
            seen_texts[qt] = n
        opts = q.get("options")
        is_num_only = bool(q.get("blank")) or bool(q.get("picture_options"))
        if not isinstance(opts, list) or len(opts) != 4 or any(not o for o in opts):
            errs.append(f"Q{n}: need 4 non-empty options")
        elif not is_num_only and all(re.fullmatch(r"\d{1,2}|[①②③④]", str(o).strip()) for o in opts):
            errs.append(f"Q{n}: options are placeholders (digits) - write REAL Korean options. "
                        f"If this is a listening PICTURE question add \"picture_options\": true with "
                        f"4 option_images descriptions, or an AUDIO-ONLY question add \"blank\": true")
        if q.get("picture_options"):
            if q.get("section") != "listening":
                errs.append(f"Q{n}: picture questions must be listening")
            imgs = q.get("option_images") or []
            if not (isinstance(imgs, list) and len(imgs) >= 4 and all(str(x or "").strip() for x in imgs[:4])):
                errs.append(f"Q{n}: picture question needs 4 non-empty option_images")
            if q.get("requiresImage"):
                errs.append(f"Q{n}: picture questions must not use a main image (requiresImage false)")
        if stage == "author":
            continue
        ca = q.get("correct_answer")
        if not isinstance(ca, list) or len(ca) != 1 or str(ca[0]) not in ("0", "1", "2", "3"):
            errs.append(f"Q{n}: correct_answer must be one of ['0','1','2','3']")
        if q.get("section") == "listening":
            script = (q.get("listening") or {}).get("audioScript")
            if not script:
                errs.append(f"Q{n}: listening missing audioScript")
            elif isinstance(script, list) and not all(t.get("text") for t in script):
                errs.append(f"Q{n}: empty audioScript turn")
        if q.get("requiresImage"):
            img += 1
            if not q.get("imagePrompt"):
                errs.append(f"Q{n}: requiresImage but no imagePrompt")
    if stage == "final":
        if not (img_min <= img <= img_max):
            errs.append(f"image questions must be {img_min}-{img_max} (config), got {img}")
    return errs


def llm_author(key, attempt, model_cfg, extra_user="", user_builder=None):
    user = (user_builder() if user_builder else render_prompt(AUTHOR_USER)) + extra_user
    if attempt:
        user += f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION:\n{attempt}"
    qs, usage = chat_json(key, model_cfg["slug"], AUTHOR_SYSTEM, user,
                          max_tokens=int(CFG.get("author_max_tokens", 32000)), extra=model_cfg["extra"])
    # some models wrap the array in an object — unwrap; keep only dict entries
    if isinstance(qs, dict):
        for k in ("questions", "items", "results", "data", "exam"):
            if isinstance(qs.get(k), list):
                qs = qs[k]
                break
        else:
            # e.g. {"1": {...}, "2": {...}} — take the values when all are dicts
            vals = [v for v in qs.values() if isinstance(v, dict)]
            if vals and len(vals) == len(qs):
                qs = vals
    if isinstance(qs, list):
        qs = [q for q in qs if isinstance(q, dict)]
    return sanitize_exam(qs), usage


def llm_proofread(key, qs, model_cfg):
    try:
        fixed, usage = chat_json(key, model_cfg["slug"], PROOF_SYSTEM,
                                 json.dumps(qs, ensure_ascii=False),
                                 max_tokens=int(CFG.get("proof_max_tokens", 32000)),
                                 temperature=float(CFG.get("proof_temperature", 0.3)),
                                 extra=model_cfg["extra"])
        if isinstance(fixed, dict):
            for k in ("questions", "items", "results", "data", "exam"):
                if isinstance(fixed.get(k), list):
                    fixed = fixed[k]
                    break
        if isinstance(fixed, list):
            fixed = [q for q in fixed if isinstance(q, dict)]
        return sanitize_exam(fixed), usage
    except Exception as e:
        print(f"[proofread] failed ({e}) — keeping original")
        return qs, {}


# ---------------------------------------------------------------------------
# PocketBase
# ---------------------------------------------------------------------------

def pb_headers():
    if not CFG["pb_pass"]:
        raise RuntimeError("pb_pass not configured - set it in the job config file, MOCK_CONFIG, or the MOCK_PB_PASS env var")
    r = httpx.post(CFG["pb_base"] + "/api/collections/_superusers/auth-with-password",
                   json={"identity": CFG["pb_email"], "password": CFG["pb_pass"]}, timeout=30)
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["token"]}


def fetch_last_sets():
    """Pull question texts from the LAST N mock exams in the end-user app library.

    Resolution (same source as create_exam): list exams -> parse 'Mock Test (N)'
    serials -> take the top N by serial -> collect their question texts via
    exam_questions?expand=question.

    Returns (serials, texts) or (None, None) on ANY failure (fail-soft - the run
    continues exactly as before when the library is unreachable or creds are
    missing)."""
    try:
        headers = pb_headers()
    except Exception as e:
        print(f"[dedup] skipped: push credentials unavailable ({str(e)[:80]})")
        return None, None
    n = max(1, int(CFG.get("dedup_sets", 5)))
    try:
        r = httpx.get(CFG["pb_base"] + "/api/collections/exams/records", headers=headers,
                      params={"perPage": 200, "fields": "id,title"}, timeout=10)
        r.raise_for_status()
        exams = r.json().get("items", [])
        if not exams:
            print("[dedup] no exams in the library - skipping")
            return None, None
        pairs = []
        for e in exams:
            m = re.search(r"Mock Test (\d+)", e.get("title") or "")
            if m:
                pairs.append((int(m.group(1)), e.get("id")))
        pairs.sort(key=lambda x: x[0], reverse=True)
        top = pairs[:n]
        if not top:
            print("[dedup] no serialed exams - skipping")
            return None, None
        serials = [s for s, _ in top]
        texts = []
        for _, eid in top:
            q = httpx.get(CFG["pb_base"] + "/api/collections/exam_questions/records",
                          headers=headers,
                          params={"filter": f'exam="{eid}"', "perPage": 200,
                                  "expand": "question", "fields": "expand.question.question_text"},
                          timeout=10)
            if q.status_code != 200:
                continue
            for item in q.json().get("items", []):
                ex = item.get("expand") or {}
                t = ((ex.get("question") or {}).get("question_text")) or ""
                if t:
                    texts.append(t)
        return serials, texts
    except Exception as e:
        print(f"[dedup] skipped: could not read library ({str(e)[:80]})")
        return None, None


def dedup_prompt_block(serials, texts):
    """Build the 'never repeat' block appended to the author prompt."""
    if not texts:
        return ""
    shown = [t.replace("\n", " ").strip()[:60] for t in texts]
    shown = list(dict.fromkeys(shown))[:200]
    return ("\n\nDEDUPLICATION - NEVER repeat or closely rephrase any of these questions. "
            f"They already exist in mock exams {serials}:\n" + "\n".join("- " + s for s in shown))


def dedup_repeats(qs, texts):
    """Exact-match repeats of generated questions against the library set."""
    if not texts or not qs:
        return []
    lib = set(t.replace("\n", " ").strip() for t in texts)
    return [q.get("question_text") for q in qs
            if q and q.get("question_text") and q["question_text"].replace("\n", " ").strip() in lib]


DIFF_MAP = {"medium": "medium", "hard": "hard", "very hard": "hard"}


def normalize_correct_answer(ca):
    """PocketBase expects a JSON array of one 0-based index string: ["0"]..["3"].
    Photo ① = ["0"], photo ④ = ["3"]. Never invent an answer; empty stays empty."""
    if ca is None or isinstance(ca, bool):
        return []
    if isinstance(ca, (int, float)):
        i = int(ca)
        return [str(i)] if 0 <= i <= 3 else []
    if isinstance(ca, str):
        s = ca.strip()
        if s in ("0", "1", "2", "3"):
            return [s]
        return []
    if isinstance(ca, (list, tuple)):
        if not ca:
            return []
        return normalize_correct_answer(ca[0])
    return []


def _enable_target_batch(headers):
    """Best-effort: enable the Batch API on the target PocketBase (superuser).
    PocketBase ships with batch DISABLED by default; a 403 on /api/batch means
    it was never switched on. We PATCH /api/settings once and retry."""
    try:
        r = httpx.patch(CFG["pb_base"] + "/api/settings", headers=headers,
                        json={"batch": {"enabled": True, "maxRequests": 200}}, timeout=30)
        if r.status_code in (200, 201):
            print("[push] batch API was disabled on the target — enabled it automatically")
            return True
        print(f"[push] could not enable batch API automatically (HTTP {r.status_code}) — "
              f"enable it in the target admin (Settings > Batch API)")
        return False
    except Exception as e:
        print(f"[push] batch API enable attempt failed: {str(e)[:120]}")
        return False


def _is_retryable_network_error(e):
    """True for httpx/httpcore transport failures (Cloudflare cuts long
    responses ~100s -> RemoteProtocolError / incomplete chunked read)."""
    names = {type(e).__name__}
    if names & {"RemoteProtocolError", "LocalProtocolError", "ReadTimeout", "ConnectTimeout",
                "ConnectError", "ReadError", "WriteError", "WriteTimeout", "PoolTimeout",
                "ChunkedEncodingError", "ProtocolError", "NetworkError", "TransportError",
                "DecodingError", "UnsupportedProtocol", "ConnectionNotAvailable"}:
        return True
    try:
        from httpcore import (RemoteProtocolError, ProtocolError, ConnectError,
                              ConnectTimeout, ReadTimeout, ReadError)
        if isinstance(e, (RemoteProtocolError, ProtocolError, ConnectError,
                          ConnectTimeout, ReadTimeout, ReadError)):
            return True
    except Exception:
        pass
    return False


BATCH_CHUNK = 10   # questions per batch request (stays well under proxy response caps)


def _resolve_or_create(ops, headers):
    """Idempotent recovery after a lost batch response: fetch this chunk's
    questions by question_text (subject-scoped), create only the missing ones
    individually, return ids aligned to ops. Requeues can never duplicate."""
    subject = CFG["subject_id"]
    found = {}
    texts = [str(op["body"].get("question_text") or "").strip() for op in ops]
    for start in range(0, len(texts), 5):
        sub = texts[start:start + 5]
        cond = "(" + " || ".join('question_text={}'.format(json.dumps(t)) for t in sub) + ")"
        try:
            r = httpx.get(CFG["pb_base"] + "/api/collections/questions/records",
                          headers=headers,
                          params={"filter": f'subject="{subject}" && {cond}',
                                  "perPage": 50, "fields": "id,question_text"}, timeout=60)
            if r.status_code == 200:
                for it in r.json().get("items", []):
                    found[str(it.get("question_text") or "").strip()] = it["id"]
        except Exception as e:
            print(f"[push] resolve check failed: {str(e)[:120]}", flush=True)
    for op in ops:
        t = str(op["body"].get("question_text") or "").strip()
        if t in found:
            continue
        try:
            r = httpx.post(CFG["pb_base"] + op["url"], headers=headers,
                           json=op["body"], timeout=120)
            if r.status_code in (200, 201):
                found[t] = r.json().get("id")
            else:
                print(f"[push] individual create failed HTTP {r.status_code}: {r.text[:150]}", flush=True)
        except Exception as e:
            print(f"[push] individual create error: {str(e)[:120]}", flush=True)
    ids = [found.get(str(op["body"].get("question_text") or "").strip()) for op in ops]
    if any(not x for x in ids):
        raise RuntimeError("[push] could not resolve/create some question records after retries")
    return ids


def _create_chunk(ops, headers):
    """POST one batch chunk with retry. On a lost response, resolve which
    records committed (text filter) and create only the missing ones."""
    for attempt in range(3):
        try:
            r = httpx.post(CFG["pb_base"] + "/api/batch", headers=headers,
                           json={"requests": ops}, timeout=300)
            if r.status_code == 403 and "atch" in r.text and _enable_target_batch(headers):
                time.sleep(0.5)
                r = httpx.post(CFG["pb_base"] + "/api/batch", headers=headers,
                               json={"requests": ops}, timeout=300)
            if r.status_code != 200:
                raise RuntimeError(f"batch create failed HTTP {r.status_code}: {r.text[:400]}")
            per = r.json()
            ids = []
            for i, p in enumerate(per):
                if p.get("status") != 200:
                    raise RuntimeError(f"record {i} failed: {json.dumps(p, ensure_ascii=False)[:200]}")
                ids.append(p["body"]["id"])
            for rid in ids:
                if not re.fullmatch(r"[a-z0-9]{15}", str(rid)):
                    raise RuntimeError(f"batch create returned a malformed record id {rid!r} — "
                                       f"refusing to store it as pbId")
            return ids
        except RuntimeError:
            raise
        except Exception as e:
            if _is_retryable_network_error(e) and attempt < 2:
                wait = 5 * (2 ** attempt)
                print(f"[push] batch network error ({type(e).__name__}) - "
                      f"retry {attempt + 2}/3 in {wait}s", flush=True)
                time.sleep(wait)
                continue
            print(f"[push] batch attempt {attempt + 1}/3 network error - "
                  f"resolving committed records idempotently", flush=True)
            return _resolve_or_create(ops, headers)
    raise RuntimeError("[push] chunk create exhausted retries")


def create_records(qs, headers):
    """Batch-create questions missing a pbId (resume-safe, retry-safe).

    Creates are sent in chunks of BATCH_CHUNK so each request finishes well
    under any proxy (Cloudflare ~100s) response cap; network failures are
    retried and committed-but-lost records are resolved by question_text, so
    retries/requeues never duplicate questions. Returns new ids in the same
    order as the questions that lacked a pbId.

    Picture questions MUST be posted as question_type=listening_picture +
    picture_options=true + non-empty correct_answer (photo index). Omitting those
    fields leaves defaults (single_choice / false / []) and the client never
    renders the 4 option_images as tappable choices.
    """
    todo = [q for q in qs if not q.get("pbId")]
    if not todo:
        return []
    ids = []
    for start in range(0, len(todo), BATCH_CHUNK):
        chunk = todo[start:start + BATCH_CHUNK]
        ops = []
        for q in chunk:
            is_pic = bool(q.get("picture_options"))
            ca = normalize_correct_answer(q.get("correct_answer"))
            body = {
                "section": q["section"],
                "subject": CFG["subject_id"],
                "question_text": q["question_text"],
                "question_type": "listening_picture" if is_pic else "single_choice",
                "picture_options": is_pic,
                "options": q["options"] if not is_pic else (q.get("options") or ["1", "2", "3", "4"]),
                "correct_answer": ca,
                "marks": int(q.get("marks") or CFG.get("marks_per_question", 1)),
                "negative_marks": int(CFG.get("negative_marks", 0)),
                "explanation": q.get("explanation", ""),
                "difficulty": DIFF_MAP.get(str(q.get("difficulty", "hard")).lower(), "hard"),
                "is_active": bool(CFG.get("is_active", True)),
            }
            if is_pic and not ca:
                print(f"[push] WARNING: picture question has NO correct_answer — "
                      f"Q{q.get('number') or q.get('question_text', '')[:40]} will be ungradeable")
            ops.append({"method": "POST", "url": "/api/collections/questions/records", "body": body})
        ids.extend(_create_chunk(ops, headers))
    return ids


def ensure_picture_record_flags(headers, record_id, q):
    """Repair picture flags without touching file fields.

    Important: only PATCH the scalar flags. Never re-POST/PATCH a full body that
    omits picture_options/correct_answer — that silently reverts them to defaults
    and is what kept breaking display of already-uploaded option_images.
    """
    if not q.get("picture_options"):
        return True
    ca = normalize_correct_answer(q.get("correct_answer"))
    fields = "picture_options,question_type,correct_answer,option_images,options"
    chk = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{record_id}",
                    headers=headers, params={"fields": fields}, timeout=30)
    if chk.status_code != 200:
        print(f"[push] flag-check failed for {record_id}: HTTP {chk.status_code}")
        return False
    rec = chk.json()
    need = {}
    if not rec.get("picture_options"):
        need["picture_options"] = True
    if rec.get("question_type") != "listening_picture":
        need["question_type"] = "listening_picture"
    have_ca = rec.get("correct_answer") or []
    if not have_ca and ca:
        need["correct_answer"] = ca
    opts = rec.get("options") or []
    if opts != ["1", "2", "3", "4"]:
        need["options"] = ["1", "2", "3", "4"]
    if not need:
        return True
    # JSON-only PATCH — do not include file fields
    pr = httpx.patch(CFG["pb_base"] + f"/api/collections/questions/records/{record_id}",
                     headers=headers, json=need, timeout=30)
    if pr.status_code not in (200, 201):
        print(f"[push] flag repair {record_id} HTTP {pr.status_code}: {pr.text[:200]}")
        return False
    print(f"[push] repaired picture flags on {record_id}: {list(need.keys())}")
    return True


def self_check_picture_questions(qs, headers):
    """GET each picture question and assert client-visible fields match the contract."""
    bad = []
    checked = 0
    for q in qs:
        if not q.get("picture_options"):
            continue
        rid = q.get("pbId")
        if not rid:
            bad.append((q.get("number"), "missing pbId"))
            continue
        checked += 1
        r = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                      headers=headers,
                      params={"fields": "picture_options,question_type,option_images,correct_answer,options"},
                      timeout=30)
        if r.status_code != 200:
            bad.append((q.get("number"), f"HTTP {r.status_code}"))
            continue
        rec = r.json()
        imgs = rec.get("option_images") or []
        ca = rec.get("correct_answer") or []
        errs = []
        if not rec.get("picture_options"):
            errs.append("picture_options!=true")
        if rec.get("question_type") != "listening_picture":
            errs.append(f"question_type={rec.get('question_type')!r}")
        if len(imgs) < 4:
            errs.append(f"option_images={len(imgs)}/4")
        if not ca:
            errs.append("correct_answer=[]")
        if errs:
            bad.append((q.get("number"), ", ".join(errs)))
        else:
            print(f"[push] self-check Q{q.get('number')} OK "
                  f"type=listening_picture pics={len(imgs)} answer={ca}")
    if checked == 0:
        print("[push] self-check: no picture questions")
        return True
    if bad:
        for num, why in bad:
            print(f"[push] self-check FAIL Q{num}: {why}")
        return False
    print(f"[push] self-check: {checked}/{checked} picture questions match contract")
    return True


PLAN_FREE = "anzwnnqhbeapgcs"
PLAN_BASIC = "zxyyn0moyx3w4g6"
PLAN_PLUS = "oqpobib5cq7exge"
PLAN_LEGEND = "6atib1qv0xkwddl"


def plan_ladder(serial):
    """Serial mock -> plan tier (matches the production ladder)."""
    if serial <= 2:
        return [PLAN_FREE]
    if serial <= 6:
        return [PLAN_BASIC]
    if serial <= 10:
        return [PLAN_PLUS]
    return [PLAN_LEGEND]


def create_exam(mock, qs, headers):
    """Create the `exams` record + `exam_questions` junctions (idempotent).

    Returns (exam_id, created). Title/code are SERIAL (Mock Test N, UBT-2026-0NN)
    based on existing exams, so the library always reads 1,2,3... regardless of the
    local folder number. Plans follow the tier ladder."""
    r = httpx.get(CFG["pb_base"] + "/api/collections/exams/records", headers=headers,
                  params={"perPage": 200, "fields": "id,title,code"}, timeout=30)
    existing = r.json().get("items", [])
    serials = []
    for e in existing:
        m = re.search(r"Mock Test (\d+)", e.get("title") or "")
        if m:
            serials.append(int(m.group(1)))
    serial = (max(serials) + 1) if serials else 1
    title = f"EPS-TOPIK UBT Mock Test {serial}"
    code = f"UBT-2026-{serial:03d}"
    plans = plan_ladder(serial)
    # resume-safe lookup: the folder-based id from a previous run takes priority,
    # then the serial title, then a fresh random id.
    folder_id = f"ex{mock}" + "a" * max(0, 15 - len(f"ex{mock}"))
    exam_id = next((e["id"] for e in existing if e["id"] == folder_id), None)
    if not exam_id:
        exam_id = next((e["id"] for e in existing if e.get("title") == title), None)
    created = False
    if not exam_id:
        import random, string
        used = {e["id"] for e in existing}
        while True:
            exam_id = "ex" + "".join(random.choices(string.ascii_lowercase + string.digits, k=13))
            if exam_id not in used:
                break
        r = httpx.post(CFG["pb_base"] + "/api/collections/exams/records", headers=headers, json={
            "id": exam_id, "title": title, "code": code, "exam_type": str(CFG.get("exam_type", "mock")),
            "subject": CFG["subject_id"],
            "duration_minutes": int(CFG.get("duration_minutes", 50)),
            "total_questions": len(qs),
            "total_marks": len(qs) * int(CFG.get("marks_per_question", 1)),
            "pass_marks": int(CFG.get("pass_marks", 24)),
            "shuffle_questions": bool(CFG.get("shuffle_questions", True)),
            "shuffle_options": bool(CFG.get("shuffle_options", False)),
            "status": str(CFG.get("exam_status", "draft")),
            "is_active": str(CFG.get("exam_status", "draft")) == "published",
            "plans": plans,
        }, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"exam create failed HTTP {r.status_code}: {r.text[:200]}")
        created = True
    else:
        # keep serial title/code/plan ladder up to date on resume
        httpx.patch(CFG["pb_base"] + f"/api/collections/exams/records/{exam_id}",
                    headers=headers, json={"title": title, "code": code, "plans": plans},
                    timeout=30)
    # junctions: create only missing (unique exam+question constraint)
    linked = httpx.get(CFG["pb_base"] + "/api/collections/exam_questions/records", headers=headers,
                       params={"filter": f'exam="{exam_id}"', "perPage": 200,
                               "fields": "question"}, timeout=30).json()
    have = {i["question"] for i in linked.get("items", [])}
    ops = []
    for q in qs:
        pid = q.get("pbId")
        if pid and pid not in have:
            ops.append({"method": "POST", "url": "/api/collections/exam_questions/records",
                        "body": {"exam": exam_id, "question": pid,
                                 "order": q.get("number", 0),
                                 "marks": int(CFG.get("marks_per_question", 1))}})
    if ops:
        # retry on network drops; the exam+question unique constraint makes
        # retries safe (duplicate junctions are rejected by the target)
        for attempt in range(3):
            try:
                r = httpx.post(CFG["pb_base"] + "/api/batch", headers=headers,
                               json={"requests": ops}, timeout=300)
                if r.status_code != 200:
                    raise RuntimeError(f"exam_questions batch failed HTTP {r.status_code}: {r.text[:200]}")
                break
            except RuntimeError:
                raise
            except Exception as e:
                if _is_retryable_network_error(e) and attempt < 2:
                    wait = 5 * (2 ** attempt)
                    print(f"[push] exam_questions network error ({type(e).__name__}) - "
                          f"retry {attempt + 2}/3 in {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"exam_questions batch failed: {str(e)[:200]}")
    print(f"[pb] exam {exam_id} ({'created' if created else 'reused'}) + {len(ops)} links")
    return exam_id, created


def upload_file(headers, record_id, field, filename, content, ctype):
    for attempt in range(3):
        try:
            r = httpx.patch(CFG["pb_base"] + f"/api/collections/questions/records/{record_id}",
                            headers=headers, files={field: (filename, content, ctype)}, timeout=300)
            if r.status_code in (200, 201):
                return True
            if r.status_code in (400, 404, 422) or attempt >= 2:
                print(f"[upload] {field} -> {record_id} HTTP {r.status_code}: {r.text[:200]}")
                return False
            print(f"[upload] {field} -> {record_id} HTTP {r.status_code} - retrying", flush=True)
        except Exception as e:
            if _is_retryable_network_error(e) and attempt < 2:
                wait = 3 * (2 ** attempt)
                print(f"[upload] {field} network error ({type(e).__name__}) - "
                      f"retry {attempt + 2}/3 in {wait}s", flush=True)
                time.sleep(wait)
                continue
            print(f"[upload] {field} -> {record_id} error: {str(e)[:150]}")
            return False
        time.sleep(3 * (attempt + 1))
    return False


def ensure_option_images_field(headers):
    """Self-heal: make sure the client 'questions' collection has the option_images
    multi-file field (4 photos, photo 1..4 in array order). Superuser PATCH is
    idempotent — only adds the field when missing."""
    try:
        r = httpx.get(CFG["pb_base"] + "/api/collections/questions",
                      headers=headers, timeout=30)
        if r.status_code not in (200, 201):
            print(f"[option_images] cannot read questions collection: HTTP {r.status_code}")
            return False
        col = r.json()
        for f in col.get("fields", []):
            if f.get("name") == "option_images":
                return True
        col["fields"].append({
            "id": "optimg000000000",
            "name": "option_images",
            "type": "file",
            "required": False,
            "presentable": False,
            "hidden": False,
            "system": False,
            "maxSelect": 4,
            "maxSize": 5242880,
            "mimeTypes": ["image/jpeg", "image/png", "image/webp"],
            "thumbs": None,
            "protected": False,
        })
        pr = httpx.patch(CFG["pb_base"] + "/api/collections/questions",
                         headers=headers, json=col, timeout=60)
        if pr.status_code not in (200, 201):
            print(f"[option_images] field add failed: HTTP {pr.status_code}: {pr.text[:200]}")
            return False
        print("[option_images] field added to questions collection")
        return True
    except Exception as e:
        print(f"[option_images] self-heal error: {str(e)[:150]}")
        return False


def upload_option_images(headers, record_id, files, field="option_images"):
    """Upload 4 photos in ONE multipart PATCH under the same field name
    (PB replaces the multi-file field; array order = photo 1..4).
    files: [(filename, bytes, ctype), ...]"""
    r = httpx.patch(CFG["pb_base"] + f"/api/collections/questions/records/{record_id}",
                    headers=headers,
                    files=[(field, (fn, content, ct)) for fn, content, ct in files],
                    timeout=180)
    if r.status_code not in (200, 201):
        print(f"[option_images] upload -> {record_id} HTTP {r.status_code}: {r.text[:250]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def gen_image_or(key, prompt, model="z-image"):
    """Magnific (preferred): configured model via REST; unknown models auto-degrade
    to z-image inside magnific_mcp (ModelNotFoundError path)."""
    return magnific_mcp.generate_image(prompt, model=model)


def gen_image_nano(key, prompt):
    r = httpx.post(CFG["or_img"], headers={"Authorization": f"Bearer {key}"},
                   json={"model": IMG_MODEL_NANO, "prompt": prompt, "n": 1}, timeout=300)
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"img HTTP {r.status_code}: {str(j.get('error'))[:150]}")
    first = j["data"][0]
    b64 = first.get("b64_json") or first.get("image_b64")
    return base64.b64decode(b64), j.get("usage", {})


def _fal_headers():
    key = os.environ.get("FAL_KEY") or ""
    if not key:
        raise RuntimeError("FAL_KEY is not set - add it to docker-compose / container env")
    return {"Authorization": "Key " + key, "Accept": "application/json"}


def fal_balance():
    """Current fal.ai credit balance (USD) via the Platform API."""
    r = httpx.get("https://api.fal.ai/v1/account/billing", params={"expand": "credits"},
                  headers=_fal_headers(), timeout=30)
    r.raise_for_status()
    j = r.json()
    c = (j.get("credits") or {})
    return {"balance": c.get("current_balance", 0.0), "currency": c.get("currency", "USD"),
            "username": j.get("username", "")}


def or_balance():
    """OpenRouter credit balance (USD remaining) via the credits API."""
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        return None
    r = httpx.get("https://openrouter.ai/api/v1/credits",
                  headers={"Authorization": "Bearer " + key}, timeout=30)
    r.raise_for_status()
    d = (r.json().get("data") or {})
    total = float(d.get("total_credits") or 0.0)
    used = float(d.get("total_usage") or 0.0)
    return {"total": total, "used": used, "remaining": round(max(0.0, total - used), 2)}


def fal_model_exists(slug):
    """True when the exact fal endpoint id is in the model search results."""
    q = slug.split("/")[-1].split(":")[0]
    r = httpx.get("https://api.fal.ai/v1/models", params={"q": q, "limit": 50},
                  headers=_fal_headers(), timeout=30)
    if r.status_code != 200:
        return None  # unknown - API unreachable
    for m in (r.json().get("models") or []):
        if m.get("endpoint_id") == slug:
            return True
    return False


def gen_image_fal(prompt, size=None):
    """Generate via Fal.ai (fal-ai/z-image/turbo, 1:1 square). Requires FAL_KEY env.

    Returns (image_bytes, cost_usd) - cost is not metered per-image anymore:
    the /v1/models/usage billing API is aggressively rate-limited (429 storms
    used to kill generation before it started). On-demand balance checks stay
    available via fal_balance() (account/billing)."""
    key = os.environ.get("FAL_KEY") or ""
    if not key:
        raise RuntimeError("FAL_KEY is not set - add it to docker-compose / container env")
    api = CFG.get("fal_api", "https://queue.fal.run/fal-ai/z-image/turbo")
    sz = int(size or CFG.get("fal_size", 512))
    r = httpx.post(api, headers={"Authorization": "Key " + key, "Content-Type": "application/json"},
                   json={"prompt": prompt, "image_size": {"width": sz, "height": sz}},
                   timeout=int(CFG.get("img_timeout_s", 240)))
    r.raise_for_status()
    j = r.json()
    status_url = j.get("status_url") or ""
    response_url = j.get("response_url") or ""
    if not status_url or not response_url:
        raise RuntimeError("fal queue response missing status/response url: " + json.dumps(j, ensure_ascii=False)[:200])
    deadline = time.time() + int(CFG.get("img_timeout_s", 240))
    while time.time() < deadline:
        s = httpx.get(status_url, headers={"Authorization": "Key " + key}, timeout=60)
        sj = s.json()
        st = sj.get("status") or ""
        if st == "COMPLETED":
            res = httpx.get(response_url, headers={"Authorization": "Key " + key}, timeout=60)
            imgs = (res.json().get("images")) or []
            if not imgs or not imgs[0].get("url"):
                raise RuntimeError("fal result has no images")
            data = httpx.get(imgs[0]["url"], timeout=120).content
            return data, 0.0
        if st in ("FAILED", "CANCELLED", "ERROR"):
            err = ((sj.get("error") or {}).get("message")) or sj.get("detail") or st
            raise RuntimeError("fal generation failed: " + str(err)[:200])
        time.sleep(3)
    raise RuntimeError("fal generation timed out")


def to_webp(data, max_size=1024, quality=80):
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=quality, method=6, optimize=True)
    return buf.getvalue()


def run_images(key, qs, record_ids, headers, work_dir, img_model=None, pdf_images=None,
               paper_photos=None):
    """Generate TOPIK images for requiresImage questions + 4-photo option_images
    for picture questions.

    Model chain (first working wins): primary provider (CFG img_model) ->
    then the other providers (z-image / OpenRouter flux / fal-ai) in order.
    Each step retried per CFG img_retries / img_fallback_retries, with size + server-side verify,
    then ONE backfill pass for anything still missing.
    pdf_images: {qnum: {"png": bytes}} — questions whose image came from a parsed PDF:
    upscaled via fal-ai/recraft/upscale/crisp when enabled (raw fallback), never regenerated.
    paper_photos: {qnum: {"ids": [...], "pngs": {id: bytes}}} — picture questions whose 4
    photos came from the parsed PDF (upscaled, never regenerated).
    Returns (ok, cost, credits, missing_numbers)."""
    from PIL import Image  # noqa: F401 — ensure importable early
    import pdf_parser as P
    pdf_images = pdf_images or {}
    paper_photos = paper_photos or {}
    primary = img_model or CFG.get("img_model") or "z-image"
    chain = img_chain(primary)
    img_retries = int(CFG.get("img_retries", 2))
    fb_retries = int(CFG.get("img_fallback_retries", 2))
    verify = bool(CFG.get("image_verify_after", True))
    max_size = int(CFG.get("img_max_size", 1024))
    quality = int(CFG.get("img_quality", 80))
    upscale_on = bool(CFG.get("upscale_pdf_images", True))
    fal_key = os.environ.get("FAL_KEY") or ""
    jobs = []
    pic_jobs = []
    for q, rid in zip(qs, record_ids):
        if q.get("picture_options"):
            pic_jobs.append((q["number"], rid, q))
            continue
        if not q.get("requiresImage"):
            continue
        rec = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                        headers=headers, params={"fields": "image"}, timeout=30)
        if rec.status_code == 200 and rec.json().get("image"):
            continue  # resume: image already uploaded — skip (saves credits)
        prompt = f"{CFG['image_style_prompt']}. Scene: {q['imagePrompt']}."
        jobs.append((q["number"], rid, prompt))
    if not jobs and not pic_jobs:
        print("[images] no image questions")
        return 0, 0.0, 0, []
    if primary == "z-image":
        bal = magnific_mcp.check_balance()
        if bal:
            avail = (bal.get("credits") or {}).get("available", 0)
            needed = (len(jobs) + len(pic_jobs) * 4) * magnific_mcp.ZIMAGE_COST
            print(f"[images] z-image: {len(jobs) + len(pic_jobs) * 4} images x {magnific_mcp.ZIMAGE_COST} credits "
                  f"= {needed} credits (available: {avail})")
            if avail < needed:
                sys.exit(f"ERROR: not enough Magnific credits ({avail} < {needed})")
        else:
            print(f"[images] z-image (API mode): {len(jobs) + len(pic_jobs) * 4} images x {magnific_mcp.ZIMAGE_COST} credits "
                  "(live balance check not available via API)")
    elif str(primary).startswith("fal-ai/"):
        print(f"[images] fal.ai provider: {len(jobs) + len(pic_jobs) * 4} image(s) x {CFG.get('fal_size', 512)}x{CFG.get('fal_size', 512)} "
              "(FAL_KEY from env, no Magnific balance check)")
    else:
        print(f"[images] model chain: {chain}")
    ok, cost, credits = 0, 0.0, 0
    missing = []
    out_dir = work_dir / "images" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(job):
        num, rid, prompt = job
        if num in pdf_images:
            png = pdf_images[num]["png"]
            webp_data = None
            used = 0.0
            try:
                if upscale_on and fal_key:
                    print(f"    q{num} pdf-extracted: upscaling via recraft/crisp…")
                    up_ok = upload_file(headers, rid, "image", f"q{num}_raw.png", png, "image/png")
                    if up_ok:
                        chk = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                                        headers=headers, params={"fields": "image"}, timeout=30)
                        fname = (chk.json().get("image") or "") if chk.status_code == 200 else ""
                        if fname:
                            url = CFG["pb_base"] + f"/api/files/questions/{rid}/{fname}"
                            webp_data = to_webp(P.upscale_image(url, fal_key), max_size=max_size, quality=quality)
                            print(f"    q{num} pdf-extracted: upscaled OK ({len(webp_data)} bytes webp)")
                        else:
                            raise RuntimeError("no uploaded filename for upscale")
                if webp_data is None:
                    webp_data = to_webp(png, max_size=max_size, quality=quality)
            except Exception as e:
                print(f"    q{num} pdf-extracted upscale failed ({str(e)[:100]}) — using raw extracted image")
                webp_data = to_webp(png, max_size=max_size, quality=quality)
            up = upload_file(headers, rid, "image", f"q{num}.webp", webp_data, "image/webp")
            if up:
                path = out_dir / f"q{num}.webp"
                path.write_bytes(webp_data)
                return (num, up, used, "pdf-extracted")
            return (num, False, 0, None)
        for mi, model in enumerate(chain):
            tries = img_retries if mi == 0 else fb_retries
            gave_up = False
            for attempt in range(tries):
                try:
                    if model == "nano-banana" or model == "black-forest-labs/flux.2-klein-4b":
                        data, usage = gen_image_nano(key, prompt)
                        used = usage.get("cost", 0)
                    elif str(model).startswith("fal-ai/"):
                        data, used = gen_image_fal(prompt)
                    else:
                        url = gen_image_or(key, prompt, model)
                        data = httpx.get(url, timeout=120).content
                        used = magnific_mcp.ZIMAGE_COST if model == "z-image" else 0
                    if not data or len(data) < 500:
                        raise RuntimeError(f"image too small ({len(data) if data else 0} bytes)")
                    webp = to_webp(data, max_size=max_size, quality=quality)
                    up = upload_file(headers, rid, "image", f"q{num}.webp", webp, "image/webp")
                    if up and verify:
                        chk = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                                        headers=headers, params={"fields": "image"}, timeout=30)
                        up = bool(chk.status_code == 200 and chk.json().get("image"))
                    if up:
                        path = out_dir / f"q{num}.webp"
                        path.write_bytes(webp)
                        return (num, up, used, model)
                    raise RuntimeError("upload failed or not verified on server")
                except Exception as e:
                    print(f"    q{num} {model} attempt {attempt + 1}: {str(e)[:140]}")
                    resp = getattr(e, "response", None)
                    is_429 = bool(resp is not None and getattr(resp, "status_code", 0) == 429)
                    retry_after = 0
                    if is_429:
                        ra = resp.headers.get("retry-after", "") if resp.headers else ""
                        try:
                            retry_after = max(0, min(120, int(float(ra))))
                        except Exception:
                            retry_after = 0
                    if is_429 and attempt == 0 and retry_after <= 10:
                        time.sleep(retry_after if retry_after > 0 else 2)
                        continue
                    if is_429:
                        gave_up = True
                        break
                    time.sleep(2)
            if mi < len(chain) - 1:
                why = "429 rate limit" if gave_up else "all attempts failed"
                print(f"    q{num} {model} {why} -> falling back to {chain[mi + 1]}")
        return (num, False, 0, None)

    def one_pic(num, rid, q):
        """Picture question: 4 photos -> option_images (array order = photo 1..4).
        Paper questions: upscale extracted PNGs (raw fallback), never regenerate.
        Fresh questions: 4 generations from the descriptions, one chain pass each.

        Resume path: if option_images already has 4 files, still confirm
        picture_options=true + question_type=listening_picture + non-empty
        correct_answer (flags can be stale while files remain).
        """
        chk = httpx.get(
            CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
            headers=headers,
            params={"fields": "option_images,picture_options,question_type,correct_answer"},
            timeout=30,
        )
        if chk.status_code == 200:
            rec = chk.json()
            have = rec.get("option_images") or []
            if len(have) >= 4:
                flags_ok = (
                    bool(rec.get("picture_options"))
                    and rec.get("question_type") == "listening_picture"
                    and bool(rec.get("correct_answer"))
                )
                if flags_ok:
                    print(f"    q{num} resume: option_images + flags OK — skip")
                    return True
                print(f"    q{num} resume: 4 photos present but flags stale — repairing")
                if ensure_picture_record_flags(headers, rid, q):
                    return True
                print(f"    q{num} resume: flag repair failed")
                return False
        paper_pngs = {}
        if num in paper_photos:
            paper_pngs = paper_photos[num].get("pngs") or {}

        # Build 4 option slots: slot = ("paper", id) when the author's value is a
        # valid extracted photo, otherwise ("gen", description) -> generate.
        raw = [str(x or "").strip() for x in (q.get("option_images") or [])[:4]]
        while len(raw) < 4:
            raw.append(f"photo {len(raw) + 1} matching the audio scene")
        slots = []
        paper_used = 0
        for v in raw:
            if v in paper_pngs:
                slots.append(("paper", v, None))
                paper_used += 1
            else:
                slots.append(("gen", None, v))
        files = []
        used_sum = 0.0
        for i, (kind, pid, desc) in enumerate(slots):
            if kind == "paper":
                png = paper_pngs.get(pid)
                if not png:
                    print(f"    q{num} missing extracted photo {pid}")
                    return False
                webp_data = None
                try:
                    if upscale_on and fal_key:
                        up_ok = upload_file(headers, rid, "option_images", f"{pid}.png", png, "image/png")
                        if up_ok:
                            chk2 = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                                             headers=headers, params={"fields": "option_images"}, timeout=30)
                            fnames = chk2.json().get("option_images") or [] if chk2.status_code == 200 else []
                            if fnames:
                                url = CFG["pb_base"] + f"/api/files/questions/{rid}/{fnames[-1]}"
                                webp_data = to_webp(P.upscale_image(url, fal_key), max_size=max_size, quality=quality)
                                print(f"    q{num} {pid} upscaled OK")
                except Exception as e:
                    print(f"    q{num} {pid} upscale failed ({str(e)[:100]}) — raw")
                    webp_data = None
                if webp_data is None:
                    webp_data = to_webp(png, max_size=max_size, quality=quality)
                files.append((f"q{num}_p{i + 1}.webp", webp_data, "image/webp"))
                continue
            # generate this slot (paper missing OR fully fresh question)
            d = str(desc or "").strip()
            if not d or _is_photo_id(d):
                d = f"Photo {i + 1}: the scene that matches the audio (flat colourful EPS-TOPIK)"
            prompt = f"{CFG['image_style_prompt']}. Photo {i + 1}: {d}."
            data = None
            for mi, model in enumerate(chain):
                tries = img_retries if mi == 0 else fb_retries
                for attempt in range(tries):
                    try:
                        if model == "nano-banana" or model == "black-forest-labs/flux.2-klein-4b":
                            d2, u2 = gen_image_nano(key, prompt)
                            used_sum += u2.get("cost", 0)
                        elif str(model).startswith("fal-ai/"):
                            d2, u2 = gen_image_fal(prompt)
                            used_sum += u2
                        else:
                            url = gen_image_or(key, prompt, model)
                            d2 = httpx.get(url, timeout=120).content
                            used_sum += magnific_mcp.ZIMAGE_COST if model == "z-image" else 0
                        if not d2 or len(d2) < 500:
                            raise RuntimeError(f"image too small ({len(d2) if d2 else 0} bytes)")
                        data = d2
                        break
                    except Exception as e:
                        print(f"    q{num} photo{i + 1} {model} attempt {attempt + 1}: {str(e)[:120]}")
                        time.sleep(2)
                if data:
                    break
            if not data:
                print(f"    q{num} photo{i + 1} FAILED on all models")
                return False
            webp = to_webp(data, max_size=max_size, quality=quality)
            files.append((f"q{num}_p{i + 1}.webp", webp, "image/webp"))
            (out_dir / f"q{num}_p{i + 1}.webp").write_bytes(webp)
        nonlocal_cost[0] += used_sum
        if len(files) != 4:
            print(f"    q{num} picture: assembled {len(files)}/4 option images")
            return False
        if not upload_option_images(headers, rid, files):
            return False
        if verify:
            chk3 = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                             headers=headers, params={"fields": "option_images"}, timeout=30)
            if chk3.status_code != 200 or len(chk3.json().get("option_images") or []) < 4:
                print(f"    q{num} picture verify failed")
                return False
        print(f"    q{num} {'hybrid: %d paper photo(s) + %d generated' % (paper_used, 4 - paper_used) if paper_used else '4 photos generated'}")
        return ensure_picture_record_flags(headers, rid, q)

    nonlocal_cost = [0.0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG["img_workers"]) as ex:
        futures = [ex.submit(one, j) for j in jobs]
        for num, up, used, model in (f.result() for f in futures):
            if model == "z-image":
                credits += used
            else:
                cost += used
            if up:
                ok += 1
                print(f"[images] q{num} OK ({model})")
            else:
                missing.append(num)
                print(f"[images] q{num} FAILED")

    pic_missing = []
    for num, rid, q in pic_jobs:
        try:
            if one_pic(num, rid, q):
                ok += 1
                print(f"[images] q{num} picture 4-photo OK")
            else:
                pic_missing.append(num)
                print(f"[images] q{num} picture FAILED")
        except Exception as e:
            pic_missing.append(num)
            print(f"[images] q{num} picture error: {str(e)[:140]}")
    missing += pic_missing

    if missing and len(pic_missing) != len(missing):
        print(f"[images] backfill pass for {len(missing) - len(pic_missing)} missing...")
        for num, rid, prompt in jobs:
            if num not in missing:
                continue
            if num in pic_missing:
                continue  # picture questions cannot be backfilled via the image field
            try:
                url = gen_image_or(key, prompt, "z-image")
                data = httpx.get(url, timeout=120).content
                webp = to_webp(data, max_size=max_size, quality=quality)
                if upload_file(headers, rid, "image", f"q{num}.webp", webp, "image/webp"):
                    ok += 1
                    credits += magnific_mcp.ZIMAGE_COST
                    (out_dir / f"q{num}.webp").write_bytes(webp)
                    missing.remove(num)
                    print(f"[images] backfill q{num} OK")
            except Exception as e:
                print(f"[images] backfill q{num} failed: {str(e)[:120]}")
    return ok, cost, credits, missing


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def run_audio(mock, qs, record_ids, headers):
    qfile = BASE / f"mock{mock}" / "questions" / f"mock{mock}_questions.json"
    if os.environ.get("MOCK_ROOT"):
        cmd = [sys.executable, str(SRC / "mock_audio_builder.py"), "--mock", str(mock)]
    else:
        cmd = ["uv", "run", "--with", "httpx",
               str(SRC / "mock_audio_builder.py"), "--mock", str(mock)]
    env = dict(os.environ)
    if CFG_PATH:
        env["MOCK_CONFIG"] = CFG_PATH
    env["MOCK_TMP"] = str(BASE / f"mock{mock}" / "_tmp")  # per-job tmp dir -> no cross-job cleanup races
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE, timeout=1800, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-1500:])
    map_file = BASE / f"mock{mock}" / "audio" / f"mock{mock}_audio_map.json"
    if not map_file.exists():
        print("[audio] WARNING: audio map not created — skipping uploads")
        return 0
    audio_map = json.loads(map_file.read_text(encoding="utf-8"))
    ok = 0
    for q, rid in zip(qs, record_ids):
        if q["section"] != "listening":
            continue
        name = audio_map.get(f"Q{q['number']}")
        if not name:
            continue
        path = BASE / f"mock{mock}" / "audio" / name
        if not path.exists():
            continue
        if upload_file(headers, rid, "audio", name, path.read_bytes(), "audio/mpeg"):
            ok += 1
    print(f"[audio] uploaded {ok} mp3s")
    return ok


def read_audio_stats(mock):
    """Read the audio builder's per-run stats (fallback/short-clip counters)."""
    try:
        stats_file = BASE / f"mock{mock}" / "audio" / f"mock{mock}_audio_stats.json"
        return json.loads(stats_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------

def _short(model):
    return model.split("/")[-1].split("-0")[0] if "/" in model else model


def dry_images_pass(key, qs, base_dir):
    """Dry-run images: generate for requiresImage questions, save locally, NO upload.

    Parallel (same img_workers as the real pass) — z-image/Magnific is a slow
    queue (~30-70s/image); sequential generation is what made dry runs take
    3+ minutes."""
    from PIL import Image  # noqa: F401 — same import check as the real pass
    primary = CFG.get("img_model") or "z-image"
    chain = img_chain(primary)
    out_dir = base_dir / "images" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(q["number"], q.get("imagePrompt") or "") for q in qs
            if q.get("requiresImage") and q.get("imagePrompt")]
    if not jobs:
        print("[dry][images] no image questions in this sample")
        return 0, 0.0, 0
    print(f"[dry][images] {len(jobs)} image(s) to generate locally via {chain[0]} "
          f"(workers={CFG.get('img_workers', 3)})")
    results = [None] * len(jobs)

    def one(idx):
        num, prompt = jobs[idx]
        fprompt = f"{CFG['image_style_prompt']}. Scene: {prompt}."
        for model in chain:
            try:
                if model == "nano-banana" or model == "black-forest-labs/flux.2-klein-4b":
                    data, usage = gen_image_nano(key, fprompt)
                    used_cost = usage.get("cost", 0)
                    used_credits = 0
                elif str(model).startswith("fal-ai/"):
                    data, used_cost = gen_image_fal(fprompt)
                    used_credits = 0
                else:
                    url = gen_image_or(key, fprompt, model)
                    data = httpx.get(url, timeout=120).content
                    used_cost = 0.0
                    used_credits = magnific_mcp.ZIMAGE_COST if model == "z-image" else 0
                if not data or len(data) < 500:
                    raise RuntimeError(f"image too small ({len(data) if data else 0} bytes)")
                webp = to_webp(data, max_size=int(CFG.get("img_max_size", 1024)),
                               quality=int(CFG.get("img_quality", 80)))
                (out_dir / f"q{num}.webp").write_bytes(webp)
                print(f"[dry][images] Q{num}: generated q{num}.webp via {model} (saved locally, not uploaded)")
                return True, used_cost, used_credits
            except Exception as e:
                print(f"[dry][images] Q{num} via {model} failed: {str(e)[:110]}")
        return False, 0.0, 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(CFG.get("img_workers", 3))) as ex:
        for idx, (okq, used_cost, used_credits) in enumerate(ex.map(one, range(len(jobs)))):
            results[idx] = (okq, used_cost, used_credits)
    ok = sum(1 for r in results if r[0])
    cost = sum(r[1] for r in results)
    credits = sum(r[2] for r in results)
    print(f"[dry][images] {ok}/{len(jobs)} images generated locally "
          f"({credits} Magnific credits, ${cost:.4f} OpenRouter)")
    return ok, cost, credits


def dry_audio_pass(mock, base_dir, qs):
    """Dry-run audio: generate listening mp3s via mock_audio_builder, NO upload."""
    if os.environ.get("MOCK_ROOT"):
        cmd = [sys.executable, str(SRC / "mock_audio_builder.py"), "--mock", str(mock)]
    else:
        cmd = ["uv", "run", "--with", "httpx",
               str(SRC / "mock_audio_builder.py"), "--mock", str(mock)]
    env = dict(os.environ)
    if CFG_PATH:
        env["MOCK_CONFIG"] = CFG_PATH
    env["MOCK_TMP"] = str(base_dir / "_tmp")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE, timeout=600, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-800:])
    map_file = base_dir / "audio" / f"mock{mock}_audio_map.json"
    if not map_file.exists():
        print("[dry][audio] audio map not created - no listening questions in sample?")
        return 0
    audio_map = json.loads(map_file.read_text(encoding="utf-8"))
    ok = 0
    for q in qs:
        if q.get("section") != "listening":
            continue
        name = audio_map.get(f"Q{q['number']}")
        if name and (base_dir / "audio" / name).exists():
            ok += 1
    print(f"[dry][audio] {ok} mp3(s) generated locally (not uploaded)")
    return ok


def final_summary(stats):
    W = 40

    def line(label, value):
        print(f"  {label.ljust(W)} → {value}")

    print("=" * 66)
    if stats.get("dry_run"):
        print(f"  DRY RUN ({stats.get('dry_mode', 'questions')}) — COMPLETE")
    else:
        print(f"  MOCK {stats['mock']} — COMPLETE")
    print("-" * 66)
    q_n = int(CFG.get("question_count", 40))
    if stats.get("authored"):
        via = "Gemini" if stats.get("author_via") == "gemini" else "OpenRouter"
        line(f"author {q_n} questions via {via} ({_short(stats['author_model'])})",
             f"${stats['author_cost']:.3f}, attempt {stats['author_attempt']}")
    else:
        line(f"author {q_n} questions", "resumed (existing file)")
    if stats.get("pdf_parser"):
        line(f"pdf parse ({stats['pdf_parser']})",
             f"{stats.get('pdf_pages', 0)} pages · {stats.get('pdf_images', 0)} extracted images · {stats.get('pdf_parse_s', 0)}s")
    if stats.get("picture_count"):
        line("picture listening", f"{stats['picture_count']} photo questions (4-photo options)")
    if stats.get("blank_count"):
        nums = stats.get("blank_numbers") or []
        line("blank listening",
             f"{stats['blank_count']} pre-made audio-only questions (1/2/3/4)"
             + (f" - Q{', Q'.join(map(str, sorted(nums)))}" if nums else ""))
    if stats.get("script_expanded"):
        line("scripts expanded", f"{stats['script_expanded']} short listening scripts -> conversations")
    if stats.get("audio_fallback_clips"):
        line("audio fallback clips", f"{stats['audio_fallback_clips']} clip(s) used the fallback TTS voice")
    if stats.get("dedup_checked"):
        line(f"dedup vs last {len(stats.get('dedup_sets_checked') or [])} mocks",
             f"{stats.get('dedup_repeats', 0)} repeats")
    parts = []
    if stats["repair"]["answers"]:
        parts.append(f"{stats['repair']['answers']} answers fixed")
    if stats["repair"]["scripts"]:
        parts.append(f"{stats['repair']['scripts']} scripts written")
    if stats["repair"]["images"]:
        parts.append(f"{stats['repair']['images']} images converted")
    total_fixes = sum(stats["repair"].values())
    if len(parts) == 1:
        repair_label = f"repair pass ({parts[0]})"
    elif total_fixes:
        repair_label = f"repair pass ({total_fixes} fixes)"
    else:
        repair_label = "repair pass (none needed)"
    line(repair_label, "auto")
    line(f"proofread ({_short(stats['proof_model'])})",
         f"auto, ${stats['proof_cost']:.4f}" if stats.get("proof_cost") else "auto")
    st = stats.get("stage_times") or {}
    if st:
        parts = [f"{k} {v}s" for k, v in st.items() if k != "total"]
        line("stage times", " · ".join(parts) + (f" · total {st['total']}s" if st.get("total") else ""))
    if stats.get("dry_run"):
        dm = stats.get("dry_mode", "questions")
        line("mode", f"dry-{dm} (sample)")
        line("PocketBase records", "skipped (dry-run)")
        if dm == "audio":
            line("listening mp3s", f"{stats.get('audio_ok', 0)} generated locally (no upload)")
            line("TOPIK images", "skipped (dry-run)")
        elif dm == "images":
            line("TOPIK images", f"{stats.get('img_ok', 0)} generated locally via {stats.get('img_model', '?')} "
                                  f"(no upload), {stats.get('img_credits', 0)} credits, ${stats.get('img_cost', 0):.4f}")
            line("listening mp3s", "skipped (dry-run)")
        else:
            line("listening mp3s", "skipped (dry-run)")
            line("TOPIK images", "skipped (dry-run)")
        line("questions file", str(stats["qfile"]))
        line("total LLM cost", f"${stats['llm_cost']:.4f}")
        print("=" * 66)
        print(f"  DRY RUN ({dm}) — nothing pushed. Re-run without --dry-run for the full mock.")
        print("=" * 66)
        return
    line(f"{stats['pb_count']} PocketBase records",
         "created" if stats["pb_created"] else "reused (resume)")
    line("exam + question links",
         "created" if stats.get("exam_created") else "reused (resume)")
    line(f"{stats['audio_ok']}/{stats.get('audio_total', 0)} listening mp3s", "generated, uploaded")
    im = str(stats["img_model"])
    if im == "nano-banana" or im == "black-forest-labs/flux.2-klein-4b":
        line(f"{stats['img_ok']}/{stats['img_total']} TOPIK images",
             f"OpenRouter ({im}), ${stats['img_cost']:.2f}, uploaded")
    elif im.startswith("fal-ai/"):
        line(f"{stats['img_ok']}/{stats['img_total']} TOPIK images",
             f"fal.ai ({im}), {CFG.get('fal_size', 512)}x{CFG.get('fal_size', 512)}, uploaded")
    else:
        line(f"{stats['img_ok']}/{stats['img_total']} TOPIK images",
             f"{im} (Magnific), {stats['img_credits']} credits, uploaded")
    if stats.get("img_missing"):
        line(f"IMAGES MISSING ({len(stats['img_missing'])})",
             "Q" + ", Q".join(map(str, stats["img_missing"])) + " — see run_report.json")
    line("questions file", str(stats["qfile"]))
    line("total LLM cost", f"${stats['llm_cost']:.4f}")
    print("=" * 66)


def _paper_picture_block(pdf_doc, key, paper_pics, paper_captions, listen_start=None):
    """Build the PAPER PICTURE PHOTOS prompt section: for each listening-range paper
    question with extracted photos, list the photo ids + short vision captions (so the
    author can write audio matching the photo it chooses). Populates paper_pics/captions.

    Partial grids (1-3 photos) are listed and tagged 'MISSING k' so the author knows
    slots -4 will be GENERATED to match the same setting."""
    groups = group_paper_pictures(pdf_doc, listen_start)
    img_by_id = {im["id"]: im for im in pdf_doc["images"]}
    lines = []
    cap_questions = 0
    for qn in sorted(groups):
        ids = groups[qn]
        paper_pics[qn] = ids
        caps = []
        for im_id in ids:
            im = img_by_id.get(im_id)
            cap = ""
            if im and cap_questions < 10:  # cap vision spend (4 calls per question)
                try:
                    cap = vision_caption(key, im["png"])
                    cap = " ".join(cap.split())[:160]
                except Exception as e:
                    cap = ""
                    print(f"[pdf] vision caption failed {im_id}: {str(e)[:90]}", flush=True)
            caps.append(cap)
        paper_captions[qn] = caps
        if any(caps):
            cap_questions += 1
        cap_text = " | ".join("%d) %s" % (i + 1, caps[i] if caps[i] else "(no caption)") for i in range(len(ids)))
        missing = max(0, 4 - len(ids))
        suffix = (f" — MISSING {missing} more photo(s), you must include 4 option_images "
                  f"for this question: reuse these ids and DESCRIBE the missing ones so they are generated") if missing else " (complete 4 photos)"
        lines.append("Q%s: photos %s%s — %s%s" % (qn, ids, " (extracted %d/%d)" % (len(ids), 4), cap_text, suffix))
    for qn, ids in list(paper_pics.items()):
        if qn not in groups:
            paper_pics[qn] = ids
    return "\n".join(lines) if lines else "(no paper picture questions detected)"


def _tts_rate_hint(model: str):
    """Sample rate for a TTS model from the shared table (scripts/audio_convert.py)."""
    try:
        sys.path.insert(0, str(Path.home() / ".config" / "opencode" / "scripts"))
        import audio_convert as _ac  # noqa: F401
        for key, rate in getattr(_ac, "MODEL_SAMPLE_RATES", {}).items():
            if key in (model or ""):
                return rate
    except Exception:
        pass
    return None


def preflight_checks():
    """Non-fatal pre-flight diagnostics printed once at run start.

    Catches the common env/config mistakes BEFORE they burn attempts on a
    full run (missing keys, wrong TTS sample rate, wrong image provider, etc).
    Warnings only — the critical key presence is already enforced by api_key()."""
    print("— preflight —")
    gen_type = int(CFG.get("gen_type") or 1)

    if not gemini_api_key():
        print("  [WARN] GEMINI_API_KEY missing — Gemini author will fall back to OpenRouter")

    img_primary = str(CFG.get("img_model") or "z-image")
    if img_primary == "z-image":
        print("  [WARN] image provider z-image (Magnific) — no local key check; ensure credits exist")
    elif str(img_primary).startswith("fal-ai/"):
        if os.environ.get("FAL_KEY"):
            print("  [OK] FAL_KEY present")
        else:
            print("  [WARN] FAL_KEY not set — fal image generation will fail and fall back")
    if bool(CFG.get("upscale_pdf_images", True)) and not os.environ.get("FAL_KEY"):
        print("  [WARN] upscale_pdf_images is ON but FAL_KEY is unset — paper images fall back to raw")

    tts_model = str(CFG.get("tts_model") or "")
    want = _tts_rate_hint(tts_model)
    sr = int(CFG.get("tts_rate") or CFG.get("sample_rate") or 0)
    if want and sr and sr != want:
        print(f"  [WARN] tts_model {tts_model} expects {want} Hz sample rate but config has "
              f"{sr} — audio will play too fast/slow")

    if gen_type == 3:
        parser = str(CFG.get("pdf_parser") or "auto")
        if parser == "upstage" and not os.environ.get("UPSTAGE_API_KEY"):
            print("  [WARN] pdf_parser=upstage but UPSTAGE_API_KEY unset — falls back to local/mistral")

    print("— preflight done —")


def pick_listening_bands(listen_available, is_paper):
    """Resolve the number of picture + blank listening questions for THIS exam.

    EPS-TOPIK structure: 20 listening = 5-8 picture + 5-7 blank + the rest
    normal (random TTS). Picture count is picked at random INSIDE the
    configurable band; the blank count is FIXED at BLANK_BAND (5-7) and no
    longer configurable. Counts are clamped to the real listening-questions
    count and written back to CFG so render_prompt / apply_premade_blanks /
    the picture gate all agree.

    Paper mode: the picture count is handled separately by paper_pics (the
    paper decides); only the blank band is resolved here."""
    from random import randint
    pmin = int(CFG.get("listening_picture_min") or 5)
    pmax = int(CFG.get("listening_picture_max") or 8)
    pmin = max(0, min(pmin, pmax))
    pic = int(CFG.get("listening_picture_count") or 0)
    bmin, bmax = BLANK_BAND
    if not is_paper:
        # random / book: pick picture from the band, then blank within the rest
        pic = randint(pmin, pmax)
        pic = min(pic, max(0, listen_available))
    blank = randint(bmin, bmax)
    # reserve space for the (band-max in random, or configured count in paper) pictures
    pic_eff = min(pic, pmax if not is_paper else int(CFG.get("listening_picture_max") or 8))
    blank = min(blank, max(0, listen_available - pic_eff))
    pic = int(pic)
    CFG["listening_picture_count"] = pic
    CFG["listening_blank_count"] = blank
    if not is_paper and pic + blank > listen_available:
        blank = max(0, listen_available - pic)
        CFG["listening_blank_count"] = blank
    return pic, blank


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="One-click full mock creator (author->audio->images->PB)")
    ap.add_argument("--mock", type=int, default=None, help="Force mock number (default: next)")
    ap.add_argument("--dry-run", action="store_true", help="Author + validate + save only (no PB/audio/images)")
    ap.add_argument("--config", default="", help="Config JSON file (see mock-config.example.json)")
    ap.add_argument("--img-model", choices=["z-image", "nano-banana", "black-forest-labs/flux.2-klein-4b", "fal-ai/z-image/turbo"], default=None,
                    help="Image model override (default: config img_model, normally z-image)")
    ap.add_argument("--author-slug", default="", help="Author model slug (skips the interactive menu)")
    ap.add_argument("--proof-slug", default="", help="Proofread model slug (skips the interactive menu)")
    args = ap.parse_args()
    if args.config:
        load_config(args.config)
    img_primary = args.img_model or CFG.get("img_model") or "z-image"

    key = api_key()
    preflight_checks()
    mock = args.mock or next_mock_number()
    dm = str(CFG.get("dry_mode") or "questions")
    if args.dry_run:
        print(f"=== DRY RUN ({dm}) - sample mock {mock} ===")
    else:
        print(f"=== Creating Mock {mock} ===")
    base_dir = BASE / f"mock{mock}"
    for sub in ("questions", "audio", "images", "extra"):
        (base_dir / sub).mkdir(parents=True, exist_ok=True)
    qfile = base_dir / "questions" / f"mock{mock}_questions.json"

    stats = {
        "mock": mock, "authored": True, "author_model": "?", "author_via": "openrouter",
        "dedup_enabled": bool(CFG.get("dedup_enabled", True)), "dedup_sets_checked": None, "dedup_checked": False, "dedup_repeats": 0,
        "author_attempt": 0, "author_cost": 0.0, "proof_model": "?",
        "proof_cost": 0.0, "repair": {"answers": 0, "scripts": 0, "images": 0},
        "pb_count": 0, "pb_created": False, "exam_created": False, "audio_ok": 0,
        "audio_total": 0, "img_ok": 0, "img_total": 0, "img_model": img_primary, "img_missing": [],
        "img_cost": 0.0, "img_credits": 0, "qfile": qfile, "llm_cost": 0.0,
        "pdf_parse_s": None, "pdf_parser": None, "pdf_pages": None, "pdf_images": None,
        "blank_count": 0, "picture_count": 0,
        "stage_times": {},
    }
    t_all = time.time()
    gen_type = int(CFG.get("gen_type", 1))
    pdf_doc = None
    paper_pics = {}
    paper_captions = {}
    paper_img_map = {}
    def stage_secs():
        return int(time.time() - t_all)

    if qfile.exists():
        print("[resume] questions file exists — skipping authoring/proofread")
        qs = json.loads(qfile.read_text(encoding="utf-8"))
        if validate_exam(qs):
            sys.exit("FAILED: existing questions file is invalid — delete it and re-run")
        stats["authored"] = False
        # paper mode resume: re-parse the PDF so extracted photos are available
        gen_type = int(CFG.get("gen_type", 1))
        if gen_type >= 2:
            import pdf_parser as P
            pdf_path = str(CFG.get("pdf_path") or "").strip()
            if pdf_path and os.path.exists(pdf_path):
                pdf_doc = P.parse_pdf(pdf_path, gen_type=gen_type, parser="local",
                                      upstage_key="", or_key=key, progress=lambda m: None)
                stats["pdf_parser"] = pdf_doc["parser_used"]
                stats["pdf_pages"] = len(pdf_doc["pages"])
                stats["pdf_images"] = len(pdf_doc["images"])
                # rebuild paper photo map from the questions file (photo ids in option_images)
                valid_ids = set(i["id"] for i in pdf_doc["images"])
                img_by_id = {i["id"]: i for i in pdf_doc["images"]}
                for q in qs:
                    if not q.get("picture_options"):
                        continue
                    ids = [str(x).strip() for x in (q.get("option_images") or [])
                           if _is_photo_id(x) and str(x).strip() in valid_ids]
                    if ids:
                        paper_img_map[q["number"]] = ids[:4]
            else:
                print("[resume] pdf_path missing — picture photos cannot be restored from the paper")
    else:
        # PDF generation modes: parse the uploaded document before authoring
        gen_type = int(CFG.get("gen_type", 1))
        listen_start = int(CFG.get("reading_count") or 0) + 1
        pdf_doc = None
        user_builder = None
        vision_pages = []
        # EPS-TOPIK bands first (so render_prompt sees the resolved counts)
        listen_available = max(0, int(CFG.get("question_count") or 40) - int(CFG.get("reading_count") or 20))
        pic_target, blank_target = pick_listening_bands(listen_available, int(CFG.get("gen_type") or 1) == 3)
        stats["blank_target"] = blank_target
        # pre-made blanks: pick numbers + pool entries NOW so the prompt lists them
        build_blank_plan(blank_target, listen_available, int(CFG.get("reading_count") or 0),
                         is_paper=int(CFG.get("gen_type") or 1) == 3)
        plan_nums = [int(x) for x in BLANK_PLAN["numbers"]]
        if BLANK_PLAN["count"] <= 0:
            print(f"[blank] PLAN: 0 pre-made blank question(s) this run "
                  f"(listening room after pictures is 0 in this exam shape)", flush=True)
        else:
            print(f"[blank] PLAN: {BLANK_PLAN['count']} pre-made blank question(s) for this exam "
                  f"({BLANK_BAND[0]}-{BLANK_BAND[1]} band)", flush=True)
            if plan_nums:
                print(f"[blank]   numbers: {', '.join(map(str, plan_nums))}", flush=True)
            else:
                print("[blank]   numbers: (paper mode - picked after authoring from the real "
                      "listening questions)", flush=True)
            for i, e in enumerate(BLANK_PLAN["entries"], 1):
                num = plan_nums[i - 1] if i - 1 < len(plan_nums) else None
                print(f"[blank]   {'Q' + str(num) if num else '???'} <- {_blank_entry_desc(e)}", flush=True)
        if gen_type >= 2:
            import pdf_parser as P
            pdf_path = str(CFG.get("pdf_path") or "").strip()
            if not pdf_path or not os.path.exists(pdf_path):
                sys.exit("FAILED: generation type %d requires a PDF file (pdf_path missing on the worker)" % gen_type)
            parser_mode = "local" if str(CFG.get("pdf_parser", "auto")) == "local" else "auto"
            t_pdf = time.time()
            pdf_progress = lambda m: print("[pdf %ds] %s" % (int(time.time() - t_all), m), flush=True)
            pdf_doc = P.parse_pdf(pdf_path, gen_type=gen_type, parser=parser_mode,
                                  upstage_key=os.environ.get("UPSTAGE_API_KEY", ""),
                                  or_key=key, progress=pdf_progress)
            stats["pdf_parse_s"] = int(time.time() - t_pdf)
            stats["pdf_parser"] = pdf_doc["parser_used"]
            stats["pdf_pages"] = len(pdf_doc["pages"])
            stats["pdf_images"] = len(pdf_doc["images"])
            pdf_dir = base_dir / "pdf"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            (pdf_dir / "parsed.md").write_text(pdf_doc["text"] or "", encoding="utf-8")
            for pi in pdf_doc["images"]:
                (pdf_dir / (pi["id"] + ".png")).write_bytes(pi["png"])
            if not pdf_doc["text"].strip():
                sys.exit("FAILED: PDF parsed but no question content found (only answer-key/instruction pages?)")
            if gen_type == 2:
                user_prompt = render_prompt(AUTHOR_USER_BOOK) + "\n\nBOOK CONTENT:\n" + pdf_doc["text"]
            else:
                img_lines = []
                for pi in pdf_doc["images"]:
                    qn = pi.get("nearest_question")
                    qn = qn if isinstance(qn, int) and qn > 0 else "no question matched"
                    img_lines.append("%s -> question %s (page %d)" % (pi["id"], qn, pi.get("page", 0)))
                user_prompt = (render_prompt(AUTHOR_USER_PAPER) + "\n\nPAPER CONTENT:\n" +
                               pdf_doc["text"] + "\n\nPAPER IMAGES:\n" +
                               ("\n".join(img_lines) if img_lines else "(none extracted)") +
                               "\n\nPAPER PICTURE PHOTOS:\n" +
                               _paper_picture_block(pdf_doc, key, paper_pics, paper_captions, listen_start=listen_start))
                user_builder = lambda: user_prompt
            # Gemini vision scan: render the actual question pages so the (always
            # multimodal) Gemini author can SEE the paper - verify image->question
            # mapping, spot 4-photo grids even from partial extraction, and write
            # coherent fills for missing photos. Bounded for token safety.
            if gen_type == 3 and bool(CFG.get("gemini_vision_scan", True)):
                try:
                    q_pages = [pg["page"] for pg in (pdf_doc.get("pages") or [])
                               if str(pg.get("kind") or "") == "question"]
                    for pg in q_pages[:18]:
                        vision_pages.append(P._render_page_png(pdf_path, max(0, int(pg) - 1)))
                    if vision_pages:
                        user_prompt += ("\n\nVISION NOTE: you are also shown the actual rendered question pages of"
                                        " the paper. USE THEM: (1) visually confirm which extracted image belongs to"
                                        " which question; (2) a listening question whose photo grid is only partly"
                                        " extracted (1-3 photos) is STILL exactly a 4-photo picture question - set"
                                        " picture_options true and option_images = [the extracted photo ids you use,"
                                        " plus a clear English description for each missing photo so it can be"
                                        " generated to match the same setting]; never skip such a question."
                                        " (3) reading questions that show an image in the page must set pdfImage to"
                                        " that extracted id and requiresImage true.")
                        print(f"[pdf] fed {len(vision_pages)} rendered question page(s) to the Gemini author (vision scan)")
                except Exception as e:
                    vision_pages = []
                    print(f"[pdf] vision scan unavailable: {str(e)[:90]}")
        else:
            user_prompt = render_prompt(AUTHOR_USER)

        # picture-question target: paper strictly decides (its own candidate grids);
        # random/book use the band-resolved pic_target (already in CFG).
        if gen_type == 3:
            target = len(paper_pics)
            print(f"[picture] paper mode: {target} picture question(s) from the paper "
                  f"(config band {pic_target} applies to random/book only)")
        else:
            target = pic_target
        if target > listen_available:
            target = listen_available
        stats["pic_target"] = target

        # 1. Author - DUAL PROVIDER: Gemini (direct Google API) first, OpenRouter fallback
        qs, last_err, author_via = None, None, ""
        sys_prompt = AUTHOR_SYSTEM
        dedup_serials, dedup_texts = (None, None)
        if CFG.get("dedup_enabled", True) and gen_type == 1:
            dedup_serials, dedup_texts = fetch_last_sets()
        if dedup_texts:
            dedup_block = dedup_prompt_block(dedup_serials, dedup_texts)
            user_prompt += dedup_block
        else:
            dedup_block = ""
        stats["dedup_sets_checked"] = dedup_serials
        stats["dedup_checked"] = bool(dedup_texts)
        stats["dedup_repeats"] = 0
        gmodel = str(CFG.get("gemini_model") or "gemini-3.5-flash").strip()
        gkey = gemini_api_key()
        gretries = int(CFG.get("author_retries", 3))
        if gkey:
            # Author ladder (fixed): Gemini is ALWAYS the primary author when a
            # GEMINI_API_KEY is present; any failure falls back to the OpenRouter
            # author below. The old author_provider toggle was removed.
            for attempt in range(gretries):
                print(f"[author] gemini ({gmodel}) attempt {attempt + 1}...")
                try:
                    raw, _usage = gemini_author(sys_prompt, user_prompt, gmodel, vision=vision_pages if vision_pages else None)
                    gqs = json.loads(raw.strip(), strict=False)
                    if isinstance(gqs, dict):
                        for k in ("questions", "items", "results", "data", "exam"):
                            if isinstance(gqs.get(k), list):
                                gqs = gqs[k]
                                break
                    if isinstance(gqs, list):
                        gqs = [q for q in gqs if isinstance(q, dict)]
                    gqs = sanitize_exam(gqs)
                    # author often emits 1/2/3/4 options without the format flag —
                    # auto-tag BEFORE validation so the exam can pass authoring
                    fix_numeric_option_questions(gqs)
                    if gen_type == 3:
                        paper_autofill(gqs)
                    gerrs = validate_exam(gqs, stage="author")
                    if not gerrs and dedup_texts:
                        reps = dedup_repeats(gqs, dedup_texts)
                        if reps:
                            stats["dedup_repeats"] += len(reps)
                            print(f"[dedup] gemini repeated {len(reps)} existing questions — retrying")
                            last_err = ("you repeated questions that already exist in earlier mocks: " +
                                        "; ".join(r.replace("\n", " ")[:60] for r in reps) +
                                        " — write DIFFERENT questions")
                            continue
                    if not gerrs and gen_type >= 2:
                        kerr = korean_issues(gqs)
                        if kerr:
                            stats["korean_rejects"] = stats.get("korean_rejects", 0) + len(kerr)
                            print(f"[korean] Q{kerr} written in English — retrying")
                            last_err = ("questions %s were written in ENGLISH — this is a KOREAN exam. "
                                        "ALL question_text, options, explanation and listening audioScript "
                                        "text MUST be Korean Hangul; English is allowed ONLY in type and imagePrompt" % kerr)
                            continue
                    if not gerrs:
                        got_pic = sum(1 for x in gqs if isinstance(x, dict) and x.get("picture_options"))
                        if got_pic != target:
                            if args.dry_run:
                                print(f"[picture] dry run: gemini produced {got_pic} picture questions "
                                      f"(full runs require exactly {target}) — continuing")
                            elif gen_type == 3 and picture_count_ok_paper(got_pic, target):
                                print(f"[picture] paper mode: {got_pic} picture questions "
                                      f"(paper target {target}, tolerance ±2) — accepted")
                            else:
                                stats["pic_rejects"] = stats.get("pic_rejects", 0) + 1
                                print(f"[picture] gemini produced {got_pic} picture questions, need exactly {target} — retrying")
                                last_err = ("you created %d listening PICTURE questions but EXACTLY %d are required. "
                                            "Each picture question needs: \"picture_options\": true, options [\"1\",\"2\",\"3\",\"4\"], "
                                            "correct_answer = the photo index matching your audio, requiresImage false, "
                                            "and \"option_images\": [4 descriptions or exact paper photo ids]" % (got_pic, target))
                                continue
                        qs = gqs
                        author_via = "gemini"
                        stats["author_attempt"] = attempt + 1
                        stats["author_model"] = "gemini:" + gmodel
                        print(f"[author] via GEMINI ({gmodel}) OK — attempt {attempt + 1}")
                        break
                    last_err = "; ".join(gerrs)
                    print(f"[author] gemini validation failed: {last_err}")
                except GeminiQuotaError as e:
                    print(f"[author] {e} — switching to OpenRouter author")
                    break
                except GeminiAuthError as e:
                    print(f"[author] {e} — switching to OpenRouter author")
                    break
                except GeminiModelError as e:
                    print(f"[author] {e} — switching to OpenRouter author")
                    break
                except Exception as e:
                    print(f"[author] gemini error: {str(e)[:120]} — switching to OpenRouter author")
                    break
        if author_via != "gemini":
            author_cfg = slug_cfg(args.author_slug, AUTHOR_MODELS) if args.author_slug \
                else (slug_cfg(CFG["author_model"], AUTHOR_MODELS)
                      if (CONFIG_LOADED or CFG["author_model"] != DEFAULTS["author_model"])
                      else pick_model(AUTHOR_MODELS, "Questions generation model"))
            stats["author_model"] = author_cfg["name"]
            for attempt in range(gretries):
                print(f"[author] openrouter {author_cfg['name']} attempt {attempt + 1}...")
                try:
                    qs, usage = llm_author(key, last_err, author_cfg, dedup_block, user_builder=user_builder)
                except Exception as e:
                    print(f"[author] generation error: {str(e)[:120]}")
                    last_err = "previous generation failed or returned broken JSON — return complete valid JSON"
                    continue
                qs = normalize_exam(qs)
                # author often emits 1/2/3/4 options without the format flag —
                # auto-tag BEFORE validation so the exam can pass authoring
                fix_numeric_option_questions(qs)
                if gen_type == 3:
                    paper_autofill(qs)
                errs = validate_exam(qs, stage="author")
                if not errs and dedup_texts:
                    reps = dedup_repeats(qs, dedup_texts)
                    if reps:
                        stats["dedup_repeats"] += len(reps)
                        print(f"[dedup] openrouter repeated {len(reps)} existing questions — retrying")
                        last_err = ("you repeated questions that already exist in earlier mocks: " +
                                    "; ".join(r.replace("\n", " ")[:60] for r in reps) +
                                    " — write DIFFERENT questions")
                        continue
                if not errs and gen_type >= 2:
                    kerr = korean_issues(qs)
                    if kerr:
                        stats["korean_rejects"] = stats.get("korean_rejects", 0) + len(kerr)
                        print(f"[korean] Q{kerr} written in English — retrying")
                        last_err = ("questions %s were written in ENGLISH — this is a KOREAN exam. "
                                    "ALL question_text, options, explanation and listening audioScript "
                                    "text MUST be Korean Hangul; English is allowed ONLY in type and imagePrompt" % kerr)
                        continue
                if not errs:
                    got_pic = sum(1 for x in qs if isinstance(x, dict) and x.get("picture_options"))
                    if got_pic != target:
                        if args.dry_run:
                            print(f"[picture] dry run: author produced {got_pic} picture questions "
                                  f"(full runs require exactly {target}) — continuing")
                        elif gen_type == 3 and picture_count_ok_paper(got_pic, target):
                            print(f"[picture] paper mode: {got_pic} picture questions "
                                  f"(paper target {target}, tolerance ±2) — accepted")
                        else:
                            stats["pic_rejects"] = stats.get("pic_rejects", 0) + 1
                            print(f"[picture] author produced {got_pic} picture questions, need exactly {target} — retrying")
                            last_err = ("you created %d listening PICTURE questions but EXACTLY %d are required. "
                                        "Each picture question needs: \"picture_options\": true, options [\"1\",\"2\",\"3\",\"4\"], "
                                        "correct_answer = the photo index matching your audio, requiresImage false, "
                                        "and \"option_images\": [4 descriptions or exact paper photo ids]" % (got_pic, target))
                            continue
                    stats["author_attempt"] = attempt + 1
                    stats["author_cost"] = usage.get("cost", 0.0)
                    author_via = "openrouter"
                    print(f"[author] via OPENROUTER ({author_cfg['name']}) OK — cost ${stats['author_cost']:.4f}")
                    break
                last_err = "; ".join(errs)
                print(f"[author] validation failed: {last_err}")
        stats["author_via"] = author_via or "openrouter"
        if not qs or validate_exam(qs, stage="author"):
            errs = validate_exam(qs, stage="author") if qs else ["no questions returned"]
            sys.exit("FAILED: could not author a valid exam after attempts — " + "; ".join(errs[:12]))
        got_pic = sum(1 for x in qs if isinstance(x, dict) and x.get("picture_options"))
        if got_pic != target and not args.dry_run:
            if gen_type == 3 and got_pic >= max(0, target - 2):
                print(f"[picture] paper mode: {got_pic} picture questions accepted (paper target {target})")
            else:
                sys.exit(f"FAILED: could not author a valid exam after attempts — author produced "
                         f"{got_pic} picture questions but exactly {target} are required "
                         f"(listening_picture_count)")

        # paper mode: resolve pdfImage refs against the extracted images
        if gen_type == 3 and pdf_doc:
            valid_ids = set(i["id"] for i in pdf_doc["images"])
            for q in qs:
                pi = str(q.get("pdfImage") or "")
                if pi:
                    if pi in valid_ids:
                        q["requiresImage"] = True
                    else:
                        print(f"[pdf] Q{q.get('number')}: pdfImage '{pi}' not in extracted images — will generate fresh")
                        q["pdfImage"] = ""

        # paper mode: resolve picture questions' photo ids -> extracted images.
        # HYBRID: keep every valid extracted photo AND the author's written
        # descriptions for any missing slot (the image pass generates those).
        # A paper 4-photo grid that extracted only 1-3 photos is topped up, never skipped.
        if gen_type == 3 and pdf_doc:
            valid_ids = set(i["id"] for i in pdf_doc["images"])
            for q in qs:
                if q.get("picture_options"):
                    imgs = [str(x or "").strip() for x in (q.get("option_images") or [])][:4]
                    resolved = [x for x in imgs if _is_photo_id(x) and x in valid_ids]
                    if len(imgs) < 4:
                        imgs = imgs + ["photo%d matching the audio scene" % i for i in range(len(imgs) + 1, 5)]
                    # keep the author's slot order: paper ids stay, descriptions stay for generation
                    q["option_images"] = imgs[:4]
                    if resolved:
                        paper_img_map[q["number"]] = resolved[:4]
                    continue
                pi = str(q.get("pdfImage") or "")
                if pi:
                    if pi in valid_ids:
                        q["requiresImage"] = True
                    else:
                        print(f"[pdf] Q{q.get('number')}: pdfImage '{pi}' not in extracted images — will generate fresh")
                        q["pdfImage"] = ""

        # author sometimes emits 1/2/3/4 options without the format flag - auto-tag
        nfix = fix_numeric_option_questions(qs)
        if nfix:
            print(f"[format] auto-tagged {nfix} questions with numeric options (picture/blank)")

        # picture stats
        stats["picture_count"] = sum(1 for q in qs if q.get("picture_options"))
        print(f"[picture] {stats['picture_count']} picture questions (target {target})")

        stats["stage_times"]["author"] = stage_secs()

        # 1b. Choose proofreading model (config-driven when --config/$MOCK_CONFIG, else interactive)
        proof_cfg = slug_cfg(args.proof_slug, PROOF_MODELS) if args.proof_slug \
            else (slug_cfg(CFG["proof_model"], PROOF_MODELS)
                  if (CONFIG_LOADED or CFG["proof_model"] != DEFAULTS["proof_model"])
                  else pick_model(PROOF_MODELS, "Proofreading model"))
        stats["proof_model"] = proof_cfg["name"]

        # 2. Pre-made blanks applied BEFORE the LLM passes: the pool content is final
        # and valid (options 1-4, 0-based answer, 5-turn script), so the repair/
        # proofread validation runs on the REAL exam shape (strict, like before).
        # The blank questions themselves are still excluded from the LLM passes.
        qs, nb = apply_premade_blanks(qs)
        stats["blank_count"] = nb
        stats["blank_numbers"] = [int(x) for x in BLANK_PLAN.get("applied") or []]
        stats["blank_detail"] = BLANK_PLAN.get("detail") or []
        if nb:
            print(f"[blank] {nb} pre-made blank question(s) integrated at numbers "
                  f"{sorted(stats['blank_numbers']) or '(none)'} - details above", flush=True)
        blanks_out = [q for q in qs if q.get("blank")]
        rest = [q for q in qs if not q.get("blank")]
        print(f"[repair] fixing missing dialogues/answers... "
              f"({len(rest)} questions, {len(blanks_out)} pre-made blanks excluded)")
        rest = normalize_exam(repair_exam(key, rest, stats["repair"]))
        repaired = rest if not validate_exam(rest + blanks_out) else None
        if repaired is None:
            print("[repair] second pass...")
            rest = normalize_exam(repair_exam(key, rest, stats["repair"]))
            if not validate_exam(rest + blanks_out):
                repaired = rest
        print(f"[proofread] {proof_cfg['name']} checking Korean quality...")
        rest, pu = llm_proofread(key, rest, proof_cfg)
        stats["proof_cost"] = pu.get("cost", 0.0)
        rest = normalize_exam(rest)
        if validate_exam(rest + blanks_out) and repaired is not None:
            print("[proofread] broke structure — reverting to repaired version")
            rest = repaired
        qs = rest + blanks_out
        # proofread can drop option_images/requiresImage from picture questions - restore
        gfix = repair_picture_prompts(key, qs, paper_pics)
        if gfix:
            print(f"[repair] {gfix} picture questions restored after proofread")
        # expand scripts that would produce 1s/word-only audio into 2-speaker
        # conversations that state the correct answer (paper questions have no TTS)
        n_exp = expand_short_scripts(key, qs)
        stats["script_expanded"] = n_exp
        # enforce the speaker cast rule: long scripts -> two speakers (MF/FM),
        # short scripts -> single speaker (M/F); pre-made blanks are skipped
        randomize_listening_speakers(qs)
        qs.sort(key=lambda q: q["number"])
        errs = validate_exam(qs)
        if errs:
            sys.exit("FAILED: exam still invalid after repair+proofread — " + "; ".join(errs[:12]))
        qfile.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[save] {qfile}")
        stats["audio_total"] = sum(1 for q in qs if q.get("section") == "listening")
        stats["stage_times"]["repair"] = stage_secs()

    if args.dry_run:
        stats["dry_run"] = True
        stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]
        dm = str(CFG.get("dry_mode") or "questions")
        stats["dry_mode"] = dm
        if dm == "images":
            print(f"[dry][images] generating sample images locally (no upload)...")
            i_ok, i_cost, i_credits = dry_images_pass(key, qs, base_dir)
            stats["img_ok"], stats["img_cost"], stats["img_credits"] = i_ok, i_cost, i_credits
            stats["stage_times"]["images"] = stage_secs()
        elif dm == "audio":
            print(f"[dry][audio] generating sample listening audio locally (no upload)...")
            stats["audio_ok"] = dry_audio_pass(mock, base_dir, qs)
            ast = read_audio_stats(mock)
            stats["audio_fallback_clips"] = ast.get("fallback_clips", 0)
            stats["stage_times"]["audio"] = stage_secs()
        else:
            print("[dry][questions] sample authored and saved (no images/audio in questions mode)")
        stats["stage_times"]["total"] = stage_secs()
        report = {
            "mock": mock, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "dry_mode": dm, "questions": len(qs),
            "gen_type": gen_type,
            "blank_count": stats.get("blank_count", 0),
            "blank_numbers": stats.get("blank_numbers", []),
            "blank_detail": stats.get("blank_detail", []),
            "script_expanded": stats.get("script_expanded", 0),
            "audio_fallback_clips": stats.get("audio_fallback_clips", 0),
            "picture_count": stats.get("picture_count", 0),
            "pdf_parser": stats.get("pdf_parser"),
            "pdf_pages": stats.get("pdf_pages"),
            "pdf_images": stats.get("pdf_images"),
            "pdf_parse_s": stats.get("pdf_parse_s"),
            "author_attempt": stats["author_attempt"],
            "author_cost_usd": round(stats["author_cost"], 4),
            "proofread_model": stats["proof_model"], "proofread_cost_usd": round(stats["proof_cost"], 4),
            "repair": stats["repair"], "exam_created": False,
            "image_count": sum(1 for q in qs if q.get("requiresImage")),
            "total_llm_cost_usd": round(stats["llm_cost"], 4),
            "img_model": stats["img_model"],
        "author_via": stats.get("author_via", "openrouter"),
        "gemini_model": str(CFG.get("gemini_model", "")),
        "dedup_enabled": bool(CFG.get("dedup_enabled", True)),
        "dedup_sets_checked": stats.get("dedup_sets_checked"),
        "dedup_checked": bool(stats.get("dedup_checked")),
        "dedup_repeats": stats.get("dedup_repeats", 0),
            "img_credits": stats["img_credits"],
            "img_count": sum(1 for q in qs if q.get("requiresImage")),
            "fal_cost": round(stats["img_cost"], 4) if str(stats["img_model"]).startswith("fal-ai/") else 0.0,
            "fal_balance": None,
            "audio_uploaded": stats.get("audio_ok", 0),
            "stage_times": stats["stage_times"],
        }
        try:
            report["fal_balance"] = fal_balance().get("balance")
        except Exception:
            pass
        (base_dir / "extra" / "run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        final_summary(stats)
        return

    if not CFG.get("pb_pass") or not CFG.get("push_enabled", True):
        print("[local] push disabled or no push password — questions saved locally only (no PB records/audio/images)")
        stats["local_only"] = True
        stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]
        final_summary(stats)
        return

    # 3. PocketBase records — SELF-HEALING: any stored pbId that no longer
    # exists on the server (stale/corrupt, e.g. an image id written by an
    # older run) is dropped and the record re-created. Without this, every
    # upload PATCHes a dead id and silently fails (404), which is exactly
    # the "[upload] image -> p9_img3 HTTP 404" failure.
    headers = pb_headers()
    missing = [q for q in qs if not q.get("pbId")]
    stale = []
    if not missing:
        for q in qs:
            chk = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{q['pbId']}",
                            headers=headers, params={"fields": "id"}, timeout=30)
            if chk.status_code != 200:
                stale.append(q)
        if stale:
            print(f"[pb] {len(stale)} stored pbIds are dead on the server — recreating those records")
            for q in stale:
                q.pop("pbId", None)
            missing = stale
    if missing:
        print(f"[pb] creating {len(missing)} missing question records...")
        ids = create_records(qs, headers)
        it = iter(ids)
        for q in qs:
            if not q.get("pbId"):
                q["pbId"] = next(it)
        qfile.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pb] created {len(ids)} records")
        stats["pb_created"] = True
    else:
        print("[pb] records already exist (resume) — reusing pbIds")
        stats["pb_created"] = False
    ids = [q["pbId"] for q in qs]
    stats["pb_count"] = len(ids)

    # 3a. Repair picture flags on resume / any prior partial push.
    # File uploads are multipart-only; a later JSON PATCH that omits
    # picture_options/correct_answer silently reverts them — re-assert here.
    for q, rid in zip(qs, ids):
        if q.get("picture_options") and rid:
            ensure_picture_record_flags(headers, rid, q)

    # 3b. Exam record + question links (admin dashboard visibility)
    exam_id, exam_created = create_exam(mock, qs, headers)
    stats["exam_created"] = exam_created

    # 4. Audio
    print("[audio] generating listening audio...")
    a_ok = run_audio(mock, qs, ids, headers)
    ast = read_audio_stats(mock)
    stats["audio_fallback_clips"] = ast.get("fallback_clips", 0)
    stats["audio_ok"] = a_ok
    stats["audio_total"] = sum(1 for q in qs if q.get("section") == "listening")
    stats["stage_times"]["audio"] = stage_secs()

    # 5. Images
    pdf_images_map = {}
    paper_photos_map = {}
    if gen_type == 3 and pdf_doc:
        img_by_id = {i["id"]: i for i in pdf_doc["images"]}
        for q in qs:
            pi = str(q.get("pdfImage") or "")
            if pi and pi in img_by_id:
                pdf_images_map[q["number"]] = {"png": img_by_id[pi]["png"]}
        for qnum, ph_ids in paper_img_map.items():
            pp = {"ids": ph_ids, "pngs": {i: img_by_id[i]["png"] for i in ph_ids if i in img_by_id}}
            if pp["pngs"]:
                pp["fill"] = max(0, 4 - len(pp["pngs"]))
                paper_photos_map[qnum] = pp
        if pdf_images_map:
            print(f"[images] {len(pdf_images_map)} extracted paper images will be upscaled (fal recraft/crisp)")
        if paper_photos_map:
            filled = sum(1 for pp in paper_photos_map.values() if pp.get("fill"))
            print(f"[images] {len(paper_photos_map)} paper picture questions use extracted photos"
                  + (f" ({filled} hybrid - missing photos will be generated)" if filled else ""))
    if any(q.get("picture_options") for q in qs):
        ensure_option_images_field(headers)
    print(f"[images] generating TOPIK images ({img_primary})...")
    i_ok, i_cost, i_credits, img_missing = run_images(
        key, qs, ids, headers, base_dir, img_primary,
        pdf_images=pdf_images_map, paper_photos=paper_photos_map)
    stats["img_ok"], stats["img_cost"], stats["img_credits"] = i_ok, i_cost, i_credits
    stats["img_missing"] = img_missing
    stats["img_total"] = sum(1 for q in qs if q.get("requiresImage"))
    stats["pic_photo_total"] = sum(1 for q in qs if q.get("picture_options"))
    stats["stage_times"]["images"] = stage_secs()
    stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]
    stats["stage_times"]["total"] = stage_secs()
    stats["paper_photos_extracted"] = len(pdf_doc["images"]) if (gen_type == 3 and pdf_doc) else 0
    stats["paper_photos_used"] = len(paper_photos_map)
    stats["paper_photos_filled"] = sum(1 for pp in paper_photos_map.values() if pp.get("fill"))
    stats["picture_complete"] = stats.get("picture_self_check")

    # 5b. Contract self-check (picture questions must match client renderer)
    stats["picture_self_check"] = self_check_picture_questions(qs, headers)

    # 6. Report
    report = {
        "mock": mock, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "questions": stats["audio_total"] + sum(1 for q in qs if q.get("section") == "reading"),
        "gen_type": gen_type,
        "blank_count": stats.get("blank_count", 0),
        "blank_numbers": stats.get("blank_numbers", []),
        "blank_detail": stats.get("blank_detail", []),
        "script_expanded": stats.get("script_expanded", 0),
        "audio_fallback_clips": stats.get("audio_fallback_clips", 0),
        "picture_count": stats.get("picture_count", 0),
        "paper_photos_extracted": stats.get("paper_photos_extracted", 0),
        "paper_photos_used": stats.get("paper_photos_used", 0),
        "paper_photos_filled": stats.get("paper_photos_filled", 0),
        "picture_complete": stats.get("picture_complete"),
        "pdf_parser": stats.get("pdf_parser"),
        "pdf_pages": stats.get("pdf_pages"),
        "pdf_images": stats.get("pdf_images"),
        "pdf_parse_s": stats.get("pdf_parse_s"),
        "author_attempt": stats["author_attempt"],
        "author_cost_usd": round(stats["author_cost"], 4),
        "proofread_model": stats["proof_model"], "proofread_cost_usd": round(stats["proof_cost"], 4),
        "repair": stats["repair"], "exam_created": exam_created,
        "exam_id": exam_id,
        "audio_uploaded": a_ok,
        "images_uploaded": i_ok, "image_model": img_primary,
        "images_missing": img_missing,
        "picture_self_check": stats.get("picture_self_check"),
        "image_cost_usd": round(i_cost, 4), "image_credits": i_credits,
        "difficulty_profile": CFG.get("difficulty_profile", "creative+difficult"),
        "image_count": int(CFG.get("image_count", 22)),
        "total_llm_cost_usd": round(stats["llm_cost"], 4),
        "img_model": stats["img_model"],
        "author_via": stats.get("author_via", "openrouter"),
        "gemini_model": str(CFG.get("gemini_model", "")),
        "dedup_enabled": bool(CFG.get("dedup_enabled", True)),
        "dedup_sets_checked": stats.get("dedup_sets_checked"),
        "dedup_checked": bool(stats.get("dedup_checked")),
        "dedup_repeats": stats.get("dedup_repeats", 0),
        "img_credits": stats["img_credits"],
        "img_count": stats["img_total"],
        "fal_cost": round(stats["img_cost"], 4) if str(stats["img_model"]).startswith("fal-ai/") else 0.0,
        "fal_balance": None,
        "or_balance": None,
        "stage_times": stats["stage_times"],
    }
    try:
        report["fal_balance"] = fal_balance().get("balance")
    except Exception:
        pass
    try:
        report["or_balance"] = (or_balance() or {}).get("remaining")
    except Exception:
        pass
    (base_dir / "extra" / "run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    final_summary(stats)
    print(f"\nReport: {base_dir / 'extra' / 'run_report.json'}")


if __name__ == "__main__":
    main()
