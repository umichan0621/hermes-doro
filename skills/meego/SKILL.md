---
name: meego
description: |
  Operate Feishu Project (Meego/Meegle) for the user: query and manage work items, drive workflow-node transitions, inspect saved views, list personal todos, analyze schedules and workload, and search with MQL. Use this skill whenever the user mentions Feishu Project, Meego, or Meegle, or asks about requirements, tasks, bugs/defects, iterations, schedule planning, views, or todos in their project tracker — even without naming the tool. Keywords: Feishu Project, Meego, meegle, work item, requirement, task, defect, schedule, view, todo, node.
version: 1.0.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Meego, Meegle, Feishu Project, work-item, MQL, project-management]
---

# Feishu Project (Meego/Meegle) Operations Guide (loader)

This file is a lightweight loader: the full skill (upstream
[meegle-cli](https://github.com/larksuite/meegle-cli) `skills/meegle/`, i.e.
SKILL.md + references/) ships baked into the image at
`/opt/hermes/skill/meegle/` (read-only, version-locked to the CLI); a
runtime download into `/opt/data/skill/meegle/` remains only as fallback
when the baked copy is missing or version-mismatched. The repo keeps only
this loader and never tracks upstream content.

Naming (deliberate — do not "fix" it): the skill directory / trigger name is
`meego` (what users call it); the upstream project and the content
directories use `meegle`. Singular `/opt/hermes/skill/` is the image-baked
layer, singular `/opt/data/skill/` is the runtime download layer; both are
kept separate from run.sh's mounted `/opt/data/skills/` (plural,
repo-managed public skills) so the three never overwrite each other.

## Bootstrap (check on first trigger per session; skip when usable)

The single source of truth for versions is the `meegle` binary baked into
the image (pinned by `MEEGLE_CLI_VERSION` in the Dockerfile). This loader
asks the binary at runtime and never hardcodes a version itself.

Primary source: `/opt/hermes/skill/meegle/` — the upstream skill baked
into the image at build time from the same-version tag (checksum-pinned
by `MEEGLE_SKILL_SHA256`), so it always matches the binary and needs no
download. Fallback: `/opt/data/skill/meegle/` (host: `data/skill/meegle/`,
bind mount, survives container rebuilds), downloaded on first use only
when the baked copy is missing or version-mismatched (e.g. the image was
not rebuilt after an upgrade).

```bash
ver="$(meegle version)"   # prints the bare version, e.g. 1.0.27
baked=/opt/hermes/skill/meegle
dest=/opt/data/skill/meegle
if [ -f "$baked/SKILL.md" ] && [ -d "$baked/references" ] \
   && [ "$(cat "$baked/.version" 2>/dev/null)" = "$ver" ]; then
  dest="$baked"   # image-baked copy matches the binary — use it, no download
elif [ -f "$dest/SKILL.md" ] && [ -d "$dest/references" ] \
   && [ "$(cat "$dest/.version" 2>/dev/null)" = "$ver" ]; then
  :   # runtime cache valid and matching — skip
else
  tmp="/opt/data/skill/.meegle.tmp.$$"
  rm -rf "$tmp"; mkdir -p "$tmp" /opt/data/skill
  curl -fsSL --connect-timeout 15 --retry 2 \
    "https://codeload.github.com/larksuite/meegle-cli/tar.gz/refs/tags/v${ver}" \
    | tar -xz --strip-components=2 -C "$tmp" "meegle-cli-${ver}/skills/meegle" \
    && [ -f "$tmp/meegle/SKILL.md" ] && [ -d "$tmp/meegle/references" ] \
    || { rm -rf "$tmp"; echo "download/extract failed" >&2; exit 1; }
  rm -rf "$dest" && mv "$tmp/meegle" "$dest"   # atomic swap only after the structure check
  printf '%s' "$ver" > "$dest/.version"; rm -rf "$tmp"
fi
echo "MEEGO_SKILL_DIR=$dest"
```

Key points:

- Resolution order: image-baked copy (structure + `.version` both match
  the binary) → runtime cache (same check) → atomic download into the
  runtime cache. The snippet prints the resolved directory as
  `MEEGO_SKILL_DIR`; use that path for everything below.
- Extract to a temp dir, structure-check (SKILL.md + references/ both
  present), then atomically swap: an interrupted run never leaves a half
  cache, and the old cache stays usable until the new download succeeds.
- `.version` stamps the CLI version the content was built for. The baked
  copy is stamped at image build time, so after a normal upgrade
  (`./build.sh`) it matches again automatically — the runtime download
  path normally never runs at all.
- The download uses the codeload.github.com direct URL because github.com
  archive redirects are flaky on some networks.
- On download/extract failure (network or disk): report it honestly. NEVER
  guess commands from memory. The maintainer can preseed manually per the
  meego section of the repo's `skills/README.md`.

## Forwarding

Once bootstrapped, **read the `SKILL.md` inside the directory printed as
`MEEGO_SKILL_DIR` (baked: `/opt/hermes/skill/meegle/`; downloaded:
`/opt/data/skill/meegle/`) and follow it fully from that point on** (its
auth flow, per-domain commands, and references/ pointers). This loader
then steps back and takes no further part. The upstream skill is written
in Chinese — follow it regardless of its language, and always keep
user-facing replies in the user's language. The baked copy is read-only:
never write into `/opt/hermes/skill/`; writable content belongs under
`/opt/data`.

## In-container adaptations (these override the cached skill's matching steps)

- No browser, no interactive TTY: replace the browser-OAuth STEP 2 of the
  cached auth flow (references/auth-guard.md) with the device-code flow;
  keep every other step (profile selection, host consistency check,
  `$profile_args` pass-through) unchanged. Device-code offers a split
  non-blocking mode that fits the session terminal's ~420 s cap:

  ```bash
  # 1) init: prints JSON (client_id / device_code / user_code / verification_uri_complete)
  meegle "${profile_args[@]}" auth login --host "$host" --device-code --phase init --format json
  # 2) show verification_uri_complete (or verification_uri + user_code) verbatim to the user,
  #    and wait for them to approve on their phone
  # 3) poll (--once is a single non-blocking attempt; sleep for `interval`, retry
  #    until success or the `expires_in` timeout):
  meegle "${profile_args[@]}" auth login --host "$host" --device-code \
    --phase poll --client-id <client_id from init> \
    --device-code-value <device_code from init> --once
  ```

  The blocking one-shot `meegle auth login --device-code` (default 600 s)
  also works, but in long sessions prefer the init/poll split above so the
  foreground command is never killed by the terminal cap.
- The `meegle` binary is baked into the image (pinned via the Dockerfile's
  `MEEGLE_CLI_VERSION` — the only version pin).
- `MEEGLE_NO_UPDATE_CHECK=1` and `MEEGLE_TELEMETRY=disabled` are set in the
  image and verified effective; ignore update prompts and diagnostics
  reporting.
