#!/usr/bin/env python3
"""Fails when a module of `vibe` imports a package that is only a DEV dependency.

Why: the packaged binary and the wheel ship runtime dependencies only. Tests run in the dev environment, where
those packages exist, so a stray import passes every test and then crashes the released app at startup (this is
what broke the v3.1.2 release: key_pool.py imported `httpx`, which upstream had moved to the dev group).

It blocks the dev-only packages, then imports every module and reports the ones that fail because of them.
Run it with the project's environment: `uv run python scripts/fork-sync/check-runtime-imports.py`.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

# Packages that exist in the dev environment but not in the shipped one. Keep in step with pyproject.toml
# ([dependency-groups].dev): runtime code uses httpx2, never httpx; respx and pytest are test-only.
DEV_ONLY = ("httpx", "respx", "pytest")

for name in DEV_ONLY:
    sys.modules[name] = None  # type: ignore[assignment]  # an import now raises ImportError

import vibe  # noqa: E402

offenders: dict[str, str] = {}
for module in pkgutil.walk_packages(vibe.__path__, "vibe."):
    try:
        importlib.import_module(module.name)
    except ImportError as error:
        if any(name in str(error) for name in DEV_ONLY):
            offenders[module.name] = str(error)[:100]
    except Exception:  # noqa: BLE001 - unrelated import problems are not what this checks
        pass

if offenders:
    print(f"FAIL: {len(offenders)} module(s) import a dev-only package:")
    for name, why in sorted(offenders.items()):
        print(f"  {name}: {why}")
    sys.exit(1)
print("ok: no vibe module imports a dev-only package")
