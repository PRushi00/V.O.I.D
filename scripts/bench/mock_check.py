"""Self-test of the OpenAI/Anthropic benchmark candidates against a LOCAL mock server (fake key, no network)."""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.argv = ["x"]; sys.path.insert(0, ".")
import llm_bench as b, evalset

FAKE = "sk-mock-not-a-real-key-0000000000"
seen = {"auth": [], "bodies": []}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n))
        seen["auth"].append(self.headers.get("Authorization") or self.headers.get("x-api-key")); seen["bodies"].append(body)
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        w = lambda o: (self.wfile.write(b"data: " + (o if isinstance(o, bytes) else json.dumps(o).encode()) + b"\n\n"), self.wfile.flush())
        time.sleep(0.15)
        if self.path.endswith("/chat/completions"):
            w({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "launch_app", "arguments": "{\"na"}}]}}]})
            w({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "me\": \"notepad\"}"}}]}}]})
            w({"choices": [], "usage": {"prompt_tokens": 2400, "completion_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 0}}})
            w(b"[DONE]")
        else:
            w({"type": "message_start", "message": {"usage": {"input_tokens": 2400}}})
            w({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "launch_app"}})
            w({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"name\": "}})
            w({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "\"notepad\"}"}})
            w({"type": "message_delta", "usage": {"output_tokens": 14}})


srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
item = evalset.BY_ID["A1_open_notepad"]; specs = b.specs_by_name()
os.environ["OPENAI_API_KEY"] = FAKE; os.environ["ANTHROPIC_API_KEY"] = FAKE
ok = True
for spec in (f"openai:mock-model;base=http://127.0.0.1:{port}/v1", f"anthropic:mock-model;base=http://127.0.0.1:{port}"):
    c = b.make_candidate(spec)
    r = c.run(b._messages(item), b.tool_specs())
    passed, note = item.check(r, specs)
    line = f"{c.label:50} tool={[(t.name, t.arguments, t.id) for t in r.tool_calls]} ttft>=0.15:{(r.ttft_s or 0) >= 0.15} usage={r.usage} check={passed}"
    print(line)
    ok &= passed and (r.ttft_s or 0) >= 0.15 and not r.error
# a request must carry the key only in the auth header (and it must never reach the printed/stored record)
rec = b._record(c, item, "1", r, True, "x", 0)
assert FAKE not in json.dumps(rec), "key leaked into a record"
assert all(a and FAKE in a for a in seen["auth"]), "key not sent in header"
assert all(FAKE not in json.dumps(bd) for bd in seen["bodies"]), "key leaked into request body"
# missing key -> clean auth error, no traceback, no network
os.environ.pop("OPENAI_API_KEY"); os.environ.pop("ANTHROPIC_API_KEY")
import remote_candidates as rc
rc.keyring = None
orig = rc._key; rc._key = lambda e, k: None
r2 = b.make_candidate("openai:x").run(b._messages(item), None)
print("no-key result:", r2.error)
ok &= r2.error and r2.error["kind"] == "auth"
print("MOCK SELF-TEST", "PASSED" if ok else "FAILED")
