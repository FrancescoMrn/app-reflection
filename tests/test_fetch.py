"""The daily job opens a Reflection chat only when friction or a submitted playbook is pending."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

FETCH = Path(__file__).resolve().parents[1] / "fetch.sh"


@pytest.fixture
def api():
  requests = []

  class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
      body = self.rfile.read(int(self.headers["Content-Length"]))
      requests.append((self.path, self.headers["Authorization"], json.loads(body)))
      self.send_response(200)
      self.end_headers()
      self.wfile.write(b"{}")

    def log_message(self, *_args):
      pass

  server = HTTPServer(("127.0.0.1", 0), Handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  yield f"http://127.0.0.1:{server.server_port}", requests
  server.shutdown()


def _run(tmp_path, base_url):
  return subprocess.run(
    ["bash", str(FETCH), "7"],
    env={**os.environ, "DATA_DIR": str(tmp_path), "API_BASE_URL": base_url,
         "APP_TOKEN": "app-token"},
    capture_output=True, text=True, check=False,
  )


def test_no_pending_friction_starts_nothing(tmp_path, api):
  base_url, requests = api
  (tmp_path / "apps" / "7").mkdir(parents=True)

  assert _run(tmp_path, base_url).returncode == 0
  assert requests == []


def test_pending_friction_opens_one_chat_per_run(tmp_path, api):
  base_url, requests = api
  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  (storage / "friction.jsonl").write_text(
    json.dumps({"id": "a", "friction": "x"}) + "\n"
    + json.dumps({"id": "b", "friction": "y"}) + "\n"
  )
  (storage / "outcomes.jsonl").write_text(
    json.dumps({"friction_id": "a", "outcome": "explained"}) + "\n"
  )

  result = _run(tmp_path, base_url)

  assert result.returncode == 0, result.stderr
  ((path, auth, body),) = requests
  assert path == "/api/app-chats/start"
  assert auth == "Bearer app-token"
  assert body["scope"].startswith("run-")
  assert body["title"].startswith("Reflection — ")
  assert "owner_visible" not in body  # runs live in the app, not the chat list
  assert "provider" not in body and "model" not in body
  assert body["content"].startswith("Friction entries waiting for an outcome: 1.")
  assert "`reflection` skill" in body["content"]


def test_a_reportless_prior_outcome_is_retried_by_the_daily_job(tmp_path, api):
  base_url, requests = api
  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  (storage / "friction.jsonl").write_text(json.dumps({"id": "a", "friction": "x"}) + "\n")
  (storage / "outcomes.jsonl").write_text(json.dumps({
    "friction_id": "a", "outcome": "asked", "run": "interrupted-run",
  }) + "\n")

  assert _run(tmp_path, base_url).returncode == 0
  assert len(requests) == 1
  assert requests[0][2]["content"].startswith("Friction entries waiting for an outcome: 1.")


def test_a_later_run_the_same_day_is_not_swallowed_by_an_earlier_one(tmp_path, api):
  base_url, requests = api
  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  (storage / "friction.jsonl").write_text(json.dumps({"id": "a", "friction": "x"}) + "\n")

  _run(tmp_path, base_url)
  time.sleep(1.1)
  _run(tmp_path, base_url)

  first, second = (body["scope"] for _path, _auth, body in requests)
  assert first != second


def test_the_owners_chosen_agent_runs_the_chat(tmp_path, api):
  base_url, requests = api
  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  (storage / "friction.jsonl").write_text(json.dumps({"id": "a", "friction": "x"}) + "\n")
  (storage / "settings.json").write_text(json.dumps({"provider": "codex", "model": "gpt-5.6-sol"}))

  _run(tmp_path, base_url)

  ((_path, _auth, body),) = requests
  assert (body["provider"], body["model"]) == ("codex", "gpt-5.6-sol")


def _git(data, *args):
  return subprocess.run(["git", "-C", str(data), *args], check=True,
                        capture_output=True, text=True).stdout.strip()


def _commit(data, path, text):
  (data / path).parent.mkdir(parents=True, exist_ok=True)
  (data / path).write_text(text)
  _git(data, "add", "--", path)
  _git(data, "commit", "-q", "-m", "commit")
  return _git(data, "rev-parse", "HEAD")


def _verdict(data, path, verdict):
  # What Reflection's review records for the exact content at `path`.
  blob = _git(data, "hash-object", "--", path)
  with (data / "apps" / "7" / "playbooks.jsonl").open("a") as ledger:
    ledger.write(json.dumps({"path": path, "blob": blob, "verdict": verdict}) + "\n")


def _submit_playbook(data):
  (data / "apps" / "7").mkdir(parents=True, exist_ok=True)
  _git(data, "init", "-q")
  _git(data, "config", "user.name", "Test")
  _git(data, "config", "user.email", "test@example.com")
  return _commit(data, "shared/skills/.playbooks-pending/playbook-a.md", "# Playbook\n")


def test_a_submitted_playbook_starts_a_run_without_friction(tmp_path, api):
  base_url, requests = api
  _submit_playbook(tmp_path)

  result = _run(tmp_path, base_url)

  assert result.returncode == 0, result.stderr
  ((_path, _auth, body),) = requests
  assert body["content"] == (
    "Staged playbooks waiting for review: 1. "
    "Work through them following the `reflection` skill."
  )


def test_a_playbook_written_directly_in_the_live_folder_starts_a_run(tmp_path, api):
  base_url, requests = api
  _submit_playbook(tmp_path)
  _verdict(tmp_path, "shared/skills/.playbooks-pending/playbook-a.md", "held")
  live = tmp_path / "shared" / "skills" / "playbook-direct.md"
  live.write_text("# Never reviewed\n")  # not even committed

  result = _run(tmp_path, base_url)

  assert result.returncode == 0, result.stderr
  ((_path, _auth, body),) = requests
  assert body["content"] == (
    "Live playbooks not yet reviewed: 1. "
    "Work through them following the `reflection` skill."
  )


def test_reviewed_playbooks_start_nothing(tmp_path, api):
  base_url, requests = api
  _submit_playbook(tmp_path)
  _verdict(tmp_path, "shared/skills/.playbooks-pending/playbook-a.md", "held")
  _commit(tmp_path, "shared/skills/playbook-b.md", "# In use\n")
  _verdict(tmp_path, "shared/skills/playbook-b.md", "promoted")

  assert _run(tmp_path, base_url).returncode == 0
  assert requests == []


def test_friction_and_playbooks_share_one_run(tmp_path, api):
  base_url, requests = api
  _submit_playbook(tmp_path)
  (tmp_path / "apps" / "7" / "friction.jsonl").write_text(
    json.dumps({"id": "a", "friction": "x"}) + "\n")

  _run(tmp_path, base_url)

  ((_path, _auth, body),) = requests
  assert body["content"].startswith(
    "Friction entries waiting for an outcome: 1. Staged playbooks waiting for review: 1.")


def test_a_failed_playbook_scan_never_holds_back_friction(tmp_path, api):
  base_url, requests = api
  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  (storage / "friction.jsonl").write_text(json.dumps({"id": "a", "friction": "x"}) + "\n")
  (tmp_path / ".git").write_text("not a repository\n")

  result = _run(tmp_path, base_url)

  assert result.returncode == 0, result.stderr
  assert "playbook_queue:" in result.stderr  # the scan's error stays in the job's log
  ((_path, _auth, body),) = requests
  assert body["content"].startswith("Friction entries waiting for an outcome: 1.")


def test_a_failed_playbook_scan_still_starts_a_run_that_says_so(tmp_path, api):
  base_url, requests = api
  (tmp_path / "apps" / "7").mkdir(parents=True)
  (tmp_path / ".git").write_text("not a repository\n")

  result = _run(tmp_path, base_url)

  assert result.returncode == 0, result.stderr
  ((_path, _auth, body),) = requests
  assert body["content"] == (
    "The playbook scan failed, so playbooks could not be listed. "
    "Work through them following the `reflection` skill."
  )
