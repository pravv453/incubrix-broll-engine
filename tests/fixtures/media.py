"""Generate small media fixtures on the fly with ffmpeg (nothing binary is committed)."""
import subprocess
from pathlib import Path

from app.core.config import get_settings


def _ff(*args: str) -> None:
    subprocess.run([get_settings().ffmpeg_bin, "-v", "error", "-y", *args], check=True)


def speechlike(path: Path, seconds: float, rate: int = 22050) -> Path:
    """Amplitude-modulated voiced tone (stand-in for speech with the mock ASR)."""
    expr = "0.3*sin(2*PI*170*t)*(0.55+0.45*sin(2*PI*3.3*t))"
    _ff("-f", "lavfi", "-i", f"aevalsrc={expr}:s={rate}:d={seconds}", "-ac", "1", str(path))
    return path


def silence(path: Path, seconds: float) -> Path:
    _ff("-f", "lavfi", "-i", f"anullsrc=r=22050:cl=mono", "-t", str(seconds), str(path))
    return path


def music(path: Path, seconds: float) -> Path:
    """Sustained chord, no speech."""
    expr = "0.2*sin(2*PI*261.6*t)+0.2*sin(2*PI*329.6*t)+0.2*sin(2*PI*392*t)"
    _ff("-f", "lavfi", "-i", f"aevalsrc={expr}:s=22050:d={seconds}", "-ac", "1", str(path))
    return path


def tiny_video(path: Path, seconds: float = 2) -> Path:
    _ff("-f", "lavfi", "-i", f"testsrc2=s=320x240:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-f", "mp4", str(path))
    return path
