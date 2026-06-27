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

Headless / always-on via systemd: see `factory.service` (edit paths, then
`systemctl --user enable --now factory`).

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
