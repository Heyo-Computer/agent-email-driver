"""Crash-resume journal: one JSON file per in-flight work item.

`pipeline.process` records an item when work starts and clears it when the
item reaches a terminal state (completed, or a failure that was reported).
Anything still on disk at daemon startup is work that was interrupted mid-
flight — a crash, restart, or power loss — and gets re-queued ahead of new
triggers. The pipeline is idempotent on resume: the branch is re-derived
deterministically, the existing draft PR is recovered by branch name, and
`printer exec` resumes from its own `.printer/exec` checkpoint.

An attempt counter guards against crash loops: an item that keeps taking the
daemon down is abandoned (with a notification) after `resume_max_attempts`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from util import log, slugify


class Journal:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir: Path = cfg.state_dir / "inflight"

    def _path(self, item) -> Path:
        # source + ref uniquely identify a work item (email uid / linear id).
        return self.dir / f"{item.source}-{slugify(str(item.ref))}.json"

    def record(self, item) -> None:
        """Persist `item` as in-flight. Keeps the attempt count of an earlier
        entry for the same item (a resume must not reset its crash budget)."""
        if self.cfg.dry_run:
            return
        path = self._path(item)
        attempts = 0
        if path.is_file():
            try:
                attempts = json.loads(path.read_text()).get("attempts", 0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"item": asdict(item), "attempts": attempts}, indent=2
            ))
        except Exception as e:  # noqa: BLE001 - journaling must not block work
            log.error("journal: could not record %s: %s", path.name, e)

    def bump(self, item) -> int:
        """Increment and return the attempt count (called before a resume)."""
        path = self._path(item)
        try:
            data = json.loads(path.read_text())
            data["attempts"] = data.get("attempts", 0) + 1
            path.write_text(json.dumps(data, indent=2))
            return data["attempts"]
        except Exception as e:  # noqa: BLE001
            log.error("journal: could not bump %s: %s", path.name, e)
            return 1

    def clear(self, item) -> None:
        if self.cfg.dry_run:
            return
        try:
            self._path(item).unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            log.error("journal: could not clear %s: %s", self._path(item).name, e)

    def pending(self) -> list[tuple[dict, int]]:
        """All in-flight entries as (item_dict, attempts), oldest first."""
        if not self.dir.is_dir():
            return []
        out: list[tuple[float, dict, int]] = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                out.append((f.stat().st_mtime, data["item"],
                            data.get("attempts", 0)))
            except Exception as e:  # noqa: BLE001
                log.error("journal: unreadable entry %s (%s); removing", f, e)
                try:
                    f.unlink()
                except Exception:  # noqa: BLE001
                    pass
        out.sort(key=lambda t: t[0])
        return [(item, attempts) for _mtime, item, attempts in out]
