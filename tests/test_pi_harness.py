import json
import shutil
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from deal_tracker.models import CollectionResult, WatchSpec
from deal_tracker.pi_harness import collect, parse_output, token_usage
from deal_tracker.runner import ExecutionFailure, execute
from deal_tracker.settings import Settings


def transcript(*, quote="Linen shirt, EUR 39, size M", outcome="success", fetched=True):
    timestamp = datetime.now(UTC).isoformat()
    page = {
        "url": "https://example.com/shirt", "fetched_at": timestamp,
        "text": "Linen shirt, EUR 39, size M", "structured_data": "",
    }
    events = [{
        "type": "tool_execution_end", "toolName": "fetch_shop_page", "isError": False,
        "result": {"content": [{"type": "text", "text": json.dumps(page)}]},
    }] if fetched else []
    events.append({
        "type": "tool_execution_end", "toolName": "finish_collection", "isError": False,
        "result": {"details": {"collection": {
            "request_id": "test-request", "watch_version": 1, "outcome": outcome,
            "observations": [{
                "title": "Shirt", "url": page["url"], "observed_at": timestamp,
                "evidence": quote, "price": 39, "currency": "EUR",
            }] if outcome == "success" else [],
        }}},
    })
    return "\n".join(json.dumps(event) for event in events).encode()


def test_pi_result_requires_real_tool_events_and_quoted_evidence():
    result = parse_output(transcript(), {"version": 1}, "test-request")
    assert result.observations[0].price == 39
    with pytest.raises(ExecutionFailure, match="quote"):
        parse_output(transcript(quote="Invented discount"), {"version": 1}, "test-request")
    with pytest.raises(ExecutionFailure, match="supporting"):
        parse_output(transcript(fetched=False), {"version": 1}, "test-request")
    with pytest.raises(ExecutionFailure, match="structured"):
        parse_output(b'{"type":"message_end","message":{"role":"assistant"}}', {"version": 1}, "test-request")


def test_pi_cannot_report_no_change_without_fetching():
    with pytest.raises(ExecutionFailure, match="supporting"):
        parse_output(transcript(outcome="no_change", fetched=False), {"version": 1}, "test-request")
    assert parse_output(
        transcript(outcome="blocked", fetched=False), {"version": 1}, "test-request"
    ).outcome == "blocked"


def test_pi_rejects_stale_snapshot_or_other_request():
    with pytest.raises(ExecutionFailure, match="different"):
        parse_output(transcript(), {"version": 2}, "test-request")
    with pytest.raises(ExecutionFailure, match="different"):
        parse_output(transcript(), {"version": 1}, "other-request")


def test_separate_visible_and_structured_quotes_are_accepted():
    events = [json.loads(line) for line in transcript().splitlines()]
    page = json.loads(events[0]["result"]["content"][0]["text"])
    page["text"] = "Ref. 1418/212\n\nUnrelated delivery information"
    page["structured_data"] = '{"offers": {"priceCurrency": "EUR", "price": 40, "eligibleRegion": "AT"}}'
    events[0]["result"]["content"][0]["text"] = json.dumps(page)
    observation = events[-1]["result"]["details"]["collection"]["observations"][0]
    observation["evidence"] = 'Ref. 1418/212\n\n"priceCurrency": "EUR", "price": 40'
    result = parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")
    assert result.observations[0].evidence == observation["evidence"]
    observation["evidence"] += "\n\nFree shipping to Austria"
    with pytest.raises(ExecutionFailure, match="excerpt"):
        parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")


def test_separated_quotes_cannot_borrow_from_another_page():
    events = [json.loads(line) for line in transcript(quote="Linen shirt\n\nFree shipping").splitlines()]
    original = json.loads(events[0]["result"]["content"][0]["text"])
    other = {**original, "url": "https://example.com/another-shirt", "text": "Free shipping"}
    events.insert(1, {
        "type": "tool_execution_end", "toolName": "fetch_shop_page", "isError": False,
        "result": {"content": [{"type": "text", "text": json.dumps(other)}]},
    })
    with pytest.raises(ExecutionFailure, match="excerpt"):
        parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")


def test_pi_is_disabled_without_explicit_configuration():
    with pytest.raises(ExecutionFailure, match="disabled"):
        collect({}, "test-request", Settings())


def test_pi_launch_is_isolated_and_uses_selected_model(monkeypatch, tmp_path):
    def execute(command, payload, timeout, output_limit, *, environment):
        for flag in ("--no-builtin-tools", "--no-mcp", "--no-extensions", "--no-context-files", "--no-session"):
            assert flag in command
        assert command[command.index("--model") + 1] == "approved-model"
        assert command[command.index("--thinking") + 1] == "low"
        assert command[command.index("--tools") + 1] == "fetch_shop_page,finish_collection"
        assert environment["PI_CODING_AGENT_DIR"] == str(tmp_path)
        assert "SMTP_PASSWORD" not in environment
        assert json.loads(payload)["watch"]["shop_url"] == "https://example.com"
        assert json.loads(payload)["research_shop"] is True
        assert timeout == 120
        assert output_limit == 256_000
        return transcript()

    monkeypatch.setattr(shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr("deal_tracker.pi_harness.execute", execute)
    settings = Settings(
        collection_enabled=True, pi_directory=tmp_path,
        pi_provider="approved-provider", pi_model="approved-model", browser_enabled=False,
    )
    watch = {**WatchSpec(title="Shirt", shop_url="https://example.com").model_dump(), "version": 1}
    assert collect(watch, "test-request", settings).outcome == "success"


def test_shop_notes_require_real_fetched_evidence():
    events = [json.loads(line) for line in transcript().splitlines()]
    page = json.loads(events[0]["result"]["content"][0]["text"])
    result = events[-1]["result"]["details"]["collection"]
    result["shop_research"] = {
        "outcome": "found", "summary": "Fixture only", "url": page["url"],
        "observed_at": page["fetched_at"], "evidence": "Invented sale calendar",
    }
    with pytest.raises(ExecutionFailure, match="quote"):
        parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")
    result["shop_research"]["evidence"] = page["text"]
    parsed = parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")
    assert parsed.shop_research.summary == "Fixture only"


@pytest.mark.parametrize("headless", [True, False])
def test_display_is_forwarded_only_for_visible_browser(monkeypatch, tmp_path, headless):
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XAUTHORITY", "/tmp/browser-test.Xauthority")
    monkeypatch.setenv("SMTP_PASSWORD", "must-not-reach-browser")
    monkeypatch.setattr(shutil, "which", lambda executable: f"/usr/bin/{executable}")

    def execute(command, payload, timeout, output_limit, *, environment):
        if headless:
            assert "DISPLAY" not in environment
            assert "XAUTHORITY" not in environment
        else:
            assert environment["DISPLAY"] == ":99"
            assert environment["XAUTHORITY"] == "/tmp/browser-test.Xauthority"
        assert "SMTP_PASSWORD" not in environment
        return transcript()

    monkeypatch.setattr("deal_tracker.pi_harness.execute", execute)
    settings = Settings(
        collection_enabled=True, pi_directory=tmp_path, pi_provider="fixture",
        pi_model="fixture", browser_headless=headless,
    )
    watch = {**WatchSpec(title="Shirt", shop_url="https://example.com").model_dump(), "version": 1}
    assert collect(watch, "test-request", settings).outcome == "success"


def test_browser_snapshots_preserve_multiple_versions_of_the_same_url():
    events = [json.loads(line) for line in transcript().splitlines()]
    original = json.loads(events[0]["result"]["content"][0]["text"])
    changed = {**original, "fetched_at": datetime.now(UTC).isoformat(), "text": "Search navigation"}
    events.insert(1, {
        "type": "tool_execution_end", "toolName": "browser_action", "isError": False,
        "result": {"content": [{"type": "text", "text": json.dumps(changed)}]},
    })
    result = parse_output("\n".join(json.dumps(event) for event in events).encode(), {"version": 1}, "test-request")
    assert result.observations[0].price == 39


def test_pi_capture_discards_progress_but_retains_bounded_evidence():
    stream = Path(__file__).parents[1] / "deal_tracker/pi_stream.mjs"
    program = "import json,sys; sys.stdin.read(); [print(json.dumps({'type':'message_update','text':'x'*4000})) for _index in range(100)]; print(sys.argv[1])"
    raw = execute(
        [shutil.which("node"), str(stream), sys.executable, "-c", program, transcript().decode()],
        b"{}", 10, 16_000,
    )
    assert b"message_update" not in raw
    assert parse_output(raw, {"version": 1}, "test-request").observations[0].price == 39


def test_usage_counts_only_final_messages_and_keeps_caches_separate():
    usage = {"input": 100, "output": 20, "cacheRead": 300, "cacheWrite": 40}
    final = {"type": "message_end", "message": {"role": "assistant", "usage": usage}}
    events = [
        {"type": "message_update", "usage": usage}, final,
        {"type": "agent_end", "messages": [final["message"]]},
        {"type": "message_end", "message": {"role": "toolResult", "usage": usage}},
        {"type": "message_end", "message": {"role": "assistant"}},
    ]
    raw = execute(
        [shutil.which("node"), str(Path(__file__).parents[1] / "deal_tracker/pi_stream.mjs"),
         sys.executable, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])",
         "\n".join(json.dumps(event) for event in events)], b"{}", 10, 16_000,
    )
    assert token_usage(raw) == {
        "model_calls": 2, "reported_calls": 1, "complete": False,
        "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 300,
        "cache_write_tokens": 40, "total_tokens": 460,
    }
    assert token_usage(b"") is None
    result = CollectionResult(request_id="usage-test", watch_version=1, outcome="no_change")
    result._usage = token_usage(raw)
    assert "_usage" not in result.model_dump()
    assert "_usage" not in CollectionResult.model_json_schema()["properties"]


@pytest.mark.parametrize("browser_enabled", [False, True])
@pytest.mark.skipif(not shutil.which("pi"), reason="Optional local pi CLI integration")
def test_installed_pi_uses_only_collector_tools_with_local_fake_model(tmp_path, browser_enabled):
    requests = []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            first = len(requests) == 1
            arguments = {"url": "https://127.0.0.1/private"} if first else {
                "schema_version": 1, "watch_version": 1, "request_id": "fixture-request",
                "outcome": "blocked", "diagnostics": "Local fixture: private URL blocked",
                "observations": [],
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"call_{len(requests)}", "type": "function",
                    "function": {
                        "name": "fetch_shop_page" if first else "finish_collection",
                        "arguments": json.dumps(arguments),
                    },
                }]}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"},
            ]
            for choice in chunks:
                message = {
                    "id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "fixture", "choices": [choice],
                }
                self.wfile.write(f"data: {json.dumps(message)}\n\n".encode())
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":2000,"completion_tokens":120,"total_tokens":2120,"prompt_tokens_details":{"cached_tokens":500}}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
        "api": "openai-completions", "apiKey": "local-fixture-only",
        "models": [{
            "id": "fixture", "name": "Local fixture", "reasoning": False,
            "input": ["text"], "contextWindow": 32000, "maxTokens": 4096,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }],
    }}}))
    watch = {**WatchSpec(title="Fixture", shop_url="https://example.com").model_dump(), "version": 1}
    settings = Settings(
        collection_enabled=True, pi_directory=tmp_path, pi_provider="fixture",
        pi_model="fixture", check_timeout=20, browser_enabled=browser_enabled,
    )
    try:
        result = collect(watch, "fixture-request", settings)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.outcome == "blocked"
    assert len(requests) == 2
    assert result._usage == {
        "model_calls": 2, "reported_calls": 2, "complete": True,
        "input_tokens": 3000, "output_tokens": 240, "cache_read_tokens": 1000,
        "cache_write_tokens": 0, "total_tokens": 4240,
    }
    for payload in requests:
        assert {tool["function"]["name"] for tool in payload["tools"]} == {
            "fetch_shop_page", "finish_collection", *({"browser_action"} if browser_enabled else set()),
        }
        schema = next(tool["function"]["parameters"] for tool in payload["tools"] if tool["function"]["name"] == "finish_collection")
        assert "shop_research" in schema["properties"]
    assert ("outside the selected HTTPS shop" if browser_enabled else "Private network addresses") in json.dumps(requests[-1]["messages"])