#!/usr/bin/env python3
"""Sanitised job report (report.md) built from untrusted agent files."""
import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory_job as fj  # noqa: E402
import factory_review as fr  # noqa: E402

FINDING_CAP = 600
ZWSP = "​"


def _link(m: "re.Match") -> str:
    target = re.sub(r"[\s\x00-\x1f]+", "", html.unescape(m.group(2)))
    return m.group(1) if re.match(r"(?i)(javascript|vbscript|data):", target) else m.group(0)


def _cut(s: str, limit: int) -> str:
    """Truncate without leaving a partial work-item number or bare mention marker."""
    if len(s) <= limit:
        return s
    return re.sub(r"[#@]" + ZWSP + r"?\w*$", "", s[:limit])


def sanitize(text: str, allowed_items: set[int]) -> str:
    """Strip markup an agent could use to ping people, link work items or inject HTML."""
    text = re.sub(r"@<([^>]*)>", lambda m: "@" + ZWSP + m.group(1), text)
    text = re.sub(r"</?[A-Za-z!][^>]*>", "", text)
    text = text.replace("<", "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\(((?:[^()]|\([^)]*\))*)\)", _link, text)
    text = re.sub(r"\[\[\s*_[A-Za-z]+_\s*\]\]", "", text)
    text = re.sub(r"#(\d+)", lambda m: m.group(0) if int(m.group(1)) in allowed_items else "",
                  text)
    text = re.sub(r"@(?=[\w<])", "@" + ZWSP, text)
    # backstop: no surviving text may contain a usable script/data scheme, including a colon
    # written as an HTML entity (&colon; &#58; &#x3a;)
    return re.sub(r"(?i)\b(javascript|vbscript|data)\s*(:|&colon;|&#0*58;?|&#x0*3a;?)",
                  lambda m: m.group(1) + ZWSP + ":", text)


def _allowed(job: dict) -> set[int]:
    wi = job.get("work_item")
    return {wi} if isinstance(wi, int) and not isinstance(wi, bool) else set()


def _clean(job: dict, s: str, limit: int | None = None) -> str:
    s = sanitize(s, _allowed(job))
    s = re.sub(r"\s+", " ", s).strip()
    return _cut(s, limit) if limit else s


def _cell(job: dict, s: str) -> str:
    return _clean(job, s).replace("|", "\\|")


def _counts(lens: str, rd) -> tuple[int, int]:
    r = rd.reviews.get(lens)
    conf = unv = 0
    for f in (r.findings if r else []):
        v = rd.verifies.get((lens, f.n), "UNVERIFIABLE")
        conf += v == "CONFIRMED"
        unv += v == "UNVERIFIABLE"
    return conf, unv


def _risk_lines(job: dict, rd) -> list[str]:
    """Only a first line of exactly `RISK: NONE` means nothing flagged; anything else (a
    variant such as `**RISK: FLAGGED**`, an unknown word, an empty file) counts as flagged."""
    text = rd.risk.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = text.split("\n") if text else []
    first = re.sub(r"\s+", " ", lines[0]).strip() if lines else ""
    if first == "RISK: NONE":
        return []
    if not lines:
        return ["Risk flagged: no risk report."]
    m = re.fullmatch(r"[*_`]*RISK:\s*FLAGGED(?![A-Za-z0-9])[*_`]*\s*(?:[:—–-]\s*)?(.*)", first)
    rest = ([m.group(1)] if m.group(1) else []) if m else [first]
    rest += lines[1:]
    # Items under `## Not verified` are what the risk check could not look at, not flags.
    out, label = [], "Risk flagged"
    for ln in rest:
        head = re.match(r"\s*#{1,6}(?:\s+(.*))?$", ln)
        if head:
            label = ("Risk check could not verify"
                     if re.fullmatch(r"not verified:?", (head.group(1) or "").strip(), re.I)
                     else "Risk flagged")
            continue
        text = _clean(job, re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln), FINDING_CAP)
        if text:
            out.append(f"{label}: {text}")
    return out or ["Risk flagged."]


def _overlap_lines(job: dict) -> list[str]:
    out = []
    for o in job.get("overlaps") or []:
        paths = o.get("paths") if isinstance(o, dict) else None
        if paths:  # a clean check (no paths) is not worth the reader's attention
            out.append(f"Overlaps with {o.get('with')}: {', '.join(str(p) for p in paths)}")
    return [_clean(job, ln) for ln in out]


def _foreign_lines(job: dict, foreign: list[str]) -> list[str]:
    if not foreign:
        return []
    return [_clean(job, "Remote refs changed by someone else during this job: "
                   + ", ".join(str(r) for r in foreign), FINDING_CAP)]


def _foreign_refs(rdir: Path) -> list[str]:
    """Foreign refs from the backstop's compare output (R/refs-compare.json), if any."""
    try:
        data = json.loads((rdir / "refs-compare.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    refs = data.get("foreign") if isinstance(data, dict) else None
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


def report_body(job: dict, rd, history: list[str], reasons: list[str] | None = None,
                foreign: list[str] | None = None) -> str:
    c = lambda s, n=None: _clean(job, s, n)  # noqa: E731
    items = [f"Draft because: {c(r)}" for r in reasons or []]
    items += _risk_lines(job, rd) + _overlap_lines(job) + _foreign_lines(job, foreign or [])
    out = ["## Needs your attention"]
    out += [f"- {i}" for i in items] or ["Nothing flagged."]
    out += ["", f"# Factory report: {c(str(job.get('feature', '')))}",
            f"Job {c(str(job.get('id', '')))}, round {job.get('round', '?')}."]

    out.append("\n## Criteria")
    if rd.criteria:
        for n, ok, ev in rd.criteria:
            out.append(f"{n}. {'met' if ok else 'NOT MET'}" + (f" - {c(ev)}" if ev else ""))
    else:
        out.append("None recorded.")

    dropped = []
    for lens in fr.LENSES:
        r = rd.reviews.get(lens) or fr.Review("MISSING")
        out.append(f"\n## {lens.capitalize()} review: {r.verdict}"
                   + (f" ({c(r.reason)})" if r.reason else ""))
        listed = False
        for f in r.findings:
            v = rd.verifies.get((lens, f.n), "UNVERIFIABLE")
            line = f"{f.n}. {c(f.location, 200)} - {c(f.text, FINDING_CAP)}"
            if v == "FALSE POSITIVE":
                dropped.append(f"- {lens} {line}")
            else:
                out.append(f"{line} [{v}]")
                listed = True
        if not listed:
            out.append("No confirmed or unverifiable findings.")
        for cmd, state in r.checks.items():
            out.append(f"- check `{c(cmd).replace('`', '')}`: {state}")

    out.append("\n## Dropped as false positives")
    out.extend(dropped or ["None."])

    out.append("\n## How it was built")
    workers = job.get("workers") or []
    for w in workers:
        if isinstance(w, dict):
            out.append("- " + c(", ".join(f"{k}: {v}" for k, v in w.items())))
        else:
            out.append("- " + c(str(w)))
    if not workers:
        out.append("No worker records.")

    out.append("\n## Rework history")
    out.extend((f"{i}. {c(h)}" for i, h in enumerate(history, 1)) if history else ["None."])
    return "\n".join(out) + "\n"


def _rework_history(job_dir: Path) -> list[str]:
    found = []
    for p in job_dir.glob("rework-*.md"):
        m = re.fullmatch(r"rework-(\d+)\.md", p.name)
        if m:
            text = p.read_text(encoding="utf-8", errors="replace")
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not re.match(r"\s*#{1,6}(\s|$)", ln)), "")
            found.append((int(m.group(1)), first))
    return [t for _, t in sorted(found)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="factory_pr_body")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("job")
    b.add_argument("round", type=int)
    a = ap.parse_args(argv)
    job_dir = Path(a.job)
    try:
        job = fj.read_job(job_dir)
        rd = fr.load_round(job_dir, a.round)
        overlaps = [p for o in job.get("overlaps") or []
                    if isinstance(o, dict) and str(o.get("with", "")).startswith(("origin/", "local/"))
                    for p in o.get("paths") or []]
        reasons = fr.draft_reasons(rd, overlaps)
        foreign = _foreign_refs(job_dir / f"round-{a.round}")
        (job_dir / "report.md").write_text(
            report_body(job, rd, _rework_history(job_dir), reasons, foreign), encoding="utf-8")
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"draft": bool(reasons), "reasons": reasons}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
