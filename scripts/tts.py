#!/usr/bin/env python3
"""
Universal OpenRouter TTS script - works with ANY TTS model
(x-ai/grok-voice-tts-1.0, google/gemini-3.1-flash-tts-preview, fish-audio/*, etc.)

Usage:
  uv run --with httpx tts.py --text "hello world" --voice ara --model x-ai/grok-voice-tts-1.0
  uv run --with httpx tts.py --file poem.txt --out poem.mp3 --voice eve
  uv run --with httpx tts.py --voices --model x-ai/grok-voice-tts-1.0     # list voices
  uv run --with httpx tts.py --check                                       # check key/limit status

Options:
  --model        TTS model slug (default: x-ai/grok-voice-tts-1.0)
  --voice        Voice id/name (auto-picks first available if omitted)
  --text         Inline text to speak
  --file         Read text from file (UTF-8)
  --out          Output file (default: tts_output.mp3)
  --format       mp3 | pcm | wav (default: mp3)
  --voices       List available voices for the model, then exit
  --check        Check API key + daily limit, then exit
  --key          API key override (default: OPENROUTER_API_KEY env, then opencode config)
  --speed        Playback speed (1.0 = normal, only if model supports it)
  --chunk        Max chars per request for long text (default: 8000, auto-split + concat)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

API = "https://openrouter.ai/api/v1"
CONFIG_PATHS = [
    os.path.expanduser("~/.config/opencode/opencode.jsonc"),
    os.path.expanduser("~/.config/opencode/opencode.json"),
]

# Known TTS models + voices (REST /models API does not list TTS models).
# Add more as needed - unknown models still work, just pass --voice explicitly.
KNOWN_MODELS: dict[str, list[str]] = {
    "x-ai/grok-voice-tts-1.0": ["eve", "ara", "rex", "sal", "leo"],
    "google/gemini-3.1-flash-tts-preview": [
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
        "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    ],
    "microsoft/mai-voice-2": [
        "en-US-Harper:MAI-Voice-2", "es-MX-Valeria:MAI-Voice-2", "fr-FR-Soleil:MAI-Voice-2", "de-DE-Klaus:MAI-Voice-2",
        "de-DE-Mia:MAI-Voice-2", "en-AU-Isla:MAI-Voice-2", "en-US-Ethan:MAI-Voice-2", "en-US-Olivia:MAI-Voice-2",
        "es-ES-Marta:MAI-Voice-2", "es-MX-Alejo:MAI-Voice-2", "fr-FR-Marc:MAI-Voice-2", "hi-IN-Arjun:MAI-Voice-2",
        "hi-IN-Dhruv:MAI-Voice-2", "hi-IN-Kavya:MAI-Voice-2", "hi-IN-Priya:MAI-Voice-2", "hu-HU-Bence:MAI-Voice-2",
        "hu-HU-Levente:MAI-Voice-2", "hu-HU-Lilla:MAI-Voice-2", "hu-HU-Réka:MAI-Voice-2", "it-IT-Luca:MAI-Voice-2",
        "it-IT-Rosa:MAI-Voice-2", "ko-KR-Haena:MAI-Voice-2", "ko-KR-Junho:MAI-Voice-2", "nl-NL-Sander:MAI-Voice-2",
        "pt-BR-Caio:MAI-Voice-2", "pt-BR-Luana:MAI-Voice-2", "pt-BR-Pedro:MAI-Voice-2", "pt-BR-Rafael:MAI-Voice-2",
        "pt-PT-Rui:MAI-Voice-2", "ro-RO-Andrei:MAI-Voice-2", "ro-RO-Elena:MAI-Voice-2", "ro-RO-Ioana:MAI-Voice-2",
        "ro-RO-Radu:MAI-Voice-2", "ru-RU-Lev:MAI-Voice-2", "ru-RU-Masha:MAI-Voice-2", "th-TH-Krit:MAI-Voice-2",
        "th-TH-Nattapong:MAI-Voice-2", "tr-TR-Aydın:MAI-Voice-2", "tr-TR-Elif:MAI-Voice-2", "zh-CN-Bo:MAI-Voice-2",
        "zh-CN-Lan:MAI-Voice-2", "zh-CN-Mei:MAI-Voice-2", "zh-CN-Wei:MAI-Voice-2",
    ],
    "microsoft/mai-voice-2-flash": [
        "en-US-Harper:MAI-Voice-2", "es-MX-Valeria:MAI-Voice-2", "fr-FR-Soleil:MAI-Voice-2", "de-DE-Klaus:MAI-Voice-2",
        "de-DE-Mia:MAI-Voice-2", "en-AU-Isla:MAI-Voice-2", "en-US-Ethan:MAI-Voice-2", "en-US-Olivia:MAI-Voice-2",
        "es-ES-Marta:MAI-Voice-2", "es-MX-Alejo:MAI-Voice-2", "fr-FR-Marc:MAI-Voice-2", "hi-IN-Arjun:MAI-Voice-2",
        "hi-IN-Dhruv:MAI-Voice-2", "hi-IN-Kavya:MAI-Voice-2", "hi-IN-Priya:MAI-Voice-2", "hu-HU-Bence:MAI-Voice-2",
        "hu-HU-Levente:MAI-Voice-2", "hu-HU-Lilla:MAI-Voice-2", "hu-HU-Réka:MAI-Voice-2", "it-IT-Luca:MAI-Voice-2",
        "it-IT-Rosa:MAI-Voice-2", "ko-KR-Haena:MAI-Voice-2", "ko-KR-Junho:MAI-Voice-2", "nl-NL-Sander:MAI-Voice-2",
        "pt-BR-Caio:MAI-Voice-2", "pt-BR-Luana:MAI-Voice-2", "pt-BR-Pedro:MAI-Voice-2", "pt-BR-Rafael:MAI-Voice-2",
        "pt-PT-Rui:MAI-Voice-2", "ro-RO-Andrei:MAI-Voice-2", "ro-RO-Elena:MAI-Voice-2", "ro-RO-Ioana:MAI-Voice-2",
        "ro-RO-Radu:MAI-Voice-2", "ru-RU-Lev:MAI-Voice-2", "ru-RU-Masha:MAI-Voice-2", "th-TH-Krit:MAI-Voice-2",
        "th-TH-Nattapong:MAI-Voice-2", "tr-TR-Aydın:MAI-Voice-2", "tr-TR-Elif:MAI-Voice-2", "zh-CN-Bo:MAI-Voice-2",
        "zh-CN-Lan:MAI-Voice-2", "zh-CN-Mei:MAI-Voice-2", "zh-CN-Wei:MAI-Voice-2",
    ],
    "qwen/qwen-audio-3.0-tts-flash": ["loongjohn", "longanhuan_v3.6"],
    "qwen/qwen-audio-3.0-tts-plus": ["longanlingxin", "longanlufeng"],
    "zyphra/zonos-v0.1-transformer": ["american_female", "american_male", "british_female", "british_male", "random"],
    "zyphra/zonos-v0.1-hybrid": ["american_female", "american_male", "british_female", "british_male", "random"],
    "canopylabs/orpheus-3b-0.1-ft": ["tara", "leah", "jess", "leo", "dan", "mia", "zac"],
    "sesame/csm-1b": ["conversational_a", "conversational_b", "read_speech_a", "read_speech_b", "read_speech_c", "read_speech_d", "none"],
    "hexgrad/kokoro-82m": [
        "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
        "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam",
        "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx",
        "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis", "ef_dora", "em_alex",
        "em_santa", "ff_siwis", "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
        "if_sara", "im_nicola", "jf_alpha", "jf_gongitsune", "jf_nezumi",
        "jf_tebukuro", "jm_kumo", "pf_dora", "pm_alex", "pm_santa", "zf_xiaobei",
        "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi",
        "zm_yunxia", "zm_yunyang",
    ],
    "mistralai/voxtral-mini-tts-2603": [
        "en_paul_sad", "en_paul_neutral", "en_paul_happy", "en_paul_frustrated",
        "en_paul_excited", "en_paul_confident", "en_paul_cheerful", "en_paul_angry",
        "gb_oliver_neutral", "gb_oliver_sad", "gb_oliver_excited", "gb_oliver_curious",
        "gb_oliver_confident", "gb_oliver_cheerful", "gb_oliver_angry", "gb_jane_sarcasm",
        "gb_jane_confused", "gb_jane_shameful", "gb_jane_sad", "gb_jane_neutral",
        "gb_jane_jealousy", "gb_jane_frustrated", "gb_jane_curious", "gb_jane_confident",
        "fr_marie_sad", "fr_marie_neutral", "fr_marie_happy", "fr_marie_excited",
        "fr_marie_curious", "fr_marie_angry",
    ],
    "deepgram/aura-2": [
        "aura-2-thalia-en", "aura-2-agathe-fr", "aura-2-agustina-es", "aura-2-alvaro-es",
        "aura-2-ama-ja", "aura-2-amalthea-en", "aura-2-andromeda-en", "aura-2-antonia-es",
        "aura-2-apollo-en", "aura-2-aquila-es", "aura-2-arcas-en", "aura-2-aries-en",
        "aura-2-asteria-en", "aura-2-athena-en", "aura-2-atlas-en", "aura-2-aurelia-de",
        "aura-2-aurora-en", "aura-2-beatrix-nl", "aura-2-callista-en", "aura-2-carina-es",
        "aura-2-celeste-es", "aura-2-cesare-it", "aura-2-cinzia-it", "aura-2-cora-en",
        "aura-2-cordelia-en", "aura-2-cornelia-nl", "aura-2-daphne-nl", "aura-2-delia-en",
        "aura-2-demetra-it", "aura-2-diana-es", "aura-2-dionisio-it", "aura-2-draco-en",
        "aura-2-ebisu-ja", "aura-2-elara-de", "aura-2-electra-en", "aura-2-elio-it",
        "aura-2-estrella-es", "aura-2-fabian-de", "aura-2-flavio-it", "aura-2-fujin-ja",
        "aura-2-gloria-es", "aura-2-harmonia-en", "aura-2-hector-fr", "aura-2-helena-en",
        "aura-2-hera-en", "aura-2-hermes-en", "aura-2-hestia-nl", "aura-2-hyperion-en",
        "aura-2-iris-en", "aura-2-izanami-ja", "aura-2-janus-en", "aura-2-javier-es",
        "aura-2-julius-de", "aura-2-juno-en", "aura-2-jupiter-en", "aura-2-kara-de",
        "aura-2-lara-de", "aura-2-lars-nl", "aura-2-leda-nl", "aura-2-livia-it",
        "aura-2-luciano-es", "aura-2-luna-en", "aura-2-maia-it", "aura-2-mars-en",
        "aura-2-melia-it", "aura-2-minerva-en", "aura-2-neptune-en", "aura-2-nestor-es",
        "aura-2-odysseus-en", "aura-2-olivia-es", "aura-2-ophelia-en", "aura-2-orion-en",
        "aura-2-orpheus-en", "aura-2-pandora-en", "aura-2-phoebe-en", "aura-2-pluto-en",
        "aura-2-rhea-nl", "aura-2-roman-nl", "aura-2-sander-nl", "aura-2-saturn-en",
        "aura-2-selena-es", "aura-2-selene-en", "aura-2-silvia-es", "aura-2-sirio-es",
        "aura-2-theia-en", "aura-2-uzume-ja", "aura-2-valerio-es", "aura-2-vesta-en",
        "aura-2-viktoria-de", "aura-2-zeus-en",
    ],
    "fish-audio/s2.1-pro-free": [
        "933563129e564b19a115bedd57b7406a",  # Sarah - Female, Young, Conversational
        "ca3007f96ae7499ab87d27ea3599956a",  # E-girl - Female, Young, egirl
        "9a9cf47702da476aa4629e2506d4a857",  # Hannah - Female, Middle Aged, Advertisement
        "802e3bc2b27e49c2995d23ef70e6ac89",  # Energetic Male - Male, Young, Social Media
    ],
}


# Sample rates + ffmpeg lookup are the single source of truth in audio_convert.py
# (same directory). Keeps the two scripts from drifting apart.
import audio_convert

KNOWN_RATES: dict[str, int] = audio_convert.MODEL_SAMPLE_RATES
find_ffmpeg = audio_convert.find_ffmpeg


def model_rate(model: str) -> int | None:
    """Substring-match the model slug against the shared rate table (like audio_convert)."""
    for key, rate in KNOWN_RATES.items():
        if key in model:
            return rate
    return None


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments without touching // inside strings."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def find_key(override: str | None) -> str:
    if override:
        return override
    env = os.environ.get("OPENROUTER_API_KEY")
    if env:
        return env
    for path in CONFIG_PATHS:
        if not os.path.exists(path):
            continue
        try:
            text = open(path, encoding="utf-8").read()
            cfg = json.loads(strip_jsonc(text))
            headers = cfg.get("mcp", {}).get("openrouter", {}).get("headers", {})
            auth = headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:]
        except Exception:
            continue
    sys.exit("ERROR: No API key found. Set OPENROUTER_API_KEY or pass --key")


def api_get(path: str, key: str, params: dict | None = None):
    r = httpx.get(f"{API}{path}", headers={"Authorization": f"Bearer {key}"}, params=params, timeout=30)
    return r


# Models with a provider-side default voice - no --voice needed.
DEFAULT_VOICE_MODELS: set[str] = {
    "fish-audio/s1",
    "fish-audio/s2-pro",
    "fish-audio/s2.1-pro",
    "fish-audio/s2.1-pro-free:free",
}


def get_voices(model: str) -> list[str]:
    """Return known voices for a model, or [] if unknown (voice then required).
    Exact key first, then fall back to prefix match so 'model:free' variants
    (e.g. fish-audio/s2.1-pro-free:free) hit the base catalog entry."""
    if model in KNOWN_MODELS:
        return KNOWN_MODELS[model]
    for key, voices in KNOWN_MODELS.items():
        if model.startswith(key):
            return voices
    return []


def check_key(key: str):
    r = api_get("/auth/key", key)
    if r.status_code != 200:
        sys.exit(f"ERROR: Key rejected by OpenRouter ({r.status_code}): {r.text[:300]}")
    d = r.json()["data"]
    lim = d.get("limit")
    used = d.get("usage_daily", 0)
    log(f"Key:        {d.get('label', '?')}")
    log(f"Expires:    {d.get('expires_at') or 'never'}")
    if lim:
        log(f"Daily cap:  ${lim}  (used ${used:.3f}, ${max(0, lim - used):.3f} left)")
        if used >= lim:
            log("WARNING: Daily limit EXHAUSTED - raise it at https://openrouter.ai/settings/keys")
    else:
        log(f"Daily cap:  none  (used ${used:.3f} today)")
    return d


def list_voices(model: str):
    voices = get_voices(model)
    if not voices:
        sys.exit(f"Model '{model}' has no voice list in the script catalog. Pass --voice manually (or add it to KNOWN_MODELS in tts.py).")
    log(f"Model:   {model}")
    log(f"Voices:  {', '.join(voices)}")
    sys.exit(0)


def make_speech(model: str, voice: str, text: str, fmt: str, key: str, speed: float | None, rate_override: int | None = None, _attempt: int = 1) -> bytes:
    """Generate speech. If the model rejects the format (e.g. Gemini: pcm only),
    retry with pcm and convert to the requested format via ffmpeg.
    Transient timeouts/network errors are retried up to 3 times."""
    body = {"model": model, "input": text}
    if voice != "default":
        body["voice"] = voice
    if fmt:
        body["response_format"] = fmt
    if speed:
        body["speed"] = speed
    try:
        r = httpx.post(
            f"{API}/audio/speech",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(300.0, connect=30.0),
        )
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        if _attempt < 3:
            log(f"  (timeout/network error on chunk - retrying {_attempt + 1}/3)")
            time.sleep(2 * _attempt)
            return make_speech(model, voice, text, fmt, key, speed, rate_override, _attempt + 1)
        sys.exit(f"ERROR: TTS request timed out after 3 attempts: {e}")
    if r.status_code == 200:
        if not r.content and _attempt < 3:
            log(f"  (empty response - retrying {_attempt + 1}/3)")
            time.sleep(2 * _attempt)
            return make_speech(model, voice, text, fmt, key, speed, rate_override, _attempt + 1)
        if not r.content:
            sys.exit("ERROR: TTS returned empty audio after 3 attempts")
        return r.content
    if r.status_code == 400 and "pcm" in r.text and fmt and fmt != "pcm":
        log("  (model needs pcm - retrying and converting)")
        pcm = make_speech(model, voice, text, "pcm", key, speed, rate_override)
        return pcm_to_mp3(pcm, model, rate_override)
    err = r.text[:400]
    if r.status_code == 401:
        sys.exit("ERROR: Invalid API key (401). Check the key at https://openrouter.ai/settings/keys")
    if r.status_code == 402:
        sys.exit("ERROR: Insufficient credits / daily limit reached (402). Top up or raise key limit at https://openrouter.ai/settings/keys")
    if r.status_code == 404:
        sys.exit(f"ERROR: Model or voice not found (404). Model: {model}, Voice: {voice}. Check --voices.")
    if r.status_code == 429:
        sys.exit("ERROR: Rate limited (429). Wait a minute and retry.")
    sys.exit(f"ERROR: TTS failed ({r.status_code}): {err}")


def pcm_to_mp3(pcm: bytes, model: str, rate_override: int | None = None) -> bytes:
    """Convert 16-bit mono PCM to MP3 via ffmpeg. Uses unique temp files (thread-safe)."""
    ffmpeg = find_ffmpeg()
    rate = rate_override or model_rate(model)
    if not rate:
        sys.exit(f"ERROR: No sample-rate preset for model '{model}' (needed for pcm->mp3). Add it to MODEL_SAMPLE_RATES in audio_convert.py or pass --rate.")
    uid = f"{os.getpid()}_{threading.get_ident()}"
    src = os.path.join(tempfile.gettempdir(), f"tts_pcm_src_{uid}.pcm")
    dst = os.path.join(tempfile.gettempdir(), f"tts_pcm_dst_{uid}.mp3")
    try:
        with open(src, "wb") as f:
            f.write(pcm)
        res = subprocess.run(
            [ffmpeg, "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", src, "-b:a", "128k", dst],
            capture_output=True,
        )
        if res.returncode != 0 or not os.path.exists(dst):
            sys.exit(f"ERROR: ffmpeg pcm->mp3 conversion failed: {res.stderr.decode(errors='ignore')[:300]}")
        with open(dst, "rb") as f:
            data = f.read()
    finally:
        for p in (src, dst):
            if os.path.exists(p):
                os.remove(p)
    return data


def concat_mp3(parts: list[str], out: str):
    """Concatenate mp3 parts with ffmpeg if available, else error clearly."""
    ffmpeg = find_ffmpeg()
    listfile = out + ".parts.txt"
    with open(listfile, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    res = subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", out],
        capture_output=True,
    )
    os.remove(listfile)
    if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        sys.exit(f"ERROR: ffmpeg concat failed: {res.stderr.decode(errors='ignore')[:300]}")


def log(msg: str):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description="Universal OpenRouter TTS")
    ap.add_argument("--model", default="x-ai/grok-voice-tts-1.0")
    ap.add_argument("--voice")
    ap.add_argument("--text")
    ap.add_argument("--file")
    ap.add_argument("--out", default="tts_output.mp3")
    ap.add_argument("--format", choices=["mp3", "pcm", "wav"], default="mp3")
    ap.add_argument("--voices", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--key")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--chunk", type=int, default=8000)
    ap.add_argument("--rate", type=int, help="Sample rate for pcm->mp3 conversion (overrides model preset).")
    args = ap.parse_args()

    key = find_key(args.key)

    if args.check:
        check_key(key)
        return

    if args.voices:
        list_voices(args.model)

    if args.text and args.file:
        sys.exit("ERROR: Use --text OR --file, not both.")
    text = args.text
    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f"ERROR: File not found: {args.file}")
        text = open(args.file, encoding="utf-8").read()
    if not text or not text.strip():
        sys.exit("ERROR: No text. Pass --text '...' or --file path")

    fmt = "pcm" if args.format == "wav" else args.format
    out = args.out
    if not out.lower().endswith(tuple([".mp3", ".pcm", ".wav"])):
        out += f".{args.format}"

    # Validate model + pick voice (from embedded catalog, case-insensitive)
    voices = get_voices(args.model)
    voice = args.voice
    if not voice and voices:
        voice = voices[0]
    if not voice:
        # Unknown model without a voice: try provider default voice automatically.
        # If the API requires a voice, its error message will say so clearly.
        voice = "default"
    if voices:
        match = [v for v in voices if v.lower() == voice.lower()]
        if not match:
            sys.exit(f"ERROR: Voice '{voice}' not in {voices} for {args.model}")
        voice = match[0]  # canonical spelling

    # Split long text
    parts = [text[i : i + args.chunk] for i in range(0, len(text), args.chunk)]

    log(f"Model:  {args.model}")
    log(f"Voice:  {voice}")
    log(f"Chunks: {len(parts)}  (total {len(text)} chars)")

    if len(parts) == 1:
        data = make_speech(args.model, voice, parts[0], fmt, key, args.speed, args.rate)
        if args.format == "wav" and fmt == "pcm":
            rate = args.rate or model_rate(args.model)
            if not rate:
                sys.exit("ERROR: unknown sample rate for wav output — pass --rate")
            tmp = tempfile.mktemp(suffix=".pcm")
            with open(tmp, "wb") as f:
                f.write(data)
            res = subprocess.run(
                [find_ffmpeg(), "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", tmp, out],
                capture_output=True,
            )
            os.remove(tmp)
            if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
                sys.exit(f"ERROR: wav conversion failed: {res.stderr.decode(errors='ignore')[:300]}")
            log(f"Saved:  {out} ({os.path.getsize(out) / 1024:.0f} KB) (wav)")
        else:
            with open(out, "wb") as f:
                f.write(data)
            log(f"Saved:  {out} ({len(data) / 1024:.0f} KB)")
        return

    # Multiple chunks -> generate parts IN PARALLEL then concat
    tmpdir = tempfile.mkdtemp(prefix="tts_")
    part_files: list[str] = [""] * len(parts)

    def gen_one(i: int) -> tuple[int, bytes]:
        data = make_speech(args.model, voice, parts[i], "mp3", key, args.speed, args.rate)
        return i, data

    with ThreadPoolExecutor(max_workers=min(len(parts), 8)) as pool:
        futures = {pool.submit(gen_one, i): i for i in range(len(parts))}
        done = 0
        for fut in as_completed(futures):
            i, data = fut.result()
            pf = os.path.join(tmpdir, f"part_{i}.mp3")
            with open(pf, "wb") as f:
                f.write(data)
            part_files[i] = pf
            done += 1
            log(f"  chunk {i + 1}/{len(parts)} ok ({len(data) / 1024:.0f} KB) [{done}/{len(parts)} done]")

    if args.format == "mp3":
        concat_mp3(part_files, out)
    elif args.format == "wav":
        rate = args.rate or model_rate(args.model) or 44100
        ffmpeg = find_ffmpeg()
        wavs = []
        for pf in part_files:
            wf = pf.rsplit(".", 1)[0] + ".wav"
            res = subprocess.run([ffmpeg, "-y", "-i", pf, "-ar", str(rate), "-ac", "1", wf], capture_output=True)
            if res.returncode != 0:
                sys.exit(f"ERROR: wav conversion failed: {res.stderr.decode(errors='ignore')[:300]}")
            wavs.append(wf)
        # concat with re-encode: "-c copy" on wav parts would keep per-part RIFF
        # headers and produce a corrupt output file
        listfile = out + f".parts.{os.getpid()}.txt"
        with open(listfile, "w", encoding="utf-8") as f:
            for p in wavs:
                f.write(f"file '{os.path.abspath(p)}'\n")
        res = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile,
             "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le", out],
            capture_output=True,
        )
        os.remove(listfile)
        if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            sys.exit(f"ERROR: wav concat failed: {res.stderr.decode(errors='ignore')[:300]}")
    else:
        sys.exit("ERROR: pcm output for multi-chunk text not supported - use --format mp3 or wav")
    log(f"Saved:  {out}")


if __name__ == "__main__":
    main()
