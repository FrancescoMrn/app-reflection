"""Only checked playbooks go into use, and each check resumes where the last stopped."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import playbook_queue  # noqa: E402

STAGED = "shared/skills/.playbooks-pending"
LIVE = "shared/skills"


def _git(data, *args):
  return subprocess.run(["git", "-C", str(data), *args], check=True,
                        capture_output=True, text=True).stdout.strip()


def _write(data, path, text="# Playbook\n"):
  target = data / path
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_text(text)
  return path


def _commit(data, *paths):
  # The way pm-commit commits: exactly the named paths, removals included.
  _git(data, "add", "-N", "--", *paths)
  _git(data, "commit", "-q", "--only", "-m", "commit", "--", *paths)
  return _git(data, "rev-parse", "HEAD")


def _report(data, through, run="run-1"):
  _commit(data, _write(data, f"apps/7/reports/{run}.md",
                       f"# Report\n\nPlaybooks checked through {through}\n"))


def _scan(data):
  return playbook_queue.pending(data, data / "apps" / "7")


@pytest.fixture
def data(tmp_path):
  _git(tmp_path, "init", "-q")
  _git(tmp_path, "config", "user.name", "Test")
  _git(tmp_path, "config", "user.email", "test@example.com")
  _commit(tmp_path, _write(tmp_path, "README.md", "data\n"))
  return tmp_path


def test_a_folder_without_git_has_nothing_to_check(tmp_path):
  assert _scan(tmp_path) == {"through": None, "staged": [], "live": []}


def test_committing_a_staged_playbook_submits_it(data):
  path = _write(data, f"{STAGED}/playbook-deploy.md")
  assert _scan(data)["staged"] == []  # still being written
  _commit(data, path)
  assert _scan(data)["staged"] == [path]


def test_without_a_checkpoint_every_submitted_and_live_playbook_is_checked(data):
  staged = _write(data, f"{STAGED}/playbook-a.md")
  live = _write(data, f"{LIVE}/playbook-b.md")
  _commit(data, staged, live)
  written_directly = _write(data, f"{LIVE}/playbook-c.md")

  result = _scan(data)

  assert result == {"through": _git(data, "rev-parse", "HEAD"),
                    "staged": [staged], "live": [live, written_directly]}


def test_the_scan_resumes_from_the_newest_reports_checkpoint(data):
  _report(data, _commit(data, _write(data, f"{STAGED}/playbook-a.md")))
  second = _write(data, f"{STAGED}/playbook-b.md")
  _commit(data, second)
  assert _scan(data)["staged"] == [second]


def test_a_playbook_submitted_while_a_run_works_is_checked_next_time(data):
  through = _scan(data)["through"]  # the run starts its check
  late = _write(data, f"{STAGED}/playbook-late.md")
  _commit(data, late)  # submitted mid-run
  _report(data, through)  # the run's report lands afterwards
  assert _scan(data)["staged"] == [late]


def test_a_playbook_kept_staged_is_not_checked_again_until_it_changes(data):
  path = _write(data, f"{STAGED}/playbook-a.md")
  _report(data, _commit(data, path))
  assert _scan(data)["staged"] == []
  _commit(data, _write(data, path, "# Playbook, fixed\n"))
  assert _scan(data)["staged"] == [path]


def test_a_live_playbook_edited_outside_review_is_rechecked_before_any_commit(data):
  live = _write(data, f"{LIVE}/playbook-a.md")
  _report(data, _commit(data, live))
  assert _scan(data)["live"] == []
  _write(data, live, "# Rewritten in place\n")
  assert _scan(data)["live"] == [live]


def test_a_report_without_a_checkpoint_falls_back_to_the_previous_one(data):
  kept_staged = _write(data, f"{STAGED}/playbook-a.md")
  _report(data, _commit(data, kept_staged), run="run-1")
  _commit(data, _write(data, "apps/7/reports/run-2.md", "# Report that forgot its line\n"))
  assert _scan(data)["staged"] == []


def test_a_checkpoint_naming_an_unknown_commit_checks_everything(data):
  path = _write(data, f"{STAGED}/playbook-a.md")
  _commit(data, path)
  _report(data, "0" * 40)
  assert _scan(data)["staged"] == [path]


def test_only_playbooks_are_listed(data):
  _commit(data, _write(data, f"{LIVE}/memory.md"), _write(data, f"{STAGED}/notes.txt"),
          _write(data, f"{LIVE}/playbook-x/SKILL.md"))
  assert _scan(data)["staged"] == [] and _scan(data)["live"] == []


def test_promote_puts_a_submitted_playbook_into_use_as_one_committable_move(data):
  staged = _write(data, f"{STAGED}/playbook-a.md", "# Checked\n")
  _commit(data, staged)

  paths = playbook_queue.promote(data, str(data / staged))

  live = f"{LIVE}/playbook-a.md"
  assert paths == [staged, live]
  assert (data / live).read_text() == "# Checked\n"
  _commit(data, *paths)
  assert _git(data, "ls-files", "--", "shared") == live


def test_promote_replaces_the_live_playbook_it_improves(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Old\n")
  staged = _write(data, f"{STAGED}/playbook-a.md", "# Improved\n")
  _commit(data, live, staged)
  _commit(data, *playbook_queue.promote(data, staged))
  assert (data / live).read_text() == "# Improved\n"
  assert _git(data, "ls-files", "--", "shared") == live


def test_promote_takes_only_the_submitted_version(data):
  unsubmitted = _write(data, f"{STAGED}/playbook-new.md")
  with pytest.raises(ValueError, match="submitted"):
    playbook_queue.promote(data, unsubmitted)
  edited = _write(data, f"{STAGED}/playbook-a.md", "# Checked\n")
  _commit(data, edited)
  _write(data, edited, "# Changed after the check\n")
  with pytest.raises(ValueError, match="submitted"):
    playbook_queue.promote(data, edited)
  assert not (data / LIVE / "playbook-a.md").exists()


def test_promote_refuses_a_link_whose_target_could_change_after_review(data):
  target = _write(data, "elsewhere.md")
  link = f"{STAGED}/playbook-a.md"
  (data / STAGED).mkdir(parents=True)
  (data / link).symlink_to(data / target)
  _commit(data, target, link)
  with pytest.raises(ValueError, match="link"):
    playbook_queue.promote(data, link)
  assert not (data / LIVE / "playbook-a.md").exists()


@pytest.mark.parametrize("move, path", [
  ("promote", f"{LIVE}/playbook-a.md"),  # already in use
  ("promote", f"{STAGED}/../memory.md"),  # leaves the pending folder
  ("promote", f"{STAGED}/notes.md"),  # not a playbook
  ("demote", f"{STAGED}/playbook-a.md"),  # not in use
  ("demote", f"{LIVE}/memory.md"),  # a shipped skill
])
def test_moves_stay_between_the_pending_and_live_playbook_folders(data, move, path):
  with pytest.raises(ValueError, match="not a"):
    getattr(playbook_queue, move)(data, path)


def test_demote_takes_a_live_playbook_out_of_every_skill_list(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Wrong step\n")
  _commit(data, live)

  paths = playbook_queue.demote(data, live)

  staged = f"{STAGED}/playbook-a.md"
  assert paths == [live, staged]
  assert (data / staged).read_text() == "# Wrong step\n"
  _commit(data, *paths)
  assert _git(data, "ls-files", "--", "shared") == staged


def test_demote_leaves_a_waiting_staged_copy_for_review(data):
  live = _write(data, f"{LIVE}/playbook-a.md", "# Wrong\n")
  staged = _write(data, f"{STAGED}/playbook-a.md", "# Fixed copy\n")
  _commit(data, live, staged)
  assert playbook_queue.demote(data, live) == [live]
  assert not (data / live).exists()
  assert (data / staged).read_text() == "# Fixed copy\n"


def test_demote_names_only_the_staged_path_for_a_playbook_git_never_tracked(data):
  live = _write(data, f"{LIVE}/playbook-a.md")
  assert playbook_queue.demote(data, live) == [f"{STAGED}/playbook-a.md"]


def test_cli_prints_the_scan_and_reports_refusals(data, capsys):
  assert playbook_queue.main(["playbook_queue.py", "pending", str(data), str(data / "apps" / "7")]) == 0
  assert json.loads(capsys.readouterr().out)["through"] == _git(data, "rev-parse", "HEAD")
  assert playbook_queue.main(["playbook_queue.py", "promote", str(data), f"{LIVE}/memory.md"]) == 1
  assert "not a staged playbook" in capsys.readouterr().err
