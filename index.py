#!/usr/bin/env python3
"""
local-link — Link/unlink local packages for live development.

Run from the consuming app root (e.g. concierge-ui/).

Commands:
  link    Link a local package source into this app
  unlink  Restore a package to its published version
  list    Show all currently active local links
  find    Locate the source directory for a package without linking

Examples:
  python3 ../local-link/index.py link -p @RHCommerceDev/new-pdp -s ../estore-ui/src/pages/NewPDP
  python3 ../local-link/index.py link -p @RHCommerceDev/new-pdp          # auto-detect source
  python3 ../local-link/index.py unlink -p @RHCommerceDev/new-pdp
  python3 ../local-link/index.py list
  python3 ../local-link/index.py find -p @RHCommerceDev/new-pdp
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STATE_FILE = ".local-links.json"
PACKAGE_JSON = "package.json"
VITE_SIDECAR = ".local-link-vite.json"

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


# Sibling directories to search when auto-detecting source paths.
# These are resolved relative to the consuming app root at runtime.
DEFAULT_SEARCH_ROOTS = [
    "../estore-ui",
    "../concierge-ui",
    "../shop-ui-develop",
    "../shared-ui",
    "..",  # catch-all: any immediate sibling repo
]


# ── State ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


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
      "@RHCommerceDev/new-pdp": "1.0.63"
      "new-pdp": "npm:@RHCommerceDev/new-pdp@1.0.63"
      "new-pdp": "file:..."  (already linked)

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
    Search sibling directories for a package.json whose 'name' field
    matches package_name.  Searches DEFAULT_SEARCH_ROOTS first, then
    walks every immediate sibling of the app_dir as a fallback.
    """
    checked: set[Path] = set()

    def scan_root(root: Path) -> Path | None:
        if not root.exists() or root in checked:
            return None
        checked.add(root)
        for pkg_json in root.rglob("package.json"):
            if "node_modules" in pkg_json.parts:
                continue
            try:
                data = json.loads(pkg_json.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("name") == package_name:
                return pkg_json.parent
        return None

    # 1. Check configured search roots
    for rel in DEFAULT_SEARCH_ROOTS:
        result = scan_root((app_dir / rel).resolve())
        if result:
            return result

    # 2. Fallback: all immediate siblings of the app directory
    parent = app_dir.parent
    for sibling in sorted(parent.iterdir()):
        if sibling.is_dir() and sibling != app_dir:
            result = scan_root(sibling)
            if result:
                return result

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
    Returns root package names (e.g. 'unstyled-ui-components', 'react').
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
    # e.g.  "unstyled-ui-components" → "npm:@RHCommerceDev/unstyled-ui-components@1.0.51"
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
    const p = path.resolve(__dirname, ".local-link-vite.json");
    if (!fs.existsSync(p)) return {{}};
    const d = JSON.parse(fs.readFileSync(p, "utf-8"));
    return Object.fromEntries(
      Object.entries(d.aliases ?? {{}}).map(([k, v]) => [k, path.resolve(__dirname, v)])
    );
  }} catch {{ return {{}}; }}
}})(); {_LL}"""

# ESM variant (for .ts / .mts / .mjs configs that use import.meta)
_IIFE_ESM = f"""\
{_LL}
import fs from "fs"; {_LL}
const __localLinkAliases = (() => {{
  try {{
    const p = new URL(".local-link-vite.json", import.meta.url).pathname;
    if (!fs.existsSync(p)) return {{}};
    const d = JSON.parse(fs.readFileSync(p, "utf-8"));
    const dir = new URL(".", import.meta.url).pathname;
    return Object.fromEntries(
      Object.entries(d.aliases ?? {{}}).map(([k, v]) => [k, path.join(dir, v)])
    );
  }} catch {{ return {{}}; }}
}})(); {_LL}"""


def _is_esm_config(config_path: Path) -> bool:
    return config_path.suffix in (".ts", ".mts", ".mjs")


def patch_vite_config(config_path: Path) -> str:
    """
    Apply the local-link patch to vite.config.  Returns the original content
    so it can be stored for later restoration.
    """
    content = config_path.read_text()
    original = content
    esm = _is_esm_config(config_path)

    # Guard: already patched
    if "__localLinkAliases" in content:
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
    dedupe_line = f"      dedupe: {json.dumps(DEDUPE_PACKAGES)}, {_LL}"
    if "dedupe:" not in content:
        content = re.sub(
            r"(\bresolve\s*:\s*\{)",
            f"\\1\n{dedupe_line}",
            content,
            count=1,
        )

    # 3. Add `...localLinkAliases` spread as the first entry in `alias: {`.
    #    Only touch the FIRST occurrence (the one inside resolve:).
    aliases_line = f"        ...__localLinkAliases, {_LL}"
    if "...__localLinkAliases" not in content:
        content = re.sub(
            r"(\balias\s*:\s*\{)",
            f"\\1\n{aliases_line}",
            content,
            count=1,
        )

    config_path.write_text(content)
    return original


def revert_vite_config(config_path: Path, original_content: str) -> None:
    """Restore the vite config to exactly its pre-patch state."""
    config_path.write_text(original_content)


def write_vite_sidecar(aliases: dict[str, str]) -> None:
    with open(VITE_SIDECAR, "w") as f:
        json.dump({"aliases": aliases}, f, indent=2)
        f.write("\n")


def remove_vite_sidecar() -> None:
    p = Path(VITE_SIDECAR)
    if p.exists():
        p.unlink()


# ── .gitignore ──────────────────────────────────────────────────────────────

def ensure_gitignored(app_dir: Path) -> None:
    """Add local-link runtime files to .gitignore if not already present."""
    entries = [STATE_FILE, VITE_SIDECAR]
    gitignore = app_dir / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    additions = [e for e in entries if e not in existing]
    if additions:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        with open(gitignore, "a") as f:
            f.write(sep + "\n".join(additions) + "\n")
        print(f"  Added to .gitignore: {', '.join(additions)}")


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


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_link(args) -> None:
    package = args.package
    app_dir = Path.cwd()

    # Resolve source path
    if args.source:
        source_path = Path(args.source).resolve()
    else:
        print(f"No --source given, searching for '{package}'...")
        source_path = find_source(package, app_dir)
        if source_path is None:
            print(f"\nCould not auto-detect source for '{package}'.")
            print("Re-run with --source <path>.")
            sys.exit(1)
        print(f"Found: {source_path}\n")

    if not source_path.exists():
        print(f"Error: source path does not exist: {source_path}")
        sys.exit(1)

    src_pkg_json = source_path / "package.json"
    if not src_pkg_json.exists():
        print(f"Error: no package.json in {source_path}")
        sys.exit(1)

    src_name = json.loads(src_pkg_json.read_text()).get("name", "")
    if src_name != package:
        print(f"Warning: source package name is '{src_name}', expected '{package}'")

    state = load_state()
    if package in state:
        print(f"'{package}' is already linked. Run 'unlink' first.")
        sys.exit(1)

    pkg_data = load_pkg()
    entries = find_pkg_entries(pkg_data, package)
    if not entries:
        print(f"Warning: '{package}' not found in {PACKAGE_JSON}. Continuing anyway.")

    rel_source = os.path.relpath(source_path, app_dir)
    is_first_link = len(state) == 0

    state[package] = {
        "source": str(source_path),
        "rel_source": rel_source,
        "entries": entries,
    }

    # On the first link: patch vite.config and save the original content
    vite_cfg = find_vite_config(app_dir)
    if vite_cfg and is_first_link:
        print(f"\nPatching {vite_cfg.name} for local-link mode...")
        original = patch_vite_config(vite_cfg)
        state["__vite_config"] = {
            "path": str(vite_cfg),
            "original": original,
        }
        print(f"  ✓ dedupe + alias sidecar enabled in {vite_cfg.name}")

    save_state(state)

    # Update package.json entries
    for section, keys in entries.items():
        for key in keys:
            pkg_data[section][key] = f"file:{rel_source}"
    save_pkg(pkg_data)

    print(f"\nUpdated {PACKAGE_JSON}:")
    for section, keys in entries.items():
        for key, original in keys.items():
            print(f"  [{section}] {key}: {original}  →  file:{rel_source}")

    # Recompute companion aliases for all active links
    active_links = {k: v for k, v in state.items() if not k.startswith("__")}
    aliases = compute_companion_aliases(active_links, app_dir)
    if aliases:
        write_vite_sidecar(aliases)
        print(f"\n  Vite aliases applied ({len(aliases)}):")
        for bare, target in aliases.items():
            print(f"    {bare}  →  {target}")
    elif vite_cfg:
        write_vite_sidecar({})

    print("\nRunning npm install...")
    run("npm install")

    print("\nClearing Vite cache...")
    clear_vite_cache(app_dir)

    ensure_gitignored(app_dir)

    print(f"\n✓ Linked: {package}")
    print(f"  Source : {source_path}")
    print(f"  Edit files there and the dev server will pick up changes live.")
    print(f"\n  To restore:  vite-local-link unlink -p {package}")


def cmd_unlink(args) -> None:
    package = args.package
    app_dir = Path.cwd()
    state = load_state()

    if package not in state:
        print(f"No active link for '{package}'. Nothing to restore.")
        sys.exit(1)

    entries = state[package]["entries"]

    pkg_data = load_pkg()
    print(f"Restoring {PACKAGE_JSON}:")
    for section, keys in entries.items():
        for key, original_value in keys.items():
            if section in pkg_data and key in pkg_data[section]:
                pkg_data[section][key] = original_value
                print(f"  [{section}] {key}  →  {original_value}")
    save_pkg(pkg_data)

    del state[package]

    active_links = {k: v for k, v in state.items() if not k.startswith("__")}
    is_last_unlink = len(active_links) == 0

    if is_last_unlink:
        # Revert vite.config to its original state
        vite_meta = state.get("__vite_config")
        if vite_meta:
            vite_path = Path(vite_meta["path"])
            if vite_path.exists():
                print(f"\nReverting {vite_path.name} to original state...")
                revert_vite_config(vite_path, vite_meta["original"])
                print(f"  ✓ {vite_path.name} restored")
            del state["__vite_config"]

        remove_vite_sidecar()
        print(f"  Removed {VITE_SIDECAR}")
    else:
        # Still some links active — recompute aliases without this package
        aliases = compute_companion_aliases(active_links, app_dir)
        write_vite_sidecar(aliases)
        print(f"\n  Vite aliases recomputed ({len(aliases)} remaining)")

    save_state(state)

    print("\nRunning npm install...")
    run("npm install")

    print("\nClearing Vite cache...")
    clear_vite_cache(app_dir)

    print(f"\n✓ Unlinked: {package} restored to published version.")
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

    sidecar = Path(VITE_SIDECAR)
    vite_meta = state.get("__vite_config")

    if not vite_meta or not sidecar.exists():
        print("⚠  Vite patches not yet applied (tool was upgraded mid-session).")
        print(f"   Run:  vite-local-link repair")
    elif sidecar.exists():
        data = json.loads(sidecar.read_text())
        aliases = data.get("aliases", {})
        if aliases:
            print(f"Active Vite aliases ({len(aliases)}):")
            for bare, target in aliases.items():
                print(f"  {bare}  →  {target}")
        else:
            print("  No companion aliases needed for current links.")


def cmd_repair(args) -> None:
    """
    Re-apply Vite patches to the current state without touching package.json.
    Use this when local-link was upgraded mid-session and the vite.config hasn't
    been patched yet, or when the sidecar is missing.
    """
    app_dir = Path.cwd()
    state = load_state()
    active = {k: v for k, v in state.items() if not k.startswith("__")}

    if not active:
        print("No active links — nothing to repair.")
        return

    vite_cfg = find_vite_config(app_dir)
    if not vite_cfg:
        print("No vite.config found — nothing to patch.")
        return

    needs_patch = "__vite_config" not in state or "__localLinkAliases" not in vite_cfg.read_text()

    if needs_patch:
        print(f"Patching {vite_cfg.name}...")
        original = patch_vite_config(vite_cfg)
        state["__vite_config"] = {
            "path": str(vite_cfg),
            "original": original,
        }
        print(f"  ✓ {vite_cfg.name} patched")
    else:
        print(f"  {vite_cfg.name} is already patched")

    aliases = compute_companion_aliases(active, app_dir)
    write_vite_sidecar(aliases)
    print(f"\nVite aliases written ({len(aliases)}):")
    for bare, target in aliases.items():
        print(f"  {bare}  →  {target}")

    clear_vite_cache(app_dir)
    ensure_gitignored(app_dir)
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
        print(f"    vite-local-link link -p {package} -s {rel}")
    else:
        print(f"  Not found in sibling directories.")
        print(f"  Pass --source manually:")
        print(f"    vite-local-link link -p {package} -s <path/to/source>")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link/unlink local packages for live development.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_link = sub.add_parser("link", help="Link a local package source")
    p_link.add_argument("-p", "--package", required=True, help="e.g. @RHCommerceDev/new-pdp")
    p_link.add_argument("-s", "--source", default=None, help="Path to source (auto-detected if omitted)")

    p_unlink = sub.add_parser("unlink", help="Restore package to published version")
    p_unlink.add_argument("-p", "--package", required=True, help="Package to restore")

    sub.add_parser("list", help="Show all active local links")

    p_find = sub.add_parser("find", help="Locate the source directory for a package")
    p_find.add_argument("-p", "--package", required=True, help="e.g. @RHCommerceDev/new-pdp")

    sub.add_parser("repair", help="Re-apply Vite patches to existing links (use after tool upgrade)")

    args = parser.parse_args()

    if not Path(PACKAGE_JSON).exists():
        print(f"Error: no {PACKAGE_JSON} found.")
        print("Run this script from the app root (e.g. concierge-ui/).")
        sys.exit(1)

    dispatch = {"link": cmd_link, "unlink": cmd_unlink, "list": cmd_list, "find": cmd_find, "repair": cmd_repair}
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
