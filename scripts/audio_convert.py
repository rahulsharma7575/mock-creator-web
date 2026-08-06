"""
audio_convert.py — Convert raw PCM (or WAV) to MP3/WAV/OGG/M4A via ffmpeg,
with TTS-model-aware sample-rate matching.

Usage:
  uv run ~/.config/opencode/scripts/audio_convert.py input.pcm output.mp3 --model fish-audio/s2.1-pro-free
  uv run ~/.config/opencode/scripts/audio_convert.py input.pcm output.wav --rate 24000
  uv run ~/.config/opencode/scripts/audio_convert.py input.wav output.mp3
  uv run ~/.config/opencode/scripts/audio_convert.py input.pcm output.mp3 --channels 2 --bitdepth 16

Rules:
  - Raw PCM input REQUIRES --model (from preset table) or --rate. Errors out otherwise
    (silent wrong-speed audio is the failure mode we prevent).
  - WAV input: sample rate auto-detected from the WAV header.
  - Defaults for raw PCM: 16-bit little-endian, mono.
  - Uses system ffmpeg; falls back to `uvx --from static-ffmpeg` if not found.
"""

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

# TTS model -> sample rate (Hz) preset table.
# Add new models here as they get tested. Verify with `ffprobe` on the output.
MODEL_SAMPLE_RATES = {
    # Fish Audio S2/S2.1/S1 — verified 44.1 kHz on OpenRouter output
    "fish-audio/s2.1-pro-free": 44100,
    "fish-audio/s2.1-pro": 44100,
    "fish-audio/s2-pro": 44100,
    "fish-audio/s1": 44100,
    # Google Gemini TTS family — verified 24 kHz (B_google.wav header)
    "google/gemini-3.1-flash-tts-preview": 24000,
    "google/gemini-2.5-flash-tts": 24000,
    "google/gemini-2.5-pro-tts": 24000,
    # Microsoft edge-tts / Azure neural voices — 24 kHz
    "edge-tts": 24000,
    "microsoft/mai-voice-2": 24000,
    "microsoft/mai-voice-2-flash": 24000,
    # xAI Grok Voice — 44.1 kHz
    "x-ai/grok-voice-tts-1.0": 44100,
}

SUPPORTED_OUTPUTS = (".mp3", ".wav", ".ogg", ".m4a")

PCM_FORMATS = {
    8: "u8",
    16: "s16le",
    24: "s24le",
    32: "s32le",
}


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # winget installs Gyan.FFmpeg under LOCALAPPDATA before PATH refreshes
    win_pkg = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if win_pkg.exists():
        for p in win_pkg.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            return str(p)
    alt = shutil.which("static_ffmpeg")
    if alt:
        return alt
    sys.exit(
        "ffmpeg not found on PATH. Install with:\n"
        "  winget install Gyan.FFmpeg\n"
        "or use the fallback: uvx --from static-ffmpeg static_ffmpeg.exe"
    )


def wav_header_rate(path: Path) -> int:
    with wave.open(str(path), "rb") as w:
        return w.getframerate()


def match_model_rate(model: str) -> int:
    for key, rate in MODEL_SAMPLE_RATES.items():
        if key in model:
            return rate
    sys.exit(
        f"No sample rate preset for model '{model}'.\n"
        f"Known presets: {sorted(set(MODEL_SAMPLE_RATES.values()))} Hz for: "
        f"{', '.join(MODEL_SAMPLE_RATES)}.\n"
        "Add the model to the MODEL_SAMPLE_RATES table or pass --rate explicitly."
    )


def raw_pcm_to_wav(pcm_bytes: bytes, rate: int, channels: int, bitdepth: int) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bitdepth // 8)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)
    return tmp


def cleanup_temp(paths):
    for t in paths:
        for _ in range(5):
            try:
                t.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description="Convert raw PCM/WAV to playable audio with TTS-model sample rates.")
    ap.add_argument("input", help="Input file: raw .pcm or .wav")
    ap.add_argument("output", help="Output file: .mp3, .wav, .ogg, or .m4a")
    ap.add_argument("--model", help="TTS model slug (e.g. fish-audio/s2.1-pro-free). Required for raw PCM unless --rate is set.")
    ap.add_argument("--rate", type=int, help="Sample rate in Hz (overrides model preset). Required for raw PCM without --model.")
    ap.add_argument("--channels", type=int, default=1, help="Channels for raw PCM input (default 1).")
    ap.add_argument("--bitdepth", type=int, default=16, choices=(8, 16, 24, 32), help="Bit depth for raw PCM input (default 16).")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    if not inp.exists():
        sys.exit(f"Input not found: {inp}")
    if out.suffix.lower() not in SUPPORTED_OUTPUTS:
        sys.exit(f"Unsupported output extension '{out.suffix}'. Use one of: {', '.join(SUPPORTED_OUTPUTS)}")

    ffmpeg = find_ffmpeg()
    is_raw = inp.suffix.lower() == ".pcm"
    cleanup = []

    if is_raw:
        rate = args.rate or (match_model_rate(args.model) if args.model else None)
        if rate is None:
            sys.exit("Raw PCM input requires --model (preset table) or --rate. Refusing to guess.")
        if args.bitdepth not in PCM_FORMATS:
            sys.exit(f"Unsupported bit depth: {args.bitdepth}")
        if inp.suffix.lower() == ".pcm":
            wav_tmp = raw_pcm_to_wav(inp.read_bytes(), rate, args.channels, args.bitdepth)
            cleanup.append(wav_tmp)
            source = str(wav_tmp)
        else:
            source = str(inp)
        sample_rate_note = f"raw PCM at {rate} Hz"
    else:
        rate = wav_header_rate(inp)
        source = str(inp)
        sample_rate_note = f"WAV header at {rate} Hz"

    if out.suffix.lower() == ".wav" and is_raw:
        # raw PCM -> WAV: just keep the wrapped temp file
        import shutil as _sh
        _sh.copy(source, out)
        print(f"Wrapped WAV: {out} ({sample_rate_note})")
        cleanup_temp(cleanup)
        return

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if is_raw:
        cmd += ["-f", PCM_FORMATS[args.bitdepth], "-ar", str(rate), "-ac", str(args.channels)]
    cmd += ["-i", source]
    if out.suffix.lower() == ".ogg":
        cmd += ["-c:a", "libvorbis", "-q:a", "6"]
    elif out.suffix.lower() == ".m4a":
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    elif out.suffix.lower() == ".wav":
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    cmd.append(str(out))

    result = subprocess.run(cmd, capture_output=True, text=True)
    cleanup_temp(cleanup)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{result.stderr}")
    size = out.stat().st_size
    print(f"Converted: {out} ({size:,} bytes, {sample_rate_note})")


if __name__ == "__main__":
    main()
