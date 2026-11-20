"""Runtime witnesses for partially-initialised module access.

The tracer replaces the module object of every watched module with a
``ModuleType`` subclass for the duration of ``exec_module``.  Any attribute
read on such a module -- whether it comes from ``module.attr`` in another
module or from ``from module import attr`` -- goes through the subclass and is
recorded together with the full stack of modules that are *currently executing*.

If the accessed module is inside its own ``exec_module`` call, the recorded
stack is, by construction, a runtime-proven import cycle: the target is on the
stack because it started executing before the accessor.
"""

from __future__ import annotations

import sys
import types
from importlib.abc import MetaPathFinder
from typing import Any, Iterable

_ACTIVE: "Tracer | None" = None


class _TrackedModule(types.ModuleType):
    """Module subclass that reports attribute reads to the active tracer."""

    def __getattribute__(self, name: str) -> Any:
        tracer = _ACTIVE
        # Dunder probes (`__path__`, `__all__`, `__spec__`, ...) are emitted by
        # the import machinery itself on almost every import; recording them
        # would bury the signal.  They are excluded and this is documented.
        if tracer is not None and not name.startswith("__"):
            try:
                tracer._record_access(self, name)
            except Exception:  # pragma: no cover - the tracer must never break imports
                pass
        return types.ModuleType.__getattribute__(self, name)


class _WatchingLoader:
    """Loader proxy that brackets ``exec_module`` with begin/end tracing."""

    def __init__(self, inner: Any, tracer: "Tracer") -> None:
        self._inner = inner
        self._tracer = tracer

    def create_module(self, spec: Any) -> Any:
        make = getattr(self._inner, "create_module", None)
        if make is None:
            return None
        return make(spec)

    def exec_module(self, module: types.ModuleType) -> Any:
        name = types.ModuleType.__getattribute__(module, "__dict__").get("__name__", "")
        if not self._tracer._should_watch(name):
            return self._inner.exec_module(module)
        self._tracer._begin(name, module)
        try:
            return self._inner.exec_module(module)
        finally:
            self._tracer._end(name, module)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class _WatchingFinder(MetaPathFinder):
    """First meta-path finder: delegates, then wraps the found loader."""

    def __init__(self, tracer: "Tracer") -> None:
        self._tracer = tracer
        self._delegates: list[Any] = []

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        for finder in self._delegates:
            try:
                spec = finder.find_spec(fullname, path, target)
            except Exception:
                continue
            if spec is None or spec.loader is None:
                continue
            if not hasattr(spec.loader, "exec_module"):
                continue
            spec.loader = _WatchingLoader(spec.loader, self._tracer)
            return spec
        return None


class Witness:
    """One observed attribute read on a module that was still executing."""

    __slots__ = (
        "target",
        "attribute",
        "accessor",
        "chain",
        "defined_at_access",
        "globals_at_access",
        "count",
        "kind",
    )

    def __init__(
        self,
        target: str,
        attribute: str,
        accessor: str,
        chain: tuple[str, ...],
        defined_at_access: bool,
        globals_at_access: int = 0,
    ) -> None:
        self.target = target
        self.attribute = attribute
        self.accessor = accessor
        self.chain = chain
        self.defined_at_access = defined_at_access
        self.globals_at_access = globals_at_access
        self.count = 1
        self.kind = "unclassified"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.target, self.attribute, self.accessor)

    @property
    def cycle(self) -> tuple[str, ...]:
        """Stack slice from the accessed module to the current top."""
        if self.target in self.chain:
            i = self.chain.index(self.target)
            return self.chain[i:]
        return self.chain

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "attribute": self.attribute,
            "accessor": self.accessor,
            "kind": self.kind,
            "defined_at_access": self.defined_at_access,
            "globals_at_access": self.globals_at_access,
            "count": self.count,
            "cycle": list(self.cycle),
            "chain": list(self.chain),
        }


class Tracer:
    """Context manager that watches imports of ``packages``.

    Parameters
    ----------
    packages:
        Top-level package names (or module names) whose modules are watched.
        A name ``foo`` also watches ``foo.bar``.
    """

    def __init__(self, packages: Iterable[str]) -> None:
        self.packages = tuple(dict.fromkeys(packages))
        self._witnesses: dict[tuple[str, str, str], Witness] = {}
        self._executing: set[str] = set()
        self._stack: list[str] = []
        self._classes: list[tuple[types.ModuleType, type]] = []
        self._finder: _WatchingFinder | None = None
        self._installed = False
        self._watched_modules: list[str] = []
        self._reentrant = False
        self.access_count = 0

    # -- lifecycle ---------------------------------------------------------
    def _should_watch(self, name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in self.packages)

    def __enter__(self) -> "Tracer":
        global _ACTIVE
        self._finder = _WatchingFinder(self)
        self._finder._delegates = [f for f in sys.meta_path if f is not self._finder]
        sys.meta_path.insert(0, self._finder)
        _ACTIVE = self
        self._installed = True
        return self

    def __exit__(self, *exc: Any) -> None:
        global _ACTIVE
        if self._finder is not None:
            try:
                sys.meta_path.remove(self._finder)
            except ValueError:
                pass
        if _ACTIVE is self:
            _ACTIVE = None
        self._installed = False
        # restore any module that is still marked as executing (import error)
        while self._classes:
            module, cls = self._classes.pop()
            try:
                module.__class__ = cls
            except TypeError:  # pragma: no cover
                pass

    # -- execution bracket -------------------------------------------------
    def _begin(self, name: str, module: types.ModuleType) -> None:
        self._stack.append(name)
        self._executing.add(name)
        self._watched_modules.append(name)
        current = module.__class__
        self._classes.append((module, current))
        if current is not _TrackedModule:
            try:
                module.__class__ = _TrackedModule
            except TypeError:  # pragma: no cover - exotic module subclasses
                pass

    def _end(self, name: str, module: types.ModuleType) -> None:
        if self._stack and self._stack[-1] == name:
            self._stack.pop()
        elif name in self._stack:
            self._stack.remove(name)
        self._executing.discard(name)
        if self._classes:
            owner, cls = self._classes.pop()
            if owner is module:
                try:
                    owner.__class__ = cls
                except TypeError:  # pragma: no cover
                    pass

    # -- recording ---------------------------------------------------------
    def _record_access(self, module: types.ModuleType, name: str) -> None:
        if self._reentrant or not self._executing:
            return
        namespace = types.ModuleType.__getattribute__(module, "__dict__")
        target = namespace.get("__name__", "")
        if target not in self._executing:
            return
        self._reentrant = True
        try:
            # `from m import x` is executed by a C opcode whose nearest Python
            # frame is importlib._bootstrap; walk out to the module that really
            # wrote the import statement.
            frame = sys._getframe(2)
            accessor = frame.f_globals.get("__name__", "<unknown>")
            depth = 0  # noqa: F841 - kept for the walk-out loop below
            while accessor.startswith("importlib._bootstrap") and depth < 12:
                frame = frame.f_back
                if frame is None:
                    break
                accessor = frame.f_globals.get("__name__", "<unknown>")
                depth += 1
            if accessor == __name__ or accessor.startswith(__name__.rsplit(".", 1)[0] + "."):
                return  # the tracer's own bookkeeping, never a witness
            chain = tuple(self._stack)
            key = (target, name, accessor)
            witness = self._witnesses.get(key)
            if witness is None:
                witness = Witness(
                    target,
                    name,
                    accessor,
                    chain,
                    name in namespace,
                    sum(1 for k in namespace if not k.startswith("__")),
                )
                self._witnesses[key] = witness
            else:
                witness.count += 1
            self.access_count += 1
        finally:
            self._reentrant = False

    # -- reporting ---------------------------------------------------------
    def _module_object(self, name: str) -> types.ModuleType | None:
        obj = sys.modules.get(name)
        return obj if isinstance(obj, types.ModuleType) else None

    def _classify(self, witness: Witness) -> str:
        module = self._module_object(witness.target)
        attribute = witness.attribute
        # `from package import submodule` makes the import machinery probe the
        # parent for a name that is a *module*: that is normal behaviour, not a
        # partially-initialised read.
        if isinstance(sys.modules.get("%s.%s" % (witness.target, attribute)), types.ModuleType):
            return "submodule-probe"
        if module is None:
            return "partial-missing"
        namespace = types.ModuleType.__getattribute__(module, "__dict__")
        if witness.defined_at_access:
            return "partial-defined"
        if attribute in namespace:
            return "partial-missing-late"
        return "partial-missing"

    def witnesses(self) -> list[Witness]:
        out = []
        for witness in self._witnesses.values():
            witness.kind = self._classify(witness)
            out.append(witness)
        out.sort(key=lambda w: (w.target, w.attribute, w.accessor))
        return out

    def _defined_fraction(self, witness: Witness) -> float | None:
        module = self._module_object(witness.target)
        if module is None:
            return None
        namespace = types.ModuleType.__getattribute__(module, "__dict__")
        final = sum(1 for k in namespace if not k.startswith("__"))
        if final <= 0:
            return None
        return round(witness.globals_at_access / final, 4)

    def report(self) -> dict[str, Any]:
        witnesses = self.witnesses()
        kinds: dict[str, int] = {}
        for w in witnesses:
            kinds[w.kind] = kinds.get(w.kind, 0) + 1
        cycles = {w.cycle for w in witnesses if w.kind != "submodule-probe"}
        rows = []
        for w in witnesses:
            row = w.to_dict()
            row["defined_fraction"] = self._defined_fraction(w)
            rows.append(row)
        return {
            "packages": list(self.packages),
            "watched_modules": sorted(set(self._watched_modules)),
            "imported_modules": len(self._watched_modules),
            "accesses": self.access_count,
            "witnesses": rows,
            "kinds": kinds,
            "runtime_cycles": [list(c) for c in sorted(cycles)],
        }
