"""Static import-graph cycles (intra-package only).

Only edges whose target is a module of the same package are kept: the question
this module answers is "how many cycles exist *inside* this distribution",
which is the set the runtime tracer can also observe.
"""

from __future__ import annotations

import ast
import os
from typing import Iterable


def module_index(root: str, package: str) -> dict[str, str]:
    """Map dotted module name -> file path for every .py under ``root``."""
    index: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".git"}]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root)
            parts = rel[:-3].split(os.sep)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                name = package
            else:
                name = package + "." + ".".join(parts)
            index[name] = path
    return index


def _package_of(module: str) -> str:
    return module.rsplit(".", 1)[0] if "." in module else ""


def _resolve_relative(
    module: str, level: int, target: str | None, is_package: bool
) -> str | None:
    """Resolve ``from ..x import y`` against the module that contains it.

    ``level == 1`` is the module's own package; for a package's ``__init__``
    that package is the module itself, which is why ``is_package`` matters.
    """
    if is_package:
        base = module
    else:
        base = module.rsplit(".", 1)[0] if "." in module else ""
    parts = base.split(".") if base else []
    if not parts or level > len(parts):
        return None
    anchor = parts[: len(parts) - (level - 1)] if level > 1 else parts
    if target:
        return ".".join(anchor + target.split("."))
    return ".".join(anchor)


def imports_of(path: str, module: str, known: dict[str, str]) -> set[str]:
    """Return intra-package import targets of one source file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (SyntaxError, ValueError):
        return set()
    package = _package_of(module)
    edges: set[str] = set()

    def add(name: str | None) -> None:
        if not name:
            return
        if name in known:
            edges.add(name)
            return
        # `import pkg.sub.mod` binds the top package; record the longest known prefix
        parts = name.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:cut])
            if prefix in known:
                edges.add(prefix)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                resolved = _resolve_relative(
                    module, node.level, node.module, path.endswith("__init__.py")
                )
                if resolved:
                    add(resolved)
                    for alias in node.names:
                        if alias.name != "*":
                            add(resolved + "." + alias.name)
            elif node.module:
                add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        add(node.module + "." + alias.name)
    edges.discard(module)
    return edges


def graph(root: str, package: str) -> dict[str, set[str]]:
    index = module_index(root, package)
    return {name: imports_of(path, name, index) for name, path in index.items()}


def strongly_connected(nodes: Iterable[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC, iterative, returning components with more than one node."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root_node in nodes:
        if root_node in index_of:
            continue
        work: list[tuple[str, iter]] = [(root_node, iter(sorted(edges.get(root_node, ()))))]
        index_of[root_node] = low[root_node] = counter
        counter += 1
        stack.append(root_node)
        on_stack[root_node] = True
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(edges.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    popped = stack.pop()
                    on_stack[popped] = False
                    component.append(popped)
                    if popped == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    result.sort()
    return result


def cycles(root: str, package: str) -> list[list[str]]:
    edges = graph(root, package)
    return strongly_connected(sorted(edges), edges)
