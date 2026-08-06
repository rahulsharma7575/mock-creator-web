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

COLLECTION_IDS = {
    "mock_clients": "c_mock_clients",
    "mock_config": "c_mock_config",
    "mock_config_meta": "c_mock_config_meta",
    "mock_models": "c_mock_models",
    "mock_jobs": "c_mock_jobs",
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
        f("llm_proofread_model", "text", max=200),
        f("llm_repair_model", "text", max=200),
        f("image_primary", "text", max=200),
        f("image_fallback", "text", max=200),
        f("tts_model", "text", max=200),
        f("tts_fallback_model", "text", max=200),
        f("tts_fallback_voice", "text", max=200),
        f("question_count", "number", min=1, max=200),
        f("reading_count", "number", min=0, max=50),
        f("image_count", "number", min=0, max=40),
        f("image_count_min", "number", min=0, max=40),
        f("image_count_max", "number", min=0, max=40),
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
        f("audio_gap_ms", "number", min=0, max=5000),
        f("sample_rate", "number", min=8000, max=48000),
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
    collection("mock_jobs", [
        f("client", "relation", required=True,
          collectionId=COLLECTION_IDS["mock_clients"], maxSelect=1, minSelect=0),
        f("status", "select", values=["queued", "running", "done", "failed"], maxSelect=1),
        f("count", "number", min=1, max=200),
        f("difficulty", "select", values=["creative+difficult", "creative+medium", "hard", ""], maxSelect=1),
        f("overrides", "json", maxSize=65536),
        f("log", "editor"),
        f("report", "json", maxSize=2097152),
        f("pushed", "bool"),
        f("error", "text", max=2000),
        f("created", "autodate", onCreate=True),
        f("updated", "autodate", onCreate=True, onUpdate=True),
    ]),
]

DEFAULT_CONFIG = {
    "name": "default",
    "llm_author_model": "google/gemini-2.5-flash",
    "llm_proofread_model": "qwen/qwen3.5-flash-02-23",
    "llm_repair_model": "google/gemini-2.5-flash",
    "image_primary": "z-image",
    "image_fallback": "p-image-ideogram-1k",
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "microsoft/mai-voice-2-flash",
    "tts_fallback_voice": "ko-KR-Haena",
    "question_count": 40,
    "reading_count": 20,
    "image_count": 22,
    "image_count_min": 18,
    "image_count_max": 26,
    "difficulty_profile": "creative+difficult",
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
    "audio_gap_ms": 300,
    "sample_rate": 44100,
    "audio_workers": 4,
    "prompts_json": {},
    "active": True,
}

META_FIELDS = [
    ("llm_author_model", "Author LLM", "text", "LLM", "OpenRouter model for question authoring"),
    ("llm_proofread_model", "Proofread LLM", "text", "LLM", "OpenRouter model for proofreading"),
    ("llm_repair_model", "Repair LLM", "text", "LLM", "OpenRouter model for repair pass"),
    ("image_primary", "Primary image model", "text", "Images", "Magnific model slug (z-image)"),
    ("image_fallback", "Fallback image model", "text", "Images", "Magnific fallback (p-image-ideogram-1k)"),
    ("tts_model", "TTS model", "text", "Audio", "OpenRouter TTS model"),
    ("tts_fallback_model", "TTS fallback model", "text", "Audio", "Fallback TTS model"),
    ("tts_fallback_voice", "TTS fallback voice", "text", "Audio", "Fallback voice id"),
    ("question_count", "Total questions", "number", "Exam", "40 by default"),
    ("reading_count", "Reading questions", "number", "Exam", "Reading section size"),
    ("image_count", "Image questions target", "number", "Exam", "Target count"),
    ("image_count_min", "Image questions min", "number", "Exam", "Minimum (18)"),
    ("image_count_max", "Image questions max", "number", "Exam", "Maximum (26)"),
    ("difficulty_profile", "Difficulty profile", "select", "Exam", "creative+difficult | creative+medium | hard", ["creative+difficult", "creative+medium", "hard"]),
    ("marks_per_question", "Marks per question", "number", "Exam", "Default marks"),
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
    ("audio_gap_ms", "Audio gap (ms)", "number", "Audio", "Gap between TTS clips"),
    ("sample_rate", "Sample rate", "number", "Audio", "TTS output sample rate"),
    ("audio_workers", "Audio workers", "number", "Audio", "Parallel TTS workers"),
    ("active", "Config enabled", "bool", "General", "Use this config"),
]

MODEL_SEEDS = [
    ("llm", "google/gemini-2.5-flash", "Gemini 2.5 Flash", "fast author/proofread"),
    ("llm", "google/gemini-3.5-flash", "Gemini 3.5 Flash", "repair pass"),
    ("llm", "qwen/qwen3.5-flash-02-23", "Qwen 3.5 Flash", "proofread"),
    ("llm", "claude-sonnet-4.5", "Claude Sonnet 4.5", "alt author"),
    ("llm", "deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "alt repair"),
    ("tts", "fish-audio/s2.1-pro-free:free", "Fish Audio S2.1 Pro (free)", "default TTS"),
    ("tts", "microsoft/mai-voice-2-flash", "MAI Voice 2 Flash", "fallback TTS"),
    ("tts", "x-ai/grok-voice-tts-1.0", "Grok Voice TTS", "alt TTS"),
    ("tts", "google/gemini-3.1-flash-tts-preview", "Gemini Flash TTS", "alt TTS"),
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
}
""" % (
    json.dumps(list(COLLECTION_IDS.keys())),
    json.dumps(SCHEMA),
    "".join(
        "var m{0} = new Record(meta); m{0}.set(\"field\", \"{1}\"); m{0}.set(\"label\", \"{2}\"); m{0}.set(\"ftype\", \"{3}\"); m{0}.set(\"group\", \"{4}\"); m{0}.set(\"help\", \"{5}\"); {6}$app.save(m{0});\n".format(
            i, field, label, ftype, group, help_text,
            ("m{0}.set(\"options\", {1}); ".format(i, json.dumps(opts[0])) if opts else ""),
        )
        for i, (field, label, ftype, group, help_text, *opts) in enumerate(META_FIELDS)
    ),
    "".join(
        "var k{0} = new Record(models); k{0}.set(\"kind\", \"{1}\"); k{0}.set(\"model\", \"{2}\"); k{0}.set(\"display\", \"{3}\"); k{0}.set(\"notes\", \"{4}\"); $app.save(k{0});\n".format(
            i, kind, model, display, notes)
        for i, (kind, model, display, notes) in enumerate(MODEL_SEEDS)
    ),
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
    for k, v in DEFAULT_CONFIG.items() if k not in ("name",)
)

HANDLERS = []


def route(name, method, path, middleware, body):
    HANDLERS.append((name, method, path, middleware, body))


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
  var jobCol = $app.findCollectionByNameOrId("mock_jobs")
  var job = new Record(jobCol)
  job.set("client", client.id)
  job.set("status", "queued")
  job.set("count", count)
  job.set("difficulty", difficulty)
  job.set("overrides", {})
  $app.save(job)
  return e.json(200, {
    job_id: job.id,
    status: "queued",
    client: client.getString("name"),
    count: count,
    difficulty: difficulty
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
      count: r.getInt("count"),
      difficulty: r.getString("difficulty"),
      pushed: r.getBool("pushed"),
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
    out.append("// (like PB's own _/ admin) — every data endpoint behind it is superuser-only,")
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
    print(build())
