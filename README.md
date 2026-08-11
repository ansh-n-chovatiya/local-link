# vite-local-link

[![Publish to npm](https://github.com/ansh-n-chovatiya/local-link/actions/workflows/publish.yml/badge.svg)](https://github.com/ansh-n-chovatiya/local-link/actions/workflows/publish.yml)
[![npm version](https://img.shields.io/npm/v/vite-local-link.svg)](https://www.npmjs.com/package/vite-local-link)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Installs and runs as `vite-local-link`.**

Testing a change in a shared package against the app that consumes it normally means one of two bad options: publish a throwaway version just to test it, or `npm link` it and then spend an hour chasing the crash that follows:

```
TypeError: Cannot read properties of null (reading 'useContext')
Warning: Invalid hook call — more than one copy of React
```

That crash happens because `npm link` swaps in the *source* package, but the consuming app's bundler still resolves that source's own bare imports (`"my-ui-components/Tabs"`) against the **published, compiled** copy — which drags in a second copy of React alongside the one your app already has.

`vite-local-link` links the source in *and* fixes the resolution so nothing double-bundles: one `package.json` edit, one `vite.config` patch, computed once per link and fully reversible.

---

## Requirements

- A **Python 3** interpreter on `PATH` (`python3` or `python`), version **3.7+**. `bin/cli.js` is a thin Node shim that finds the interpreter and forwards to `index.py` — npm's own cross-platform binary shimming doesn't reliably handle a raw Python shebang on Windows.
- macOS or Linux. The shim also tries the Windows `py` launcher, but `package.json`'s `os` field is intentionally left at `darwin`/`linux` only until someone actually confirms it working on Windows.
- A **Vite** app as the consumer, with a `vite.config.{ts,mts,js,mjs}` at its root.

## Installation

```bash
npm install -g vite-local-link
```

Or skip the install and run it ad hoc from the consuming app root:

```bash
npx vite-local-link link -p my-ui-components
```

## Quick start

Run every command from the **consuming app's root** — the Vite app you're developing against, not the package you're editing.

```bash
# Link a package — auto-detects the source by scanning sibling directories
vite-local-link link -p my-ui-components

# Source not found next to this app? Point at it explicitly:
vite-local-link link -p my-ui-components=../some-other-repo/packages/ui

# Link several packages in one shot — one install, not one per package
vite-local-link link -p pkg-a pkg-b pkg-c=../shared-repo/src/pkg-c

# See what's currently linked
vite-local-link list

# Done testing — restore everything, package.json and vite.config included
vite-local-link unlink --all
```

---

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [CLI reference](#cli-reference)
  - [`link`](#vite-local-link-link)
  - [`unlink`](#vite-local-link-unlink)
  - [`list`](#vite-local-link-list)
  - [`find`](#vite-local-link-find)
  - [`repair`](#vite-local-link-repair)
- [What gets written, and where](#what-gets-written-and-where)
- [The React-hook-crash fix, in detail](#the-react-hook-crash-fix-in-detail)
- [Concurrency and safety](#concurrency-and-safety)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## How it works

Every `link`/`unlink`/`repair` does the same four things, in order:

```
vite-local-link link -p my-ui-components
   │
   ▼
1. Resolve the source
   │  explicit path given? use it.
   │  otherwise: rglob every sibling directory for a package.json
   │  whose "name" matches — no hardcoded repo layout assumed.
   ▼
2. Rewrite package.json
   │  every dependencies/devDependencies/peerDependencies entry that
   │  currently points at the package (plain version, or npm:-aliased)
   │  is saved verbatim, then overwritten with "file:<relative path>".
   ▼
3. Patch vite.config
   │  inject a small IIFE that reads local-link/vite-aliases.json,
   │  add resolve.dedupe for react/react-dom/react-router(-dom),
   │  spread the computed aliases into resolve.alias.
   │  (no-ops if already patched; migrates a stale patch from an
   │  older version.)
   ▼
4. Compute companion aliases
   │  scan the source repo's own files for bare imports, cross-reference
   │  against what the CONSUMING app has published — see below —
   │  write the result to local-link/vite-aliases.json.
   ▼
npm/yarn/pnpm install (once, for every package linked in this call)
clear node_modules/.vite
```

`unlink` runs the same machinery backwards: restore the saved `package.json` values, recompute aliases for whatever links remain, and — once nothing is left linked — revert `vite.config` to the exact bytes it had before the first link and delete the whole `local-link/` folder. There is nothing to commit or clean up by hand at any point.

---

## CLI reference

### `vite-local-link link`

Links one or more local packages into the app at the current directory.

| Flag | Required | Description |
|---|---|---|
| `-p, --package <spec...>` | yes | One or more `name` or `name=path` specs |
| `-s, --source <path>` | no | Explicit source path — only valid with a single `--package`, and only when that package's spec doesn't already carry `=path` |

```bash
vite-local-link link -p my-ui-components
vite-local-link link -p my-ui-components=../my-ui-components-repo/src
vite-local-link link -p pkg-a pkg-b pkg-c
```

What happens automatically, every time: the original `package.json` entries (and, on first link, the original `vite.config`) are saved to `local-link/state.json`; matching entries are rewritten to `file:` paths; the source repo's `package.json` is scanned for companion aliases; the alias sidecar is written; `vite.config` is patched if it isn't already; a single install runs after every package in the command is linked; `node_modules/.vite` is cleared.

Linking a package that's already linked is a no-op with a warning — run `unlink` on it first.

---

### `vite-local-link unlink`

Restores package(s) to their pre-link state.

| Flag | Required | Description |
|---|---|---|
| `-p, --package <name...>` | one of these two | Package(s) to restore |
| `--all` | one of these two | Restore every currently active link |

```bash
vite-local-link unlink -p my-ui-components
vite-local-link unlink --all
```

Restores the exact original `package.json` values (not just "remove the `file:` entry" — a package aliased via `npm:@scope/name@1.2.3` goes back to that exact string). Recomputes aliases for whatever links remain. If this was the last active link, `vite.config` reverts to its original bytes and the entire `local-link/` folder is deleted — nothing from a linking session is left behind to accidentally commit.

---

### `vite-local-link list`

```bash
vite-local-link list
```

Shows every active link (package, source path, and the original `package.json` value it will restore to), plus the currently computed Vite aliases. If `vite.config`'s patch is missing or stale, prints a warning telling you to run `repair`.

---

### `vite-local-link find`

Locates a package's source without linking it — useful to sanity-check auto-detection before committing to a link, or when a source lives somewhere the scan won't reach.

```bash
vite-local-link find -p my-ui-components
```

Prints the resolved path and the relative form, plus a ready-to-paste `link` command using it.

---

### `vite-local-link repair`

Re-applies the `vite.config` patch and recomputes aliases for whatever is currently linked, without touching `package.json`. Doesn't add or remove any links.

```bash
vite-local-link repair
```

Use this after upgrading `vite-local-link` itself mid-session, or if `list` reports the patch as missing/stale — for example after a manual `git checkout` of `vite.config` while links were still active.

---

## What gets written, and where

Everything this tool owns lives under `local-link/` at the app root:

| Path | Purpose |
|---|---|
| `local-link/state.json` | Active links: source path, original `package.json` entries, original `vite.config` content |
| `local-link/vite-aliases.json` | Computed Vite aliases, read by the patched `vite.config` at Vite startup |
| `local-link/.lock` | Held for the duration of a `link`/`unlink`/`repair` call; see [Concurrency and safety](#concurrency-and-safety) |
| `local-link/.gitignore` | A single `*` — created the first time the folder is, so none of this is ever git-tracked and your project's own `.gitignore` is never touched |

The folder is fully removed the moment no links remain (last `unlink`, or `unlink --all`). A version prior to 1.1.0 stored this as dotfiles at the app root (`.local-links.json`, `.local-link-vite.json`) and appended their names to the project's `.gitignore` directly — if those are found, they're migrated into `local-link/` and the appended `.gitignore` lines are cleaned up automatically on the next state-writing command.

`vite.config` itself gets three small, clearly-marked insertions (every inserted line carries a `// @local-link` comment so they're easy to spot in a diff, though none of this should ever end up in a real commit):

1. An IIFE (or its ESM equivalent, chosen by checking the config's file extension and the nearest `package.json`'s `"type"` field) that reads `vite-aliases.json` at startup and exposes the result as `__localLinkAliases`.
2. `resolve.dedupe: ["react", "react-dom", "react-router", "react-router-dom"]`.
3. `...__localLinkAliases` spread as the first entry of `resolve.alias`.

---

## The React-hook-crash fix, in detail

Linking swaps a package's `package.json` entry for `file:../source-repo/src`. Inside that source, imports like `import { Tabs } from "my-ui-components"` are bare specifiers that, in the *source repo*, resolve via its own `tsconfig` `baseUrl` to local `.tsx` files. Nothing in a plain link changes how the *consuming* app resolves that same bare name — it still resolves to whatever that name means in the consuming app's own dependency tree, which is normally the **published, compiled** package.

If any dependency in that chain bundles its own copy of React, Vite's dependency optimizer now has two React copies in the module graph, and React hooks throw the moment a component from one copy renders under a provider from the other.

Two independent fixes handle this:

- **`resolve.dedupe`** forces Vite to resolve `react`/`react-dom`/`react-router(-dom)` to a single copy everywhere in the graph, no matter how many times they appear in `node_modules`. Cheap, and correct on its own for most cases — but not sufficient if the source repo imports a *third* package (a shared UI/utility package, say) that itself isn't deduped and carries its own React.
- **Companion aliases** close that gap. For every active link, the source repo's files are scanned for bare imports (`from`/`import(...)` specifiers not starting with `.` or `/`). Each import is checked against the source repo's *own* `package.json`: does it map that name to its own local `src/`? If so, and the *consuming* app has that same bare name resolving to a `npm:`-aliased published package, the consuming app's `resolve.alias` is pointed at the source repo's local file instead — so both the linked package and your app import the exact same on-disk React (or whatever the shared dependency is), never two separately-bundled copies.

Aliases are skipped when the consuming app already resolves that bare name to its own local `src/<name>` (an alias would hijack those imports) or when the consuming app's own entry already points at the same on-disk path as the source repo's (nothing to fix). Recomputed on every `link`, `unlink`, and `repair`, so aliases never go stale as links change.

---

## Concurrency and safety

`link`, `unlink`, and `repair` take an exclusive lock (`local-link/.lock`, created with `O_EXCL` — atomic at the OS level, no dependency needed) before touching `state.json` or `package.json`, and release it on exit. A second invocation while one is already running fails fast with a clear error instead of interleaving writes and corrupting state.

A run that ends up changing nothing (e.g. `unlink --all` with no active links, or `link` where every package failed to resolve) still creates `local-link/` for the lock file, then removes it again on exit — a no-op never leaves an empty folder behind.

---

## Known limitations

- **Auto-detection scans sibling directories only.** `find`/`link` without `-s`/`--source` `rglob`s every sibling of the app root for a `package.json` with a matching `name` — a source repo nested more than one level away, or living outside the app's parent directory entirely, needs an explicit path.
- **`--source` is single-package only.** Linking several packages in one call with per-package explicit paths needs the inline `name=path` form for each one, not `--source`.
- **Import scanning is regex-based, not a real parser.** `scan_bare_imports` matches `from`/`import(...)` specifier strings via regex across `.ts`/`.tsx`/`.js`/`.jsx` files. Re-exports and dynamic import patterns outside that shape won't be picked up as companion-alias candidates.
- **One `vite.config` patch strategy for all configs.** The patch looks for `resolve: {` and `alias: {` textually; a config that builds its `resolve`/`alias` object through a helper function instead of a literal object won't have the dedupe/alias lines inserted in a useful place, and `repair` will show as needed indefinitely. Restructure that part of the config to an inline literal, or open an issue.

---

## Troubleshooting

**`⚠ Vite patch is missing or out of date` from `list`**

Run `vite-local-link repair`. Common causes: the tool was upgraded while links were active, or `vite.config` was reverted manually (e.g. `git checkout`) without going through `unlink`.

**"another vite-local-link command appears to be running"**

Another `link`/`unlink`/`repair` is genuinely in flight, or a previous run crashed before releasing `local-link/.lock`. If you're sure nothing is running, delete the lock file and retry.

**Hook crash still happens after linking**

Run `vite-local-link list` and check the printed aliases — if the package causing the duplicate copy isn't listed, it's likely not detected as a companion import (see [Known limitations](#known-limitations)), or the consuming app doesn't have it as a `npm:`-aliased dependency in the first place. Restart the dev server after any `link`/`unlink`/`repair` — Vite's dep optimizer caches its bundle across restarts even after `node_modules/.vite` is cleared mid-session.

**A linked package doesn't show the change I just made in its source**

`file:` links are copied by some package managers rather than symlinked, depending on version and lockfile settings. If edits aren't showing up live, check whether your package manager symlinks `file:` dependencies by default.

---

## Development

```bash
python3 test_index.py
```

CI (`.github/workflows/publish.yml`) runs this on every push to `main` before publishing, and only publishes when `package.json`'s version doesn't already match what's on npm.

The suite (`test_index.py`, stdlib `unittest` only, no fixtures) covers spec parsing, `package.json` entry matching, the Vite-config patch/revert round-trip for both the corruption bugs that motivated it, ESM-vs-CJS detection, the lock, and a full `link` → `unlink --all` integration round trip.

---

## License

MIT
