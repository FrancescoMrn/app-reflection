#!/usr/bin/env python3
"""The playbook review queue: playbooks agents staged for Reflection to check.

Agents write playbooks into ``shared/skills/.playbooks-pending/`` in the Möbius
data checkout (``<data-dir>``, its root); committing one there submits it. The skill loader skips
dot-folders, so a staged playbook is never live guidance. Only a Reflection run
moves a checked playbook into ``shared/skills/``, where every chat's skill list
includes it, so the folder a playbook is in, not anything written inside it,
records whether it was checked.

Each Reflection report ends with ``Playbooks checked through <commit>``; the
next scan starts from the newest committed report's checkpoint, so a playbook
submitted while a run works is checked by the next one.

  playbook_queue.py pending <data-dir> <storage-dir>
      Print the commit this check covers (``through``), the staged playbooks
      submitted since the last checkpoint (``staged``), and the live playbooks
      changed since then (``live``) as JSON.
  playbook_queue.py promote <data-dir> <staged-path>
      Move a checked staged playbook into use, replacing a live one of the
      same name, and print the paths to commit.
  playbook_queue.py demote <data-dir> <live-path>
      Take a live playbook out of every skill list, back into the pending
      folder, and print the paths to commit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

LIVE = "shared/skills"
STAGED = f"{LIVE}/.playbooks-pending"
_LIVE_PLAYBOOK = re.compile(rf"{re.escape(LIVE)}/playbook-[^/]+\.md")
_STAGED_PLAYBOOK = re.compile(rf"{re.escape(STAGED)}/playbook-[^/]+\.md")
_CHECKPOINT = re.compile(r"Playbooks checked through\W*([0-9a-f]{40})\b")


def _git(data: Path, *args: str) -> subprocess.CompletedProcess:
  # Scans run beside agents that are committing: never take Git's optional
  # index lock, or a concurrent commit could fail on it.
  return subprocess.run(
    ["git", "--no-optional-locks", "-c", "core.quotePath=false", "-C", str(data), *args],
    capture_output=True, text=True, check=False,
  )


def _names(data: Path, *args: str) -> list[str]:
  result = _git(data, *args)
  if result.returncode:
    raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
  return result.stdout.splitlines()


def _tracked(data: Path, path: str) -> bool:
  return _git(data, "ls-files", "--error-unmatch", "--", path).returncode == 0


def _checkpoint(data: Path, storage: Path) -> str | None:
  """The newest commit a committed report says its playbook check covered.

  A report that lacks a usable checkpoint falls back to an earlier one rather
  than to checking everything, so one forgotten line cannot make every
  playbook kept staged start a run each day.
  """
  reports = os.path.relpath(storage / "reports", data)
  log = _git(data, "log", "--name-only", "--format=", "--", reports)
  if log.returncode:
    return None
  for name in dict.fromkeys(line for line in log.stdout.splitlines() if line.endswith(".md")):
    try:
      found = _CHECKPOINT.findall((data / name).read_text(encoding="utf-8"))
    except OSError:
      continue  # Removed since it was committed.
    if found and not _git(data, "cat-file", "-e", f"{found[-1]}^{{commit}}").returncode:
      return found[-1]
  return None


def _present(data: Path, names: list[str], shape: re.Pattern) -> list[str]:
  return sorted({name for name in names if shape.fullmatch(name) and (data / name).is_file()})


def pending(data: Path, storage: Path) -> dict:
  if not (data / ".git").exists():
    # Not a checkout: nothing was ever submitted, and there is no history to
    # check against.
    return {"through": None, "staged": [], "live": []}
  through = _names(data, "rev-parse", "--verify", "HEAD")[0]
  base = _checkpoint(data, storage)
  if base:
    # Committing submits a staged playbook, so only commits count for those;
    # a live playbook counts as changed even before anyone commits the edit.
    staged = _names(data, "diff", "--name-only", base, through, "--", STAGED)
    live = (_names(data, "diff", "--name-only", base, "--", LIVE)
            + _names(data, "ls-files", "--others", "--exclude-standard", "--", LIVE))
  else:
    staged = _names(data, "ls-files", "--", STAGED)
    live = _names(data, "ls-files", "--cached", "--others", "--exclude-standard", "--", LIVE)
  return {
    "through": through,
    "staged": _present(data, staged, _STAGED_PLAYBOOK),
    "live": _present(data, live, _LIVE_PLAYBOOK),
  }


def _relative(data: Path, path: str) -> str:
  return os.path.relpath(path, data) if os.path.isabs(path) else os.path.normpath(path)


def promote(data: Path, path: str) -> list[str]:
  staged = _relative(data, path)
  if not _STAGED_PLAYBOOK.fullmatch(staged):
    raise ValueError(f"not a staged playbook: {path}")
  if (data / staged).is_symlink():
    # A link's target could change after review and go live unchecked.
    raise ValueError(f"{staged} is a link; a playbook must be a regular file")
  if not _tracked(data, staged) or _git(data, "diff", "--quiet", "HEAD", "--", staged).returncode:
    # Only the committed version was submitted, so only that may go live.
    raise ValueError(f"{staged} is not the version that was submitted; it is checked once committed")
  live = f"{LIVE}/{Path(staged).name}"
  os.replace(data / staged, data / live)
  return [staged, live]


def demote(data: Path, path: str) -> list[str]:
  live = _relative(data, path)
  if not _LIVE_PLAYBOOK.fullmatch(live):
    raise ValueError(f"not a live playbook: {path}")
  staged = f"{STAGED}/{Path(live).name}"
  commit = [live] if _tracked(data, live) else []
  if (data / staged).exists():
    # A newer copy already waits for review; it supersedes the live one.
    (data / live).unlink()
    return commit
  (data / STAGED).mkdir(parents=True, exist_ok=True)
  os.replace(data / live, data / staged)
  return [*commit, staged]


def main(argv: list[str]) -> int:
  try:
    if len(argv) == 4 and argv[1] == "pending":
      result = pending(Path(argv[2]), Path(argv[3]))
    elif len(argv) == 4 and argv[1] == "promote":
      result = {"commit": promote(Path(argv[2]), argv[3])}
    elif len(argv) == 4 and argv[1] == "demote":
      result = {"commit": demote(Path(argv[2]), argv[3])}
    else:
      print(__doc__.strip(), file=sys.stderr)
      return 2
  except (OSError, RuntimeError, ValueError) as exc:
    print(f"playbook_queue: {exc}", file=sys.stderr)
    return 1
  print(json.dumps(result, ensure_ascii=False, indent=1))
  return 0


if __name__ == "__main__":
  raise SystemExit(main(sys.argv))
