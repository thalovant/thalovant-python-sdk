#!/usr/bin/env python3
"""Dump the SDK's public API, or compare two dumps.

    python scripts/public_api.py dump > api.json          # the installed/importable SDK
    python scripts/public_api.py compare old.json new.json

The public API is every name a caller can import from a public module of
``thalovant`` (``__all__`` when a module defines one, otherwise every name
without a leading underscore that is defined in or re-exported by it), and for
each function, class and method its signature. ``compare`` lists what was
removed or changed -- a break -- and what was added. It exits non-zero on a
break.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
from typing import Any, TypeVar

SKIP_MODULES = {"thalovant.__main__"}


def _signature(obj: Any) -> list[list[str]] | None:
    """Each parameter's name, kind and default: what a call can rely on.

    Annotations are left out -- widening a type is not a break, and spelling
    the same type two ways is not a change.
    """
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    return [
        [name, param.kind.name, "" if param.default is inspect.Parameter.empty else repr(param.default)]
        for name, param in signature.parameters.items()
    ]


def _compatible(before: list[list[str]] | None, after: list[list[str]] | None) -> bool:
    """Whether every call valid against *before* is valid against *after*."""
    if before == after:
        return True
    if before is None or after is None:
        return False
    new = {name: (kind, default) for name, kind, default in after}
    catch_keywords = any(kind == "VAR_KEYWORD" for _, kind, _ in after)
    catch_positional = any(kind == "VAR_POSITIONAL" for _, kind, _ in after)
    old_positional = [name for name, kind, _ in before if kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    new_positional = [name for name, kind, _ in after if kind in ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")]
    if new_positional[: len(old_positional)] != old_positional and not catch_positional:
        return False
    for name, kind, default in before:
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            continue
        if name not in new:
            if kind == "KEYWORD_ONLY" and catch_keywords:
                continue
            return False
        new_kind, new_default = new[name]
        if default and new_default != default:
            return False
        if kind == "POSITIONAL_OR_KEYWORD" and new_kind == "KEYWORD_ONLY":
            return False
    for name, kind, default in after:
        if kind in ("VAR_POSITIONAL", "VAR_KEYWORD") or default:
            continue
        if name not in {old_name for old_name, _, _ in before}:
            return False  # a new required parameter
    return True


def _describe(obj: Any) -> dict[str, Any]:
    if inspect.isclass(obj):
        members: dict[str, Any] = {}
        for name, value in inspect.getmembers(obj):
            if name.startswith("_") and name not in {"__init__", "__call__", "__enter__", "__exit__",
                                                     "__aenter__", "__aexit__", "__iter__", "__aiter__"}:
                continue
            if isinstance(inspect.getattr_static(obj, name, None), property):
                members[name] = {"kind": "property"}
            elif callable(value):
                kind = "async" if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value) else "sync"
                members[name] = {"kind": kind, "signature": _signature(value)}
            else:
                members[name] = {"kind": "attribute"}
        bases = [f"{base.__module__}.{base.__qualname__}" for base in obj.__mro__[1:]]
        return {"kind": "class", "bases": bases, "members": members}
    if callable(obj):
        kind = "async" if inspect.iscoroutinefunction(obj) or inspect.isasyncgenfunction(obj) else "function"
        return {"kind": kind, "signature": _signature(obj)}
    if inspect.ismodule(obj):
        return {"kind": "module"}
    return {"kind": "value", "type": type(obj).__name__}


def _ours(value: Any) -> bool:
    if inspect.ismodule(value) or isinstance(value, TypeVar):
        return False
    if type(value).__name__ == "_Feature":  # from __future__ import annotations
        return False
    module = getattr(value, "__module__", None)
    if inspect.isclass(value) or inspect.isfunction(value) or inspect.isbuiltin(value):
        return bool(module) and str(module).startswith("thalovant")
    if callable(value) and module is not None and not str(module).startswith("thalovant"):
        return False
    return True


def dump() -> dict[str, Any]:
    import thalovant

    modules = {"thalovant": thalovant}
    for info in pkgutil.walk_packages(thalovant.__path__, "thalovant."):
        name = info.name
        if any(part.startswith("_") for part in name.split(".")[1:]) or name in SKIP_MODULES:
            continue
        modules[name] = importlib.import_module(name)
    api: dict[str, Any] = {}
    for name, module in sorted(modules.items()):
        exported = getattr(module, "__all__", None)
        names = list(exported) if exported is not None else [
            item for item in dir(module) if not item.startswith("_")
        ]
        entries = {}
        for item in sorted(set(names)):
            if not hasattr(module, item):
                continue
            value = getattr(module, item)
            if exported is None and not _ours(value):
                continue  # a stdlib or typing import is not the SDK's API
            entries[item] = _describe(value)
        api[name] = {"all": exported is not None, "names": entries}
    return api


def compare(old: dict[str, Any], new: dict[str, Any]) -> int:
    broken, added = [], []
    for module, spec in old.items():
        if module not in new:
            broken.append(f"module removed: {module}")
            continue
        now = new[module]["names"]
        for name, before in spec["names"].items():
            if name not in now:
                broken.append(f"{module}.{name}: removed")
                continue
            after = now[name]
            if before.get("kind") != after.get("kind"):
                broken.append(f"{module}.{name}: {before.get('kind')} -> {after.get('kind')}")
            if not _compatible(before.get("signature"), after.get("signature")):
                broken.append(f"{module}.{name}: {before.get('signature')} -> {after.get('signature')}")
            for member, was in (before.get("members") or {}).items():
                present = (after.get("members") or {}).get(member)
                if present is None:
                    broken.append(f"{module}.{name}.{member}: removed")
                elif was.get("kind") != present.get("kind") or not _compatible(
                    was.get("signature"), present.get("signature")
                ):
                    broken.append(
                        f"{module}.{name}.{member}: {was.get('kind')} {was.get('signature') or ''}"
                        f" -> {present.get('kind')} {present.get('signature') or ''}"
                    )
            for member in (after.get("members") or {}):
                if member not in (before.get("members") or {}):
                    added.append(f"{module}.{name}.{member}")
        for name in now:
            if name not in spec["names"]:
                added.append(f"{module}.{name}")
    for module in new:
        if module not in old:
            added.append(f"module {module}")
    print(f"{len(broken)} breaking change(s)")
    for line in broken:
        print(f"  - {line}")
    print(f"{len(added)} addition(s)")
    for line in added:
        print(f"  + {line}")
    return 1 if broken else 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "dump":
        json.dump(dump(), sys.stdout, indent=1, sort_keys=True)
        return 0
    if len(argv) == 4 and argv[1] == "compare":
        with open(argv[2], encoding="utf-8") as old, open(argv[3], encoding="utf-8") as new:
            return compare(json.load(old), json.load(new))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
