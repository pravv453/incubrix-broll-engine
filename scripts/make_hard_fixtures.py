#!/usr/bin/env python3
"""Generate the audio hard-case fixtures (nothing binary is committed):

  tests/scenarios/audio_hard/
    a01_silent.json + silent.wav            -> NO_SPEECH_DETECTED
    a02_music_only.json + music_only.wav    -> NO_SPEECH_DETECTED (needs ASR_PROVIDER=faster_whisper for VAD)
    a03_wrong_type.json + notes.txt         -> UNSUPPORTED_AUDIO_FORMAT
    a04_over_10_min.json + over_10min.mp3   -> AUDIO_TOO_LONG
    a05_video_renamed.json + clip.mp3       -> UNSUPPORTED_AUDIO_FORMAT
  tests/scenarios/long/l01_long_narration.json  (~4-5 min script built from the core scenarios)

Real-voice inputs (accents, Hindi/Tamil voiceovers) must be recordings you own or have consent for;
drop them into a folder and point the batch runner at it.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/scenarios/audio_hard"
FF = shutil.which("ffmpeg") or "ffmpeg"


def ff(*a):
    subprocess.run([FF, "-v", "error", "-y", *a], check=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "8", str(OUT / "silent.wav"))
    ff("-f", "lavfi", "-i", "aevalsrc=0.2*sin(2*PI*261.6*t)+0.2*sin(2*PI*329.6*t)+0.2*sin(2*PI*392*t)*(0.6+0.4*sin(2*PI*2*t)):s=44100:d=15",
       str(OUT / "music_only.wav"))
    (OUT / "notes.txt").write_text("this is not audio\n")
    ff("-f", "lavfi", "-i", "aevalsrc=0.3*sin(2*PI*170*t)*(0.55+0.45*sin(2*PI*3.3*t)):s=8000:d=605", "-b:a", "32k",
       str(OUT / "over_10min.mp3"))
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:d=3", "-c:v", "libx264", "-preset", "ultrafast", "-f", "mp4", str(OUT / "clip.mp3"))
    specs = {
        "a01_silent": ("silent.wav", "Silent audio"),
        "a02_music_only": ("music_only.wav", "Music only, no speech"),
        "a03_wrong_type": ("notes.txt", "Wrong file type"),
        "a04_over_10_min": ("over_10min.mp3", "Audio longer than 10 minutes"),
        "a05_video_renamed": ("clip.mp3", "A video file renamed to .mp3"),
    }
    for name, (fn, desc) in specs.items():
        (OUT / f"{name}.json").write_text(json.dumps({"name": name, "description": desc, "audio_file": fn,
                                                      "options": {"aspect_ratios": ["16:9"]}}, indent=2) + "\n")
    core = sorted((ROOT / "tests/scenarios/core").glob("*.json"))
    text = " ".join(json.loads(p.read_text())["request"]["script"] for p in core)
    long_dir = ROOT / "tests/scenarios/long"
    long_dir.mkdir(parents=True, exist_ok=True)
    (long_dir / "l01_long_narration.json").write_text(json.dumps({
        "name": "l01_long_narration", "description": f"Long narration (~{len(text.split())} words, about 4-5 minutes of speech)",
        "request": {"script": text, "aspect_ratios": ["16:9"], "captions": {"burn_in": True, "srt": True}}}, indent=2) + "\n")
    print(f"wrote fixtures to {OUT} and {long_dir}")


if __name__ == "__main__":
    sys.exit(main())
