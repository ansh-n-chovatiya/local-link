# Changelog

## 1.2.4

- Fixed: `unlink --all` (or the final `unlink -p`) claimed "vite.config
  reverted to original" even when the revert was silently skipped — because
  `state.json`'s `__vite_config` record was missing or stale, or the config
  file had moved — and then deleted `local-link/`, the only place the
  pre-patch content was recorded. Now only claims success when the revert
  actually happens, and warns instead of pretending otherwise.

## 1.2.3

- Fixed: `find_pkg_entries` matched any `package.json` key equal to the
  target package's short name, regardless of that key's value — so linking
  a scoped package (`@scope/foo`) could clobber an unrelated bare-name
  dependency (`foo: file:../something-else`) that just happened to share
  the short name, overwriting its `file:` link with the scoped package's
  source path. Now only matches the short-name key when its value is
  genuinely an `npm:`-aliased reference to the target package.
- Reordered the README so Requirements, Installation, and Quick start come
  right after the intro, ahead of the rest of the docs.

## 1.2.0

- `--version` flag, sourced from `package.json` (single source of truth).
- Detects `pnpm-lock.yaml` / `yarn.lock` and runs the matching install command
  instead of always shelling out to `npm install`.
- The `bin` entry is now a small Node wrapper (`bin/cli.js`) that finds a
  Python 3 interpreter and forwards to `index.py`, instead of relying on
  npm's cross-platform shimming of a raw `.py` shebang. It also tries the
  Windows `py` launcher, but `package.json`'s `os` gate stays at
  `darwin`/`linux` until Windows is actually verified — this is prep, not a
  support claim.
- `link` / `unlink` / `repair` now take an exclusive lock (`local-link/.lock`)
  so two concurrent invocations can't corrupt `state.json` or `package.json`.
  Fixed a bug in the same change where a no-op run (e.g. `unlink --all` on a
  clean tree) left an empty `local-link/` behind instead of nothing.
- Added `LICENSE` (MIT, matching the `license` field that was already
  declared) and this changelog.
- Added a test suite (`test_index.py`, 30 cases) covering `patch_vite_config` /
  `revert_vite_config`, the two config-corruption bugs fixed in 1.1.0,
  `_is_esm_config`, the lock, and a full link → unlink --all integration
  round-trip. CI now runs it before every publish.
- Fixed (found by independent review): `ensure_vite_patch_current()` would
  print a warning that a stale `vite.config.ts` patch couldn't be migrated
  automatically ("restore it manually... before continuing") and then
  return as if the patch were current anyway — every command proceeded to
  write a fresh alias sidecar the live config never reads, silently
  bringing back the duplicate-React bug this tool exists to prevent. Now
  exits with an error instead.
- Fixed: `_acquire_lock()` only caught `FileExistsError`; a permission error
  or other `OSError` creating the lock file surfaced as a raw traceback
  instead of a clean message.
- Fixed: `link -p name=../inline-source --source ../other-path` silently
  used the inline source with no warning, inconsistent with the loud error
  for the same kind of conflict when linking multiple packages. Now errors.

## 1.1.0

- **Breaking:** state and the generated Vite alias sidecar moved from
  dotfiles at the app root (`.local-links.json`, `.local-link-vite.json`)
  into a `local-link/` folder, which gets its own catch-all `.gitignore` on
  creation. The project's own `.gitignore` is no longer touched; an old
  version's entries there are cleaned up automatically on first upgrade.
- `link` accepts multiple packages in one call (`link -p a b c`, or
  `name=source` for an explicit path) — one `npm install` for all of them
  instead of one per package.
- `unlink --all` restores every active package and fully reverts
  `vite.config.ts` in one command.
- Fixed: a same-line `alias: {}` (a common Vite scaffold default) had its
  closing brace swallowed by the injected comment, corrupting the config.
- Fixed: the ESM patch imported `path`/`fs` under names that collided with
  a host config's own `import path from "path"` — namespaced now.
- Fixed: `.ts`/`.js` config format now follows the nearest `package.json`
  `"type"` field (matches Vite's own `isFilePathESM`) instead of assuming
  `.ts` is always ESM.
- Fixed: importing this module under Python < 3.10 raised `TypeError` from
  `X | None` / `dict[str, str]` style annotations. Added
  `from __future__ import annotations`; supported floor is Python 3.7+.
- Generalized away from a single company's fork layout (dropped hardcoded
  sibling-repo names and org-scoped examples); `find_source` now just
  recursively searches every sibling directory.

## 1.0.0

Initial release: `link`, `unlink`, `list`, `find` for linking a local sibling
package into a consuming Vite app, with `resolve.dedupe` + companion Vite
aliases to prevent duplicate-React hook crashes.
