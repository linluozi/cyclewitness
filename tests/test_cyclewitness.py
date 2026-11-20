"""Behavioural tests for cyclewitness (stdlib unittest, no third-party deps)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from cyclewitness import static  # noqa: E402
from cyclewitness.cli import scan  # noqa: E402
from cyclewitness.tracer import Tracer  # noqa: E402

PREFIXES = (
    "cwtest_fragile",
    "cwtest_late",
    "cwtest_benign",
    "cwtest_sub",
    "cwtest_acyclic",
    "cwtest_static",
)


def write_tree(root: str, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(body).lstrip())


class PackageCase(unittest.TestCase):
    files: dict[str, str] = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write_tree(self.root, self.files)
        sys.path.insert(0, self.root)
        self._drop_modules()

    def tearDown(self) -> None:
        self._drop_modules()
        try:
            sys.path.remove(self.root)
        except ValueError:
            pass
        self.tmp.cleanup()

    def _drop_modules(self) -> None:
        for name in list(sys.modules):
            if any(name == p or name.startswith(p + ".") for p in PREFIXES):
                del sys.modules[name]


class TestFailingCycle(PackageCase):
    """A cycle that reads a name defined later: the import must fail."""

    files = {
        "cwtest_fragile/__init__.py": """
            from .b import f
            X = 1
        """,
        "cwtest_fragile/b.py": """
            from cwtest_fragile import X
            def f():
                return X
        """,
    }

    def test_witness_and_error(self) -> None:
        tracer = Tracer(["cwtest_fragile"])
        with tracer:
            with self.assertRaises(ImportError):
                __import__("cwtest_fragile")
        report = tracer.report()
        self.assertEqual(len(report["witnesses"]), 1, report)
        witness = report["witnesses"][0]
        self.assertEqual(witness["target"], "cwtest_fragile")
        self.assertEqual(witness["attribute"], "X")
        self.assertEqual(witness["accessor"], "cwtest_fragile.b")
        self.assertEqual(witness["kind"], "partial-missing")
        self.assertFalse(witness["defined_at_access"])
        self.assertIn("cwtest_fragile", witness["cycle"])

    def test_modules_left_untracked(self) -> None:
        with Tracer(["cwtest_fragile"]):
            with self.assertRaises(ImportError):
                __import__("cwtest_fragile")
        module = sys.modules.get("cwtest_fragile")
        if module is not None:
            self.assertIs(type(module), types.ModuleType)


class TestLateDefinition(PackageCase):
    """The attribute is missing at read time but defined before the module ends."""

    files = {
        "cwtest_late/__init__.py": """
            from .b import probe
            X = 1
        """,
        "cwtest_late/b.py": """
            import cwtest_late as pkg
            probe = getattr(pkg, "X", None)
        """,
    }

    def test_kind_is_late(self) -> None:
        with Tracer(["cwtest_late"]) as tracer:
            __import__("cwtest_late")
        report = tracer.report()
        kinds = {w["kind"] for w in report["witnesses"]}
        self.assertEqual(kinds, {"partial-missing-late"}, report["witnesses"])
        self.assertEqual(report["witnesses"][0]["defined_fraction"] < 1.0, True)


class TestBenignCycle(PackageCase):
    """The name exists before the cycle re-enters: works, but is order-fragile."""

    files = {
        "cwtest_benign/__init__.py": """
            X = 1
            from .b import f
        """,
        "cwtest_benign/b.py": """
            from cwtest_benign import X
            def f():
                return X
        """,
    }

    def test_defined_at_access(self) -> None:
        with Tracer(["cwtest_benign"]) as tracer:
            __import__("cwtest_benign")
        witnesses = tracer.report()["witnesses"]
        self.assertEqual(len(witnesses), 1, witnesses)
        self.assertEqual(witnesses[0]["kind"], "partial-defined")
        self.assertTrue(witnesses[0]["defined_at_access"])


class TestSubmoduleProbe(PackageCase):
    """`from package import submodule` must not be reported as a bad read."""

    files = {
        "cwtest_sub/__init__.py": "from . import alpha\n",
        "cwtest_sub/alpha.py": "from . import beta\n",
        "cwtest_sub/beta.py": "from cwtest_sub import alpha\n",
    }

    def test_probe_classified(self) -> None:
        with Tracer(["cwtest_sub"]) as tracer:
            __import__("cwtest_sub")
        witnesses = tracer.report()["witnesses"]
        self.assertTrue(witnesses, "expected the submodule probe to be observed")
        self.assertTrue(all(w["kind"] == "submodule-probe" for w in witnesses), witnesses)
        self.assertEqual(tracer.report()["runtime_cycles"], [])


class TestAcyclic(PackageCase):
    files = {
        "cwtest_acyclic/__init__.py": "from .b import f\n",
        "cwtest_acyclic/b.py": "def f():\n    return 1\n",
    }

    def test_no_witnesses(self) -> None:
        with Tracer(["cwtest_acyclic"]) as tracer:
            __import__("cwtest_acyclic")
        report = tracer.report()
        self.assertEqual(report["witnesses"], [])
        self.assertEqual(report["accesses"], 0)


class TestStaticGraph(PackageCase):
    files = {
        "cwtest_static/__init__.py": "from . import sub\n",
        "cwtest_static/a.py": "from .b import x\n",
        "cwtest_static/b.py": "from .a import y\n",
        "cwtest_static/sub/__init__.py": "from . import leaf\n",
        "cwtest_static/sub/leaf.py": "VALUE = 1\n",
    }

    def test_relative_resolution_and_scc(self) -> None:
        graph = static.graph(self.root + "/cwtest_static", "cwtest_static")
        # regression: `from . import leaf` inside the *package* sub/ resolves
        # to cwtest_static.sub.leaf, not to cwtest_static.leaf
        self.assertIn("cwtest_static.sub.leaf", graph["cwtest_static.sub"])
        self.assertEqual(graph["cwtest_static.sub.leaf"], set())
        found = static.strongly_connected(sorted(graph), graph)
        self.assertEqual(found, [["cwtest_static.a", "cwtest_static.b"]])

    def test_no_cycle_for_acyclic(self) -> None:
        graph = static.graph(self.root + "/cwtest_static", "cwtest_static")
        edges = static.graph(self.root + "/cwtest_static", "cwtest_static")
        self.assertTrue(all(isinstance(v, set) for v in edges.values()))
        self.assertEqual(len(graph), 5)


class TestCli(PackageCase):
    files = TestBenignCycle.files

    def test_scan_shape(self) -> None:
        result = scan("cwtest_benign")
        self.assertTrue(result["import_ok"])
        self.assertEqual(result["static"]["modules"], 2)
        self.assertGreaterEqual(len(result["runtime"]["witnesses"]), 1)

        with tempfile.TemporaryDirectory() as out_dir:
            out = os.path.join(out_dir, "scan.json")
            env = dict(os.environ)
            env["PYTHONPATH"] = SRC + os.pathsep + self.root
            proc = subprocess.run(
                [sys.executable, "-m", "cyclewitness", "scan", "--package", "cwtest_benign", "--json", out],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["package"], "cwtest_benign")
            self.assertIn("kinds", payload["runtime"])


if __name__ == "__main__":
    unittest.main()
