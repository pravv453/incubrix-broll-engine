"""Print the README error-code table from the single source of truth (app/core/errors.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.errors import ERROR_CATALOGUE  # noqa: E402

print("| Code | HTTP | Retryable | Meaning |\n|---|---|---|---|")
for code, (status, retry, desc) in ERROR_CATALOGUE.items():
    print(f"| `{code.value}` | {status} | {'yes' if retry else 'no'} | {desc} |")
print("| `JOB_CANCELLED` | n/a | no | Job-level error recorded when a job is cancelled (appears in job status, not as an HTTP error). |")
