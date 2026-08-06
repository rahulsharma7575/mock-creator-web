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
OR_API = "https://openrouter.ai/api/v1/chat/completions"
OR_IMG = "https://openrouter.ai/api/v1/images/generations"
PB_BASE = "https://ubt.wts.com.np"
PB_EMAIL = "shiva@cld.com.np"
PB_PASS = "shiva@saharsh"
SUBJECT_ID = "illfosglou0e3j6"          # Korean Language UBT

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
IMG_MODEL_NANO = "google/gemini-2.5-flash-image"   # OpenRouter fallback ($0.039/img)
IMG_MODEL_Z = "z-image"                           # Magnific MCP (5 credits/img — preferred)
IMG_WORKERS = 3
REPAIR_MODEL = {"name": "gemini-2.5-flash", "slug": "google/gemini-2.5-flash",
                "price": "$0.30/$2.50 per M", "extra": {}}  # repair always uses a strong model

STYLE_PROMPT = (
    "Simple flat vector illustration in the standard Korean TOPIK test style, "
    "minimal clean line art, plain solid white background, simple everyday scene, "
    "centered single main subject, clear silhouette, no text, no letters, no numbers, "
    "no watermark, no border"
)

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
    r = httpx.post(OR_API, headers={"Authorization": f"Bearer {key}"}, json=payload, timeout=600)
    j = r.json()
    if "choices" not in j:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {json.dumps(j, ensure_ascii=False)[:300]}")
    content = j["choices"][0]["message"].get("content") or ""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
    return json.loads(content), j.get("usage", {})


AUTHOR_SYSTEM = """You are a senior EPS-TOPIK UBT exam writer and psychometric expert. Output ONLY valid JSON."""

AUTHOR_USER = """Write a complete Korean EPS-TOPIK UBT mock exam: EXACTLY 40 questions as a JSON array.

HARD RULES:
- Q1-20 section "reading", Q21-40 section "listening" (in that order, number 1..40 unique).
- Difficulty: "medium", "hard", or "very hard" only (no easy). Never beginner drills.
- Each question: number, section, difficulty, type (short English label), question_text (Korean,
  starts with "Q<N>. ", NO html), options (4 REAL Korean strings — natural, believable, similar
  length, only ONE best; options MUST be actual Korean words/phrases, NEVER numbers, digits,
  placeholders like "0 1 2 3", or empty), correct_answer (array with ONE option index string
  like ["2"]), marks 1, explanation (Korean, 1-2 sentences), requiresImage (bool), imagePrompt
  (English, detailed: people/objects/actions/environment/camera angle/key visual clues — ONLY
  when requiresImage true).
- NEVER reuse the exact same question_text for two questions in the exam — vary the stems
  (e.g. "그림을 보고 알맞은 것을 고르십시오." vs "그림을 보고 알맞지 않은 것을 고르십시오." vs
  "그림을 보고 무엇에 대한 그림인지 고르십시오.") — every question_text must be unique.
- EXACTLY 16 questions (40% of 40, spread randomly through 1-40) MUST have requiresImage true
  plus a detailed imagePrompt (English: people/objects/actions/environment/camera angle/key clues).
- Reading mixes: fill-in-blank, grammar in context, vocabulary, sentence completion, conversation
  completion, honorifics, idioms, connectors, sentence ordering, reading comprehension
  (안내문/공지/이메일/광고/편지/일기), situation judgment, sign/menu/schedule/map/notice interpretation.
- Listening (Q21-40): each needs "listening": {"audioScript": [{"voice":"V1".."V4","text":"..."}],
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


def repair_exam(key, qs, stats=None, model_cfg=None):
    """Fix what the author missed: missing listening dialogues, invalid answers, low image count.

    stats (dict, optional) is filled with repair counters: scripts, answers, images.
    model_cfg (optional) — repair always runs on a strong fixed model (REPAIR_MODEL)."""
    model_cfg = REPAIR_MODEL
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
    if img < 10:
        need = 10 - img
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


def validate_exam(qs, stage="final"):
    """stage='author' = structure only (repair pass fixes the rest); 'final' = everything."""
    errs = []
    if not isinstance(qs, list) or len(qs) != 40:
        errs.append(f"need exactly 40 questions, got {len(qs) if isinstance(qs, list) else type(qs)}")
        return errs
    nums = [q.get("number") for q in qs]
    if sorted(nums) != list(range(1, 41)):
        errs.append("numbers must be 1..40 unique")
    secs = [q.get("section") for q in qs]
    if secs[:20] != ["reading"] * 20 or secs[20:] != ["listening"] * 20:
        errs.append("Q1-20 reading, Q21-40 listening required")
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
        lo = 6 if img < 10 else 10
        if not (lo <= img <= 18):
            errs.append(f"image questions must be ~40% ({lo}-18 accepted), got {img}")
    return errs


def llm_author(key, attempt, model_cfg):
    user = AUTHOR_USER
    if attempt:
        user += f"\n\nPREVIOUS ATTEMPT FAILED VALIDATION:\n{attempt}"
    qs, usage = chat_json(key, model_cfg["slug"], AUTHOR_SYSTEM, user,
                          max_tokens=32000, extra=model_cfg["extra"])
    # some models wrap the array in an object — unwrap; keep only dict entries
    if isinstance(qs, dict):
        for k in ("questions", "items", "results", "data", "exam"):
            if isinstance(qs.get(k), list):
                qs = qs[k]
                break
    if isinstance(qs, list):
        qs = [q for q in qs if isinstance(q, dict)]
    return qs, usage


def llm_proofread(key, qs, model_cfg):
    try:
        fixed, usage = chat_json(key, model_cfg["slug"], PROOF_SYSTEM,
                                 json.dumps(qs, ensure_ascii=False), max_tokens=32000,
                                 temperature=0.3, extra=model_cfg["extra"])
        if isinstance(fixed, dict):
            for k in ("questions", "items", "results", "data", "exam"):
                if isinstance(fixed.get(k), list):
                    fixed = fixed[k]
                    break
        if isinstance(fixed, list):
            fixed = [q for q in fixed if isinstance(q, dict)]
        return fixed, usage
    except Exception as e:
        print(f"[proofread] failed ({e}) — keeping original")
        return qs, {}


# ---------------------------------------------------------------------------
# PocketBase
# ---------------------------------------------------------------------------

def pb_headers():
    r = httpx.post(PB_BASE + "/api/collections/_superusers/auth-with-password",
                   json={"identity": PB_EMAIL, "password": PB_PASS}, timeout=30)
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["token"]}


DIFF_MAP = {"medium": "medium", "hard": "hard", "very hard": "hard"}


def create_records(qs, headers):
    """Batch-create all 40 questions; return list of created record ids (same order)."""
    ops = []
    for q in qs:
        body = {
            "section": q["section"],
            "subject": SUBJECT_ID,
            "question_text": q["question_text"],
            "question_type": "single_choice",
            "options": q["options"],
            "correct_answer": q["correct_answer"],
            "marks": q.get("marks", 1),
            "negative_marks": 0,
            "explanation": q.get("explanation", ""),
            "difficulty": DIFF_MAP.get(str(q.get("difficulty", "hard")).lower(), "hard"),
            "is_active": True,
        }
        ops.append({"method": "POST", "url": "/api/collections/questions/records", "body": body})
    r = httpx.post(PB_BASE + "/api/batch", headers=headers, json={"requests": ops}, timeout=120)
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
    r = httpx.get(PB_BASE + "/api/collections/exams/records", headers=headers,
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
        r = httpx.post(PB_BASE + "/api/collections/exams/records", headers=headers, json={
            "id": exam_id, "title": title, "code": code, "exam_type": "ubt",
            "subject": SUBJECT_ID, "duration_minutes": 50, "total_questions": 40,
            "total_marks": 40, "pass_marks": 24, "shuffle_questions": True,
            "shuffle_options": False, "status": "published", "is_active": True,
            "plans": plans,
        }, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"exam create failed HTTP {r.status_code}: {r.text[:200]}")
        created = True
    else:
        # keep serial title/code/plan ladder up to date on resume
        httpx.patch(PB_BASE + f"/api/collections/exams/records/{exam_id}",
                    headers=headers, json={"title": title, "code": code, "plans": plans},
                    timeout=30)
    # junctions: create only missing (unique exam+question constraint)
    linked = httpx.get(PB_BASE + "/api/collections/exam_questions/records", headers=headers,
                       params={"filter": f'exam="{exam_id}"', "perPage": 200,
                               "fields": "question"}, timeout=30).json()
    have = {i["question"] for i in linked.get("items", [])}
    ops = []
    for q in qs:
        pid = q.get("pbId")
        if pid and pid not in have:
            ops.append({"method": "POST", "url": "/api/collections/exam_questions/records",
                        "body": {"exam": exam_id, "question": pid,
                                 "order": q.get("number", 0), "marks": 1}})
    if ops:
        r = httpx.post(PB_BASE + "/api/batch", headers=headers, json={"requests": ops}, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"exam_questions batch failed HTTP {r.status_code}: {r.text[:200]}")
    print(f"[pb] exam {exam_id} ({'created' if created else 'reused'}) + {len(ops)} links")
    return exam_id, created


def upload_file(headers, record_id, field, filename, content, ctype):
    r = httpx.patch(PB_BASE + f"/api/collections/questions/records/{record_id}",
                    headers=headers, files={field: (filename, content, ctype)}, timeout=120)
    return r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def gen_image_or(key, prompt):
    """z-image via Magnific MCP (default, 5 credits) — no OpenRouter call needed."""
    return magnific_mcp.generate_image(prompt)


def gen_image_nano(key, prompt):
    r = httpx.post(OR_IMG, headers={"Authorization": f"Bearer {key}"},
                   json={"model": IMG_MODEL_NANO, "prompt": prompt, "n": 1}, timeout=300)
    j = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"img HTTP {r.status_code}: {str(j.get('error'))[:150]}")
    first = j["data"][0]
    b64 = first.get("b64_json") or first.get("image_b64")
    return base64.b64decode(b64), j.get("usage", {})


def to_webp(data, max_size=1024, quality=80):
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=quality, method=6, optimize=True)
    return buf.getvalue()


def run_images(key, qs, record_ids, headers, work_dir, img_model="z-image"):
    from PIL import Image  # noqa: F401 — ensure importable early
    jobs = []
    for q, rid in zip(qs, record_ids):
        if not q.get("requiresImage"):
            continue
        rec = httpx.get(PB_BASE + f"/api/collections/questions/records/{rid}",
                        headers=headers, params={"fields": "image"}, timeout=30)
        if rec.status_code == 200 and rec.json().get("image"):
            continue  # resume: image already uploaded — skip (saves credits)
        prompt = f"{STYLE_PROMPT}. Scene: {q['imagePrompt']}."
        jobs.append((q["number"], rid, prompt))
    if not jobs:
        print("[images] no image questions")
        return 0, 0.0, 0
    if img_model == "z-image":
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
    ok, cost, credits = 0, 0.0, 0
    out_dir = work_dir / "images" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    def one(job):
        num, rid, prompt = job
        for attempt in range(2):
            try:
                if img_model == "z-image":
                    url = gen_image_or(key, prompt)
                    data = httpx.get(url, timeout=120).content
                    used = magnific_mcp.ZIMAGE_COST
                else:
                    data, usage = gen_image_nano(key, prompt)
                    used = usage.get("cost", 0)
                webp = to_webp(data)
                path = out_dir / f"q{num}.webp"
                path.write_bytes(webp)
                up = upload_file(headers, rid, "image", f"q{num}.webp", webp, "image/webp")
                return (num, True, up, used)
            except Exception as e:
                print(f"    q{num} attempt {attempt + 1}: {str(e)[:120]}")
                time.sleep(2)
        return (num, False, False, 0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=IMG_WORKERS) as ex:
        for num, ok_gen, up, used in ex.map(one, jobs):
            if img_model == "z-image":
                credits += used
            else:
                cost += used
            if ok_gen and up:
                ok += 1
                print(f"[images] q{num} OK")
            else:
                print(f"[images] q{num} FAILED (upload={up})")
    return ok, cost, credits


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
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE, timeout=1800)
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


def final_summary(stats):
    W = 40

    def line(label, value):
        print(f"  {label.ljust(W)} → {value}")

    print("=" * 66)
    print(f"  MOCK {stats['mock']} — COMPLETE")
    print("-" * 66)
    if stats.get("authored"):
        line(f"author 40 questions ({_short(stats['author_model'])})",
             f"${stats['author_cost']:.3f}, attempt {stats['author_attempt']}")
    else:
        line("author 40 questions", "resumed (existing file)")
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
    if stats.get("dry_run"):
        line("PocketBase records", "skipped (dry-run)")
        line("listening mp3s", "skipped (dry-run)")
        line("TOPIK images", "skipped (dry-run)")
        line("questions file", str(stats["qfile"]))
        line("total LLM cost", f"${stats['llm_cost']:.4f}")
        print("=" * 66)
        print("  DRY RUN — nothing pushed, no audio/images. Re-run without --dry-run.")
        print("=" * 66)
        return
    line(f"{stats['pb_count']} PocketBase records",
         "created" if stats["pb_created"] else "reused (resume)")
    line("exam + question links",
         "created" if stats.get("exam_created") else "reused (resume)")
    line(f"{stats['audio_ok']}/20 listening mp3s", "fish-audio, uploaded")
    if stats["img_model"] == "z-image":
        line(f"{stats['img_ok']}/{stats['img_total']} TOPIK images",
             f"z-image via Magnific MCP, {stats['img_credits']} credits, uploaded")
    else:
        line(f"{stats['img_ok']}/{stats['img_total']} TOPIK images",
             f"nano-banana (OpenRouter), ${stats['img_cost']:.2f}, uploaded")
    line("questions file", str(stats["qfile"]))
    line("total LLM cost", f"${stats['llm_cost']:.4f}")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="One-click full mock creator (author->audio->images->PB)")
    ap.add_argument("--mock", type=int, default=None, help="Force mock number (default: next)")
    ap.add_argument("--dry-run", action="store_true", help="Author + validate + save only (no PB/audio/images)")
    ap.add_argument("--img-model", choices=["z-image", "nano-banana"], default="z-image",
                    help="Image model: z-image (Magnific, 5 credits) or nano-banana (OpenRouter $)")
    ap.add_argument("--author-slug", default="", help="Author model slug (skips the interactive menu)")
    ap.add_argument("--proof-slug", default="", help="Proofread model slug (skips the interactive menu)")
    args = ap.parse_args()

    key = api_key()
    mock = args.mock or next_mock_number()
    print(f"=== Creating Mock {mock} ===")
    base_dir = BASE / f"mock{mock}"
    for sub in ("questions", "audio", "images", "extra"):
        (base_dir / sub).mkdir(parents=True, exist_ok=True)
    qfile = base_dir / "questions" / f"mock{mock}_questions.json"

    stats = {
        "mock": mock, "authored": True, "author_model": "?",
        "author_attempt": 0, "author_cost": 0.0, "proof_model": "?",
        "proof_cost": 0.0, "repair": {"answers": 0, "scripts": 0, "images": 0},
        "pb_count": 0, "pb_created": False, "exam_created": False, "audio_ok": 0,
        "img_ok": 0, "img_total": 0, "img_model": args.img_model,
        "img_cost": 0.0, "img_credits": 0, "qfile": qfile, "llm_cost": 0.0,
    }

    if qfile.exists():
        print("[resume] questions file exists — skipping authoring/proofread")
        qs = json.loads(qfile.read_text(encoding="utf-8"))
        if validate_exam(qs):
            sys.exit("FAILED: existing questions file is invalid — delete it and re-run")
        stats["authored"] = False
    else:
        # 0. Choose authoring model (interactive unless --author-slug given)
        author_cfg = slug_cfg(args.author_slug, AUTHOR_MODELS) if args.author_slug \
            else pick_model(AUTHOR_MODELS, "Questions generation model")
        stats["author_model"] = author_cfg["name"]

        # 1. Author (up to 3 attempts; structure-only check — repair pass fixes the rest)
        qs, last_err = None, None
        for attempt in range(3):
            print(f"[author] {author_cfg['name']} attempt {attempt + 1}...")
            try:
                qs, usage = llm_author(key, last_err, author_cfg)
            except Exception as e:
                print(f"[author] generation error: {str(e)[:120]}")
                last_err = "previous generation failed or returned broken JSON — return complete valid JSON"
                continue
            qs = normalize_exam(qs)
            errs = validate_exam(qs, stage="author")
            if not errs:
                stats["author_attempt"] = attempt + 1
                stats["author_cost"] = usage.get("cost", 0.0)
                print(f"[author] OK — cost ${stats['author_cost']:.4f}")
                break
            last_err = "; ".join(errs)
            print(f"[author] validation failed: {last_err}")
        if not qs or validate_exam(qs, stage="author"):
            sys.exit("FAILED: could not author a valid exam after 3 attempts")

        # 1b. Choose proofreading model (interactive unless --proof-slug given)
        proof_cfg = slug_cfg(args.proof_slug, PROOF_MODELS) if args.proof_slug \
            else pick_model(PROOF_MODELS, "Proofreading model")
        stats["proof_model"] = proof_cfg["name"]

        # 2. Proofread + repair (fill missing dialogues/answers, then Korean quality pass)
        authored = qs
        print("[repair] fixing missing dialogues/answers...")
        qs = normalize_exam(repair_exam(key, qs, stats["repair"], author_cfg))
        if validate_exam(qs):
            print("[repair] second pass...")
            qs = normalize_exam(repair_exam(key, qs, stats["repair"], author_cfg))
        print(f"[proofread] {proof_cfg['name']} checking Korean quality...")
        qs, pu = llm_proofread(key, qs, proof_cfg)
        stats["proof_cost"] = pu.get("cost", 0.0)
        qs = normalize_exam(qs)
        if validate_exam(qs):
            print("[proofread] broke structure — reverting to repaired version")
            qs = normalize_exam(repair_exam(key, authored, stats["repair"], author_cfg))
        qs.sort(key=lambda q: q["number"])
        if validate_exam(qs):
            sys.exit("FAILED: exam still invalid after repair+proofread — delete the folder and re-run")
        qfile.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[save] {qfile}")

    if args.dry_run:
        print("[dry-run] stopping after author+proofread (no PB/audio/images)")
        stats["dry_run"] = True
        stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]
        final_summary(stats)
        return

    # 3. PocketBase records
    headers = pb_headers()
    if all(q.get("pbId") for q in qs):
        print("[pb] records already exist (resume) — reusing pbIds")
        ids = [q["pbId"] for q in qs]
    else:
        print("[pb] creating 40 question records...")
        ids = create_records(qs, headers)
        for q, rid in zip(qs, ids):
            q["pbId"] = rid
        qfile.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[pb] created {len(ids)} records")
    stats["pb_count"] = len(ids)
    stats["pb_created"] = all(q.get("pbId") for q in qs)

    # 3b. Exam record + question links (admin dashboard visibility)
    exam_id, exam_created = create_exam(mock, qs, headers)
    stats["exam_created"] = exam_created

    # 4. Audio
    print("[audio] generating listening audio...")
    a_ok = run_audio(mock, qs, ids, headers)
    stats["audio_ok"] = a_ok

    # 5. Images
    print(f"[images] generating TOPIK images ({args.img_model})...")
    i_ok, i_cost, i_credits = run_images(key, qs, ids, headers, base_dir, args.img_model)
    stats["img_ok"], stats["img_cost"], stats["img_credits"] = i_ok, i_cost, i_credits
    stats["img_total"] = sum(1 for q in qs if q.get("requiresImage"))
    stats["llm_cost"] = stats["author_cost"] + stats["proof_cost"]

    # 6. Report
    report = {
        "mock": mock, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "questions": 40, "author_attempt": stats["author_attempt"],
        "author_cost_usd": round(stats["author_cost"], 4),
        "proofread_model": stats["proof_model"], "proofread_cost_usd": round(stats["proof_cost"], 4),
        "repair": stats["repair"], "exam_created": exam_created,
        "audio_uploaded": a_ok,
        "images_uploaded": i_ok, "image_model": args.img_model,
        "image_cost_usd": round(i_cost, 4), "image_credits": i_credits,
        "total_llm_cost_usd": round(stats["llm_cost"], 4),
    }
    (base_dir / "extra" / "run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    final_summary(stats)
    print(f"\nReport: {base_dir / 'extra' / 'run_report.json'}")


if __name__ == "__main__":
    main()
