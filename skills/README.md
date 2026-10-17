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
| `skill-creator`   | Upstream skill-creator: workflow and eval tooling for building/improving skills |

---

## opencode

OpenCode bridge: lets Doro drive whitelisted host projects through host-side
OpenCode 2.x services — send coding tasks, run read-only repository queries,
read host files, and monitor long-running work.

### Layout

| File                                        | Purpose                                                                                          |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `opencode/SKILL.md`                         | Agent-facing protocol: V2 API rules, session lifecycle, permission handling, reporting rules      |
| `opencode/scripts/oc_api.py`                | In-container API client: auth, version gating, response unwrapping, polling, file and shell ops   |
| `opencode/scripts/oc-servectl`              | Host-side service management: `sync` installs the unit and reconciles instances, `status` shows state |
| `opencode/systemd/opencode-serve@.service`  | Unit template: one `systemd --user` instance per project                                         |

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
container cannot run systemd or restart host services; inspect the matching
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
| `/opencode <natural-language task>` | Send the task to the current project |

Session rules:

- Doro keeps one persistent OpenCode session per project and reuses it across
  tasks; a stored session ID is recreated automatically only on a 404 (that is
  recovery, not rotation).
- Rotate explicitly with `/opencode new` / `/opencode project <name> new`
  (natural language like "start a fresh session for xxx" also works). On
  rotation, the old session's creation time, topic, and cumulative tokens are
  announced.
- Task replies end with the current session's cumulative tokens.
- `/new` resets Doro's own conversation and never rotates OpenCode sessions.

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
