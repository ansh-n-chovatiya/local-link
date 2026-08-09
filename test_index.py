#!/usr/bin/env python3
"""
Test suite for index.py. Stdlib unittest only, no fixtures/frameworks.

Run: python3 test_index.py
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import index  # noqa: E402


def _node_check(js_path: Path) -> None:
    """Skip-friendly syntax check via `node --check`, when node is on PATH."""
    if shutil.which("node") is None:
        return
    result = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"node --check failed for {js_path}:\n{result.stderr}")


class TempDirCase(unittest.TestCase):
    """Base class that chdirs into a fresh temp dir and always restores cwd."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()


class ParsePackageSpecTests(unittest.TestCase):
    def test_bare_name(self):
        self.assertEqual(index.parse_package_spec("foo"), ("foo", None))

    def test_name_with_source(self):
        self.assertEqual(index.parse_package_spec("foo=../bar"), ("foo", "../bar"))

    def test_scoped_name_with_source(self):
        self.assertEqual(index.parse_package_spec("@scope/foo=../bar"), ("@scope/foo", "../bar"))


class FindPkgEntriesTests(unittest.TestCase):
    def test_exact_key_match(self):
        pkg = {"dependencies": {"@scope/foo": "1.0.0"}}
        hits = index.find_pkg_entries(pkg, "@scope/foo")
        self.assertEqual(hits, {"dependencies": {"@scope/foo": "1.0.0"}})

    def test_short_name_key_match(self):
        pkg = {"dependencies": {"foo": "npm:@scope/foo@1.0.0"}}
        hits = index.find_pkg_entries(pkg, "@scope/foo")
        self.assertEqual(hits, {"dependencies": {"foo": "npm:@scope/foo@1.0.0"}})

    def test_no_match(self):
        pkg = {"dependencies": {"bar": "1.0.0"}}
        self.assertEqual(index.find_pkg_entries(pkg, "@scope/foo"), {})


class SrcSubpathTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(index._src_subpath("./src/foo"), "src/foo")
        self.assertEqual(index._src_subpath("file:src/foo"), "src/foo")
        self.assertEqual(index._src_subpath("file:./src/foo"), "src/foo")
        self.assertEqual(index._src_subpath("./src"), "src")
        self.assertIsNone(index._src_subpath("1.0.0"))
        self.assertIsNone(index._src_subpath("npm:@x/y@1.0.0"))


class IsEsmConfigTests(TempDirCase):
    def test_mjs_always_esm(self):
        p = Path("vite.config.mjs")
        p.write_text("")
        self.assertTrue(index._is_esm_config(p))

    def test_mts_always_esm(self):
        p = Path("vite.config.mts")
        p.write_text("")
        self.assertTrue(index._is_esm_config(p))

    def test_ts_follows_package_type_module(self):
        Path("package.json").write_text(json.dumps({"type": "module"}))
        p = Path("vite.config.ts")
        p.write_text("")
        self.assertTrue(index._is_esm_config(p))

    def test_ts_defaults_to_cjs_without_type(self):
        Path("package.json").write_text(json.dumps({"name": "app"}))
        p = Path("vite.config.ts")
        p.write_text("")
        self.assertFalse(index._is_esm_config(p))

    def test_js_with_no_package_json_defaults_to_cjs(self):
        p = Path("vite.config.js")
        p.write_text("")
        self.assertFalse(index._is_esm_config(p))


class PatchViteConfigRegressionTests(TempDirCase):
    """Locks in the two real bugs found while building this feature."""

    def test_inline_empty_braces_not_corrupted(self):
        # Regression: `alias: {}` on one line used to have its `}` swallowed
        # by the injected `// @local-link` comment.
        original = (
            "import { defineConfig } from \"vite\";\n\n"
            "export default defineConfig({\n"
            "  resolve: {\n"
            "    alias: {},\n"
            "  },\n"
            "});\n"
        )
        cfg = Path("vite.config.ts")
        cfg.write_text(original)
        Path("package.json").write_text(json.dumps({"type": "module"}))

        saved = index.patch_vite_config(cfg)
        self.assertEqual(saved, original)

        patched = cfg.read_text()
        self.assertNotIn("@local-link},", patched)
        _node_check(cfg)

        index.revert_vite_config(cfg, saved)
        self.assertEqual(cfg.read_text(), original)

    def test_esm_does_not_collide_with_existing_path_fs_imports(self):
        # Regression: the ESM template used to `import path from "path"` /
        # `import fs from "fs"`, colliding with the same lines in a host
        # config that builds resolve.alias entries by hand (very common).
        original = (
            "import path from \"path\";\n"
            "import fs from \"fs\";\n"
            "import { defineConfig } from \"vite\";\n\n"
            "export default defineConfig({\n"
            "  resolve: {\n"
            "    alias: { \"@\": path.resolve(__dirname, \"./src\") },\n"
            "  },\n"
            "});\n"
        )
        cfg = Path("vite.config.ts")
        cfg.write_text(original)
        Path("package.json").write_text(json.dumps({"type": "module"}))

        index.patch_vite_config(cfg)
        patched = cfg.read_text()

        self.assertEqual(patched.count('import path from "path"'), 1)
        self.assertEqual(patched.count('import fs from "fs"'), 1)
        self.assertIn("__localLinkPath", patched)
        self.assertIn("__localLinkFs", patched)
        _node_check(cfg)

    def test_cjs_variant_for_non_module_js_config(self):
        original = (
            "module.exports = {\n"
            "  resolve: {\n"
            "    alias: {},\n"
            "  },\n"
            "};\n"
        )
        cfg = Path("vite.config.js")
        cfg.write_text(original)
        Path("package.json").write_text(json.dumps({"name": "app"}))  # no "type": "module"

        index.patch_vite_config(cfg)
        patched = cfg.read_text()
        self.assertIn('require("fs")', patched)
        self.assertNotIn("import.meta", patched)
        _node_check(cfg)

    def test_patch_is_idempotent(self):
        original = "export default {\n  resolve: {\n    alias: {},\n  },\n};\n"
        cfg = Path("vite.config.ts")
        cfg.write_text(original)
        Path("package.json").write_text(json.dumps({"type": "module"}))

        index.patch_vite_config(cfg)
        once = cfg.read_text()
        index.patch_vite_config(cfg)
        twice = cfg.read_text()
        self.assertEqual(once, twice)
        self.assertEqual(once.count("__localLinkAliases ="), 1)


class EnsureVitePatchCurrentTests(TempDirCase):
    def test_fresh_patch(self):
        cfg = Path("vite.config.ts")
        cfg.write_text("export default {\n  resolve: {\n    alias: {},\n  },\n};\n")
        Path("package.json").write_text(json.dumps({"type": "module"}))
        state = {}

        result = index.ensure_vite_patch_current(Path.cwd(), state)
        self.assertEqual(result.resolve(), cfg.resolve())
        self.assertIn("__vite_config", state)
        self.assertIn(json.dumps(index.VITE_SIDECAR), cfg.read_text())

    def test_noop_when_already_current(self):
        cfg = Path("vite.config.ts")
        cfg.write_text("export default {\n  resolve: {\n    alias: {},\n  },\n};\n")
        Path("package.json").write_text(json.dumps({"type": "module"}))
        state = {}
        index.ensure_vite_patch_current(Path.cwd(), state)
        after_first = cfg.read_text()

        index.ensure_vite_patch_current(Path.cwd(), state)
        self.assertEqual(cfg.read_text(), after_first)

    def test_migrates_stale_patch(self):
        original = "export default {\n  resolve: {\n    alias: {},\n  },\n};\n"
        cfg = Path("vite.config.ts")
        Path("package.json").write_text(json.dumps({"type": "module"}))

        # Simulate a pre-1.1 patch: has the old marker, but points at a
        # different (old) sidecar filename, not the current VITE_SIDECAR.
        stale = (
            "const __localLinkAliases = (() => {\n"
            '  const p = "OLD-SIDECAR-PATH.json";\n'
            "  return {};\n"
            "})();\n"
        ) + original
        cfg.write_text(stale)
        # Must match the absolute path find_vite_config() resolves to, same
        # as the real code stores it (app_dir is always Path.cwd()).
        state = {"__vite_config": {"path": str(cfg.resolve()), "original": original}}

        index.ensure_vite_patch_current(Path.cwd(), state)
        result = cfg.read_text()
        self.assertIn(json.dumps(index.VITE_SIDECAR), result)
        self.assertEqual(result.count("__localLinkAliases ="), 1)

    def test_aborts_instead_of_pretending_success_on_unrecoverable_stale_patch(self):
        # Regression: a stale patch with no recorded __vite_config metadata
        # (or a path mismatch) used to print a warning and then `return
        # vite_cfg` anyway — every caller treated that as "patch is fine"
        # and proceeded to write a sidecar the live config never reads.
        cfg = Path("vite.config.ts")
        cfg.write_text("const __localLinkAliases = (() => ({}))();\nexport default {};\n")
        Path("package.json").write_text(json.dumps({"type": "module"}))
        state = {}  # no __vite_config recorded -> can't safely migrate

        with self.assertRaises(SystemExit):
            index.ensure_vite_patch_current(Path.cwd(), state)
        # And it must not have been silently "fixed" in place.
        self.assertNotIn(json.dumps(index.VITE_SIDECAR), cfg.read_text())


class LinkUnlinkAllIntegrationTests(TempDirCase):
    """Full link -> unlink --all round trip, with npm/yarn/pnpm stubbed out."""

    def _build_fixture(self):
        Path("pkg-a").mkdir()
        (Path("pkg-a") / "package.json").write_text(json.dumps({"name": "pkg-a", "version": "1.0.0"}))
        Path("pkg-b").mkdir()
        (Path("pkg-b") / "package.json").write_text(json.dumps({"name": "pkg-b", "version": "1.0.0"}))

        Path("app").mkdir()
        os.chdir("app")
        self.original_pkg = {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {
                "pkg-a": "npm:pkg-a@1.0.0",
                "pkg-b": "npm:pkg-b@2.0.0",
            },
        }
        Path("package.json").write_text(json.dumps(self.original_pkg))
        self.original_vite_config = (
            "import { defineConfig } from \"vite\";\n\n"
            "export default defineConfig({\n"
            "  resolve: {\n"
            "    alias: {},\n"
            "  },\n"
            "});\n"
        )
        Path("vite.config.ts").write_text(self.original_vite_config)

    @mock.patch.object(index, "run")
    def test_conflicting_source_flag_and_inline_source_errors(self, mock_run):
        # Regression: `-p name=../inline --source ../flag` used to silently
        # use the inline source with no warning, while the exact same kind
        # of conflict (multiple packages + --source) errored loudly.
        self._build_fixture()
        args = argparse.Namespace(package=["pkg-a=../pkg-a"], source="../pkg-b")
        with self.assertRaises(SystemExit):
            index.cmd_link(args)
        mock_run.assert_not_called()
        self.assertFalse(Path("local-link").exists())

    @mock.patch.object(index, "run")
    def test_link_multiple_then_unlink_all(self, mock_run):
        self._build_fixture()

        link_args = argparse.Namespace(package=["pkg-a", "pkg-b"], source=None)
        index.cmd_link(link_args)

        # One install for both packages, not two.
        self.assertEqual(mock_run.call_count, 1)

        pkg = json.loads(Path("package.json").read_text())
        self.assertEqual(pkg["dependencies"]["pkg-a"], "file:../pkg-a")
        self.assertEqual(pkg["dependencies"]["pkg-b"], "file:../pkg-b")

        cfg_text = Path("vite.config.ts").read_text()
        self.assertIn("__localLinkAliases", cfg_text)
        _node_check(Path("vite.config.ts"))

        self.assertTrue(Path("local-link/state.json").exists())
        self.assertTrue(Path("local-link/vite-aliases.json").exists())
        self.assertEqual(Path("local-link/.gitignore").read_text(), "*\n")

        mock_run.reset_mock()
        unlink_args = argparse.Namespace(package=None, all=True)
        index.cmd_unlink(unlink_args)

        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(json.loads(Path("package.json").read_text()), self.original_pkg)
        self.assertEqual(Path("vite.config.ts").read_text(), self.original_vite_config)
        self.assertFalse(Path("local-link").exists())

    @mock.patch.object(index, "run")
    def test_unlink_all_on_clean_tree_is_idempotent(self, mock_run):
        self._build_fixture()
        index.cmd_unlink(argparse.Namespace(package=None, all=True))
        mock_run.assert_not_called()
        # Regression: _acquire_lock() used to create local-link/ (for its
        # .gitignore) before the no-op path returned, leaking an empty
        # folder that was never cleaned up.
        self.assertFalse(Path("local-link").exists())

    @mock.patch.object(index, "run")
    def test_repair_with_nothing_active_leaves_no_directory(self, mock_run):
        self._build_fixture()
        index.cmd_repair(argparse.Namespace())
        mock_run.assert_not_called()
        self.assertFalse(Path("local-link").exists())

    @mock.patch.object(index, "run")
    def test_link_where_every_package_fails_leaves_no_directory(self, mock_run):
        self._build_fixture()
        with self.assertRaises(SystemExit):
            index.cmd_link(argparse.Namespace(package=["nonexistent-pkg"], source=None))
        mock_run.assert_not_called()
        self.assertFalse(Path("local-link").exists())


class LockTests(TempDirCase):
    def test_second_acquire_fails_while_first_held(self):
        lock_path = index._acquire_lock()
        try:
            with self.assertRaises(SystemExit):
                index._acquire_lock()
        finally:
            lock_path.unlink()

    def test_decorator_releases_lock_even_on_sys_exit(self):
        @index.with_state_lock
        def raises():
            sys.exit(1)

        with self.assertRaises(SystemExit):
            raises()
        self.assertFalse(Path(index.LOCK_FILE).exists())

    def test_non_conflict_oserror_exits_cleanly_instead_of_raising(self):
        # Regression: only FileExistsError was caught; a PermissionError or
        # other OSError from os.open used to propagate as a raw traceback.
        with mock.patch.object(os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(SystemExit):
                index._acquire_lock()


class RemoveLegacyGitignoreEntriesTests(TempDirCase):
    def test_untouched_without_legacy_evidence(self):
        content = "node_modules\n.local-links.json\n.local-link-vite.json\nmy-own-line\n"
        Path(".gitignore").write_text(content)
        index._remove_legacy_gitignore_entries()
        self.assertEqual(Path(".gitignore").read_text(), content)

    def test_strips_only_known_lines_when_legacy_state_present(self):
        Path(".local-links.json").write_text("{}")
        Path(".gitignore").write_text("node_modules\n.local-links.json\n.local-link-vite.json\nmy-own-line\n")
        index._remove_legacy_gitignore_entries()
        self.assertEqual(Path(".gitignore").read_text(), "node_modules\nmy-own-line\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
