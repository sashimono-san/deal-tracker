import argparse
import fcntl
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .collection import ingest
from .models import CollectionResult
from .store import Conflict, Store


class ExecutionFailure(ValueError):
    pass


def execute(
    command: list[str], payload: bytes, timeout: float, output_limit: int,
    *, environment: dict[str, str] | None = None,
) -> bytes:
    if not command or not 0 < timeout <= 600 or not 0 < output_limit <= 256_000:
        raise ValueError("Use a command, a 0-600 second deadline and at most 256 KB output")
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="deal-tracker-run-") as workspace:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env={"PATH": os.defpath, **(environment or {}), "HOME": workspace, "LANG": "en_US.UTF-8"},
            start_new_session=True,
        )
        assert process.stdin and process.stdout and process.stderr
        selector = selectors.DefaultSelector()
        output = bytearray()
        total_bytes = 0
        input_offset = 0
        try:
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            selector.register(process.stdout, selectors.EVENT_READ, "output")
            selector.register(process.stderr, selectors.EVENT_READ, "error")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Local collector exceeded its deadline")
                for key, events in selector.select(remaining):
                    if key.data == "input":
                        try:
                            input_offset += os.write(key.fd, payload[input_offset:input_offset + 8192])
                        except BrokenPipeError:
                            input_offset = len(payload)
                        if input_offset >= len(payload):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > output_limit:
                        raise ExecutionFailure("Local collector exceeded its output limit")
                    if key.data == "output":
                        output.extend(chunk)
            try:
                exit_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("Local collector exceeded its deadline") from error
            if exit_code:
                raise ExecutionFailure(f"Local collector exited with status {exit_code}")
            return bytes(output)
        finally:
            selector.close()
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def run_once(
    store: Store,
    watch_id: str,
    command: list[str],
    timeout: float = 30,
    output_limit: int = 128_000,
) -> dict:
    with store.path.with_suffix(".runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Conflict("Another local collection is already running") from error
        watch = store.get(watch_id)
        request_id = uuid4().hex
        payload = json.dumps({
            "request_id": request_id,
            "watch": watch,
            "result_schema": CollectionResult.model_json_schema(),
        }).encode()
        try:
            raw = execute(command, payload, timeout, output_limit)
            result = CollectionResult.model_validate_json(raw)
            if result.watch_version != watch["version"] or result.request_id != request_id:
                raise ExecutionFailure("Collector response does not match the supplied watch snapshot")
        except (TimeoutError, ExecutionFailure, OSError, ValidationError) as error:
            result = CollectionResult(
                watch_version=watch["version"],
                request_id=request_id,
                outcome="timed_out" if isinstance(error, TimeoutError) else "failed",
                diagnostics=("Collector returned invalid structured output" if isinstance(error, ValidationError) else str(error))[:1000],
            )
        saved = ingest(store, watch_id, result, source="local_command")
        return {**saved, "outcome": result.outcome, "diagnostics": result.diagnostics}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a trusted offline collector or back up the store")
    parser.add_argument("--database", type=Path, default=Path("data/tracker.sqlite3"))
    commands = parser.add_subparsers(dest="action", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    run = commands.add_parser("run")
    run.add_argument("watch_id")
    run.add_argument("--timeout", type=float, default=30)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    store = Store(args.database)
    if args.action == "backup":
        store.backup(args.destination)
        print(f"Backup created: {args.destination}")
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        result = run_once(store, args.watch_id, command, timeout=args.timeout)
        print(json.dumps(result))
        if result["outcome"] in {"failed", "timed_out", "blocked"}:
            raise SystemExit(1)


if __name__ == "__main__":
    main()