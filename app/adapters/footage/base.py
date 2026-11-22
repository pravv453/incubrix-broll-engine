"""Footage provider interface + runtime fault injection.

Fault injection lets a reviewer "switch a source off during a job" without
restarting anything: write {"pixabay": "down"} to FAULT_INJECTION_FILE.
Modes: down (raise), empty (no results), corrupt (downloads are garbage), slow (timeout)."""
import json
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings
from app.schemas.footage import FootageCandidate


class ProviderError(RuntimeError):
    def __init__(self, provider: str, reason: str, retryable: bool = True):
        super().__init__(f"{provider}: {reason}")
        self.provider, self.reason, self.retryable = provider, reason, retryable


class FootageProvider(Protocol):
    name: str

    def search(self, query: str, per_page: int, orientation: str | None = None) -> list[FootageCandidate]: ...
    def download(self, cand: FootageCandidate, dest: Path) -> Path: ...
    def thumbnail(self, cand: FootageCandidate, dest: Path) -> Path | None: ...
    def get_asset(self, asset_id: str) -> FootageCandidate | None: ...


def injected_fault(provider: str) -> str | None:
    path = get_settings().fault_injection_file
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text()).get(provider)
    except (OSError, ValueError):
        return None


def apply_search_fault(provider: str) -> bool:
    """Raise for 'down'/'slow'; return True for 'empty'."""
    f = injected_fault(provider)
    if f == "down":
        raise ProviderError(provider, "injected fault: provider down (HTTP 503)")
    if f == "slow":
        raise ProviderError(provider, "injected fault: request timed out")
    return f == "empty"


def apply_download_fault(provider: str, dest: Path) -> bool:
    if injected_fault(provider) == "corrupt":
        dest.write_bytes(b"\x00corrupted-download\x00" * 64)
        return True
    return False
