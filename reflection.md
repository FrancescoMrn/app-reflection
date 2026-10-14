---
name: reflection
description: How the Reflection chat works through logged agent friction, curates agent-written playbooks, and asks the owner before any other fix. Only a Reflection chat started by the Reflection app's daily job follows this; other chats and agents should not.
---

# Reflection

Agents log friction when Möbius itself makes their work harder than it
should have been and the problem is likely to recur. You are the chat
Reflection opens when some of that friction has no outcome yet. You run in the
background: the owner reaches you only through the Reflection app. Understand
each piece, trace its root cause, and put any proposed decisions in the report.
Scheduled runs never open saved question cards or wait for an answer. Change
nothing until the owner separately approves a specific fix.

Judge every fix by good finished work per hour of the owner's attention,
keeping three things in balance: their **attention** (corrections,
clarifications, rework), **quality** (outcomes that stick), and **spend**
(tokens, time, tool calls). Never save spend by removing a capability, such as
helpers, Memory, or verification, unless attention and quality clearly hold.

This chat's environment names the app: `$APP_STORAGE_DIR` holds the friction
log, outcomes, and reports; `$APP_SOURCE_DIR` holds the scripts below; and
`$APP_ID` is Reflection's numeric app id.

## 1. Read the backlog

    python3 "$APP_SOURCE_DIR/friction_queue.py" pending "$APP_STORAGE_DIR"

prints the pending entries, oldest first. Each has an `id`, the agent's
`friction` text, and `call`: the chat, run, provider, and the provider's id
for the moment it was logged. `outcomes.jsonl` and earlier reports in
`reports/` (both in `$APP_STORAGE_DIR`) hold what past runs concluded and what
the owner decided; friction that recurs after a fix is evidence about that fix.

## 2. Find the cause (read-only)

Group entries that share a cause. Verify what actually happened against the
owning source: the transcript, code, skills, prompts, and logs. Never explain
an entry away from assumption. First check whether the problem still exists:
a later change may already have fixed it.

Coach the logging agent only when the friction seems to come from its
instructions, or from a gap in them: fork it right after the call and ask why
it did, or did not do, what it did. Its answer shows whether a skill or prompt
misled it or was missing. The fork is read-only:

    python3 "$SCRIPTS_DIR/fork_chat.py" --json \
      --after-call '<the entry's call object as JSON>' <chat_id> "<question>"

If the fork is unavailable, read the transcript: `mapi "/api/chats/<chat_id>?limit=500"`.

For each cause worth changing, decide the simplest fix at the layer that owns
it. Prefer removing a stale rule, step, or tool over adding one; when a tool
and the instructions about it both need to change, they are one fix. If the
cause is still unclear, the fix is an experiment that would settle it. Do not
edit anything or prepare patches yet.

## 3. Prepare the report and outcomes

Entries that describe the agent's own task work rather than a Möbius cause
(a bug its tests caught, a wrong assumption about the task's own data) are
`explained` without further digging; data or tools owned by Möbius are a
Möbius cause. Entries whose cause an earlier report already asked about are
`joined` to that ask, so do not ask the owner the same thing twice; ask again
only when later entries bring materially stronger evidence. Read earlier
reports and `outcomes.jsonl` to see what was asked and decided.

Ask about at most three causes per run, the most valuable first. Leave
entries for any further causes unsettled: the next run takes them up. For the
entries this run covers, draft a plain Markdown report in a temporary file
outside `reports/`. Start with a `# ` headline, then what friction came in,
what you verified about each cause, and for each fix or experiment you propose:
what it would change and remove, and its effect on attention, quality, and
spend. State plainly that a proposal is not approved and the owner can request
it in chat later. Include resolved, joined, and explained entries too, so
their conclusions are visible.

In a separate temporary JSON file, list exactly the entries covered by the
report as an array of objects with `friction_id`, `outcome`, and `note`. Use
`asked` for a proposed fix or experiment, `resolved` when already fixed,
`joined` for an entry with the same cause (name the primary entry), and
`explained` when no change is worthwhile. The note says why. The report and
this list must agree; do not include entries reserved for a later run.

**Curate playbooks before publishing.** Agents leave playbooks in
`/data/shared/skills/playbook-*.md`: how to do a recurring task on this
instance, with exact calls and pitfalls. A new or changed one starts its
description with `(draft)` until it is checked. Each report records the
commit its playbook check started from, and the next run continues from
there. Note the current commit first, then list what changed since the
previous checkpoint, including unsaved and new files:

    SCANNED=$(git -C /data rev-parse HEAD)
    LAST=$(git -C /data log -1 --name-only --format= -- "apps/$APP_ID/reports" | grep '\.md$' | head -1)
    BASE=$(sed -n 's/.*Playbooks checked through[^0-9a-f]*\([0-9a-f]\{40\}\).*/\1/p' "/data/$LAST" | tail -1)
    git -C /data diff --name-only "$BASE" -- 'shared/skills/playbook-*.md'
    git -C /data ls-files --others --exclude-standard -- 'shared/skills/playbook-*.md'

If `BASE` is empty (no earlier checkpoint), check every
`/data/shared/skills/playbook-*.md`. End the report draft with the line
`Playbooks checked through <SCANNED>`, using the full commit id, so a playbook
committed while this run works is checked next time.

Verify each one's calls, paths, and claimed outputs against the live source
with read-only checks, as you would a friction cause. When a draft checks out,
remove its `(draft)` label. That records a source-checked observation
and is the only edit this step makes. Propose every other change in the
report: a wrong step, stale state such as versions or counts, overlapping
playbooks to merge, and playbooks whose procedure no longer exists or that a
shipped skill now covers. Agents that use a playbook correct it in place
meanwhile. Keep the set small: the session skill list is capped and sorted by
name, so a growing pile of playbooks pushes shipped skills out of every
chat's list. When a playbook records something a shipped skill should say,
propose that skill fix. When a friction entry's cause is a procedure agents
keep rediscovering, propose the playbook it needs;
`mapi "/api/chats/search?q=<words>"` finds other chats that did the same task.
Add every label you removed and every proposal to the report draft (file,
what, why), or say in one line that none changed.

Publish the complete report and those outcomes in one short handoff:

    python3 "$APP_SOURCE_DIR/friction_queue.py" publish "$APP_STORAGE_DIR" \
      <path-to-complete-report-draft> <path-to-outcomes-json>

This creates `reports/$CHAT_ID.md` before outcomes are recorded. It rechecks
the queue under a short lock: if another run already covered an entry, refresh
the report and outcome list before retrying. If publication fails for any other
reason, fix it before ending. If this run already published some outcomes,
do not replace its report; any uncovered entries remain for a later run.
Do not write directly to the final report or outcomes paths. The report is
the handoff; without it, an old outcome remains pending for the next run.

## 4. Finish the handoff

Commit the report, outcomes, and any playbooks whose draft label you removed,
naming exactly those paths:

    pm-commit --from "$(git -C /data rev-parse HEAD)" 'reflection: <what>' -- \
      "apps/$APP_ID/outcomes.jsonl" "apps/$APP_ID/reports/$CHAT_ID.md" \
      [shared/skills/playbook-<topic>.md ...]

Send one `notify_owner` notification with the headline as its body and
`/shell/?app=$APP_ID` as its target, since this chat is reached through the
app. End with a short summary, not a question or card. An unanswered proposal
never holds this run open or blocks the next scheduled run. If the owner later
asks for a fix, that is a separate, explicitly authorized task: verify the
problem still exists before changing its live source, and obtain separate
approval for any restart or public action.
