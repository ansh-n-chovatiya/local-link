#!/usr/bin/env python3
"""
local-link — Link/unlink local packages for live development.

Run from the consuming app root (the Vite app you're developing against).

Commands:
  link    Link one or more local package sources into this app
  unlink  Restore package(s) to their published version
  list    Show all currently active local links
  find    Locate the source directory for a package without linking
  repair  Re-apply Vite patches to existing links (use after a tool upgrade)

Examples:
  vite-local-link link -p my-package                        # auto-detect source
  vite-local-link link -p my-package=../my-package-repo/src # explicit source
  vite-local-link link -p pkg-a pkg-b pkg-c                  # link several at once
  vite-local-link unlink -p my-package
  vite-local-link unlink --all                               # unlink everything, revert vite.config
  vite-local-link list
  vite-local-link find -p my-package
"""

# Defers evaluation of type annotations (PEP 563) so the `X | None` /
# `dict[str, str]` hints below (PEP 604 / 585, Python 3.10 / 3.9+ syntax)
# don't raise TypeError on import under older interpreters — they're only
# ever used as annotations here, never evaluated at runtime.
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STATE_FILE = "local-link/state.json"
PACKAGE_JSON = "package.json"
VITE_SIDECAR = "local-link/vite-aliases.json"

# Pre-1.1 file locations. Read for one-time migration only; never written.
LEGACY_STATE_FILE = ".local-links.json"
LEGACY_VITE_SIDECAR = ".local-link-vite.json"

# Candidate names for the Vite config file (checked in order).
VITE_CONFIG_NAMES = [
    "vite.config.ts",
    "vite.config.mts",
    "vite.config.js",
    "vite.config.mjs",
]

# React-ecosystem singletons that must never be duplicated.
DEDUPE_PACKAGES = [
    "react",
    "react-dom",
    "react-router",
    "react-router-dom",
]

LOCK_FILE = "local-link/.lock"


def _get_version() -> str:
    """Read the version from package.json — the one place it's declared."""
    try:
        pkg_path = Path(__file__).resolve().parent / "package.json"
        return json.loads(pkg_path.read_text()).get("version", "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


__version__ = _get_version()


# ── State ──────────────────────────────────────────────────────────────────

def _ensure_local_link_dir() -> Path:
    """
    Create the local-link/ folder and give it a catch-all .gitignore, so
    nothing inside it is ever git-tracked and nobody has to touch the
    project's own .gitignore.
    """
    d = Path(STATE_FILE).parent
    d.mkdir(parents=True, exist_ok=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return d


def _remove_legacy_gitignore_entries() -> None:
    """
    Pre-1.1 versions appended their state/sidecar filenames straight into the
    project's own .gitignore. Strip those two lines if a legacy install is
    actually detected, so upgrading fully honors "never touch the project
    .gitignore" too. Only runs when there's evidence of a legacy install —
    never rewrites a project .gitignore that has nothing to do with this tool.
    """
    if not (Path(LEGACY_STATE_FILE).exists() or Path(LEGACY_VITE_SIDECAR).exists()):
        return
    gitignore = Path(".gitignore")
    if not gitignore.exists():
        return
    lines = gitignore.read_text().splitlines()
    kept = [l for l in lines if l not in (LEGACY_STATE_FILE, LEGACY_VITE_SIDECAR)]
    if kept != lines:
        text = "\n".join(kept)
        gitignore.write_text((text + "\n") if text else "")


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    if Path(LEGACY_STATE_FILE).exists():
        with open(LEGACY_STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    _remove_legacy_gitignore_entries()  # while legacy files still prove there was one
    _ensure_local_link_dir()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    legacy = Path(LEGACY_STATE_FILE)
    if legacy.exists():
        legacy.unlink()


def remove_local_link_dir() -> None:
    """Delete the whole local-link/ folder — used once no links remain."""
    _remove_legacy_gitignore_entries()  # while legacy files still prove there was one
    d = Path(STATE_FILE).parent
    if d.exists():
        shutil.rmtree(d)
    for legacy in (LEGACY_STATE_FILE, LEGACY_VITE_SIDECAR):
        p = Path(legacy)
        if p.exists():
            p.unlink()


def _acquire_lock() -> Path:
    """
    Atomic create-if-absent via O_EXCL — race-free at the OS level, stdlib
    only. Used to serialize state-mutating commands.
    """
    _ensure_local_link_dir()
    lock_path = Path(LOCK_FILE)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print("Error: another vite-local-link command appears to be running")
        print(f"(lock file: {lock_path}). If that's stale, delete it and retry.")
        sys.exit(1)
    except OSError as e:
        print(f"Error: could not create lock file {lock_path}: {e}")
        sys.exit(1)
    return lock_path


def _remove_local_link_dir_if_empty() -> None:
    """
    _acquire_lock() creates local-link/ (with its .gitignore) before the
    command body runs, even on no-op paths that never write real state
    (unlink --all / repair with nothing active, link where every package
    failed). Clean it back up so a no-op leaves nothing behind — the whole
    point of this folder is that nothing survives when there's nothing to
    track.
    """
    d = Path(STATE_FILE).parent
    if d.exists() and not any(p.name != ".gitignore" for p in d.iterdir()):
        shutil.rmtree(d)


def with_state_lock(fn):
    """
    Decorator serializing link/unlink/repair so two concurrent invocations
    can't interleave writes to local-link/state.json or package.json.
    """
    def wrapper(*args, **kwargs):
        lock_path = _acquire_lock()
        try:
            return fn(*args, **kwargs)
        finally:
            if lock_path.exists():
                lock_path.unlink()
            _remove_local_link_dir_if_empty()
    return wrapper


# ── package.json ────────────────────────────────────────────────────────────

def load_pkg(path: str = PACKAGE_JSON) -> dict:
    with open(path) as f:
        return json.load(f)


def save_pkg(data: dict, path: str = PACKAGE_JSON) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def find_pkg_entries(pkg_data: dict, package_name: str) -> dict:
    """
    Return every package.json entry referencing package_name.

    Handles:
      "@my-scope/my-package": "1.0.63"
      "my-package": "npm:@my-scope/my-package@1.0.63"
      "my-package": "file:..."  (already linked)

    Returns: { section: { key: original_value } }
    """
    short = package_name.split("/")[-1]
    hits = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        if section not in pkg_data:
            continue
        for key, value in pkg_data[section].items():
            is_hit = (
                key == package_name
                or key == short
                or value.startswith(f"npm:{package_name}@")
                or value == f"npm:{package_name}"
            )
            if is_hit:
                hits.setdefault(section, {})[key] = value
    return hits


# ── Source auto-detection ───────────────────────────────────────────────────

def find_source(package_name: str, app_dir: Path) -> Path | None:
    """
    Recursively search every sibling directory of app_dir for a
    package.json whose 'name' field matches package_name.
    """
    parent = app_dir.parent
    if not parent.exists():
        return None
    for pkg_json in parent.rglob("package.json"):
        if "node_modules" in pkg_json.parts:
            continue
        try:
            data = json.loads(pkg_json.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("name") == package_name:
            return pkg_json.parent
    return None


def find_repo_root(source_path: Path) -> Path | None:
    """
    Walk up from source_path to find the nearest ancestor that looks like
    a repo root: has a package.json AND a src/ subdirectory.
    """
    current = source_path.resolve()
    while current != current.parent:
        if (current / "package.json").exists() and (current / "src").is_dir():
            return current
        current = current.parent
    return None


# ── Alias computation ───────────────────────────────────────────────────────

def _src_subpath(value: str) -> str | None:
    """
    Extract the src-relative path from a local package.json dep value.
    Returns None if the value is not a local-source entry.

    Handles: "./src/foo", "file:src/foo", "file:./src/foo", "./src"
    """
    if value.startswith("file:"):
        value = value[5:]       # strip "file:"
    if value.startswith("./"):
        value = value[2:]       # strip "./"
    if value.startswith("src/") or value == "src":
        return value
    return None


def scan_bare_imports(source_path: Path) -> set[str]:
    """
    Scan all .ts/.tsx/.js/.jsx files under source_path for bare module imports.
    Returns root package names (e.g. 'my-ui-components', 'react').
    Bare = not starting with '.' or '/'.
    """
    bare: set[str] = set()
    _IMPORT_RE = re.compile(r"""(?:from|import\s*\()\s*['"]([^./][^'"]*)['"']""")
    for ts_file in source_path.rglob("*"):
        if "node_modules" in ts_file.parts:
            continue
        if ts_file.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        try:
            text = ts_file.read_text(errors="ignore")
        except OSError:
            continue
        for m in _IMPORT_RE.finditer(text):
            specifier = m.group(1)
            if specifier.startswith("@"):
                parts = specifier.split("/")
                bare.add("/".join(parts[:2]) if len(parts) >= 2 else specifier)
            else:
                bare.add(specifier.split("/")[0])
    return bare


def compute_companion_aliases(state: dict, app_dir: Path) -> dict[str, str]:
    """
    For each active link, find packages that:
      1. The SOURCE FILES actually import (bare name import scan)
      2. The SOURCE REPO maps to its own local src (file: in its package.json)
      3. The CONSUMING APP has as a published npm: package

    These are the exact packages that Vite's dep optimizer would pre-bundle as a
    separate compiled artifact, potentially creating a second copy of React.

    Returns: { bare_name: relative_path_from_app_dir }
    """
    aliases: dict[str, str] = {}
    app_pkg = load_pkg()

    # Build: bare-name → value, for all non-scoped deps in the consuming app
    # e.g.  "my-ui-components" → "npm:@my-scope/my-ui-components@1.0.51"
    consuming_bare: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        for key, value in app_pkg.get(section, {}).items():
            if "/" not in key:
                consuming_bare[key] = value

    processed_roots: set[Path] = set()

    for info in state.values():
        source_path = Path(info["source"])

        # Collect bare names actually imported by this source
        print(f"  Scanning imports in {source_path}...")
        imported_bare = scan_bare_imports(source_path)

        repo_root = find_repo_root(source_path)
        if not repo_root or repo_root in processed_roots:
            continue
        processed_roots.add(repo_root)

        repo_pkg_path = repo_root / "package.json"
        if not repo_pkg_path.exists():
            continue

        try:
            repo_pkg = json.loads(repo_pkg_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        for section in ("dependencies", "devDependencies"):
            for key, value in repo_pkg.get(section, {}).items():
                subpath = _src_subpath(value)
                if not subpath:
                    continue

                # Derive the bare name (scoped "@scope/foo" → "foo")
                bare = key.split("/")[-1]

                # Only alias if the source files actually import this package
                if bare not in imported_bare:
                    continue

                if bare not in consuming_bare:
                    continue

                consuming_val = consuming_bare[bare]

                # Skip if consuming app also maps it to local source from same repo
                if consuming_val.startswith("file:"):
                    consuming_abs = (app_dir / consuming_val[5:].lstrip("./")).resolve()
                    source_abs = (repo_root / subpath).resolve()
                    if consuming_abs == source_abs:
                        continue

                # Only alias published (npm:) entries — compiled artifacts that can
                # bundle a second React and break hooks
                if not consuming_val.startswith("npm:"):
                    continue

                # Don't alias if the consuming app has its own src/<bare> directory.
                # That means the app resolves this bare name to its OWN source via
                # tsconfig baseUrl, and our alias would hijack those imports.
                if (app_dir / "src" / bare).is_dir():
                    continue

                alias_target = (repo_root / subpath).resolve()
                if alias_target.exists():
                    rel = os.path.relpath(alias_target, app_dir)
                    aliases[bare] = rel

    return aliases


# ── Vite config ─────────────────────────────────────────────────────────────

def find_vite_config(app_dir: Path) -> Path | None:
    for name in VITE_CONFIG_NAMES:
        p = app_dir / name
        if p.exists():
            return p
    return None


# Sentinel that marks every line local-link inserts so they can be removed cleanly.
_LL = "// @local-link"

# The IIFE that reads the sidecar and exposes __localLinkAliases.
_IIFE = f"""\
{_LL}
const __localLinkAliases = (() => {{
  try {{
    const fs = require("fs"), path = require("path");
    const p = path.resolve(__dirname, {json.dumps(VITE_SIDECAR)});
    if (!fs.existsSync(p)) return {{}};
    const d = JSON.parse(fs.readFileSync(p, "utf-8"));
    return Object.fromEntries(
      Object.entries(d.aliases ?? {{}}).map(([k, v]) => [k, path.resolve(__dirname, v)])
    );
  }} catch {{ return {{}}; }}
}})(); {_LL}"""

# ESM variant (for .ts / .mts / .mjs configs that use import.meta).
# Namespaced import names avoid colliding with the host config's own
# `import path from "path"` / `import fs from "fs"` — extremely common in
# vite.config.ts files that build resolve.alias entries by hand.
_IIFE_ESM = f"""\
{_LL}
import * as __localLinkFs from "node:fs"; {_LL}
import * as __localLinkPath from "node:path"; {_LL}
import {{ fileURLToPath as __localLinkFileURLToPath }} from "node:url"; {_LL}
const __localLinkAliases = (() => {{
  try {{
    const __localLinkDir = __localLinkPath.dirname(__localLinkFileURLToPath(import.meta.url));
    const p = __localLinkPath.join(__localLinkDir, {json.dumps(VITE_SIDECAR)});
    if (!__localLinkFs.existsSync(p)) return {{}};
    const d = JSON.parse(__localLinkFs.readFileSync(p, "utf-8"));
    return Object.fromEntries(
      Object.entries(d.aliases ?? {{}}).map(([k, v]) => [k, __localLinkPath.join(__localLinkDir, v)])
    );
  }} catch {{ return {{}}; }}
}})(); {_LL}"""


def _is_esm_config(config_path: Path) -> bool:
    """
    .mjs/.mts are unambiguously ESM. .ts/.js depend on the nearest
    package.json's "type" field — Vite bundles them as CJS unless it's
    "module", so blindly treating .ts as ESM breaks configs without it.
    """
    if config_path.suffix in (".mjs", ".mts"):
        return True
    try:
        pkg_type = json.loads((config_path.parent / PACKAGE_JSON).read_text()).get("type")
    except (OSError, json.JSONDecodeError):
        pkg_type = None
    return pkg_type == "module"


def patch_vite_config(config_path: Path) -> str:
    """
    Apply the local-link patch to vite.config.  Returns the original content
    so it can be stored for later restoration.
    """
    content = config_path.read_text()
    original = content
    esm = _is_esm_config(config_path)

    # Guard: already patched with the current sidecar path.
    if json.dumps(VITE_SIDECAR) in content:
        return original

    iife = _IIFE_ESM if esm else _IIFE

    # 1. Insert IIFE block immediately before `export default` (ESM) or
    #    `module.exports =` (CJS).
    export_pattern = r"(\nexport default )" if esm else r"(\nmodule\.exports\s*=\s*)"
    if re.search(export_pattern, content):
        content = re.sub(export_pattern, f"\n{iife}\n\\1", content, count=1)
    else:
        # Fallback: prepend
        content = iife + "\n\n" + content

    # 2. Add dedupe to the resolve block.
    #    Pattern: the `resolve:` key inside the config object.
    #    We match `resolve: {` or `resolve:{` and insert dedupe as first key.
    # A trailing "\n" guards against a same-line closing brace (e.g. `resolve: {}`,
    # a common Vite scaffold default) — without it, the `}` would land on the
    # `// @local-link` comment line and get swallowed, breaking the config.
    dedupe_line = f"      dedupe: {json.dumps(DEDUPE_PACKAGES)}, {_LL}"
    if "dedupe:" not in content:
        content = re.sub(
            r"(\bresolve\s*:\s*\{)",
            f"\\1\n{dedupe_line}\n",
            content,
            count=1,
        )

    # 3. Add `...localLinkAliases` spread as the first entry in `alias: {`.
    #    Only touch the FIRST occurrence (the one inside resolve:).
    aliases_line = f"        ...__localLinkAliases, {_LL}"
    if "...__localLinkAliases" not in content:
        content = re.sub(
            r"(\balias\s*:\s*\{)",
            f"\\1\n{aliases_line}\n",
            content,
            count=1,
        )

    config_path.write_text(content)
    return original


def revert_vite_config(config_path: Path, original_content: str) -> None:
    """Restore the vite config to exactly its pre-patch state."""
    config_path.write_text(original_content)


def ensure_vite_patch_current(app_dir: Path, state: dict) -> Path | None:
    """
    Make sure vite.config carries a patch pointing at the current sidecar
    path. No-ops if a current patch is already present. Migrates (revert +
    re-patch) a stale patch left by an older version of this tool. Applies
    a fresh patch if there is none yet. Returns the vite.config path, or
    None if the app has no vite.config.
    """
    vite_cfg = find_vite_config(app_dir)
    if not vite_cfg:
        return None

    content = vite_cfg.read_text()
    if json.dumps(VITE_SIDECAR) in content:
        return vite_cfg  # already current

    if "__localLinkAliases" in content:
        vite_meta = state.get("__vite_config")
        if not vite_meta or Path(vite_meta["path"]) != vite_cfg:
            print(f"\nError: {vite_cfg.name} has a local-link patch from an older")
            print("version, and its original content wasn't recorded, so it can't be")
            print("migrated automatically. Restore it manually (e.g. from git), then")
            print("re-run this command — proceeding would leave the alias sidecar")
            print("pointing at a config that never reads it.")
            sys.exit(1)
        print(f"\nMigrating local-link patch in {vite_cfg.name}...")
        revert_vite_config(vite_cfg, vite_meta["original"])

    print(f"Patching {vite_cfg.name} for local-link mode...")
    original = patch_vite_config(vite_cfg)
    state["__vite_config"] = {"path": str(vite_cfg), "original": original}
    print(f"  ✓ dedupe + alias sidecar enabled in {vite_cfg.name}")
    return vite_cfg


def write_vite_sidecar(aliases: dict[str, str]) -> None:
    _ensure_local_link_dir()
    with open(VITE_SIDECAR, "w") as f:
        json.dump({"aliases": aliases}, f, indent=2)
        f.write("\n")
    legacy = Path(LEGACY_VITE_SIDECAR)
    if legacy.exists():
        legacy.unlink()


# ── Vite cache ──────────────────────────────────────────────────────────────

def clear_vite_cache(app_dir: Path) -> None:
    """Delete Vite's dep-optimizer cache so stale prebundles never survive."""
    cleared = []
    for rel in ["node_modules/.vite"]:
        p = app_dir / rel
        if p.exists():
            shutil.rmtree(p)
            cleared.append(rel)
    if cleared:
        print(f"  Cleared Vite cache: {', '.join(cleared)}")


# ── Shell ───────────────────────────────────────────────────────────────────

def run(cmd: str, cwd: str | None = None) -> None:
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, text=True)
    if result.returncode != 0:
        print(f"\nError: command exited with code {result.returncode}")
        sys.exit(1)


def install_cmd(app_dir: Path) -> str:
    """Match whichever package manager's lockfile is already present."""
    if (app_dir / "pnpm-lock.yaml").exists():
        return "pnpm install"
    if (app_dir / "yarn.lock").exists():
        return "yarn install"
    return "npm install"


# ── Commands ─────────────────────────────────────────────────────────────────

def parse_package_spec(spec: str) -> tuple[str, str | None]:
    """Split 'name' or 'name=source' into (name, source_or_None)."""
    if "=" in spec:
        name, source = spec.split("=", 1)
        return name, source
    return spec, None


@with_state_lock
def cmd_link(args) -> None:
    app_dir = Path.cwd()
    specs = [parse_package_spec(s) for s in args.package]

    if args.source and len(specs) != 1:
        print("Error: --source only works with a single --package.")
        print("For multiple packages, give an explicit source per package instead:")
        print("  vite-local-link link -p name=../path/to/source other-name")
        sys.exit(1)

    if args.source and specs[0][1]:
        package, inline_source = specs[0]
        print(f"Error: both --source and an inline source were given for '{package}'")
        print(f"  (--source {args.source}  vs.  {package}={inline_source})")
        print("Use only one.")
        sys.exit(1)

    state = load_state()
    pkg_data = load_pkg()
    pristine_pkg = load_pkg()  # unmutated snapshot for find_pkg_entries lookups

    linked = []
    failed = False

    for package, source_arg in specs:
        if package in state:
            print(f"'{package}' is already linked. Skipping (run 'unlink' first).")
            failed = True
            continue

        source_arg = source_arg or args.source
        if source_arg:
            source_path = Path(source_arg).resolve()
        else:
            print(f"No source given for '{package}', searching sibling repos...")
            source_path = find_source(package, app_dir)
            if source_path is None:
                print(f"  Could not auto-detect source for '{package}'.")
                print(f"  Re-run with -p {package}=<path>.")
                failed = True
                continue
            print(f"  Found: {source_path}")

        if not source_path.exists():
            print(f"Error: source path does not exist: {source_path}")
            failed = True
            continue

        src_pkg_json = source_path / "package.json"
        if not src_pkg_json.exists():
            print(f"Error: no package.json in {source_path}")
            failed = True
            continue

        src_name = json.loads(src_pkg_json.read_text()).get("name", "")
        if src_name != package:
            print(f"Warning: source package name is '{src_name}', expected '{package}'")

        entries = find_pkg_entries(pristine_pkg, package)
        if not entries:
            print(f"Warning: '{package}' not found in {PACKAGE_JSON}. Continuing anyway.")

        rel_source = os.path.relpath(source_path, app_dir)
        state[package] = {
            "source": str(source_path),
            "rel_source": rel_source,
            "entries": entries,
        }
        for section, keys in entries.items():
            for key in keys:
                pkg_data[section][key] = f"file:{rel_source}"

        linked.append((package, source_path, rel_source))
        print(f"  [{package}]  →  file:{rel_source}")

    if not linked:
        print("\nNothing linked.")
        sys.exit(1)

    vite_cfg = ensure_vite_patch_current(app_dir, state)

    save_pkg(pkg_data)
    save_state(state)

    if vite_cfg:
        active_links = {k: v for k, v in state.items() if not k.startswith("__")}
        aliases = compute_companion_aliases(active_links, app_dir)
        write_vite_sidecar(aliases)
        if aliases:
            print(f"\n  Vite aliases applied ({len(aliases)}):")
            for bare, target in aliases.items():
                print(f"    {bare}  →  {target}")

    cmd = install_cmd(app_dir)
    print(f"\nRunning {cmd}...")
    run(cmd)

    print("\nClearing Vite cache...")
    clear_vite_cache(app_dir)

    names = ", ".join(name for name, *_ in linked)
    print(f"\n✓ Linked {len(linked)} package(s): {names}")
    for package, source_path, _ in linked:
        print(f"  {package}  →  {source_path}")
    print(f"\n  To restore:  vite-local-link unlink -p {names.replace(', ', ' ')}")

    if failed:
        sys.exit(1)


@with_state_lock
def cmd_unlink(args) -> None:
    app_dir = Path.cwd()
    state = load_state()
    active_before = {k: v for k, v in state.items() if not k.startswith("__")}

    if args.all and not active_before:
        print("No active links — nothing to unlink.")
        return

    packages = list(active_before.keys()) if args.all else args.package

    pkg_data = load_pkg()
    unlinked = []

    for package in packages:
        if package not in state:
            print(f"No active link for '{package}'. Skipping.")
            continue

        print(f"Restoring {PACKAGE_JSON} for '{package}':")
        entries = state[package]["entries"]
        for section, keys in entries.items():
            for key, original_value in keys.items():
                if section in pkg_data and key in pkg_data[section]:
                    pkg_data[section][key] = original_value
                    print(f"  [{section}] {key}  →  {original_value}")

        del state[package]
        unlinked.append(package)

    if not unlinked:
        print("\nNothing unlinked.")
        sys.exit(1)

    save_pkg(pkg_data)

    active_links = {k: v for k, v in state.items() if not k.startswith("__")}
    is_last_unlink = len(active_links) == 0

    if is_last_unlink:
        vite_meta = state.get("__vite_config")
        if vite_meta:
            vite_path = Path(vite_meta["path"])
            if vite_path.exists():
                print(f"\nReverting {vite_path.name} to original state...")
                revert_vite_config(vite_path, vite_meta["original"])
                print(f"  ✓ {vite_path.name} restored")
        remove_local_link_dir()
        print("  Removed local-link/ — nothing left to commit.")
    else:
        ensure_vite_patch_current(app_dir, state)
        aliases = compute_companion_aliases(active_links, app_dir)
        write_vite_sidecar(aliases)
        print(f"\n  Vite aliases recomputed ({len(aliases)} remaining)")
        save_state(state)

    cmd = install_cmd(app_dir)
    print(f"\nRunning {cmd}...")
    run(cmd)

    print("\nClearing Vite cache...")
    clear_vite_cache(app_dir)

    print(f"\n✓ Unlinked {len(unlinked)} package(s): {', '.join(unlinked)}")
    if is_last_unlink:
        print("  No more active links — vite.config reverted to original.")


def cmd_list(args) -> None:
    state = load_state()
    active = {k: v for k, v in state.items() if not k.startswith("__")}

    if not active:
        print("No local links active.")
        return

    print(f"Active local links ({len(active)}):\n")
    for package, info in active.items():
        print(f"  {package}")
        print(f"    Source : {info['source']}")
        for section, keys in info["entries"].items():
            for key, original in keys.items():
                print(f"    [{section}] {key} (was: {original})")
        print()

    vite_cfg = find_vite_config(Path.cwd())
    sidecar = Path(VITE_SIDECAR)
    is_current = bool(vite_cfg) and json.dumps(VITE_SIDECAR) in vite_cfg.read_text()

    if not is_current or not sidecar.exists():
        print("⚠  Vite patch is missing or out of date.")
        print("   Run:  vite-local-link repair")
    else:
        data = json.loads(sidecar.read_text())
        aliases = data.get("aliases", {})
        if aliases:
            print(f"Active Vite aliases ({len(aliases)}):")
            for bare, target in aliases.items():
                print(f"  {bare}  →  {target}")
        else:
            print("  No companion aliases needed for current links.")


@with_state_lock
def cmd_repair(args) -> None:
    """
    Re-apply Vite patches to the current state without touching package.json.
    Use this when local-link was upgraded mid-session and the vite.config hasn't
    been patched yet, has a stale patch from an older version, or the sidecar
    is missing.
    """
    app_dir = Path.cwd()
    state = load_state()
    active = {k: v for k, v in state.items() if not k.startswith("__")}

    if not active:
        print("No active links — nothing to repair.")
        return

    vite_cfg = ensure_vite_patch_current(app_dir, state)
    if not vite_cfg:
        print("No vite.config found — nothing to patch.")
        return

    aliases = compute_companion_aliases(active, app_dir)
    write_vite_sidecar(aliases)
    print(f"\nVite aliases written ({len(aliases)}):")
    for bare, target in aliases.items():
        print(f"  {bare}  →  {target}")

    clear_vite_cache(app_dir)
    save_state(state)
    print("\n✓ Repair complete. Restart the dev server.")


def cmd_find(args) -> None:
    package = args.package
    app_dir = Path.cwd()
    print(f"Searching for '{package}' in sibling repos...")
    source = find_source(package, app_dir)
    if source:
        rel = os.path.relpath(source, app_dir)
        print(f"  Found : {source}")
        print(f"  Relative path from here: {rel}")
        print(f"\n  To link:")
        print(f"    vite-local-link link -p {package}={rel}")
    else:
        print(f"  Not found in sibling directories.")
        print(f"  Pass an explicit source:")
        print(f"    vite-local-link link -p {package}=<path/to/source>")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link/unlink local packages for live development.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"vite-local-link {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_link = sub.add_parser("link", help="Link one or more local package sources")
    p_link.add_argument(
        "-p", "--package", required=True, nargs="+", metavar="PACKAGE[=SOURCE]",
        help="One or more packages, e.g. my-package or my-package=../sibling/src",
    )
    p_link.add_argument(
        "-s", "--source", default=None,
        help="Path to source (only with a single --package; auto-detected if omitted)",
    )

    p_unlink = sub.add_parser("unlink", help="Restore package(s) to published version")
    unlink_group = p_unlink.add_mutually_exclusive_group(required=True)
    unlink_group.add_argument("-p", "--package", nargs="+", help="One or more packages to restore")
    unlink_group.add_argument("--all", action="store_true", help="Unlink every active package")

    sub.add_parser("list", help="Show all active local links")

    p_find = sub.add_parser("find", help="Locate the source directory for a package")
    p_find.add_argument("-p", "--package", required=True, help="e.g. @my-scope/my-package")

    sub.add_parser("repair", help="Re-apply Vite patches to existing links (use after tool upgrade)")

    args = parser.parse_args()

    if not Path(PACKAGE_JSON).exists():
        print(f"Error: no {PACKAGE_JSON} found.")
        print("Run this from the consuming app root (the Vite app you're developing against).")
        sys.exit(1)

    dispatch = {"link": cmd_link, "unlink": cmd_unlink, "list": cmd_list, "find": cmd_find, "repair": cmd_repair}
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
