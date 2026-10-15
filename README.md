# Reflection

Möbius learns from the friction its agents actually hit, and keeps the
procedures they work out.

1. **Agents log friction as it happens.** Every chat gets Reflection's
   `log_friction` tool and a short note on when to use it. Each entry records
   the exact moment it was logged, so it can be revisited later.
2. **Once a day, if new friction or a submitted playbook is waiting,
   Reflection works through it in the background.** It traces each root
   cause, checks whether it has been fixed since, and asks the logging agent
   why it acted as it did when the friction seems to come from its
   instructions. Then it writes a report. On days with nothing new, nothing
   runs.
3. **It proposes, but never fixes, in a scheduled run.** Decisions to consider
   appear in the report; they do not hold the run open. You can ask for a
   specific fix later. Problems already fixed since are reported too.
4. **Agents stage playbooks for recurring work, and only checked ones are
   used.** After multi-step work that will likely come up again, an agent
   writes a short playbook into a pending folder that no chat's skill list
   includes, and commits it to submit it. The next run checks each submitted
   playbook against live source and moves the ones that hold up into the
   shared skills folder, where later agents find them; the rest stay staged
   with a proposed fix, and a live playbook that stops holding up is moved
   back out. Only Reflection moves playbooks into use, so the folder a
   playbook is in, not anything written inside it, records whether it was
   checked. Moving playbooks into or out of use is the only change a run makes
   on its own.

The app has three tabs: **Reports** (each run's report with its chat),
**Backlog** (waiting, fixes proposed, done, each linked to
the chat where it happened), and **Settings** (which agent runs it, the daily
run time, and run now).

## Pieces

| File | Role |
|---|---|
| `service.py` | The `log_friction` tool: appends to `friction.jsonl` in app storage |
| `reflection-core.md` | The note every chat gets: when to log friction, and how to stage a playbook |
| `fetch.sh` | Daily job: opens a Reflection chat when friction or a submitted playbook is waiting |
| `friction_queue.py` | Lists entries still without an outcome; records outcomes |
| `playbook_queue.py` | Lists playbooks to check since the last report; moves them into or out of use |
| `reflection.md` | What the Reflection chat does |
| `index.jsx`, `friction.js`, `schedule.js` | The app screen |

## Install

Open the **App Store** in Möbius, find **Reflection**, and tap **Install**, or
install from URL:

```
https://raw.githubusercontent.com/mobius-os/app-reflection/main/mobius.json
```

## License

MIT — see [LICENSE](LICENSE).
