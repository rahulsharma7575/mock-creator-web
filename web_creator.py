#!/usr/bin/env python3
"""
web_creator.py — Web GUI + JSON API for the Himal KB mock creator pipeline.

Runs the pipeline (pipeline/mock_next.py) as a background subprocess with
streaming logs. One job at a time.

Endpoints (all JSON except /):
  GET  /                      -> dashboard (web_dashboard.html)
  GET  /api/health            -> {"ok": true}                     (no auth)
  POST /api/start             -> start a job                      (Basic auth)
        body: {mock?: int, author_model?, proof_model?, img_model?, dry_run?: bool}
  GET  /api/status            -> job state + log tail             (Basic auth)
  GET  /api/logs?offset=N     -> incremental log lines            (Basic auth)

Auth: HTTP Basic — ADMIN_USER / ADMIN_PASSWORD env vars (defaults admin/changeme).
Run:  python web_creator.py  (PORT env, default 33445)
"""
import base64
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time

APP_DIR = pathlib.Path(__file__).resolve().parent
PIPELINE = APP_DIR / "pipeline" / "mock_next.py"
DATA_DIR = pathlib.Path(os.environ.get("MOCK_ROOT") or (APP_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
if "ADMIN_PASSWORD" not in os.environ:
    print("WARNING: ADMIN_PASSWORD not set — using default 'changeme'. Set it in .env!")

DEFAULT_AUTHOR = "google/gemini-2.5-flash"
DEFAULT_PROOF = "qwen/qwen3.5-flash-02-23"
DEFAULT_IMG = "z-image"

# ---------------------------------------------------------------------------
# Job state (single worker)
# ---------------------------------------------------------------------------

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"          # idle | running | done | failed
        self.job_id = None
        self.mock = None             # requested mock number (None = auto next)
        self.actual_mock = None      # parsed from pipeline output
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.lines = []
        self.proc = None
        self.log_file = None

    def tail(self, n=100):
        with self.lock:
            return self.lines[-n:]

    def since(self, offset):
        with self.lock:
            lines = self.lines[offset:]
            return offset + len(lines), lines


JOB = Job()
CR_MOCK = re.compile(r"=== Creating Mock (\d+) ===")


def _log(line):
    line = line.rstrip("\n")
    with JOB.lock:
        JOB.lines.append(line)
        if len(JOB.lines) > 5000:
            JOB.lines = JOB.lines[-4000:]
    if JOB.log_file:
        try:
            with open(JOB.log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _reader(stream):
    try:
        for raw in iter(stream.readline, b""):
            _log(raw.decode("utf-8", errors="replace"))
            m = CR_MOCK.search(raw.decode("utf-8", errors="replace"))
            if m:
                JOB.actual_mock = int(m.group(1))
    except Exception as e:  # stream closed
        _log(f"[web] log reader stopped: {e}")


def start_job(mock, author, proof, img_model, dry_run):
    if JOB.state == "running":
        return False, "A job is already running (state=running)"
    if not PIPELINE.exists():
        return False, f"pipeline script not found: {PIPELINE}"

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"job_{int(time.time())}_{os.getpid()}"
    JOB.log_file = JOBS_DIR / f"{job_id}.log"
    JOB.state = "running"
    JOB.job_id = job_id
    JOB.mock = mock
    JOB.actual_mock = None
    JOB.started_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    JOB.finished_at = None
    JOB.exit_code = None
    JOB.lines = []
    JOB.log_file.parent.mkdir(parents=True, exist_ok=True)
    JOB.log_file.write_text("", encoding="utf-8")

    cmd = [sys.executable, str(PIPELINE),
           "--author-slug", author, "--proof-slug", proof, "--img-model", img_model]
    if mock:
        cmd += ["--mock", str(int(mock))]
    if dry_run:
        cmd += ["--dry-run"]
    env = dict(os.environ)
    env["MOCK_ROOT"] = str(DATA_DIR)

    _log(f"[web] starting job {job_id}: {' '.join(cmd)}")
    _log(f"[web] mock={mock or 'auto-next'} author={author} proof={proof} img={img_model} dry_run={dry_run}")

    JOB.proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(APP_DIR), env=env,
    )
    threading.Thread(target=_reader, args=(JOB.proc.stdout,), daemon=True).start()

    def _waiter():
        rc = JOB.proc.wait()
        JOB.exit_code = rc
        JOB.state = "failed" if rc != 0 else "done"
        JOB.finished_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        _log(f"[web] job finished rc={rc} state={JOB.state}")
        JOB.proc = None

    threading.Thread(target=_waiter, daemon=True).start()
    return True, job_id


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def check_auth(authorization: str | None):
    ok = False
    if authorization and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[6:]).decode("utf-8", "replace")
            user, _, pwd = decoded.partition(":")
            ok = secrets.compare_digest(user, ADMIN_USER) and secrets.compare_digest(pwd, ADMIN_PASSWORD)
        except Exception:
            ok = False
    if not ok:
        from fastapi import HTTPException
        raise HTTPException(401, detail="Unauthorized", headers={"WWW-Authenticate": 'Basic realm="mock-creator"'})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app():
    from fastapi import FastAPI, Header, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel, Field

    app = FastAPI(title="Himal KB Mock Creator")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["WWW-Authenticate"],
    )

    class StartBody(BaseModel):
        mock: int | None = Field(default=None, ge=1, le=999)
        author_model: str = DEFAULT_AUTHOR
        proof_model: str = DEFAULT_PROOF
        img_model: str = DEFAULT_IMG
        dry_run: bool = False

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "mock-creator"}

    @app.get("/")
    def index(request: Request):
        check_auth(request.headers.get("authorization"))
        return FileResponse(APP_DIR / "web_dashboard.html")

    @app.get("/api/status")
    def status(authorization: str | None = Header(default=None)):
        check_auth(authorization)
        report = None
        m = JOB.actual_mock or JOB.mock
        if JOB.state in ("done", "failed") and m:
            rp = DATA_DIR / f"mock{m}" / "extra" / "run_report.json"
            if rp.exists():
                try:
                    report = json.loads(rp.read_text(encoding="utf-8"))
                except Exception:
                    report = None
        return {
            "state": JOB.state,
            "job_id": JOB.job_id,
            "mock": JOB.mock,
            "actual_mock": JOB.actual_mock,
            "started_at": JOB.started_at,
            "finished_at": JOB.finished_at,
            "exit_code": JOB.exit_code,
            "report": report,
            "tail": JOB.tail(100),
            "offset": len(JOB.lines),
        }

    @app.get("/api/logs")
    def logs(offset: int = Query(default=0, ge=0), authorization: str | None = Header(default=None)):
        check_auth(authorization)
        new_offset, lines = JOB.since(offset)
        return {"offset": new_offset, "lines": lines}

    @app.post("/api/start")
    def start(body: StartBody, authorization: str | None = Header(default=None)):
        check_auth(authorization)
        ok, msg = start_job(body.mock, body.author_model, body.proof_model, body.img_model, body.dry_run)
        if not ok:
            raise HTTPException(409, detail=msg)
        return {"started": True, "job_id": msg, "state": "running"}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "33445"))
    print(f"Himal KB Mock Creator — http://0.0.0.0:{port}  (login: {ADMIN_USER})")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
