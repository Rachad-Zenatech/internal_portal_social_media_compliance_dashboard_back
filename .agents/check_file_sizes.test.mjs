import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const checker = fileURLToPath(new URL("./check_file_sizes.mjs", import.meta.url));
const program = (padding, body = 5, value = 1) => [
  "def legacy():", ...Array.from({ length: body }, (_, n) => `    value_${n} = ${value}`),
  "    return value_0", ...Array(padding).fill("# Existing module documentation"), "",
].join("\n");

function checkChange(before, after, lowered = false) {
  const root = mkdtempSync(join(tmpdir(), "size-gate-test-"));
  const git = (...args) => execFileSync("git", args, { cwd: root, stdio: "pipe" });
  const count = text => text.trimEnd().split("\n").length;
  try {
    mkdirSync(join(root, ".agents"));
    copyFileSync(checker, join(root, ".agents/check_file_sizes.mjs"));
    const baseline = {
      limit: 10, block_limits: { py_function: 3, jsx_return: 3 },
      files: { "sample.py": { max_lines: count(before) } },
      blocks: { "sample.py#legacy": { max_lines: 7, kind: "py_function" } },
    };
    const saveBaseline = () => writeFileSync(join(root, ".agents/file_size_baseline.json"), JSON.stringify(baseline));
    saveBaseline();
    writeFileSync(join(root, "sample.py"), before);
    git("init", "--quiet");
    git("-c", "core.hooksPath=NUL", "add", ".");
    git("-c", "core.hooksPath=NUL", "-c", "user.name=Size Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Base");
    writeFileSync(join(root, "sample.py"), after);
    if (lowered) {
      baseline.files["sample.py"].max_lines = count(after);
      baseline.blocks["sample.py#legacy"].max_lines = after.split("    return")[0].split("\n").length;
      saveBaseline();
    }
    git("add", ".");
    return spawnSync(process.execPath, [join(root, ".agents/check_file_sizes.mjs"), "--staged"], { cwd: root, encoding: "utf8" });
  } finally {
    assert.equal(resolve(dirname(root)), resolve(tmpdir()));
    assert.ok(root.includes("size-gate-test-"));
    rmSync(root, { recursive: true, force: true });
  }
}

test("shrinking files pass after --update lowers their ceilings; untouched long functions need no refactor", () => {
  const result = checkChange(program(20), program(15), true);
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
test("shrinking changed functions pass with a lowered baseline", () => {
  const result = checkChange(program(20), program(15, 4), true);
  assert.equal(result.status, 0, result.stdout + result.stderr);
});
test("rewriting a long function without shrinking still fails", () => {
  const result = checkChange(program(20), program(15, 5, 2));
  assert.notEqual(result.status, 0);
  assert.match(result.stdout + result.stderr, /changed refactoring-debt block/);
});
test("growing legacy files still fails", () => {
  const result = checkChange(program(20), program(21));
  assert.notEqual(result.status, 0);
  assert.match(result.stdout + result.stderr, /grew/);
});
test("changing a legacy file without shrinking still fails", () => {
  const result = checkChange(program(20), program(20).replace("documentation", "description"));
  assert.notEqual(result.status, 0);
  assert.match(result.stdout + result.stderr, /refactoring-debt file must shrink/);
});
