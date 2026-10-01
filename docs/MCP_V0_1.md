# V.O.I.D MCP v0.1

Date: 2026-10-01 · Base commit 82b2e45 (working tree, uncommitted) · SDK `mcp` **2.2.0**

> **MCP is an interoperability protocol, not V.O.I.D's authorization boundary.**

## 1. What this is

A Model Context Protocol server that exposes six of V.O.I.D's existing capabilities to any MCP client, over stdio.
It is an **adapter**, roughly 600 lines of translation with no capability of its own: no application resolver, no path
policy, no launcher, no authorization. Every call is executed by the capability layer V.O.I.D already had, behind the
security controls it already had.

## 2. Why V.O.I.D has one

V.O.I.D can already launch applications and open files when the owner speaks to it. An MCP adapter lets *other*
tools ask for the same things through a standard protocol, without any of them learning V.O.I.D's internals — and,
more importantly, without any of them getting a second, weaker way in. One capability layer, one security path, two
front doors.

The direction matters. This is **V.O.I.D as a server**: external clients call in. V.O.I.D is deliberately *not* an
MCP client yet — consuming external MCP servers would add a large untrusted-tool surface and is a separate decision.

## 3. Architecture

```
MCP client (stdio)
      │  JSON-RPC
      ▼
void/mcp/server.py        protocol only: six tool registrations, each a 1-line delegation
      │                   schemas generated from type hints by the SDK (void/mcp/schemas.py)
      ▼
void/mcp/adapter.py       argument rules, then hands off. Owns no capability and no policy.
      │
      ▼
Agent.invoke_tool  ──►  Agent._run_call          ◄── the SAME funnel the agent loop uses
      │                   ├─ KillSwitch.raise_if_engaged()
      │                   ├─ Tool.effective_risk(arguments)
      │                   ├─ RiskGate.authorize(...)
      │                   ├─ ToolRegistry.execute(...)  ─►  AppActions / FileActions  ─►  Windows
      │                   ├─ audit:     TOOL_CALL_DONE name=… risk=… ok=… duration=…
      │                   └─ telemetry: perf.emit("tool", …)
      ▼
structured MCP result  (ok / error.code / error.message)
```

Files:

| file | role |
|---|---|
| `void/mcp/server.py` | `MCPServer` construction and the six tool registrations. No logic. |
| `void/mcp/adapter.py` | The only bridge. Argument rules, delegation, failure classification. |
| `void/mcp/schemas.py` | The wire contract: typed results the SDK turns into JSON Schema. |
| `void/mcp/errors.py` | The stable error taxonomy and the outbound redaction pass. |
| `void/mcp/__main__.py` | `python -m void.mcp`. |
| `scripts/void_mcp_server.py` | A flag-free launcher, for hosts that mis-parse `-m`. |
| `void/core/agent.py` | **+`Agent.invoke_tool`**, a public entry onto the existing funnel. Adds no policy. |
| `void/providers/registry.py` | **+`ProviderRegistry.names()`**, mirroring `ToolRegistry.names()`. |

## 4. The security boundary

The boundary is `Agent._run_call`, exactly where it was before MCP existed. The adapter cannot get past it, and three
properties make that true by construction rather than by care:

**No model is ever consulted.** The agent the adapter builds is given `_NoProvider` — the same refuses-loudly provider
the deterministic voice fast path uses. A bug cannot quietly reach Gemini or Ollama to decide what to launch.
Measured: 0 model calls across every tool, including the full protocol round trips.

**The caller cannot name the thing that runs.** `launch_application` takes a human name, resolves it through the
existing `find_app` capability, and passes **the catalog's own `app_id`** to `launch_app`. The caller's string is
never the argument that reaches the launcher. A path, a command line or an executable therefore has nothing to attach
to — and an `app_id` supplied directly by a caller is just a name that resolves to nothing.

**A security decision is reported, never softened.** A `RiskGate` denial becomes `denied`; a protected location
becomes `protected`; the kill switch becomes `stopped`. None can become `ok`.

What the MCP layer contains, verified structurally by tests: no `subprocess`, no `os.system`, no `shell=True`, no
`Popen`, no `os.startfile`, no `eval`, no `exec`, no `ctypes`, and no import of `subprocess`/`shutil`/`winreg`/
`socket`. It never passes `owner_decision`, never touches `confirm_fn`, never calls `authorize` itself, never reads
the risk threshold, and never engages or resets the kill switch.

Only three existing tools are reachable: **`find_app`**, **`launch_app`**, **`open_path`**. That is the entire blast
radius and a test pins it.

## 5. Tools

| tool | reaches | risk | effect |
|---|---|---|---|
| `list_applications` | `find_app` | LOW | read-only |
| `find_application` | `find_app` | LOW | read-only |
| `launch_application` | `find_app` → `launch_app` | LOW | starts a program |
| `open_path` | `open_path` | LOW | opens a file/folder/URL |
| `get_system_info` | — | — | read-only, in-process |
| `get_void_status` | — | — | read-only, in-process |

Read-only tools go through the funnel too. That is deliberate: an owner who sets
`security.confirm_at_or_above: low` expects `find_app` to be gated, and if MCP read the catalog directly it would
quietly ignore that policy. Routing reads through `find_app` keeps the two paths identical — and bounds results at
that capability's own maximum rather than a larger number invented here.

### Schemas

Generated by the SDK from Python type hints; there is no hand-written JSON Schema to drift.

| tool | arguments |
|---|---|
| `list_applications` | `limit: int` — `minimum 1`, `maximum 50`, default 25 |
| `find_application` | `name: str` — `minLength 1`, `maxLength 200`, required |
| `launch_application` | `name: str` — `minLength 1`, `maxLength 200`, required |
| `open_path` | `path: str` — `minLength 1`, `maxLength 400`, required |
| `get_system_info` | none |
| `get_void_status` | none |

Every result is structured with an `ok` boolean and, on failure, `error: {code, message}`. A refusal is a **normal**
response, not a protocol fault, so a client can read *why*. Codes: `invalid_input`, `not_found`, `ambiguous`,
`denied`, `protected`, `stopped`, `unavailable`, `execution_failed`, `timeout`, `internal`.

`get_system_info` returns OS family and release, Python and V.O.I.D versions, CPU count and memory totals — and
deliberately no hostname, user name, environment value or path. `get_void_status` returns the kill-switch state, an
active-task count (reading at most 200 rows, never the whole history), configured providers with a coarse
reachable/not flag, voice state and V.O.I.D's own health-check lines.

## 6. Local stdio usage

```bash
.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.venv\Scripts\python.exe -m void.mcp
```

Client configuration:

```json
{
  "mcpServers": {
    "void": {
      "command": "C:/V.O.I.D/.venv/Scripts/python.exe",
      "args": ["C:/V.O.I.D/scripts/void_mcp_server.py"]
    }
  }
}
```

The script path rather than `-m void.mcp` because some hosts and dev tools parse a leading `-m` as one of their own
options and never forward it. Both entry points are identical otherwise.

**stdio only.** No socket is bound, nothing listens, and SSE / Streamable HTTP are not offered. `stdout` belongs to
the protocol, so all logging goes to `stderr`.

## 7. Development and testing

```bash
.venv\Scripts\python.exe -m pytest tests/test_mcp_adapter.py tests/test_mcp_server.py tests/test_mcp_security.py -q
.venv\Scripts\python.exe scripts\bench\mcp_overhead_bench.py --repeats 200
npx -y @modelcontextprotocol/inspector --cli --config <config.json> --server void --method tools/list
```

**153 MCP tests**, in three files: the adapter and its convergence on the funnel (28), the protocol surface driven by
the SDK's own client plus a real stdio subprocess (27), and the hostile-input matrix (98).

A note for anyone adding tests here: **do not patch `subprocess.Popen` globally.** `asyncio.windows_utils`
subclasses it at import time, so replacing it with a lambda breaks the event loop the MCP transport runs on. Use the
launcher seam `AppActions(..., launcher=...)` that the existing tests use.

### Overhead

200 repeats, catalog as a fixture, launches recorded:

| layer | p50 | p90 |
|---|---|---|
| `Agent.invoke_tool` direct (the floor) | 0.220 ms | 0.245 ms |
| adapter `launch_application` | 0.258 ms | 0.291 ms |
| adapter `find_application` | 0.013 ms | 0.015 ms |
| **protocol round trip `launch_application`** | **0.812 ms** | 0.947 ms |
| protocol round trip `find_application` | 0.405 ms | 0.541 ms |
| `initialize` + `tools/list` per client | 0.646 ms | 1.476 ms |

The adapter costs **+0.038 ms** over the bare funnel; the whole protocol round trip is well under a millisecond. MCP
is serialisation, not inference — 0 model calls at every layer.

## 8. Threat model

The caller is untrusted. It may be a well-behaved client, a confused model, or an attacker who controls every byte of
every argument.

| attempt | what stops it |
|---|---|
| executable path or command line as an application name | adapter character rules, before the catalog is asked |
| shell metacharacters, chaining, substitution, globs, null byte | same |
| supplying an `app_id` directly | `app_id` is engine-owned; a raw id resolves to nothing |
| malformed Store AUMID | existing `launch_app` validation (`is_app_user_model_id`) |
| path outside the owner's allowed roots | existing `FileActions._confine` → `denied`, and the message does not enumerate the owner's roots |
| `..` traversal out of an allowed root | same — canonicalised first, then checked |
| V.O.I.D's state, memory database, task store, `local_config.yaml` | existing `EngineProtected` → `protected` |
| Windows Credentials store, `~/.ssh` | same |
| `file:`, `data:`, `javascript:`, `vbscript:`, `ms-settings:`, `shell:`, `ftp:`, UNC | adapter scheme allowlist: http/https only |
| RiskGate denial treated as success | `denied` is a distinct code; nothing maps it to `ok` |
| acting while V.O.I.D is stopped | `KillSwitch.raise_if_engaged()` inside the funnel → `stopped` |
| text in an argument that reads like permission | an argument is data; the policy is never read from it |
| a memory item phrased as permission | memory never authorizes; verified with a denying gate |
| secrets, traces or paths in a response | outbound redaction in `errors.sanitise`, bounded to 400 chars |
| unbounded result or unbounded task scan | schema maxima, the capability's own cap, and a 200-row scan limit |
| an unexpected extra argument | ignored by the SDK; verified it cannot change the outcome |

**The reachable filesystem surface is exactly `security.allowed_roots` minus the protected roots — and that is a
configuration choice, not an MCP property.** On the development machine `allowed_roots` is `C:\`, so MCP can open
most of the drive, precisely as the owner's own voice commands can. Narrowing `allowed_roots` narrows MCP in the same
step. Protected locations are refused regardless.

## 9. Intentionally not exposed

No shell, command line, PowerShell or `cmd.exe`. No launching by path. No file read, write, move or delete. No
credential, key or token access. No elevation or administrator operations. No registry or policy modification. No
process termination or window manipulation. No memory read or write. No kill-switch control. No security-policy
change. No network transport, and no internet-exposed endpoint.

These are absent by construction — there is no code path to them — not merely disabled by configuration.

## 10. Future transport

The adapter does not know what transport it is behind: `main()` chooses, and every tool body is a delegation. Adding
Streamable HTTP is a change to `main()` plus the things a network listener needs and stdio does not — origin
validation, authentication, per-client authorization, rate limiting, and a decision about what "the owner" means when
the caller is remote. None of that is built, which is exactly why v0.1 is stdio only.

## 11. Future capabilities

Candidates, in rough order of how much new untrusted surface they add: richer read-only filesystem queries (reusing
`find_directory` / `list_directory`), window and running-process listing, controlled network *information*, device
information. Each needs the same treatment as these six: an existing capability, a bounded typed schema, a hostile
test matrix, and no new security layer.

Deliberately further out: V.O.I.D as an MCP *client*. That inverts the trust relationship and is a bigger decision
than anything here.

## 12. Limitations, and what is not validated

* **`list_applications` is a bounded sample, not an inventory.** `find_app` caps at 50; on this machine 201
  applications are known. `truncated` says so. Use `find_application` to resolve a specific name.
* **No `tier` in `find_application`.** The existing capability reports matches, not which tier produced them, and
  asking the resolver again just for the label would be a second resolution path that could disagree.
* **`get_void_status` probes providers.** Read-only and bounded (Gemini checks SDK + credential with no network
  call; the local provider does a 2 s loopback GET that never pulls a model), but it is not instant.
* **Concurrency is untested.** The server is single-client stdio; nothing here exercises simultaneous tool calls.
* **No long-running-session testing.** No soak test, no memory-growth measurement over hours.
* **Resources and prompts are not implemented** — tools only.
* **The Inspector was driven in CLI mode only.** `initialize`, `tools/list`, a safe `tools/call` and two refusals were
  verified; the web UI and TUI were not used.
* **One machine, one OS.** Windows 11, Python 3.14.7. Nothing here has run on another platform.
* **`timeout` is in the error taxonomy but is never produced** — no tool currently imposes its own deadline.

## 13. Fixed in final review

Two defects in the error contract, found by the pre-commit review rather than by the test suite:

* a path outside the owner's `allowed_roots` was reported as `execution_failed`. It is a policy refusal, so a client
  branching on the code would reasonably have retried it. It is now `denied`.
* that refusal echoed the configured allowed roots verbatim. The caller already knows the path it asked for;
  enumerating the owner's approved scope hands an attacker the map. Both that message and the protected-location
  message are now fixed sentences that name nothing internal.

Four tests pin them. An existing test checked for the literal string `allowed_roots` in responses but not for the
root *values*, which is why it did not catch this.
