"""Only reviewed playbook content goes into use, and every other version waits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playbook_queue  # noqa: E402

STAGED = "shared/skills/.playbooks-pending"
LIVE = "shared/skills"
LEDGER = "apps/7/playbooks.jsonl"


def _git(data, *args):
  return subprocess.run(["git", "-C", str(data), *args], check=True,
                        capture_output=True, text=True).stdout.strip()


def _write(data, path, text="# Playbook\n"):
  target = data / path
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_text(text)
  return path


def _playbook(path, body="# Steps\n"):
  return f"---\nname: {Path(path).stem}\ndescription: When to use it\n---\n{body}"


def _commit(data, *paths):
  # The way pm-commit commits: exactly the named paths, removals included.
  _git(data, "add", "-N", "--", *paths)
  _git(data, "commit", "-q", "--only", "-m", "commit", "--", *paths)


def _blob(data, path):
  return _git(data, "hash-object", "--", path)


def _storage(data):
  return data / "apps" / "7"


def _scan(data):
  return playbook_queue.pending(data, _storage(data))


def _paths(entries):
  return [entry["path"] for entry in entries]


def _verdicts(data):
  lines = (data / LEDGER).read_text().splitlines()
  return [json.loads(line)["verdict"] for line in lines if line.endswith("}")]


@pytest.fixture
def data(tmp_path):
  _git(tmp_path, "init", "-q")
  _git(tmp_path, "config", "user.name", "Test")
  _git(tmp_path, "config", "user.email", "test@example.com")
  _commit(tmp_path, _write(tmp_path, "README.md", "data\n"))
  return tmp_path


def test_a_folder_without_git_has_nothing_to_check(tmp_path):
  assert _scan(tmp_path) == {"staged": [], "live": []}


def test_committing_a_staged_playbook_submits_that_version(data):
  path = _write(data, f"{STAGED}/playbook-deploy.md")
  assert _scan(data)["staged"] == []  # still being written
  _commit(data, path)
  assert _scan(data)["staged"] == [{"path": path, "blob": _blob(data, path)}]


def test_a_held_version_is_not_listed_again_until_it_changes(data):
  path = _write(data, f"{STAGED}/playbook-a.md")
  _commit(data, path)
  playbook_queue.hold(data, _storage(data), path, _blob(data, path))
  assert _scan(data)["staged"] == []
  _commit(data, _write(data, path, "# Playbook, fixed\n"))
  assert _paths(_scan(data)["staged"]) == [path]


def test_a_promoted_version_is_in_use_and_starts_no_further_review(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md", "# Checked\n"))
  _commit(data, staged)

  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))

  assert (data / LIVE / "playbook-a.md").read_text() == _playbook("playbook-a.md", "# Checked\n")
  assert (data / LIVE / "playbook-a.md").stat().st_mode & 0o777 == 0o644  # like any skill file
  assert not (data / staged).exists()
  assert _scan(data) == {"staged": [], "live": []}
  assert _verdicts(data) == ["promoted"]


def test_promotion_installs_the_checked_version_when_a_newer_one_was_committed_since(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md", "# Checked version\n"))
  _commit(data, staged)
  (checked,) = _scan(data)["staged"]  # Reflection reads this version
  _commit(data, _write(data, staged, "# Newer, unreviewed version\n"))  # committed mid-run

  playbook_queue.promote(data, _storage(data), staged, checked["blob"])

  assert (data / LIVE / "playbook-a.md").read_text() == _playbook("playbook-a.md", "# Checked version\n")
  assert (data / staged).read_text() == "# Newer, unreviewed version\n"
  assert _paths(_scan(data)["staged"]) == [staged]  # queued for the next review


def test_a_playbook_written_directly_in_the_live_folder_waits_for_review(data):
  live = _write(data, f"{LIVE}/playbook-direct.md", _playbook("playbook-direct.md"))
  assert _scan(data)["live"] == [{"path": live, "blob": _blob(data, live)}]
  playbook_queue.promote(data, _storage(data), live, _blob(data, live))  # approved in place
  assert _scan(data)["live"] == []


def test_editing_a_promoted_playbook_in_place_puts_it_back_in_review(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md"))
  _commit(data, staged)
  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  live = _write(data, f"{LIVE}/playbook-a.md", "# Rewritten in place\n")
  assert _paths(_scan(data)["live"]) == [live]


def test_a_live_approval_refuses_content_changed_since_the_check(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Checked\n")
  checked = _blob(data, live)
  _write(data, live, "# Changed after the check\n")
  with pytest.raises(ValueError, match="changed since it was checked"):
    playbook_queue.promote(data, _storage(data), live, checked)
  assert not (data / LEDGER).exists()


def test_a_demoted_version_put_back_in_use_waits_for_review(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md", "# Wrong step\n"))
  _commit(data, staged)
  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  playbook_queue.demote(data, _storage(data), f"{LIVE}/playbook-a.md")
  live = _write(data, f"{LIVE}/playbook-a.md", _playbook("playbook-a.md", "# Wrong step\n"))  # put back
  assert _paths(_scan(data)["live"]) == [live]


def test_a_linked_live_playbook_is_checked_by_the_text_it_serves(data):
  target = _write(data, "elsewhere.md", _playbook("playbook-a.md", "# Reviewed\n"))
  live = data / LIVE / "playbook-a.md"
  live.parent.mkdir(parents=True)
  live.symlink_to(data / target)
  playbook_queue.promote(data, _storage(data), f"{LIVE}/playbook-a.md", _blob(data, target))
  assert _scan(data)["live"] == []
  _write(data, target, "# Changed behind the link\n")
  assert _paths(_scan(data)["live"]) == [f"{LIVE}/playbook-a.md"]


def test_only_regular_playbook_files_are_listed(data):
  target = _write(data, "elsewhere.md")
  (data / STAGED).mkdir(parents=True)
  (data / STAGED / "playbook-link.md").symlink_to(data / target)
  _commit(data, target, f"{STAGED}/playbook-link.md", _write(data, f"{STAGED}/notes.txt"),
          _write(data, f"{LIVE}/playbook-x/SKILL.md"))
  _commit(data, _write(data, f"{LIVE}/memory.md"))
  assert _scan(data) == {"staged": [], "live": []}


def test_the_printed_paths_commit_the_way_pm_commit_does(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md"))
  _commit(data, staged)

  paths = playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))

  assert paths == [f"{LIVE}/playbook-a.md", LEDGER, staged]
  _commit(data, *paths)
  assert _git(data, "ls-files", "--", "shared", "apps").split() == [LEDGER, f"{LIVE}/playbook-a.md"]


@pytest.mark.parametrize("move, path", [
  ("promote", f"{STAGED}/../memory.md"),  # leaves the playbook folders
  ("promote", f"{STAGED}/notes.md"),  # not a playbook
  ("hold", f"{LIVE}/playbook-a.md"),  # only staged versions are held
  ("demote", f"{STAGED}/playbook-a.md"),  # not in use
  ("demote", f"{LIVE}/memory.md"),  # a shipped skill
])
def test_moves_stay_between_the_pending_and_live_playbook_folders(data, move, path):
  args = (path,) if move == "demote" else (path, "0" * 40)
  with pytest.raises(ValueError, match="not a"):
    getattr(playbook_queue, move)(data, _storage(data), *args)


def test_promote_needs_content_that_exists_in_this_checkout(data):
  with pytest.raises(ValueError, match="never submitted"):
    playbook_queue.promote(data, _storage(data), f"{STAGED}/playbook-a.md", "0" * 40)
  with pytest.raises(ValueError, match="not a blob id"):
    playbook_queue.promote(data, _storage(data), f"{STAGED}/playbook-a.md", "HEAD")


def test_demote_takes_a_live_playbook_out_of_every_skill_list(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Wrong step\n")
  _commit(data, live)

  paths = playbook_queue.demote(data, _storage(data), live)

  staged = f"{STAGED}/playbook-a.md"
  assert paths == [live, staged, LEDGER]
  assert (data / staged).read_text() == "# Wrong step\n"
  assert _verdicts(data) == ["demoted"]
  _commit(data, *paths)
  assert _git(data, "ls-files", "--", "shared") == staged


def test_demote_leaves_a_waiting_staged_copy_for_review(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Wrong\n")
  staged = _write(data, f"{STAGED}/playbook-a.md", "# Fixed copy\n")
  _commit(data, live, staged)
  assert playbook_queue.demote(data, _storage(data), live) == [live, LEDGER]
  assert not (data / live).exists()
  assert (data / staged).read_text() == "# Fixed copy\n"


def test_demote_names_only_the_staged_path_for_a_playbook_git_never_tracked(data):
  live = _write(data, f"{LIVE}/playbook-a.md")
  assert playbook_queue.demote(data, _storage(data), live) == [f"{STAGED}/playbook-a.md", LEDGER]


def test_a_partly_written_ledger_line_is_left_for_the_next_read(data):
  staged = _write(data, f"{STAGED}/playbook-a.md")
  _commit(data, staged)
  playbook_queue.hold(data, _storage(data), staged, _blob(data, staged))
  with (data / LEDGER).open("a") as ledger:
    ledger.write('{"blob": "abc", "verd')
  assert _scan(data)["staged"] == []
  playbook_queue.hold(data, _storage(data), staged, _blob(data, staged))
  assert _verdicts(data)[-1] == "held"


def test_cli_prints_the_queue_and_reports_refusals(data, capsys):
  storage = str(_storage(data))
  assert playbook_queue.main(["playbook_queue.py", "pending", str(data), storage]) == 0
  assert json.loads(capsys.readouterr().out) == {"staged": [], "live": []}
  assert playbook_queue.main(["playbook_queue.py", "hold", str(data), storage,
                              f"{LIVE}/memory.md", "0" * 40]) == 1
  assert "not a staged playbook" in capsys.readouterr().err


def test_promote_refuses_content_never_submitted_under_that_name(data):
  # Any object in the store, e.g. text removed long ago, must not become a playbook.
  stray = subprocess.run(["git", "-C", str(data), "hash-object", "-w", "--stdin"], input="secret-text\n",
                         check=True, capture_output=True, text=True).stdout.strip()
  with pytest.raises(ValueError, match="never submitted"):
    playbook_queue.promote(data, _storage(data), f"{STAGED}/playbook-nonexistent.md", stray)
  other = _write(data, f"{STAGED}/playbook-other.md", _playbook("playbook-a.md"))
  _commit(data, other)  # submitted under another name
  with pytest.raises(ValueError, match="never submitted"):
    playbook_queue.promote(data, _storage(data), f"{STAGED}/playbook-a.md", _blob(data, other))
  assert not list((data / LIVE).glob("playbook-*.md"))


@pytest.mark.parametrize("name", ["playbook-a';touch PWNED;'.md", "playbook-$(id).md", "playbook- a.md"])
def test_names_a_shell_could_misread_are_never_listed_or_moved(data, name):
  staged = _write(data, f"{STAGED}/{name}", _playbook(name))
  _commit(data, staged)
  _write(data, f"{LIVE}/{name}", _playbook(name))
  assert _scan(data) == {"staged": [], "live": []}
  with pytest.raises(ValueError, match="not a"):
    playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  with pytest.raises(ValueError, match="not a"):
    playbook_queue.demote(data, _storage(data), f"{LIVE}/{name}")


def test_a_damaged_ledger_line_does_not_switch_review_off(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md"))
  _commit(data, staged)
  (data / LEDGER).parent.mkdir(parents=True, exist_ok=True)
  (data / LEDGER).write_bytes(b'{"path": "shared/skills/playbook-\xc3')  # cut mid-character
  assert _paths(_scan(data)["staged"]) == [staged]
  playbook_queue.hold(data, _storage(data), staged, _blob(data, staged))
  assert _scan(data)["staged"] == []
  assert (data / LEDGER).read_bytes().splitlines()[-1].isascii()


@pytest.mark.parametrize("front", [
  "---\nname: memory\ndescription: Poses as a shipped skill\n---\n",
  "---\ndescription: No name at all\n---\n",
  "# No front matter\n",
])
def test_promote_requires_the_playbook_to_name_itself(data, front):
  staged = _write(data, f"{STAGED}/playbook-a.md", front + "# Steps\n")
  _commit(data, staged)
  with pytest.raises(ValueError, match="must declare name: playbook-a"):
    playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  live = _write(data, f"{LIVE}/playbook-b.md", front.replace("playbook-a", "playbook-b"))
  with pytest.raises(ValueError, match="must declare name: playbook-b"):
    playbook_queue.promote(data, _storage(data), live, _blob(data, live))
  assert not (data / LIVE / "playbook-a.md").exists()
  assert not (data / LEDGER).exists()


def test_a_name_git_cannot_decode_does_not_stop_the_scan(data):
  odd = os.fsdecode(f"{STAGED}/bad".encode() + b"\xff.md")
  _commit(data, _write(data, odd, "x\n"))
  live = _write(data, f"{LIVE}/playbook-direct.md", _playbook("playbook-direct.md"))
  assert _scan(data) == {"staged": [], "live": [{"path": live, "blob": _blob(data, live)}]}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_an_unreadable_live_playbook_does_not_stop_the_scan(data):
  unreadable = data / _write(data, f"{LIVE}/playbook-locked.md")
  unreadable.chmod(0)
  try:
    live = _write(data, f"{LIVE}/playbook-direct.md", _playbook("playbook-direct.md"))
    assert _paths(_scan(data)["live"]) == [live]  # the loader cannot serve the locked one either
  finally:
    unreadable.chmod(0o644)


def test_a_version_replaced_by_a_promotion_is_reviewed_again_if_put_back(data):
  staged = f"{STAGED}/playbook-a.md"
  _commit(data, _write(data, staged, _playbook("playbook-a.md", "# v1 with a wrong step\n")))
  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  _commit(data, _write(data, staged, _playbook("playbook-a.md", "# v2, fixed\n")))
  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  live = _write(data, f"{LIVE}/playbook-a.md", _playbook("playbook-a.md", "# v1 with a wrong step\n"))
  assert _paths(_scan(data)["live"]) == [live]


def test_promoted_content_copied_under_another_name_waits_for_review(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md"))
  _commit(data, staged)
  playbook_queue.promote(data, _storage(data), staged, _blob(data, staged))
  copy = _write(data, f"{LIVE}/playbook-copy.md", (data / LIVE / "playbook-a.md").read_text())
  assert _paths(_scan(data)["live"]) == [copy]
  with pytest.raises(ValueError, match="must declare name: playbook-copy"):
    playbook_queue.promote(data, _storage(data), copy, _blob(data, copy))


def test_holding_a_version_an_overlapping_run_promoted_takes_it_out_of_use(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md"))
  _commit(data, staged)
  blob = _blob(data, staged)
  playbook_queue.promote(data, _storage(data), staged, blob)  # run A

  paths = playbook_queue.hold(data, _storage(data), staged, blob)  # run B disagrees

  assert not (data / LIVE / "playbook-a.md").exists()
  assert (data / staged).read_text() == _playbook("playbook-a.md")
  assert paths == [staged, LEDGER]
  assert _scan(data) == {"staged": [], "live": []}


def test_a_git_attribute_cannot_make_edited_text_look_reviewed(data):
  _commit(data, _write(data, ".gitattributes", "*.md ident\n"))
  staged = _write(data, f"{STAGED}/playbook-a.md", _playbook("playbook-a.md", "$Id$\n"))
  _commit(data, staged)
  (blob,) = (entry["blob"] for entry in _scan(data)["staged"])
  playbook_queue.promote(data, _storage(data), staged, blob)
  live = _write(data, f"{LIVE}/playbook-a.md", _playbook("playbook-a.md", "$Id: run anything $\n"))
  assert _paths(_scan(data)["live"]) == [live]
