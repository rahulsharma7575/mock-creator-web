"""
build_hooks.py - generates pb_hooks/main.pb.js for PocketBase v0.39.10.

WHY A GENERATOR: PocketBase JSVM (0.39.x) source-serializes router handlers and
re-evaluates them per request in a fresh VM. File-local functions, closures and
top-level variables are NOT visible inside handlers. The only way to share code
between handlers is to physically inline it. This script embeds the shared
snippets (collection ensure + seeds + api key check) into every handler body.

Verified against the real v0.39.10 binary (see scratch tests):
  - $app.importCollections(cols, false)  -> programmatic schema creation (RUNTIME only,
    calling $app in onBootstrap panics with nil pointer)
  - new Record(col) + rec.set(k, v) + $app.save(rec)
  - $app.findRecordsByFilter(col, filter, sort, limit, offset)   # sort FIRST
  - $app.findFirstRecordByData(col, field, value)                # throws when missing
  - $app.countRecords(col)
  - $security.sha256(str) -> hex string
  - e.bindBody() is BROKEN (json: Unmarshal(non-pointer map)) -> use headers + query params
  - middlewares: single function as 4th routerAdd arg, NOT array
  - e.request.header.get(name), e.request.url.query(), e.request.pathValue("id")
"""

import json
import textwrap


def js_str(s):
    """Safe JS string literal: JSON-escaped (ASCII-only, so the generated file
    stays byte-level ASCII-clean for Goja). NEVER interpolate raw user/help
    text into generated JS - a stray double quote kills the whole hooks file."""
    return json.dumps(str(s))

COLLECTION_IDS = {
    "mock_clients": "c_mock_clients",
    "mock_config": "c_mock_config",
    "mock_config_meta": "c_mock_config_meta",
    "mock_models": "c_mock_models",
    "mock_jobs": "c_mock_jobs",
    "dryrun": "c_dryrun",
    "fullrun": "c_fullrun",
    "mock_cache": "c_mock_cache",
    "audio_pool": "c_audio_pool",
}


def f(name, ftype, **kw):
    out = {"id": "f_%s" % name, "name": name, "type": ftype,
           "required": False, "presentable": False, "hidden": False,
           "primaryKey": False}
    out.update(kw)
    return out


def collection(name, fields, options=None):
    return {
        "id": COLLECTION_IDS[name],
        "name": name,
        "type": "base",
        "system": False,
        "fields": fields,
        "indexes": [],
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "options": options or {},
    }


SCHEMA = [
    collection("mock_clients", [
        f("name", "text", required=True, max=100),
        f("api_key_hash", "text", required=True, max=100),
        f("active", "bool"),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("mock_config", [
        f("client", "relation", required=True,
          collectionId=COLLECTION_IDS["mock_clients"], maxSelect=1, minSelect=0),
        f("name", "text", max=100),
        f("llm_author_model", "text", max=200),
        f("author_provider", "select", values=["gemini", "openrouter"], maxSelect=1),
        f("gemini_model", "text", max=200),
        f("llm_proofread_model", "text", max=200),
        f("llm_repair_model", "text", max=200),
        f("image_primary", "text", max=200),
        f("image_fallback", "text", max=200),
        f("image_style_prompt", "text", max=2000),
        f("tts_model", "text", max=200),
        f("tts_fallback_model", "text", max=200),
        f("tts_fallback_voice", "text", max=200),
        f("tts_voices", "json", maxSize=65536),
        f("tts_male_voice", "text", max=200),
        f("tts_female_voice", "text", max=200),
        f("tts_fallback_male_voice", "text", max=200),
        f("tts_fallback_female_voice", "text", max=200),
        f("pdf_parser", "select", values=["auto", "local", "upstage", "mistral"], maxSelect=1),
        f("upscale_pdf_images", "bool"),
        f("question_count", "number", min=1, max=200),
        f("reading_count", "number", min=0, max=50),
        f("image_count", "number", min=0, max=40),
        f("image_count_min", "number", min=0, max=40),
        f("image_count_max", "number", min=0, max=40),
        f("listening_picture_count", "number", min=0, max=15),
        f("difficulty_profile", "select", values=["creative+difficult", "creative+medium", "hard"], maxSelect=1),
        f("marks_per_question", "number", min=1, max=20),
        f("max_tokens", "number", min=0, max=100000),
        f("temperature", "number", min=0, max=2),
        f("timeout_s", "number", min=10, max=3600),
        f("retries", "number", min=0, max=20),
        f("img_retries", "number", min=0, max=20),
        f("push_pb_base", "text", max=300),
        f("push_pb_email", "text", max=200),
        f("push_pb_pass", "text", max=200),
        f("push_subject_id", "text", max=100),
        f("push_exam_type", "text", max=20),
        f("push_exam_status", "text", max=20),
        f("push_enabled", "bool"),
        f("dedup_enabled", "bool"),
        f("dedup_sets", "number", min=0, max=20),
        f("audio_gap_ms", "number", min=0, max=5000),
        f("sample_rate", "number", min=8000, max=48000),
        f("tts_speed", "number", min=0.5, max=2.0),
        f("tts_natural_pacing", "bool"),
        f("tts_polish", "bool"),
        f("tts_atempo_models", "text", max=500),
        f("tts_male_speed", "number", min=0, max=2.0),
        f("tts_female_speed", "number", min=0, max=2.0),
        f("tts_fallback_male_speed", "number", min=0, max=2.0),
        f("tts_fallback_female_speed", "number", min=0, max=2.0),
        f("listening_audio_count", "number", min=0, max=10),
        f("audio_workers", "number", min=1, max=16),
        f("prompts_json", "json", maxSize=2097152),
        f("active", "bool"),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("mock_config_meta", [
        f("field", "text", required=True, max=100),
        f("label", "text", max=200),
        f("ftype", "select", values=["text", "number", "select", "bool", "json"], maxSelect=1),
        f("options", "json", maxSize=65536),
        f("group", "text", max=100),
        f("help", "text", max=1000),
        f("order", "number", min=0, max=1000),
        f("created", "autodate", onCreate=True),
    ]),
    collection("mock_models", [
        f("kind", "select", values=["llm", "tts", "image"], maxSelect=1, required=True),
        f("model", "text", required=True, max=200),
        f("display", "text", max=200),
        f("notes", "text", max=1000),
        f("created", "autodate", onCreate=True),
    ]),
    collection("mock_cache", [
        f("key", "text", required=True, max=100),
        f("value", "json", maxSize=1048576),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("audio_pool", [
        {"id": "f_file", "name": "file", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 1, "maxSize": 52428800,
                     "mimeTypes": ["application/json", "text/plain", "application/octet-stream",
                                   "application/gzip", "application/x-gzip"]}},
        f("count", "number", min=0, max=2000000),
        f("active", "bool"),
        f("note", "text", max=500),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("mock_jobs", [
        f("client", "relation", required=True,
          collectionId=COLLECTION_IDS["mock_clients"], maxSelect=1, minSelect=0),
        f("status", "select", values=["queued", "running", "done", "failed"], maxSelect=1),
        f("kind", "select", values=["full", "dry_questions", "dry_images", "dry_audio", "pdf_test"], maxSelect=1),
        f("gen_type", "number", min=1, max=3),
        {"id": "f_pdf", "name": "pdf", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 1, "maxSize": 10485760,
                     "mimeTypes": ["application/pdf", "application/x-pdf"]}},
        f("count", "number", min=1, max=200),
        f("difficulty", "select", values=["creative+difficult", "creative+medium", "hard", ""], maxSelect=1),
        f("overrides", "json", maxSize=65536),
        f("log", "editor"),
        f("report", "json", maxSize=2097152),
        f("summary", "editor"),
        f("pushed", "bool"),
        f("error", "text", max=2000),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("dryrun", [
        f("client", "relation",
          collectionId=COLLECTION_IDS["mock_clients"], maxSelect=1, minSelect=0),
        f("kind", "select", values=["dry_questions", "dry_images", "dry_audio"], maxSelect=1),
        f("status", "select", values=["done", "failed"], maxSelect=1),
        f("count", "number", min=1, max=200),
        f("difficulty", "text", max=100),
        f("questions", "json", maxSize=2097152),
        f("report", "json", maxSize=2097152),
        f("log", "editor"),
        {"id": "f_images", "name": "images", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 10, "maxSize": 20971520,
                     "mimeTypes": ["image/webp", "image/png", "image/jpeg"]}},
        {"id": "f_audio", "name": "audio", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 10, "maxSize": 20971520,
                     "mimeTypes": ["audio/mpeg", "audio/mp3", "audio/wav"]}},
        f("summary", "editor"),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
    collection("fullrun", [
        f("client", "relation",
          collectionId=COLLECTION_IDS["mock_clients"], maxSelect=1, minSelect=0),
        f("kind", "text", max=50),
        f("status", "select", values=["done", "failed"], maxSelect=1),
        f("count", "number", min=1, max=200),
        f("difficulty", "text", max=100),
        f("questions", "json", maxSize=2097152),
        f("report", "json", maxSize=2097152),
        f("log", "editor"),
        {"id": "f_images", "name": "images", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 20, "maxSize": 20971520,
                     "mimeTypes": ["image/webp", "image/png", "image/jpeg"]}},
        {"id": "f_audio", "name": "audio", "type": "file", "required": False,
         "presentable": False, "hidden": False, "primaryKey": False,
         "options": {"maxSelect": 20, "maxSize": 20971520,
                     "mimeTypes": ["audio/mpeg", "audio/mp3", "audio/wav"]}},
        f("summary", "editor"),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
]

DEFAULT_CONFIG = {
    "name": "default",
    "llm_author_model": "google/gemini-2.5-flash",
    "author_provider": "gemini",
    "gemini_model": "google/gemini-2.5-flash",
    "dedup_enabled": True,
    "dedup_sets": 5,
    "llm_proofread_model": "qwen/qwen3.5-flash-02-23",
    "llm_repair_model": "google/gemini-2.5-flash",
    "image_primary": "z-image",
    "image_fallback": "p-image-ideogram-1k",
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "deepgram/flux-tts:free",
    "tts_fallback_voice": "",
    "tts_male_voice": "",
    "tts_female_voice": "",
    "tts_fallback_male_voice": "",
    "tts_fallback_female_voice": "",
    "pdf_parser": "auto",
    "upscale_pdf_images": True,
    "question_count": 40,
    "reading_count": 20,
    "image_count": 22,
    "image_count_min": 18,
    "image_count_max": 26,
    "difficulty_profile": "creative+medium",   # fixed: balanced - mostly medium, few hard, never easy/very-hard
    "marks_per_question": 1,
    "max_tokens": 32000,
    "temperature": 0.7,
    "timeout_s": 600,
    "retries": 3,
    "img_retries": 4,
    "push_pb_base": "https://ubt.wts.com.np",
    "push_pb_email": "shiva@cld.com.np",
    "push_pb_pass": "",
    "push_subject_id": "illfosglou0e3j6",
    "push_exam_type": "mock",
    "push_exam_status": "draft",
    "push_enabled": True,
    "audio_gap_ms": 300,
    "sample_rate": 44100,
    "tts_speed": 1.0,
    "tts_natural_pacing": False,
    "tts_polish": False,
    "tts_atempo_models": "x-ai/grok-voice-tts-1.0",
    "tts_male_speed": 0.0,
    "tts_female_speed": 0.0,
    "tts_fallback_male_speed": 0.0,
    "tts_fallback_female_speed": 0.0,
    "listening_audio_count": 5,          # resolved by the pipeline (AUDIO_BAND 5-8, pre-made pool)
    "listening_picture_count": 5,
    "listening_picture_min": 5,
    "listening_picture_max": 8,
    "reading_image_count": 10,
    "gemini_vision_scan": True,
    "image_style_prompt": "Simple flat vector illustration in the standard Korean EPS-TOPIK test style, VIVID COLOURFUL palette (never black-and-white, never muted), minimal clean line art, plain solid white background, simple everyday scene, centered single main subject, clear silhouette, no gradients, no photorealism, do NOT write the text 'EPS-TOPIK' or any exam title/logo in the image, small incidental text is allowed only when the scene naturally requires it (e.g. storefront sign), no watermark, no border",
    "audio_workers": 4,
    "prompts_json": {},
    "active": True,
}

META_FIELDS = [
    ("gemini_model", "Gemini author model", "text", "LLM", "PRIMARY author via the direct Google Gemini API (needs GEMINI_API_KEY in the container). Any official Gemini model name works — the google/ prefix is stripped automatically. Default google/gemini-2.5-flash. Verify it with the check button / Verify on this card."),
    ("llm_author_model", "OpenRouter fallback author", "text", "LLM", "OpenRouter model used ONLY when the Gemini author fails, has no key, or quota is exceeded"),
    ("llm_author_model", "OpenRouter fallback author", "text", "LLM", "OpenRouter model used ONLY when the Gemini author fails or quota is exceeded"),
    ("llm_proofread_model", "Proofread LLM", "text", "LLM", "OpenRouter model for proofreading (always via OpenRouter)"),
    ("llm_repair_model", "Repair LLM", "text", "LLM", "OpenRouter model for repair pass"),
    ("dedup_enabled", "Check duplicates against previous mockups", "bool", "LLM", "Compares new questions against the last N mock exams in the end-user app and steers the author away from repeats"),
    ("dedup_sets", "Check last N mockups", "number", "LLM", "How many of the latest mock exams to check for duplicates (default 5)"),
    ("image_primary", "Image provider", "select", "Images", "fal-ai (Fal.ai) | z-image (Magnific) | black-forest-labs/flux.2-klein-4b (OpenRouter)", ["fal-ai/z-image/turbo", "z-image", "black-forest-labs/flux.2-klein-4b"]),
    ("image_style_prompt", "Image style prompt", "text", "Images", "Style instruction wrapped around every generated image (flat colourful EPS-TOPIK vector style by default)."),
    ("image_fallback", "Fallback image model", "text", "Images", "Deprecated - providers fall back automatically, keep empty"),
    ("image_count", "Image questions target", "number", "Images", "How many questions carry a picture, spread randomly across reading AND listening. Applies to RANDOM generation only - Paper PDF mode follows the paper."),
    ("listening_picture_count", "Picture listening questions", "number", "Images", "How many LISTENING questions use 4 separate photos as options (audio plays, student taps the matching photo). Photos 1-4 appear in a 2x2 grid with the number overlaid. Flat colourful EPS-TOPIK style."),
    ("image_count_min", "Image questions min", "number", "Images", "Minimum (18)"),
    ("image_count_max", "Image questions max", "number", "Images", "Maximum (26)"),
    ("tts_model", "TTS model", "select", "Audio", "Model that reads the listening scripts aloud. Options load from the models API.", ["fish-audio/s2.1-pro-free:free", "microsoft/mai-voice-2-flash", "x-ai/grok-voice-tts-1.0"]),
    ("tts_male_voice", "Male listening voice", "text", "Audio", "Voice for the male speaker (V1) in listening dialogues. Leave empty to auto-pick per TTS model (fish-audio free male / MAI ko-KR-InJoon). Use the speaker icon to hear a sample."),
    ("tts_female_voice", "Female listening voice", "text", "Audio", "Voice for the female speaker (V2) in listening dialogues. Leave empty to auto-pick per TTS model (fish-audio free female / MAI ko-KR-Haena). Use the speaker icon to hear a sample."),
    ("tts_fallback_model", "Fallback TTS model", "select", "Audio", "Used only if the primary TTS model fails or times out. Default: Deepgram Flux TTS (free).", ["deepgram/flux-tts:free", "microsoft/mai-voice-2-flash", "fish-audio/s2.1-pro-free:free", "x-ai/grok-voice-tts-1.0"]),
    ("tts_fallback_male_voice", "Fallback male listening voice", "text", "Audio", "Voice for the male speaker (V1) when the run falls back to the fallback TTS model. Leave empty to auto-pick per fallback model."),
    ("tts_fallback_female_voice", "Fallback female listening voice", "text", "Audio", "Voice for the female speaker (V2) when the run falls back to the fallback TTS model. Leave empty to auto-pick per fallback model."),
    ("max_tokens", "Max tokens", "number", "Advanced", "LLM generation cap"),
    ("temperature", "Temperature", "number", "Advanced", "LLM sampling temp"),
    ("timeout_s", "Timeout (s)", "number", "Advanced", "LLM call timeout"),
    ("retries", "LLM retries", "number", "Advanced", "Retry count"),
    ("img_retries", "Image retries", "number", "Advanced", "Per-model image retries"),
    ("push_pb_base", "Push base URL", "text", "Push", "Client PocketBase base URL"),
    ("push_pb_email", "Push email", "text", "Push", "Client PB admin email"),
    ("push_pb_pass", "Push password", "text", "Push", "Client PB admin password"),
    ("push_subject_id", "Subject id", "text", "Push", "Subject record id on client PB"),
    ("push_exam_type", "Exam type on push", "text", "Push", "exam_type for the auto-created exam (mock/ubt/practice/official)"),
    ("push_exam_status", "Exam status on push", "select", "Push", "draft = review-then-publish | published = immediate", ["draft", "published"]),
    ("push_enabled", "Upload to teacher app", "bool", "Push", "On = exams are uploaded to the end-user app after generation | Off = generated locally only, nothing is uploaded"),
    ("pdf_parser", "PDF parser", "select", "PDF", "Auto = PyMuPDF first, cloud OCR when scanned/garbled | Local = PyMuPDF only | Upstage = cloud OCR first | Mistral = OpenRouter vision OCR first. Selected parser runs first; on failure it falls to the next online parser, then local.", ["auto", "local", "upstage", "mistral"]),
    ("upscale_pdf_images", "Upscale extracted paper images", "bool", "PDF", "Every image extracted from the PDF is upscaled via fal-ai/recraft/upscale/crisp before upload (bills FAL credits); on failure the raw extracted image is used"),
    ("audio_gap_ms", "Gap between clips (ms)", "number", "Audio", "Pause between sentences inside a clip. 300-500 ms sounds natural; lower feels rushed."),
    ("sample_rate", "Sample rate (Hz)", "number", "Audio", "MUST match the TTS model: 44100 for fish-audio / grok-voice, 24000 for mai-voice. Wrong rate makes audio play too fast or slow."),
    ("tts_speed", "Speech speed", "number", "Audio", "1.0 = normal · 0.8 = slower · 1.2 = faster · range 0.5-2.0. Models that don't support speed ignore it."),
    ("tts_natural_pacing", "Natural pacing", "bool", "Audio", "Relaxed human rhythm: speed 0.92 when speed is untouched, gaps >= 450ms, extra pause after ? and ! turns."),
    ("tts_polish", "Audio polish", "bool", "Audio", "Post-pass on merged clips: loudness normalization + rumble filter + click-free fades."),
    ("tts_male_speed", "Male voice speed", "number", "Audio", "0 = follow the global speech speed · 0.5-2.0 = speed for the male voice only."),
    ("tts_female_speed", "Female voice speed", "number", "Audio", "0 = follow the global speech speed · 0.5-2.0 = speed for the female voice only."),
    ("tts_fallback_male_speed", "Fallback male speed", "number", "Audio", "Speed for the male voice when the fallback TTS model is used (0 = follow global)."),
    ("tts_fallback_female_speed", "Fallback female speed", "number", "Audio", "Speed for the female voice when the fallback TTS model is used (0 = follow global)."),
    ("gemini_vision_scan", "Scan PDF pages with Gemini", "bool", "PDF", "Paper mode only: feed the rendered question pages to the (multimodal) Gemini author so it visually maps images to questions and fills missing 4-photo grids coherently. Requires GEMINI_API_KEY."),
    ("listening_picture_min", "Picture listening (min)", "number", "Advanced", "Lower bound of the random picture-listening band (EPS-TOPIK 5-8)."),
    ("listening_picture_max", "Picture listening (max)", "number", "Advanced", "Upper bound of the random picture-listening band (EPS-TOPIK 5-8)."),
    ("reading_image_count", "Reading image questions", "number", "Advanced", "How many of the 20 reading questions carry a single image by default (paper uses its own; random/book generate online)."),
    ("active", "Config enabled", "bool", "General", "Use this config"),
]

MODEL_SEEDS = [
    ("llm", "google/gemini-2.5-flash", "Gemini 2.5 Flash", "fast author/proofread"),
    ("llm", "google/gemini-3.5-flash", "Gemini 3.5 Flash", "repair pass"),
    ("llm", "qwen/qwen3.5-flash-02-23", "Qwen 3.5 Flash", "proofread"),
    ("llm", "claude-sonnet-4.5", "Claude Sonnet 4.5", "alt author"),
    ("llm", "deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "alt repair"),
    ("tts", "fish-audio/s2.1-pro-free:free", "Fish Audio S2.1 Pro (free)", "default TTS - 44100 Hz"),
    ("tts", "microsoft/mai-voice-2-flash", "MAI Voice 2 Flash", "fallback TTS - 24000 Hz"),
    ("tts", "x-ai/grok-voice-tts-1.0", "Grok Voice TTS", "alt TTS - 44100 Hz"),
    ("tts", "google/gemini-3.1-flash-tts-preview", "Gemini Flash TTS", "alt TTS - 24000 Hz"),
    ("tts", "hexgrad/kokoro-82m", "Kokoro 82M", "alt TTS - open weights"),
    ("tts", "mistralai/voxtral-mini-tts-2603", "Voxtral Mini TTS", "alt TTS"),
    ("tts", "deepgram/flux-tts:free", "Deepgram Flux TTS (free)", "free fallback TTS"),
    
    ("image", "z-image", "z-image", "primary image gen (5 credits/img)"),
    ("image", "p-image-ideogram-1k", "P-Image Ideogram 1K", "fallback (may 404)"),
]


# ---------------------------------------------------------------------------
# shared JS snippets (inlined into EVERY handler by the generator)
# ---------------------------------------------------------------------------

ENSURE_FN = """
function ensureCollections() {
  var need = %s
  var missing = []
  for (var i = 0; i < need.length; i++) {
    try { $app.findCollectionByNameOrId(need[i]) } catch (err) { missing.push(need[i]) }
  }
  if (missing.length > 0) {
    $app.importCollections(%s, false)
  }
  if ($app.countRecords("mock_config_meta") === 0) {
    var meta = $app.findCollectionByNameOrId("mock_config_meta")
    %s
  }
  if ($app.countRecords("mock_models") === 0) {
    var models = $app.findCollectionByNameOrId("mock_models")
    %s
  }
  try {
    // keep the mock_config / mock_jobs schemas in sync with the generator:
    // importCollections(SCHEMA, false) is idempotent and applies BOTH missing fields
    // AND changed definitions (e.g. select field values) on existing installs.
    // NOTE: fields.items() does NOT exist in the 0.39 JSVM (TypeError) - use
    // fields.getByName() for detection and importCollections to apply changes.
    var cfgCol4 = $app.findCollectionByNameOrId("mock_config")
    var needFields = ["pdf_parser", "upscale_pdf_images", "tts_male_voice", "tts_female_voice",
                      "tts_fallback_male_voice", "tts_fallback_female_voice", "tts_speed",
                      "tts_natural_pacing", "tts_polish", "tts_atempo_models",
                      "tts_male_speed", "tts_female_speed", "tts_fallback_male_speed",
                      "tts_fallback_female_speed", "listening_audio_count", "listening_picture_count",
                      "image_style_prompt", "push_enabled"]
    var missingField = false
    for (var nfi = 0; nfi < needFields.length; nfi++) {
      var hasIt = false
      try { hasIt = !!cfgCol4.fields.getByName(needFields[nfi]) } catch (err) { hasIt = false }
      if (!hasIt) missingField = true
    }
    var jobsCol = $app.findCollectionByNameOrId("mock_jobs")
    var jobsNeed = ["gen_type", "pdf"]
    for (var nji = 0; nji < jobsNeed.length; nji++) {
      var hasJ = false
      try { hasJ = !!jobsCol.fields.getByName(jobsNeed[nji]) } catch (err) { hasJ = false }
      if (!hasJ) missingField = true
    }
    var pdfFieldOk = false
    try {
      var pf = cfgCol4.fields.getByName("pdf_parser")
      var pfv = (pf && ((pf.options && pf.options.values) || pf.values)) || []
      pdfFieldOk = pfv.indexOf("upstage") >= 0 && pfv.indexOf("mistral") >= 0
    } catch (errF) { pdfFieldOk = false }
    // always import when any field is missing OR the pdf_parser select values are stale
    if (missingField || !pdfFieldOk) {
      $app.importCollections(%s, false)
    }
  } catch (errH) { try { $app.logger().info("mig-cfg4: " + String(errH)) } catch (errL) {} }
}
""" % (
    json.dumps(list(COLLECTION_IDS.keys())),
    json.dumps(SCHEMA),
    "".join(
        "var m{0} = new Record(meta); m{0}.set(\"field\", {1}); m{0}.set(\"label\", {2}); m{0}.set(\"ftype\", {3}); m{0}.set(\"group\", {4}); m{0}.set(\"help\", {5}); {6}$app.save(m{0});\n".format(
            i, js_str(field), js_str(label), js_str(ftype), js_str(group), js_str(help_text),
            ("m{0}.set(\"options\", {1}); ".format(i, json.dumps(opts[0])) if opts else ""),
        )
        for i, (field, label, ftype, group, help_text, *opts) in enumerate(META_FIELDS)
    ) + """
  // ---- migrations (idempotent, cheap after first run) ----
  try {
    var mImg = $app.findFirstRecordByData("mock_config_meta", "field", "image_primary")
    var cur = mImg.get("options") || []
    if (typeof cur === "string") { try { cur = JSON.parse(cur) } catch (errP) { cur = [] } }
    if (cur.indexOf("black-forest-labs/flux.2-klein-4b") === -1) {
      mImg.set("label", "Image provider")
      mImg.set("ftype", "select")
      mImg.set("options", ["fal-ai/z-image/turbo", "z-image", "black-forest-labs/flux.2-klein-4b"])
      mImg.set("help", "fal-ai (Fal.ai) | z-image (Magnific) | black-forest-labs/flux.2-klein-4b (OpenRouter)")
      $app.save(mImg)
    }
    var oldCfgs = $app.findRecordsByFilter("mock_config", "image_primary = 'nano-banana'", "", 200, 0)
    for (var cI = 0; cI < oldCfgs.length; cI++) {
      oldCfgs[cI].set("image_primary", "black-forest-labs/flux.2-klein-4b")
      $app.save(oldCfgs[cI])
    }
  } catch (errM) {}
  try {
    var runCols = ["dryrun", "fullrun"]
    for (var rc = 0; rc < runCols.length; rc++) {
      var col = $app.findCollectionByNameOrId(runCols[rc])
      var hasImg = false, hasAud = false
      for (var fi = 0; fi < col.fields.length; fi++) {
        var fld = col.fields[fi]
        if (fld.name === "images") hasImg = true
        if (fld.name === "audio") hasAud = true
      }
      if (!hasImg || !hasAud) {
        if (!hasImg) col.fields.add({"name": "images", "type": "file", "required": false, "options": {"maxSelect": 20, "maxSize": 20971520, "mimeTypes": ["image/webp", "image/png", "image/jpeg"]}})
        if (!hasAud) col.fields.add({"name": "audio", "type": "file", "required": false, "options": {"maxSelect": 20, "maxSize": 20971520, "mimeTypes": ["audio/mpeg", "audio/mp3", "audio/wav"]}})
        $app.save(col)
      } else {
        // upgrade single-file fields (maxSelect defaulted to 1 on old installs) to multi-file
        var changed = false
        for (var fi2 = 0; fi2 < col.fields.length; fi2++) {
          var fld2 = col.fields[fi2]
          if (fld2.name === "images" || fld2.name === "audio") {
            var opts = fld2.options || {}
            var max = Number(opts.maxSelect || 1)
            if (max < 10) { opts.maxSelect = 20; changed = true }
            if (!opts.mimeTypes || opts.mimeTypes.length === 0) {
              opts.mimeTypes = fld2.name === "images" ? ["image/webp", "image/png", "image/jpeg"] : ["audio/mpeg", "audio/mp3", "audio/wav"]
              changed = true
            }
          }
        }
        if (changed) $app.save(col)
      }
    }
  } catch (errF) {}
  try {
    var cfgCol = $app.findCollectionByNameOrId("mock_config")
    var hasVoices = false
    for (var fi3 = 0; fi3 < cfgCol.fields.length; fi3++) { if (cfgCol.fields[fi3].name === "tts_voices") hasVoices = true }
    if (!hasVoices) {
      cfgCol.fields.add({"name": "tts_voices", "type": "json", "required": false, "maxSize": 65536})
      $app.save(cfgCol)
    }
  } catch (errV) {}
  try {
    var cfgCol3 = $app.findCollectionByNameOrId("mock_config")
    var hasDup = false, hasSets = false
    for (var fi5 = 0; fi5 < cfgCol3.fields.length; fi5++) {
      var nm2 = cfgCol3.fields[fi5].name
      if (nm2 === "dedup_enabled") hasDup = true
      if (nm2 === "dedup_sets") hasSets = true
    }
    var changed3 = false
    if (!hasDup) { cfgCol3.fields.add({"name": "dedup_enabled", "type": "bool", "required": false}); changed3 = true }
    if (!hasSets) { cfgCol3.fields.add({"name": "dedup_sets", "type": "number", "required": false, "min": 0, "max": 20}); changed3 = true }
    if (changed3) $app.save(cfgCol3)
  } catch (errD) {}
  try {
    var cfgCol2 = $app.findCollectionByNameOrId("mock_config")
    var hasProv = false, hasGem = false
    for (var fi4 = 0; fi4 < cfgCol2.fields.length; fi4++) {
      var nm = cfgCol2.fields[fi4].name
      if (nm === "author_provider") hasProv = true
      if (nm === "gemini_model") hasGem = true
    }
    var changed2 = false
    if (!hasProv) { cfgCol2.fields.add({"name": "author_provider", "type": "select", "required": false, "values": ["gemini", "openrouter"], "maxSelect": 1}); changed2 = true }
    if (!hasGem) { cfgCol2.fields.add({"name": "gemini_model", "type": "text", "required": false, "max": 200}); changed2 = true }
    if (changed2) $app.save(cfgCol2)
  } catch (errG) {}
  try {
    // author_provider dropdown was removed - purge any stale meta row so the UI stops showing it
    var mProv = $app.findFirstRecordByData("mock_config_meta", "field", "author_provider")
    if (mProv) $app.delete(mProv)
  } catch (errP) {}
  try {
    var mGem = $app.findFirstRecordByData("mock_config_meta", "field", "gemini_model")
    mGem.set("label", "Gemini author model")
    mGem.set("ftype", "text")
    mGem.set("help", "PRIMARY author via the direct Google Gemini API (needs GEMINI_API_KEY in the container). Any official Gemini model name works - the google/ prefix is stripped automatically. Default google/gemini-2.5-flash. Use the check button / the card's Verify to probe the model live.")
    mGem.set("group", "LLM")
    $app.save(mGem)
  } catch (errQ) {}
  try {
    var mAuth = $app.findFirstRecordByData("mock_config_meta", "field", "llm_author_model")
    mAuth.set("label", "OpenRouter fallback author")
    mAuth.set("help", "OpenRouter model used ONLY when the Gemini author fails or quota is exceeded")
    $app.save(mAuth)
  } catch (errR) {}
  try {
    var pdfMetaDefs = [
      ["pdf_parser", "PDF parser", "select", "PDF", "Auto = PyMuPDF first, cloud OCR when scanned/garbled | Local = PyMuPDF only | Upstage = cloud OCR first | Mistral = OpenRouter vision OCR first. Selected parser runs first; on failure it falls to the next online parser, then local.", ["auto", "local", "upstage", "mistral"]],
      ["upscale_pdf_images", "Upscale extracted paper images", "bool", "PDF", "Every image extracted from the PDF is upscaled via fal-ai/recraft/upscale/crisp before upload (bills FAL credits); on failure the raw extracted image is used", null],
      ["tts_male_voice", "Male listening voice", "text", "Audio", "Voice for the male speaker (V1) in listening dialogues. Leave empty to auto-pick per TTS model (fish-audio free male / MAI ko-KR-InJoon). Use the speaker icon to hear a sample.", null],
      ["tts_female_voice", "Female listening voice", "text", "Audio", "Voice for the female speaker (V2) in listening dialogues. Leave empty to auto-pick per TTS model (fish-audio free female / MAI ko-KR-Haena). Use the speaker icon to hear a sample.", null],
      ["tts_fallback_male_voice", "Fallback male listening voice", "text", "Audio", "Voice for the male speaker (V1) when the run falls back to the fallback TTS model. Leave empty to auto-pick per fallback model.", null],
      ["tts_fallback_female_voice", "Fallback female listening voice", "text", "Audio", "Voice for the female speaker (V2) when the run falls back to the fallback TTS model. Leave empty to auto-pick per fallback model.", null],
      ["push_enabled", "Upload to teacher app", "bool", "Push", "On = exams are uploaded to the end-user app after generation | Off = generated locally only, nothing is uploaded", null],
      ["tts_speed", "Speech speed", "number", "Audio", "1.0 = normal · 0.8 = slower · 1.2 = faster · range 0.5-2.0. Models that don't support speed ignore it.", null],
      ["tts_natural_pacing", "Natural pacing", "bool", "Audio", "Relaxed human rhythm: speed 0.92 when speed is untouched, gaps >= 450ms, extra pause after ? and ! turns.", null],
      ["tts_polish", "Audio polish", "bool", "Audio", "Post-pass on merged clips: loudness normalization + rumble filter + click-free fades.", null],
      ["tts_male_speed", "Male voice speed", "number", "Audio", "0 = follow the global speech speed · 0.5-2.0 = speed for the male voice only.", null],
      ["tts_female_speed", "Female voice speed", "number", "Audio", "0 = follow the global speech speed · 0.5-2.0 = speed for the female voice only.", null],
      ["tts_fallback_male_speed", "Fallback male speed", "number", "Audio", "Speed for the male voice when the fallback TTS model is used (0 = follow global).", null],
      ["tts_fallback_female_speed", "Fallback female speed", "number", "Audio", "Speed for the female voice when the fallback TTS model is used (0 = follow global).", null],
      ["listening_picture_count", "Picture listening questions", "number", "Images", "How many LISTENING questions use 4 separate photos as options (audio plays, student taps the matching photo). Photos 1-4 appear in a 2x2 grid with the number overlaid. Flat colourful EPS-TOPIK style.", null],
      ["image_style_prompt", "Image style prompt", "text", "Images", "Style instruction wrapped around every generated image (flat colourful EPS-TOPIK vector style by default).", null],
      ["image_count", "Image questions target", "number", "Images", "How many questions carry a picture, spread randomly across reading AND listening. Applies to RANDOM generation only - Paper PDF mode follows the paper.", null]
    ]
    var mColM = $app.findCollectionByNameOrId("mock_config_meta")
    for (var pmi = 0; pmi < pdfMetaDefs.length; pmi++) {
      var pm = pdfMetaDefs[pmi]
      var mRec = null
      try { mRec = $app.findFirstRecordByData("mock_config_meta", "field", pm[0]) } catch (errM2) {}
      if (!mRec) { mRec = new Record(mColM); mRec.set("field", pm[0]) }
      mRec.set("label", pm[1])
      mRec.set("ftype", pm[2])
      mRec.set("group", pm[3])
      mRec.set("help", pm[4])
      if (pm[5]) mRec.set("options", pm[5])
      $app.save(mRec)
    }
  } catch (errS) {}
  try {
    var mT = $app.findFirstRecordByData("mock_config_meta", "field", "tts_model")
    if (mT.getString("ftype") !== "select") { mT.set("ftype", "select"); mT.set("options", ["fish-audio/s2.1-pro-free:free", "microsoft/mai-voice-2-flash", "x-ai/grok-voice-tts-1.0"]); mT.set("label", "TTS model"); mT.set("help", "Model that reads the listening scripts aloud. Options load from the models API."); $app.save(mT) }
  } catch (errT) {}
  try {
    // upsert missing default TTS model seeds (existing installs never got them)
    var mCol = $app.findCollectionByNameOrId("mock_models")
    var seedDefs = [["tts", "fish-audio/s2.1-pro-free:free", "Fish Audio S2.1 Pro (free)", "default TTS - 44100 Hz"], ["tts", "microsoft/mai-voice-2-flash", "MAI Voice 2 Flash", "fallback TTS - 24000 Hz"], ["tts", "x-ai/grok-voice-tts-1.0", "Grok Voice TTS", "alt TTS - 44100 Hz"], ["tts", "google/gemini-3.1-flash-tts-preview", "Gemini Flash TTS", "alt TTS - 24000 Hz"], ["tts", "hexgrad/kokoro-82m", "Kokoro 82M", "alt TTS - open weights"], ["tts", "mistralai/voxtral-mini-tts-2603", "Voxtral Mini TTS", "alt TTS"], ["tts", "deepgram/flux-tts:free", "Deepgram Flux TTS (free)", "free fallback TTS"]]
    for (var si = 0; si < seedDefs.length; si++) {
      var exists = false
      try { exists = !!$app.findFirstRecordByData("mock_models", "model", seedDefs[si][1]) } catch (errS) {}
      if (!exists) {
        var mRec = new Record(mCol)
        mRec.set("kind", seedDefs[si][0]); mRec.set("model", seedDefs[si][1]); mRec.set("display", seedDefs[si][2]); mRec.set("notes", seedDefs[si][3])
        $app.save(mRec)
      }
    }
  } catch (errU) {}
  try {
    // TTS fallback default migration: configs still on the old default pair
    // (MAI flash + ko-KR-Haena, no longer supported on OpenRouter) -> Deepgram Flux (free).
    var oldFb = $app.findRecordsByFilter("mock_config", "tts_fallback_model = 'microsoft/mai-voice-2-flash' && tts_fallback_voice = 'ko-KR-Haena:MAI-Voice-2'", "", 200, 0)
    for (var fbi = 0; fbi < oldFb.length; fbi++) {
      oldFb[fbi].set("tts_fallback_model", "deepgram/flux-tts:free")
      oldFb[fbi].set("tts_fallback_voice", "")
      $app.save(oldFb[fbi])
    }
  } catch (errFb) {}
  try {
    // Removed settings: blank/audio-question fields + force-speed models are gone from
    // the GUI (fixed in the pipeline / the audio stack). Remove their meta rows
    // so the config editor stops rendering them (fields stay in the schema).
    var blankMetaFields = ["listening_blank_count", "listening_blank_min", "listening_blank_max",
                           "listening_audio_count", "listening_audio_min", "listening_audio_max",
                           "tts_atempo_models"]
    for (var bmi = 0; bmi < blankMetaFields.length; bmi++) {
      var bm = null
      try { bm = $app.findFirstRecordByData("mock_config_meta", "field", blankMetaFields[bmi]) } catch (errBm) {}
      if (bm) $app.delete(bm)
    }
  } catch (errB) {}
  try {
    // full removal: migrate blank_pool -> audio_pool once, then delete blank_pool collection
    var oldPoolCol = null; try { oldPoolCol = $app.findCollectionByNameOrId("blank_pool") } catch(errOP) {}
    if (oldPoolCol) {
      var audioCol = null; try { audioCol = $app.findCollectionByNameOrId("audio_pool") } catch(errA) {}
      if (audioCol) {
        try { if ($app.countRecords("audio_pool") === 0 && $app.countRecords("blank_pool") > 0) {
          var recs = $app.findRecordsByFilter("blank_pool", "", "-updated", 1, 0)
          if (recs.length) {
            var r = recs[0]; var nr = new Record(audioCol)
            try { nr.set("file", r.get("file")) } catch(e) {}
            nr.set("count", r.getInt("count")); nr.set("active", r.getBool("active")); nr.set("note", r.getString("note"))
            $app.save(nr); $app.logger().info("migrated blank_pool record to audio_pool")
          }
        }} catch(errMigPool) {}
        // remove old blank_pool records then collection is left empty; delete via DAO if possible
        try { var delRecs = $app.findRecordsByFilter("blank_pool", "", "", 200, 0); for(var di=0; di<delRecs.length; di++) $app.delete(delRecs[di]); } catch(errDelRecs){}
      }
      // remove listening_blank_count field from mock_config if exists
      try {
        var cfgColR = $app.findCollectionByNameOrId("mock_config")
        var hasBlankF = false; try { hasBlankF = !!cfgColR.fields.getByName("listening_blank_count") } catch(e) {}
        if (hasBlankF) {
          // PocketBase 0.39: remove field via collection fields array
          for(var fi=cfgColR.fields.length-1; fi>=0; fi--) { if(cfgColR.fields[fi].name==="listening_blank_count") cfgColR.fields.remove(fi) }
          $app.save(cfgColR); $app.logger().info("removed deprecated listening_blank_count field")
        }
      } catch(errBlkField){}
    }
  } catch(errMigAll) {}
""",
    "".join(
        "var k{0} = new Record(models); k{0}.set(\"kind\", {1}); k{0}.set(\"model\", {2}); k{0}.set(\"display\", {3}); k{0}.set(\"notes\", {4}); $app.save(k{0});\n".format(
            i, js_str(kind), js_str(model), js_str(display), js_str(notes))
        for i, (kind, model, display, notes) in enumerate(MODEL_SEEDS)
    ),
    json.dumps(SCHEMA),
)

KEY_CHECK_FN = """
function checkKey(apiKey) {
  if (!apiKey || apiKey.length === 0) return { error: "missing X-API-Key header" }
  var hash = $security.sha256(apiKey)
  var client = null
  try { client = $app.findFirstRecordByData("mock_clients", "api_key_hash", hash) } catch (err) {}
  if (!client) return { error: "invalid API key" }
  if (!client.getBool("active")) return { error: "client is disabled" }
  return { client: client }
}
"""

ENSURE_CONFIG_FN = """
function ensureConfig(clientId) {
  var cfg = null
  try { cfg = $app.findFirstRecordByData("mock_config", "client", clientId) } catch (err) {}
  if (!cfg) {
    var col = $app.findCollectionByNameOrId("mock_config")
    cfg = new Record(col)
    cfg.set("client", clientId)
    %s
    $app.save(cfg)
  }
  return cfg
}
""" % "".join(
    "cfg.set(\"{0}\", {1}); ".format(k, json.dumps(v))
    for k, v in DEFAULT_CONFIG.items()
)

HANDLERS = []


def route(name, method, path, middleware, body):
    HANDLERS.append((name, method, path, middleware, body))


# GET /api/creator/health - public - reports whether schema migrations have applied
# (deploy-time early warning: a stale DB shows missing fields here, and opening any
# creator page runs ensureCollections which self-heals)
route(
    "health", "GET", "/api/creator/health", None,
    """
try {
  ensureCollections()
  var version = "26"
  var checkField = function (col, name) {
    try {
      var c = $app.findCollectionByNameOrId(col)
      return !!c.fields.getByName(name)
    } catch (err) { return false }
  }
  var cfgNeed = ["pdf_parser", "upscale_pdf_images", "tts_male_voice", "tts_female_voice",
                 "tts_fallback_male_voice", "tts_fallback_female_voice", "tts_speed",
                 "tts_natural_pacing", "tts_polish", "tts_atempo_models",
                 "image_style_prompt", "push_enabled"]
  var jobsNeed = ["gen_type", "pdf"]
  var missing = []
  for (var i = 0; i < cfgNeed.length; i++) { if (!checkField("mock_config", cfgNeed[i])) missing.push("mock_config." + cfgNeed[i]) }
  for (var j = 0; j < jobsNeed.length; j++) { if (!checkField("mock_jobs", jobsNeed[j])) missing.push("mock_jobs." + jobsNeed[j]) }
  var pdfOk = false
  try {
    var pf = $app.findCollectionByNameOrId("mock_config").fields.getByName("pdf_parser")
    var pv = (pf && ((pf.options && pf.options.values) || pf.values)) || []
    pdfOk = pv.indexOf("upstage") >= 0 && pv.indexOf("mistral") >= 0
  } catch (errF) {}
  if (!pdfOk) missing.push("mock_config.pdf_parser (values)")
  return e.json(200, { ok: missing.length === 0, version: version, migrations_ok: missing.length === 0, missing: missing })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/ping - no auth, no schema - diagnostic: are hooks even loading?
route(
    "ping", "GET", "/api/creator/ping", None,
    """
try {
  return e.json(200, { ok: true, hooks: "loaded" })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/ensure - superuser only, idempotent schema+seeds
route(
    "ensure", "GET", "/api/creator/ensure", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  return e.json(200, { ok: true, collections: %s })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""" % json.dumps(list(COLLECTION_IDS.keys())),
)

# GET /api/creator/verify-model - superuser only - REAL model existence probe
# against the OpenRouter chat completions API (1-token ping). The public
# /api/v1/models catalog EXCLUDES TTS/speech models (only ~400 curated chat
# models), so catalog lookups give false negatives for fish-audio / mai-voice.
# A probe call distinguishes: 200 = exists, 404 / "not a valid model ID" =
# does not exist, 401 = bad key, any other 4xx/5xx = exists (server accepted
# the ID but rejected the request shape - normal for TTS models).
route(
    "verify-model", "GET", "/api/creator/verify-model", "$apis.requireSuperuserAuth()",
    """
try {
  var slug = (e.request.url.query().get("slug") || "").trim()
  if (!slug) return e.json(400, { error: "slug query param required" })
  if (slug.indexOf("fal-ai/") === 0) {
    var fkey = $os.getenv("FAL_KEY") || ""
    if (!fkey) return e.json(200, { exists: null, status: 0, message: "FAL_KEY is not set in the container - fal model checks unavailable" })
    var q = slug.split("/").pop().split(":")[0]
    var fres = $http.send({ url: "https://api.fal.ai/v1/models?q=" + encodeURIComponent(q) + "&limit=50", method: "GET", headers: { "Authorization": "Key " + fkey }, timeout: 30 })
    var found = false
    try { var fj = JSON.parse(fres.raw || "{}"); var list = fj.models || []; for (var i = 0; i < list.length; i++) { if (list[i].endpoint_id === slug) { found = true; break } } } catch (errF) {}
    if (fres.statusCode === 401) return e.json(200, { exists: null, status: 401, message: "FAL_KEY rejected by fal.ai (401)" })
    return e.json(200, { exists: found, status: fres.statusCode || 0, message: found ? "" : "model not found on fal.ai" })
  }
  var key = $os.getenv("OPENROUTER_API_KEY") || ""
  if (!key) return e.json(200, { exists: null, status: 0, message: "OPENROUTER_API_KEY is not set in the container - model checks unavailable" })
  var res = $http.send({
    url: "https://openrouter.ai/api/v1/chat/completions",
    method: "POST",
    headers: { "Authorization": "Bearer " + key, "Content-Type": "application/json" },
    body: JSON.stringify({ model: slug, messages: [{ role: "user", content: "ping" }], max_tokens: 1 }),
    timeout: 60
  })
  var status = res.statusCode || 0
  var msg = ""
  try { var j = JSON.parse(res.raw || "{}"); msg = (j.error && j.error.message) || "" } catch (err) {}
  var invalid = /not a valid model|does not exist|model not found|not found/i.test(msg)
  var exists = null
  if (status === 200) exists = true
  else if (status === 401) exists = null
  else if (status === 404 || invalid) exists = false
  else exists = true
  return e.json(200, { exists: exists, status: status, message: String(msg).slice(0, 200) })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/gemini-status - superuser only - Gemini key validity + models (real API call)
route(
    "gemini-status", "GET", "/api/creator/gemini-status", "$apis.requireSuperuserAuth()",
    """
try {
  var key = $os.getenv("GEMINI_API_KEY") || ""
  if (!key) return e.json(200, { ok: false, valid: false, models: [], message: "GEMINI_API_KEY is not set in the container env" })
  var res = $http.send({ url: "https://generativelanguage.googleapis.com/v1beta/models?key=" + encodeURIComponent(key), method: "GET", timeout: 30 })
  if (res.statusCode !== 200) return e.json(200, { ok: false, valid: false, models: [], message: "Gemini key rejected (HTTP " + (res.statusCode || 0) + ")" })
  var j = {}
  try { j = JSON.parse(res.raw || "{}") } catch (err) {}
  var models = []
  var list = j.models || []
  for (var i = 0; i < list.length; i++) {
    var m = list[i]
    var methods = m.supportedGenerationMethods || []
    var okGen = false
    for (var mm = 0; mm < methods.length; mm++) { if (methods[mm] === "generateContent") okGen = true }
    if (okGen) models.push({ name: String(m.name || "").replace("models/", ""), input_limit: m.inputTokenLimit, output_limit: m.outputTokenLimit })
  }
  return e.json(200, { ok: true, valid: true, models: models, message: "" })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/verify-gemini-model - superuser only - REAL probe of the
# primary author model against the Gemini API (1-token generateContent call).
# Classifies: 200 = valid; "not found/does not exist" = wrong model name;
# anything else (401/400 key, 429 quota, 5xx transient) = unverifiable/warn.
route(
    "verify-gemini-model", "GET", "/api/creator/verify-gemini-model", "$apis.requireSuperuserAuth()",
    """
try {
  var model = (e.request.url.query().get("model") || "").trim()
  if (!model) return e.json(400, { error: "model query param required" })
  var key = $os.getenv("GEMINI_API_KEY") || ""
  if (!key) return e.json(200, { ok: null, status: 0, message: "GEMINI_API_KEY is not set in the container" })
  var apiModel = model.split("/").pop()
  var res = $http.send({
    url: "https://generativelanguage.googleapis.com/v1beta/models/" + encodeURIComponent(apiModel) + ":generateContent?key=" + encodeURIComponent(key),
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ contents: [{ role: "user", parts: [{ text: "ping" }] }], generationConfig: { maxOutputTokens: 1 } }),
    timeout: 40
  })
  var status = res.statusCode || 0
  var msg = ""
  try { var j = JSON.parse(res.raw || "{}"); msg = (j.error && j.error.message) || (j.message) || "" } catch (errJ) {}
  var ok = null
  if (status === 200) ok = true
  else if (/not found|does not exist|not a valid|is not available|invalid model|unsupported model/i.test(msg)) ok = false
  else ok = null
  return e.json(200, { ok: ok, status: status, message: String(msg).slice(0, 220), model: apiModel })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# POST /api/creator/push-status - superuser only - verify end-user app credentials + list mock exams
route(
    "push-status", "POST", "/api/creator/push-status", "$apis.requireSuperuserAuth()",
    """
try {
  var req = {}
  try { req = e.requestInfo().body || {} } catch (err) {}
  var base = String(req.base || "").trim().replace(/\\/+$/, "")
  var email = String(req.email || "").trim()
  var pass = String(req.pass || "")
  if (!pass) { pass = $os.getenv("MOCK_PB_PASS") || "" }
  if (!base) return e.json(200, { ok: true, connected: false, total_exams: 0, url: "", mocks: [], message: "base URL missing" })
  if (!email || !pass) return e.json(200, { ok: true, connected: false, total_exams: 0, url: base, mocks: [], message: "email/password missing - set them in Push or MOCK_PB_PASS" })
  var res = $http.send({
    url: base + "/api/collections/_superusers/auth-with-password",
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identity: email, password: pass }), timeout: 10
  })
  if (res.statusCode !== 200) {
    return e.json(200, { ok: true, connected: false, total_exams: 0, url: base, mocks: [], message: "auth failed (HTTP " + (res.statusCode || 0) + ") - check email/password" })
  }
  var tok = ""
  try { tok = JSON.parse(res.raw || "{}").token || "" } catch (err) {}
  if (!tok) return e.json(200, { ok: true, connected: false, total_exams: 0, url: base, mocks: [], message: "auth returned no token" })
  var ex = $http.send({
    url: base + "/api/collections/exams/records?perPage=200&fields=id,title,status",
    method: "GET", headers: { "Authorization": "Bearer " + tok }, timeout: 15
  })
  var mocks = [], total = 0, published = 0, draft = 0
  try {
    var exj = JSON.parse(ex.raw || "{}")
    var items = exj.items || []
    total = Number(exj.totalItems) || items.length || 0
    for (var i = 0; i < items.length; i++) {
      var it = items[i]
      var t = String(it.title || "")
      var st = String(it.status || "")
      var mm = t.match(/Mock Test ?\\(?(\\d+)\\)?/i)
      var serial = mm ? Number(mm[1]) : -1
      if (st === "published") published++
      else if (st === "draft") draft++
      mocks.push({ title: t, status: st, serial: serial })
    }
    mocks.sort(function (a, b) { return a.serial - b.serial })
  } catch (err) {}
  return e.json(200, { ok: true, connected: true, total_exams: total, published: published, draft: draft, mocks: mocks, url: base, message: "" })
} catch (err) {
  return e.json(200, { ok: true, connected: false, total_exams: 0, url: "", mocks: [], message: "unreachable: " + String(err).slice(0, 80) })
}
""",
)

# GET /api/creator/or-status - superuser only - OpenRouter credit balance (real API call)
route(
    "or-status", "GET", "/api/creator/or-status", "$apis.requireSuperuserAuth()",
    """
try {
  var key = $os.getenv("OPENROUTER_API_KEY") || ""
  if (!key) return e.json(200, { ok: false, message: "OPENROUTER_API_KEY is not set in the container" })
  var res = $http.send({ url: "https://openrouter.ai/api/v1/credits", method: "GET", headers: { "Authorization": "Bearer " + key }, timeout: 30 })
  var j = {}
  try { j = JSON.parse(res.raw || "{}") } catch (err) {}
  var d = j.data || {}
  var total = Number(d.total_credits || 0)
  var used = Number(d.total_usage || 0)
  return e.json(200, {
    ok: res.statusCode === 200,
    total: total,
    used: used,
    remaining: Math.max(0, Math.round((total - used) * 100) / 100),
    status: res.statusCode || 0
  })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/fal-status - superuser only - fal.ai credit balance (real API call)
route(
    "fal-status", "GET", "/api/creator/fal-status", "$apis.requireSuperuserAuth()",
    """
try {
  var key = $os.getenv("FAL_KEY") || ""
  if (!key) return e.json(200, { ok: false, message: "FAL_KEY is not set in the container" })
  var res = $http.send({ url: "https://api.fal.ai/v1/account/billing?expand=credits", method: "GET", headers: { "Authorization": "Key " + key }, timeout: 30 })
  var j = {}
  try { j = JSON.parse(res.raw || "{}") } catch (err) {}
  var creds = j.credits || {}
  return e.json(200, {
    ok: res.statusCode === 200,
    balance: creds.current_balance,
    currency: creds.currency || "USD",
    username: j.username || "",
    status: res.statusCode || 0
  })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/magnific-status - superuser only - Magnific key state (credits are
# not exposed via the Magnific REST API - only through OAuth/MCP, which the container lacks)
route(
    "magnific-status", "GET", "/api/creator/magnific-status", "$apis.requireSuperuserAuth()",
    """
try {
  var key = $os.getenv("MAGNIFIC_API_KEY") || ""
  if (!key) return e.json(200, { ok: false, key_set: false, message: "MAGNIFIC_API_KEY is not set in the container" })
  return e.json(200, { ok: true, key_set: true, message: "key set - credit balance is not exposed via the Magnific API" })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/upstage-status - superuser only - Upstage key validity (free 401/404 probe)
route(
    "upstage-status", "GET", "/api/creator/upstage-status", "$apis.requireSuperuserAuth()",
    """
try {
  var key = $os.getenv("UPSTAGE_API_KEY") || ""
  if (!key) return e.json(200, { ok: false, valid: false, message: "UPSTAGE_API_KEY not set (optional - vision OCR is used instead)" })
  var res = $http.send({ url: "https://api.upstage.ai/v1/credits", method: "GET", headers: { "Authorization": "Bearer " + key }, timeout: 20 })
  if (res.statusCode === 401) return e.json(200, { ok: false, valid: false, message: "key rejected (401) - check UPSTAGE_API_KEY" })
  if (res.statusCode === 404) return e.json(200, { ok: true, valid: true, message: "key valid - scanned-PDF OCR ready" })
  return e.json(200, { ok: res.statusCode !== 401, valid: res.statusCode !== 401, status: res.statusCode, message: "probe HTTP " + res.statusCode })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/tts-models - superuser only - ALL OpenRouter TTS models with
# their live supported_voices (no hardcoded model list). Fetches
# /api/v1/models?output_modalities=speech with the container key, caches the
# result in mock_cache for 15 min and upserts each model into mock_models.
route(
    "tts-models", "GET", "/api/creator/tts-models", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  var key = $os.getenv("OPENROUTER_API_KEY") || ""
  if (!key) return e.json(200, { ok: false, cached: false, models: [], message: "OPENROUTER_API_KEY is not set in the container" })
  var force = (e.request.url.query().get("force") || "") === "1"
  var cached = null
  try { cached = $app.findFirstRecordByData("mock_cache", "key", "tts_models_v1") } catch (errC) {}
  var val = null
  try { val = cached ? (cached.get("value") || null) : null } catch (errV) { val = null }
  var fetchedAt = (val && val.fetched_at) || 0
  var age = Date.now() - fetchedAt
  if (cached && !force && age < 900000) {
    return e.json(200, { ok: true, cached: true, models: (val && val.models) || [], fetched_at: fetchedAt })
  }
  var res = $http.send({ url: "https://openrouter.ai/api/v1/models?output_modalities=speech", method: "GET", headers: { "Authorization": "Bearer " + key }, timeout: 30 })
  if (res.statusCode !== 200) {
    return e.json(200, { ok: false, cached: !!cached, models: (val && val.models) || [], message: "OpenRouter list failed (HTTP " + (res.statusCode || 0) + ")" })
  }
  var j = {}
  try { j = JSON.parse(res.raw || "{}") } catch (errJ) {}
  var out = []
  var list = j.data || []
  var col = null
  try { col = $app.findCollectionByNameOrId("mock_models") } catch (errCol) {}
  for (var i = 0; i < list.length; i++) {
    var m = list[i]
    var id = String(m.id || "")
    if (!id) continue
    var voices = m.supported_voices || []
    var params = m.supported_parameters || []
    var pricing = m.pricing || {}
    var supportsSpeed = false
    for (var pi = 0; pi < params.length; pi++) { if (String(params[pi]).toLowerCase() === "speed") supportsSpeed = true }
    var free = Number(pricing.prompt || 0) === 0 && Number(pricing.completion || 0) === 0
    out.push({ model: id, display: String(m.name || id), voices: voices, supports_speed: supportsSpeed, free: free })
    if (col) {
      try {
        var rec = null
        try { rec = $app.findFirstRecordByData("mock_models", "model", id) } catch (errR) {}
        if (!rec) { rec = new Record(col); rec.set("kind", "tts"); rec.set("model", id) }
        rec.set("display", String(m.name || id))
        rec.set("notes", (free ? "free " : "") + "TTS - " + voices.length + " voices" + (supportsSpeed ? " - speed ok" : ""))
        $app.save(rec)
      } catch (errS) {}
    }
  }
  out.sort(function (a, b) { return a.model < b.model ? -1 : 1 })
  var payload = { fetched_at: Date.now(), models: out }
  if (cached) { cached.set("value", payload); $app.save(cached) }
  else {
    try {
      var ccol = $app.findCollectionByNameOrId("mock_cache")
      var rec2 = new Record(ccol); rec2.set("key", "tts_models_v1"); rec2.set("value", payload); $app.save(rec2)
    } catch (err2) {}
  }
  return e.json(200, { ok: true, cached: false, models: out, fetched_at: payload.fetched_at })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/audio-pool-status - superuser only - uploaded audio-question
# pool status (file, verified count, worker note).
route(
    "audio-pool-status", "GET", "/api/creator/audio-pool-status", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  var rec = null
  try {
    var list = $app.findRecordsByFilter("audio_pool", "", "-updated", 1, 0)
    if (list && list.length > 0) rec = list[0]
  } catch (err) {}
  if (!rec) return e.json(200, { ok: true, id: "", active: false, count: 0, file: "", updated: "", note: "" })
  var fname = ""
  try { fname = rec.getString("file") || "" } catch (errF) {}
  return e.json(200, { ok: true, id: rec.id, active: rec.getBool("active"), count: rec.getInt("count"),
                       file: fname, updated: rec.getString("updated"),
                       note: rec.getString("note") || "" })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)
route(
    "blank-pool-status", "GET", "/api/creator/blank-pool-status", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  var rec = null
  try {
    var list = $app.findRecordsByFilter("audio_pool", "", "-updated", 1, 0)
    if (list && list.length > 0) rec = list[0]
  } catch (err) {}
  if (!rec) return e.json(200, { ok: true, id: "", active: false, count: 0, file: "", updated: "", note: "" })
  var fname = ""
  try { fname = rec.getString("file") || "" } catch (errF) {}
  return e.json(200, { ok: true, id: rec.id, active: rec.getBool("active"), count: rec.getInt("count"),
                       file: fname, updated: rec.getString("updated"),
                       note: rec.getString("note") || "" })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/tts-preview?model=&voice=&text= - superuser only - voice sample (mp3/wav)
route(
    "tts-preview", "GET", "/api/creator/tts-preview", "$apis.requireSuperuserAuth()",
    """
try {
  function pcmWav(model, pcm) {
    var rate = 24000
    var rl = String(model || "").toLowerCase()
    if (rl.indexOf("fish") >= 0 || rl.indexOf("grok") >= 0) rate = 44100
    var head = new Uint8Array(44)
    var dv = new DataView(head.buffer)
    // FOURCCs must be written big-endian so the bytes come out as "RIFF"/"WAVE"/"fmt "/"data"
    dv.setUint32(0, 0x52494646, false)  // "RIFF"
    dv.setUint32(4, 36 + pcm.length, true)
    dv.setUint32(8, 0x57415645, false)  // "WAVE"
    dv.setUint32(12, 0x666d7420, false) // "fmt "
    dv.setUint32(16, 16, true)
    dv.setUint16(20, 1, true)
    dv.setUint16(22, 1, true)
    dv.setUint32(24, rate, true)
    dv.setUint32(28, rate * 2, true)
    dv.setUint16(32, 2, true)
    dv.setUint16(34, 16, true)
    dv.setUint32(36, 0x64617461, false) // "data"
    dv.setUint32(40, pcm.length, true)
    var out = new Uint8Array(44 + pcm.length)
    out.set(head, 0)
    out.set(pcm, 44)
    return out
  }
  var q = e.request.url.query()
  var model = (q.get("model") || "").trim()
  var voice = (q.get("voice") || "").trim()
  var text = (q.get("text") || "안녕하세요. 음성 미리듣기입니다.").trim().substring(0, 300)
  var key = $os.getenv("OPENROUTER_API_KEY") || ""
  if (!model) return e.json(400, { error: "model param required" })
  if (!voice) return e.json(400, { error: "voice param required" })
  if (!key) return e.json(400, { error: "OPENROUTER_API_KEY is not set in the container" })
  // PCM-only providers (Gemini, MAI) reject response_format=mp3 with a generic
  // 400 - retry with pcm and wrap it in a WAV header so the browser can play it.
  var attempts = [
    { "model": model, "input": text, "voice": voice, "response_format": "mp3" },
    { "model": model, "input": text, "voice": voice, "response_format": "pcm" },
    { "model": model, "input": text, "response_format": "mp3" },
    { "model": model, "input": text, "response_format": "pcm" }
  ]
  var lastErr = "no usable response"
  for (var ai = 0; ai < attempts.length; ai++) {
    var res = $http.send({
      url: "https://openrouter.ai/api/v1/audio/speech",
      method: "POST", headers: { "Authorization": "Bearer " + key, "Content-Type": "application/json" },
      body: JSON.stringify(attempts[ai]),
      timeout: 60
    })
    if (res.statusCode === 200) {
      var bytes = res.body || []
      if (bytes.length >= 100) {
        if (attempts[ai].response_format === "pcm") {
          return e.blob(200, "audio/wav", pcmWav(attempts[ai].model, bytes))
        }
        return e.blob(200, "audio/mpeg", bytes)
      }
    }
    lastErr = "HTTP " + (res.statusCode || 0) + ": " + String(res.raw || "").substring(0, 140)
  }
  return e.json(400, { error: "TTS failed - " + lastErr })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# POST /api/creator/start - API-key (header) based, overrides via query params.
# Superuser tokens also work: pass ?client=<id-or-name> instead of an API key.
route(
    "start", "POST", "/api/creator/start", None,
    """
try {
  ensureCollections()
  var q = e.request.url.query()
  var client = null
  var isSuper = false
  if (e.auth) { try { $app.findRecordById("_superusers", e.auth.id); isSuper = true } catch (err2) {} }
  if (isSuper) {
    var cid = q.get("client") || ""
    if (!cid) return e.json(400, { error: "client query param required (id or name)" })
    try { client = $app.findRecordById("mock_clients", cid) } catch (err3) { client = null }
    if (!client) { try { client = $app.findFirstRecordByData("mock_clients", "name", cid) } catch (err4) {} }
    if (!client) return e.json(404, { error: "client not found" })
  } else {
    var keyRes = checkKey(e.request.header.get("X-API-Key"))
    if (keyRes.error) return e.json(401, { error: keyRes.error })
    client = keyRes.client
  }
  var cfg = ensureConfig(client.id)
  var count = 40
  try { count = parseInt(q.get("count") || "40", 10) } catch (err) {}
  if (isNaN(count) || count < 1 || count > 200) count = 40
  var difficulty = q.get("difficulty") || cfg.getString("difficulty_profile") || "creative+difficult"
  // Normalize teacher-portal vocabulary to pipeline profiles:
  //   creative+medium | creative+difficult | TOPIK EPIS HARD (-> hard)
  var dm = (difficulty || "").trim()
  if (dm === "TOPIK EPIS HARD") dm = "hard"
  if (["creative+medium", "creative+difficult", "hard", "standard"].indexOf(dm) === -1) dm = "creative+difficult"
  difficulty = dm
  var kind = (q.get("kind") || "full").trim()
  if (kind === "dry") kind = "dry_questions"
  if (["full","dry_questions","dry_images","dry_audio","pdf_test"].indexOf(kind) === -1) kind = "full"
  var focus = (q.get("focus") || "").trim()
  if (focus.length > 500) focus = focus.substring(0, 500)
  var parserQ = (q.get("parser") || "").trim()
  if (["auto", "local", "upstage", "mistral"].indexOf(parserQ) === -1) parserQ = ""
  var ov = {}
  if (focus) ov.focus = focus
  if (parserQ) ov.pdf_parser = parserQ
  var gen = 1
  try { gen = parseInt(q.get("gen_type") || "1", 10) } catch (err) {}
  if (isNaN(gen) || gen < 1 || gen > 3) gen = 1
  var pdfFiles = []
  try { pdfFiles = e.findUploadedFiles("pdf") } catch (err) {}
  var pdfName = ""
  if (gen >= 2 && pdfFiles.length === 0) {
    return e.json(400, { error: "PDF file required for generation type " + gen + " - upload the document as multipart field 'pdf'" })
  }
  if (pdfFiles.length > 0) {
    if (pdfFiles[0].size > 10485760) return e.json(400, { error: "PDF exceeds the 10 MB upload limit" })
    pdfName = pdfFiles[0].name || "doc.pdf"
  }
  var jobCol = $app.findCollectionByNameOrId("mock_jobs")
  var job = new Record(jobCol)
  job.set("client", client.id)
  job.set("status", "queued")
  job.set("kind", kind)
  job.set("gen_type", gen)
  job.set("count", count)
  job.set("difficulty", difficulty)
  job.set("overrides", ov)
  if (pdfFiles.length > 0) { try { job.set("pdf", pdfFiles[0]) } catch (err) {} }
  $app.save(job)
  return e.json(200, {
    job_id: job.id,
    status: "queued",
    kind: kind,
    gen_type: gen,
    client: client.getString("name"),
    count: count,
    difficulty: difficulty,
    focus: focus,
    parser: parserQ,
    pdf: pdfName
  })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/config?client=<id-or-name> - superuser only; returns the
# client's config record (created with defaults if missing) as a flat JSON map.
route(
    "config_get", "GET", "/api/creator/config", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  var q = e.request.url.query()
  var cid = q.get("client") || ""
  if (!cid) return e.json(400, { error: "client query param required" })
  var cli = null
  try { cli = $app.findRecordById("mock_clients", cid) } catch (err2) { cli = null }
  if (!cli) { try { cli = $app.findFirstRecordByData("mock_clients", "name", cid) } catch (err3) {} }
  if (!cli) return e.json(404, { error: "client not found" })
  var cfg = ensureConfig(cli.id)
  return e.json(200, {
    client: cli.id,
    client_name: cli.getString("name"),
    config: cfg.id,
    record: {%s}
  })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""" % ", ".join(
    '"%s": cfg.get("%s")' % (k, k) for k in DEFAULT_CONFIG
),
)

# POST /api/creator/delete-client?client=<id> - superuser only - cascade-delete a client
# (PocketBase blocks direct deletes: mock_config/mock_jobs/dryrun/fullrun reference the
# client via required relations, so this hook removes all related records first).
route(
    "delete_client", "POST", "/api/creator/delete-client", "$apis.requireSuperuserAuth()",
    """
try {
  ensureCollections()
  var q = e.request.url.query()
  var cid = q.get("client") || ""
  if (!cid) return e.json(400, { error: "client param required" })
  var client = null
  try { client = $app.findRecordById("mock_clients", cid) } catch (err) { client = null }
  if (!client) return e.json(404, { error: "client not found" })
  var delFor = function (col) {
    var n = 0
    var recs = $app.findRecordsByFilter(col, "client = '" + cid + "'", "", 1000, 0)
    for (var i = 0; i < recs.length; i++) { try { $app.delete(recs[i]); n++ } catch (err) {} }
    return n
  }
  var deleted = {
    configs: delFor("mock_config"),
    jobs: delFor("mock_jobs"),
    dryruns: delFor("dryrun"),
    fullruns: delFor("fullrun")
  }
  $app.delete(client)
  return e.json(200, { ok: true, deleted: deleted })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/jobs - API-key: own jobs; superuser: all jobs (+ ?client=, ?status=)
route(
    "jobs_list", "GET", "/api/creator/jobs", None,
    """
try {
  ensureCollections()
  var q = e.request.url.query()
  var recs = null
  var isSuper = false
  if (e.auth) { try { $app.findRecordById("_superusers", e.auth.id); isSuper = true } catch (err2) {} }
  if (isSuper) {
    var filter = "1=1"
    var cid = q.get("client") || ""
    if (cid) filter = "client = '" + cid + "'"
    var st = q.get("status") || ""
    if (st) filter = filter + " && status = '" + st + "'"
    recs = $app.findRecordsByFilter("mock_jobs", filter, "-created", 100, 0)
  } else {
    var keyRes = checkKey(e.request.header.get("X-API-Key"))
    if (keyRes.error) return e.json(401, { error: keyRes.error })
    recs = $app.findRecordsByFilter("mock_jobs", "client = '" + keyRes.client.id + "'", "-created", 50, 0)
  }
  var out = []
  for (var i = 0; i < recs.length; i++) {
    var r = recs[i]
    var cname = ""
    try { cname = $app.findRecordById("mock_clients", r.getString("client")).getString("name") } catch (err3) {}
    out.push({
      id: r.id,
      client: r.getString("client"),
      client_name: cname,
      status: r.getString("status"),
      kind: r.getString("kind") || "full",
      gen_type: r.getInt("gen_type") || 1,
      pdf: r.getString("pdf"),
      count: r.getInt("count"),
      difficulty: r.getString("difficulty"),
      focus: (function () {
        try {
          var ov = r.get("overrides")
          if (ov && typeof ov === "string") { ov = JSON.parse(ov) }
          return (ov && ov.focus) || ""
        } catch (errF) { return "" }
      })(),
      log: (r.getString("log") || "").substring(0, 300),
      pushed: r.getBool("pushed"),
      report: (function () {
        try {
          var rep = r.get("report")
          if (rep && typeof rep === "string") { rep = JSON.parse(rep) }
          return rep || null
        } catch (errR) { return null }
      })(),
      created: r.getString("created"),
      updated: r.getString("updated"),
      error: r.getString("error")
    })
  }
  return e.json(200, { jobs: out })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)

# GET /api/creator/jobs/{id} - API-key: own only; superuser: any job
route(
    "jobs_get", "GET", "/api/creator/jobs/{id}", None,
    """
try {
  ensureCollections()
  var isSuper = false
  if (e.auth) { try { $app.findRecordById("_superusers", e.auth.id); isSuper = true } catch (err2) {} }
  var client = null
  if (!isSuper) {
    var keyRes = checkKey(e.request.header.get("X-API-Key"))
    if (keyRes.error) return e.json(401, { error: keyRes.error })
    client = keyRes.client
  }
  var id = e.request.pathValue("id")
  var rec = null
  try { rec = $app.findRecordById("mock_jobs", id) } catch (err) {}
  if (!rec) return e.json(404, { error: "job not found" })
  if (!isSuper && rec.getString("client") !== client.id) return e.json(403, { error: "forbidden" })
  var cname = ""
  try { cname = $app.findRecordById("mock_clients", rec.getString("client")).getString("name") } catch (err3) {}
  return e.json(200, {
    id: rec.id,
    client: rec.getString("client"),
    client_name: cname,
    status: rec.getString("status"),
    kind: rec.getString("kind") || "full",
    gen_type: rec.getInt("gen_type") || 1,
    pdf: rec.getString("pdf"),
    count: rec.getInt("count"),
    difficulty: rec.getString("difficulty"),
    overrides: rec.get("overrides"),
    log: rec.getString("log"),
    report: rec.get("report"),
    error: rec.getString("error"),
    pushed: rec.getBool("pushed"),
    created: rec.getString("created"),
    updated: rec.getString("updated")
  })
} catch (err) {
  return e.json(500, { error: String(err) })
}
""",
)


def build():
    out = []
    out.append("// GENERATED by build_hooks.py - DO NOT EDIT DIRECTLY.")
    out.append("// Run: uv run python build_hooks.py > main.pb.js  (or edit the generator)")
    out.append("// PocketBase v0.39.10 JSVM. Handlers are fully self-contained because")
    out.append("// PB source-serializes handlers and re-evaluates them per request.")
    out.append("// Top-level declarations are NOT visible inside handlers, so all shared")
    out.append("// snippets are physically inlined into every handler body.")
    out.append("")
    out.append("// Serve the /creator/ admin GUI (pb_public/creator). The page shell is public")
    out.append("// (like PB's own _/ admin) - every data endpoint behind it is superuser-only,")
    out.append("// and the GUI gates itself behind a superuser login screen.")
    out.append("routerAdd(\"GET\", \"/creator/{path...}\", $apis.static($filepath.join($filepath.dir(__hooks), \"pb_public\", \"creator\"), { indexFallback: true }))")
    out.append("")
    for name, method, path, middleware, body in HANDLERS:
        src = "routerAdd(%s, %s, (e) => {\n%s\n%s\n%s\n%s\n}" % (
            json.dumps(method), json.dumps(path),
            ENSURE_FN.rstrip(), KEY_CHECK_FN.rstrip(), ENSURE_CONFIG_FN.rstrip(), body)
        if middleware:
            src += ", %s)" % middleware
        else:
            src += ")"
        out.append(src)
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    # Write with explicit UTF-8. NEVER rely on stdout redirection ("> main.pb.js"):
    # Windows' cp1252 console mangles non-ASCII (e.g. em-dash) into invalid UTF-8,
    # which then makes Goja refuse to parse the whole hooks file at runtime.
    out = build()
    with open("main.pb.js", "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"wrote main.pb.js ({len(out)} bytes)")
