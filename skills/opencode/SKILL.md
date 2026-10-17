---
name: opencode
description: "Control configured host OpenCode projects from Lark: send coding tasks, run repository queries over HTTP, retrieve host files, and monitor long-running work. Use whenever the user mentions OpenCode, a project listed in /opt/data/opencode_projects.yaml, asks to delegate work to the host coding agent, or wants history, activity, files, or analysis from a host-side project—even without explicitly saying 'OpenCode'."
version: 1.0.0
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

## Non-negotiable rules

- Use only projects listed in `/opt/data/opencode_projects.yaml`; never invent a
  project name, directory, port, or endpoint.
- Use the V2 API only. Verify `GET /api/info` returns JSON with a 2.x version
  before other API calls. A `200 text/html` response is the web SPA, not a
  healthy API. Do not fall back to V1 paths.
- Prefer `scripts/oc_api.py` for authentication, version gating, response
  unwrapping, polling, file reads, and shell commands.
- The API endpoint is `http://host.docker.internal:<port>` with Basic Auth
  username `opencode` and `$OPENCODE_SERVER_PASSWORD`. Never expose the
  password or API keys.
- Run host git, file, build, and analysis commands through OpenCode. Do not
  claim host-container visibility without an actual mount or control socket.
- Preserve the user's intent, project, and scope. Do not silently broaden work.
- User-facing replies, progress updates, reports, and help text must be in the
  user's language; templates and examples in this file remain English.

## Project resolution

Read `/opt/data/opencode_projects.yaml` before the first API call. It contains a
`default` project and a `projects` map with `dir` and `port`.

- Use `default` when no project is named.
- Natural language such as “check X in my_project” selects that project.
- `/opencode project` lists projects and the current selection.
- `/opencode project <name>` selects a project.
- `/opencode project <name> new` rotates that project's session.
- `/opencode new` rotates the current project, or the configured default.
- `/opencode <task>` sends a task to the selected project.
- A bare `/opencode` or `/opencode help` returns only:

  ```text
  OpenCode remote control:
    /opencode project                     list projects and current selection
    /opencode project <name>              switch project
    /opencode project <name> new          rotate that project's session
    /opencode new                         rotate the current project's session
    /opencode <natural-language task>     send the task to the current project
    Plain language also works: "check ... in <project>".
  ```

Keep the selected project in conversation state. Each project has an
independent stored session.

## Session lifecycle

Persist sessions in `/opt/data/workspace/opencode_sessions.json`, keyed by
project name. Create the file when absent, keep valid JSON, and update it
atomically when practical. Read the file immediately before rewriting it in
the same session — the file-write tool refuses a write based on a stale
copy read in an earlier turn, so a fresh read right before the write is
required, not optional.

### Reuse and recovery

Reuse the stored session for every request, including unrelated tasks. Create a
replacement only when the stored ID returns 404; this is recovery, not
rotation. Do not create duplicate sessions or cron checks.

Create a session with:

```json
{"location":{"directory":"<project dir>"}}
```

The `location` wrapper is required. Confirm the returned location matches the
configured project directory.

Interrupt with `POST /api/session/<id>/interrupt`. The session remains usable;
do not replace it after interruption.

### Explicit rotation

Rotate only after an explicit `/opencode ... new` command or a natural-language
request for a fresh session. `/new` resets Doro's conversation and never
rotates OpenCode.

Before switching (run `oc_api.py` with no arguments to print the full CLI):

1. Get the old session's label and tokens: `oc_api.py label <project> <session_id>`
   (prints '<created time> <topic>') and `oc_api.py info <project> <session_id>`
   (adds outcome and token totals).
2. If the title is missing, derive a short topic from the earliest user
   message. An unused session is labeled `empty session (never used)`.
3. Announce the old session in the user's language, including its local time,
   topic, and token totals (input/output and non-zero cache read).
4. Create the replacement with `oc_api.py new <project>` (asserts the project
   dir and prints the new session id), then persist its ID and prepend the old
   entry to that project's history, keeping roughly the latest ten entries.

Batch the label, info, and new-session calls in one Python invocation that
imports `oc_api` (`session_label` + `session_report` + `new_session`) instead of
three separate CLI round-trips.

Rotation affects only the named project; subsequent requests use the new
session.

## Fast repository queries

For read-only git, grep, stat, or file inspection, use the location shell rather
than an agent turn. It does not require a session and does not pollute context:

```text
POST /api/shell?location[directory]=<dir>  {"command":"..."}
GET  /api/shell/<id>
GET  /api/shell/<id>/output
DELETE /api/shell/<id>
```

`oc_api.py shell <project> '<command>'` performs this lifecycle.

- Send one single-line command joined with `;`; split multi-line scripts.
- Quote literal separator-like text such as `echo '=== section'` for zsh.
- Keep shell work read-only. Stateful or reasoning-heavy work belongs in an
  agent prompt.
- For activity summaries use `git log --since=<date> --all`, then per-author
  `--stat` or `--numstat`; plain `HEAD` can miss divergent branches.
- For "what is this project doing" questions, answer with 2-3 location-shell
  calls instead of a session turn: `head -c 6000 README.md` (the project's own
  story), `git log --since='14 days ago' --all --oneline | head -30; git log -1
  --format='%ci %s'` (momentum plus last-touch date), and a module map plus a
  commit-topic histogram (`find <src dirs> -maxdepth 2 -type d | sort | head -40`;
  `git log --since=<date> --all --oneline | sed -E 's/^[a-f0-9]+ //; s/\(.*\)//'
  | awk '{print $1}' | sort | uniq -c | sort -rn`) to name where recent work
  concentrated. Summarize conclusion-first: what it is, its architecture, then
  recent activity. Escape the sed/awk regex for the outer shell like the TODO
  recipe below.
- For "next tasks / TODO" questions, answer with 2-3 location-shell
  calls instead of a session turn: `find . -name '<doc>*' -not -path './.git/*'`
  to locate the design doc, then
  `grep -n -E '^\s*- \[ \]|TODO|next steps' <doc>` to list open checkboxes
  with line numbers, then `sed -n '<range>p' <doc>` for the surrounding
  section context (batch vs later-todos). Escape the regex for the outer
  shell when sending it through the terminal tool (`\\s`, `\\[`).

## Send and monitor an agent task

1. Resolve the project from the YAML map and check `/api/info`.
2. Reuse or recover the project's stored session.
3. Send `POST /api/session/<id>/prompt` with `{"text":"<faithful request>"}`.
4. Treat the response ID as this turn's anchor. A successful POST means
   accepted, not completed.

If the POST times out, poll before retrying because the request may have been
delivered. A 409 means the session is busy; wait, verify, or interrupt rather
than retrying in a loop.

### Completion

For messages after the anchor, combine these signals:

- `GET /api/session/active`: the session is running only when listed.
- An `idle` message: authoritative per-turn `outcome` is
  `succeeded`, `failed`, or `interrupted`.
- The newest assistant message with terminal `finish` and `time.completed`:
  `stop`, `length`, `content-filter`, or `error`.

Fetch messages newest-first, reverse them, then slice from the anchor. Do not
use the stale session-level outcome or an ascending limited query. Extract
assistant prose from `content[]` items of type `text`; extract completed tool
results from type `tool` at `state.content[].text`. `oc_api.py turn_status()`
and `answer` implement this logic.

After confirmed completion, read session tokens and append one trailing line to
the final reply, translated into the user's language:

```text
Session tokens: input 123k · output 45k · cache read 67k
```

Report model names only; `/api/config` may contain provider credentials.
Use `/api/session/<id>/diff` when the user asks what changed.

## Host files and skills

Use location-scoped filesystem routes for ordinary reads:

- `GET /api/fs/read/<path>?location[directory]=<dir>` returns raw bytes.
- `GET /api/fs/list?location[directory]=<dir>&path=.` lists relative entries.
- `GET /api/skill?location[directory]=<dir>` returns registered skills and
  their full `SKILL.md` content.

Paths must resolve inside the project directory. For files outside it, use a
location-shell `cat` or an agent prompt. Verify retrieved files are non-empty
and parseable before reporting success. For bulk or binary transfers, prefer a
tarball in a shared directory with checksums on both sides.

## Host-side analysis

If the required capability is a host skill (for example, `xmotion-analysis`),
run that workflow inside the host OpenCode session. Do not export its scripts
or reimplement its parser locally; this preserves the host skill's version,
dependencies, and data assumptions.

For data unavailable to the host, use a writable shared directory:

1. Probe known mounts such as `/mnt/*`; verify listing and a write/remove test.
2. Pack the input, transfer it, and verify an md5 checksum in both environments.
3. Prompt the host agent to unpack, run the standard skill workflow, and write
   a Markdown report to a stable path in the share.
4. Poll the anchored turn, then read the report directly from the share.
5. Reuse the same session for follow-up questions and ask the agent to append a
   named section to the same report.

Non-standard record layouts can be passed as context for the host skill to
inspect. A quick count or channel scan is acceptable, but do not hand-roll a
full binary parser locally.

## Permissions, forms, and refusals

While polling, check `/api/session/<id>/permission`. In autonomous mode, approve
ordinary in-scope requests with:

```json
{"decision":"once"}
```

Allowed decisions are `once`, `always`, and `reject`. Never approve work outside
the configured project map. Mention pending or rejected permissions in the
final report.

For `/api/session/<id>/form`, relay each typed question to the user and submit
the exact answer under its field key. Never fabricate form answers.

If the agent refuses a broad content request, narrow it to a plainly scoped
read of named files instead of escalating the request.

## Lark reporting and long-running work

For multi-step OpenCode analysis, use two messages:

1. Write a task-specific `progress.md` with the title and planned steps, then
   send it as the progress message. Read or rename an existing file first so a
   stale progress file is not overwritten.
2. Update it once per logical step with `+messages-edit`.
3. Send the final summary separately. If the agent wrote a report, copy it
   under an allowlisted root (`cwd`, `/tmp`, or `~/files`) before attaching it.

Poll for about 90 seconds in the current turn. If still running, tell the user
and register one temporary Hermes cron check about every two minutes, with a
30-minute default timeout. Each check must target only the selected project,
use the anchored status, deliver a terminal result to the originating
conversation, and remove itself afterward. Never create duplicate checks.

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

Keep the same password in `config/.env` before starting the container. `sync`
requires a host OpenCode binary at version 2.0 or newer. The container cannot
run systemd or restart host services; instruct the user to run the commands and
inspect the matching `opencode-serve@<project>.service` logs.

## Final verification

Before claiming success, verify:

- `/api/info` returned JSON with a 2.x version;
- the project and session used are identified;
- every modified or generated file is accounted for;
- tests or checks are reported with their actual results;
- no credential, API key, or unverified host visibility is included.

Remember: the prompt response is only an acceptance anchor, the idle marker is
the per-turn outcome, location-shell output must be read from its output route,
and host work must remain on the host side.
