"""Leakage screen, run BEFORE any evaluation is spent.

Borrowed from RRSI rrsi/critic.py: deterministic pre-checks first (generic
denylist + the domain's own patterns), empty diff rejected, a rejection sends
the reasons back and the candidate is recorded without a measurement.

Changed: the LLM-review layer is REMOVED. morde-rsi is local-only and never
calls an external model; the critic is deterministic-only. The layer RRSI's
LLM review provided (intent review, undeclared bundling, degenerate edits) is
replaced by two cheaper controls: a narrow allowlist in domains/morde/SKILL.md
and human review of every adopted branch before merge.

A hit on a protected path (tests, verifier, held-out material, reward files)
is gaming, not just a reject: the controller records GAMING-DETECTED.
"""
from __future__ import annotations

import re

# (pattern, reason). Applied to changed file PATHS extracted from the diff.
PROTECTED_PATH_PATTERNS = [
    (r"(^|/)e2e/", "held-out e2e directory touched"),
    (r"\.e2e\.(ts|js)$", "e2e test file touched"),
    (r"(^|/)test_[^/]*\.py$", "Python test file touched"),
    (r"_test\.py$", "Python test file touched"),
    (r"(^|/)conftest\.py$", "pytest conftest touched"),
    (r"\.test\.(ts|tsx|js|jsx)$", "test file touched"),
    (r"\.spec\.(ts|tsx|js|jsx)$", "spec file touched"),
    (r"heldout", "held-out material touched"),
    (r"reward\.json$", "reward file touched"),
    (r"(^|/)scripts/verify\.py$", "verifier script touched"),
    (r"(^|/)rsi/verifier\.py$", "verifier module touched"),
    (r"(^|/)rsi/critic\.py$", "critic module touched"),
    (r"(^|/)rsi/selection\.py$", "selection module touched"),
    (r"(^|/)rsi/feedback\.py$", "feedback module touched"),
    (r"(^|/)rsi/history\.py$", "ledger module touched"),
    (r"human_feedback\.jsonl$", "human feedback ledger touched"),
    (r"telemetry\.jsonl$", "telemetry ledger touched"),
    (r"(^|/)approval-[^/]*\.json$", "approval card touched"),
    (r"\.env(\.|$)", "environment/secret file touched"),
]

# (pattern, reason). Applied to the whole diff TEXT.
GENERIC_CONTENT_PATTERNS = [
    (r"AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{20,}|api[_-]?key\s*=\s*[\"'][^\"']{8,}",
     "credential in diff"),
]

# Self-domain (meta) protected paths. ONLY enforced when the caller says
# domain == "self" -- never for the morde domain (see review()/precheck()).
# The self loop grades its own repo, so "grading its own exam" has a wider
# surface than the morde loop: the morde Fixer works in the Morde repo and
# can never reach rsi.py / tests / runs / domains/self at all, while the
# self Fixer works in THIS repo and all of those paths are in its worktree.
# A hit here is the same GAMING-DETECTED as a hit on the generic list:
# the candidate was caught rewriting the judge, the threshold, the task,
# or the ledger, and only a human PR may touch these paths.
SELF_PROTECTED_PATH_PATTERNS = [
    (r"(^|/)rsi\.py$", "self-loop CLI entry touched"),
    (r"(^|/)tests/", "test directory touched (self domain)"),
    (r"(^|/)runs/", "runs ledger/evidence directory touched (self domain)"),
    (r"history\.jsonl$", "evidence ledger touched (self domain)"),
    (r"(^|/)domains/self/rrsi\.json$", "self-loop thresholds touched"),
    (r"(^|/)domains/self/SKILL\.md$", "self proposer constitution touched"),
    (r"(^|/)domains/self/adapter\.py$", "self domain adapter touched"),
    (r"(^|/)domains/self/tasks/", "self task material touched"),
]

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.M)
_PLUS_RE = re.compile(r"^\+\+\+ b/(.+?)$", re.M)


def extract_paths(diff: str) -> list[str]:
    """Changed file paths from a unified diff."""
    paths = []
    for m in _DIFF_GIT_RE.finditer(diff or ""):
        paths.append(m.group(2))
    if not paths:
        for m in _PLUS_RE.finditer(diff or ""):
            p = m.group(1).strip()
            if p != "/dev/null":
                paths.append(p)
    return paths


def precheck(diff: str, domain_patterns: list | None = None,
             domain: str | None = None) -> list[dict]:
    """Deterministic checks. Returns a list of {kind, reason, where} hits.

    `domain` selects the domain-scoped blacklist: "self" adds
    SELF_PROTECTED_PATH_PATTERNS (the meta-guardrail: the self loop must
    never rewrite its own judge/threshold/tasks/ledger). Any other value
    (including None, the morde domain) applies only the generic list, so
    morde behaviour is unchanged.
    """
    hits: list[dict] = []
    patterns = list(PROTECTED_PATH_PATTERNS)
    if domain == "self":
        patterns += SELF_PROTECTED_PATH_PATTERNS
    patterns += list(domain_patterns or [])
    for path in extract_paths(diff):
        for pat, why in patterns:
            if re.search(pat, path):
                hits.append({"kind": "protected-path", "reason": why,
                             "where": path})
    for pat, why in GENERIC_CONTENT_PATTERNS:
        if re.search(pat, diff or ""):
            hits.append({"kind": "content", "reason": why, "where": "diff text"})
    return hits


def review(diff: str, domain_patterns: list | None = None,
           task_ids: list[str] | None = None,
           domain: str | None = None) -> dict:
    """Verdict for one candidate diff, before evaluation.

    Returns {"verdict": "accept" | "reject", "gaming": bool,
             "reasons": [...], "risk_notes": [...]}.

    `domain="self"` turns on the self-loop blacklist (see
    SELF_PROTECTED_PATH_PATTERNS); any other domain leaves it off.
    """
    if not (diff or "").strip():
        return {"verdict": "reject", "gaming": False,
                "reasons": ["empty diff"], "risk_notes": []}
    hits = precheck(diff, domain_patterns, domain=domain)
    # Task-id hard-coding: a fix must not branch on the identity of the
    # held-out task it is being measured on.
    for tid in task_ids or []:
        if tid and re.search(rf"(?<![\w-]){re.escape(tid)}(?![\w-])", diff):
            hits.append({"kind": "content",
                         "reason": f"task id '{tid}' hard-coded in diff",
                         "where": "diff text"})
    if hits:
        return {"verdict": "reject", "gaming": True,
                "reasons": [f"{h['kind']}: {h['reason']} ({h['where']})"
                            for h in hits],
                "risk_notes": []}
    return {"verdict": "accept", "gaming": False, "reasons": [],
            "risk_notes": []}


def review_brief(brief: str, heldout_markers: list[str] | None = None) -> dict:
    """Check a Fixer brief for leaked verifier/feedback material.

    Only caller-supplied markers are checked: specific held-out file names
    / test ids (the content that must never reach the Fixer), plus the
    feedback ledger file names. Generic words are NOT flagged on purpose:
    the rules text itself legitimately says "don't touch reward.json"
    (domains/morde/SKILL.md mentions it), so matching the word would
    reject every real brief. Returns the same shape as review().
    A hit here is a wiring bug, not fixer gaming -- but the brief must not
    be sent until it is clean.
    """
    hits: list[str] = []
    for m in heldout_markers or []:
        if m and m in (brief or ""):
            hits.append(f"held-out marker in brief: {m}")
    for pat in (r"human_feedback\.jsonl", r"telemetry\.jsonl"):
        if re.search(pat, brief or ""):
            hits.append(f"feedback ledger in brief: {pat}")
    if hits:
        return {"verdict": "reject", "gaming": False, "reasons": hits,
                "risk_notes": []}
    return {"verdict": "accept", "gaming": False, "reasons": [],
            "risk_notes": []}
