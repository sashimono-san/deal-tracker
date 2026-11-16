import fcntl
import json
import sys
from pathlib import Path

import pytest

from deal_tracker.models import WatchSpec
from deal_tracker.runner import execute, run_once
from deal_tracker.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "tracker.sqlite3")


def test_offline_runner_roundtrip_has_honest_provenance(store):
    watch = store.create(WatchSpec(title="Local experiment"))
    command = [sys.executable, "-c", "import json, sys; payload=json.load(sys.stdin); print(json.dumps({'watch_version':payload['watch']['version'],'request_id':payload['request_id'],'outcome':'no_change'}))"]
    result = run_once(store, watch["id"], command)
    assert result["outcome"] == "no_change"
    assert result["source"] == "local_command"
    assert store.runs(watch["id"])[0]["result"]["outcome"] == "no_change"


@pytest.mark.parametrize("code, expected, reason", [
    ("while True: pass", "timed_out", "deadline"),
    ("print('x' * 150000)", "failed", "output limit"),
    ("import sys; sys.exit(7)", "failed", "status 7"),
    ("print('not json')", "failed", "invalid structured output"),
    ("print('{}')", "failed", "invalid structured output"),
])
def test_failures_are_bounded_and_persisted(store, code, expected, reason):
    watch = store.create(WatchSpec(title="Bounded experiment"))
    result = run_once(store, watch["id"], [sys.executable, "-c", code], timeout=1)
    assert result["outcome"] == expected
    assert reason in result["diagnostics"]
    assert len(store.runs(watch["id"])) == 1
    assert store.candidates(watch["id"]) == []


def test_environment_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("FAKE_API_SECRET", "must-not-leak")
    command = [sys.executable, "-c", "import os, json; print(json.dumps({'secret':os.getenv('FAKE_API_SECRET'),'home':os.environ['HOME'],'cwd':os.getcwd()}))"]
    result = json.loads(execute(command, b"{}", timeout=2, output_limit=4096))
    assert result["secret"] is None
    assert Path(result["home"]).resolve() == Path(result["cwd"]).resolve()


def test_concurrent_runs_are_rejected(store):
    watch = store.create(WatchSpec(title="Locked experiment"))
    with store.path.with_suffix(".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Conflict, match="already running"):
            run_once(store, watch["id"], [sys.executable, "-c", "print('{}')"])
    assert store.runs(watch["id"]) == []