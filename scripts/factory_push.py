#!/usr/bin/env python3
"""Push guard for the factory skill: the only path that pushes to origin (ship: the default branch).
A repo whose origin is missing or not an allowed Azure DevOps URL runs in local mode: nothing
fetches from or pushes to it, and the guards check local refs instead.

Refusals print "rule <n>: <detail>" (a path or short reason), never matched text.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_job as fj  # noqa: E402  (stdlib only; no cycle)

BRANCH_RE = re.compile(r"^factory/[0-9]{3,}-[a-z0-9-]+$")
MAX_LINES = 5000
MAX_FILES = 200
DEFAULT_ORGS = "example-org"
PUSHURL_DISABLED = "no-push://factory-disabled"

# Worker-writable repo/worktree config keys that must not appear (case-insensitive).
BAD_CONFIG_PREFIXES = ("url.", "credential.", "http.", "push.", "include.", "includeif.",
                       "filter.", "diff.", "remote.origin.")
# Harmless keys under a refused prefix: Orca's `worktree create` sets push.autoSetupRemote, which
# only affects a bare `git push`; the factory pushes an explicit URL and refspec.
ALLOWED_CONFIG_KEYS = {"push.autosetupremote"}
BAD_CONFIG_KEYS = {"core.sshcommand", "core.fsmonitor", "core.askpass",
                   "core.bigfilethreshold", "core.hookspath", "core.gitproxy"}

CONTENT_RULES = [
    ("C1", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("C2", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("C3", re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("C4", re.compile(r"""(?i)(password|secret|token|api_key)\s*[:=]\s*["'][^"']{8,}["']""")),
]
# C5: a 52+ char alphanumeric run on a line that also mentions pat/token/azure
# Anchored at ^ so the scan stays linear in the line length (unanchored it is quadratic).
CONTENT_RULES.append(("C5", re.compile(r"^(?=.*[A-Za-z0-9]{52})(?=.*(?i:pat|token|azure))")))

# Matched with fullmatch against the basename; case-insensitive (.ENV, KEY.PEM, ID_RSA) and
# DOTALL so a control character in a file name cannot end the match early.
_PF = re.IGNORECASE | re.DOTALL
PATH_RULES = [
    ("P1", re.compile(r"\.env(?!\.example\Z).*", _PF)),
    ("P2", re.compile(r".*\.pem", _PF)),
    ("P3", re.compile(r".*\.pfx", _PF)),
    ("P4", re.compile(r".*\.p12", _PF)),
    ("P5", re.compile(r"id_rsa.*", _PF)),
]


@dataclass
class Violation:
    rule: str
    commit: str
    path: str


class GitError(Exception):
    pass


_GIT_PREFIX = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false",
               "-c", "core.fsmonitor=false"]
REMOTE_TIMEOUT, PUSH_TIMEOUT = 120, 600
_REMOTE_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def remote_env() -> dict:
    """os.environ without the variables that point git at another repo or inject config
    (url.*.insteadOf and the like) into calls that talk to origin."""
    return {k: v for k, v in os.environ.items() if k not in _REMOTE_ENV_DROP
            and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}


def url_rewrites(repo: Path, origin_url: str) -> list[str]:
    """Which ways some config scope rewrites origin_url: "fetch"/"push" (url.<x>.insteadOf /
    pushInsteadOf), "remote" (a remote named by the URL itself, remote."<url>".*, which git
    uses when the URL is given where a remote name may stand). Remote calls keep the user's global config for
    credentials, and a worker (same OS user) can write ~/.gitconfig; this pins the one URL the
    factory talks to. Raises GitError when git cannot answer."""
    if not isinstance(origin_url, str) or not origin_url or origin_url.startswith("-"):
        return ["fetch", "push"]
    out = []
    f = _git("/", "ls-remote", "--get-url", origin_url, env=remote_env(), timeout=REMOTE_TIMEOUT)
    if f.stdout.strip() != origin_url:
        out.append("fetch")
    # `remote get-url` ignores a remote defined only by -c; `remote -v` lists it, rewritten.
    p = _git(repo, "-c", f"remote.factoryprobe.url={origin_url}", "remote", "-v",
             env=remote_env(), timeout=REMOTE_TIMEOUT)
    push = [line[len("factoryprobe\t"):-len(" (push)")] for line in p.stdout.splitlines()
            if line.startswith("factoryprobe\t") and line.endswith(" (push)")]
    if push != [origin_url]:
        out.append("push")
    r = _git(repo, "config", "--null", "--name-only", "--get-regexp", r"^remote\.",
             check=False, env=remote_env(), timeout=REMOTE_TIMEOUT)
    if r.returncode not in (0, 1):
        raise GitError("git config failed")
    for name in r.stdout.split("\0"):
        section, _, rest = name.strip("\n").partition(".")
        if section.lower() == "remote" and rest.rpartition(".")[0] == origin_url:
            out.append("remote")
            break
    return out


def rewrite_reasons(repo: Path, origin_url: str) -> list[str]:
    return [f"rule 6: origin url is rewritten by config ({k})"
            for k in url_rewrites(repo, origin_url)]


def _git(directory, *args, check=True, raw=False, env=None, timeout=None):
    """Run git. raw=True: stdout is decoded from bytes without newline translation (text mode
    would turn a bare CR into a line break). env: base environment (default os.environ).
    A timeout raises GitError whatever check says."""
    cmd = [*_GIT_PREFIX, "-C", str(directory), *args]
    env = dict(os.environ if env is None else env, GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    try:
        if raw:
            p = subprocess.run(cmd, capture_output=True, env=env, timeout=timeout)
            p.stdout = p.stdout.decode("utf-8", errors="replace")
            p.stderr = p.stderr.decode("utf-8", errors="replace")
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out") from None
    if check and p.returncode != 0:
        raise GitError(f"git {args[0]} failed")
    return p


_DIFF_FLAGS = ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]


def disable_push(repo: Path, worktree: Path) -> None:
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(worktree, "config", "--worktree", "remote.origin.pushurl",
         "no-push://factory-disabled")


def _content_hit(line: str):
    for rid, rx in CONTENT_RULES:
        if rx.search(line):
            return rid
    return None


def _path_hit(path: str):
    base = path.rsplit("/", 1)[-1]
    for rid, rx in PATH_RULES:
        if rx.fullmatch(base):
            return rid
    return None


def scan_commits(worktree: Path, base: str, head: str = "HEAD") -> list[Violation]:
    out: list[Violation] = []
    seen: set[tuple[str, str]] = set()

    def add(rule, commit, path):
        if (rule, path) not in seen:
            seen.add((rule, path))
            out.append(Violation(rule, commit, path))

    # --text: scan text even when attributes/size mark the file binary. raw: git separates
    # lines with LF only; a CR stays inside its + line and is scanned with it.
    log = _git(worktree, "log", "-p", "--text", "--diff-merges=first-parent",
               "--format=commit:%H", *_DIFF_FLAGS,
               f"{base}..{head}", raw=True).stdout
    commit = path = ""
    in_header = False
    for line in log.split("\n"):
        if line.startswith("commit:") and len(line) == 47:
            commit, in_header = line[7:], False
        elif line.startswith("diff --git "):
            in_header = True
            path = line.split(" b/", 1)[-1]
        elif in_header:
            if line.startswith("@@"):
                in_header = False
            elif line.startswith("+++ "):
                p = line[4:]
                path = p[2:] if p.startswith("b/") else path
            elif line.startswith("--- "):
                p = line[4:]
                if p.startswith("a/"):
                    path = p[2:]
        elif line.startswith("+"):
            hit = _content_hit(line[1:])
            if hit:
                add(hit, commit, path)

    # Path rules: raw NUL-separated names per commit (no C-quoting).
    commits = _git(worktree, "rev-list", f"{base}..{head}").stdout.split()
    for c in commits:
        names = _git(worktree, "diff-tree", "-m", "-r", "--root", "--no-commit-id",
                     "--name-only", "-z", "--no-renames", c, raw=True).stdout
        for name in names.split("\0"):
            hit = _path_hit(name) if name else None
            if hit:
                add(hit, c, name)
    return out


def allowed_orgs() -> set[str]:
    raw = os.environ.get("FACTORY_ADO_ORGS", DEFAULT_ORGS)
    return {o.strip().lower() for o in raw.split(",") if o.strip()}


def validate_origin(url: str, orgs: set[str]) -> bool:
    if not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return False
    try:
        u = urlsplit(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return False
    if u.scheme != "https" or u.password is not None or port not in (None, 443):
        return False
    orgs = {o.lower() for o in orgs}
    if host == "dev.azure.com":
        segs = [x for x in u.path.split("/") if x]
        if any(unquote(x) in (".", "..") or "/" in unquote(x) or "\\" in unquote(x) for x in segs):
            return False
        return bool(segs) and segs[0].lower() in orgs
    if host.endswith(".visualstudio.com"):
        org = host[: -len(".visualstudio.com")]
        return "." not in org and org in orgs
    return False


def repo_mode(origin_url) -> str:
    """"ado" when origin_url is an allowed Azure DevOps https URL, else "local"."""
    return "ado" if isinstance(origin_url, str) and validate_origin(origin_url,
                                                                   allowed_orgs()) else "local"


def repo_info(repo: Path) -> dict:
    """{mode, origin_url, default} for preflight. origin_url is the raw configured value (never
    rewritten by url.*.insteadOf), or None. default: ado -> origin/HEAD's branch; local -> main,
    else master, else the checked-out branch; None when that branch has no commit."""
    p = _git(repo, "config", "--get", "remote.origin.url", check=False)
    url = p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None
    mode = repo_mode(url)
    default = None
    if mode == "ado":
        h = _git(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", check=False)
        name = h.stdout.strip()
        if h.returncode == 0 and name.startswith("origin/"):
            default = name[len("origin/"):]
    else:
        cur = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        for name in ("main", "master", cur):
            if name and _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}^{{commit}}",
                             check=False).returncode == 0:
                default = name
                break
    return {"mode": mode, "origin_url": url, "default": default}


def _config_entries(worktree, *scope):
    p = _git(worktree, "config", *scope, "--list", "--null", check=False)
    if p.returncode != 0:
        return None
    return [e.partition("\n")[::2] for e in p.stdout.split("\0") if e]


def config_violations(worktree: Path, origin_url: str) -> list[str]:
    reasons = []
    entries = _config_entries(worktree, "--local")
    if entries is None:
        return ["rule 6: config unreadable"]
    wt = _config_entries(worktree, "--worktree")
    if wt is None:
        enabled = _git(worktree, "config", "--local", "--get", "extensions.worktreeconfig",
                       check=False).stdout.strip()
        if enabled:
            # A checkout whose config.worktree was never written (the main checkout, after
            # disable on a linked worktree) has an empty worktree scope; anything else at that
            # path is unreadable.
            path = _git(worktree, "rev-parse", "--path-format=absolute", "--git-path",
                        "config.worktree", check=False).stdout.strip()
            if not path or os.path.lexists(path):
                return ["rule 6: config unreadable"]
        wt = []
    for key, val in entries + wt:
        k = key.lower()
        if k == "remote.origin.fetch":
            continue
        if k == "remote.origin.pushurl" and val == PUSHURL_DISABLED:
            continue
        if k == "remote.origin.url" and val == origin_url:
            continue
        if k in ALLOWED_CONFIG_KEYS:
            continue
        # git takes the origin URL argument as a remote name too: remote."<url>".* (exact
        # subsection) would redirect it. Other remotes (a fork's upstream) are harmless.
        sec, _, rest = key.partition(".")
        url_named = sec.lower() == "remote" and rest.rpartition(".")[0] == origin_url
        if k in BAD_CONFIG_KEYS or k.startswith(BAD_CONFIG_PREFIXES) or url_named:
            reasons.append(f"rule 6: config {k}")
    return sorted(set(reasons))


def config_check(worktree: Path, origin_url: str | None, *, local: bool = False) -> list[str]:
    """Rule 6 alone: trusted origin and no worker-set repo/worktree config. Runs no
    command that could execute configured filters, fsmonitor or hooks. local: the origin need
    not be trusted (nothing talks to it) but must still be the recorded one, or absent when
    origin_url is None, so a worker cannot point the user's own pushes elsewhere."""
    if not local and not validate_origin(origin_url, allowed_orgs()):
        return ["rule 6: origin url not allowed"]
    reasons: list[str] = []
    cfg = _git(worktree, "config", "--get", "remote.origin.url", check=False)
    if origin_url is None:
        if cfg.returncode == 0:
            reasons.append("rule 6: remote.origin.url set but the run recorded no origin")
    elif cfg.returncode != 0 or cfg.stdout.strip() != origin_url:
        reasons.append("rule 6: remote.origin.url differs from recorded origin url")
    reasons += config_violations(worktree, origin_url)
    gd = _git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if gd.returncode != 0 or not gd.stdout.strip():
        reasons.append("rule 6: cannot locate git dir")
    elif (Path(gd.stdout.strip()) / "info" / "grafts").exists():
        reasons.append("rule 6: grafts file present")
    return reasons


def _check(worktree: Path, *, branch: str, base: str, default_branch: str,
           origin_url: str | None, local: bool = False) -> tuple[list[str], str | None]:
    # Rule 6 first: nothing that could run worker-configured code happens before it.
    reasons = config_check(worktree, origin_url, local=local)
    if reasons:
        return reasons, None

    if base.startswith("-"):
        return ["rule 3: invalid base"], None
    h = _git(worktree, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    b = _git(worktree, "rev-parse", "--verify", "-q", f"{base}^{{commit}}", check=False)
    if h.returncode != 0 or b.returncode != 0:
        return ["rule 3: cannot resolve HEAD or base"], None
    head, base = h.stdout.strip(), b.stdout.strip()

    cur = _git(worktree, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if not BRANCH_RE.fullmatch(branch):
        reasons.append("rule 1: branch name does not match factory pattern")
    elif cur != branch:
        reasons.append("rule 1: branch is not the worktree's current branch")

    st = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if st.returncode != 0 or st.stdout.strip():
        reasons.append("rule 2: worktree not clean")

    def is_anc(a, c):
        return _git(worktree, "merge-base", "--is-ancestor", a, c, check=False).returncode
    if is_anc(base, head) != 0:
        reasons.append("rule 3: base is not an ancestor of HEAD")
    if local:
        if is_anc(base, f"refs/heads/{default_branch}") != 0:
            reasons.append("rule 3: base is not an ancestor of the local default branch")
    elif is_anc(base, f"refs/remotes/origin/{default_branch}") != 0:
        reasons.append("rule 3: base is not an ancestor of origin default branch")
    merges = _git(worktree, "rev-list", "--merges", f"{base}..{head}", check=False)
    if merges.returncode != 0 or merges.stdout.strip():
        reasons.append("rule 3: merge commit in range")
    if BRANCH_RE.fullmatch(branch) and not local:
        try:
            ls = _git(worktree, "ls-remote", origin_url, f"refs/heads/{branch}", check=False,
                      env=remote_env(), timeout=REMOTE_TIMEOUT)
        except GitError:
            ls = None
        if ls is None or ls.returncode != 0:
            reasons.append("rule 3: cannot read remote branch")
        elif ls.stdout.strip():
            rsha = ls.stdout.split()[0]
            if is_anc(rsha, head) != 0:
                reasons.append("rule 3: remote branch is not an ancestor of HEAD (not fast-forward)")

    try:
        for v in scan_commits(worktree, base, head):
            reasons.append(f"rule 4: {v.rule} {v.path} (commit {v.commit[:12]})")
    except GitError:
        reasons.append("rule 4: secret scan failed")

    names = _git(worktree, "diff", "--name-only", "-z", *_DIFF_FLAGS, f"{base}..{head}",
                 check=False, raw=True)
    if names.returncode != 0:
        reasons.append("rule 7: cannot list changed paths")
    else:
        # The factory's own files (the committed backlog, its .gitignore) change only through
        # the coordinator's backlog commands, never through a job.
        for name in sorted({n for n in names.stdout.split("\0") if n}):
            if name == fj.HOME_DIR or name.startswith(fj.HOME_DIR + "/"):
                reasons.append(f"rule 7: job changes {name}")

    ns = _git(worktree, "diff", "--numstat", *_DIFF_FLAGS, f"{base}..{head}", check=False)
    if ns.returncode != 0:
        reasons.append("rule 5: cannot compute diff size")
    else:
        files = lines = 0
        for row in ns.stdout.splitlines():
            parts = row.split("\t", 2)
            if len(parts) < 3:
                continue
            files += 1
            for n in parts[:2]:
                lines += int(n) if n.isdigit() else 0
        if lines >= MAX_LINES:
            reasons.append(f"rule 5: {lines} changed lines, limit is under {MAX_LINES}")
        if files >= MAX_FILES:
            reasons.append(f"rule 5: {files} files, limit is under {MAX_FILES}")
    return reasons, head


def check(worktree: Path, *, branch: str, base: str, default_branch: str,
          origin_url: str | None, local: bool = False) -> list[str]:
    return _check(worktree, branch=branch, base=base, default_branch=default_branch,
                  origin_url=origin_url, local=local)[0]


def ship_argv(origin_url: str, default_branch: str, sha: str) -> list[str]:
    return ["git", "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
            origin_url, f"{sha}:refs/heads/{default_branch}"]


def remote_refs(repo: Path, origin_url: str) -> dict[str, str]:
    # Run outside any repo so worker-writable repo config (url.*.insteadOf etc.) is not read.
    p = _git("/", "ls-remote", origin_url, env=remote_env(), timeout=REMOTE_TIMEOUT)
    refs = {}
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        # Skip peeled tag entries (refs/tags/x^{}): the tag itself is already listed.
        if ref and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


def unexpected_changes(before: dict, after: dict, branch) -> list[str]:
    """Refs that changed, other than refs/heads/<b> for each expected branch b and the
    server-managed pull-request refs (refs/pull/*, which ADO moves on every PR update)."""
    branches = [branch] if isinstance(branch, str) else list(branch)
    own = {f"refs/heads/{b}" for b in branches}
    return sorted(r for r in set(before) | set(after)
                  if r not in own and not r.startswith("refs/pull/")
                  and before.get(r) != after.get(r))


SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def has_local_commit(repo: Path, sha) -> bool:
    """True when sha names a commit (or a tag of one) present in repo's object store."""
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return False
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def reachable_from(repo: Path, sha, ref: str) -> bool:
    """True when commit sha is ref itself or an ancestor of it (false if either is unknown)."""
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or ref.startswith("-"):
        return False
    return _git(repo, "merge-base", "--is-ancestor", sha, ref, check=False).returncode == 0


def classify_changes(before: dict, after: dict, branch, *, has_commit,
                     is_fetched=None) -> dict[str, list[str]]:
    """Split unexpected ref changes into suspicious and foreign.

    suspicious: a new or moved ref whose new tip is a commit in the local object store and not
    on the default branch as the server reported it (is_fetched false), i.e. work done on this
    machine reached the remote other than through the push guard.
    foreign: every other change: teammates' commits, deletions, and refs pointing at commits
    reachable from the fetched default branch (a branch or tag made from main).
    """
    out: dict[str, list[str]] = {"suspicious": [], "foreign": []}
    for ref in unexpected_changes(before, after, branch):
        new = after.get(ref)
        local_work = bool(new) and has_commit(new) and not (is_fetched and is_fetched(new))
        out["suspicious" if local_work else "foreign"].append(ref)
    return out


def classify_snapshots(repo: Path, before: dict, after: dict, branch,
                       default_branch: str | None) -> dict[str, list[str]]:
    """classify_changes for two ls-remote snapshots of repo's origin. "On the default branch"
    is anchored on the default branch tips the server reported in the snapshots, never on
    refs/remotes/*, which any worker process can rewrite locally."""
    is_fetched = None
    if default_branch and not default_branch.startswith("-"):
        ref = f"refs/heads/{default_branch}"
        anchors = [t for t in (before.get(ref), after.get(ref))
                   if isinstance(t, str) and SHA_RE.fullmatch(t)]
        if anchors:
            is_fetched = lambda sha: any(  # noqa: E731
                reachable_from(repo, sha, t) for t in anchors)
    return classify_changes(before, after, branch,
                            has_commit=lambda sha: has_local_commit(repo, sha),
                            is_fetched=is_fetched)


def server_head(origin_url: str, default_branch: str) -> tuple[str | None, str | None]:
    """(ref origin's HEAD points at, tip of refs/heads/<default_branch>) as the server reports
    them. Run outside any repo; never refs/remotes/*, which any worker process can rewrite.
    Raises GitError when origin cannot be read."""
    ls = _git("/", "ls-remote", "--symref", origin_url, "HEAD", f"refs/heads/{default_branch}",
              env=remote_env(), timeout=REMOTE_TIMEOUT)
    tip = head_ref = None
    for line in ls.stdout.splitlines():
        if line.startswith("ref: "):
            head_ref = line[5:].partition("\t")[0]
        else:
            t, _, ref = line.partition("\t")
            if ref == f"refs/heads/{default_branch}" and SHA_RE.fullmatch(t):
                tip = t
    return head_ref, tip


def ship_check(repo: Path, *, default_branch: str, sha: str, base: str,
               origin_url: str) -> list[str]:
    # Rule 6 first: nothing that could run worker-configured code happens before it.
    reasons = config_check(repo, origin_url)
    if not reasons:
        try:
            reasons = rewrite_reasons(repo, origin_url)
        except GitError:
            reasons = ["rule 6: cannot check origin url rewrites"]
    if reasons:
        return reasons
    d = default_branch
    if (not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or base.startswith("-")
            or d.startswith("-")
            or _git(repo, "check-ref-format", f"refs/heads/{d}", check=False).returncode != 0):
        return ["ship: invalid sha, base or default branch"]
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        return ["ship: sha is not a commit"]

    def resolve(ref):
        p = _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        return p.stdout.strip() if p.returncode == 0 else None
    try:
        head_ref, tip = server_head(origin_url, d)
    except GitError:
        return ["ship: cannot read origin"]
    if head_ref != f"refs/heads/{d}":
        return [f"ship: {d} is not origin's default branch"]
    if tip is None:
        return [f"ship: origin/{d} not found"]
    if resolve(f"refs/heads/{d}") != sha:
        reasons.append(f"ship: local {d} is not {sha}")
    if resolve(tip) != tip or _git(repo, "merge-base", "--is-ancestor", tip, sha,
                                   check=False).returncode != 0:
        reasons.append(f"ship: origin/{d} is not an ancestor of {sha}")
    if resolve(base) != tip:
        reasons.append(f"ship: base is not origin/{d}")
    if not reasons:
        try:
            for v in scan_commits(repo, tip, sha):
                reasons.append(f"rule 4: {v.rule} {v.path} (commit {v.commit[:12]})")
        except GitError:
            reasons.append("rule 4: secret scan failed")
    return reasons


RUN_ID_RE = re.compile(r"\d{8}-\d{4}-[0-9a-f]{4}")


def run_gate_reasons(run_id: str, *, repo: Path, default_branch: str, sha: str,
                     base: str, origin_url: str) -> list[str]:
    """Bind a push to the run's gate record: FACTORY_HOME/runs/<run-id>.json (the file is opened
    without following a symlink; FACTORY_HOME and runs/ themselves are trusted) must name this repo and default branch, a ship of exactly sha on base
    whose gate passed for sha, and no finished ship."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        return ["ship: invalid run id"]
    try:
        fd = os.open(fj.home(repo) / "runs" / f"{run_id}.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as f:
            run = json.load(f)
    except (OSError, ValueError, RecursionError):  # fj.BadRequest is a ValueError
        return ["ship: cannot read run file"]
    if not isinstance(run, dict):
        return ["ship: cannot read run file"]
    ship = run.get("ship") if isinstance(run.get("ship"), dict) else {}
    gate = ship.get("gate") if isinstance(ship.get("gate"), dict) else {}
    reasons = []
    try:
        same = (isinstance(run.get("repo"), str) and bool(run["repo"])
                and Path(run["repo"]).resolve() == Path(repo).resolve())
    except (OSError, ValueError, RuntimeError):
        same = False
    if not same:
        reasons.append("ship: run's repo is not --repo")
    if run.get("mode") == "local":
        reasons.append("ship: run is local; nothing is pushed")
    if run.get("origin_url") != origin_url:
        reasons.append("ship: run's origin url is not --origin-url")
    if run.get("default_branch") != default_branch:
        reasons.append("ship: run's default branch is not --default")
    if ship.get("merge_sha") != sha:
        reasons.append("ship: run's ship merge is not --sha")
    if ship.get("base") != base:
        reasons.append("ship: run's ship base is not --base")
    if gate.get("status") != "pass" or gate.get("merge_sha") != sha:
        reasons.append("ship: gate has not passed for --sha")
    if run.get("shipped") is not None:
        reasons.append("ship: run already shipped")
    return reasons


def _cmd_ship(a) -> int:
    if not validate_origin(a.origin_url, allowed_orgs()):
        print("rule 6: origin url not allowed", file=sys.stderr)
        return 2
    repo = Path(a.repo)
    reasons = run_gate_reasons(a.run, repo=repo, default_branch=a.default, sha=a.sha,
                               base=a.base, origin_url=a.origin_url)
    if reasons:
        for r in reasons:
            print(r, file=sys.stderr)
        return 2
    reasons = ship_check(repo, default_branch=a.default, sha=a.sha, base=a.base,
                         origin_url=a.origin_url)
    if reasons:
        for r in reasons:
            print(r, file=sys.stderr)
        return 2
    argv = ship_argv(a.origin_url, a.default, a.sha)
    cmd = [*_GIT_PREFIX, "-C", str(repo), *argv[1:]]
    env = dict(remote_env(), GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env,
                           timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("push timed out", file=sys.stderr)
        return 1
    if p.returncode != 0:
        print("push rejected", file=sys.stderr)
        return 1
    for line in p.stdout.splitlines():
        if f":refs/heads/{a.default}" in line:
            print(line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_push")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("disable")
    d.add_argument("--repo", required=True)
    d.add_argument("--worktree", required=True)
    s = sub.add_parser("check")
    s.add_argument("--worktree", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--base", required=True)
    s.add_argument("--default", required=True)
    s.add_argument("--origin-url")
    s.add_argument("--local", action="store_true",
                   help="local-mode run: --origin-url is the recorded origin, if any")
    sh = sub.add_parser("ship")
    sh.add_argument("--repo", required=True)
    sh.add_argument("--default", required=True)
    sh.add_argument("--sha", required=True)
    sh.add_argument("--base", required=True)
    sh.add_argument("--origin-url", required=True)
    sh.add_argument("--run", required=True,
                    help="the run whose recorded, gated ship this push must be")
    r = sub.add_parser("refs")
    r.add_argument("--repo", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--origin-url", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)
    c.add_argument("--branch", action="append", default=[],
                   help="expected to change; repeat per branch; none means no branch may change")
    c.add_argument("--repo", required=True,
                   help="the job's repo; its object store tells local work from foreign")
    c.add_argument("--default", help="default branch the coordinator fetched")
    cc = sub.add_parser("config-check")
    cc.add_argument("--worktree", required=True)
    cc.add_argument("--origin-url")
    cc.add_argument("--local", action="store_true",
                    help="local-mode run: --origin-url is the recorded origin, if any")
    m = sub.add_parser("mode")
    m.add_argument("--repo", required=True)
    a = ap.parse_args(argv)
    if a.cmd in ("check", "config-check") and not a.local and not a.origin_url:
        print("rule 6: origin url not allowed", file=sys.stderr)
        return 2
    try:
        if a.cmd == "disable":
            disable_push(Path(a.repo), Path(a.worktree))
        elif a.cmd == "check":
            reasons = check(Path(a.worktree), branch=a.branch, base=a.base,
                            default_branch=a.default, origin_url=a.origin_url, local=a.local)
            for x in reasons:
                print(x, file=sys.stderr)
            return 2 if reasons else 0
        elif a.cmd == "config-check":
            reasons = config_check(Path(a.worktree), a.origin_url, local=a.local)
            for x in reasons:
                print(x, file=sys.stderr)
            return 2 if reasons else 0
        elif a.cmd == "ship":
            return _cmd_ship(a)
        elif a.cmd == "mode":
            print(json.dumps(repo_info(Path(a.repo)), indent=2))
        elif a.cmd == "refs":
            if not validate_origin(a.origin_url, allowed_orgs()):
                print("rule 6: origin url not allowed", file=sys.stderr)
                return 2
            Path(a.out).write_text(json.dumps(remote_refs(Path(a.repo), a.origin_url),
                                              sort_keys=True))
        elif a.cmd == "compare":
            repo = Path(a.repo)
            before = json.loads(Path(a.before).read_text())
            after = json.loads(Path(a.after).read_text())
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise ValueError("refs snapshot is not an object")
            res = classify_snapshots(repo, before, after, a.branch, a.default)
            print(json.dumps(res, indent=2, sort_keys=True))
            for x in res["suspicious"]:
                print(f"suspicious change: {x}", file=sys.stderr)
            return 2 if res["suspicious"] else 0
    except (GitError, OSError, ValueError) as e:
        print(f"error: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
