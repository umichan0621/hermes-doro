---
name: lark-progress
description: "Manage progress reporting for Lark conversations. Every task that runs tool steps gets a progress message reporting each step; tasks exceeding three steps end with a summary. Use for any multi-step work — repository changes, multi-source reports, long analysis, OpenCode/host queries — even if the user does not mention progress updates. Do not use for greetings or immediately answerable requests."
version: 1.0.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Lark, progress, task reporting]
---

# Lark Multi-step Task Reporting

Use this protocol for any Lark task that will run tool steps. The user's
standing rule: every step gets feedback, tasks over three steps end with a
summary, and the conversation ends with exactly two messages — one editable
progress message and one final reply.

## Start before doing the work

For a task covered by this skill, send the initial progress message before any
investigation, recall, planning, or other substantive tool call. Loading this
skill with `skill_view(name='lark-progress')` is the only allowed preliminary
tool call; sending the progress message must still be the first substantive
action.

1. Create `/opt/data/workspace/progress.md` with the `#` task title, the 🚀
   task status line, and the planned steps as ⬜ lines (formats below). It
   must describe the actual plan, not just say that work has started.
2. Send it in the current source chat (pipe the file via stdin — see the
   pitfall below):

   ```bash
   lark-cli im +messages-send --as bot --chat-id <current-chat-id> --markdown - < /opt/data/workspace/progress.md
   ```

3. Record the returned `message_id`. Use that same ID for every update:

   ```bash
   lark-cli im +messages-edit --as bot --message-id <message-id> --markdown - < /opt/data/workspace/progress.md
   ```

The chat ID must come from the current Lark event. This applies to both
direct and group chats; never use a hard-coded home channel.

## Pitfalls

- **Pipe markdown files via stdin (`--markdown - < file`), never `@<path>`** —
  the CLI rejects `@` paths outside its allowlist (cwd, /tmp, ~/files), and
  progress.md normally lives in the workspace, which is often outside them.
- **Pass only `oc_` chat IDs to `--chat-id`** — an `ou_` user ID is rejected
  as an invalid format; in a DM event that carries only an `ou_` ID, use the
  session context's home-channel `oc_` ID as the destination.
- **Read `progress.md` before the first write of a new task** — the file
  still holds the previous task's content and `write_file` refuses a stale
  overwrite (never-read guard); reading it first (or using `patch` for a
  targeted edit) saves a wasted refused call at every task start.
- **Reuse the same `message_id` across a whole task, and never run two
  progress-message tasks in the same chat against one stored ID** — each
  `+messages-send` returns a fresh ID; overwrite the stored ID immediately
  when a new one is returned or every later `+messages-edit` lands on the
  wrong message.
- **Compute step durations from timestamps, not from plan estimates** —
  capture `date +%T` at step start and completion and subtract; the
  completed line's end time can land slightly earlier than the running
  step's claimed start when times are typed by hand, which reads as a
  negative duration.
- **State the verification method, not just the count, in the final
  summary** — "29/29 deleted" plus how it was verified (re-list the chat,
  cross-check `deleted: true` via mget) is what makes a destructive bulk
  operation trustworthy; a bare count from the delete responses alone can
  overstate success.

## Update after every step

After EVERY step with a real result (each tool call or command batch:
file reads, shell commands, API calls, host queries), rewrite the complete
accumulated content of `progress.md` and edit the progress message once.
Do not batch several tool calls into one "logical step" — the user wants
per-step feedback. Keep every completed step visible.

### Title and task status line

The message opens with the task title as a `#` heading, followed by a task
status line. The task-level timeline (🚀/🏁/❌) mirrors the step-level one
(✅/🔄/⬜):

- Running: `🚀 HH:MM:SS · step N/M` — N is the step currently running (0
  before the first step starts), M the planned total number of steps.
- Completed: `🏁 HH:MM:SS → HH:MM:SS · M/M · total MsSs` — the arrow lands
  on the completion time; `total` reuses the compact duration form.
- Failed: `❌ HH:MM:SS → HH:MM:SS · failed at step N`.

The 🏁 or ❌ line is finalized in the last progress edit — the same edit
that appends the closing sections.

### Step line format (standing user preference)

- Step label format is `[N]` (not "Step N:"); all times use the 24h clock.
- A step that has not started yet shows as `- ⬜ [N] <desc>`.
- When a step STARTS, its line becomes `- 🔄 [N] <desc> · HH:MM:SS →` —
  the arrow stays open while the step runs.
- While the step runs, show the CURRENT action under it as a sub-bullet:
  `  - ⏳ HH:MM:SS <what is being done right now>` — one line, updated at
  every rewrite so the user always sees what is happening and when.
- When a step COMPLETES, delete the in-progress sub-bullets and land the
  arrow on the completion time, changing the line to:
  `- ✅ [N] <desc> · HH:MM:SS → HH:MM:SS (MsSs)`
  The duration uses the compact form `(19s)` or `(2m19s)` — never a
  zero-padded form like "0m19s"
  and never a rounded value like "2min". Under 60s → `(19s)`; at or over
  60s → `(2m19s)`.

Example of a completed step 2, a running step 3, and a pending step 4:

```markdown
- ✅ [2] inspect the state machine · 05:15:46 → 05:17:00 (1m14s)
- 🔄 [3] rewrite and write back · 05:17:04 →
  - ⏳ 05:17:12 replacing the broken table block
- ⬜ [4] verify rendering in Lark
```

Because editing replaces the whole message, always write the full replacement
content before editing. If a send or edit must be retried, update the stored
message ID when a new one is returned.

Before the final response, append the closing sections to the progress
message:

```markdown
## Learnings
- Reusable finding from this task.
```

When unresolved blockers exist, list them explicitly under `## Blockers`
(before Learnings); when there are none, omit the section entirely instead
of writing `- None`. Do not create a progress message merely to confirm that
work is happening.

## Self-improvement disclosure in the final reply

After the final response, the runtime's background review may patch skills or
memory on its own and post a terse `💾 Self-improvement review: ...` line —
the user prefers this accounted for inside the two-message protocol, not as
an unexplained extra message. In THIS task's final summary reply, add one
line listing exactly which skills/memory entries THIS task changed. The
canonical English form:

```markdown
💾 **Self-improvement**: 🆕 skill-creator created · 🔧 lark-progress updated · 🧠 memory +1
```

The 💾 prefix mirrors the runtime's review line so both read as one concept,
and the per-entry emoji keeps a dense line scannable in Lark. Each entry is
`<emoji> <name> <verb>`, joined by ` · ` — 🆕 created, 🔧 updated, ✏️ renamed
for skills; 🧠 memory +N for memory (a single entry however many records
were added).

Render the labels in the language the user is writing in, keeping the
template's shape (💾, bold labels, bulleted key points) identical across
languages.

## Finish or recover

The final response is the second Lark message. When the task took more than
three steps, the final reply MUST follow this template — conclusion first
and easy to scan; unresolved blockers go under Key points, and the Next
section is omitted entirely when there is no next action:

```markdown
**Result**: <one-sentence result>

**Key points**
- <important changes or evidence>
- <verification method and result>

**Next**: <next action — omit the whole section when empty>
```

Render the labels in the language the user is writing in, keeping the
template's shape (bold labels, bulleted key points) identical across
languages.

For tasks of three steps or fewer a brief closing answer is fine. Do not send
separate commentary or progress messages beyond the two. A per-step progress
message also covers read-only lookups (log scans, history reads, cron list)
— they still count as steps and get their own ✅ line, never a silent pass.

If the initial send is skipped, lost, or times out, repair the protocol as soon
as the omission is noticed: write the accumulated full `progress.md`, send it,
record the new `message_id`, and continue. A task that fails or is interrupted
must be marked as failed in the progress message with the ❌ task status
line; never present a final success summary that the evidence does not
support.