#!/usr/bin/env node
// Generates .agents/code-map.md: every source file with its file-level summary, functions,
// classes, routes and components, and a one-line summary, so AI models can just look at
// the description and jump in for everything without line numbers that shift.
//
// Regenerate after changing code:   node .agents/gen_code_map.mjs
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, ".agents", "code-map.md");
const SOURCE = /\.(py|ts|tsx|js|jsx|mjs)$/;
const SKIP = /(^|\/)(node_modules|dist|build|coverage|migrations|alembic|__pycache__|\.agents|public)\//;
const SKIP_FILE = /(\.d\.ts|\.min\.js|vite-env\.d\.ts|\.test\.|\.spec\.|setupTests)/;
// Files above this size also list their nested handlers/helpers, because those are what agents hunt for.
const NESTED_THRESHOLD = 400;

const files = execSync("git ls-files", { cwd: ROOT, encoding: "utf8" })
  .split("\n")
  .filter((f) => SOURCE.test(f) && !SKIP.test(f) && !SKIP_FILE.test(f))
  .sort();

function summary(text) {
  const s = text.replace(/^[\s*/#"'`]+|[\s*/"'`]+$/g, "").trim();
  const first = s.split(/(?<=\.)\s/)[0];
  return first.length > 140 ? first.slice(0, 137) + "..." : first;
}

function fileSummary(text, filename) {
  // Python docstring at top
  const pyMatch = text.match(/^\s*(?:"""|''')([\s\S]*?)(?:"""|''')/);
  if (pyMatch) {
    const s = pyMatch[1].replace(/^\s*\*|\s\*\s/g, " ").replace(/\s+/g, " ").trim();
    if (s) {
      const first = s.split(/(?<=\.)\s/)[0];
      return first.length > 140 ? first.slice(0, 137) + "..." : first;
    }
  }

  // JS/TS block comment at top
  const jsMatch = text.match(/^\s*\/\*\*?([\s\S]*?)\*\//);
  if (jsMatch) {
    const s = jsMatch[1].replace(/^\s*\*|\s\*\s/g, " ").replace(/\s+/g, " ").trim();
    if (s) {
      const first = s.split(/(?<=\.)\s/)[0];
      return first.length > 140 ? first.slice(0, 137) + "..." : first;
    }
  }

  // Any comment in the first 25 lines (including comments right after imports)
  const lines = text.split(/\r?\n/).slice(0, 25);
  for (const l of lines) {
    const m = l.match(/^\s*(?:\/\/|#|\/\*\*?)\s*([A-Za-z0-9].*)/);
    if (m && !m[1].startsWith("eslint") && !m[1].startsWith("@ts-") && !m[1].startsWith("!/") && !m[1].startsWith("import")) {
      const clean = m[1].replace(/\*\/$/, "").trim();
      if (clean.length > 10) return summary(clean);
    }
  }

  // Meaningful human-readable fallback with directory context
  const parts = filename.split("/");
  const name = parts.pop().replace(/\.[^.]+$/, "");
  const dir = parts.slice(1).join(" ");
  const cleanName = name.replace(/([A-Z])/g, " $1").replace(/[_-]+/g, " ").toLowerCase().trim();
  const cleanDir = dir.replace(/([A-Z])/g, " $1").replace(/[_-]+/g, " ").toLowerCase().trim();
  return cleanDir ? `${cleanName} (${cleanDir})` : cleanName;
}

function pyEntries(lines) {
  const out = [];
  let decorators = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const dec = line.match(/^(\s{0,4})@(\w+)\.(get|post|put|patch|delete|websocket)\(\s*["']([^"']*)["']/);
    if (dec) {
      decorators.push(`${dec[3].toUpperCase()} ${dec[4]}`);
      continue;
    }
    const m = line.match(/^(\s{0,4})(async\s+def|def|class)\s+(\w+)/);
    if (!m) {
      if (!/^\s*@/.test(line)) decorators = [];
      continue;
    }
    const indent = m[1].length;
    const kind = m[2] === "class" ? "class" : "def";
    // Find the end of the signature, then look for a docstring.
    let j = i;
    while (j < lines.length && !/:\s*(#.*)?$/.test(lines[j])) j++;
    let doc = "";
    let k = j + 1;
    while (k < lines.length && lines[k].trim() === "") k++;
    if (k < lines.length && /^\s*[rbu]?("""|''')/.test(lines[k])) {
      let body = lines[k].trim().replace(/^[rbu]?("""|''')/, "");
      while (!body.trim() && k + 1 < lines.length) body = lines[++k].trim();
      doc = summary(body.replace(/("""|''').*$/, ""));
    }
    out.push({ depth: indent ? 1 : 0, name: m[3], kind, route: decorators.join(", "), doc });
    decorators = [];
  }
  return out;
}

function tsEntries(lines, nested) {
  const out = [];
  const top =
    /^(export\s+)?(default\s+)?(async\s+)?(function\*?|class|interface|type|enum|const|let)\s+([A-Za-z_$][\w$]*)/;
  const inner = /^\s{2,4}(const|function|async function)\s+([A-Za-z_$][\w$]*)\s*(=\s*(async\s*)?(\(|use[A-Z]\w*\(|function)|\()/;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m = line.match(top);
    let depth = 0;
    let name, kind;
    if (m) {
      kind = m[4];
      name = m[5];
      // Plain top-level constants are noise unless exported or a function/component.
      if ((kind === "const" || kind === "let") && !m[1] && !/=\s*(async\s*)?(\(|function|React\.|memo|forwardRef|styled|create)/.test(line))
        continue;
      if (m[1]) kind = "export " + kind;
    } else if (nested && (m = line.match(inner))) {
      depth = 1;
      kind = "fn";
      name = m[2];
    } else continue;
    // JSDoc or line comment directly above.
    let doc = "";
    let k = i - 1;
    if (k >= 0 && /\*\/\s*$/.test(lines[k])) {
      while (k > 0 && !/\/\*\*?/.test(lines[k])) k--;
      const block = lines.slice(k, i).join(" ").replace(/\/\*\*?|\*\/|^\s*\*|\s\*\s/g, " ");
      doc = summary(block.replace(/@\w+.*$/, ""));
    } else if (k >= 0 && /^\s*\/\/\s*\S/.test(lines[k])) {
      doc = summary(lines[k].replace(/^\s*\/\/\s*/, ""));
    }
    out.push({ depth, name, kind, route: "", doc });
  }
  return out;
}

const parts = [
  "# Code map",
  "",
  "<!-- Generated by `node .agents/gen_code_map.mjs` - do not edit by hand; regenerate after changes. -->",
  "",
  "How to use: find the file or function you need here by description and name, then jump directly",
  "into the target symbol without relying on brittle line numbers that shift as code edits occur.",
  "",
];

for (const f of files) {
  let text;
  try {
    text = readFileSync(join(ROOT, f), "utf8");
  } catch {
    continue;
  }
  const lines = text.split(/\r?\n/);
  const isPy = f.endsWith(".py");
  const entries = isPy ? pyEntries(lines) : tsEntries(lines, lines.length > NESTED_THRESHOLD);
  // Large data files (seed data, catalogs) are listed but not expanded.
  if (!entries.length && lines.length < 50) continue;
  const fileDesc = fileSummary(text, f);
  const descSuffix = fileDesc ? ` - ${fileDesc}` : "";
  parts.push(`## ${f} (${lines.length} lines)${descSuffix}`);
  for (const e of entries) {
    const pad = e.depth ? "    " : "";
    const route = e.route ? ` [${e.route}]` : "";
    const doc = e.doc ? ` - ${e.doc}` : "";
    parts.push(`${pad}- \`${e.name}\` (${e.kind})${route}${doc}`);
  }
  parts.push("");
}

writeFileSync(OUT, parts.join("\n"));
console.log(`Wrote ${OUT} (${files.length} files)`);
