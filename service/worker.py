"""Service worker: claims jobs from the JobStore and runs them through
the RSI Controller (one task per job), then records a cost receipt and,
on ADOPT with PRs enabled, opens a GitHub PR.

Stdlib only. Only infra errors (unknown domain, bad repo/clone, task
not found, run_task raising) mark a job "failed". A fixer failure
(including CostCapExceeded) is caught by the Controller itself and
surfaces as a STUCK summary, so the job still completes as "done".
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rsi.config import RSIConfig  # noqa: E402
from rsi.controller import Controller  # noqa: E402
from rsi.fixer import ManualFixer  # noqa: E402
from service.config import resolve_api_key, resolve_data_dir  # noqa: E402
from service.fixer_api import APIFixer  # noqa: E402
from service.receipt import build_receipt, write_receipt  # noqa: E402


def _load_domain(name: str):
    """Load domains/<name>/adapter.py under a per-domain module name.

    rsi.py loads it as top-level ``adapter``, which is fine for a
    single-domain CLI process — but a long-lived service handles several
    domains, and a plain ``import adapter`` returns whichever domain was
    imported first (the sys.modules cache): after any code path imports
    the self adapter, every later morde job would silently run with the
    self domain's SKILL.md, guards and components. Loading by file
    location under a unique name keeps the domains separate.
    """
    import importlib.util
    path = ROOT / "domains" / name / "adapter.py"
    if not path.exists():
        raise ImportError(f"unknown domain: {name}")
    mod_name = f"rsi_domain_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name].DOMAIN
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"unknown domain: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module.DOMAIN


class Worker:
    def __init__(self, config: dict, store):
        self.config = config
        self.store = store

    # ------------------------------------------------------------ queue ---
    def run_once(self) -> dict | None:
        job = self.store.claim_next()
        if job is None:
            return None
        return self.execute_job(job)

    def run_forever(self, stop_event=None, poll_s: float = 1.0) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if self.run_once() is None:
                if stop_event is not None:
                    stop_event.wait(poll_s)
                else:
                    time.sleep(poll_s)

    # ------------------------------------------------------------ jobs ---
    def _fail(self, job_id: str, message: str) -> dict:
        result = {"error": message}
        self.store.update_job(job_id, status="failed", result=result)
        return result

    def execute_job(self, job: dict) -> dict:
        t0 = time.time()
        job_id = job["id"]
        config = self.config
        data_dir = resolve_data_dir(config)
        job_dir = data_dir / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        runs_dir = job_dir / "runs"

        # 2. Domain adapter.
        domain_name = job.get("domain")
        adapter_path = ROOT / "domains" / str(domain_name) / "adapter.py"
        if not adapter_path.exists():
            return self._fail(job_id, f"unknown domain: {domain_name}")
        try:
            domain = _load_domain(domain_name)
        except ImportError as e:
            return self._fail(job_id, f"unknown domain: {domain_name} ({e})")

        # 3. Target repo.
        repo_raw = job.get("repo")
        if repo_raw and str(repo_raw).startswith(("http://", "https://")):
            dest = data_dir / "clones" / job_id
            try:
                r = subprocess.run(
                    ["git", "clone", str(repo_raw), str(dest)],
                    capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return self._fail(job_id,
                                  f"git clone timed out: {repo_raw}")
            if r.returncode != 0:
                return self._fail(
                    job_id,
                    f"git clone failed: {r.stderr.strip()}")
            repo = dest
        elif repo_raw:
            repo = Path(repo_raw)
            if not repo.is_dir() or not (repo / ".git").exists():
                return self._fail(
                    job_id, f"repo not found or not a git repo: {repo_raw}")
        else:
            repo = Path(config["domains"][domain_name]["repo"])

        # 4. Task.
        task = None
        if job.get("task_id"):
            task = domain.load_task(job["task_id"])
            if task is None:
                return self._fail(job_id,
                                  f"task not found: {job['task_id']}")
        elif job.get("task"):
            task = job["task"]
            if not isinstance(task, dict) or not task.get("task_id"):
                return self._fail(job_id,
                                  "inline task must contain task_id")
        else:
            return self._fail(job_id, "job has neither task_id nor task")
        (job_dir / "tasks.json").write_text(
            json.dumps({"tasks": [task]}, indent=1, ensure_ascii=False))

        # 5. RSI run config (domain config is local; the repo is the target).
        budgets = config.get("budgets", {}) or {}
        cfg = RSIConfig.load(
            domain.root / "rrsi.json",
            max_submissions=budgets.get("max_submissions", 3),
            time_budget_s=budgets.get("time_budget_s", 3600))

        # 6. Fixer.
        skill_path = domain.root / "SKILL.md"
        skill_md = skill_path.read_text() if skill_path.exists() else ""
        fixer_kind = job.get("fixer") or "api"
        if fixer_kind == "manual":
            diff_path = job_dir / "manual.diff"
            diff_path.write_text(job.get("diff") or "")
            fixer = ManualFixer(diff_path)
        else:
            api_key = resolve_api_key(config)
            if api_key is None:
                fixer_cfg = config.get("fixer", {}) or {}
                env_name = fixer_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
                return self._fail(
                    job_id, f"no model API key: set {env_name}")
            fixer_cfg = config.get("fixer", {}) or {}
            fixer = APIFixer(
                base_url=fixer_cfg.get(
                    "base_url", "https://api.deepseek.com/v1"),
                model=fixer_cfg.get("model", "deepseek-chat"),
                api_key=api_key,
                timeout_s=fixer_cfg.get("timeout_s", 120),
                max_tokens=fixer_cfg.get("max_tokens", 8192),
                price_per_1m_input_usd=fixer_cfg.get(
                    "price_per_1m_input_usd"),
                price_per_1m_output_usd=fixer_cfg.get(
                    "price_per_1m_output_usd"),
                max_usd_per_job=budgets.get("max_usd_per_job"),
                skill_md=skill_md)

        # 7. Run the round.
        try:
            ctl = Controller(
                repo, runs_dir, job_dir / "tasks.json", cfg, fixer,
                skill_md=skill_md,
                guard_fn=getattr(domain, "guards_for_diff", None),
                domain=domain,
                domain_patterns=getattr(domain, "critic_patterns", None),
                worktree_excludes=getattr(
                    domain, "fixer_worktree_excludes", None),
                domain_components=getattr(domain, "components", None))
            summary = ctl.run_task(task, 0)
        except Exception as e:  # infra error, not a fixer verdict
            return self._fail(job_id, str(e))

        # 8. Receipt.
        verdict = summary.get("verdict")
        branch = summary.get("branch")
        if branch is None and verdict == "ADOPT":
            # The Controller only sets summary["branch"] on the
            # NEEDS_HUMAN path; on ADOPT it commits to rsi/<task-id>
            # (its own branch convention) without recording the key,
            # so derive it here. Verified by the branch existing in
            # the repo, not assumed.
            branch = f"rsi/{task.get('task_id')}"
        wall = time.time() - t0
        if isinstance(fixer, APIFixer):
            tokens_in, tokens_out = fixer.tokens_in, fixer.tokens_out
            usd = fixer.usd_spent
        else:
            tokens_in, tokens_out, usd = 0, 0, None
        receipt = build_receipt(job_id, tokens_in=tokens_in,
                                tokens_out=tokens_out, usd=usd,
                                wall_seconds=wall, verdict=verdict,
                                branch=branch, pr_url=None)
        write_receipt(data_dir, job_id, receipt)

        # 9. PR on ADOPT (best effort; a PR failure never fails the job).
        pr_url = None
        pr_error = None
        if verdict == "ADOPT" and (config.get("github", {}) or {}).get(
                "create_pr"):
            try:
                from service.github_pr import (  # noqa: E402
                    create_pr, render_pr_body)
            except ImportError:
                pr_error = "github_pr module unavailable"
            else:
                title = (f"rsi: {task.get('task_id')} "
                         f"(score {summary.get('score')})")
                body = render_pr_body(
                    verdict, summary.get("baseline_score"),
                    summary.get("score"), summary.get("tiers"), receipt,
                    evidence_note=f"Evidence: {runs_dir}", branch=branch)
                try:
                    pr_url = create_pr(str(repo), branch, "main", title,
                                       body, config)
                except Exception as e:
                    pr_url, pr_error = None, str(e)
            if pr_url:
                receipt = build_receipt(
                    job_id, tokens_in=tokens_in, tokens_out=tokens_out,
                    usd=usd, wall_seconds=wall, verdict=verdict,
                    branch=branch, pr_url=pr_url)
                write_receipt(data_dir, job_id, receipt)

        # 10. Record the outcome.
        result = {
            "verdict": verdict,
            "task_id": task.get("task_id"),
            "branch": branch,
            "baseline_score": summary.get("baseline_score"),
            "score": summary.get("score"),
            "tiers": summary.get("tiers"),
            "detail": summary.get("detail"),
            "evidence_dir": str(runs_dir),
            "receipt": receipt,
            "pr_url": pr_url,
        }
        if pr_error is not None:
            result["pr_error"] = pr_error
        cost = {"tokens_in": tokens_in, "tokens_out": tokens_out,
                "usd": usd, "wall_seconds": wall}
        self.store.update_job(job_id, status="done", result=result,
                              cost=cost)
        return result
