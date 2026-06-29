# factory

An always-on agent that turns work requests into pull requests.

```
Linear ticket (Todo)  ─┐
                       ├─▶ draft PR ─▶ write spec ─▶ printer exec ─▶ push ─▶ gh pr ready ─▶ update
trigger email (unread) ─┘
```

- **Linear** is the primary work queue. New tickets in a configured state get
  picked up automatically.
- **Email** is a second trigger: an unread message's **subject becomes the
  title** and its **body becomes the spec**.
- For each item, factory opens a **draft PR** up front, writes a `printer` spec,
  runs `printer exec` to implement + review it, pushes the result, marks the PR
  **ready for review**, and reports back (Linear state/comment, or an email
  reply). It **polls every few minutes** and runs unattended.

All GitHub operations go through the **`gh` CLI**. Linear is accessed through the
**Linear MCP server** by driving headless `claude -p` (which already holds the
Linear OAuth session) — the daemon never stores Linear credentials.

## How it fits together

| Piece | File | Role |
| --- | --- | --- |
| Daemon / loop | `factoryd.py` | poll → dispatch, signals, logging |
| Config | `config.py`, `.env` | all `FACTORY_*` settings |
| Pipeline | `pipeline.py` | shared trigger→PR→printer→ready→update flow |
| Linear (MCP) | `tools/linear_mcp.py` | list trigger issues / set state / comment via `claude -p` |
| Inbox | `tools/inbox.py` | IMAP: fetch unread triggers, mark `\Seen` |
| Notifications | `tools/notify.py` | SMTP send (status + email replies) |
| GitHub | `tools/gh.py` | `gh pr create --draft` / `pr ready` / `pr comment` |
| Git | `tools/gitops.py` | branch / commit spec / commit work / push |
| Printer | `tools/printer.py` | run `printer exec`, classify success/blocked |
| Spec generation | `tools/specgen.py` | `claude -p`: request → printer checklist spec |
| Self-improvement | `selfimprove.py` | run printer on factory's **own** source → build → commit → restart |
| Build / restart | `tools/selfupdate.py` | compile+import build gate; `supervisorctl restart` |

## Setup

1. **Requirements** (already present on this machine): `gh` (authenticated),
   `printer`, `claude`, `git`, Python 3.12. SMTP/IMAP use only the stdlib.
2. **Configure**:
   ```sh
   cd ~/Projects/factory
   cp .env.example .env
   $EDITOR .env          # set FACTORY_REPO_PATH, FACTORY_LINEAR_TEAM, IMAP/SMTP creds
   ```
   The target repo (`FACTORY_REPO_PATH`) must be a clone with an `origin` remote
   you can push to via `gh`/`git`.
3. **Verify connectivity**:
   ```sh
   ./run.sh --probe         # checks gh auth + Linear MCP reachability
   ```

## Running

```sh
./run.sh                    # run forever (polls every FACTORY_POLL_INTERVAL s)
./run.sh --once             # single poll cycle, then exit
./run.sh --once --verbose   # same, with debug logging
FACTORY_DRY_RUN=1 ./run.sh --once   # log intended actions, mutate nothing
```

Headless / always-on, two options:

- **supervisord** (`supervisord.conf`) — **required for self-improvement**, since
  factory restarts itself via `supervisorctl`:
  ```sh
  pip install --user supervisor          # or pipx install supervisor
  supervisord -c supervisord.conf        # start in the background
  supervisorctl -c supervisord.conf status
  supervisorctl -c supervisord.conf tail -f factory
  ```
- **systemd** (`factory.service`) — plain always-on without self-restart (edit
  paths, then `systemctl --user enable --now factory`).

## Self-improvement

factory can improve a second "code directory": **its own source**. Any trigger
(Linear ticket or email) whose **title starts with the self marker** (default
`[self]`) is routed away from the customer repo and instead:

```
[self] request ─▶ spec ─▶ printer exec (on factory's source) ─▶ build ─▶ commit ─▶ restart
                                                                   │
                                                          (build fails → roll back, don't restart)
```

1. `printer exec` runs against `FACTORY_SELF_PATH` (factory's own checkout).
2. **build gate** (`tools/selfupdate.py`): byte-compile every module + import the
   entry points in a fresh interpreter. This must pass before anything is kept.
3. on success: commit on the running branch (and push if `FACTORY_SELF_PUSH=1`),
   report back (Linear comment / email reply), then **restart via supervisor** so
   the new code runs.
4. on failure (printer blocked or build broken): the working tree is rolled back
   (`git reset --hard` + `git clean -fd`, leaving git-ignored `.env` untouched)
   and factory is **not** restarted.

Self-updates happen on the *running branch* (not a throwaway PR branch) because a
restart redeploys the working tree — the build gate, not code review, is the
safety net. Enable pushes/PRs with `FACTORY_SELF_PUSH=1` if you also want the
change on `origin`.

Trigger it manually too (no ticket needed):

```sh
./selfimprove.py "add a --status flag to factoryd"
./selfimprove.py "rework retries" --body-file note.md --no-restart   # apply+build only
echo "switch to JSON logging" | ./selfimprove.py "json logs" --body -
./selfimprove.py --build-only        # just run the build gate
./selfimprove.py --restart-only      # just restart via supervisor
./selfimprove.py --status            # supervisor status for factory
```

## Triggers & dedup

- **Linear**: only issues in `FACTORY_LINEAR_TRIGGER_STATE` (default `Todo`) are
  picked up. On claim the issue moves to `In Progress` (so it won't re-trigger);
  when the PR is ready it moves to `In Review`. State is the dedup mechanism — no
  external database.
- **Email**: only **unread** messages are processed (optionally restricted by
  `FACTORY_IMAP_ALLOWED_SENDERS`). A message is marked read once its draft PR
  exists, so it is handled exactly once. Completion is reported as a reply to the
  sender.

## Crash safety

State lives in Linear (`In Progress`) and IMAP (`\Seen`), and branch/spec names
are derived deterministically from the ticket id / subject. After a crash,
restart the daemon: a half-finished item is resumed by re-running
`printer exec` against the same spec (printer's on-disk checkpoint continues
where it left off), and no duplicate PR is opened (the branch/PR already exist).

## Configuration

See `.env.example` for every variable. The essentials:

| Var | Purpose |
| --- | --- |
| `FACTORY_REPO_PATH` | repo the PRs are opened on (**required**) |
| `FACTORY_LINEAR_TEAM` | Linear team key, e.g. `ENG` (required to poll Linear) |
| `FACTORY_LINEAR_TRIGGER_STATE` / `_INPROGRESS_STATE` / `_REVIEW_STATE` | workflow states |
| `FACTORY_IMAP_*` | inbox to monitor |
| `FACTORY_SMTP_*`, `FACTORY_NOTIFY_TO` | notifications |
| `FACTORY_POLL_INTERVAL` | seconds between polls (default 180) |
| `FACTORY_AGENT_MODEL` | model passed to `printer` / `claude -p` |

## Notes & assumptions

- One repo per daemon instance. Run multiple instances (each with its own
  `.env`) for multiple repos.
- Email-triggered work has no Linear ticket; its "ticket update" is the SMTP
  reply. To file a Linear ticket from each email instead, have the pipeline call
  a `linear.create_issue` step (left as a one-line extension).
- The Linear bridge assumes `claude -p` can reach the Linear MCP server defined
  in `.mcp.json`. If your headless `claude` needs the server pre-approved, run an
  interactive `claude` once in this directory to authorize it.
# agent-email-driver
