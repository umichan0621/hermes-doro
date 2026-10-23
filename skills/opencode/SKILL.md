---
name: opencode
description: "Control configured host OpenCode projects from Lark: send coding tasks, run repository queries over HTTP, retrieve host files, and monitor long-running work. Use whenever the user mentions OpenCode, a project listed in /opt/data/opencode_projects.yaml, asks to delegate work to the host coding agent, or wants history, activity, files, or analysis from a host-side project—even without explicitly saying 'OpenCode'."
version: 1.1.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [OpenCode, host project, coding, remote session]
---

# Host OpenCode Control

This skill bridges Doro to host-side OpenCode 2.x. The container and host are
separate machines: a host path is not locally readable merely because it
appears in the project map. Use the configured API for host work.

All orchestration — session persistence, version gating, polling, permission
approval, rotation bookkeeping — lives in `scripts/oc_api.py`. Prefer its
commands over manual HTTP; branch on its exit codes, not on stderr text.

## Non-negotiable rules

- Use only projects listed in `/opt/data/opencode_projects.yaml`; never invent a
  project name, directory, port, or endpoint. The literal project name
  `default` resolves to the YAML's default project.
- Use the V2 API only. `oc_api.py` gates every call on `GET /api/info`
  reporting a 2.x version (cached per project, TTL 300 s). A `200 text/html`
  response is the web SPA, not a healthy API — never hand-roll V1 paths; they
  fall through to the SPA and fail silently.
- The API endpoint is `http://host.docker.internal:<port>` with Basic Auth
  username `opencode` and `$OPENCODE_SERVER_PASSWORD`. Never expose the
  password or API keys.
- The location shell (`oc_api.py shell`, `overview`, `todos`) is NOT gated by
  the plan agent's read-only permissions. Keep every shell command read-only;
  stateful or reasoning-heavy work belongs in a `task` turn.
- Run host git, file, build, and analysis commands through OpenCode. Do not
  claim host-container visibility without an actual mount or control socket.
- Preserve the user's intent, project, and scope. Do not silently broaden work.
- User-facing replies, progress updates, reports, and help text must be in the
  user's language; templates and examples in this file remain English.

## Agents: plan by default, build only with consent

- Stored sessions run the read-only **plan** agent by default. `task` switches
the session back to the requested agent before every turn, so a previous
build run never leaves residual write access.
- **Work-item sessions (`task --dir D`)**: route tasks to a per-work-item
session keyed by a directory INSIDE the project dir (e.g. a per-work-item
record subdirectory) — one work item per session for free, no rotation
needed. Sessions stay anchored at the PROJECT ROOT, so every work item
keeps full-project access; the directory is a client-side registry key
(per-project `dirs` map in the session store), not a server-side sandbox.
`--dir` alone resumes the work item's session when one is already recorded
for that directory (same work item follow-up), otherwise creates a fresh
one (after verifying the directory exists); `--dir + --new` archives that
work item's old session into the rotation history and starts a fresh one.
The directory guard (`_resolve_workdir`) resolves relative paths against
the project dir and refuses anything outside it — the project dir is the
hard boundary. Work-item sessions are ordinary root sessions: they appear
in `sessions <project>` and `info <project> <session_id>` (which reports
`location`).
- **build** (write access) requires BOTH the user's explicit consent AND the
  double flag `--agent build --allow-build`. Never pass these on your own
  initiative — ask first, and mention what the task intends to change.
- Any agent other than `plan` is treated like build and needs the same double
  opt-in.
- When a plan-agent turn concludes that it cannot proceed because writes are
  needed, report that conclusion and ask the user for consent; on approval,
  re-send the task with `--agent build --allow-build`. Do not re-run with
  build privileges just because the plan turn refused.

## Session hygiene: one work item per session

- A stored session carries ALL its prior turns — user messages, tool outputs,
  reports — into every new turn. Reusing a session for a different work item
  wastes tokens and time and pollutes the context so conclusions from one
  ticket can leak into another analysis. Before sending a task, check the
  session's title / prior prompts via `status`; a different work item MUST be
  rotated first (`task --new` or `rotate` — the old session is titled and
  archived, not lost). The only exception is a follow-up on the SAME ticket/
  artifact.
- **Report the session's context size to the user before running** — read
  `tokens.input` / `tokens.cache.read` from `status` or `info` for the
  session about to receive the task and state it in the progress message
  (after a rotation it reads near zero). This lets the user stop a polluted
  run before it burns tokens. Caller skills (e.g. robotics-data-analysis)
  may mandate this — follow them.

## Project resolution and Lark commands

Read `/opt/data/opencode_projects.yaml` before the first API call. It contains
a `default` project and a `projects` map with `dir` and `port`.

- Use the default project (or say `default`) when no project is named.
- Natural language such as "check X in my_project" selects that project.
- `/opencode project` lists projects and the current selection.
- `/opencode project <name>` selects a project.
- `/opencode project <name> new` rotates that project's session.
- `/opencode new` rotates the current project's session.
- `/opencode new <task>` rotates, then sends the task (`task --new`).
- `/opencode <task>` sends a task to the selected project.
- `/opencode status` shows every project's session state.
- `/opencode follow` waits for the running turn and reports (`wait`).
- `/opencode diff` reports what the last turn changed (`diff`).
- `/opencode stop` interrupts the running turn (`stop`); the session stays
  usable.
- A bare `/opencode` or `/opencode help` returns only:

  ```text
  OpenCode remote control:
    /opencode project                     list projects and current selection
    /opencode project <name>              switch project
    /opencode project <name> new          rotate that project's session
    /opencode new [task]                  rotate the current project, optionally send a task
    /opencode <natural-language task>     send the task to the current project
    /opencode status                      per-project session overview
    /opencode follow                      wait for the running turn and report
    /opencode diff                        what changed in the last turn
    /opencode stop                        interrupt the running turn
    Plain language also works: "check ... in <project>".
  ```

Keep the selected project in conversation state. Each project has an
independent stored session.

## Command reference

In-container: `python3 /opt/data/skills/opencode/scripts/oc_api.py <command>`.
The script owns `/opt/data/workspace/opencode_sessions.json` (per project:
`session_id`, `last_anchor`, `agent`, last 10 rotations) with atomic,
lock-guarded writes — never edit it by hand; use `session-get`/`session-set`.

### High-level (stored session, plan agent, automatic polling)

| Command | Effect |
| --- | --- |
| `task <project> [text]` | One full turn: resume/create session, switch agent, send prompt, poll to completion (default 90 s), auto-approve permissions, print conclusion + token line. `--new` rotate first · `--dir D` work-item session for directory D (root-anchored, full-project access) · `--stdin` / `--file F` for long prompts · `--agent A --allow-build` · `--timeout S` · `--no-approve` · `--delivery steer\|queue` |
| `wait <project>` | Block until the stored turn finishes (experimental long-poll, polling fallback). `--timeout S` (300) · `--no-approve` |
| `stop <project>` | Interrupt the stored session's running turn |
| `status [project]` | All-project overview table, or one project's turn status JSON |
| `diff <project>` | Last turn's changes, `+/-` per file; >100-line patches saved under `/opt/data/workspace/diffs/`. `--all` whole session · `--from ID` `--to ID` explicit range |
| `rotate <project>` | Label + title the old session, persist history, create replacement |
| `overview <project>` | Read-only "what is this project" recipes (README, commit momentum, module map) |
| `todos <project>` | Read-only "next tasks" recipes (TODO/ROADMAP/design docs) |
| `selftest <project>` | Endpoint shape smoke test — run after upgrading host OpenCode |
| `handoff <project> [--write]` | Generate the deterministic one-line cron check script for the stored turn; `--write` saves it under `/opt/data/scripts/` and prints the bare filename (for Hermes `cronjob_manage`) |
| `session-get <project> [key]` | Inspect the stored entry |
| `session-set <project> <key> <value>` | Override `session_id` / `last_anchor` / `agent` |
| `approve <project> <request_id> [once\|always\|reject]` | Answer a pending permission |
| `form <project> [<form_id> k=v ...]` | List pending forms / answer one |

### Low-level (explicit session ids)

| Command | Effect |
| --- | --- |
| `server <project>` | Version gate + info |
| `sessions <project>` | Root sessions, newest first (local time) |
| `info <project> <session_id>` | Title/outcome/tokens |
| `label <project> <session_id>` | `'<created time> <topic>'` |
| `new <project>` | Fresh session (asserts project dir) |
| `prompt <project> <session_id> <text>` | Send prompt, print turn anchor |
| `answer <project> <session_id>` | Final assistant prose + tool outputs |
| `pending <project> <session_id>` | Permissions + forms |
| `interrupt <project> <session_id>` | Stop one session's running turn |
| `shell <project> '<command>'` | Read-only location shell (no session) |
| `file <project> <path>` / `ls <project> [path]` | Host file bytes / directory listing |

Importable functions keep their names and signatures (`turn_status(project,
session, anchor)` etc.) — cron scripts may rely on them.

Notes:

- `task`/`wait` stdout carries ONLY the report (conclusion, outcome line with
  elapsed time, token line with cost) — relay it as-is; interim progress
  (approved permissions, rotation notices, warnings) goes to stderr.
- Timestamps use the container's local timezone; without `TZ` set in the
  container they remain UTC — set `TZ` in `config/.env` if the user's chat
  times look offset.
- Prompt text starting with `-` must be passed after a bare `--` separator,
  or via `--stdin`/`--file`.
- `form` answers are submitted as strings for every field type.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | success | report the conclusion + token line |
| 1 | error (API failure, failed/interrupted turn, dead session) | report `error:` line; recover only via documented paths |
| 2 | usage error or policy refusal (e.g. build without `--allow-build`) | fix the invocation |
| 3 | awaiting a human decision (pending form, or permission in `--no-approve` mode) | relay the question to the user verbatim; answer with `form`/`approve` |
| 4 | still running (deadline reached; anchor persisted) | hand off to a cron check or `wait`, tell the user |

## Task lifecycle and long-running work

1. Resolve the project and send the task with `task <project> <text>` (batch
   label/info/new calls are no longer needed — the command does everything).
2. A prompt POST means accepted, not completed; `task` polls for you and
   returns the conclusion with a trailing token line:
   `Session tokens: input 123k · output 45k · cache read 67k`.
3. Exit 4 (still running after the default 90 s): do NOT keep polling in-turn.
   The terminal tool's foreground cap (~420 s observed even when a longer
   timeout was requested) kills the call mid-run, and with it the
   permission-approval duty — hand over to a cron check instead (generate it
   with `handoff <project> --write`, see below). A busy error tells you how
   long the running turn has been going, so the user can decide wait vs stop.
4. Exit 3: relay the pending form or permission decision to the user; answer
   with `form`/`approve`, then re-check with `wait`.

### Cron-check recipe (Hermes `cronjob_manage`)

Generate the check script instead of hand-writing it:

```text
oc_api.py handoff <project> --write     # writes /opt/data/scripts/oc_check_<project>.py
                                        # and prints the bare filename
```

The generated script prints exactly ONE deterministic line — `RUNNING` /
`DONE:<outcome>` / `TIMEOUT` / `ERR:<reason>` — because the `monitor` gate
hashes exact stdout bytes: any timestamp or variation makes every tick look
changed. It also auto-approves pending permissions (`"once"`) — a pending
permission blocks the host agent forever while nobody is polling — and bakes
in a 30-minute TIMEOUT deadline. `ERR:form-pending` means a form awaits a
human answer: relay it to the user, then answer with the `form` command.

Create the job with `script` and `monitor` set to the same bare filename;
identical RUNNING output then suppresses the agent run silently. Set `repeat`
(e.g. 20 ticks) as a backstop. The FIRST tick always wakes the agent as the
baseline, so the job prompt must handle RUNNING explicitly (reply one "still
running" line, keep the job), handle DONE (fetch `answer` + `info` tokens,
verify the host commit via a location shell `git log`/`git status`, report in
the user's language, then remove itself by listing jobs — never by guessing an
ID), and handle TIMEOUT/ERR (report the state, remove itself). Never create
duplicate checks. Generate the script right after exit 4, while the stored
anchor still points at the running turn.

### Progress reporting

For multi-step OpenCode analysis, use two messages:

1. Write a task-specific `progress.md` with the title and planned steps, then
   send it as the progress message. Read or rename an existing file first so a
   stale progress file is not overwritten.
2. Update it once per logical step with `+messages-edit`.
3. Send the final summary separately. If the agent wrote a report, copy it
   under an allowlisted root (`cwd`, `/tmp`, or `~/files`) before attaching it.

On QQ, report progress with the qq-progress skill's recall-as-edit flow instead
of `+messages-edit`; its quota and recall-window pitfalls apply as written
there.

## Rotation

Rotate only after an explicit `/opencode ... new` (or a natural-language
request for a fresh session), or `task --new`. `/new` resets Doro's
conversation and never rotates OpenCode. `rotate` labels and titles the old
session, remembers it in the history (last 10), creates the replacement, and
prints both — announce the old session in the user's language with its local
creation time, topic, and token totals. Rotation affects only the named
project.

## Completion signals (why the helpers look the way they do)

- `GET /api/session/active`: the session is running only when listed.
- An `idle` message after the anchor carries the per-turn `outcome`
  (`succeeded` / `failed` / `interrupted`). The session-level outcome field
  goes stale between turns — never trust it alone.
- The newest assistant message with terminal `finish` (`stop`, `length`,
  `content-filter`, `error`) and `time.completed` is the conclusion.
- Messages must be fetched newest-first (order=desc) and reversed — an
  ascending limited query returns the OLDEST messages and silently misses the
  conclusion. `turn_status()` widens the page (50 → 250 → 1000) until the
  anchor is inside it.

Use `/api/session/<id>/diff` (via the `diff` command) when the user asks what
changed.

## Host files, skills, and analysis

- `file <project> <path>` and `ls` use location-scoped routes; paths must
  resolve inside the project directory. For files outside it, use a shell
  `cat` or an agent prompt. Verify retrieved files are non-empty and parseable
  before reporting success.
- `host_skills()` (GET /api/skill) lists registered host skills with full
  SKILL.md content.
- If the required capability is a host skill (for example, `xmotion-analysis`),
  run that workflow inside the host OpenCode session. Do not export its scripts
  or reimplement its parser locally; this preserves the host skill's version,
  dependencies, and data assumptions.
- For data unavailable to the host, use a writable shared directory: probe
  known mounts (`/mnt/*`, verify with a write/remove test), transfer a packed
  input with md5 checksums on both sides, prompt the host agent to write a
  Markdown report to a stable path, poll the anchored turn, then read the
  report from the share and reuse the same session for follow-ups.

## Permissions, forms, and refusals

- `task`/`wait` auto-approve ordinary in-scope permissions with
  `{"decision":"once"}`. Never approve work outside the configured project
  map; run manual mode (`--no-approve`) when in doubt — it exits 3 with the
  request details.
- Relay each pending form question to the user and submit the exact answer
  under its field key (`form <project> <form_id> key=value ...`). Never
  fabricate form answers.
- If the agent refuses a broad content request, narrow it to a plainly scoped
  read of named files instead of escalating the request.

## Host deployment

Host-side service management is outside the container. The relevant files are:

```text
skills/opencode/scripts/oc-servectl
skills/opencode/systemd/opencode-serve@.service
```

On the host, first-time setup is:

```bash
mkdir -p ~/.config/opencode-serve && chmod 700 ~/.config/opencode-serve
echo 'OPENCODE_SERVER_PASSWORD=<random-long-secret>' > ~/.config/opencode-serve/auth.env
chmod 600 ~/.config/opencode-serve/auth.env
loginctl enable-linger "$USER"
./skills/opencode/scripts/oc-servectl sync
./skills/opencode/scripts/oc-servectl status
```

`sync` installs the unit template (hardened with `NoNewPrivileges`,
`PrivateTmp`, `ProtectSystem=full`), reconciles per-project instances, and —
when the template changed — restarts the affected instances automatically.
Keep the same password in `config/.env` before starting the container. `sync`
requires a host OpenCode binary at version 2.0 or newer. The container cannot
run systemd or restart host services; instruct the user to run the commands
and inspect the matching `opencode-serve@<project>.service` logs.

## Final verification

Before claiming success, verify:

- `/api/info` returned JSON with a 2.x version (selftest check 1 covers this);
- the project and session used are identified;
- every modified or generated file is accounted for;
- tests or checks are reported with their actual results;
- no credential, API key, or unverified host visibility is included.

Remember: the prompt response is only an acceptance anchor, the idle marker is
the per-turn outcome, location-shell output must be read from its output route,
and host work must remain on the host side.
