# local-link

A dev tool for linking any package from a sibling repo directly into a consuming
app for live local development — without publishing, without manual config edits.

Works on any machine and folder structure. No hardcoded repo paths.

---

## Usage

All commands run from the **consuming app root** (e.g. `concierge-ui/`).

### Link a package

```bash
python3 ../local-link/index.py link -p @RHCommerceDev/<package-name>
```

Auto-detects the source by scanning sibling directories. If it can't find it:

```bash
python3 ../local-link/index.py link -p @RHCommerceDev/<package-name> -s ../estore-ui/src/pages/<Folder>
```

**What `link` does automatically:**
1. Saves the original `package.json` entries and (on first link) the original `vite.config.ts`
2. Rewrites matching `package.json` entries to `file:` paths
3. Analyzes the source repo's `package.json` to find companion bare-name imports
   that need Vite aliases (the root cause of React hook crashes with local links)
4. Writes `.local-link-vite.json` with those aliases
5. Patches `vite.config.ts` to read the sidecar (first link only)
6. Runs `npm install`
7. Clears `node_modules/.vite` so stale prebundles never survive
8. Adds `.local-links.json` and `.local-link-vite.json` to `.gitignore`

### Unlink a package

```bash
python3 ../local-link/index.py unlink -p @RHCommerceDev/<package-name>
```

**What `unlink` does automatically:**
- Restores original `package.json` entries
- Recomputes Vite aliases for remaining active links
- **If this was the last link: restores `vite.config.ts` to its original state
  and removes `.local-link-vite.json`** — nothing to commit
- Runs `npm install`
- Clears the Vite cache

### List active links

```bash
python3 ../local-link/index.py list
```

Shows active links and which Vite aliases are currently applied.

### Find a package source

```bash
python3 ../local-link/index.py find -p @RHCommerceDev/<package-name>
```

---

## Real examples

```bash
# From concierge-ui/

# Link NewPDP + PDP Switcher
python3 ../local-link/index.py link -p @RHCommerceDev/new-pdp
python3 ../local-link/index.py link -p @RHCommerceDev/component-rhr-pdp-switcher

# Check what's active
python3 ../local-link/index.py list

# Restore — vite.config.ts reverts to original on last unlink
python3 ../local-link/index.py unlink -p @RHCommerceDev/new-pdp
python3 ../local-link/index.py unlink -p @RHCommerceDev/component-rhr-pdp-switcher
```

---

## How the React hook crash prevention works

When you link source packages, those source files import bare names
(e.g. `"unstyled-ui-components/Tabs"`) that in the source repo resolve via
`tsconfig baseUrl` to local `.tsx` files. In the consuming app, the same name
resolves to the **published compiled package** — which can re-bundle `@radix-ui`
with a second copy of React, causing:

```
TypeError: Cannot read properties of null (reading 'useContext')
Warning: Invalid hook call — more than one copy of React
```

`local-link` fixes this in two ways:

1. **`resolve.dedupe`** — forces Vite to always use one copy of React, React DOM,
   React Router. Added to vite.config on first link; removed on last unlink.

2. **Companion aliases** — scans the source repo's `package.json` to find every
   package it maps to local src that the consuming app has as a published `npm:`
   alias. Writes those as `resolve.alias` entries so the source files run against
   the same code they expect. Recomputed on every link/unlink.

---

## File layout

| File | Purpose |
|------|---------|
| `.local-links.json` | State: active links, original vite.config content, package.json originals |
| `.local-link-vite.json` | Generated Vite aliases (read by patched vite.config at startup) |

Both files are local-only and automatically added to `.gitignore`.
The `vite.config.ts` patch and both files are **fully removed** when you unlink
the last package. Nothing gets committed.

---

## Folder structure

The tool works regardless of how your repos are laid out. It searches:
1. Standard sibling paths (`../estore-ui`, `../concierge-ui`, `../shop-ui-develop`, etc.)
2. All immediate siblings of the consuming app root as a fallback

If a package still isn't found, pass `--source` with any path on your machine.
The source path is stored as an absolute path internally and as a relative path
in `package.json` — so it works correctly wherever your repos live.
