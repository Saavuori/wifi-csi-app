// How a pull request says which part of the version it moves.
//
// Shared by the PR check and the release job so the two can never disagree — a PR that passes
// review as "minor" and then releases as "patch" would be a silent downgrade, and the only way
// to guarantee that cannot happen is one implementation.
//
// A label is explicit and wins. Failing that the title is read as a conventional commit, which
// covers most PRs without anyone touching the label picker.

const LABEL_PREFIX = "release:";
const TYPES = ["major", "minor", "patch", "skip"];

// Conventional-commit type -> release type. Anything not listed needs a label, rather than
// being guessed at: silently defaulting an unrecognised prefix to `patch` is how a breaking
// change ships as a point release.
const CONVENTIONAL = {
  feat: "minor",
  fix: "patch",
  perf: "patch",
  refactor: "patch",
  revert: "patch",
  build: "patch",
  docs: "skip",
  test: "skip",
  tests: "skip",
  ci: "skip",
  chore: "skip",
  style: "skip",
};

/** `feat(scope)!: thing` -> {type: 'feat', breaking: true}; null if it is not conventional. */
function parseTitle(title) {
  const match = /^([a-zA-Z]+)(\([^)]*\))?(!)?:\s*\S/.exec(title || "");
  if (match === null) return null;
  return { type: match[1].toLowerCase(), breaking: match[3] === "!" };
}

/**
 * Decide the release type for a pull request.
 *
 * Returns {type, source} or {error} — never throws, because the caller is a workflow step and a
 * stack trace in the Actions log is a worse message than the sentence explaining what to add.
 */
function resolve({ labels = [], title = "", body = "" }) {
  const release = labels
    .filter((name) => name.startsWith(LABEL_PREFIX))
    .map((name) => name.slice(LABEL_PREFIX.length).toLowerCase());

  const unknown = release.filter((type) => !TYPES.includes(type));
  if (unknown.length > 0) {
    return { error: `Unknown release label(s): ${unknown.map((u) => LABEL_PREFIX + u).join(", ")}. Use one of ${TYPES.map((t) => LABEL_PREFIX + t).join(", ")}.` };
  }
  if (release.length > 1) {
    return { error: `This PR has ${release.length} release labels (${release.join(", ")}). Exactly one decides the version, so remove the others.` };
  }
  if (release.length === 1) {
    return { type: release[0], source: `the ${LABEL_PREFIX}${release[0]} label` };
  }

  // A BREAKING CHANGE footer is major regardless of the title's type, which is what the
  // conventional-commit spec says and what someone writing one expects.
  if (/^BREAKING[ -]CHANGE:/m.test(body)) {
    return { type: "major", source: "a BREAKING CHANGE footer" };
  }

  const parsed = parseTitle(title);
  if (parsed === null) {
    return {
      error:
        "This PR does not say how it changes the version. Add one of " +
        TYPES.map((t) => `\`${LABEL_PREFIX}${t}\``).join(", ") +
        ", or start the title with a conventional-commit type such as `feat:`, `fix:` or `docs:`.",
    };
  }
  if (parsed.breaking) {
    return { type: "major", source: `the \`!\` in the title's \`${parsed.type}!:\`` };
  }
  const mapped = CONVENTIONAL[parsed.type];
  if (mapped === undefined) {
    return {
      error:
        `\`${parsed.type}:\` is not a release type this repo maps. Use one of ` +
        `${Object.keys(CONVENTIONAL).join(", ")}, or add a \`${LABEL_PREFIX}\` label.`,
    };
  }
  return { type: mapped, source: `the \`${parsed.type}:\` prefix in the title` };
}

/** Next version string from the current one and a release type. */
function bump(current, type) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(current || "");
  if (match === null) throw new Error(`not a semantic version: ${current}`);
  let [major, minor, patch] = match.slice(1).map(Number);
  if (type === "major") {
    major += 1;
    minor = 0;
    patch = 0;
  } else if (type === "minor") {
    minor += 1;
    patch = 0;
  } else if (type === "patch") {
    patch += 1;
  } else {
    throw new Error(`no bump for release type: ${type}`);
  }
  return `${major}.${minor}.${patch}`;
}

module.exports = { resolve, bump, parseTitle, TYPES, LABEL_PREFIX, CONVENTIONAL };
