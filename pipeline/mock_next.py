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
    "difficulty_profile": "creative+difficult",  # creative+difficult | creative+medium | hard | standard
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
    "gemini_model": "gemini-3.5-flash",      # Gemini author model (gemini-3.6-flash / gemini-3.5-flash / gemini-3.5-flash-lite / gemini-2.5-pro)
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
        "Simple flat vector illustration in the standard Korean TOPIK test style, "
        "minimal clean line art, plain solid white background, simple everyday scene, "
        "centered single main subject, clear silhouette, no text, no letters, no numbers, "
        "no watermark, no border"
    ),
    "image_verify_after": True,
    # audio / TTS — OpenRouter
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "microsoft/mai-voice-2-flash",
    "tts_fallback_voice": "ko-KR-Haena:MAI-Voice-2",
    "tts_rate": 44100,
    "tts_gap_ms": 400,
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
    """Fill {placeholders} in a prompt template from the active config."""
    out = template
    r = int(CFG.get("reading_count", 20))
    q = int(CFG.get("question_count", 40))
    ls = int(CFG.get("reading_count", 20)) + 1
    if r <= 0:
        section_order = f'ALL questions are section "listening" (Q1-{q}) - no reading section'
    else:
        section_order = f'Q1-{r} section "reading", Q{ls}-{q} section "listening"'
    ctx = {**CFG,
           "difficulty_note": DIFFICULTY_PROFILES.get(CFG.get("difficulty_profile"), ""),
           "focus_note": ("The teacher needs to test this specific area — give questions that match: " +
                          str(CFG.get("focus"))).strip() if str(CFG.get("focus") or "").strip() else "",
           "listening_start": ls,
           "section_order": section_order}
    for k, v in ctx.items():
        if isinstance(v, (str, int, float)):
            out = out.replace("{" + k + "}", str(v))
    return out


CONFIG_LOADED = load_config()   # reads $MOCK_CONFIG at import time (before main)

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


def gemini_author(system, user, model):
    """Author via the DIRECT Google Gemini API. Raises GeminiQuotaError on quota,
    GeminiAuthError on bad key, GeminiModelError on bad model."""
    key = gemini_api_key()
    if not key:
        raise GeminiAuthError("GEMINI_API_KEY is not set")
    api = CFG.get("gemini_api", "https://generativelanguage.googleapis.com/v1beta")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": int(CFG.get("author_max_tokens", 32000)),
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }
    r = httpx.post(f"{api}/models/{model}:generateContent", params={"key": key},
                   json=body, timeout=int(CFG.get("llm_timeout_s", 600)))
    if r.status_code == 429 or "RESOURCE_EXHAUSTED" in r.text:
        raise GeminiQuotaError("Gemini quota exceeded (429)")
    if r.status_code == 404:
        raise GeminiModelError(f"Gemini model '{model}' not found")
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
- Reading mixes: fill-in-blank, grammar in context, vocabulary, sentence completion, conversation
  completion, honorifics, idioms, connectors, sentence ordering, reading comprehension
  (안내문/공지/이메일/광고/편지/일기), situation judgment, sign/menu/schedule/map/notice interpretation.
- Listening (Q{listening_start}-{question_count}): each needs "listening": {"audioScript": [{"voice":"V1".."V4","text":"..."}],
  "durationSeconds", "speakers", "situation"}. Situations rotate: phone, 방송, news, weather,
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
Return the JSON array ONLY — no markdown, no commentary."""


PROOF_SYSTEM = """You are a meticulous Korean-language examiner. Fix every grammar, particle, spelling and
띄어쓰기 error and any awkward/unnatural phrasing. Keep the JSON structure EXACTLY identical
(keys, numbers, options order, correct_answer, section, audioScript voices). Output ONLY the corrected JSON array."""


def normalize_exam(qs):
    """Coerce LLM quirks into the canonical schema (int answers, 보기 tags, script placement)."""
    circle = {"①": "0", "②": "1", "③": "2", "④": "3",
              "1.": "0", "2.": "1", "3.": "2", "4.": "3",
              "a": "0", "b": "1", "c": "2", "d": "3",
              "A": "0", "B": "1", "C": "2", "D": "3"}
    for q in qs:
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
    return qs


REPAIR_SYSTEM = "You are a Korean EPS-TOPIK exam writer. Output ONLY valid JSON."


def repair_exam(key, qs, stats=None):
    """Fix what the author missed: missing listening dialogues, invalid answers, low image count."""
    model_cfg = repair_cfg()
    stats = stats if stats is not None else {}
    stats.setdefault("scripts", 0)
    stats.setdefault("answers", 0)
    stats.setdefault("images", 0)
    for q in qs:
        if q.get("section") == "listening" and not (q.get("listening") or {}).get("audioScript"):
            print(f"[repair] writing listening script for Q{q['number']}...")
            user = (
                "다음 한국어 듣기 문제를 위한 자연스러운 대화(2-4턴, 턴당 최대 2문장, 구어체)를 작성하세요. "
                'JSON만: {"audioScript": [{"voice": "V1"~"V4", "text": "..."}], '
                '"durationSeconds": 15, "speakers": 2, "situation": "..."}\n'
                f"문제: {q.get('question_text')}\n선택지: {q.get('options')}\n"
                f"정답(인덱스): {q.get('correct_answer')}\n"
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
        if not isinstance(opts, list) or len(opts) != 4 or any(not o for o in opts):
            errs.append(f"Q{n}: need 4 non-empty options")
        elif all(re.fullmatch(r"\d{1,2}|[①②③④]", str(o).strip()) for o in opts):
            errs.append(f"Q{n}: options are placeholders (digits), not real Korean text")
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


def llm_author(key, attempt, model_cfg, extra_user=""):
    user = render_prompt(AUTHOR_USER) + extra_user
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


def create_records(qs, headers):
    """Batch-create questions missing a pbId (resume-safe). Returns newly created ids."""
    ops = []
    for q in qs:
        if q.get("pbId"):
            continue
        body = {
            "section": q["section"],
            "subject": CFG["subject_id"],
            "question_text": q["question_text"],
            "question_type": "single_choice",
            "options": q["options"],
            "correct_answer": q["correct_answer"],
            "marks": int(q.get("marks") or CFG.get("marks_per_question", 1)),
            "negative_marks": int(CFG.get("negative_marks", 0)),
            "explanation": q.get("explanation", ""),
            "difficulty": DIFF_MAP.get(str(q.get("difficulty", "hard")).lower(), "hard"),
            "is_active": bool(CFG.get("is_active", True)),
        }
        ops.append({"method": "POST", "url": "/api/collections/questions/records", "body": body})
    if not ops:
        return []
    r = httpx.post(CFG["pb_base"] + "/api/batch", headers=headers, json={"requests": ops}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"batch create failed HTTP {r.status_code}: {r.text[:400]}")
    per = r.json()  # top-level list of {"status": 200, "body": {record}}
    ids = []
    for i, p in enumerate(per):
        if p.get("status") != 200:
            raise RuntimeError(f"record {i} failed: {json.dumps(p, ensure_ascii=False)[:200]}")
        ids.append(p["body"]["id"])
    return ids


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
        r = httpx.post(CFG["pb_base"] + "/api/batch", headers=headers, json={"requests": ops}, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"exam_questions batch failed HTTP {r.status_code}: {r.text[:200]}")
    print(f"[pb] exam {exam_id} ({'created' if created else 'reused'}) + {len(ops)} links")
    return exam_id, created


def upload_file(headers, record_id, field, filename, content, ctype):
    r = httpx.patch(CFG["pb_base"] + f"/api/collections/questions/records/{record_id}",
                    headers=headers, files={field: (filename, content, ctype)}, timeout=120)
    if r.status_code not in (200, 201):
        print(f"[upload] {field} -> {record_id} HTTP {r.status_code}: {r.text[:200]}")
    return r.status_code in (200, 201)


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


def run_images(key, qs, record_ids, headers, work_dir, img_model=None):
    """Generate TOPIK images for requiresImage questions.

    Model chain (first working wins): primary provider (CFG img_model) ->
    then the other providers (z-image / OpenRouter flux / fal-ai) in order.
    Each step retried per CFG img_retries / img_fallback_retries, with size + server-side verify,
    then ONE backfill pass for anything still missing.
    Returns (ok, cost, credits, missing_numbers)."""
    from PIL import Image  # noqa: F401 — ensure importable early
    primary = img_model or CFG.get("img_model") or "z-image"
    chain = img_chain(primary)
    img_retries = int(CFG.get("img_retries", 2))
    fb_retries = int(CFG.get("img_fallback_retries", 2))
    verify = bool(CFG.get("image_verify_after", True))
    max_size = int(CFG.get("img_max_size", 1024))
    quality = int(CFG.get("img_quality", 80))
    jobs = []
    for q, rid in zip(qs, record_ids):
        if not q.get("requiresImage"):
            continue
        rec = httpx.get(CFG["pb_base"] + f"/api/collections/questions/records/{rid}",
                        headers=headers, params={"fields": "image"}, timeout=30)
        if rec.status_code == 200 and rec.json().get("image"):
            continue  # resume: image already uploaded — skip (saves credits)
        prompt = f"{CFG['image_style_prompt']}. Scene: {q['imagePrompt']}."
        jobs.append((q["number"], rid, prompt))
    if not jobs:
        print("[images] no image questions")
        return 0, 0.0, 0, []
    if primary == "z-image":
        bal = magnific_mcp.check_balance()
        if bal:
            avail = (bal.get("credits") or {}).get("available", 0)
            needed = len(jobs) * magnific_mcp.ZIMAGE_COST
            print(f"[images] z-image: {len(jobs)} images x {magnific_mcp.ZIMAGE_COST} credits "
                  f"= {needed} credits (available: {avail})")
            if avail < needed:
                sys.exit(f"ERROR: not enough Magnific credits ({avail} < {needed})")
        else:
            print(f"[images] z-image (API mode): {len(jobs)} images x {magnific_mcp.ZIMAGE_COST} credits "
                  "(live balance check not available via API)")
    elif str(primary).startswith("fal-ai/"):
        print(f"[images] fal.ai provider: {len(jobs)} image(s) x {CFG.get('fal_size', 512)}x{CFG.get('fal_size', 512)} "
              "(FAL_KEY from env, no Magnific balance check)")
    else:
        print(f"[images] model chain: {chain}")
    ok, cost, credits = 0, 0.0, 0
    missing = []
    out_dir = work_dir / "images" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(job):
        num, rid, prompt = job
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=CFG["img_workers"]) as ex:
        for num, up, used, model in ex.map(one, jobs):
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

    if missing:
        print(f"[images] backfill pass for {len(missing)} missing...")
        for num, rid, prompt in jobs:
            if num not in missing:
                continue
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


# ---------------------------------------------------------------------------

def _short(model):
    return model.split("/")[-1].split("-0")[0] if "/" in model else model


def dry_images_pass(key, qs, base_dir):
    """Dry-run images: generate for requiresImage questions, save locally, NO upload."""
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
    print(f"[dry][images] {len(jobs)} image(s) to generate locally via {chain[0]}")
    ok = 0
    cost = 0.0
    credits = 0
    for num, prompt in jobs:
        fprompt = f"{CFG['image_style_prompt']}. Scene: {prompt}."
        done = False
        for model in chain:
            try:
                if model == "nano-banana" or model == "black-forest-labs/flux.2-klein-4b":
                    data, usage = gen_image_nano(key, fprompt)
                    cost += usage.get("cost", 0)
                elif str(model).startswith("fal-ai/"):
                    data, used = gen_image_fal(fprompt)
                    cost += used
                else:
                    url = gen_image_or(key, fprompt, model)
                    data = httpx.get(url, timeout=120).content
                    if model == "z-image":
                        credits += magnific_mcp.ZIMAGE_COST
                if not data or len(data) < 500:
                    raise RuntimeError(f"image too small ({len(data) if data else 0} bytes)")
                webp = to_webp(data, max_size=int(CFG.get("img_max_size", 1024)),
                               quality=int(CFG.get("img_quality", 80)))
                (out_dir / f"q{num}.webp").write_bytes(webp)
                print(f"[dry][images] Q{num}: generated q{num}.webp via {model} (saved locally, not uploaded)")
                ok += 1
                done = True
                break
            except Exception as e:
                print(f"[dry][images] Q{num} via {model} failed: {str(e)[:110]}")
        if not done:
            print(f"[dry][images] Q{num}: FAILED on all models")
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
        "stage_times": {},
    }
    t_all = time.time()
    def stage_secs():
        return int(time.time() - t_all)

    if qfile.exists():
        print("[resume] questions file exists — skipping authoring/proofread")
        qs = json.loads(qfile.read_text(encoding="utf-8"))
        if validate_exam(qs):
            sys.exit("FAILED: existing questions file is invalid — delete it and re-run")
        stats["authored"] = False
    else:
        # 1. Author - DUAL PROVIDER: Gemini (direct Google API) first, OpenRouter fallback
        qs, last_err, author_via = None, None, ""
        sys_prompt = AUTHOR_SYSTEM
        user_prompt = render_prompt(AUTHOR_USER)
        dedup_serials, dedup_texts = (None, None)
        if CFG.get("dedup_enabled", True):
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
        if str(CFG.get("author_provider", "gemini")) == "gemini" and gkey:
            for attempt in range(gretries):
                print(f"[author] gemini ({gmodel}) attempt {attempt + 1}...")
                try:
                    raw, _usage = gemini_author(sys_prompt, user_prompt, gmodel)
                    gqs = json.loads(raw.strip(), strict=False)
                    if isinstance(gqs, dict):
                        for k in ("questions", "items", "results", "data", "exam"):
                            if isinstance(gqs.get(k), list):
                                gqs = gqs[k]
                                break
                    if isinstance(gqs, list):
                        gqs = [q for q in gqs if isinstance(q, dict)]
                    gqs = sanitize_exam(gqs)
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
                    if not gerrs:
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
                    qs, usage = llm_author(key, last_err, author_cfg, dedup_block)
                except Exception as e:
                    print(f"[author] generation error: {str(e)[:120]}")
                    last_err = "previous generation failed or returned broken JSON — return complete valid JSON"
                    continue
                qs = normalize_exam(qs)
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
                if not errs:
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
        stats["stage_times"]["author"] = stage_secs()

        # 1b. Choose proofreading model (config-driven when --config/$MOCK_CONFIG, else interactive)
        proof_cfg = slug_cfg(args.proof_slug, PROOF_MODELS) if args.proof_slug \
            else (slug_cfg(CFG["proof_model"], PROOF_MODELS)
                  if (CONFIG_LOADED or CFG["proof_model"] != DEFAULTS["proof_model"])
                  else pick_model(PROOF_MODELS, "Proofreading model"))
        stats["proof_model"] = proof_cfg["name"]

        # 2. Proofread + repair (fill missing dialogues/answers, then Korean quality pass)
        print("[repair] fixing missing dialogues/answers...")
        qs = normalize_exam(repair_exam(key, qs, stats["repair"]))
        repaired = qs if not validate_exam(qs) else None
        if repaired is None:
            print("[repair] second pass...")
            qs = normalize_exam(repair_exam(key, qs, stats["repair"]))
            if not validate_exam(qs):
                repaired = qs
        print(f"[proofread] {proof_cfg['name']} checking Korean quality...")
        qs, pu = llm_proofread(key, qs, proof_cfg)
        stats["proof_cost"] = pu.get("cost", 0.0)
        qs = normalize_exam(qs)
        if validate_exam(qs) and repaired is not None:
            print("[proofread] broke structure — reverting to repaired version")
            qs = repaired
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
            stats["stage_times"]["audio"] = stage_secs()
        else:
            print("[dry][questions] sample authored and saved (no images/audio in questions mode)")
        stats["stage_times"]["total"] = stage_secs()
        report = {
            "mock": mock, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "dry_mode": dm, "questions": len(qs),
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

    # 3. PocketBase records
    headers = pb_headers()
    missing = [q for q in qs if not q.get("pbId")]
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

    # 3b. Exam record + question links (admin dashboard visibility)
    exam_id, exam_created = create_exam(mock, qs, headers)
    stats["exam_created"] = exam_created

    # 4. Audio
    print("[audio] generating listening audio...")
    a_ok = run_audio(mock, qs, ids, headers)
    stats["audio_ok"] = a_ok
    stats["audio_total"] = sum(1 for q in qs if q.get("section") == "listening")
    stats["stage_times"]["audio"] = stage_secs()

    # 5. Images
    print(f"[images] generating TOPIK images ({img_primary})...")
    i_ok, i_cost, i_credits, img_missing = run_images(key, qs, ids, headers, base_dir, img_primary)
    stats["img_ok"], stats["img_cost"], stats["img_credits"] = i_ok, i_cost, i_credits
    stats["img_missing"] = img_missing
    stats["img_total"] = sum(1 for q in qs if q.get("requiresImage"))
    stats["stage_times"]["images"] = stage_secs()
    stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]
    stats["stage_times"]["total"] = stage_secs()

    # 6. Report
    report = {
        "mock": mock, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "questions": stats["audio_total"] + sum(1 for q in qs if q.get("section") == "reading"),
        "author_attempt": stats["author_attempt"],
        "author_cost_usd": round(stats["author_cost"], 4),
        "proofread_model": stats["proof_model"], "proofread_cost_usd": round(stats["proof_cost"], 4),
        "repair": stats["repair"], "exam_created": exam_created,
        "exam_id": exam_id,
        "audio_uploaded": a_ok,
        "images_uploaded": i_ok, "image_model": img_primary,
        "images_missing": img_missing,
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
