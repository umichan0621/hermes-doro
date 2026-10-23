---
name: qq-progress
description: "Use for multi-step QQ tasks; progress via recall-as-edit."
version: 1.0.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [QQ, qqbot, progress, task reporting]
---

# QQ Multi-step Task Progress (recall-as-edit)

QQ has no message-edit API. This protocol replaces the edit-based progress
flow with **recall + resend**: at most ONE progress message is visible at
any time; every update recalls the previous message and sends the full
accumulated log as a new one. The message layout (step lines, status line,
closing sections, final-reply shape) mirrors the lark-progress skill so both
platforms report identically — but this file restates the format in full;
loading lark-progress is not required.

## When to Use

- Any QQ (qqbot) task that will run several tool steps and deserves per-step
  feedback: repo changes, multi-source reports, long analysis, host queries.
- Do NOT use for greetings, single lookups, or answers you can give at once.
- Group chats: same recipe, replace `/v2/users/{openid}` with
  `/v2/groups/{group_openid}` (endpoints unverified for groups — test first).

## Hard quota facts (official docs; C2C = direct chat)

- Active messages (no msg_id): **4 per user per MONTH**. Never send progress as
  active messages — one long task would burn a month of quota.
- Passive replies (with msg_id): valid **60 min** after the user's message, max
  **5 replies per msg_id**. All progress sends + the final reply share this
  budget.
- Group chats: the passive window is 5 min, also 5 replies per msg_id.

## Budget discipline

Per inbound user message: ≤5 passive replies total. Leave seq 1..2 for the
gateway's own final reply (its counter starts at 1). Use msg_seq 80+ for
every progress send so you never collide with gateway sequence numbers, and
bump the seq on every send — a reused seq is rejected as a duplicate (see
Pitfalls). With the gateway's reply taking 1–2 slots, plan for at most
three progress sends per task: the initial plan, one or two milestone
updates, the last one carrying the 🏁 close. If steps outnumber budget:
merge steps, report milestones only, or skip interim progress and let the
final reply carry the full step log.

## API recipe

Use `scripts/qq_api.py` (stdlib-only, reads `/opt/data/.env` itself):

```bash
# send (passive): prints the new message id — SAVE it, recall needs it
python3 /opt/data/skills/qq-progress/scripts/qq_api.py send "$(cat /opt/data/workspace/progress.md)" --seq 80
python3 /opt/data/skills/qq-progress/scripts/qq_api.py recall "<message-id-from-send>"
```

The script picks the passive msg_id itself: the latest inbound
`platform_message_id` (role='user') from `state.db` messages — that is where
message freshness lives; there is no other history source.

Endpoints (verified on this bot): token
`POST https://api.bot.qq.com/app/getAppAccessToken` `{appId, clientSecret}`;
send `POST https://api.sgroup.qq.com/v2/users/{openid}/messages`
`{content, msg_type:0, msg_id, msg_seq}` → returns `{"id", "timestamp"}`;
recall `DELETE /v2/users/{openid}/messages/{id}` → `200 {}`. openid = the
value of `QQBOT_HOME_CHANNEL` in `/opt/data/.env` (same as session User ID;
the adapter stores it raw as chat_id). URL-encode the message id (`!` inside).
JSON body must be UTF-8; set timeout ≥5s.

## Start before doing the work

For a task covered by this skill, send the initial progress message before
any investigation, recall, planning, or other substantive tool call. Loading
this skill with `skill_view(name='qq-progress')` is the only allowed
preliminary tool call; sending the progress message must still be the first
substantive action.

1. Read `/opt/data/workspace/progress.md` first — it still holds the previous
   task's content and `write_file` refuses a stale overwrite (never-read
   guard); reading it first (or using `patch` for a targeted edit) saves a
   wasted refused call.
2. Rewrite it with the `#` task title, the 🚀 task status line, and the
   planned steps as ⬜ lines (formats below). It must describe the actual
   plan, not just say that work has started.
3. Send it (seq 80) and record the printed message id — every later update
   recalls that id and stores the id the new send returns.

## Update cadence (quota-aware)

Each resend spends one passive reply, so you cannot resend after literally
every step the way lark-progress edits after every step. Update at
milestones within the send budget: initial plan → one or two milestone
resends → the 🏁 close. The LAST progress send must carry the complete
accumulated step log — merging steps into milestone lines is fine, dropping
completed steps is not. Per-resend flow:

1. Rewrite the complete accumulated content of `progress.md` — recall +
   resend replaces the whole message, so always write the full replacement
   content before sending.
2. Recall the stored message id immediately, before sending the new one;
   the recall time window is unverified, so never leave a message to be
   recalled minutes later.
3. Send the new content with the next seq (81, 82, ...) and store the new
   id for the next recall.

### Title and task status line

The message opens with the task title as a `#` heading, followed by a task
status line. The task-level timeline (🚀/🏁/❌) mirrors the step-level one
(✅/🔄/⬜):

- Running: `🚀 HH:MM:SS · step N/M` — N is the step currently running (0
  before the first step starts), M the planned total number of steps.
- Completed: `🏁 HH:MM:SS → HH:MM:SS · M/M · total MsSs` — the arrow lands
  on the completion time; `total` reuses the compact duration form.
- Failed: `❌ HH:MM:SS → HH:MM:SS · failed at step N`.

The 🏁 or ❌ line is finalized in the last progress send — the same send
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
- ⬜ [4] verify rendering
```

Read-only lookups (log scans, history reads, cron list) also count as steps
and get their own ✅ line, never a silent pass.

### Closing sections

Before the final response, append the closing sections to the last progress
send:

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
the user prefers this accounted for inside the task's reporting, not as an
unexplained extra message. In THIS task's final summary reply, add one line
listing exactly which skills/memory entries THIS task changed. The canonical
English form:

```markdown
💾 **Self-improvement**: 🆕 skill-creator created · 🔧 qq-progress updated · 🧠 memory +1
```

The 💾 prefix mirrors the runtime's review line so both read as one concept,
and the per-entry emoji keeps a dense line scannable. Each entry is
`<emoji> <name> <verb>`, joined by ` · ` — 🆕 created, 🔧 updated, ✏️ renamed
for skills; 🧠 memory +N for memory (a single entry however many records
were added). Render the labels in the language the user is writing in,
keeping the template's shape identical across languages.

## Finish or recover

The gateway delivers the agent's final response as the task's closing
message. When the task took more than three steps, the final reply MUST
follow this template — conclusion first and easy to scan; unresolved
blockers go under Key points, and the Next section is omitted entirely when
there is no next action:

```markdown
**Result**: <one-sentence result>

**Key points**
- <important changes or evidence>
- <verification method and result>

**Next**: <next action — omit the whole section when empty>
```

Render the labels in the language the user is writing in, keeping the
template's shape (bold labels, bulleted key points) identical across
languages. State the verification method, not just the count — "29/29
deleted" plus how it was verified is what makes a destructive bulk operation
trustworthy.

For tasks of three steps or fewer a brief closing answer is fine. Beyond
the progress message and the final reply, do not send separate commentary.

If the initial send is skipped, lost, or times out, repair the protocol as
soon as the omission is noticed: write the accumulated full `progress.md`,
send it, record the new message id, and continue. A stored id that is lost
can never be recovered (no history API) — the orphan message cannot be
recalled; send the next update anyway and keep the one-visible-message
invariant from then on. A task that fails or is interrupted must be marked
as failed in the progress message with the ❌ task status line; never
present a final success summary that the evidence does not support.

## Pitfalls

- **Save the id from every send.** The send response is the only source of
  the message id (no history API); print it in full — a truncated id can
  never be recalled.
- **Token 401 / code 11201 is often QQ-side jitter, not a bug**: the token
  service intermittently returns a ~20-char dud token (healthy ones are
  ~78 chars). Validate `len(access_token) > 50` before use and refetch with
  backoff; tokens live ~2 min here (expires_in 63–198s observed), so fetch
  fresh per batch. One good token worked on both api.sgroup.qq.com and
  api.bot.qq.com.
- **msg_seq dedup**: same msg_id + msg_seq returns 400 code 40054005
  (message deduplicated). Bump the seq, don't retry blind.
- **Compute step durations from timestamps, not from plan estimates** —
  capture `date +%T` at step start and completion and subtract; times typed
  by hand can produce a negative duration that reads as broken.
- Probe messages are visible to the user — send at most one test message per
  task and recall it right away.
- Text only (`msg_type: 0`). Markdown (msg_type 2) needs platform approval,
  and plain-text rendering means `#`/`**` appear literally — keep the
  borrowed layout (it still scans fine), but never add tables or links to
  the progress text.
