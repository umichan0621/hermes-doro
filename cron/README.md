# cron/ — container cron job assets

Host-side home for assets used by the **Hermes in-container scheduler**
(`hermes cron`). Only this README is tracked; everything else stays local
(internal session IDs, project names — private repo workflow).

## Layout

| File                 | Purpose                                                        |
| -------------------- | -------------------------------------------------------------- |
| `jobs.yaml`          | Job definition archive (recovered/expired jobs + templates)     |
| `scripts/`           | Monitor scripts, mounted into the container (see below)        |
| `apply.sh`           | (optional, local) helper to (re)create jobs via `hermes cron`  |

## Mount

`cron/scripts/` ↔ `/opt/data/scripts` inside the container (`rw`, see
`run.sh`; the mount is skipped when the directory is missing). The agent
writes monitor scripts there and references them by absolute path in
`cronjob_manage` / `hermes cron create --monitor`.

Note: the mount **shadows** any files the container previously wrote to
`/opt/data/scripts` via the `data/` volume — keep the canonical copies here.
Create `cron/scripts/` **before** registering any monitor job (jobs reference
`/opt/data/scripts/<name>` by absolute path). When adopting an existing
deployment that already has `data/scripts/` content, copy it into
`cron/scripts/` first, then run `./run.sh`.

The mountpoint itself lives on the host as `data/scripts/` (Docker creates it
inside the `/opt/data` bind; it may appear root-owned). Never delete that
directory while the container exists — removing it detaches the mount inside
the running container until the next `docker restart`.

## Two cron systems — do not confuse

- **This directory** — assets (scripts + archived definitions) for jobs run
  by the Hermes scheduler *inside* the container. Jobs are created with
  `hermes cron create` (or `cronjob_manage` in chat) and fire in-container.
- **`data/cron/`** — the Hermes scheduler's *runtime state* (jobs.json,
  executions.db, heartbeats). Bind-mounted via `data/`, never edited by hand.

Host-side crontab is a separate mechanism again and not managed here.

## Security note

`cron/scripts` is mounted `rw` into the container on purpose: the agent
creates and iterates monitor scripts there. Anything that compromises the
container can therefore modify files the agent will later execute — the same
trust boundary as `skills/` (agent-writable, mounted into the container).
Do not place host-crontab-executed scripts in this directory; host-executed
automation belongs outside the container's writable surface.

## Provenance

The first two archived jobs (`oc-featapi-watch`,
`opencode-webrefactor-check`) were one-shot watchdogs for background
OpenCode tasks that completed and self-removed on 2026-10-18. Their
definitions (including internal identifiers) were recovered from
`data/state.db` message history after `data/cron/jobs.json` had been
emptied; see `jobs.yaml`.
