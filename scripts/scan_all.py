"""Run :mod:`cyclewitness` over a corpus, one fresh interpreter per package.

Usage::

    python scripts/scan_all.py --out evidence/scan.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

STDLIB = [
    "asyncio",
    "email",
    "http",
    "xml.etree",
    "unittest",
    "logging",
    "importlib",
    "concurrent.futures",
    "multiprocessing",
    "json",
    "urllib",
    "collections",
]

THIRD_PARTY = [
    "jinja2",
    "babel",
    "click",
    "requests",
    "yaml",
    "markdown_it",
    "docutils",
    "httplib2",
    "jmespath",
    "pygments",
    "boto3",
    "botocore",
    "eventlet",
    "twisted",
    "attr",
]


def run_one(package: str, src: str) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        path = handle.name
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", "cyclewitness", "scan", "--package", package, "--json", path, "--quiet"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
        record["returncode"] = proc.returncode
        record["stderr_tail"] = proc.stderr.strip().splitlines()[-1:] if proc.stderr else []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return record


def summarize(records: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    static_cycles = 0
    witnessed_cycles = 0
    for record in records:
        static_cycles += record["static"]["cycle_count"]
        runtime = record.get("runtime") or {}
        for kind, count in runtime.get("kinds", {}).items():
            kinds[kind] = kinds.get(kind, 0) + count
        witnessed_cycles += len(runtime.get("runtime_cycles", []))
    return {
        "packages": len(records),
        "packages_imported_ok": sum(1 for r in records if r["import_ok"]),
        "static_cycles": static_cycles,
        "runtime_cycles": witnessed_cycles,
        "partial_init_reads": sum(len((r.get("runtime") or {}).get("witnesses", [])) for r in records),
        "kinds": kinds,
        "modules_executed": sum((r.get("runtime") or {}).get("imported_modules", 0) for r in records),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="evidence/scan.json")
    parser.add_argument("--src", default="src")
    args = parser.parse_args()
    src = os.path.abspath(args.src)
    records = []
    for package in STDLIB + THIRD_PARTY:
        record = run_one(package, src)
        record["corpus"] = "stdlib" if package in STDLIB else "third-party"
        records.append(record)
        print(
            "%-20s static_cycles=%-2d runtime_reads=%-3d kinds=%s"
            % (
                package,
                record["static"]["cycle_count"],
                len((record.get("runtime") or {}).get("witnesses", [])),
                (record.get("runtime") or {}).get("kinds", {}),
            )
        )
    payload = {
        "generated_by": "scripts/scan_all.py",
        "python": sys.version.split()[0],
        "records": records,
        "summary": summarize(records),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
