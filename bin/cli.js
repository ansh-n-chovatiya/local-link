#!/usr/bin/env node
"use strict";

// npm's bin-shim mechanism is only truly cross-platform for .js targets run
// through node — a raw Python shebang isn't reliably shimmed on Windows, and
// Windows Python installs commonly expose "py" or "python", not "python3".
// This wrapper is the actual `bin` entry; it just finds an interpreter and
// forwards to index.py.

const { spawnSync } = require("child_process");
const path = require("path");

const scriptPath = path.join(__dirname, "..", "index.py");
const candidates =
  process.platform === "win32" ? ["py", "python3", "python"] : ["python3", "python"];

let result;
for (const cmd of candidates) {
  result = spawnSync(cmd, [scriptPath, ...process.argv.slice(2)], { stdio: "inherit" });
  if (!result.error) break;
}

if (!result || result.error) {
  console.error(`Error: could not find a Python 3 interpreter (tried: ${candidates.join(", ")}).`);
  console.error("Install Python 3 and make sure it's on your PATH.");
  process.exit(1);
}

process.exit(result.status ?? 1);
