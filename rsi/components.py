"""Component labels for Morde candidates.

Ported idea from RRSI ``refs/rrsi/rrsi/components.py`` (fixed vocabulary K
+ structural subset K_STR + classify_diff from file-name signals +
has_evidence / normalize + text_only). NOT a copy: the vocabulary, the
signals, the default label and the text-change rule are rewritten for
Morde's repo layout (``~/workspace/morde-check`` top level: ``server/``,
``web/portal/``, ``runtime/``, ``clients/``, ``deploy/``, ``scripts/``),
and every function below is written fresh for this file.

Morde K (14 labels). The first six are the proposer vocabulary already in
``domains/morde/SKILL.md`` (kept verbatim so existing task JSONs still
validate); the rest are added from Morde's real directories:

* ``ui-text``      文案 / 提示语 / 状态文案（只改字符串）
* ``ui-style``     样式 / 布局（CSS / className，不改结构）
* ``null-check``   空值防御（nil / null / 越界保护）
* ``error-message`` 报错信息
* ``config-default`` 配置默认值 / 超时 / 重试次数
* ``logic-small``  小逻辑修复（一个函数内；也是未知路径的默认标签）
* ``portal-ui``    Portal 界面结构（``web/portal/src/routes|components|app``）
* ``api-contract`` API 契约（``server/openapi``、``web/portal/src/api``、schema）
* ``mock-fixture`` mock / fixture / 测试替身（``src/mocks``、``fixtures``）
* ``docs``         文档（``*.md``）
* ``agent-tool``   新 Agent 工具 / MCP 工具 —— 结构组件（K_STR）
* ``skill``        新 Skill 文件 —— 结构组件（K_STR）
* ``workflow``     新工作流 / 部署 / 自动化脚本 —— 结构组件（K_STR）
* ``test-harness`` 新测试 harness（e2e、playwright/vitest 配置）—— 结构组件

K_STR is "加机器 / 加机制" (adds machinery: a new tool, a new Skill file,
a new workflow, a new test harness), not "只改文字 / 常量". A fix that
only edits text or constants can never land in K_STR, however it is
declared -- ``normalize()`` sends it back to the label its diff evidences.

Precedence in ``classify_diff`` (first match wins, same shape as RRSI):
text change -> structural path signals -> other path signals -> content
signals (null-check / error-message) -> code file -> default
``logic-small``. Path signals beat content signals: a nil guard added
inside a Portal route file is ``portal-ui`` first, because the file is
what the Fixer actually touched.
"""

from __future__ import annotations

import re

K = ["ui-text", "ui-style", "null-check", "error-message", "config-default",
     "logic-small", "portal-ui", "api-contract", "mock-fixture", "docs",
     "agent-tool", "skill", "workflow", "test-harness"]

# K_STR: structural components -- "加机器 / 加机制". Only these count for
# novelty (see novelty()): a text/config tweak must not earn novelty credit.
K_STR = ["agent-tool", "skill", "workflow", "test-harness"]

# Path signals, in classify order. Patterns match against the changed file
# path as it appears in the diff (``b/<path>``). Grounded in the Morde repo:
# server/internal/app/agent_tools.go + cloud_dsh_skills.go, server/internal/
# mcp/ + mcpconnector/, web/portal/src/{routes,components,app,api,mocks},
# server/openapi/, deploy/, scripts/.
_PATH_SIGNALS: list[tuple[str, list[str]]] = [
    ("agent-tool", [r"server/internal/app/agent_tools\.go",
                    r"server/internal/app/cloud_dsh_skills\.go",
                    r"server/internal/mcp/",
                    r"server/internal/mcpconnector/",
                    r"tool_broker"]),
    ("skill", [r"SKILL\.md$", r"(^|/)skills/"]),
    ("workflow", [r"^\.github/workflows/",
                  r"^deploy/",
                  r"^scripts/.*\.sh$"]),
    ("test-harness", [r"web/portal/e2e/",
                      r"playwright\.config\.",
                      r"vitest\.config\.",
                      r"testutil",
                      r"scripts/verify\.py$"]),
    ("api-contract", [r"server/openapi/",
                      r"web/portal/src/api/",
                      r"schema\.d\.ts$"]),
    ("mock-fixture", [r"web/portal/src/mocks/",
                      r"(^|/)mocks/",
                      r"(^|/)fixtures?/",
                      r"testdata/"]),
    ("docs", [r"\.md$"]),
    ("portal-ui", [r"web/portal/src/(routes|components|app)/"]),
    ("config-default", [r"server/internal/config/",
                        r"\.(yaml|yml|toml)$",
                        r"(^|/)\.env"]),
    ("ui-style", [r"\.css$", r"\.scss$"]),
]

# Content signals (matched against the diff body, after path signals).
_CONTENT_SIGNALS: list[tuple[str, list[str]]] = [
    ("null-check", [r"==\s*nil", r"!=\s*nil", r"\?\.", r"\?\?",
                    r"\bis None\b", r"\bis not None\b",
                    r"if\s+len\(\w+\)\s*==\s*0",
                    r"==\s*null\b", r"!=\s*null\b", r"guard\s+let\b"]),
    ("error-message", [r"errors\.New\(", r"fmt\.Errorf\(", r"Errorf\(",
                       r"throw new Error", r"new Error\("]),
]

_CODE_EXTS = (".go", ".ts", ".tsx", ".js", ".jsx", ".py", ".sh")

_STRING_LINE = re.compile(r'^[+-]\s*(?:[frb]?["\']|""")')
# A string literal (double, single or backtick), for the skeleton rule.
_STR_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
                     r'|`(?:[^`\\]|\\.)*`')
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_DIFF_GIT_RE = re.compile(r"^diff --git a/\S+ b/(.+)$", re.M)


def changed_files(diff: str) -> list[str]:
    """File paths touched by a unified diff (``+++ b/<path>`` lines;
    falls back to ``diff --git`` headers when there are no +++ lines)."""
    if not diff:
        return []
    files = _DIFF_FILE_RE.findall(diff)
    if files:
        return files
    return _DIFF_GIT_RE.findall(diff)


def text_only(diff: str) -> bool:
    """True when every changed line is a string literal or a comment.

    Ported from RRSI components.py (same shape): a model-facing text edit.
    Morde extension: Go/TS comment markers (``//``, ``/*``) alongside
    RRSI's ``#``. Strict on purpose: a line such as
    ``return "已发送"`` is NOT text-only here -- see text_change() for the
    Morde rule that does catch it.
    """
    changed = [l for l in diff.splitlines()
               if (l.startswith("+") or l.startswith("-"))
               and not l.startswith("+++") and not l.startswith("---")
               and l[1:].strip()]
    if not changed:
        return False
    return all(_STRING_LINE.match(l)
               or l[1:].lstrip().startswith(("//", "#", "/*", "*", "<!--"))
               for l in changed)


def _skeleton(line: str) -> str:
    """A changed line with every string literal blanked out."""
    return _STR_RE.sub('""', line[1:]).strip()


def text_change(diff: str) -> bool:
    """True when the diff only swaps string contents: blank out every
    string literal and the removed/added line skeletons are identical
    (as multisets), with at least one literal on each side.

    Morde addition (RRSI has no equivalent): Morde's copy fixes usually
    look like ``-    return "已发送"`` / ``+    return "已分派任务"`` --
    a string inside a code line, which strict text_only() misses. Adding
    a brand-new string line is NOT a text change (nothing removed), and
    changing ``a + b + 1`` to ``a + b`` is not one either (skeletons
    differ), so logic fixes cannot pass as copy edits.
    """
    if not diff:
        return False
    removed, added = [], []
    for l in diff.splitlines():
        if l.startswith("+++") or l.startswith("---"):
            continue
        if l.startswith("-") and l[1:].strip():
            removed.append(l)
        elif l.startswith("+") and l[1:].strip():
            added.append(l)
    if not removed or not added:
        return False
    if not any(_STR_RE.search(l) for l in removed + added):
        return False
    return sorted(map(_skeleton, removed)) == sorted(map(_skeleton, added))


def _path_component(files: list[str]) -> str | None:
    for component, pats in _PATH_SIGNALS:
        if any(re.search(p, f) for f in files for p in pats):
            return component
    return None


def _content_component(diff: str) -> str | None:
    for component, pats in _CONTENT_SIGNALS:
        if any(re.search(p, diff) for p in pats):
            return component
    return None


def classify_diff(diff: str) -> str:
    """First matching component; default ``logic-small``.

    Order: text change -> path signals -> content signals -> any code
    file -> default. The default differs from RRSI's ``prompt``: a Morde
    candidate with no recognisable signal is a small logic fix until
    proven otherwise, and ``ui-text`` requires actual text evidence.
    """
    if not diff or not diff.strip():
        return "logic-small"
    if text_only(diff) or text_change(diff):
        return "ui-text"
    files = changed_files(diff)
    hit = _path_component(files)
    if hit:
        return hit
    hit = _content_component(diff)
    if hit:
        return hit
    if any(f.endswith(_CODE_EXTS) for f in files):
        return "logic-small"
    return "logic-small"


def has_evidence(component: str, diff: str) -> bool:
    """Does the diff carry any signal of ``component``?

    Same role as RRSI's has_evidence: a declared tag is kept only when
    the diff evidences it, so a proposer cannot name a skill/tool/workflow
    edit without shipping one, or relabel a fix as an untried component
    to satisfy a reserved exploration slot.
    """
    if component not in K or not diff:
        return False
    if component == "ui-text":
        return text_only(diff) or text_change(diff)
    if component == "ui-style":
        return (any(f.endswith((".css", ".scss")) for f in changed_files(diff))
                or bool(re.search(r"className=|style=\{\{", diff)))
    if component == "logic-small":
        files = changed_files(diff)
        if any(f.endswith(_CODE_EXTS) for f in files):
            return True
        # No parseable file headers: fall back to "there are code lines".
        return bool(diff.strip()) and not (text_only(diff) or text_change(diff))
    if component in ("null-check", "error-message"):
        pats = dict(_CONTENT_SIGNALS)[component]
        return any(re.search(p, diff) for p in pats)
    if component == "agent-tool":
        pats = dict(_PATH_SIGNALS)["agent-tool"]
        if any(re.search(p, f) for f in changed_files(diff) for p in pats):
            return True
        return bool(re.search(r"ToolBroker|host_tools|agent_tools|"
                              r"cloud_dsh_skills", diff))
    pats = dict(_PATH_SIGNALS).get(component, [])
    return any(re.search(p, f) for f in changed_files(diff) for p in pats)


def normalize(declared: str | None, diff: str,
              domain_signals: list | None = None) -> str:
    """Keep a declared label only with diff evidence, else re-attribute.

    Same contract as RRSI's normalize(). ``domain_signals`` is accepted
    for signature parity and ignored: Morde's signals are fixed in this
    module (they come from the Morde repo layout, not from a caller).
    """
    d = (declared or "").strip().lower()
    if d in K and has_evidence(d, diff):
        return d
    return classify_diff(diff)


def novelty(components: list[str], incumbent_counts: dict) -> int:
    """nu: number of STRUCTURAL components the candidate touches that the
    incumbent has never had an accepted edit on. RRSI components.py
    formula (K_STR only), ported verbatim in meaning.

    NOTE: this differs from ``rsi/selection.py::novelty()``, which counts
    every component type never in a winning edit. This one counts only
    K_STR, so a copy/config tweak earns no novelty credit -- only new
    machinery does.
    """
    return sum(1 for c in set(components or [])
               if c in K_STR and not incumbent_counts.get(c))
