#!/usr/bin/env python3
"""
worker.py — job runner for the Mock Creator SaaS.

Polls the local PocketBase (pb_hooks/main.pb.js) for queued mock_jobs and
executes one job at a time with the client's mock_config record:

    mock_config record  ->  pipeline config JSON  ->  pipeline/mock_next.py --config

mock_next.py authors questions, proofreads, pushes question records + exam to
the client's PocketBase (push_pb_base) and generates audio/images itself, so
the worker only has to stream output into the job log and record the outcome.

Environment:
  PB_URL               PocketBase base URL        (default http://127.0.0.1:8090)
  PB_SUPERUSER_EMAIL   superuser email            (required)
  PB_PASSWORD          superuser password         (required)
  POLL_INTERVAL        poll delay in seconds      (default 10)
  WORK_ROOT            per-job work dir           (default ./worker_data)
  OPENROUTER_API_KEY   passed through to the pipeline
  MAGNIFIC_API_KEY     passed through to the pipeline
  MOCK_NEXT            pipeline entry script      (default ./pipeline/mock_next.py)

Usage:
  python worker.py            # run forever
  python worker.py --once     # process one queued job (if any) and exit
  python worker.py --check    # verify auth + connectivity, then exit
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PB_URL = os.environ.get("PB_URL", "http://127.0.0.1:8090").rstrip("/")
SUPERUSER_EMAIL = os.environ.get("PB_SUPERUSER_EMAIL", "")
SUPERUSER_PASS = os.environ.get("PB_PASSWORD", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))
WORK_ROOT = pathlib.Path(os.environ.get("WORK_ROOT", "worker_data")).resolve()
HERE = pathlib.Path(__file__).resolve().parent
MOCK_NEXT = pathlib.Path(os.environ.get("MOCK_NEXT", str(HERE / "pipeline" / "mock_next.py")))
if str(HERE / "pipeline") not in sys.path:
    sys.path.insert(0, str(HERE / "pipeline"))  # for pdf_parser (pdf_test jobs)

LOG_CAP = 24000          # max chars kept in the job log field
LOG_FLUSH_LINES = 8      # flush log after this many new lines
LOG_FLUSH_SECS = 4.0     # ... or this many seconds
RUNNING_STALE_SECS = 20 * 60

# mock_config record field -> pipeline config key(s).
# Keys are skipped when null/empty. Unknown pipeline keys are ignored by
# mock_next.py's load_config (it filters against its own DEFAULTS), so extra
# keys like tts_workers are safe to include.
CONFIG_MAP = {
    "llm_author_model": "author_model",
    "author_provider": "author_provider",
    "gemini_model": "gemini_model",
    "llm_proofread_model": "proof_model",
    "llm_repair_model": "repair_model",
    "image_primary": "img_model",
    "image_fallback": "img_fallback_model",
    "tts_model": "tts_model",
    "tts_fallback_model": "tts_fallback_model",
    "tts_fallback_voice": "tts_fallback_voice",
    "tts_male_voice": "tts_male_voice",
    "tts_female_voice": "tts_female_voice",
    "tts_fallback_male_voice": "tts_fallback_male_voice",
    "tts_fallback_female_voice": "tts_fallback_female_voice",
    "pdf_parser": "pdf_parser",
    "upscale_pdf_images": "upscale_pdf_images",
    "question_count": "question_count",
    "reading_count": "reading_count",
    "image_count": "image_count",
    "image_count_min": "image_count_min",
    "image_count_max": "image_count_max",
    "difficulty_profile": "difficulty_profile",
    "marks_per_question": "marks_per_question",
    "max_tokens": ["author_max_tokens", "proof_max_tokens"],
    "temperature": "proof_temperature",
    "timeout_s": ["llm_timeout_s", "author_timeout_s", "proof_timeout_s"],
    "retries": ["author_retries", "proof_retries"],
    "img_retries": ["img_retries", "img_fallback_retries"],
    "push_pb_base": "pb_base",
    "push_pb_email": "pb_email",
    "push_pb_pass": "pb_pass",
    "push_subject_id": "subject_id",
    "push_exam_type": "exam_type",
    "push_exam_status": "exam_status",
    "push_enabled": "push_enabled",
    "dedup_enabled": "dedup_enabled",
    "dedup_sets": "dedup_sets",
    "audio_gap_ms": "tts_gap_ms",
    "sample_rate": "tts_rate",
    "tts_speed": "tts_speed",
    "tts_natural_pacing": "tts_natural_pacing",
    "tts_polish": "tts_polish",
    "tts_atempo_models": "tts_atempo_models",
    "tts_male_speed": "tts_male_speed",
    "tts_female_speed": "tts_female_speed",
    "tts_fallback_male_speed": "tts_fallback_male_speed",
    "tts_fallback_female_speed": "tts_fallback_female_speed",
    "listening_blank_count": "listening_blank_count",
    "listening_picture_count": "listening_picture_count",
    "image_style_prompt": "image_style_prompt",
    "audio_workers": "tts_workers",
    "active": "is_active",
}

_token = None
_token_at = 0.0


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg), flush=True)


def api(method, path, body=None):
    """Raw request against the local PocketBase. Returns parsed JSON."""
    global _token, _token_at
    url = PB_URL + path
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if path != "/api/collections/_superusers/auth-with-password":
        if not _token or time.time() - _token_at > 3500:
            _token = auth()
            _token_at = time.time()
        headers["Authorization"] = "Bearer " + _token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read()).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            _token = None  # force re-auth on next call
        raise RuntimeError(f"HTTP {e.code} {path}: {msg or e.reason}")


def auth():
    if not SUPERUSER_EMAIL or not SUPERUSER_PASS:
        raise RuntimeError("PB_SUPERUSER_EMAIL / PB_PASSWORD not set")
    url = PB_URL + "/api/collections/_superusers/auth-with-password"
    data = json.dumps({"identity": SUPERUSER_EMAIL, "password": SUPERUSER_PASS}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["token"]


def download_file(path, dest):
    """Download a stored file (e.g. a job PDF) from the local PocketBase."""
    global _token, _token_at
    if not _token or time.time() - _token_at > 3500:
        _token = auth()
        _token_at = time.time()
    req = urllib.request.Request(PB_URL + path, headers={"Authorization": "Bearer " + _token})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def get_jobs(status):
    path = "/api/creator/jobs"
    if status:
        path += "?status=" + status
    out = api("GET", path)
    return out.get("jobs") or []


def get_config(client_id):
    from urllib.parse import quote
    return api("GET", "/api/creator/config?client=" + quote(client_id))


def get_record(collection, rec_id):
    return api("GET", f"/api/collections/{collection}/records/{rec_id}")


def patch_job(job_id, fields):
    api("PATCH", f"/api/collections/mock_jobs/records/{job_id}", fields)


def build_pipeline_config(cfg_record, job):
    cfg = {}
    for k, v in cfg_record.items():
        targets = CONFIG_MAP.get(k)
        if not targets or v is None or v == "":
            continue
        if isinstance(targets, list):
            for t in targets:
                cfg[t] = v
        else:
            cfg[targets] = v
    # job-level overrides win over the stored config
    if job.get("count"):
        cfg["question_count"] = int(job["count"])
    if job.get("difficulty"):
        cfg["difficulty_profile"] = job["difficulty"]
    for k, v in (job.get("overrides") or {}).items():
        cfg[k] = v
    return cfg


def stream_job(job_id, proc, log_tail):
    """Drain pipeline output into the job log; returns final log tail."""
    last_flush = time.time()
    pending = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            pending.append(line)
            print(line, flush=True)
        if len(pending) >= LOG_FLUSH_LINES or time.time() - last_flush >= LOG_FLUSH_SECS:
            if pending:
                log_tail += "\n" + "\n".join(pending)
                log_tail = log_tail[-LOG_CAP:]
                pending = []
                try:
                    patch_job(job_id, {"log": log_tail})
                except Exception as e:
                    log(f"log flush failed: {e}")
            last_flush = time.time()
    if pending:
        log_tail += "\n" + "\n".join(pending)
        log_tail = log_tail[-LOG_CAP:]
    return log_tail


def read_report(workdir):
    mocks_dir = workdir / "mocks"
    if not mocks_dir.is_dir():
        return None
    dirs = sorted(mocks_dir.glob("mock*"),
                  key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
    for d in dirs:
        rep = d / "extra" / "run_report.json"
        if rep.exists():
            try:
                return json.loads(rep.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def find_questions_file(workdir):
    """Newest mock*/questions/mock*_questions.json under the job workdir."""
    mocks_dir = workdir / "mocks"
    if not mocks_dir.is_dir():
        return None
    dirs = sorted(mocks_dir.glob("mock*"),
                  key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
    for d in dirs:
        hits = sorted(d.glob("questions/mock*_questions.json"))
        if hits:
            return hits[0]
    return None


def collect_dryrun_assets(workdir):
    """Collect generated images (webp) + audio (mp3) from the newest mock dir."""
    mocks_dir = workdir / "mocks"
    images, audios = [], []
    if not mocks_dir.is_dir():
        return images, audios
    dirs = sorted(mocks_dir.glob("mock*"),
                  key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
    for d in dirs:
        for p in sorted((d / "images" / "generated").glob("*.webp")) if (d / "images" / "generated").is_dir() else []:
            images.append(p)
        for p in sorted(d.glob("audio/*.mp3")) if (d / "audio").is_dir() else []:
            audios.append(p)
        if images or audios:
            break
    return images, audios


def find_audio_map(workdir):
    """Newest mock*/audio/mock*_audio_map.json mapping Q<num> -> mp3 filename."""
    mocks_dir = workdir / "mocks"
    if not mocks_dir.is_dir():
        return None
    dirs = sorted(mocks_dir.glob("mock*"),
                  key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True)
    for d in dirs:
        m = d / "audio" / (d.name + "_audio_map.json")
        if m.exists():
            try:
                return json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def multipart_create_record(collection, fields, image_paths, audio_paths):
    """Create a record with JSON fields + files in ONE multipart POST (atomic)."""
    global _token, _token_at
    boundary = "----mc" + format(time.time_ns(), "x") + os.urandom(4).hex()
    parts = []

    def add_text(name, value):
        if value is None:
            return
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    def add_file(name, path, ctype):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{path.name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode("utf-8"))
        parts.append(path.read_bytes())
        parts.append(b"\r\n")

    for k, v in fields.items():
        add_text(k, v)
    for p in image_paths:
        add_file("images", p, "image/webp")
    for p in audio_paths:
        add_file("audio", p, "audio/mpeg")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    if not _token or time.time() - _token_at > 3500:
        _token = auth()
        _token_at = time.time()
    req = urllib.request.Request(
        PB_URL + f"/api/collections/{collection}/records",
        data=body, method="POST",
        headers={"Authorization": "Bearer " + _token,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def summarize_log(log_tail, kind):
    """Plain-language summary of a job log via deepseek-v4-flash (cheap, ~1k tokens).

    Returns a short string or '' on any failure (summary is a nicety, never a blocker)."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key or not log_tail:
        return ""
    prompt = (
        "Summarize this mock-exam generation job log for a non-technical admin. "
        "Output 5-8 short plain-English bullets: what was generated (question/image/audio counts), "
        "which model providers were used, stage timings, any costs shown, and any warnings or errors. "
        "Do not invent numbers that are not in the log. Job kind: " + kind + ".\n\nLOG:\n" + log_tail[-6000:]
    )
    try:
        body = json.dumps({
            "model": "deepseek/deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": "You write concise, friendly, accurate summaries for a non-technical admin."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 400,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            j = json.loads(r.read().decode("utf-8"))
        return str((j.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()[:1200]
    except Exception as e:
        log(f"summary generation failed: {e}")
        return ""


def run_job(job):
    job_id = job["id"]
    client_id = job.get("client") or ""
    client_name = job.get("client_name") or client_id
    t0 = time.time()
    log(f"job {job_id} ({client_name}): starting")

    try:
        cfg_record = get_config(client_id).get("record") or {}
        cfg = build_pipeline_config(cfg_record, job)
    except Exception as e:
        log(f"job {job_id}: config fetch failed: {e}")
        patch_job(job_id, {"status": "failed", "error": f"config fetch failed: {e}"[:1900]})
        return

    if not cfg.get("is_active", True):
        log(f"job {job_id}: config disabled for client {client_name}")
        patch_job(job_id, {"status": "failed", "error": "client config is disabled"})
        return

    cfg["is_active"] = True  # pipeline pushes only when active

    # PDF generation modes (end-user app sends gen_type + the PDF file)
    kind = job.get("kind", "full") or "full"
    gen_type = int(job.get("gen_type") or 1)
    if kind == "pdf_test":
        gen_type = max(gen_type, 3)  # parser tests default to paper mode
    cfg["gen_type"] = gen_type
    if gen_type >= 2 and kind != "pdf_test":
        cfg["question_count"] = 40
        cfg["reading_count"] = 20
        if gen_type == 3:
            cfg["image_count"] = 0
            cfg["image_count_min"] = 0
            cfg["image_count_max"] = 40  # image count follows the paper, never forced
            log(f"job {job_id}: gen_type=3 (paper PDF) - forcing 40 questions (20 reading / 20 listening), images follow the paper")
        else:
            cfg["difficulty_profile"] = "creative+medium"  # balanced: mostly medium, few hard, never easy
            log(f"job {job_id}: gen_type=2 (book PDF) - forcing 40 questions (20 reading / 20 listening), balanced difficulty")

    kind = job.get("kind", "full") or "full"
    if kind.startswith("dry_"):
        dry_mode = kind[4:]  # questions | images | audio
        cfg["dry_mode"] = dry_mode
        cfg["question_count"] = 3
        if dry_mode == "images":
            # 3 questions, all with images
            cfg["reading_count"] = 3
            cfg["image_count"] = 3
            cfg["image_count_min"] = 3
            cfg["image_count_max"] = 3
        elif dry_mode == "audio":
            # 3 questions, all listening (each gets an mp3), no images
            cfg["reading_count"] = 0
            cfg["image_count"] = 0
            cfg["image_count_min"] = 0
            cfg["image_count_max"] = 0
        else:
            # questions: 2 reading + 1 listening, no images
            cfg["reading_count"] = 2
            if gen_type != 3:
                # paper mode keeps its own image bounds (0-40) - images follow the paper
                cfg["image_count"] = 0
                cfg["image_count_min"] = 0
                cfg["image_count_max"] = 0
        log(f"job {job_id}: dry run mode={dry_mode}, sample_size=3")

    if kind != "pdf_test" and cfg.get("push_enabled", True) and not cfg.get("pb_pass") and not os.environ.get("MOCK_PB_PASS"):
        log(f"job {job_id}: push credentials missing for client {client_name}")
        patch_job(job_id, {
            "status": "failed",
            "error": ("push_pb_pass is empty for client " + client_name +
                      " - set the Push password in the /creator config editor "
                      "or toggle 'Push to PocketBase' off to generate locally only")
        })
        return
    if not cfg.get("push_enabled", True):
        log(f"job {job_id}: push disabled for client {client_name} — local-only generation")

    workdir = WORK_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "mocks").mkdir(parents=True, exist_ok=True)

    # PDF generation modes need the uploaded document on disk before the pipeline starts
    log_tail = ""
    pdf_path = ""
    if gen_type >= 2:
        pdf_name = str(job.get("pdf") or "").strip()
        if not pdf_name:
            patch_job(job_id, {"status": "failed",
                               "error": f"generation type {gen_type} requires a PDF file"})
            return
        pdf_path = str(workdir / pdf_name)
        patch_job(job_id, {"status": "running",
                           "log": f"[worker {int(time.time() - t0)}s] downloading PDF '{pdf_name}'…",
                           "error": ""})
        try:
            n = download_file(f"/api/files/mock_jobs/{job_id}/{pdf_name}", pdf_path)
            log_tail = f"[worker {int(time.time() - t0)}s] PDF downloaded ({n} bytes)"
            log(f"job {job_id}: downloaded PDF {pdf_name} ({n} bytes)")
        except Exception as e:
            patch_job(job_id, {"status": "failed", "error": f"PDF download failed: {e}"[:1900]})
            return
        cfg["pdf_path"] = pdf_path

    cfgfile = workdir / "config.json"
    cfgfile.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    patch_job(job_id, {
        "status": "running",
        "log": (log_tail + "\n" if log_tail else "") +
               ("[worker] dry_run=" + cfg.get("dry_mode", "full") +
                " started for " + client_name +
                " (count=" + str(cfg.get("question_count")) +
                ", difficulty=" + str(cfg.get("difficulty_profile")) + ")"),
        "error": "",
    })
    log_tail = ((log_tail + "\n") if log_tail else "") + (
        "[worker] dry_run=" + cfg.get("dry_mode", "full") + " started for " + client_name +
        " (count=" + str(cfg.get("question_count")) + ", difficulty=" + str(cfg.get("difficulty_profile")) + ")")

    # ---- parser quick-test: parse only, no authoring/push ----
    if kind == "pdf_test":
        def log_job(m):
            nonlocal log_tail
            log_tail += "\n" + m
            log_tail = log_tail[-LOG_CAP:]
            try:
                patch_job(job_id, {"log": log_tail})
            except Exception:
                pass
            log(m)

        import base64
        import io
        log_job(f"[worker {int(time.time() - t0)}s] PDF parser test — parser '{cfg.get('pdf_parser', 'auto')}'")
        try:
            import pdf_parser as P
            from PIL import Image
            doc = P.parse_pdf(pdf_path, gen_type=gen_type,
                              parser=str(cfg.get("pdf_parser") or "auto"),
                              upstage_key=os.environ.get("UPSTAGE_API_KEY", ""),
                              or_key=os.environ.get("OPENROUTER_API_KEY", ""),
                              progress=lambda m: log_job("[pdf] " + m))
            thumbs = []
            for im in doc["images"]:
                if len(thumbs) >= 24:
                    break
                try:
                    pil = Image.open(io.BytesIO(im["png"])).convert("RGB")
                    pil.thumbnail((160, 160))
                    buf = io.BytesIO()
                    pil.save(buf, "JPEG", quality=70)
                    qn = im.get("nearest_question")
                    thumbs.append({"id": im["id"], "page": im.get("page"),
                                   "question": qn if isinstance(qn, int) and qn > 0 else None,
                                   "b64": base64.b64encode(buf.getvalue()).decode("ascii")})
                except Exception:
                    continue
            report = {
                "parser_used": doc["parser_used"],
                "pages": len(doc["pages"]),
                "images": thumbs,
                "text_chars": len(doc["text"]),
                "excluded_pages": doc["stats"].get("excluded_pages", []),
                "time_ms": int((time.time() - t0) * 1000),
            }
            patch_job(job_id, {"status": "done", "log": log_tail, "report": report, "error": ""})
            log(f"job {job_id}: pdf_test done ({doc['parser_used']}, {len(doc['pages'])} pages, {len(thumbs)} thumbs)")
        except Exception as e:
            log_tail += f"\n[worker] pdf test failed: {e}"
            log_tail = log_tail[-LOG_CAP:]
            patch_job(job_id, {"status": "failed", "log": log_tail,
                               "error": "pdf test failed: %s" % str(e)[:1900]})
            log(f"job {job_id}: pdf_test FAILED: {e}")
        return

    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env["MOCK_ROOT"] = str(workdir / "mocks")
    env["MOCK_CONFIG"] = str(cfgfile)
    cmd = [sys.executable, str(MOCK_NEXT), "--config", str(cfgfile)]
    if kind.startswith("dry_"):
        cmd.append("--dry-run")

    rc = -1
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, env=env)
        log_tail = stream_job(job_id, proc, log_tail)
        rc = proc.wait()
    except Exception as e:
        log_tail += f"\n[worker] subprocess error: {e}"
        log_tail = log_tail[-LOG_CAP:]

    report = read_report(workdir)

    if rc == 0:
        log(f"job {job_id}: DONE")
        pushed = (not kind.startswith("dry_")) and bool(report and report.get("exam_created"))
        summary = summarize_log(log_tail, kind)
        patch_job(job_id, {
            "status": "done", "log": log_tail or "[worker] finished (no output)",
            "report": report, "pushed": pushed, "summary": summary,
        })
        try:
            qs_file = find_questions_file(workdir)
            questions = None
            if qs_file:
                questions = json.loads(qs_file.read_text(encoding="utf-8"))
            imgs, auds = collect_dryrun_assets(workdir)
            rec_collection = "dryrun" if kind.startswith("dry_") else "fullrun"
            rec = multipart_create_record(rec_collection, {
                "client": client_id, "kind": kind, "status": "done",
                "count": len(questions) if questions else 0,
                "difficulty": str(cfg.get("difficulty_profile") or ""),
                "questions": json.dumps(questions) if questions else None,
                "report": json.dumps(report) if report else None,
                "summary": summary or None,
                "log": (log_tail or "")[-8000:],
                "audio_map": json.dumps(find_audio_map(workdir)),
            }, imgs, auds)
            log(f"job {job_id}: saved to {rec_collection} collection "
                f"({len(questions) if questions else 0} questions, {len(imgs)} images, {len(auds)} audio)")
        except Exception as e:
            log(f"job {job_id}: {rec_collection} save failed: {e}")
    else:
        err = log_tail.strip().splitlines()
        tail = "\n".join(err[-12:]) if err else "pipeline exited " + str(rc)
        log(f"job {job_id}: FAILED (rc={rc})")
        patch_job(job_id, {
            "status": "failed", "log": log_tail,
            "error": f"exit {rc}: {tail}"[:1900],
        })


def requeue_stale_running():
    """On startup nothing can actually be running — return stragglers to the queue."""
    for job in get_jobs("running"):
        try:
            patch_job(job["id"], {"status": "queued"})
            log(f"job {job['id']}: requeued (stale 'running' from previous worker)")
        except Exception as e:
            log(f"requeue {job['id']} failed: {e}")


def main():
    if not SUPERUSER_EMAIL or not SUPERUSER_PASS:
        log("ERROR: PB_SUPERUSER_EMAIL and PB_PASSWORD are required")
        return 1

    args = sys.argv[1:]
    once = "--once" in args
    check = "--check" in args

    try:
        api("GET", "/api/creator/jobs")
    except Exception as e:
        log(f"ERROR: cannot reach PocketBase at {PB_URL}: {e}")
        return 1
    log(f"connected to {PB_URL} as {SUPERUSER_EMAIL}")

    if check:
        return 0

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    requeue_stale_running()

    while True:
        try:
            queued = get_jobs("queued")
        except Exception as e:
            log(f"poll failed: {e}")
            queued = []
        if queued:
            oldest = queued[-1]  # hooks sorts newest first
            try:
                run_job(oldest)
            except Exception as e:
                log(f"job {oldest['id']} crashed in worker: {e}")
                try:
                    patch_job(oldest["id"], {"status": "failed", "error": f"worker error: {e}"[:1900]})
                except Exception:
                    pass
            if once:
                return 0
            continue
        if once:
            log("no queued jobs")
            return 0
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
