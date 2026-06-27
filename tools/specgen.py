"""Turn a work request (title + body) into a clean printer spec.

Uses headless `claude -p` to convert prose into the printer spec format (a
`## Tasks` section of `- [ ]` checklist items — see printer/README.md). If the
request body already looks like a checklist, or if `claude -p` is unavailable,
it falls back to a deterministic template so the pipeline still proceeds (the
printer bootstrap turn will expand prose into tasks).
"""

from __future__ import annotations

import re
from pathlib import Path

from util import log, run

_PROMPT = """\
Convert the following work request into a `printer` spec file in markdown.

Output ONLY the markdown spec, nothing else. Format:

# <short project title>

<one or two sentence summary of the goal>

## Tasks

- [ ] <imperative task title>
  <optional 2-space-indented description>
- [ ] <next task>

Rules: top-level checklist items must be `- [ ]` at column 0. Keep tasks
concrete and independently completable. Do not include code fences around the
whole document.

REQUEST TITLE: {title}

REQUEST BODY:
{body}
"""


def _looks_like_checklist(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*[-*+]\s*\[[ xX]\]", text or ""))


def _fallback_spec(title: str, body: str) -> str:
    if _looks_like_checklist(body):
        # Already a checklist — keep it, just ensure a heading exists.
        head = "" if body.lstrip().startswith("#") else f"# {title}\n\n"
        return f"{head}{body.strip()}\n"
    body_block = (body or "").strip() or "(no description provided)"
    return (
        f"# {title}\n\n"
        f"{body_block}\n\n"
        f"## Tasks\n\n"
        f"- [ ] {title}\n"
        f"  Implement the request described above. Break this into concrete\n"
        f"  steps as needed and complete them.\n"
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", t, re.S)
    return m.group(1).strip() if m else t


class SpecGen:
    def __init__(self, cfg):
        self.cfg = cfg

    def write_spec(self, spec_path: Path, *, title: str, body: str) -> Path:
        """Generate and write the spec file. Returns the path written."""
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        content = self._generate(title, body)
        if self.cfg.dry_run:
            log.info("[dry-run] would write spec %s (%d chars)", spec_path,
                     len(content))
            return spec_path
        spec_path.write_text(content)
        log.info("wrote spec %s", spec_path)
        return spec_path

    def _generate(self, title: str, body: str) -> str:
        cfg = self.cfg
        if cfg.dry_run:
            return _fallback_spec(title, body)
        prompt = _PROMPT.format(title=title, body=(body or "").strip())
        res = run(
            [
                cfg.claude_bin, "-p",
                "--model", cfg.agent_model,
                prompt,
            ],
            cwd=self.cfg.repo_path,
            timeout=300,
        )
        if res.ok and res.out.strip():
            spec = _strip_fences(res.out)
            if _looks_like_checklist(spec):
                return spec.rstrip() + "\n"
            log.warning("claude spec output had no checklist; using fallback")
        else:
            log.warning("claude -p spec generation failed (%s); using fallback",
                        res.err.strip()[:120])
        return _fallback_spec(title, body)
