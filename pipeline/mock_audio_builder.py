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
import threading
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

# Runtime counters (thread-safe) for the audio_stats report written per run.
_FALLBACK_LOCK = threading.Lock()
FALLBACK_CLIPS = 0
SHORT_CLIPS = 0
MIN_MERGED_SECONDS = 2.5    # merged question clip shorter than this = invalid, not uploaded
MIN_CLIP_SECONDS = 1.5      # individual turn clip shorter than this = warning

# TTS pipeline values (all editable per-client via the /creator/ GUI -> mock_config,
# or MOCK_CONFIG file / $MOCK_* env vars — same keys as mock_next.py)
TTS_DEFAULTS = {
    "tts_model": "fish-audio/s2.1-pro-free:free",
    "tts_fallback_model": "deepgram/flux-tts:free",
    "tts_fallback_voice": "",
    "tts_rate": 44100,
    "tts_gap_ms": 400,
    "tts_speed": 1.0,    # speech speed (0.5-2.0); 1.0 = normal
    "tts_natural_pacing": False,   # relaxed 0.92 speed + >=450ms gaps + extra pause after ?/!
    "tts_polish": False,           # loudnorm + highpass + fades on the merged clip
    "tts_atempo_models": "",       # comma list of model fragments where speed is forced via ffmpeg atempo
    "tts_male_speed": 0.0,         # per-voice speed (0 = follow tts_speed)
    "tts_female_speed": 0.0,
    "tts_fallback_male_speed": 0.0,
    "tts_fallback_female_speed": 0.0,
    "tts_workers": 4,
    "tts_voices": {},
    "tts_male_voice": "",    # PDF dialogue speaker V1 (male) - empty = auto per model
    "tts_female_voice": "",  # PDF dialogue speaker V2 (female) - empty = auto per model
    "tts_fallback_male_voice": "",    # V1 voice when the fallback model is used
    "tts_fallback_female_voice": "",  # V2 voice when the fallback model is used
}

# Auto speaker voices per TTS model family (used for PDF-mode dialogues):
# fish-audio free endpoint -> free named voices (V1 = Energetic male, V2 = Hannah female)
# MAI Voice 2 -> native Korean male/female voices (NOTE: OpenRouter's MAI flash
# endpoint currently supports only 4 en/eu voices - ko-KR entries are legacy)
# Deepgram Flux -> free en voices
# NOTE: the fish female default MUST match the /creator UI placeholder
# (voicePh -> 9a9cf47702da476aa4629e2506d4a857). Keep them in sync.
DIALOGUE_VOICE_DEFAULTS = {
    "fish": {"V1": "802e3bc2b27e49c2995d23ef70e6ac89", "V2": "9a9cf47702da476aa4629e2506d4a857"},
    "mai": {"V1": "de-DE-Klaus:MAI-Voice-2", "V2": "en-US-Harper:MAI-Voice-2"},
    "flux": {"V1": "flux-bruce-en", "V2": "flux-alexis-en"},
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
    """Return the global tts module (global scripts dir or repo scripts dir) or None."""
    candidates = [Path.home() / ".config" / "opencode" / "scripts",
                  Path(__file__).resolve().parent.parent / "scripts"]
    for scripts in candidates:
        try:
            sys.path.insert(0, str(scripts))
            import tts  # noqa: F401
            return tts
        except Exception:
            continue
    return None


def resolve_voice(model, voice, cfg, tts_mod, use_fallback_voice=False):
    """Pick a voice that is VALID for the chosen model.

    Speaker labels V1-V4 map to fish-audio UUID voices - which only fish
    models accept. For every other model: tts_voices map > (fallback voice,
    only for the fallback model) > explicit configured voice passed through
    AS-IS (the API validates; make_speech strips an invalid voice on 400) >
    first voice in the TTS catalog > provider default. A concrete voice is
    NEVER silently swapped for a catalog voice."""
    if not model:
        return model, voice
    try:
        tts_voices = cfg.get("tts_voices") or {}
        if voice in tts_voices:
            return model, str(tts_voices[voice])
        if "fish" in model:
            return model, voice
        if use_fallback_voice:
            fb = str(cfg.get("tts_fallback_voice") or "").strip()
            if fb:
                return model, fb
    except Exception:
        pass
    v = str(voice or "").strip()
    if v and v != "default":
        return model, v
    try:
        if tts_mod is not None:
            vs = tts_mod.get_voices(model)
            if vs:
                return model, vs[0]
    except Exception:
        pass
    return model, "default"


def synth(tts, text: str, voice: str, out: Path, model: str,
          fallback_model: str = "", fallback_voice: str = "",
          speed: float = 1.0, fallback_speed: float = 1.0, atempo_models: str = "") -> bool:
    """Synthesize one clip. Uses the tts module directly; falls back to subprocess.
    On failure retries once with the fallback model/voice (config-driven).
    Models listed in atempo_models get speed forced via ffmpeg atempo (pitch-preserving)."""
    key = os.environ.get("OPENROUTER_API_KEY") or (tts.find_key(None) if tts else None)
    atempo_list = [f.strip().lower() for f in (atempo_models or "").split(",") if f.strip()]

    def force_speed(m, spd, path):
        if spd == 1.0 or not atempo_list:
            return
        ml = m.lower()
        if not any(frag in ml for frag in atempo_list):
            return
        try:
            ffmpeg = find_ffmpeg()
            if _apply_atempo(str(ffmpeg), path, spd):
                print(f"    speed forced via atempo ({spd})", flush=True)
            else:
                print("    atempo failed - keeping provider speed", flush=True)
        except Exception as e:
            print(f"    atempo error: {e}", flush=True)

    def try_one(m, v, spd):
        if tts is not None:
            try:
                data = tts.make_speech(m, v, text, "mp3", key, spd)
                out.write_bytes(data)
                if out.stat().st_size > 2000:
                    force_speed(m, spd, out)
                    return True
            except Exception as e:
                print(f"    module synth ({m}) failed: {e}", flush=True)
        tts_path = Path.home() / ".config" / "opencode" / "scripts" / "tts.py"
        cmd = [sys.executable, str(tts_path), "--model", m, "--voice", v,
               "--text", text, "--out", str(out)]
        if spd and spd != 1.0:
            cmd += ["--speed", str(spd)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 2000:
            force_speed(m, spd, out)
            return True
        return False

    if try_one(model, voice, speed):
        return True
    if fallback_model and fallback_model != model:
        print(f"    falling back to {fallback_model} (voice {fallback_voice})", flush=True)
        ok = try_one(fallback_model, fallback_voice or voice, fallback_speed)
        if ok:
            global FALLBACK_CLIPS
            with _FALLBACK_LOCK:
                FALLBACK_CLIPS += 1
        return ok
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


def merge_clips(ffmpeg: str, clips: list, dest: Path, gap_ms: int, gaps: list = None) -> bool:
    """Merge clips into one audio with fixed gaps (plus optional per-boundary extras),
    precisely aligned (adelay + amix)."""
    gaps = gaps or []
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
        extra = (gaps[i - 1] if i - 1 < len(gaps) else 0) or 0
        delays.append(delays[i - 1] + durs[i - 1] + (gap_ms + extra) / 1000.0)
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


def _apply_atempo(ffmpeg: str, path: Path, speed: float) -> bool:
    """Pitch-preserving speed change (ffmpeg atempo) - forces speed on models that
    ignore the provider-side speed parameter (e.g. grok voice)."""
    tmp = path.with_suffix(path.suffix + ".tmp.mp3")
    r = subprocess.run(
        [ffmpeg, "-y", "-i", str(path), "-filter:a", "atempo=%.3f" % speed, "-b:a", "128k", str(tmp)],
        capture_output=True)
    if r.returncode != 0 or not tmp.exists():
        return False
    tmp.replace(path)
    return True


def _polish_mp3(ffmpeg: str, path: Path) -> bool:
    """Audio polish post-pass: loudness normalization + rumble high-pass + click-free fades."""
    tmp = path.with_suffix(path.suffix + ".tmp.mp3")
    r = subprocess.run(
        [ffmpeg, "-y", "-i", str(path),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,highpass=f=60,"
                "afade=t=in:st=0:d=0.015,areverse,afade=t=in:st=0:d=0.015,areverse",
         "-b:a", "128k", str(tmp)],
        capture_output=True)
    if r.returncode != 0 or not tmp.exists():
        return False
    tmp.replace(path)
    return True


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

    # PDF-dialogue speaker voices: explicit config > per-model default > stock VOICES.
    # Order matters: the tts_voices map below (explicit per-speaker overrides) wins last.
    model_l = str(cfg.get("tts_model") or "").lower()
    fam = "flux" if ("flux" in model_l or "deepgram" in model_l) else ("mai" if ("mai-voice" in model_l) else ("fish" if ("fish" in model_l or "s2.1" in model_l) else ""))
    fam_defaults = DIALOGUE_VOICE_DEFAULTS.get(fam, {})
    for key, cfg_key in (("V1", "tts_male_voice"), ("V2", "tts_female_voice")):
        v = str(cfg.get(cfg_key) or "").strip()
        if not v:
            v = fam_defaults.get(key, "")
        if v:
            VOICES[key] = v
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
        futs = {pool.submit(_gen, j, tts, args, cfg, ffmpeg): j for j in jobs}
        for fut in as_completed(futs):
            n, k, ok = fut.result()
            if ok:
                done += 1
            else:
                print(f"  FAIL q{n} line{k}", flush=True)
    print(f"Synthesized {done}/{len(jobs)} clips")

    # natural pacing preset: relaxed speed when untouched, bigger minimum gap,
    # extra pause after ? / ! turns
    natural = bool(cfg.get("tts_natural_pacing"))
    if natural:
        if float(cfg.get("tts_speed") or 1.0) == 1.0:
            cfg["tts_speed"] = 0.92
            print("[audio] natural pacing: speed -> 0.92 (explicit speed always wins)", flush=True)
        if args.gap < 450:
            args.gap = 450
            print(f"[audio] natural pacing: gap -> 450ms", flush=True)
    polish = bool(cfg.get("tts_polish"))

    by_num = {}
    for j in jobs:
        by_num.setdefault(j[0], []).append(j)

    results = {}
    ok_c, fail_c = 0, 0
    ffprobe = str(Path(ffmpeg).parent / "ffprobe.exe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    for num, group in by_num.items():
        group.sort(key=lambda j: j[1])
        clips = [j[4] for j in group if j[4].exists() and j[4].stat().st_size > 2000]
        if len(clips) != len(group):
            fail_c += 1
            continue
        # duration guards: individual clips under MIN_CLIP_SECONDS are warned;
        # a merged question clip under MIN_MERGED_SECONDS is INVALID (1s audio)
        # and never uploaded.
        durs = [duration(ffprobe, c) for c in clips]
        if any(d < MIN_CLIP_SECONDS for d in durs):
            global SHORT_CLIPS
            with _FALLBACK_LOCK:
                SHORT_CLIPS += 1
            print(f"  WARN q{num}: short clip(s) {[round(d, 1) for d in durs]}s "
                  f"(min {MIN_CLIP_SECONDS}s) - script may be too short", flush=True)
        if sum(durs) < MIN_MERGED_SECONDS:
            fail_c += 1
            print(f"  WARN q{num}: merged audio only {sum(durs):.1f}s (< {MIN_MERGED_SECONDS}s) - "
                  f"INVALID, not uploaded (script too short)", flush=True)
            continue
        extras = []
        if natural:
            for i, j in enumerate(group[:-1]):
                t = str(j[3] or "").rstrip()
                extras.append(120 if t.endswith(("?", "!")) else 0)
        dest = out_dir / f"q{num}_{rand8()}.mp3"
        if merge_clips(ffmpeg, clips, dest, args.gap, extras):
            if polish:
                if _polish_mp3(ffmpeg, dest):
                    print(f"  polish q{num} OK", flush=True)
                else:
                    print(f"  polish q{num} failed - keeping raw merge", flush=True)
            results[f"Q{num}"] = dest.name
            ok_c += 1
        else:
            fail_c += 1
            print(f"  CONCAT FAIL q{num}", flush=True)

    print(f"\nDONE: {ok_c} ok, {fail_c} fail")
    map_file = out_dir / f"mock{args.mock}_audio_map.json"
    map_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Map written -> {map_file}")
    stats_file = out_dir / f"mock{args.mock}_audio_stats.json"
    stats_file.write_text(json.dumps({
        "ok": ok_c, "fail": fail_c,
        "fallback_clips": FALLBACK_CLIPS,
        "short_clips": SHORT_CLIPS,
    }), encoding="utf-8")
    print(f"Stats written -> {stats_file} (fallback clips: {FALLBACK_CLIPS}, short clips: {SHORT_CLIPS})")


def _gen(job, tts, args, cfg, ffmpeg):
    n, k, voice, text, out = job
    if out.exists() and out.stat().st_size > 2000:
        return (n, k, True)
    raw_voice = voice
    model, voice = resolve_voice(cfg["tts_model"], voice, cfg, tts)
    fallback_model = cfg.get("tts_fallback_model", "")
    fallback_voice = ""
    if fallback_model and fallback_model != model:
        key = next((vk for vk, vid in VOICES.items() if str(vid) == str(raw_voice)), "")
        fm_l = str(fallback_model).lower()
        f_fam = "flux" if ("flux" in fm_l or "deepgram" in fm_l) else ("mai" if "mai-voice" in fm_l else ("fish" if ("fish" in fm_l or "s2.1" in fm_l) else ""))
        f_defs = DIALOGUE_VOICE_DEFAULTS.get(f_fam, {})
        if key == "V1":
            fv = str(cfg.get("tts_fallback_male_voice") or "").strip() or f_defs.get("V1", "") or str(cfg.get("tts_fallback_voice") or "").strip()
        elif key == "V2":
            fv = str(cfg.get("tts_fallback_female_voice") or "").strip() or f_defs.get("V2", "") or str(cfg.get("tts_fallback_voice") or "").strip()
        else:
            fv = str(cfg.get("tts_fallback_voice") or "").strip()
        if fv:
            fallback_voice = resolve_voice(fallback_model, fv, cfg, tts)[1]
    for attempt in range(3):
        try:
            # per-voice speed: explicit voice speed wins, else global tts_speed
            key = next((vk for vk, vid in VOICES.items() if str(vid) == str(raw_voice)), "")
            base_spd = float(cfg.get("tts_speed") or 1.0)
            spd = base_spd
            fspd = base_spd
            if key == "V1":
                spd = float(cfg.get("tts_male_speed") or 0) or base_spd
                fspd = float(cfg.get("tts_fallback_male_speed") or 0) or base_spd
            elif key == "V2":
                spd = float(cfg.get("tts_female_speed") or 0) or base_spd
                fspd = float(cfg.get("tts_fallback_female_speed") or 0) or base_spd
            spd = max(0.5, min(2.0, spd))
            fspd = max(0.5, min(2.0, fspd))
            if synth(tts, text, voice, out, model, fallback_model, fallback_voice,
                     speed=spd, fallback_speed=fspd,
                     atempo_models=str(cfg.get("tts_atempo_models") or "")):
                print(f"  ok q{n}_{k:02d} ({out.stat().st_size} bytes, voice speed {spd})", flush=True)
                return (n, k, True)
        except SystemExit as se:
            msg = str(se.code) if se.code else "TTS fatal error"
            print(f"    q{n}_{k:02d} attempt {attempt + 1}: {msg}", flush=True)
        except Exception as e:
            print(f"    q{n}_{k:02d} attempt {attempt + 1}: {e}", flush=True)
        time.sleep(3 * (attempt + 1))
    return (n, k, False)


if __name__ == "__main__":
    main()
