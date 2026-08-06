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

PB_URL = os.environ.get("PB_URL", "http://127.0.0.1:8090").rstrip("/")
SUPERUSER_EMAIL = os.environ.get("PB_SUPERUSER_EMAIL", "")
SUPERUSER_PASS = os.environ.get("PB_PASSWORD", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))
WORK_ROOT = pathlib.Path(os.environ.get("WORK_ROOT", "worker_data")).resolve()
HERE = pathlib.Path(__file__).resolve().parent
MOCK_NEXT = pathlib.Path(os.environ.get("MOCK_NEXT", str(HERE / "pipeline" / "mock_next.py")))

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
    "llm_proofread_model": "proof_model",
    "llm_repair_model": "repair_model",
    "image_primary": "img_model",
    "image_fallback": "img_fallback_model",
    "tts_model": "tts_model",
    "tts_fallback_model": "tts_fallback_model",
    "tts_fallback_voice": "tts_fallback_voice",
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
    "audio_gap_ms": "tts_gap_ms",
    "sample_rate": "tts_rate",
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


def run_job(job):
    job_id = job["id"]
    client_id = job.get("client") or ""
    client_name = job.get("client_name") or client_id
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

    workdir = WORK_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "mocks").mkdir(parents=True, exist_ok=True)
    cfgfile = workdir / "config.json"
    cfgfile.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    patch_job(job_id, {
        "status": "running",
        "log": f"[worker] job started for {client_name} (count={cfg.get('question_count')}, "
               f"difficulty={cfg.get('difficulty_profile')})",
        "error": "",
    })

    env = dict(os.environ)
    env["MOCK_ROOT"] = str(workdir / "mocks")
    env["MOCK_CONFIG"] = str(cfgfile)
    cmd = [sys.executable, str(MOCK_NEXT), "--config", str(cfgfile)]

    log_tail = ""
    rc = -1
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        log_tail = stream_job(job_id, proc, log_tail)
        rc = proc.wait()
    except Exception as e:
        log_tail += f"\n[worker] subprocess error: {e}"
        log_tail = log_tail[-LOG_CAP:]

    report = read_report(workdir)

    if rc == 0:
        log(f"job {job_id}: DONE")
        patch_job(job_id, {
            "status": "done", "log": log_tail or "[worker] finished (no output)",
            "report": report, "pushed": True,
        })
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
