# vite-local-link

A dev tool for linking any package from a sibling repo directly into a consuming
Vite app for live local development — without publishing, without manual config edits.

Works on any machine and folder structure. No hardcoded repo paths.

---

## Install

Requires a Python 3 interpreter on your PATH (`python3` or `python`) — any
version from **3.7 up** (tested through 3.14). Supported on macOS and Linux.

`bin/cli.js` also tries the Windows `py` launcher and forwards through to
`index.py`, but this hasn't been verified on an actual Windows machine yet —
`package.json`'s `os` gate is intentionally left at `darwin`/`linux` only
until someone confirms it. If you've tried it on Windows, open an issue.

```bash
npm install -g vite-local-link
```

Or run it ad hoc with `npx vite-local-link ...` from the consuming app root.

Runs `npm install` / `yarn install` / `pnpm install` after each `link` or
`unlink`, matching whichever lockfile (`yarn.lock`, `pnpm-lock.yaml`, or
neither) is already in the consuming app.

Check what's installed with `vite-local-link --version`.

## Usage

All commands run from the **consuming app root** (the Vite app you're developing against).

### Link a package

```bash
vite-local-link link -p <package-name>
```

Auto-detects the source by scanning sibling directories. If it can't find it,
give an explicit source with `name=path`:

```bash
vite-local-link link -p <package-name>=../<sibling-repo>/src/<Folder>
```

Link several packages in one shot — one `npm install`, not one per package:

```bash
vite-local-link link -p pkg-a pkg-b pkg-c=../shared-repo/src/pkg-c
```

**What `link` does automatically:**
1. Saves the original `package.json` entries for every package linked, and
   (on first link) the original `vite.config.ts`
2. Rewrites matching `package.json` entries to `file:` paths
3. Analyzes each source repo's `package.json` to find companion bare-name
   imports that need Vite aliases (the root cause of React hook crashes with
   local links)
4. Writes the alias sidecar in `local-link/`
5. Patches `vite.config.ts` to read the sidecar (only if not already patched)
6. Runs `npm install` once, after every package is linked
7. Clears `node_modules/.vite` so stale prebundles never survive

### Unlink packages

```bash
vite-local-link unlink -p <package-name> [<package-name> ...]
vite-local-link unlink --all
```

`--all` unlinks everything that's currently active in one command.

**What `unlink` does automatically:**
- Restores original `package.json` entries for every package unlinked
- Recomputes Vite aliases for remaining active links
- **If no links remain: restores `vite.config.ts` to its original state and
  deletes the entire `local-link/` folder** — nothing to commit
- Runs `npm install` once
- Clears the Vite cache

### List active links

```bash
vite-local-link list
```

Shows active links and which Vite aliases are currently applied.

### Find a package source

```bash
vite-local-link find -p <package-name>
```

---

## Example

```bash
# From your consuming app root

# Link multiple packages in one command — one npm install for all of them
vite-local-link link -p my-design-system my-shared-components

# Check what's active
vite-local-link list

# Restore everything at once — vite.config.ts reverts, local-link/ is removed
vite-local-link unlink --all
```

---

## How the React hook crash prevention works

When you link source packages, those source files import bare names
(e.g. `"my-ui-components/Tabs"`) that in the source repo resolve via
`tsconfig baseUrl` to local `.tsx` files. In the consuming app, the same name
resolves to the **published compiled package** — which can re-bundle a
UI dependency with a second copy of React, causing:

```
TypeError: Cannot read properties of null (reading 'useContext')
Warning: Invalid hook call — more than one copy of React
```

`vite-local-link` fixes this in two ways:

1. **`resolve.dedupe`** — forces Vite to always use one copy of React, React DOM,
   React Router. Added to vite.config on first link; removed on last unlink.

2. **Companion aliases** — scans the source repo's `package.json` to find every
   package it maps to local src that the consuming app has as a published `npm:`
   alias. Writes those as `resolve.alias` entries so the source files run against
   the same code they expect. Recomputed on every link/unlink.

---

## File layout

Everything the tool writes lives inside a `local-link/` folder at your app root:

| File | Purpose |
|------|---------|
| `local-link/state.json` | State: active links, original vite.config content, package.json originals |
| `local-link/vite-aliases.json` | Generated Vite aliases (read by patched vite.config at startup) |
| `local-link/.gitignore` | A single `*` — ignores everything in the folder |

The folder gives itself a catch-all `.gitignore`, so none of this is ever
git-tracked and you never need to touch your project's own `.gitignore`.
The whole `local-link/` folder and the `vite.config.ts` patch are **fully
removed** once you unlink the last package (or run `unlink --all`). Nothing
gets committed.

---

## Folder structure

The tool works regardless of how your repos are laid out. It recursively
searches every sibling directory of the consuming app root for a
`package.json` whose `name` matches.

If a package still isn't found, pass `--source` with any path on your machine.
The source path is stored as an absolute path internally and as a relative path
in `package.json` — so it works correctly wherever your repos live.

---

## Development

```bash
python3 test_index.py
```

CI runs this on every push before publishing. See `CHANGELOG.md` for release
history.
