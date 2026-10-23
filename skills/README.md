# skills/

Public skill layer: one skill per subdirectory. Each `SKILL.md` is the
agent-facing protocol; this README is the maintainer-facing deployment and
usage documentation.

- Subdirectories are auto-discovered by `run.sh` and bind-mounted read-write
  one by one (`skills/<name>/` → `/opt/data/skills/<name>/`), single copy:
  agent edits land directly in the repo working tree.
- Adding or removing a skill requires `./run.sh` to rebuild the container;
  a plain `docker restart` does not refresh the mount list.
- For the three-layer model (bundled / public / private) and the review
  workflow, see "Skills three-layer model" in the repo root README.

Current public skills:

| Skill             | Purpose                                                      |
| ----------------- | ------------------------------------------------------------ |
| `opencode`        | OpenCode bridge: drive whitelisted host projects             |
| `lark-progress`   | Two-message reporting protocol for multi-step Lark tasks     |
| `meego`           | Feishu Project (Meego/Meegle) operations: loader for the upstream meegle-cli skill; CLI and skill baked into the image |
| `lark-suite`      | Pointer to the 28 domain skills embedded in the lark-cli binary |
| `qq-progress`     | Recall-as-edit reporting protocol for multi-step QQ tasks    |
| `skill-creator`   | Upstream skill-creator: workflow and eval tooling for building/improving skills |

---

## opencode

OpenCode bridge: lets Doro drive whitelisted host projects through host-side
OpenCode 2.x services — send coding tasks, run read-only repository queries,
read host files, and monitor long-running work.

### Layout

| File                                        | Purpose                                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `opencode/SKILL.md`                         | Agent-facing command reference: CLI surface, exit codes, plan/build policy, cron handover recipe |
| `opencode/scripts/oc_api.py`                | In-container API client + orchestrator: session store, task/wait/diff/rotate commands, gating, polling, shell/file ops |
| `opencode/scripts/oc-servectl`              | Host-side service management: `sync` installs the unit, reconciles instances, and restarts them when the template changed; `status` shows state |
| `opencode/systemd/opencode-serve@.service`  | Unit template: one `systemd --user` instance per project (hardened: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`) |

### Requirements

- Host OpenCode **>= 2.0**: the bridge speaks only the V2 server API
  (`/api/...`). V1 is deprecated and silently degrades on V2. `oc-servectl
  sync` version-gates `~/.opencode/bin/opencode` and fails outright if too
  old.
- All serve instances of the same host user share one SQLite database
  (`~/.local/share/opencode/opencode.db`); treat that file as the source of
  truth for backup/migration.

### First-time host setup

```bash
# 1) Auth file
mkdir -p ~/.config/opencode-serve && chmod 700 ~/.config/opencode-serve
echo 'OPENCODE_SERVER_PASSWORD=<random-long-secret>' > ~/.config/opencode-serve/auth.env
chmod 600 ~/.config/opencode-serve/auth.env

# 2) Enable linger (user services start at boot)
loginctl enable-linger "$USER"

# 3) Register projects in config/opencode_projects.yaml, then from the repo root:
./skills/opencode/scripts/oc-servectl sync
./skills/opencode/scripts/oc-servectl status
```

The project list path can be overridden with `OPENCODE_PROJECTS_CONFIG`. The
unit template is sandboxed (`NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`); when the template changes, `sync` detects the
difference and restarts the affected instances automatically. The container
cannot run systemd or restart host services; inspect the matching
`opencode-serve@<project>.service` logs for service issues.

### Container-side configuration

Put the same password into `OPENCODE_SERVER_PASSWORD` in `config/.env`, then
`./run.sh`.

### Chat commands and usage

In Lark/QQ you can simply say "check xxx in my_project", or use commands:

| Command                        | Effect                        |
| ------------------------------ | ----------------------------- |
| `/opencode`                    | Show help                     |
| `/opencode project`            | List projects and current one |
| `/opencode project <name>`     | Switch project                |
| `/opencode project <name> new` | Rotate that project's session |
| `/opencode new`                | Rotate current project's session |
| `/opencode new <task>`         | Rotate, then send the task    |
| `/opencode <natural-language task>` | Send the task to the current project |
| `/opencode status`             | Per-project session overview  |
| `/opencode follow`             | Wait for the running turn and report |
| `/opencode diff`               | What changed in the last turn |
| `/opencode stop`               | Interrupt the running turn    |

Session rules:

- Doro keeps one persistent OpenCode session per project, stored in
  `/opt/data/workspace/opencode_sessions.json` (managed entirely by
  `oc_api.py` — atomic, lock-guarded writes; last 10 rotations remembered).
  A stored session ID is recreated automatically only on a 404 (that is
  recovery, not rotation).
- Sessions run the read-only **plan** agent by default; the write-capable
  **build** agent requires explicit user consent plus `--agent build
  --allow-build` (see the skill).
- Rotate explicitly with `/opencode new` / `/opencode project <name> new`
  (natural language like "start a fresh session for xxx" also works). On
  rotation, the old session's creation time, topic, and cumulative tokens are
  announced.
- Task replies end with the current session's cumulative tokens.
- `/new` resets Doro's own conversation and never rotates OpenCode sessions.

### CLI quick reference (in-container)

`python3 /opt/data/skills/opencode/scripts/oc_api.py <command> ...`

- High-level: `task` `wait` `stop` `status` `diff` `rotate` `overview`
  `todos` `selftest` `handoff` `session-get`/`session-set` `approve` `form`
- Low-level: `server` `sessions` `info` `label` `new` `prompt` `answer`
  `pending` `interrupt` `shell` `file` `ls`

Exit codes: `0` success · `1` error · `2` usage/policy refusal · `3` awaiting
a human decision (pending form/permission) · `4` still running (anchor
persisted; hand off via `handoff <project> --write` + a cron check, or
`wait`). `task`/`wait` stdout is directly relay-able (conclusion + outcome
with elapsed + tokens with cost); progress goes to stderr. After upgrading
host OpenCode, run `selftest <project>` to smoke-test endpoint shapes.
Timestamps use the container timezone; set `TZ` in `config/.env` if chat
times look offset.

### Security boundary

This feature grants Doro read/write file access and command execution inside
whitelisted project directories. Protections: user allowlist, OpenCode Basic
Auth, Docker-bridge-only listening (172.17.0.1), Hermes approval policy. Do
not add users you do not trust to the allowlist.

---

## lark-progress

Constrains reporting for genuinely multi-step Lark tasks: the conversation
contains exactly two messages, avoiding fragment spam.

1. **Progress message**: one message created and continuously edited by the
   agent; each completed logical step appends "what was done + elapsed time",
   and `Blockers` and `Learnings` are appended before finishing.
2. **Final answer**: a normal reply organized as conclusion, evidence,
   verification, and next actions.

Short exchanges (greetings, one-question answers, one- or two-step lookups,
immediately answerable requests) get no progress message.

- Trigger: any task expected to require **three or more real tool steps**,
  even if the user never mentions "progress". Trigger rules live in
  `config/SOUL.md`; the full agent-facing protocol (lark-cli commands, update
  cadence, failure recovery) is in `lark-progress/SKILL.md`.
- Related config (`config/config.yaml`, template in
  `config/config.yaml.example`): `display.tool_progress: log` keeps automatic
  tool progress in the container log instead of chat;
  `display.platforms.feishu.interim_assistant_messages: false` disables
  interim fragment messages.
- Progress files live at `/opt/data/workspace/` inside the container
  (`data/workspace/` on the host). After changing real config, persona files,
  or skills, run `./run.sh` to refresh mounted content.

---

## meego

Feishu Project (Meego/Meegle) operations: query/manage work items, workflow
nodes, views, personal todos, schedule stats, MQL search. Unlike the other
public skills, `meego/SKILL.md` is only a **loader** — the full upstream
skill (SKILL.md + `references/`) from
[meegle-cli](https://github.com/larksuite/meegle-cli) ships **baked into
the image** (read-only at `/opt/hermes/skill/meegle/`, built from the
same-version upstream tag) and the repo never tracks upstream content.

- Both the `meegle` CLI binary and its skill content are baked into the
  image by the `Dockerfile`: the binary comes from the npm tarball
  (`MEEGLE_CLI_SHA256`), the skill from the same-version GitHub tag
  tarball (`MEEGLE_SKILL_SHA256`). They are version-locked together —
  the loader's runtime bootstrap normally never downloads anything.
- Upgrade: bump `MEEGLE_CLI_VERSION` plus both checksums and rebuild.
  Get `MEEGLE_SKILL_SHA256` via
  `curl -fsSL <codeload tag url> | sha256sum`; if GitHub regenerates
  archives and only that check fails, refresh just it.
- Fallback: if the baked copy is missing or version-mismatched (e.g. the
  image was not rebuilt), the loader downloads the matching tag into the
  persistent cache `data/skill/meegle/` (`/opt/data/skill/meegle/`
  in-container), stamped with `.version`, with atomic swap on bootstrap.
- The container has no browser: the upstream OAuth step runs as the
  device-code flow (`meegle auth login --device-code`, init/poll split — see
  the loader's adaptation section).
- Manual preseed (now only needed for the fallback path on
  GitHub-unreachable hosts): extract the upstream tag tarball into
  `data/skill/meegle/` on the host and write the CLI version into
  `data/skill/meegle/.version`.
- `MEEGLE_NO_UPDATE_CHECK=1` and `MEEGLE_TELEMETRY=disabled` are set in the
  image (update prompt off; failed-command diagnostics reporting off).

---

## lark-suite

Pointer/index skill for the **28 domain skills embedded in the lark-cli
binary** (im, doc, sheets, base, calendar, mail, task, okr, approval,
attendance, contact, wiki, drive, markdown, meeting, whiteboard, slides,
apps, event, openapi-explorer, skill-maker, shared, workflow-* and
aliases). The embedded skills are read on demand via
`lark-cli skills list / read` — always in sync with the CLI version, no
files on disk, no download.

- `lark-suite/SKILL.md` carries a compact domain catalog (28 rows) plus
  the read commands and the auth prerequisite (`lark-shared` first).
  **The table is manually maintained**: after bumping `LARK_CLI_VERSION`,
  refresh it from `docker exec hermes-doro lark-cli skills list` output if the
  set changed.
- Caveat documented in the skill (verified against the embedded binary):
  the only real gap is `lark-apps` — its SKILL.md references
  `creative-design/creative-design.md` for creative-design flows, and that
  directory is not embedded (upstream excludes it wholesale because it
  carries machine resources). `lark-sheets` `scripts/` and `lark-whiteboard`
  `elements/` are also unembedded but unreferenced by the protocol (no
  functional impact); `lark-meeting` scenes are markdown docs and ARE
  embedded.
- Related: `lark-progress` (reporting protocol used alongside any domain
  skill), `meego` (Feishu Project — a separate CLI, do not conflate).

---

## qq-progress

QQ counterpart of `lark-progress` for genuinely multi-step tasks. QQ has no
message-edit API, so the two-message protocol becomes **recall + resend**:
at most one progress message is visible at any time; each update recalls the
previous message and resends the full accumulated log. Message layout (step
lines, status line, closing sections, final-reply template) mirrors
`lark-progress` and is restated in full — the skill stands alone and does
not require loading `lark-progress`. Full protocol, quotas, and pitfalls are
in `qq-progress/SKILL.md`.

- QQ official-bot quotas drive the design: passive replies (with msg_id)
  are capped at 5 per inbound message, a budget shared between interim
  progress and the final reply; active messages (4 per user per month) are
  never spent on progress. Progress sends use `msg_seq` 80+ so they never
  collide with gateway sequence numbers.
- `qq-progress/scripts/qq_api.py` (stdlib-only) handles token fetch (with
  dud-token retry), passive send, and recall. It reads `QQ_APP_ID`,
  `QQ_CLIENT_SECRET`, and `QQBOT_HOME_CHANNEL` from `config/.env`
  (`/opt/data/.env` in-container) and picks the latest inbound `msg_id`
  from `state.db` — there is no other history source.
- Same trigger boundary as `lark-progress`: no progress message for
  greetings, single lookups, or immediately answerable requests.

---

## skill-creator

A copy of the upstream skill-creator (Apache-2.0, see
`skill-creator/LICENSE.txt`) with Hermes `skill_manage` tool constraints
prepended (description ≤ 60 chars, batch-op atomicity, etc.). Used to create
new skills, improve existing ones, run evals and benchmarks, and optimize
trigger descriptions.

- `config/SOUL.md` requires the agent to load this skill and follow its
  workflow before creating or modifying any skill (including `skill_manage`
  create/patch operations and direct file edits), except for pure typo fixes;
  self-initiated improvements are equally covered.
- Skills the agent creates or iterates on land in `data/skills/` by default;
  on first publication they are reviewed and moved into the repo `skills/`
  (see "Skills three-layer model" in the root README).

| Path                          | Purpose                                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| `skill-creator/SKILL.md`      | Full agent-facing workflow (interview → draft → eval → iterate)   |
| `skill-creator/scripts/`      | Eval, benchmark, description-improvement, packaging scripts      |
| `skill-creator/eval-viewer/`  | Generates review pages for eval results                          |
| `skill-creator/agents/`       | Sub-agent definitions used by evals                              |
| `skill-creator/references/`   | Reference docs such as skill structure                            |
| `skill-creator/assets/`       | Static assets                                                    |
