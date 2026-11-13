import pytest

from conftest import FIX_DIFF
from rsi.fixer import DSHFixer, ManualFixer, render_brief


def test_render_brief_contains_task_and_rules():
    brief = render_brief({"task_id": "FIX-001", "description": "off by one",
                          "repro": "pytest test_add"}, "/tmp/wt",
                         "SKILL RULES", max_submissions=3)
    assert "FIX-001" in brief and "SKILL RULES" in brief
    assert "3" in brief and "off by one" in brief


def test_dsh_fixer_is_a_stub_with_wiring_instructions():
    fixer = DSHFixer(skill_md="RULES")
    with pytest.raises(NotImplementedError) as e:
        fixer.fix({"task_id": "FIX-001"}, "/tmp/wt", {})
    assert "DSH Task" in str(e.value)


def test_manual_fixer_applies_diff(fixture_repo, tmp_path):
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text(FIX_DIFF)
    fixer = ManualFixer(diff_file)
    assert fixer.name == "manual-pilot"
    result = fixer.fix({"task_id": "FIX-001"}, fixture_repo, {})
    assert result.diff == FIX_DIFF and result.submissions == 1
    assert "a + b\n" in (fixture_repo / "calc.py").read_text()
    assert "SIMULATED" in result.detail
