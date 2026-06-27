"""Linear access through the Linear MCP server.

Rather than re-implement Linear's OAuth, the daemon speaks to Linear by driving
headless `claude -p`, which already holds the Linear MCP session. Each operation
is a tightly-scoped prompt with `--allowedTools mcp__linear` and a JSON-only
contract, so the surrounding Python stays deterministic.

Project `.mcp.json` (next to this package) registers the `linear` HTTP server and
is passed explicitly with `--mcp-config` so headless runs don't depend on global
state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from util import log, run

PROJECT_DIR = Path(__file__).resolve().parent.parent
MCP_CONFIG = PROJECT_DIR / ".mcp.json"


@dataclass
class Issue:
    id: str
    identifier: str   # e.g. ENG-123
    title: str
    description: str


def _extract_json(text: str):
    """Parse the model's final message as JSON, tolerating stray prose/fences."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        # Fall back to the first balanced {...} or [...] blob.
        for opener, closer in (("[", "]"), ("{", "}")):
            start = t.find(opener)
            end = t.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(t[start : end + 1])
                except Exception:  # noqa: BLE001
                    continue
    return None


class Linear:
    def __init__(self, cfg):
        self.cfg = cfg

    def _ask(self, prompt: str, *, timeout: int = 180):
        """Run `claude -p` with the Linear MCP tools and return parsed JSON."""
        cfg = self.cfg
        args = [
            cfg.claude_bin, "-p", prompt,
            "--model", cfg.agent_model,
            "--output-format", "json",
            "--allowedTools", "mcp__linear",
            "--permission-mode", "bypassPermissions",
        ]
        if MCP_CONFIG.exists():
            args += ["--mcp-config", str(MCP_CONFIG)]
        res = run(args, cwd=PROJECT_DIR, timeout=timeout)
        if not res.ok:
            log.error("linear/claude -p failed: %s", (res.err or res.out).strip()[:200])
            return None
        # `--output-format json` wraps the run; the assistant's final text is in
        # `result`. Older/newer shapes are tolerated.
        envelope = None
        try:
            envelope = json.loads(res.out)
        except Exception:  # noqa: BLE001
            return _extract_json(res.out)
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                log.error("linear/claude reported error: %s",
                          str(envelope.get("result"))[:200])
            inner = envelope.get("result", res.out)
            return _extract_json(inner if isinstance(inner, str) else json.dumps(inner))
        return _extract_json(res.out)

    def list_trigger_issues(self) -> list[Issue]:
        cfg = self.cfg
        if not cfg.linear_enabled:
            return []
        prompt = (
            f"Use the Linear MCP tools to find all issues in team \"{cfg.linear_team}\" "
            f"whose workflow state name is exactly \"{cfg.linear_trigger_state}\". "
            f"Return ONLY a JSON array, no prose and no markdown fences. Each element "
            f"must be an object with keys: id (the issue's id), identifier (e.g. "
            f"\"{cfg.linear_team}-123\"), title (string), description (string, empty "
            f"string if none). If there are no such issues, return []."
        )
        data = self._ask(prompt)
        if not isinstance(data, list):
            if data is not None:
                log.warning("list_trigger_issues: unexpected payload: %r", data)
            return []
        issues: list[Issue] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            issues.append(
                Issue(
                    id=str(it.get("id") or it.get("identifier") or "").strip(),
                    identifier=str(it.get("identifier") or it.get("id") or "").strip(),
                    title=str(it.get("title") or "").strip() or "(untitled)",
                    description=str(it.get("description") or ""),
                )
            )
        issues = [i for i in issues if i.id]
        if issues:
            log.info("linear: %d trigger issue(s) in %s/%s",
                     len(issues), cfg.linear_team, cfg.linear_trigger_state)
        return issues

    def set_state(self, identifier: str, state: str) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] would set %s -> state %s", identifier, state)
            return True
        if not state:
            return True
        data = self._ask(
            f"Use the Linear MCP tools to set the workflow state of issue "
            f"\"{identifier}\" to the state named \"{state}\". Return ONLY JSON: "
            f"{{\"ok\": true}} on success or {{\"ok\": false, \"error\": \"...\"}}."
        )
        ok = isinstance(data, dict) and bool(data.get("ok"))
        if not ok:
            log.error("linear set_state(%s -> %s) failed: %r", identifier, state, data)
        return ok

    def comment(self, identifier: str, body: str) -> bool:
        if self.cfg.dry_run:
            log.info("[dry-run] would comment on %s: %s", identifier, body[:80])
            return True
        # Embed the comment body as a JSON string so quoting is unambiguous.
        encoded = json.dumps(body)
        data = self._ask(
            f"Use the Linear MCP tools to add a comment to issue \"{identifier}\". "
            f"The comment body, as a JSON-encoded string, is: {encoded}. "
            f"Return ONLY JSON: {{\"ok\": true}} on success or "
            f"{{\"ok\": false, \"error\": \"...\"}}."
        )
        ok = isinstance(data, dict) and bool(data.get("ok"))
        if not ok:
            log.error("linear comment(%s) failed: %r", identifier, data)
        return ok

    def probe(self) -> bool:
        """One-shot connectivity check used by the verify step."""
        data = self._ask(
            "Use the Linear MCP tools to list the teams I have access to. Return "
            "ONLY a JSON array of team keys (strings). If you cannot reach Linear, "
            "return []."
        )
        log.info("linear probe result: %r", data)
        return isinstance(data, list)
