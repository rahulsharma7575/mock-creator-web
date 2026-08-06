#!/usr/bin/env python3
"""
mock_audio_builder.py — ONE tool to generate listening audio for any mock set.

Usage:
    uv run --with httpx mock_audio_builder.py --mock 9
    uv run --with httpx mock_audio_builder.py --mock 9 --workers 4 --gap 400
    uv run --with httpx mock_audio_builder.py --mock 9 --dry-run

Input : mock<N>/questions/mock<N>_questions.json  (see mock-blueprint.md, §4)
Output: mock<N>/audio/q<num>_<rand8>.mp3  +  mock<N>/audio/mock<N>_audio_map.json

Supports both audioScript styles:
  * array  : "audioScript": [{"voice": "V1", "text": "..."}, ...]
  * string : "audioScript": "남: ...\n여: ..."   (speaker labels -> voices)
Temp clips are hash-named per line, so edited scripts never merge stale audio.
Re-running the same mock = retry of failures only.
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SRC = Path(__file__).resolve().parent
BASE = Path(os.environ.get("MOCK_ROOT") or SRC)  # MOCK_ROOT override for container/web use


def load_env() -> None:
    """Load OPENROUTER_API_KEY from the .env file next to this script (if not already set)."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    env_file = SRC / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "OPENROUTER_API_KEY":
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ["OPENROUTER_API_KEY"] = v
            return

# Fish Audio FREE voices (model: fish-audio/s2.1-pro-free:free — the :free suffix
# routes to the no-cost endpoint on OpenRouter; never drop it)
VOICES = {
    "V1": "933563129e564b19a115bedd57b7406a",  # Sarah — young female
    "V2": "ca3007f96ae7499ab87d27ea3599956a",  # E-girl — young female
    "V3": "9a9cf47702da476aa4629e2506d4a857",  # Hannah — middle-aged female
    "V4": "802e3bc2b27e49c2995d23ef70e6ac89",  # Energetic male
}

# Speaker labels (mock3-style string scripts) -> voice key
LABEL_VOICES = {
    "남": "V4", "환자": "V4", "손님": "V4",
    "여": "V2", "간호사": "V1", "점원": "V2", "상담원": "V1",
    "종업원": "V2", "방송": "V3", "안내": "V3",
}

MODEL = "fish-audio/s2.1-pro-free:free"  # :free suffix = FREE endpoint, no cost
DEFAULT_GAP_MS = 400
SAMPLE_RATE = 44100
TMP = Path(os.environ.get("MOCK_TMP") or Path(tempfile.gettempdir()) / "mock_audio_builder")

# TTS pipeline values (all editable per-client via the /creator/ GUI -> mock_config,
# or MOCK_CONFIG file / $MOCK_* env vars — same keys as mock_next.py)
TTS_DEFAULTS = {
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "microsoft/mai-voice-2-flash",
    "tts_fallback_voice": "ko-KR-Haena",
    "tts_rate": 44100,
    "tts_gap_ms": 400,
    "tts_workers": 4,
    "tts_voices": {},
}


def _coerce(default, raw):
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
    if isinstance(default, (list, dict)) and raw.startswith(("[", "{")):
        try:
            return json.loads(raw)
        except Exception:
            return default
    return raw


def load_tts_config(path=None) -> dict:
    """Merge $MOCK_CONFIG file + individual $MOCK_* env vars over TTS_DEFAULTS."""
    cfg = dict(TTS_DEFAULTS)
    path = path or os.environ.get("MOCK_CONFIG")
    if path and os.path.exists(path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for k in TTS_DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
        except Exception as e:
            print(f"WARNING: unreadable MOCK_CONFIG ({e}) — using TTS defaults", flush=True)
    for k in TTS_DEFAULTS:
        env = os.environ.get("MOCK_" + k.upper())
        if env is not None:
            cfg[k] = _coerce(TTS_DEFAULTS[k], env)
    return cfg


def rand8() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def find_ffmpeg() -> str:
    try:
        sys.path.insert(0, str(Path.home() / ".config" / "opencode" / "scripts"))
        import audio_convert  # noqa: F401
        return audio_convert.find_ffmpeg()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        sys.exit("ERROR: ffmpeg not found (install it or run inside the uv env with static-ffmpeg)")


def load_tts():
    """Return the global tts module (from ~/.config/opencode/scripts) or None."""
    try:
        scripts = Path.home() / ".config" / "opencode" / "scripts"
        sys.path.insert(0, str(scripts))
        import tts  # noqa: F401
        return tts
    except Exception:
        return None


def synth(tts, text: str, voice: str, out: Path, model: str,
          fallback_model: str = "", fallback_voice: str = "") -> bool:
    """Synthesize one clip. Uses the tts module directly; falls back to subprocess.
    On failure retries once with the fallback model/voice (config-driven)."""
    key = os.environ.get("OPENROUTER_API_KEY") or (tts.find_key(None) if tts else None)

    def try_one(m, v):
        if tts is not None:
            try:
                data = tts.make_speech(m, v, text, "mp3", key, None)
                out.write_bytes(data)
                return out.stat().st_size > 2000
            except Exception as e:
                print(f"    module synth ({m}) failed: {e}", flush=True)
        tts_path = Path.home() / ".config" / "opencode" / "scripts" / "tts.py"
        r = subprocess.run(
            [sys.executable, str(tts_path), "--model", m, "--voice", v,
             "--text", text, "--out", str(out)],
            capture_output=True, text=True, timeout=180,
        )
        return r.returncode == 0 and out.exists() and out.stat().st_size > 2000

    if try_one(model, voice):
        return True
    if fallback_model and fallback_model != model:
        print(f"    falling back to {fallback_model} (voice {fallback_voice})", flush=True)
        return try_one(fallback_model, fallback_voice or voice)
    return False


def parse_script(script):
    """Normalize an audioScript to a list of (voice_id, text).

    Accepts:
      [{"voice": "V1"|"<id>", "text": "..."}, ...]   (array style)
      "남: ...\n여: ..."                             (label-line style)
    """
    lines = []
    if isinstance(script, list):
        for ln in script:
            text = str(ln.get("text", "")).strip()
            if not text:
                continue
            voice = str(ln.get("voice", "V1")).strip()
            lines.append((VOICES.get(voice, voice), text))
    elif isinstance(script, str):
        import re
        for raw in script.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            m = re.match(r"^([^:：]+)[:：]\s*(.*)$", raw)
            if m:
                label, text = m.group(1).strip(), m.group(2).strip()
                if not text:
                    continue
                key = LABEL_VOICES.get(label, "V1")
                lines.append((VOICES[key], text))
            else:
                lines.append((VOICES["V1"], raw))
    return lines


def duration(ffprobe: str, path: Path) -> float:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def merge_clips(ffmpeg: str, clips: list, dest: Path, gap_ms: int) -> bool:
    """Merge clips into one audio with fixed gaps, precisely aligned (adelay + amix)."""
    if not clips:
        return False
    if len(clips) == 1:
        r = subprocess.run([ffmpeg, "-y", "-i", str(clips[0]), "-b:a", "128k", str(dest)],
                           capture_output=True)
        return r.returncode == 0 and dest.exists()

    ffprobe = str(Path(ffmpeg).parent / "ffprobe.exe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"

    durs = [duration(ffprobe, c) for c in clips]
    delays = [0]
    for i in range(1, len(clips)):
        delays.append(delays[i - 1] + durs[i - 1] + gap_ms / 1000.0)
    total = delays[-1] + durs[-1]

    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]
    parts = []
    for i in range(len(clips)):
        parts.append(f"[{i}:a]aresample={SAMPLE_RATE},adelay={int(delays[i] * 1000)}:all=1[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(clips))) + \
        f"amix=inputs={len(clips)}:normalize=0:dropout_transition=0,atrim=0:{total:.3f}[out]"
    fc = ";".join(parts) + ";" + mix
    r = subprocess.run(
        [ffmpeg, "-y", *inputs, "-filter_complex", fc, "-map", "[out]", "-b:a", "128k", str(dest)],
        capture_output=True,
    )
    return r.returncode == 0 and dest.exists()


def resolve_mock_paths(mock: int) -> tuple:
    """Resolve input questions file + output audio dir for a mock.

    New convention (preferred):
        Himal KB Questions Creator/mock<N>/questions/mock<N>_questions.json
        Himal KB Questions Creator/mock<N>/audio/            (output + map inside)
    Legacy fallback (mock8 reference data):
        Himal KB Questions Creator/mock<N>_questions.json  ->  Himal KB Questions Creator/mock<N>_audio/
    """
    new_q = BASE / f"mock{mock}" / "questions" / f"mock{mock}_questions.json"
    new_out = BASE / f"mock{mock}" / "audio"
    legacy_q = BASE / f"mock{mock}_questions.json"
    legacy_out = BASE / f"mock{mock}_audio"
    if new_q.exists():
        return new_q, new_out
    return legacy_q, legacy_out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate listening audio for a mock set (see mock-blueprint.md)")
    ap.add_argument("--mock", required=True, type=int, help="Mock number (reads mock<N>_questions.json)")
    ap.add_argument("--workers", type=int, default=0, help="Parallel TTS workers (default: config tts_workers=4)")
    ap.add_argument("--gap", type=int, default=0, help="Gap between dialogue lines in ms (default: config tts_gap_ms=400)")
    ap.add_argument("--out-dir", default="", help="Override output dir (default mock<N>/audio)")
    ap.add_argument("--dry-run", action="store_true", help="Report what would be synthesized, do nothing")
    args = ap.parse_args()

    cfg = load_tts_config()
    if args.workers <= 0:
        args.workers = int(cfg["tts_workers"])
    if args.gap <= 0:
        args.gap = int(cfg["tts_gap_ms"])
    global SAMPLE_RATE
    SAMPLE_RATE = int(cfg["tts_rate"])
    voices = cfg.get("tts_voices") or {}
    if isinstance(voices, dict):
        VOICES.update({k: v for k, v in voices.items() if v})

    qfile, out_dir = resolve_mock_paths(args.mock)
    if not qfile.exists():
        sys.exit(
            f"ERROR: {qfile.name} not found (looked in {qfile.parent}).\n"
            f"       Author it first — see mock-blueprint.md for the format.\n"
            f"       Expected: Himal KB Questions Creator/mock{args.mock}/questions/mock{args.mock}_questions.json"
        )
    if args.out_dir:
        out_dir = BASE / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    for stale in TMP.glob("q*_*.mp3"):
        stale.unlink()

    load_env()

    qs = json.loads(qfile.read_text(encoding="utf-8"))
    listening = [q for q in qs if str(q.get("section", "")).strip().lower() == "listening"]
    if not listening:
        sys.exit("No listening questions found in the questions file.")

    tts = load_tts()
    ffmpeg = find_ffmpeg()

    # report questions missing an audioScript (they would silently produce no audio)
    no_script = [q.get("number") for q in listening
                 if not (q.get("listening") or {}).get("audioScript")]
    if no_script:
        print(f"WARNING: {len(no_script)} listening question(s) missing audioScript: {no_script}")

    jobs, missing = [], 0
    for q in listening:
        num = q.get("number")
        if num is None:
            continue
        turns = parse_script(q.get("listening", {}).get("audioScript"))
        if not turns:
            missing += 1
        for i, (voice, text) in enumerate(turns):
            h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
            jobs.append((num, i, voice, text, TMP / f"q{num}_{i:02d}_{h}.mp3"))

    if not jobs:
        sys.exit("No audioScript lines found in the listening questions.")

    print(f"Mock {args.mock}: {len(listening)} listening questions "
          f"({len(no_script)} without script), {len(jobs)} clips to synthesize")
    if missing:
        print(f"  note: {missing} question(s) produced no turns — skipped")

    if args.dry_run:
        voices = {}
        for num, i, voice, text, _ in jobs:
            voices[voice] = voices.get(voice, 0) + 1
        print(f"[dry-run] would synthesize {len(jobs)} clips using {voices}")
        return

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_gen, j, tts, args, cfg): j for j in jobs}
        for fut in as_completed(futs):
            n, k, ok = fut.result()
            if ok:
                done += 1
            else:
                print(f"  FAIL q{n} line{k}", flush=True)
    print(f"Synthesized {done}/{len(jobs)} clips")

    by_num = {}
    for j in jobs:
        by_num.setdefault(j[0], []).append(j)

    results = {}
    ok_c, fail_c = 0, 0
    for num, group in by_num.items():
        group.sort(key=lambda j: j[1])
        clips = [j[4] for j in group if j[4].exists() and j[4].stat().st_size > 2000]
        if len(clips) != len(group):
            fail_c += 1
            continue
        dest = out_dir / f"q{num}_{rand8()}.mp3"
        if merge_clips(ffmpeg, clips, dest, args.gap):
            results[f"Q{num}"] = dest.name
            ok_c += 1
        else:
            fail_c += 1
            print(f"  CONCAT FAIL q{num}", flush=True)

    print(f"\nDONE: {ok_c} ok, {fail_c} fail")
    map_file = out_dir / f"mock{args.mock}_audio_map.json"
    map_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Map written -> {map_file}")


def _gen(job, tts, args, cfg):
    n, k, voice, text, out = job
    if out.exists() and out.stat().st_size > 2000:
        return (n, k, True)
    for attempt in range(3):
        try:
            if synth(tts, text, voice, out, cfg["tts_model"],
                     cfg.get("tts_fallback_model", ""), cfg.get("tts_fallback_voice", "")):
                print(f"  ok q{n}_{k:02d} ({out.stat().st_size} bytes)", flush=True)
                return (n, k, True)
        except SystemExit:
            print(f"    q{n}_{k:02d} attempt {attempt + 1}: TTS fatal error (check API key)", flush=True)
        except Exception as e:
            print(f"    q{n}_{k:02d} attempt {attempt + 1}: {e}", flush=True)
        time.sleep(3 * (attempt + 1))
    return (n, k, False)


if __name__ == "__main__":
    main()
