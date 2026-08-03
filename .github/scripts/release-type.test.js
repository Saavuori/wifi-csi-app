// Tests for the release-type classifier. Run with `node .github/scripts/release-type.test.js`.
//
// No framework: this file has to run in a workflow step on a runner that has only checked out
// the repo, and the whole point of it is that it needs nothing.

const assert = require("node:assert/strict");
const { resolve, bump } = require("./release-type.js");

let failures = 0;

function test(name, fn) {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`FAIL  ${name}\n      ${error.message}`);
  }
}

console.log("release type from labels");

test("an explicit label wins", () => {
  const r = resolve({ labels: ["release:minor"], title: "fix: something" });
  assert.equal(r.type, "minor");
});

test("a label beats a conflicting conventional title", () => {
  const r = resolve({ labels: ["release:major"], title: "docs: readme" });
  assert.equal(r.type, "major");
});

test("two release labels are refused rather than guessed", () => {
  const r = resolve({ labels: ["release:minor", "release:patch"], title: "x" });
  assert.match(r.error, /Exactly one/);
});

test("an unknown release label is refused", () => {
  const r = resolve({ labels: ["release:huge"], title: "feat: x" });
  assert.match(r.error, /Unknown release label/);
});

test("non-release labels are ignored", () => {
  const r = resolve({ labels: ["bug", "release:patch", "area:pi"], title: "x" });
  assert.equal(r.type, "patch");
});

console.log("release type from the title");

for (const [title, expected] of [
  ["feat: wifi view", "minor"],
  ["fix: guard bands", "patch"],
  ["perf: fewer copies", "patch"],
  ["refactor(pi): tidy", "patch"],
  ["docs: readme", "skip"],
  ["ci: cache wheels", "skip"],
  ["chore(deps): bump", "skip"],
]) {
  test(`${title} -> ${expected}`, () => {
    assert.equal(resolve({ labels: [], title }).type, expected);
  });
}

test("a bang means major", () => {
  assert.equal(resolve({ labels: [], title: "feat!: new wire format" }).type, "major");
});

test("a bang with a scope means major", () => {
  assert.equal(resolve({ labels: [], title: "refactor(api)!: drop v1" }).type, "major");
});

test("a BREAKING CHANGE footer means major whatever the title says", () => {
  const r = resolve({
    labels: [],
    title: "fix: small thing",
    body: "Some detail.\n\nBREAKING CHANGE: the uplink header grew a field.",
  });
  assert.equal(r.type, "major");
});

test("an unclassifiable title is refused, not defaulted", () => {
  const r = resolve({ labels: [], title: "made some changes" });
  assert.match(r.error, /does not say how it changes the version/);
});

test("an unmapped conventional type is refused", () => {
  const r = resolve({ labels: [], title: "wip: half done" });
  assert.match(r.error, /not a release type/);
});

test("a type-only title with no subject is not conventional", () => {
  const r = resolve({ labels: [], title: "feat:" });
  assert.ok(r.error, "expected an error for an empty subject");
});

console.log("version arithmetic");

test("major zeroes the lower parts", () => assert.equal(bump("v1.4.2", "major"), "2.0.0"));
test("minor zeroes the patch", () => assert.equal(bump("v1.4.2", "minor"), "1.5.0"));
test("patch increments", () => assert.equal(bump("v1.4.2", "patch"), "1.4.3"));
test("a bare version works too", () => assert.equal(bump("0.9.9", "minor"), "0.10.0"));
test("the first release", () => assert.equal(bump("v0.0.0", "minor"), "0.1.0"));
test("double digits do not sort as strings", () =>
  assert.equal(bump("v1.9.0", "minor"), "1.10.0"));

test("a non-version is rejected", () => {
  assert.throws(() => bump("latest", "patch"), /not a semantic version/);
});

test("skip has no bump", () => {
  assert.throws(() => bump("v1.0.0", "skip"), /no bump/);
});

console.log(failures === 0 ? "\nall passed" : `\n${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
