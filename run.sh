#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

IMAGE=hermes-doro-hardened
CONTAINER=hermes-doro

DATA_DIR="$PWD/data"
mkdir -p "$DATA_DIR"
DATA_DIR="$(realpath "$DATA_DIR")"
chmod 700 "$DATA_DIR"

chmod 700 config
seeded=0
for config_file in .env config.yaml SOUL.md opencode_projects.yaml; do
  if [ ! -f "$PWD/config/$config_file" ]; then
    if [ ! -f "config/$config_file.example" ]; then
      echo "==> Missing config and template: config/$config_file"
      exit 1
    fi
    (umask 077; cp "config/$config_file.example" "config/$config_file")
    chmod 600 "config/$config_file"
    echo "==> Created config/$config_file from template"
    seeded=1
  fi
done
if [ "$seeded" -eq 1 ]; then
  echo "==> Edit the newly created files under config/, fill in credentials, model and project paths, then run ./run.sh again"
  exit 1
fi

# Host shared directory (for file exchange); always mounted at /mnt/hermes-doro inside the container.
SHARED_DATA_DIR="${SHARED_DATA_DIR:-/mnt/hermes-doro}"
if [ ! -d "$SHARED_DATA_DIR" ]; then
  echo "==> Host directory $SHARED_DATA_DIR does not exist; create it before running ./run.sh"
  exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "==> Image $IMAGE not found; run ./build.sh first"
  exit 1
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  docker stop --time 60 "$CONTAINER" >/dev/null
  docker rm "$CONTAINER" >/dev/null
fi

vol_args=(-v "$DATA_DIR:/opt/data" -v "$SHARED_DATA_DIR:/mnt/hermes-doro:rw")
for config_file in .env config.yaml SOUL.md opencode_projects.yaml; do
  vol_args+=(--mount "type=bind,src=$PWD/config/$config_file,dst=/opt/data/$config_file")
done
# Optional: managed cron job scripts (definitions archived in cron/jobs.yaml).
# Mounted at /opt/data/scripts inside the container; skipped if the directory
# is missing so fresh clones work without local cron assets.
if [ -d "$PWD/cron/scripts" ]; then
  vol_args+=(-v "$PWD/cron/scripts:/opt/data/scripts:rw")
  echo "==> Mounting cron scripts: $PWD/cron/scripts -> /opt/data/scripts"
elif [ -d "$PWD/cron" ]; then
  echo "==> Note: cron/ exists but cron/scripts/ is missing; monitor jobs would not find their scripts (see cron/README.md)"
fi
# Public skills, single copy: agent edits land directly in the repo working tree.
for skill_dir in "$PWD"/skills/*/; do
  [ -f "${skill_dir}SKILL.md" ] || continue
  skill_name=$(basename "$skill_dir")
  vol_args+=(-v "${skill_dir%/}:/opt/data/skills/$skill_name:rw")
done
# Optional: private skills (agent-writable learning layer, managed in its own git).
# Defaults to the repo's skills_private/ (gitignored; may hold several private repo clones);
# the PRIVATE_SKILLS_SRC env var can override this with another location; skipped if the directory is missing.
PRIVATE_SKILLS_SRC="${PRIVATE_SKILLS_SRC:-$PWD/skills_private}"
if [ -d "$PRIVATE_SKILLS_SRC" ]; then
  vol_args+=(-v "$PRIVATE_SKILLS_SRC:/opt/data/skills_private")
  echo "==> Mounting private skills: $PRIVATE_SKILLS_SRC -> /opt/data/skills_private"
fi

# Timezone: keep in sync with the host (read-only mount)
vol_args+=(-v /etc/localtime:/etc/localtime:ro)
if [ -f /etc/timezone ]; then
  vol_args+=(-v /etc/timezone:/etc/timezone:ro)
fi

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges \
  --memory=4g --cpus=2.0 --pids-limit=512 \
  --sysctl net.ipv6.conf.all.disable_ipv6=1 \
  -e HERMES_UID="$(id -u)" -e HERMES_GID="$(id -g)" \
  -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  --add-host=host.docker.internal:host-gateway \
  "${vol_args[@]}" \
  "$IMAGE" gateway run

echo "==> Started. Follow logs: docker logs -f $CONTAINER"
