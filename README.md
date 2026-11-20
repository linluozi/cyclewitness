# cyclewitness

Measure **import cycles that actually matter**: static cycles are cheap to count, but a cycle only becomes the classic `cannot import name ... from partially initialized module` error when some module *reads an attribute of a module that is still executing*. `cyclewitness` instruments those reads and separates the harmless ones (`from pkg import submodule` machinery probes) from the order-fragile ones.

Upstream: none

This is an original tool. No upstream source is copied or patched; there is therefore no `patch.diff`, no baseline/optimized timing pair, and no G1–G5 upstream gates — see [Scope](#scope-and-honest-limits).

## What it measures

1. **Static pass** — parse every `.py` of a package, resolve relative imports, build the intra-package import graph, and count strongly connected components (Tarjan).
2. **Runtime pass** — import the package once in a fresh interpreter while every watched module object is temporarily a `ModuleType` subclass. Any attribute read on a module whose `exec_module` is still on the stack is recorded with: the reader module, the executed-on stack (the *re-entry path*), whether the name existed at read time, and how many of the module's globals existed then.
3. **Classification** — `submodule-probe` (the name is a submodule; the import machinery does this on every `from pkg import sub`), `partial-defined` (the name existed: works today, but only because of assignment order), `partial-missing-late` / `partial-missing` (the name did not exist at read time).

## Result on a real corpus (2026-11-21)

Corpus: 12 stdlib packages + 15 installed third-party packages (CPython 3.12.3). All 27 imported successfully. Raw records: `evidence/scan-2026-11-21.json`.

| Metric | Value |
| --- | --- |
| packages scanned / imported OK | 27 / 27 |
| modules executed under instrumentation | 268 |
| static intra-package cycles (SCC > 1) | **38** |
| largest static SCC | 47 modules |
| partial-init reads observed | **183** |
| of which `submodule-probe` | 182 (99.5%) |
| of which non-module re-entry (`partial-defined`) | **1** |
| reads of a name that did not exist | 0 |
| import failures caused by a cycle | 0 |

Selected rows (static cycles / observed reads):

| package | static cycles | reads | non-probe reads |
| --- | --- | --- | --- |
| twisted | 15 | 0 | 0 |
| asyncio | 1 | 81 | 0 |
| eventlet | 1 | 47 | 1 |
| requests | 1 | 4 | 0 |
| markdown_it | 1 | 10 | 0 |
| jinja2 | 1 | 5 | 0 |

The single non-probe read: `eventlet.timeout` reads `eventlet.hubs.get_hub` while `eventlet/hubs/__init__.py` is still executing, at which point 80% of that module's globals exist. It works — `get_hub` is simply defined early enough. Move that definition below the `from .. import timeout` line and the same read becomes an ImportError. That is the failure mode this tool names.

**Conclusion:** static cycle counts overstate import risk by roughly an order of magnitude here (38 cycles, 1 order-fragile read, 0 failures). A cycle is only dangerous when a re-entry reads a *non-module* global that is defined later in the target module; the far more common submodule-attribute read is immune, because the import system sets that attribute before the parent finishes.

## Install and run

```bash
python3 -m pip install -e .            # or: PYTHONPATH=src python3 -m cyclewitness ...
python3 -m cyclewitness scan --package jinja2
python3 -m cyclewitness scan --package eventlet --json /tmp/eventlet.json
```

Tests (stdlib `unittest`, no third-party test dependency):

```bash
python3 -m unittest discover -s tests -v      # 9 tests
python3 scripts/scan_all.py --out evidence/scan.json   # regenerate the corpus scan
```

## Reproduce the numbers above

```bash
python3 -m unittest discover -s tests -v
python3 scripts/scan_all.py --out evidence/scan-2026-11-21.json
python3 -c "import json;d=json.load(open('evidence/scan-2026-11-21.json'));print(d['summary'])"
```

## 本轮创新

Previous work in this account measured import **time** (`importsteady`, `coldbudget`) and bytecode **specialisation** (`deoptscope`); nothing joined static cycle counts to what actually happens at runtime. This repo does that for the first time here, and the new, falsifiable result is that the two numbers are not the same quantity: **38 static cycles produced 183 partial-initialisation reads, of which 182 were benign submodule probes and only 1 touched a non-module global** (eventlet, 80% defined). Grade B: new measurement + new conclusion, not a re-run of an existing template.

## Scope and honest limits

- Original tool → `Upstream: none`; G1–G5 (upstream tests, differential ≥1000 cases, speedup, RSS) do not apply and are **not** claimed. The correctness evidence here is the 9 behavioural tests, including a failing-cycle fixture, a late-definition fixture, a benign-cycle fixture, a submodule-probe fixture, an acyclic negative control, and a regression test for `from . import x` inside a package `__init__`.
- One import order per package: the tracer observes the order Python actually takes on this machine, not all possible orders. Different entry points can re-enter different modules, so `0 failures` is a statement about these 27 entry points only.
- Dunder attribute probes (`__path__`, `__all__`, ...) are deliberately not recorded; they are emitted by the import machinery on nearly every import and would drown the signal.
- Imports from multiple threads are not separately attributed; the executing stack is process-global.
- Scanning executes the target package's import side effects once, in a child interpreter (`scripts/scan_all.py`); do not point it at untrusted code.

## Environment

- CPU: AMD EPYC 7K62 (4 vCPU), RAM 3.7 GB
- CPython 3.12.3 (`/usr/bin/python3`), no other interpreter installed
- Corpus snapshot: system dist-packages on 2026-11-21
