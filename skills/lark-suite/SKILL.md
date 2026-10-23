---
name: lark-suite
description: |
  Gateway to the 28 domain skills embedded in the lark-cli binary. Use whenever the user asks to operate any Lark/Feishu business domain — send or read messages and manage chats, create/edit docs, spreadsheets, Base tables, slides or whiteboards, manage calendar events and meeting rooms, read or send email, manage tasks and to-dos, OKRs, approvals, attendance records, wiki spaces, drive files, meetings/minutes, or build Spark/Miaoda (妙搭) apps — even without naming lark-cli. Teaches how to browse and read the embedded skills. Keywords: Lark, Feishu, 飞书, message, chat, doc, sheet, Base, calendar, mail, task, OKR, approval, attendance, wiki, drive, meeting, minutes, whiteboard, slides, markdown, 妙搭, contact, event.
version: 1.0.0
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [Lark, Feishu, lark-cli, skills, messaging, docs, sheets, base, calendar, mail]
---

# Lark domain skills (embedded in lark-cli)

The `lark-cli` binary baked into the image embeds **28 domain skills** at
build time — always in sync with the CLI version, zero setup, no download.
This skill is the index page: it tells you what exists and how to read it.

Before any business operation, make sure auth is fine: check
`lark-cli auth status`; for login, identity, scopes and config questions
read the `lark-shared` skill first (other skills assume it).

## How to read the embedded skills

```bash
lark-cli skills list                     # all 28 skills (JSON: name + description)
lark-cli skills read <skill>             # a skill's SKILL.md
lark-cli skills list <skill>[/subdir]    # entries under a skill (like ls)
lark-cli skills read <skill> <file>      # one file under the skill, e.g.
                                         # lark-cli skills read lark-im references/lark-im-card-action-reply.md
```

Before reading any subfile, run `lark-cli skills list <skill>[/subdir]` to get
real filenames — per-skill layouts differ (`references/` vs `scenes/` vs
`routes/`), and a guessed path fails with `invalid_argument` ("path not found
in skill"), which means a wrong path, not a broken reader; re-list and retry
with a real path.

Pick the matching skill below, read its `SKILL.md`, then follow it (the
embedded content is written in Chinese — follow it regardless of its
language and keep user-facing replies in the user's language).

## Domain catalog

| Skill                          | Covers                                                             |
| ------------------------------ | ------------------------------------------------------------------ |
| `lark-im`                      | Messaging: send/read messages, chats, reactions, files, cards      |
| `lark-doc`                     | Docs (Docx/Wiki doc content): read, create, edit, image assets     |
| `lark-sheets`                  | Spreadsheets: create and operate sheets                            |
| `lark-base`                    | Base: tables, fields, records, views, dashboards, roles, templates |
| `lark-calendar`                | Calendar events and meeting rooms                                  |
| `lark-mail`                    | Mail: drafts, send, reply, forward, search, folders, watch         |
| `lark-task`                    | Tasks, task lists, subtasks, task agents                           |
| `lark-okr`                     | OKR objectives and key results                                     |
| `lark-approval`                | Approval tasks and instances, start native approvals               |
| `lark-attendance`              | Personal attendance check-in records                               |
| `lark-contact`                 | Directory: name/email ↔ open_id, profiles, search bots/agents      |
| `lark-wiki`                    | Wiki spaces, members, document nodes                               |
| `lark-drive`                   | Drive files/folders: upload, download, permissions, comments       |
| `lark-markdown`                | Drive-native Markdown files: view, create, edit, compare           |
| `lark-meeting`                 | Meetings: records, Minutes (妙记), live content, in-meeting Q&A    |
| `lark-whiteboard`              | Whiteboards in docs: query and edit                                |
| `lark-slides`                  | Slides: create, edit, manage                                       |
| `lark-apps`                    | Spark/Miaoda (妙搭) app dev, hosting, creative design              |
| `lark-event`                   | Real-time event subscribe/consume (NDJSON stream)                  |
| `lark-openapi-explorer`        | Dig up raw OpenAPI endpoints not yet wrapped by the CLI            |
| `lark-skill-maker`             | Create custom lark-cli skills                                      |
| `lark-shared`                  | lark-cli setup/auth, identity switching, scopes (auto-loaded)      |
| `lark-workflow-meeting-summary`| Workflow: aggregate meeting minutes into a structured report       |
| `lark-workflow-standup-report` | Workflow: calendar + tasks standup digest                          |
| `lark-minutes`                 | Alias — handled by `lark-meeting`                                  |
| `lark-note`                    | Alias — handled by `lark-meeting`                                  |
| `lark-vc`                      | Alias — handled by `lark-meeting`                                  |
| `lark-vc-agent`                | Alias — handled by `lark-meeting`                                  |

Caveat: upstream ships some per-skill content that is NOT embedded in the
binary — verified against this image, the only functionally relevant gap
is `lark-apps`: its SKILL.md points creative-design workflows (UI mockup,
prototype, landing page, deck) at `creative-design/creative-design.md`,
which `lark-cli skills read` cannot serve (the upstream directory carries
machine resources — agents/assets/starter-components — and is excluded
wholesale). For such flows, fetch the file from the larksuite/cli repo
(tag `v$(lark-cli --version | awk '{print $NF}')`) or tell the user the
flow needs the upstream repo. Two more upstream-only directories,
`lark-sheets` `scripts/` and `lark-whiteboard` `elements/`, are likewise
not embedded but never referenced by the embedded protocol — no
functional impact. Markdown docs such as `lark-meeting`/`lark-whiteboard`
`scenes/` and `routes/` ARE embedded.

## Integrity audit (after a lark-cli upgrade or skill update)

```bash
# every embedded skill must read; expect: 28 skills, all OK
lark-cli skills list | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['skills']), 'skills')"
for n in $(lark-cli skills list | python3 -c "import json,sys; print(' '.join(s['name'] for s in json.load(sys.stdin)['skills']))"); do
  lark-cli skills read "$n" >/dev/null 2>&1 && echo "OK   $n" || echo "FAIL $n"
done
```

Then spot-check the Caveat areas so the audit matches reality: reading
`lark-apps` `creative-design/creative-design.md` is EXPECTED to fail with
"path not found" (the one confirmed gap), while any file under
`lark-meeting` `scenes/`/`references/`, `lark-whiteboard`
`scenes/`/`routes/`, or `lark-apps` `references/` must read. The read
command's exit code is the pass/fail signal — error JSON is printed on
stdout but exits non-zero. Any FAIL outside creative-design means the
binary and this catalog have drifted; re-verify the catalog before trusting
it, and update this skill's Caveat with what the audit actually finds.

## Related

- `lark-progress` — the two-message reporting protocol for multi-step Lark
  tasks (progress message + final answer); use it alongside any skill here.
- `meego` — Feishu Project (Meegle) operations, a separate CLI (`meegle`)
  with its own baked skill; do not route project-management work here.
