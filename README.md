# hermes-doro

**English** | [简体中文](docs/README.zh-CN.md)

Doro — a private AI agent running inside a hardened Docker container, built on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Online on both Lark and QQ; the model goes through any OpenAI-compatible endpoint; can optionally bridge host OpenCode services to drive whitelisted projects.

## Architecture

```
Lark / QQ (outbound WebSocket, no public ingress)
   │
   ▼
┌──────────────────── Docker container hermes ─────────────────────┐
│  Hermes Agent gateway (s6, privilege-dropped)                    │
│  - Lark messaging (lark-oapi / websockets) + QQ open platform    │
│  - TTS (edge-tts) / lark-cli skills                              │
│  - Write tools confined to /opt/data (HERMES_WRITE_SAFE_ROOT)    │
└──────┬─────────────────────────────────────┬─────────────────────┘
       │ bind mount: data/ ↔ /opt/data      │ bind mount: /mnt/hermes-doro
       │ + config/ (four files) + skills/*  │
       ▼                                     ▼
 host data/ (runtime) / config/ (real)   host /mnt/hermes-doro (file exchange)
 / skills/ (public) / skills_private/ (private) / cron/scripts (cron assets)
       │
       │ Basic Auth (HTTP, Docker bridge 172.17.0.1 only)
       ▼
┌─────────────── host OpenCode user services ────────────────────┐
│  opencode-serve@<project>.service (one per project, systemd --user) │
└──────────────────────────────────────────────────────────────────┘
```

## Security model

The container boundary is the host's moat.

| Guarantee                 | Mechanism                                                                                                                                                             |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reduce host risk          | `--cap-drop ALL` (only the 5 caps needed for startup and privilege dropping added back), `no-new-privileges`; s6 drops privileges inside the container                 |
| No Docker socket mount    | Never `-v /var/run/docker.sock`, no `--privileged`                                                                                                                     |
| Bounded write surface     | Code comes from the image; `data/` keeps runtime state; the four real config files in `config/` are mounted read-write but stay out of git; public `skills/<name>/` are mounted read-write and git-tracked; `cron/scripts/` (scheduler monitor scripts) is mounted read-write and gitignored; business files are exchanged through the shared directory (host `SHARED_DATA_DIR`, default `/mnt/hermes-doro`; mounted at the same path in the container) |
| Tool writes confined      | `HERMES_WRITE_SAFE_ROOT=/opt/data`; write tools can only act inside the data volume                                                                                     |
| No public ingress         | Lark/QQ use outbound WebSocket; no `-p` ports exposed; dashboard disabled                                                                                               |
| Destructive cmds reviewed | `approvals.mode: smart` + user allowlist                                                                                                                               |
| Resources capped          | Memory 4G / CPU 2 / pids 512 limits                                                                                                                                    |

Residual risk: the in-container terminal backend can still read files inside the same container, `/opt/data`, and whatever is reachable under `/mnt/hermes-doro` (including `config/.env` mounted at `/opt/data/.env`). Do not treat this container as a fully unreadable key vault.

## Skills three-layer model

The agent's skills come in three layers with clear read/write boundaries:

| Layer           | In-container path            | Access                                | Source & maintenance                                                                                           |
| --------------- | ---------------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Bundled         | `/opt/hermes/skills/`        | Read-only (root-owned, agent can't write) | Baked into the image, upgraded via `./build.sh --pull`; injected read-only into the index via `skills.external_dirs` |
| Learned · public | `/opt/data/skills/<name>/`  | Read-write                            | Repo `skills/<name>/` bind-mounted per directory, single copy; agent edits land directly in the working tree, humans review then commit |
| Learned · private | `/opt/data/skills_private/` | Read-write                          | Repo `skills_private/` (gitignored, only the directory README is tracked; may hold several private repo clones) bind-mounted, auto-mounted by default |

- The bundled layer is not copied into the writable area: `hermes skills opt-out --remove` (marker at `data/.no-bundled-skills`, persistent via bind mount).
- Skills the agent creates (`skill_manage`, `/learn`, curator) land by default in unmounted directories under `/opt/data/skills/` — still untracked runtime content. To publish one for the first time, stop the container, review, move the directory into this repo's `skills/<name>/`, then run `./run.sh`. Later iterations of an already-published skill flow straight into the repo, no copying needed.
- Private skills: private repos live under `skills_private/` (must be real clones); mounting mechanics, `git clean` caveats, and the `PRIVATE_SKILLS_SRC` override are documented in that directory's README.

Public skills use a flat top-level layout: each `skills/<name>/SKILL.md` is the agent-facing protocol, and `skills/README.md` is the maintainer-facing deployment and usage documentation. `run.sh` auto-discovers them and mounts each directory read-write. Adding or removing a public skill requires `./run.sh` to rebuild the container; a plain `docker restart` does not refresh the mount list.

Current public skills: `opencode` (OpenCode bridge), `lark-progress` (Lark multi-step task reporting), `meego` (Feishu Project/Meegle operations; loader skill, CLI and skill baked into the image), `lark-suite` (pointer to the 28 domain skills embedded in lark-cli), `qq-progress` (QQ multi-step task reporting via recall + resend), `skill-creator` (workflow for creating/improving skills) — see [`skills/README.md`](skills/README.md).

Review the agent's iterations and commit selected files:

```bash
git status --short -- skills/
git diff -- skills/
```

Only commit public skill docs and necessary assets; `__pycache__/` is ignored. The repo's `.git` is never mounted into the container. `config/SOUL.md` requires the agent to load `skill-creator` before creating or modifying any skill, except for pure typo fixes.

Cron job assets: monitor scripts for the in-container scheduler live in `cron/scripts/`, bind-mounted read-write at `/opt/data/scripts` (the agent writes and iterates them there; the mount is skipped when the directory is missing). Job definitions (`cron/jobs.yaml`) stay local — only `cron/README.md` is tracked. See [`cron/README.md`](cron/README.md).

## Quick start

```bash
# 0) Prepare the shared directory (write files under a temp name first, then
#    rename, so the other side never reads a half-written file). Default
#    /mnt/hermes-doro; override with an env var:
#    SHARED_DATA_DIR=/path/to/dir ./run.sh (always mounted at /mnt/hermes-doro in the container)
sudo mkdir -p /mnt/hermes-doro

# 1) First run: missing real configs are auto-generated from the examples,
#    then the script prompts you to edit them and exits
./run.sh
$EDITOR config/.env                # model API key + Lark/QQ credentials and user allowlists
$EDITOR config/config.yaml         # real model ID and endpoint (api_mode if needed)
$EDITOR config/opencode_projects.yaml  # host project paths (when using the OpenCode bridge)
# Optionally edit config/SOUL.md; existing real files are never overwritten by the bootstrap flow

# 2) Build and start (public skills are auto-mounted; rebuild after changing the Dockerfile)
./build.sh && ./run.sh

# 3) Confirm connectivity
docker logs -f hermes-doro
# [Lark] connected to wss://... means the link is up — DM the bot to test
```

To get your own Lark `open_id`: temporarily add `GATEWAY_ALLOW_ALL_USERS=true` to `config/.env` and comment out `FEISHU_ALLOWED_USERS`, restart, send the bot a message, then:

```bash
docker exec hermes-doro grep -o 'ou_[a-z0-9]*' /opt/data/sessions/sessions.json
```

Fill the allowlist back in, remove the allow-all line, and run `./run.sh` again.

### QQ bot (official open platform)

1. Register a bot at [q.qq.com](https://q.qq.com), get the AppID / Secret, and fill in `QQ_APP_ID` / `QQ_CLIENT_SECRET` in `config/.env`
2. `platforms.qq` in `config/config.yaml` ships with an allowlist skeleton: temporarily add `allow_all_users: true` under `extra`, restart, DM the bot one message, then run `docker exec hermes-doro cat /opt/data/sessions/sessions.json | grep -o 'ou_[a-z0-9]*'` to get your openid; fill in `allow_from`, remove `allow_all_users`, and restart. For group chats set `group_policy: allowlist` as needed (groups require an @mention)
3. Production use (out of the sandbox) may require a business entity

## Model configuration

`config/config.yaml` is filled in for the real service (template: `config/config.yaml.example`); after changing it, `./run.sh` rebuilds the container to apply:

- `model.*`: primary model (any OpenAI-compatible endpoint; model names follow what `/v1/models` returns; endpoints that only expose `/chat/completions` need `api_mode: chat_completions`)
- `fallback_providers`: fallback model chain, switched automatically on rate-limit/5xx/disconnect; keys live in `config/.env` (`key_env`), never in this file
- `context_length` set to the model's real window; auto-compaction triggers around 75% of the threshold

## OpenCode bridge (optional: let Doro drive host projects)

Provided and maintained by `skills/opencode/`. Requires host OpenCode **>= 2.0** (V2 server API only; V1 is deprecated): register projects in `config/opencode_projects.yaml`, deploy the systemd user services on the host side with `oc-servectl`, put the same password into `config/.env`, then `./run.sh`. Deployment steps, chat commands (`/opencode ...`), session rules, and security boundaries: see [`skills/README.md`](skills/README.md#opencode).

## Lark task reporting

Multi-step tasks use two messages: a progress message created and continuously edited by the agent (per step "what + duration", ending with `Blockers` and `Learnings`), plus a final answer organized into conclusion, evidence, verification, and next actions; short Q&A does not create a progress message. Protocol details, trigger rules, and related display config: see [`skills/README.md`](skills/README.md#lark-progress).

## Directory layout

```
hermes-doro/
├── Dockerfile          # based on the official image; preinstalls the Lark SDK, edge-tts, lark-cli and the baked meegle skill
├── build.sh            # build the image (--pull force-refreshes the base image)
├── run.sh              # launch script and runtime limits (run ./build.sh first)
├── update.sh           # daily update SOP (optional --rebuild-image)
├── cron/               # container-cron assets (gitignored except README; scripts/ mounted rw at /opt/data/scripts)
├── skills/             # public skills, git-tracked; mounted read-write per directory, single copy
│   ├── README.md       # per-skill deployment and usage docs (maintainer-facing)
│   ├── lark-progress/  # Lark multi-step task reporting protocol
│   ├── meego/          # Feishu Project (Meego/Meegle) loader skill (CLI + skill baked into the image)
│   ├── lark-suite/     # pointer to the 28 domain skills embedded in lark-cli
│   ├── opencode/       # OpenCode bridge (incl. oc-servectl and the systemd unit template)
│   ├── qq-progress/    # QQ multi-step task reporting protocol (recall + resend)
│   └── skill-creator/  # upstream skill-creator and its eval tooling
├── skills_private/     # private skills (gitignored; only the README is tracked)
├── docs/               # translated documentation (Chinese README)
├── .dockerignore       # ignores everything (Dockerfile has no COPY; build context is empty)
├── .gitignore          # data/ fully ignored; config/ tracks only the 4 examples; cron/ tracks only its README
├── data/               # runtime state (mounted at /opt/data, fully ignored)
└── config/             # only the 4 example templates are tracked; real configs stay local
    ├── .env.example / .env
    ├── config.yaml.example / config.yaml
    ├── opencode_projects.yaml.example / opencode_projects.yaml
    └── SOUL.md.example / SOUL.md
```

`data/` is fully ignored and holds `state.db`, `sessions/`, `logs/`, runtime auth state, and similar content. The four real files in `config/` are bind-mounted read-write to the same-named paths under `/opt/data/`. Docker may leave same-named zero-byte mount placeholder files under `data/`; the files in `config/` are authoritative. Real configs and the persona never appear in `git diff` — inspect them directly or diff against the templates.

## Day-to-day operations

```bash
docker logs -f hermes-doro                 # live logs
./run.sh                              # rebuild the container after changing config/ or the skills layout (refreshes mounts)
docker restart hermes-doro                 # everyday restart when config mounts are unchanged
docker exec hermes-doro hermes gateway status
docker exec -it hermes-doro hermes tools   # trim unused toolsets to slim the system prompt
```

Upgrade: `./update.sh --rebuild-image` (equivalent to `./build.sh --pull && ./run.sh`; `config/` keeps local real configs, `data/` keeps runtime state via bind mounts).

## Troubleshooting

**Bot connects but doesn't reply** → the Lark console is missing event subscriptions or the app isn't published:
- Open platform → Events & callbacks → subscribe to `im.message.receive_v1`
- Version management → create a version and publish (enterprise apps need approval)
- Check that permissions include `im:message`, `im:message:send_as_bot`

**model not found** → `default` in `config/config.yaml` must exactly match a model name the endpoint actually exposes:
`curl -H "Authorization: Bearer <key>" <base_url>/models`

**Dockerfile changes don't take effect** → rebuild the image with `./build.sh`, then `./run.sh`; `run.sh` stops the container gracefully (≤60s) and recreates the hermes container, but never deletes `data/` or `config/`.

## Handy in-chat commands

| Command                | Purpose                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `/usage`               | show current context usage                                     |
| `/compress`            | compress a long conversation immediately                       |
| `/new`                 | start a new session (memory persists)                          |
| `/sethome`             | make this session the landing spot for cron/notifications      |
| `/model provider:model` | switch model manually (session-scoped; `--global` writes back) |
| `/reasoning low`       | reduce reasoning tokens                                        |

The OpenCode bridge's `/opencode ...` command (optional feature) is documented in [`skills/README.md`](skills/README.md#opencode).

## License

Apache-2.0
