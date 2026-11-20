"""Command line entry point: ``python -m cyclewitness scan --package X``."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from typing import Any

from . import static
from .tracer import Tracer


def _package_root(name: str) -> str | None:
    spec = None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        return list(spec.submodule_search_locations)[0]
    if spec.origin:
        return os.path.dirname(spec.origin)
    return None


def scan(package: str) -> dict[str, Any]:
    """Scan one package: static cycles first, then one instrumented import."""
    result: dict[str, Any] = {
        "package": package,
        "python": sys.version.split()[0],
        "root": None,
        "import_ok": False,
        "error": None,
        "elapsed_s": None,
        "static": {"modules": 0, "edges": 0, "cycle_count": 0, "cycles": []},
        "runtime": None,
    }
    root = _package_root(package)
    result["root"] = root
    if root and os.path.isdir(root):
        graph = static.graph(root, package)
        found = static.strongly_connected(sorted(graph), graph)
        result["static"] = {
            "modules": len(graph),
            "edges": sum(len(v) for v in graph.values()),
            "cycle_count": len(found),
            "cycles": found,
            "largest_cycle": max((len(c) for c in found), default=0),
        }
    started = time.perf_counter()
    tracer = Tracer([package])
    with tracer:
        try:
            importlib.import_module(package)
            result["import_ok"] = True
        except BaseException as exc:  # noqa: BLE001 - recorded, never swallowed silently
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
    result["elapsed_s"] = round(time.perf_counter() - started, 4)
    result["runtime"] = tracer.report()
    return result


def _fmt_cycle(cycle: list[str]) -> str:
    return " -> ".join(cycle)


def summarize(result: dict[str, Any]) -> str:
    static_info = result["static"]
    runtime = result["runtime"] or {}
    kinds = runtime.get("kinds", {})
    lines = [
        "package: %s (python %s)" % (result["package"], result["python"]),
        "static : %d modules, %d intra-package edges, %d cycle(s), largest %d"
        % (
            static_info["modules"],
            static_info["edges"],
            static_info["cycle_count"],
            static_info.get("largest_cycle", 0),
        ),
        "import : %s%s"
        % (
            "ok" if result["import_ok"] else "FAILED",
            "" if result["import_ok"] else " (%s)" % result["error"],
        ),
        "runtime: %d module(s) executed, %d partial-init read(s)"
        % (runtime.get("imported_modules", 0), len(runtime.get("witnesses", []))),
    ]
    if kinds:
        lines.append(
            "kinds  : "
            + ", ".join("%s=%d" % (k, v) for k, v in sorted(kinds.items()))
        )
    for witness in runtime.get("witnesses", []):
        lines.append(
            "  [%s] %s.%s  <- %s  (cycle %s, globals %.0f%%)"
            % (
                witness["kind"],
                witness["target"],
                witness["attribute"],
                witness["accessor"],
                _fmt_cycle(witness["cycle"]),
                100.0 * (witness.get("defined_fraction") or 0.0),
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cyclewitness", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="scan one importable package")
    scan_parser.add_argument("--package", required=True)
    scan_parser.add_argument("--json", dest="json_path")
    scan_parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    result = scan(args.package)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
    if not args.quiet:
        print(summarize(result))
    return 0 if result["import_ok"] else 1
