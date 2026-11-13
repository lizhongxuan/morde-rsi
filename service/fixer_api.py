"""APIFixer: the real model-backed fixer for the morde-rsi service.

Implements the DSHFixer 4-step wiring (rsi/fixer.py) against any
OpenAI-compatible ``/chat/completions`` endpoint (DeepSeek by default):
render the brief, leak-check it, POST it to the model, and return the
model's unified diff. The model replies with ONLY a diff in a single
fenced block; the diff is validated with ``git apply --check`` and then
applied to the fixer worktree (like ManualFixer) so the Controller can
collect it with ``diff_of_worktree``.

Stdlib only (urllib, subprocess, json). The API key is never logged or
stored: it is only placed in the Authorization header of the request.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from rsi import critic as critic_mod
from rsi.fixer import Fixer, FixResult, render_brief
from service.receipt import compute_usd

_DIFF_FENCE_RE = re.compile(r"```diff[ \t]*\n(.*?)```", re.DOTALL)
_PLAIN_FENCE_RE = re.compile(r"```[ \t]*\n(.*?)```", re.DOTALL)

# Retryable: 429 + 5xx. Sleeps between attempts 1->2 and 2->3.
_RETRY_DELAYS = (2.0, 8.0)


class CostCapExceeded(RuntimeError):
    """Raised when a job's priced spend passes its USD cap."""

    def __init__(self, spent_usd: float, cap_usd: float):
        super().__init__(
            f"cost cap exceeded: spent ${spent_usd:.6f} > "
            f"cap ${cap_usd:.6f}")
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


def _extract_diff(content: str) -> str | None:
    """First ```diff fenced block; else a plain fence holding a diff."""
    m = _DIFF_FENCE_RE.search(content or "")
    if m:
        return m.group(1)
    for m in _PLAIN_FENCE_RE.finditer(content or ""):
        text = m.group(1)
        if "diff --git" in text or ("--- " in text and "+++ " in text):
            return text
    return None


class APIFixer(Fixer):
    """Fixer that asks a chat-completions model for a unified diff."""
    name = "api"

    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout_s: int = 120, max_tokens: int = 8192,
                 price_per_1m_input_usd: float | None = None,
                 price_per_1m_output_usd: float | None = None,
                 max_usd_per_job: float | None = None,
                 skill_md: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.price_per_1m_input_usd = price_per_1m_input_usd
        self.price_per_1m_output_usd = price_per_1m_output_usd
        self.max_usd_per_job = max_usd_per_job
        self.skill_md = skill_md
        self.tokens_in = 0
        self.tokens_out = 0
        self.usd_spent: float | None = None
        self.cost_seconds = 0.0

    # ------------------------------------------------------------ HTTP ---
    def _post(self, body: dict) -> dict:
        """POST {base_url}/chat/completions with retries.

        Retries up to 2 times (3 attempts total) on HTTP 429 / 5xx,
        sleeping 2s then 8s. Other 4xx raise immediately without retry;
        transport errors (URLError/timeout) raise without retry.
        Returns the parsed JSON response. Never includes the API key in
        any error message.
        """
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        call_seconds = 0.0
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST")
            t0 = time.time()
            try:
                with urllib.request.urlopen(req,
                                            timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                call_seconds += time.time() - t0
                if e.code == 429 or e.code >= 500:
                    if attempt < 2:
                        time.sleep(_RETRY_DELAYS[attempt])
                        continue
                    self.cost_seconds += call_seconds
                    raise RuntimeError(
                        "model API request failed: HTTP "
                        f"{e.code} after 3 attempts") from None
                self.cost_seconds += call_seconds
                raise RuntimeError(
                    f"model API request failed: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                call_seconds += time.time() - t0
                self.cost_seconds += call_seconds
                raise RuntimeError(f"model API request failed: {e}") from None
            call_seconds += time.time() - t0
            self.cost_seconds += call_seconds
            if not isinstance(payload, dict):
                raise RuntimeError(
                    "model API response was not a JSON object")
            return payload
        raise RuntimeError("model API request failed: no attempt made")

    # ------------------------------------------------------------ fix ----
    def fix(self, task: dict, worktree: Path, budget: dict) -> FixResult:
        worktree = Path(worktree)
        brief = render_brief(
            task, worktree, self.skill_md, budget.get("max_submissions", 3),
            prior_feedback=task.get("prior_feedback"))
        # Wiring guard: the brief must never carry held-out material. A
        # hit is a bug in the caller's wiring, not fixer gaming.
        check = critic_mod.review_brief(
            brief, heldout_markers=task.get("heldout_markers") or [])
        if check["verdict"] == "reject":
            raise RuntimeError(
                "brief leaked verifier material (wiring bug, not a fix): "
                + "; ".join(check["reasons"]))
        body = {
            "model": self.model,
            "messages": [
                {"role": "system",
                 "content": ("You are a code-fixing agent. Reply with ONLY "
                             "a unified diff in a single ```diff fenced "
                             "block, nothing else.")},
                {"role": "user", "content": brief},
            ],
            "max_tokens": self.max_tokens,
        }
        _cost_before = self.cost_seconds
        payload = self._post(body)
        call_seconds = round(self.cost_seconds - _cost_before, 1)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                "unexpected model response: no choices[0].message.content"
                ) from None
        diff = _extract_diff(content)
        if diff is None:
            raise RuntimeError("no diff block in model response")
        r = subprocess.run(["git", "apply", "--check"],
                           cwd=str(worktree), input=diff,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"git apply --check failed: {r.stderr.strip()}")
        # Apply it (as ManualFixer does) so the Controller can collect
        # the diff from this worktree with diff_of_worktree.
        r = subprocess.run(["git", "apply", "--whitespace=nowarn"],
                           cwd=str(worktree), input=diff,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git apply failed: {r.stderr.strip()}")
        usage = payload.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        self.tokens_in += prompt
        self.tokens_out += completion
        n = prompt + completion
        if (self.price_per_1m_input_usd is not None
                and self.price_per_1m_output_usd is not None):
            call_usd = compute_usd(prompt, completion,
                                   self.price_per_1m_input_usd,
                                   self.price_per_1m_output_usd)
            if call_usd is not None:
                self.usd_spent = (self.usd_spent or 0.0) + call_usd
                if (self.max_usd_per_job is not None
                        and self.usd_spent > self.max_usd_per_job):
                    raise CostCapExceeded(self.usd_spent,
                                          self.max_usd_per_job)
        return FixResult(diff=diff, submissions=1, cost_tokens=n,
                         cost_seconds=call_seconds,
                         detail=(f"api-fixer: model={self.model} "
                                 f"tokens={n}"))
