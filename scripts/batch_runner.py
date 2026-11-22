#!/usr/bin/env python3
"""Batch runner (Integration Standard #10).

Runs a scenario file or a folder of scenarios (or raw audio files) against the
API and writes everything into ONE output folder:

  <output>/
    summary.json, summary.csv          one row per scenario (status, timings, warnings, links)
    ratings_template.csv               one row per scene, blank human_rating column (for Core/Outstanding scoring)
    index.md                           evidence index linking every scenario's files
    resources.csv                      (optional) docker stats samples during the run
    <scenario>/request.json, submit_response.json, status_history.jsonl, final_status.json,
               video_*.mp4, captions.srt, timeline.json, manifest.json, error.json (if failed)

Scenario JSON formats (a file may hold one object or a list):
  {"name": "...", "description": "...", "request": {...JSON body...}}
  {"name": "...", "audio_file": "relative/or/absolute.mp3", "options": {...}}
Raw .mp3/.wav/.m4a files in the input folder are submitted as uploads with default options.

Failed scenarios are recorded, never skipped. Exit code is 0 unless --fail-on-error.

Example:
  python scripts/batch_runner.py --input tests/scenarios --api http://localhost:8000 --output test-results/run1
"""
import argparse
import concurrent.futures as cf
import csv
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

AUDIO_EXT = {".mp3", ".wav", ".m4a"}


def load_scenarios(inp: Path) -> list[dict]:
    files = sorted(inp.rglob("*")) if inp.is_dir() else [inp]
    out = []
    for f in files:
        if f.suffix.lower() == ".json":
            data = json.loads(f.read_text())
            items = data if isinstance(data, list) else [data]
            for i, sc in enumerate(items):
                sc.setdefault("name", f.stem if len(items) == 1 else f"{f.stem}_{i + 1}")
                sc["_base"] = str(f.parent)
                out.append(sc)
    referenced = {(Path(s["_base"]) / s["audio_file"]).resolve() for s in out if "audio_file" in s}
    for f in files:
        if f.suffix.lower() in AUDIO_EXT and f.resolve() not in referenced:
            out.append({"name": f.stem, "audio_file": str(f), "options": {}, "_base": str(f.parent)})
    return out


class Runner:
    def __init__(self, args):
        self.a = args
        self.out = Path(args.output)
        self.out.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
        self.http = httpx.Client(base_url=args.api.rstrip("/"), headers=headers, timeout=120)
        self.run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _abs(self, url: str) -> str:
        return url if url.startswith("http") else f"{self.a.api.rstrip('/')}{url}"

    def submit(self, sc: dict, d: Path) -> httpx.Response:
        key = f"batch-{self.run_id}-{sc['name']}-{uuid.uuid4().hex[:6]}"
        cid = f"batch-{self.run_id}-{sc['name']}"
        h = {"Idempotency-Key": key, "X-Correlation-Id": cid}
        if "audio_file" in sc:
            p = Path(sc["audio_file"])
            p = p if p.is_absolute() else Path(sc["_base"]) / p
            (d / "request.json").write_text(json.dumps({"audio_file": str(p), "options": sc.get("options", {}),
                                                        "headers": h}, indent=2))
            with open(p, "rb") as f:
                return self.http.post("/api/v1/broll/jobs", headers=h, files={"audio_file": (p.name, f)},
                                      data={"options": json.dumps(sc.get("options", {}))})
        (d / "request.json").write_text(json.dumps({"body": sc["request"], "headers": h}, indent=2, ensure_ascii=False))
        return self.http.post("/api/v1/broll/jobs", headers=h, json=sc["request"])

    def download(self, url: str, dest: Path) -> None:
        with httpx.stream("GET", self._abs(url), headers=self.http.headers, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)

    def run_one(self, sc: dict) -> dict:
        name = sc["name"]
        d = self.out / name
        d.mkdir(parents=True, exist_ok=True)
        row = {"scenario": name, "description": sc.get("description", ""), "status": "error", "job_id": None,
               "error_code": None, "warnings": 0, "turnaround_s": None, "narration_s": None, "scenes": None,
               "ai_generated_scenes": None, "providers": None, "judge_mean": None, "folder": str(d)}
        t0 = time.time()
        try:
            r = self.submit(sc, d)
            (d / "submit_response.json").write_text(json.dumps({"status_code": r.status_code, "headers": dict(r.headers),
                                                                "body": r.json()}, indent=2))
            if r.status_code != 202:
                err = r.json().get("error", {})
                row.update(status="rejected", error_code=err.get("code"), http_status=r.status_code)
                (d / "error.json").write_text(json.dumps(r.json(), indent=2))
                return row
            jid = r.json()["job_id"]
            row["job_id"] = jid
            if not self.a.wait:
                row["status"] = "submitted"
                return row
            hist = open(d / "status_history.jsonl", "w")
            while True:
                st = self.http.get(f"/api/v1/jobs/{jid}").json()
                hist.write(json.dumps({"t": round(time.time() - t0, 2), "status": st["status"], "stage": st["stage"],
                                       "progress": st["progress"]}) + "\n")
                hist.flush()
                if st["status"] in ("succeeded", "failed", "cancelled"):
                    break
                if time.time() - t0 > self.a.timeout:
                    st["status"] = "timeout"
                    break
                time.sleep(self.a.poll_interval)
            hist.close()
            row["turnaround_s"] = round(time.time() - t0, 2)
            (d / "final_status.json").write_text(json.dumps(st, indent=2, ensure_ascii=False))
            row["status"] = st["status"]
            row["warnings"] = len(st.get("warnings") or [])
            if st["status"] != "succeeded":
                row["error_code"] = (st.get("error") or {}).get("code")
                (d / "error.json").write_text(json.dumps(st.get("error"), indent=2))
                return row
            res = st["result"]
            if self.a.download:
                for ratio, url in res["videos"].items():
                    self.download(url, d / f"video_{ratio.replace(':', 'x')}.mp4")
                for k, fn in (("captions_srt", "captions.srt"), ("timeline_json", "timeline.json"),
                              ("manifest_json", "manifest.json")):
                    if res.get(k):
                        self.download(res[k], d / fn)
                tl = json.loads((d / "timeline.json").read_text())
                man = json.loads((d / "manifest.json").read_text())
                row.update(narration_s=tl["duration_s"], scenes=len(tl["scenes"]),
                           ai_generated_scenes=sum(1 for s in tl["scenes"] if s["clip"]["is_ai_generated"]),
                           providers="|".join(sorted({s["clip"]["provider"] for s in tl["scenes"]})),
                           judge_mean=man["validation"].get("independent_judge_mean"),
                           server_turnaround_s=man.get("turnaround_s"))
        except Exception as exc:  # noqa: BLE001 - record and continue with the next scenario
            row["error_code"] = f"RUNNER_{type(exc).__name__}"
            (d / "error.json").write_text(json.dumps({"runner_exception": repr(exc)}, indent=2))
        finally:
            if row["turnaround_s"] is None:
                row["turnaround_s"] = round(time.time() - t0, 2)
            print(f"[{row['status']:>9}] {name}  {row['turnaround_s']}s  {row.get('error_code') or ''}", flush=True)
        return row


def sample_resources(out: Path, stop: threading.Event, interval: float = 2.0) -> None:
    with open(out / "resources.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "container", "cpu_pct", "mem_usage"])
        t0 = time.time()
        while not stop.is_set():
            try:
                res = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}},{{.CPUPerc}},{{.MemUsage}}"],
                                     capture_output=True, text=True, timeout=10)
                for line in res.stdout.strip().splitlines():
                    n, cpu, mem = line.split(",", 2)
                    w.writerow([round(time.time() - t0, 1), n, cpu, mem])
                f.flush()
            except Exception:  # noqa: BLE001
                pass
            stop.wait(interval)


def write_reports(out: Path, rows: list[dict], wall_s: float, concurrency: int) -> None:
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    ok = [r for r in rows if r["status"] == "succeeded"]
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "scenarios": len(rows), "succeeded": len(ok),
               "failed_or_rejected": len(rows) - len(ok), "wall_clock_s": round(wall_s, 2), "concurrency": concurrency,
               "render_success_rate": round(len(ok) / len(rows), 3) if rows else None,
               "total_narration_s": round(sum(r["narration_s"] or 0 for r in ok), 2), "rows": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    with open(out / "ratings_template.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "job_id", "scene_id", "start_s", "end_s", "duration_s", "text", "provider", "asset_id",
                    "is_ai_generated", "auto_score", "judge_score", "human_rating_1_5", "notes"])
        for r in ok:
            tl_path = Path(r["folder"]) / "timeline.json"
            if not tl_path.exists():
                continue
            for s in json.loads(tl_path.read_text())["scenes"]:
                w.writerow([r["scenario"], r["job_id"], s["scene_id"], s["start_s"], s["end_s"], s["duration_s"], s["text"],
                            s["clip"]["provider"], s["clip"]["asset_id"], s["clip"]["is_ai_generated"],
                            s["selection"]["score"], (s.get("independent_judge") or {}).get("score_1_5", ""), "", ""])
    lines = ["# Evidence index", "", f"Wall clock {wall_s:.1f}s, concurrency {concurrency}, "
             f"{len(ok)}/{len(rows)} succeeded.", "", "| Scenario | Status | Job | Turnaround s | Files |", "|---|---|---|---|---|"]
    for r in rows:
        d = Path(r["folder"])
        files = " ".join(f"[{p.name}]({d.name}/{p.name})" for p in sorted(d.iterdir()) if p.is_file())
        lines.append(f"| {r['scenario']} | {r['status']} {r.get('error_code') or ''} | {r['job_id'] or ''} | {r['turnaround_s']} | {files} |")
    (out / "index.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="scenario .json file, audio file, or folder")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--output", default="test-results/latest")
    ap.add_argument("--token", default=None, help="DEMO_ACCESS_TOKEN if the API is protected")
    ap.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--download", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--concurrency", type=int, default=1, help="scenarios submitted in parallel")
    ap.add_argument("--only", nargs="*", help="run only these scenario names")
    ap.add_argument("--sample-resources", action="store_true", help="record `docker stats` to resources.csv")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--fail-on-error", action="store_true")
    a = ap.parse_args()
    scenarios = load_scenarios(Path(a.input))
    if a.only:
        scenarios = [s for s in scenarios if s["name"] in a.only]
    if not scenarios:
        print("no scenarios found", file=sys.stderr)
        return 2
    runner = Runner(a)
    stop = threading.Event()
    sampler = None
    if a.sample_resources:
        sampler = threading.Thread(target=sample_resources, args=(runner.out, stop), daemon=True)
        sampler.start()
    print(f"running {len(scenarios)} scenario(s) against {a.api} -> {runner.out}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
        rows = list(ex.map(runner.run_one, scenarios))
    wall = time.time() - t0
    stop.set()
    if sampler:
        sampler.join(timeout=5)
    write_reports(runner.out, rows, wall, a.concurrency)
    ok = sum(1 for r in rows if r["status"] == "succeeded")
    print(f"done: {ok}/{len(rows)} succeeded in {wall:.1f}s -> {runner.out}/summary.json")
    return 1 if (a.fail_on_error and ok != len(rows)) else 0


if __name__ == "__main__":
    sys.exit(main())
