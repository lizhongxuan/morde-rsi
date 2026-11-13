"""Component labels (rsi/components.py) and the per-component ledger summary.

Diff fixtures are hand-written unified diffs against Morde's real paths
(server/internal/..., web/portal/src/...); no Morde code is read or run.
"""
from rsi.components import (K, K_STR, changed_files, classify_diff,
                            has_evidence, normalize, novelty, text_change,
                            text_only)
from rsi.history import History

GO_FIX_DIFF = """diff --git a/server/internal/app/foo.go b/server/internal/app/foo.go
--- a/server/internal/app/foo.go
+++ b/server/internal/app/foo.go
@@ -1,3 +1,3 @@
 func calc(a, b int) int {
-    return a + b + 1
+    return a + b
 }
"""

# A copy fix: only the string inside the return changes (CAND-001 shape).
TEXT_TSX_DIFF = """diff --git a/web/portal/src/routes/Projects.tsx b/web/portal/src/routes/Projects.tsx
--- a/web/portal/src/routes/Projects.tsx
+++ b/web/portal/src/routes/Projects.tsx
@@ -1,3 +1,3 @@
 function statusText(s: string) {
-    return "已发送";
+    return "已分派任务";
 }
"""

TEXT_ONLY_DIFF = """diff --git a/web/portal/src/data/strings.ts b/web/portal/src/data/strings.ts
--- a/web/portal/src/data/strings.ts
+++ b/web/portal/src/data/strings.ts
@@ -1,3 +1,3 @@
-    "hello",
+    "你好",
"""

PORTAL_UI_DIFF = """diff --git a/web/portal/src/routes/Assistant.tsx b/web/portal/src/routes/Assistant.tsx
--- a/web/portal/src/routes/Assistant.tsx
+++ b/web/portal/src/routes/Assistant.tsx
@@ -1,3 +1,3 @@
-    const [open, setOpen] = useState(false);
+    const [open, setOpen] = useState(true);
"""

API_DIFF = """diff --git a/server/openapi/openapi.yaml b/server/openapi/openapi.yaml
--- a/server/openapi/openapi.yaml
+++ b/server/openapi/openapi.yaml
@@ -1,3 +1,3 @@
 paths:
   /tasks:
-    get: {}
+    post: {}
"""

MOCK_DIFF = """diff --git a/web/portal/src/mocks/handlers.ts b/web/portal/src/mocks/handlers.ts
--- a/web/portal/src/mocks/handlers.ts
+++ b/web/portal/src/mocks/handlers.ts
@@ -1,3 +1,3 @@
-    return { ok: true };
+    return { ok: false };
"""

DOCS_DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
-旧的介绍
+新的介绍
"""

CONFIG_DIFF = """diff --git a/server/internal/config/default.yaml b/server/internal/config/default.yaml
--- a/server/internal/config/default.yaml
+++ b/server/internal/config/default.yaml
@@ -1,3 +1,3 @@
-timeout: 30
+timeout: 60
"""

AGENT_TOOL_DIFF = """diff --git a/server/internal/app/agent_tools.go b/server/internal/app/agent_tools.go
--- a/server/internal/app/agent_tools.go
+++ b/server/internal/app/agent_tools.go
@@ -1,3 +1,3 @@
 func tools() []Tool {
-    return []Tool{search}
+    return []Tool{search, fetch}
 }
"""

SKILL_DIFF = """diff --git a/skills/review/SKILL.md b/skills/review/SKILL.md
--- a/skills/review/SKILL.md
+++ b/skills/review/SKILL.md
@@ -1,3 +1,3 @@
-旧规则
+新规则
"""

WORKFLOW_DIFF = """diff --git a/scripts/start-macos.sh b/scripts/start-macos.sh
--- a/scripts/start-macos.sh
+++ b/scripts/start-macos.sh
@@ -1,3 +1,3 @@
-./old.sh
+./new.sh
"""

HARNESS_DIFF = """diff --git a/web/portal/e2e/login.spec.ts b/web/portal/e2e/login.spec.ts
--- a/web/portal/e2e/login.spec.ts
+++ b/web/portal/e2e/login.spec.ts
@@ -1,3 +1,3 @@
-    test.setTimeout(5000);
+    test.setTimeout(10000);
"""

NULL_CHECK_DIFF = """diff --git a/server/internal/app/foo.go b/server/internal/app/foo.go
--- a/server/internal/app/foo.go
+++ b/server/internal/app/foo.go
@@ -1,3 +1,4 @@
 func get(x *Thing) string {
+    if x == nil {
+        return ""
+    }
     return x.Name
 }
"""

ERROR_DIFF = """diff --git a/server/internal/app/foo.go b/server/internal/app/foo.go
--- a/server/internal/app/foo.go
+++ b/server/internal/app/foo.go
@@ -1,3 +1,3 @@
-    return nil
+    return errors.New("bad input")
"""

UNKNOWN_DIFF = """diff --git a/somewhere/odd.xyz b/somewhere/odd.xyz
--- a/somewhere/odd.xyz
+++ b/somewhere/odd.xyz
@@ -1,3 +1,3 @@
-    do_old_thing()
+    do_new_thing()
"""


def test_vocabulary_is_morde_not_rrsi_harness():
    assert "prompt" not in K and "subagent" not in K and "memory" not in K
    assert set(K_STR) == {"agent-tool", "skill", "workflow", "test-harness"}
    assert set(K_STR) <= set(K)
    # The six SKILL.md proposer labels survive verbatim.
    for label in ("ui-text", "ui-style", "null-check", "error-message",
                  "config-default", "logic-small"):
        assert label in K


def test_text_only_strict():
    assert text_only(TEXT_ONLY_DIFF) is True
    assert text_only(GO_FIX_DIFF) is False
    assert text_only("") is False
    # A string inside a code line is NOT strict text-only (RRSI rule).
    assert text_only(TEXT_TSX_DIFF) is False


def test_text_change_catches_string_inside_code():
    # CAND-001 shape: only the string contents differ.
    assert text_change(TEXT_TSX_DIFF) is True
    assert text_change(TEXT_ONLY_DIFF) is True
    # A real logic change is not a text change (skeletons differ).
    assert text_change(GO_FIX_DIFF) is False
    # Adding a brand-new string line is not a text change either.
    assert text_change(NULL_CHECK_DIFF) is False


def test_changed_files_parses_paths():
    assert changed_files(GO_FIX_DIFF) == ["server/internal/app/foo.go"]
    assert changed_files("") == []


def test_classify_diff_filename_signals():
    assert classify_diff(TEXT_TSX_DIFF) == "ui-text"
    assert classify_diff(TEXT_ONLY_DIFF) == "ui-text"
    assert classify_diff(PORTAL_UI_DIFF) == "portal-ui"
    assert classify_diff(GO_FIX_DIFF) == "logic-small"
    assert classify_diff(API_DIFF) == "api-contract"
    assert classify_diff(MOCK_DIFF) == "mock-fixture"
    assert classify_diff(DOCS_DIFF) == "docs"
    assert classify_diff(CONFIG_DIFF) == "config-default"
    assert classify_diff(AGENT_TOOL_DIFF) == "agent-tool"
    assert classify_diff(WORKFLOW_DIFF) == "workflow"
    assert classify_diff(HARNESS_DIFF) == "test-harness"
    assert classify_diff(NULL_CHECK_DIFF) == "null-check"
    assert classify_diff(ERROR_DIFF) == "error-message"


def test_classify_diff_skill_beats_docs_for_skill_md():
    # A SKILL.md is a .md file, but the skill signal wins (checked first).
    assert classify_diff(SKILL_DIFF) == "skill"


def test_classify_diff_unknown_path_defaults_to_logic_small():
    assert classify_diff(UNKNOWN_DIFF) == "logic-small"
    assert classify_diff("") == "logic-small"


def test_has_evidence_and_normalize_reattribute_unevidenced_claim():
    # Declared "skill" with a copy-only diff: no evidence -> re-attribute.
    assert has_evidence("skill", TEXT_TSX_DIFF) is False
    assert normalize("skill", TEXT_TSX_DIFF) == "ui-text"
    # Declared label with evidence is kept.
    assert has_evidence("logic-small", GO_FIX_DIFF) is True
    assert normalize("logic-small", GO_FIX_DIFF) == "logic-small"
    assert has_evidence("ui-text", TEXT_TSX_DIFF) is True
    assert normalize("ui-text", TEXT_TSX_DIFF) == "ui-text"
    # Missing / bogus declaration falls back to the diff.
    assert normalize(None, GO_FIX_DIFF) == "logic-small"
    assert normalize("bogus-label", GO_FIX_DIFF) == "logic-small"
    assert has_evidence("bogus-label", GO_FIX_DIFF) is False


def test_novelty_counts_structural_only():
    assert novelty(["agent-tool", "ui-text"], {}) == 1
    assert novelty(["agent-tool"], {"agent-tool": 1}) == 0
    assert novelty(["skill", "workflow"], {"skill": 2}) == 1
    assert novelty(["logic-small"], {}) == 0


def test_component_summary_from_handwritten_ledger(tmp_path):
    h = History(tmp_path / "history.jsonl")
    h.append_candidate(0, "T1", "c1", "A", "logic-small", None, "d1", {},
                       "ADOPT", "aaa", 0.8, None, 1.0)
    h.append_candidate(1, "T2", "c1", "A", "logic-small", None, "d2", {},
                       "REJECT", "aaa", 0.5, None, 1.0)
    h.append_candidate(2, "T3", "c1", "A", "logic-small", None, "d3", {},
                       "REJECT", "aaa", 0.5, None, 1.0)
    h.append_candidate(3, "T4", "c1", "A", "portal-ui", None, "d4", {},
                       "ADOPT", "aaa", 0.9, None, 1.0)
    h.append_candidate(4, "T5", "c1", "A", "portal-ui", None, "d5", {},
                       "NEEDS_HUMAN", "aaa", 0.7, None, 1.0)
    # Legacy record: no "components" list, only the single field.
    h.append({"round": 5, "task_id": "T6", "candidate_id": "c1",
              "component": "ui-text", "verdict": "REJECT", "score": 0.5})
    s = h.component_summary()
    assert s["logic-small"] == {"attempts": 3, "adopt": 1, "reject": 2,
                                "decided": 3, "win_rate": 0.3333}
    assert s["portal-ui"] == {"attempts": 2, "adopt": 1, "reject": 0,
                              "decided": 1, "win_rate": 1.0}
    assert s["ui-text"] == {"attempts": 1, "adopt": 0, "reject": 1,
                            "decided": 1, "win_rate": 0.0}


def test_append_candidate_stores_components_list(tmp_path):
    h = History(tmp_path / "history.jsonl")
    h.append_candidate(0, "T1", "c1", "A", None, None, "d1", {},
                       "ADOPT", "aaa", 0.8, None, 1.0,
                       components=["portal-ui", "ui-text"])
    r = h.records()[0]
    assert r["components"] == ["portal-ui", "ui-text"]
    assert r["component"] == "portal-ui"  # first entry is the primary
