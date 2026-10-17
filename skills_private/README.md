# skills_private/

Private skill layer for the agent, bind-mounted read-write to `/opt/data/skills_private`.

Clone private skill repos as real subdirectories here (each keeps its own git
and push workflow; symlinks do not resolve inside the container). Everything
except this README is gitignored in the outer repo — `git clean -fdx` in the
outer repo WILL delete inner repos' unpushed iterations, so commit and push
inside them promptly. `PRIVATE_SKILLS_SRC=<path> ./run.sh` mounts an external
directory instead. Ensure `/opt/data/skills_private` is listed under
`skills.external_dirs` in `config/config.yaml` (missing dirs are skipped).
