"""Persistent memory: durable learnings recalled into future runs.

Each memory is one markdown file with frontmatter:

    ---
    name: deploy-needs-migration
    description: Run DB migrations before deploying the web service
    tags: [deploy, db]
    type: project
    created: 2026-07-15
    ---
    The deploy step fails silently if migrations haven't run first ...

The durable store lives OUTSIDE any worktree (survives teardown), namespaced
per target repo: `<memory_dir>/<repo-key>/NNN-slug.md` plus a `MEMORY.md`
index.

Flow per run:
  * install(wt)  — copy the store into a gitignored `.factory/memory/` inside
    the worktree, create an `inbox/` for the agent to drop new learnings, and
    return a compact index to inject into the spec (read path).
  * harvest(wt)  — after the exec, validate + dedupe the inbox files and merge
    them into the durable store, rebuilding the index (write path).

Discovery is the injected index + the agent's own Read/Grep over
`.factory/memory/`; codegraph is not used (it indexes code, not markdown).
Curation is automatic: schema-check + dedupe, no human/LLM in the loop.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from util import log, slugify

_VALID_TYPES = {"project", "reference", "gotcha", "convention", "user"}


@dataclass
class MemoryRecord:
    name: str
    description: str
    tags: list[str]
    type: str
    body: str
    created: str = ""
    filename: str = ""

    def content_hash(self) -> str:
        return hashlib.sha256(self.body.strip().encode()).hexdigest()[:16]


def _parse(text: str) -> MemoryRecord | None:
    """Parse a memory file (lenient frontmatter). None if it's not a valid,
    non-empty memory (missing fences, name, description, or body)."""
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None
    fm_raw, body = m.group(1), m.group(2).strip()
    fields: dict[str, str] = {}
    for line in fm_raw.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip().lower()] = val.strip()
    name = slugify(fields.get("name", ""))
    desc = fields.get("description", "").strip().strip('"').strip("'")
    if not name or not desc or not body:
        return None
    tags_raw = fields.get("tags", "").strip().strip("[]")
    tags = [t.strip().strip('"').strip("'") for t in tags_raw.split(",") if t.strip()]
    typ = fields.get("type", "").strip().lower()
    if typ not in _VALID_TYPES:
        typ = "project"
    return MemoryRecord(name=name, description=desc, tags=tags, type=typ,
                        body=body, created=fields.get("created", "").strip())


def _render(rec: MemoryRecord) -> str:
    tags = "[" + ", ".join(rec.tags) + "]"
    return (
        f"---\n"
        f"name: {rec.name}\n"
        f"description: {rec.description}\n"
        f"tags: {tags}\n"
        f"type: {rec.type}\n"
        f"created: {rec.created}\n"
        f"---\n\n"
        f"{rec.body.strip()}\n"
    )


class Memory:
    def __init__(self, cfg):
        self.cfg = cfg
        # Single process, multiple item threads: serialize store mutation and
        # the read-during-install so a concurrent harvest can't tear the index.
        self._lock = threading.Lock()

    # --- store layout -----------------------------------------------------------

    def _repo_key(self) -> str:
        return slugify(Path(self.cfg.repo_path).name) or "repo"

    def _store(self) -> Path:
        return self.cfg.memory_dir / self._repo_key()

    def _load_all(self) -> list[MemoryRecord]:
        store = self._store()
        out: list[MemoryRecord] = []
        if not store.is_dir():
            return out
        for f in sorted(store.glob("[0-9]*.md")):
            try:
                rec = _parse(f.read_text(errors="replace"))
            except OSError:
                continue
            if rec:
                rec.filename = f.name
                out.append(rec)
        return out

    def _next_id(self) -> int:
        store = self._store()
        best = 0
        if store.is_dir():
            for f in store.glob("[0-9]*.md"):
                m = re.match(r"(\d+)-", f.name)
                if m:
                    best = max(best, int(m.group(1)))
        return best + 1

    # --- read path --------------------------------------------------------------

    def install(self, wt: Path) -> str:
        """Copy the store into a gitignored `.factory/memory/` in the worktree,
        create the `inbox/`, and return the compact index text to inject into
        the spec. Returns "" when memory is disabled or empty."""
        if not self.cfg.memory_enabled:
            return ""
        with self._lock:
            records = self._load_all()
        dst = Path(wt) / ".factory" / "memory"
        try:
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "inbox").mkdir(exist_ok=True)
            with self._lock:
                for f in self._store().glob("*.md") if self._store().is_dir() else []:
                    shutil.copy2(f, dst / f.name)
        except OSError as e:  # noqa: BLE001 - memory must never break a run
            log.error("memory: could not install into %s: %s", dst, e)
            return ""
        return self._render_injection(records)

    def _render_injection(self, records: list[MemoryRecord]) -> str:
        if not records:
            return ""
        shown = records[-self.cfg.memory_max_inject:]
        lines = [
            f"- {r.name} (.factory/memory/{r.filename}) — {r.description}"
            + (f" [{', '.join(r.tags)}]" if r.tags else "")
            for r in shown
        ]
        omitted = len(records) - len(shown)
        more = (f"\n(+{omitted} older memories on disk — Grep `.factory/memory` "
                f"to find them.)") if omitted > 0 else ""
        return (
            "## Working memory (learnings from prior factory runs)\n\n"
            "Before starting, skim these. When a hook looks relevant, Read the "
            "full note from its `.factory/memory/<file>` path. If, while doing "
            "this work, you discover a durable and reusable learning (a gotcha, "
            "a convention, a non-obvious fact about this repo), record it by "
            "writing a new markdown file into `.factory/memory/inbox/` with "
            "frontmatter `name`, `description`, `tags`, `type`. Keep each memory "
            "to one fact. Do not commit anything under `.factory/`.\n\n"
            f"{chr(10).join(lines)}{more}\n"
        )

    # --- write path -------------------------------------------------------------

    def harvest(self, wt: Path) -> int:
        """Merge new learnings from `.factory/memory/inbox/` into the durable
        store: validate frontmatter, dedupe by name and by content, assign ids,
        rebuild the index. Harvested inbox files are removed. Returns the number
        of memories newly added or updated."""
        if not self.cfg.memory_enabled:
            return 0
        inbox = Path(wt) / ".factory" / "memory" / "inbox"
        if not inbox.is_dir():
            return 0
        candidates = sorted(inbox.glob("*.md"))
        if not candidates:
            return 0
        merged = 0
        with self._lock:
            store = self._store()
            store.mkdir(parents=True, exist_ok=True)
            existing = self._load_all()
            by_name = {r.name: r for r in existing}
            hashes = {r.content_hash() for r in existing}
            for c in candidates:
                try:
                    rec = _parse(c.read_text(errors="replace"))
                except OSError:
                    rec = None
                if rec is None:
                    log.info("memory: dropping invalid candidate %s", c.name)
                    c.unlink(missing_ok=True)
                    continue
                h = rec.content_hash()
                if rec.name in by_name:
                    prior = by_name[rec.name]
                    if prior.content_hash() == h:
                        c.unlink(missing_ok=True)   # identical, skip
                        continue
                    # Same name, changed content: update in place.
                    rec.created = prior.created or str_today()
                    rec.filename = prior.filename
                    (store / prior.filename).write_text(_render(rec))
                    hashes.discard(prior.content_hash())
                    hashes.add(h)
                    by_name[rec.name] = rec
                    merged += 1
                    log.info("memory: updated '%s'", rec.name)
                elif h in hashes:
                    c.unlink(missing_ok=True)   # same body under a new name
                    continue
                else:
                    rec.created = rec.created or str_today()
                    rec.filename = f"{self._next_id():03d}-{rec.name}.md"
                    (store / rec.filename).write_text(_render(rec))
                    hashes.add(h)
                    by_name[rec.name] = rec
                    existing.append(rec)   # so _next_id advances within the loop
                    merged += 1
                    log.info("memory: added '%s'", rec.name)
                c.unlink(missing_ok=True)
            if merged:
                self._write_index()
        return merged

    def _write_index(self) -> None:
        records = self._load_all()
        lines = [f"# Factory memory: {self._repo_key()}", ""]
        for r in records:
            tags = f" [{', '.join(r.tags)}]" if r.tags else ""
            lines.append(f"- [{r.name}]({r.filename}) — {r.description}{tags}")
        try:
            (self._store() / "MEMORY.md").write_text("\n".join(lines) + "\n")
        except OSError as e:  # noqa: BLE001
            log.error("memory: could not write index: %s", e)


def str_today() -> str:
    # Isolated so a caller can't accidentally depend on a mocked clock; harvest
    # only needs a coarse date stamp.
    return date.today().isoformat()
