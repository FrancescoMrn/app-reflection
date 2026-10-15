#!/usr/bin/env python3
"""The playbook review queue: playbooks agents staged for Reflection to check.

Agents write playbooks into ``shared/skills/.playbooks-pending/`` in the Möbius
data checkout (``<data-dir>``, its root); committing one there submits it. The
skill loader skips dot-folders, so a staged playbook is never live guidance.
Only a Reflection run puts a playbook into ``shared/skills/``, where every
chat's skill list includes it.

Reflection records each verdict in ``playbooks.jsonl`` in its own storage,
keyed by the playbook's path and the Git blob id of the exact content it
checked. That record, not anything written inside a playbook, says whether a
version was reviewed: a staged version waits until it has a verdict, and a
live playbook waits for review whenever its content is not the version
Reflection promoted under that name (written, edited, or copied in directly,
or a replaced or demoted version put back).

  playbook_queue.py pending <data-dir> <storage-dir>
      Print the staged and live playbooks waiting for review as JSON, each
      with the blob id of the content to check.
  playbook_queue.py promote <data-dir> <storage-dir> <path> <blob>
      Put exactly the checked content into use. A staged version must be one
      that was committed under that name; it is written into the live folder,
      and a newer staged revision stays queued. A live playbook is approved
      where it is only while it is still that content. Either way, its front
      matter must declare its own file name as its name.
  playbook_queue.py hold <data-dir> <storage-dir> <path> <blob>
      Keep a staged version that does not hold up out of use, taking it out
      of use if an overlapping run already promoted it; it is not listed
      again until it changes.
  playbook_queue.py demote <data-dir> <storage-dir> <path>
      Take a live playbook out of every skill list, back into the pending
      folder, and record its content as demoted.

promote, hold and demote print the paths to commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

LIVE = "shared/skills"
STAGED = f"{LIVE}/.playbooks-pending"
LEDGER = "playbooks.jsonl"
# Reflection pastes these paths into shell commands, so a name may hold only
# characters no shell treats specially; any other name is never listed or moved.
_NAME = r"playbook-[A-Za-z0-9][A-Za-z0-9._-]*\.md"
_LIVE_PLAYBOOK = re.compile(rf"{re.escape(LIVE)}/{_NAME}")
_STAGED_PLAYBOOK = re.compile(rf"{re.escape(STAGED)}/{_NAME}")
_BLOB_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _git(data: Path, *args: str) -> subprocess.CompletedProcess:
  # Scans run beside agents that are committing: never take Git's optional
  # index lock, or a concurrent commit could fail on it.
  return subprocess.run(
    ["git", "--no-optional-locks", "-c", "core.quotePath=false", "-C", str(data), *args],
    capture_output=True, check=False,
  )


def _out(data: Path, *args: str) -> str:
  result = _git(data, *args)
  if result.returncode:
    raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
  # A name Git cannot decode stays distinct and simply never matches a playbook
  # name, so one odd file cannot stop the whole scan.
  return result.stdout.decode("utf-8", "surrogateescape")


def _content_id(data: Path, path: str) -> str | None:
  """The blob id of the text the skill loader would serve, or None if it serves none.

  Follows a link, like the loader; a file Git cannot read is one the loader
  (the same user) cannot serve either.
  """
  if not (data / path).is_file():
    return None
  # Raw bytes, exactly as the loader reads them: no attribute or filter may make
  # changed text hash like the reviewed version.
  hashed = _git(data, "hash-object", "--no-filters", "--", path)
  return hashed.stdout.decode("ascii").strip() if hashed.returncode == 0 else None


def _tracked(data: Path, path: str) -> bool:
  return _git(data, "ls-files", "--error-unmatch", "--", path).returncode == 0


def _verdicts(storage: Path) -> tuple[dict[tuple[str, str], str], set[str]]:
  """The latest verdict for each (path, blob), and every blob with any verdict."""
  try:
    # A damaged line must not stop every later review: it just fails to parse.
    lines = (storage / LEDGER).read_text(encoding="utf-8", errors="replace").splitlines()
  except FileNotFoundError:
    return {}, set()
  latest: dict[tuple[str, str], str] = {}
  judged: set[str] = set()
  for line in lines:
    try:
      record = json.loads(line)
    except ValueError:
      continue  # A line still being appended; it completes on the next read.
    if isinstance(record, dict) and all(isinstance(record.get(key), str) for key in ("path", "blob")):
      latest[(record["path"], record["blob"])] = record.get("verdict")
      judged.add(record["blob"])
  return latest, judged


def pending(data: Path, storage: Path) -> dict:
  if not (data / ".git").exists():
    return {"staged": [], "live": []}  # Not a checkout: nothing was submitted.
  latest, judged = _verdicts(storage)
  staged = []
  # Committing submits a staged playbook, so its committed content is what
  # waits; a version that already has a verdict is not checked again.
  for entry in filter(None, _out(data, "ls-tree", "-r", "-z", "HEAD", "--", STAGED).split("\0")):
    meta, path = entry.split("\t", 1)
    mode, kind, blob = meta.split()
    if (kind == "blob" and mode.startswith("100") and _STAGED_PLAYBOOK.fullmatch(path)
        and (data / path).is_file() and blob not in judged):
      staged.append({"path": path, "blob": blob})
  # Live content counts as reviewed only while it is exactly the version
  # Reflection promoted under that name, so a direct write, an edit, a copy
  # under another name, or a replaced version put back waits as soon as it exists.
  live = []
  for entry in sorted((data / LIVE).glob("playbook-*.md")):
    path = f"{LIVE}/{entry.name}"
    blob = _content_id(data, path) if _LIVE_PLAYBOOK.fullmatch(path) else None
    if blob and latest.get((path, blob)) != "promoted":
      live.append({"path": path, "blob": blob})
  return {"staged": staged, "live": live}


def _record(data: Path, storage: Path, path: str, blob: str, verdict: str) -> str:
  storage.mkdir(parents=True, exist_ok=True)
  entry = {"path": path, "blob": blob, "verdict": verdict,
           "run": os.environ.get("CHAT_ID", ""),
           "at": datetime.now(UTC).isoformat(timespec="seconds")}
  with (storage / LEDGER).open("a+b") as ledger:
    fcntl.flock(ledger, fcntl.LOCK_EX)
    ledger.seek(0, os.SEEK_END)
    if ledger.tell():
      ledger.seek(-1, os.SEEK_END)
      if ledger.read(1) != b"\n":
        ledger.write(b"\n")  # Keep a crashed partial line from swallowing this one.
    ledger.write((json.dumps(entry) + "\n").encode("ascii"))
    ledger.flush()
    os.fsync(ledger.fileno())
  return os.path.relpath(storage / LEDGER, data)


def _relative(data: Path, path: str) -> str:
  return os.path.relpath(path, data) if os.path.isabs(path) else os.path.normpath(path)


def _checked_blob(blob: str) -> str:
  if not _BLOB_ID.fullmatch(blob):
    raise ValueError(f"not a blob id: {blob}")
  return blob


def _replace(target: Path, content: bytes) -> None:
  descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
  try:
    with os.fdopen(descriptor, "wb") as handle:
      handle.write(content)
      handle.flush()
      os.fchmod(handle.fileno(), 0o644)  # Readable like every other skill file.
      os.fsync(handle.fileno())
    os.replace(temporary, target)  # Replaces a link itself, never its target.
  finally:
    if os.path.exists(temporary):
      os.unlink(temporary)


def _submitted(data: Path, staged: str) -> set[str]:
  """Every regular-file version ever committed at this staged path."""
  log = _out(data, "log", "--format=", "--raw", "--no-abbrev", "--no-renames", "HEAD", "--", staged)
  versions = set()
  for line in log.splitlines():
    if line.startswith(":"):
      _, mode, _, blob, *_ = line.split("\t", 1)[0].split()
      if mode.startswith("100"):
        versions.add(blob)
  return versions


def _require_own_name(playbook: str, content: bytes) -> None:
  # Its front matter name is what every chat's skill list shows, so it must be
  # the playbook's own file name and can never pose as another skill.
  text = content.decode("utf-8", errors="replace")
  end = text.find("\n---", 3) if text.startswith("---\n") else -1
  front = text[4:end].splitlines() if end > 0 else []
  names = [value.strip().strip("'\"") for key, sep, value in (line.partition(":") for line in front)
           if sep and key.strip() == "name"]
  if names != [Path(playbook).stem]:
    raise ValueError(f"{playbook} must declare name: {Path(playbook).stem} in its front matter")


def promote(data: Path, storage: Path, path: str, blob: str) -> list[str]:
  playbook, blob = _relative(data, path), _checked_blob(blob)
  if _STAGED_PLAYBOOK.fullmatch(playbook):
    if blob not in _submitted(data, playbook):
      raise ValueError(f"{blob} was never submitted as {playbook}")
    content = _git(data, "cat-file", "blob", blob).stdout
    _require_own_name(playbook, content)
    live = f"{LIVE}/{Path(playbook).name}"
    replaced = _content_id(data, live)
    if replaced and replaced != blob:
      # Recorded first, so even a crash right after cannot leave the replaced
      # version counting as reviewed if it is ever put back.
      _record(data, storage, live, replaced, "superseded")
    # Write exactly the checked version: a revision committed after the check
    # must not reach the skill list unreviewed.
    _replace(data / live, content)
    commit = [live, _record(data, storage, live, blob, "promoted")]
    staged = data / playbook
    if not staged.is_symlink() and _content_id(data, playbook) == blob:
      staged.unlink()
      if _tracked(data, playbook):
        commit.append(playbook)
    return commit
  if _LIVE_PLAYBOOK.fullmatch(playbook):
    if _content_id(data, playbook) != blob:
      raise ValueError(f"{playbook} changed since it was checked; it waits for the next review")
    _require_own_name(playbook, (data / playbook).read_bytes())
    return [playbook, _record(data, storage, playbook, blob, "promoted")]
  raise ValueError(f"not a playbook: {path}")


def hold(data: Path, storage: Path, path: str, blob: str) -> list[str]:
  staged = _relative(data, path)
  if not _STAGED_PLAYBOOK.fullmatch(staged):
    raise ValueError(f"not a staged playbook: {path}")
  commit = [_record(data, storage, staged, _checked_blob(blob), "held")]
  live = f"{LIVE}/{Path(staged).name}"
  if _content_id(data, live) == blob:
    # An overlapping run already put this same version into use; a version
    # that does not hold up must not stay there.
    commit = [*demote(data, storage, live), *commit]
  return list(dict.fromkeys(commit))


def demote(data: Path, storage: Path, path: str) -> list[str]:
  live = _relative(data, path)
  if not _LIVE_PLAYBOOK.fullmatch(live):
    raise ValueError(f"not a live playbook: {path}")
  blob = _content_id(data, live)
  staged = f"{STAGED}/{Path(live).name}"
  commit = [live] if _tracked(data, live) else []
  if (data / staged).exists():
    # A newer copy already waits for review; it supersedes the live one.
    (data / live).unlink()
  else:
    (data / STAGED).mkdir(parents=True, exist_ok=True)
    os.replace(data / live, data / staged)
    commit.append(staged)
  # Recording the removed content keeps it from counting as reviewed if it is
  # put back; content the loader could not read has nothing to record.
  return [*commit, *([_record(data, storage, live, blob, "demoted")] if blob else [])]


def main(argv: list[str]) -> int:
  try:
    if len(argv) == 4 and argv[1] == "pending":
      result = pending(Path(argv[2]), Path(argv[3]))
    elif len(argv) == 6 and argv[1] in ("promote", "hold"):
      move = promote if argv[1] == "promote" else hold
      result = {"commit": move(Path(argv[2]), Path(argv[3]), argv[4], argv[5])}
    elif len(argv) == 5 and argv[1] == "demote":
      result = {"commit": demote(Path(argv[2]), Path(argv[3]), argv[4])}
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
