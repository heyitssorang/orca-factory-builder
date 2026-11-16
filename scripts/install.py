#!/usr/bin/env python3
"""Install or uninstall the orca-factory pack (P7) into a Claude home (default ~/.claude).

Usage:
  install.py install [--claude-home H]
  install.py uninstall [--claude-home H]

Guarantees:
  * Nothing is changed unless lint passes and no destination exists. Bundled dependencies (vendor/)
    are installed only if absent from the claude home; an existing copy is used as is.
  * The manifest (sha256 per file, directories created) is written before copying starts, so an
    interrupted run can still be uninstalled; a failed install rolls everything back.
  * uninstall removes only files that are unchanged since install; edited files are kept and
    reported. Created directories are removed only if empty.
  * Files are copied byte for byte; symlinks and bytecode are never installed.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint_roster  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = ".orca-factory-manifest.json"
# Bundled under vendor/, installed only when the destination is absent (see _vendored).
VENDORED = (
    "agents/review-verifier-p2.md",
    "agents/review-verifier-p2-security.md",
    "skills/differential-review",
)

# Module-level so tests can patch it to simulate a failing copy.
copy_file = shutil.copyfile


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(p: Path) -> bool:
    return "__pycache__" in p.parts or p.suffix == ".pyc"


def _sources(root: Path) -> list[tuple[Path, Path]]:
    """(source, destination relative to the claude home), sorted."""
    out: list[tuple[Path, Path]] = []
    for f in sorted((root / "roster").glob("*.md")):
        out.append((f, Path("agents") / f.name))
    skill = root / "skills" / "factory"
    for f in sorted(skill.rglob("*")):
        if not _skip(f) and (f.is_symlink() or f.is_file()):
            out.append((f, Path("skills/factory") / f.relative_to(skill)))
    for f in sorted((root / "scripts").glob("factory_*.py")):
        out.append((f, Path("skills/factory/scripts") / f.name))
    page = root / "dashboard" / "index.html"
    if page.exists() or page.is_symlink():
        out.append((page, Path("skills/factory/dashboard/index.html")))
    return out


def _vendored(root: Path, claude_home: Path) -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    for dep in VENDORED:
        if (claude_home / dep).exists() or (claude_home / dep).is_symlink():
            continue
        base = root / "vendor" / dep
        files = sorted(f for f in base.rglob("*") if not _skip(f) and (f.is_symlink() or f.is_file())) \
            if base.is_dir() else [base]
        for f in files:
            out.append((f, Path(dep) / f.relative_to(base) if base.is_dir() else Path(dep)))
    return out


def plan(root: Path, claude_home: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for src, rel in _sources(root) + _vendored(root, claude_home):
        if src.is_symlink():
            sys.exit(f"refusing symlink source: {src}")
        pairs.append((src, claude_home / rel))
    return pairs


def _rel(p: Path, home: Path) -> str:
    return p.relative_to(home).as_posix()


def install(root: Path, claude_home: Path) -> None:
    manifest = claude_home / MANIFEST_NAME
    if manifest.exists():
        sys.exit("already installed; uninstall first")
    errors = lint_roster.lint(root)
    if errors:
        sys.exit("lint failed; not installing:\n" + "\n".join(f" - {e}" for e in errors))
    agents = claude_home / "agents"
    stray = sorted(agents.rglob("*-p7.md")) if agents.is_dir() else []
    pairs = plan(root, claude_home)
    clash = stray + [d for _, d in pairs if d.exists() or d.is_symlink()]
    if clash:
        sys.exit("refusing to overwrite; nothing was changed:\n" + "\n".join(f" - {c}" for c in clash))

    if claude_home.exists():
        for _, dst in pairs:
            anchor = dst.parent
            while not anchor.exists() and not anchor.is_symlink():
                anchor = anchor.parent
            if not _inside(anchor, claude_home, strict=False):
                sys.exit(f"refusing: {dst} resolves outside {claude_home}; nothing was changed")

    new_dirs: list[Path] = []
    for _, dst in pairs:
        chain, d = [], dst.parent
        while not d.exists() and d != claude_home:
            chain.append(d)
            d = d.parent
        for c in reversed(chain):
            if c not in new_dirs:
                new_dirs.append(c)
    new_dirs.sort(key=lambda p: len(p.parts))  # deepest last

    data = {"version": 1,
            "files": [{"path": _rel(dst, claude_home), "sha256": sha256(src)} for src, dst in pairs],
            "dirs": [_rel(d, claude_home) for d in new_dirs]}

    def write_manifest() -> None:
        tmp = manifest.with_name(manifest.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, manifest)

    created: list[Path] = []
    try:
        claude_home.mkdir(parents=True, exist_ok=True)
        write_manifest()
        for src, dst in pairs:
            dst.parent.mkdir(parents=True, exist_ok=True)
            created.append(dst)  # before copying: a partial file must be rolled back too
            copy_file(src, dst)
        for (src, dst), row in zip(pairs, data["files"]):
            if sha256(dst) != row["sha256"]:
                raise OSError(f"copy verification failed for {dst}")
    except BaseException:
        for p in created:
            p.unlink(missing_ok=True)
        for d in reversed(new_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        manifest.unlink(missing_ok=True)
        manifest.with_name(manifest.name + ".tmp").unlink(missing_ok=True)
        raise
    print(f"installed {len(created)} files, {len(new_dirs)} new directories; manifest {manifest}")


def _inside(p: Path, home: Path, strict: bool = True) -> bool:
    """True if p resolves inside home (strict: and is not home itself)."""
    try:
        rel = p.resolve().relative_to(home.resolve())
    except ValueError:
        return False
    return not (strict and rel == Path("."))


def _check_entry(rel, home: Path, is_dir: bool) -> Path:
    """Validate one manifest path before anything is deleted; exit on any problem."""
    if not isinstance(rel, str) or not rel:
        sys.exit(f"manifest entry {rel!r} is not a non-empty path; nothing was removed")
    r = Path(rel)
    if r.is_absolute() or ".." in r.parts or r == Path("."):
        sys.exit(f"manifest entry {rel!r} is not a plain relative path; nothing was removed")
    p = home / r
    if not _inside(p if is_dir else p.parent, home):
        sys.exit(f"manifest entry {rel!r} resolves outside {home}; nothing was removed")
    return p


def uninstall(claude_home: Path) -> list[str]:
    """Remove unchanged installed files; return the relative paths kept because they were edited."""
    manifest = claude_home / MANIFEST_NAME
    if not manifest.exists():
        sys.exit("no manifest; nothing to uninstall")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    try:
        files = [(_check_entry(row["path"], claude_home, False), row["path"], row["sha256"])
                 for row in data["files"]]
        dirs = [_check_entry(rel, claude_home, True) for rel in data["dirs"]]
    except (KeyError, TypeError):
        sys.exit("manifest is malformed; nothing was removed")
    kept, removed = [], 0
    for p, rel, digest in files:
        if p.is_symlink() or p.is_dir():
            kept.append(rel)
            continue
        if not p.exists():
            continue
        if sha256(p) != digest:
            kept.append(rel)
            continue
        p.unlink()
        removed += 1
    for d in reversed(dirs):
        try:
            d.rmdir()
        except OSError:
            pass
    if kept:
        print("kept (modified since install):", *kept, sep="\n  ")
        print(f"To finish, delete those files and {manifest}.")
    else:
        manifest.unlink()
    print(f"uninstalled: removed {removed} files" + (f", kept {len(kept)}" if kept else ""))
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install")
    ap.add_argument("cmd", choices=["install", "uninstall"])
    ap.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    a = ap.parse_args(argv)
    if a.cmd == "install":
        install(ROOT, a.claude_home)
    else:
        uninstall(a.claude_home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
