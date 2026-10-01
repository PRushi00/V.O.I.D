# V.O.I.D V2 — the eight capability domains

2026-10-01. What was built, what was measured, what is deliberately absent, and why.

This document is the record for the V2 capability work. It is written to be checkable: every performance
number here was measured on this machine on this date and can be re-measured with the command given, and
every "we do not do X" is paired with the test that would fail if we started.

Machine the measurements come from: Windows 11 (26200), 24 logical cores, 31.4 GB RAM, NVIDIA RTX 5070
Laptop, ASUS FHD webcam, Python 3.14.7.

---

## 1. The shape of the work

V2 added four things and extended three:

| | What | Where |
|---|---|---|
| new | A read-only OS observation layer | `void/system/` |
| new | Observation capabilities (domains 3, 6, 7) | `void/actions/observe.py` |
| new | A camera gate, single-frame capture and controlled cloud description (domain 5) | `void/vision/`, `void/actions/vision.py` |
| new | Optional image input on the provider abstraction | `void/providers/base.py`, `gemini_provider.py`, `registry.py` |
| new | Conversation mode (domain 4) | `void/voice/runtime.py` |
| extended | Window inspection and state (domain 1) | `void/actions/computer.py` |
| extended | File metadata, copy, move (domain 2) | `void/actions/files.py` |
| extended | Memory-is-not-authority coverage (domain 8) | `tests/test_memory_is_not_authority.py` |

The tool count went from 21 to 30 (the camera's description is part of the existing `look`, not a new tool). Nothing was replaced: the Agent, ToolRegistry, RiskGate, KillSwitch,
AppCatalog, FileActions confinement, provider abstraction, memory service, voice pipeline and MCP layer are
the same ones, and every new capability is a `Tool` in the same registry reached through the same
`Agent._run_call` funnel.

### The observation layer, and why it is separate from the tools

`void/system/` produces facts and nothing else: no tools, no authorization, no model, and it changes nothing.
The capability tools that expose a curated subset live in `void/actions/observe.py`. Four rules hold across
the whole package, each with tests:

**Never fabricate.** A metric that cannot be read is `None`, and the reason sits in `Reading.unavailable`.
This machine genuinely cannot report CPU temperature — psutil ships no Windows sensor support — so V.O.I.D
says that rather than estimating one from load. `Reading.set(name, None)` is a no-op by construction.

**No shell.** Nothing builds a command string. Device enumeration goes through WMI via pywin32 (already a
dependency), which spawns no process at all; the one probe that runs an external program (`gpu.py`) passes a
frozen argv with `shell=False`. A caller's string reaches neither.

**No secrets, no content.** Process rows carry a name and resource use, never a command line — `--api-key=…`
in an argv is routine on a developer's machine, and a process list that V.O.I.D can speak aloud or hand to a
cloud model must not be able to carry one. Network rows carry addresses and ports, never payloads. Interface
rows carry IP addresses but never MAC addresses.

**Bounded.** A timeout on every probe and a cap on every list.

---

## 2. Domain 1 — Application & Desktop Control

**Already working, unchanged:** `find_app`, `launch_app`, `list_running_apps`, `list_windows`,
`activate_window`, `close_app`, the `AppCatalog` with its identity/spacing/publisher/sound match tiers, the
deterministic fast path, and multi-target launches.

**Added:** `get_active_window` (LOW) and `set_window_state` (LOW).

`get_active_window` is the piece of desktop context that makes "close this" or "what am I looking at"
answerable without the owner naming an application. It mints a window token exactly as `list_windows` does,
so the existing TOCTOU revalidation applies to anything done with it: a handle that has gone away, been
reused by another process, or changed application identity is refused, not acted on.

`set_window_state` takes `minimize`, `maximize` or `restore`. All three are reversible and lose no work,
which is why they are LOW while `close_app` stays HIGH — a change that blurred that line would change what
the owner is interrupted for, so `test_the_risk_levels_separate_reversible_from_destructive` pins it.

Both backend methods are *optional* (`foreground_window`, `set_window_state` return `None`/`False` by
default), following the existing `discovery_fingerprint` pattern. No existing backend — including the test
fake and `NullBackend` — needed changing, and a platform that cannot answer says so.

**Defect found and fixed during this work:** a non-string `state` (a model can emit `42`) raised
`AttributeError` rather than being refused, and `Tool.run` only converts `TypeError` into a clean refusal.
Now coerced defensively.

Measured: `get_active_window` on the real desktop returns in well under the probe budget; a window state
change is a single `ShowWindow` call.

---

## 3. Domain 2 — Filesystem Intelligence

**Already working, unchanged:** `search_files`, `find_directory`, `list_directory`, `read_file`,
`write_file`, `delete_file`, allowed roots, protected roots (which override allowed roots), engine-protected
paths, traversal protection, reparse-point refusal, and `FolderCatalog`.

**Added:** `get_file_info` (LOW), `copy_file` and `move_file` (MEDIUM, rising to HIGH when they would
replace an existing file).

`get_file_info` answers "how big is this?" and "when did I last change this?" without a byte of the file
entering V.O.I.D or a model's context. A folder gets a *count* of its entries, not a listing.

Copy and move are the first file operations with two paths, which makes them the first that could be used to
reach somewhere `write_file` would refuse. Both ends are confined independently; the destination is always
confined for writing, and a move confines its source for writing too because a move removes it.
`tests/test_file_transfer.py` asks that question from both ends: destination outside the roots, source
outside the roots, traversal in either, protected destination, protected source (the exfiltration case —
copying a secret somewhere readable), and engine-protected state.

Folders are deliberately **not** transferable. One wrong argument on a recursive move is unrecoverable and
nothing in V2 needs it, so `copy_file`/`move_file` refuse a directory rather than quietly supporting one.
Links are refused too: a link's target may be anywhere, and copying one produces something ambiguous.

**Defect found and fixed during this work:** `_transfer_risk` and the pre-existing `_write_risk` both failed
*open*. On Windows a path containing a NUL byte resolves without raising and then reports that it does not
exist, so a nonsense destination was graded MEDIUM (no confirmation) instead of HIGH. Both now go through
`_risk_path`, which refuses anything that is not a plain, non-empty, NUL-free string, and the caller fails
safe.

---

## 4. Domain 3 — System Awareness

**Before V2: nothing.** There was no system-information tool at all; psutil was a dependency used only to
name the process owning a window, and MCP's `get_system_info` was a five-field inline stub.

**Added:** `get_system_status`, `list_processes`, `diagnose_slowness` (all LOW, all read-only).

What is readable here, measured:

| Metric | Source | Status on this machine |
|---|---|---|
| CPU utilisation, core counts, frequency | psutil | available |
| Memory, swap | psutil | available |
| Disk usage per mount | psutil | available |
| Battery charge, mains state | psutil | available |
| Uptime | psutil | available |
| GPU name, utilisation, memory, temperature | `nvidia-smi` (frozen argv) | available |
| **CPU temperature** | — | **unavailable, reported as such** |

psutil has no `sensors_temperatures` on Windows, so CPU temperature is genuinely not readable by an
unprivileged process here. `get_system_status` therefore ends with *"Not available on this machine:
temperatures."* rather than inventing a number. Getting it would need a privileged driver, which V2 does not
install — see **Deferred** below.

### The process probe: a measured method switch

The first implementation primed and read `psutil.Process.cpu_percent` across every process. Measured over
386 processes:

```
psutil, per-process handles:  1050 ms per pass, and the cost is the handle, not the attribute
                              (memory_info alone 1044 ms, cpu_times alone 1054 ms, all three 1115 ms)
two passes + 250 ms sample:   3018 ms total — it hit the probe timeout and returned truncated
Windows perf counters (WMI):   534-811 ms, ONE query, no sampling sleep needed
```

Windows has already differenced the CPU figures over its own interval, so the counter set needs no second
pass. Switched to it; the probe now runs in **578 ms** and psutil remains as a fallback that reports memory
and says CPU is unavailable rather than filling the column with zeroes.

**Defect found and fixed during this work:** the counter set contains two rows that are not processes —
`_Total` and `Idle` (pid 0). The first version reported *"Idle is using 94% of your CPU"* as the answer to
"why is my laptop slow", which is worse than silence. Both are excluded, and
`test_the_idle_pseudo_process_is_never_reported_as_busy` pins it.

Measured latencies (`scripts/bench/observe_bench.py`):

```
get_system_status      1023 ms   (including the process sample)
diagnose_slowness       810 ms
list_processes          497 ms
```

---

## 5. Domain 4 — Voice Intelligence

**Already working, unchanged:** wake word, adaptive endpointing (see `VOICE_ENDPOINTING_V5_2026-10-01.md`),
STT with `temperature=0.0`, the deterministic fast path, LLM fallback, TTS, barge-in, mic-health supervision
and recovery, prewarm.

**Added: conversation mode.** Before this, every sentence needed its own wake word, because the detector
re-armed the instant a reply finished: *"open Opera"* / *"and what's the weather"* / *"thanks"* was three
wake words. Now the microphone stays open for a short window after a reply, so a follow-up needs none.

The cost is real and is why the window is short: during it, anything heard becomes a command with no wake
word in front of it. Four independent bounds contain that:

1. the window is seconds, not minutes (`voice.conversation_follow_up_s`, default 7, clamped 1–30);
2. one silent window ends the conversation — a follow-up capture that ends on `no_speech` closes it;
3. a conversation is capped at `voice.conversation_max_turns` (default 12, clamped 1–100);
4. the owner can end it in words, and the kill switch ends it instantly.

The spoken exits are matched against the **whole** transcript, never as a substring, because *"stop the
music"* and *"never mind, open Opera"* are commands. `test_a_command_that_merely_contains_a_dismissal_is_still_a_command`
covers nine such cases.

**What conversation mode does not change: authorization.** A follow-up turn is a new command through the
same funnel and inherits nothing — no confirmation, no risk decision, no authority. Being mid-conversation is
not a credential, and `test_a_follow_up_turn_authorizes_nothing_the_first_turn_did_not` asserts it directly.

**Defect found and fixed during this work:** a conversation left open when the kill switch engaged would
resume — with no wake word — the moment the session was explicitly re-armed, which is exactly when the owner
expects a clean slate. A latched `STOPPED` or terminal `CLOSED` now ends it.

Implementation note: a follow-up reuses the entire wake-capture path — same reducer, same generation
handling, same endpointer, same single broker — with one substitution, the no-speech timeout. It opens no
second audio stream (`test_a_follow_up_capture_reuses_the_single_microphone_owner`).

`voice.conversation_mode: false` restores wake-per-sentence, which remains a supported mode and keeps its
own coverage: `tests/test_voice_wake_integration.py` pins its rig to it.

---

## 6. Domain 5 — Multimodal / Camera

**Before V2: nothing.** Zero camera code in the repository.

**Status: COMPLETE.** Controlled capture, the gate, session expiry, the audit trail, egress control *and*
semantic description are implemented and validated against the real camera and the live Gemini service. The
description is a cloud capability behind its own switch, off by default; see "Semantic description" below for
why that separation is the design rather than a limitation.

### Four independent controls

A camera on a personal machine points at the owner, so the question the design answers is not "can V.O.I.D
see?" but "can the owner tell, at any moment, whether V.O.I.D can see, and did they say so?"

**A master switch in configuration.** `camera.enabled` is `false` by default. While it is false the
capability does not exist — not for the model, not for a confirmed tool call, not for MCP, not for an owner
confirmation. Turning it on is an edit to the owner's own `local_config.yaml`, which no part of V.O.I.D can
write. Same deny-by-default shape as `security.allowed_roots`.

**An owner-confirmed activation.** `enable_camera` is HIGH risk, so RiskGate asks. Validated live: with no
confirmer present (an unattended run) the gate's default is to deny and the camera cannot come on at all;
with the owner refusing, it stays off; with the owner agreeing, it activates.

**A session that expires by itself.** `camera.session_timeout_s` (default 120, clamped 5–600). There is no
way to activate indefinitely — `test_there_is_no_way_to_activate_indefinitely` tries `inf` and 10¹². Every
capture re-checks the gate, so a lapsed activation stops *the next frame*, not the next session.

**No recording, at all.** There is no function in `void/vision/` that records video and none that writes a
frame to disk. `Frame` has `__slots__` and no `save`. "No silent recording" is the absence of the capability,
not a policy, and `test_nothing_in_the_camera_path_writes_to_disk` scans the code (AST, docstrings stripped)
for `imwrite`, `VideoWriter`, `open(`, `Path(`, `pickle`, `tempfile`.

### Risk levels, and why `look` is MEDIUM

| Tool | Risk | Why |
|---|---|---|
| `get_camera_status` | LOW | A privacy indicator that needs permission to read is not an indicator. |
| `enable_camera` | HIGH | The decision that matters. Unattended runs cannot pass it. |
| `disable_camera` | LOW | Stopping is never the risky direction; "stop looking" must always work. |
| `look` | MEDIUM | Inside a window the owner just confirmed at HIGH, audited but not re-confirmed. |

Re-asking for every frame would train the owner to click yes without reading. The control on capture is the
gate — confirmed at HIGH, self-expiring, re-checked per frame. Outside an active window `look` is refused
outright, not escalated: there is no path from "no session" to "a frame" that avoids `enable_camera`.

### The indicator light

The device is opened, read and released for every single frame. That costs about a second and buys the one
camera-activity signal the owner can trust: the hardware LED, driven by the operating system, which V.O.I.D
cannot fake. It is off whenever V.O.I.D is not looking.
`test_the_device_is_always_released_so_the_indicator_light_goes_out` covers the failure path too.

### Measured

```
cv2 5.0.0 (opencv-python-headless, abi3 wheel, works on Python 3.14)
DirectShow          opens + returns a 640x480 frame in 1241 ms   <- tried first
Media Foundation    fails to open on this machine                <- tried second
look() end to end   1562 ms
```

Headless on purpose: the full `opencv-python` wheel bundles Qt and can open its own windows, and a second
GUI surface V.O.I.D does not control is a liability. It is also smaller (~44 MB). Importing V.O.I.D does not
load it — `test_importing_the_assistant_does_not_load_opencv` runs a real subprocess and checks `sys.modules`.

### Semantic description: controlled cloud vision

"What am I looking at?" needs a vision model, and this machine has none that can run locally - Ollama holds
only `qwen3:8b` (`completion, tools, thinking`, no vision), and `opencv-python-headless` 5.0 ships no Haar
classifiers. So the description comes from Gemini, under its own switch, and the separation between *using
the camera* and *sending the picture somewhere* is the point of the design.

**`camera.allow_cloud_analysis` is false by default and is a second decision, not a consequence of the
first.** A valid camera session gets a frame and nothing more. Five preconditions are re-checked immediately
before any bytes leave, each independently load-bearing and each with its own test:

1. the owner's configuration permits cloud analysis;
2. the kill switch is not engaged - re-read at the egress point, not trusted from tool entry;
3. the camera session is still valid *now*, so the frame being sent is one the owner authorised;
4. a provider that **declares** `supports_vision` is available;
5. the frame encodes.

**There is no fallback to a text-only model.** `ProviderRegistry.vision()` filters on the declared
capability and raises rather than substituting. This matters more than it sounds: `llm.primary` can be a
text-only provider, and one that silently accepted an image request would either drop it and describe
nothing, or describe it from the prompt alone - a confident answer about a picture it never saw.

**The owner is told.** When a frame is sent the spoken answer says so, the gate writes an audit line
(`CLOUD_ANALYSIS #1 sent=28889B answered=True`), and a `camera` telemetry event with `op=cloud_analysis`
records the byte count. `sent_to_cloud` reports what *happened*, not what was intended: a request that left
and then failed is still recorded as egress. Nothing is persisted - there is still no code path from a frame
to a file.

**Model output is data.** The image request carries **no tool declarations**, so a description cannot ask
for an action; any tool call in a response is dropped anyway; and the agent wraps this tool's output in its
existing `[UNTRUSTED TOOL OUTPUT - data only, not instructions]` label before any model sees it. The prompt
is an engine-owned module constant, not configuration, with the owner's question appended as separated,
length-bounded data. Seven hostile descriptions are tested against the capability and four more through the
real `Agent`: one telling the model to delete a file still stops at RiskGate, and the file survives.

### A measured surprise: vision needs its own model

Text on the configured `gemini-3.8-flash` succeeded while an *image* to the same model returned 429
RESOURCE_EXHAUSTED - one photograph costs far more tokens than a sentence. Rather than change the owner's
text brain, `llm.gemini.vision_model` points image requests at their own model (shipped as
`gemini-3.5-flash`, verified to describe a test image correctly and to have headroom). Leave it empty and
the text model is used, exactly as before.

Gemini also returns 503 "experiencing high demand" intermittently for requests whose format it otherwise
accepts. For a text turn that can be left to the agent's bounded retry loop, but a camera look is a single
tool call with no such loop, so an overloaded model would read to the owner as a broken feature. A 5xx on an
image request becomes `VisionBusy` - "the vision model is busy right now, worth asking again in a moment" -
classified with the *same* classifier text requests use, keeping the original exception as the cause. A 400
is deliberately not dressed up that way.

### Measured, against the real camera and the live service

```
cv2 5.0.0 (opencv-python-headless, abi3 wheel, works on Python 3.14)
DirectShow          opens + returns a 640x480 frame in 1241 ms   <- tried first
Media Foundation    fails to open on this machine                <- tried second
JPEG encode         0.6 ms for a 640x480 frame (~6.5-29 KB)
look(), local only          1365 ms p50
look(), with cloud analysis 4216-11216 ms (p50 9477 ms), 3/3 described
```

Validated end to end on 2026-10-01 against the owner's actual webcam and Gemini credential: camera disabled
initially, unauthorized look rejected, owner authorization through the real RiskGate, session activated,
frame captured, **cloud analysis off -> no request and nothing invented**, cloud analysis on -> one request,
an accurate description of the real scene returned, egress recorded in audit and telemetry, session expiry
rejecting a later look, and the kill switch blocking an egress with a session still valid. No image was
written to disk at any point and none appears in any log.

## 7. Domain 6 — Connectivity & Devices

**Before V2: half.** `void/device/` already held the *trusted communication* side — the explicitly-started,
TLS-only local Device Gateway with pairing, HMAC verification, replay protection, rate limiting and a closed
capability allow-list. That side is unchanged. What was missing was *observation*: V.O.I.D could not tell the
owner what was attached to their own machine.

**Added:** `list_devices` and `find_device` (both LOW, read-only).

Sources, both already present as dependencies: `sounddevice` for audio endpoints (the same view the voice
pipeline gets, so a USB headset appears exactly as it will when the microphone opens), and WMI via pywin32
for cameras, Bluetooth and USB.

### Why WMI and not PowerShell

Measured, same device enumeration:

```
Get-PnpDevice via PowerShell    568 ms, spawns a shell
WMI via pywin32                 120 ms, spawns no process at all
```

4.7× faster, no process creation, no new dependency — and, crucially, a *closed query table*. A caller names
a query (`wmi.query("cameras")`); it never supplies WQL. Every query in `QUERIES` is a literal, hand-written,
read-only `SELECT`, and no code path in `void/system/wmi.py` concatenates, formats or interpolates anything
into one. WMI can execute methods (process creation, service control, reboot) and WQL has no
injection-proof quoting story worth relying on, so "no caller-authored query" is structural rather than
escaped. Adding a query is a source change and a review, exactly like adding a gateway capability.

`FIELDS` projects explicitly, so a widened `SELECT` cannot start leaking: `Win32_PnPEntity` also carries
`DeviceID`/`PNPDeviceID`, which embed hardware serial numbers, and those are never selected.

A connected device's *name* is attacker-influenced data — a Bluetooth peer broadcasts whatever it likes, and
that name reaches the owner and possibly a model's context. `clean_name` strips control characters and
bidirectional-override characters and caps the length, at the source.

**Defect found and fixed during this work:** *"is my headset connected?"* returned nothing, while the machine
was reporting two endpoints flagged `looks_like_headset` — because no device is literally *named* "headset".
People ask for device *categories*, and Windows names hardware after its chipset. `_CATEGORY_WORDS` now maps
category words ("headset", "microphone", "webcam", "speakers") to the lists and flags that count as a match,
and a category hit is labelled `matched_category` so the answer says *"I can see 2 devices of that kind"*
rather than a bare yes.

**Second defect:** asking for *"ASUS MD100 Mouse"* also returned both ASUS cameras, on the brand word alone,
burying the device the owner named. A multi-word query now needs at least two shared words.

Measured: audio 103 ms, cameras 109 ms, Bluetooth 72 ms, USB 78 ms, full snapshot **225 ms**.

---

## 8. Domain 7 — Network Monitoring

**Before V2: nothing.**

**Added:** `get_network_status` and `list_connections` (both LOW, read-only).

The architecture asked for is OBSERVE → ANALYZE → DETECT → ALERT → ASK PERMISSION, and this is the first two
stages and nothing beyond them. Being explicit about what that excludes, because "network monitoring" covers
a lot of ground this deliberately does not touch:

* **It never sends a packet.** No scan, no probe, no ping, no connect, no DNS lookup. Every fact comes from
  this machine's own interface table and socket table — what `netstat` prints, readable without privilege
  and without touching the network. `test_the_network_module_sends_nothing` scans the code for `subprocess`,
  `urllib`, `requests`, `socket.socket(`, `sendto`, `create_connection`, `ping`.
* **It never reads payloads.** Packet capture needs a driver and administrator rights; V.O.I.D asks for
  neither. What a connection carries is not observable from here, by construction.
* **It never acts.** No block, no disconnect, no firewall change, no countermeasure. Detection produces a
  sentence for the owner; the owner decides.

Addresses are classified by *scope* (loopback / private / public / multicast) so a summary can say "three
connections to the internet, the rest local" without the addresses travelling anywhere. MAC addresses are
never collected — `_IP_FAMILIES` excludes the link layer, and
`test_interfaces_report_addresses_but_never_mac_addresses` checks a planted MAC does not appear in the
reading.

### Making the detector say something worth hearing

The first version flagged every listener reachable from the network. On this machine that is 21 sockets,
essentially all Windows RPC, SMB and service-host plumbing. An alert naming `lsass.exe` and `svchost.exe` as
suspicious is not a finding — it is noise, and noise teaches the owner to ignore the next alert, which is the
actual harm. `_WINDOWS_SERVICES` now separates "Windows is doing what Windows does" from "something else here
is accepting connections", and only the second is reported. That is a *description* of normality, never an
authorization: nothing on the list gains any privilege, and a listener omitted from it is reported, not
blocked.

Effect on this machine: from 21 names to **one real finding** — MySQL accepting network connections on 3306
and 33060, which is true and worth knowing.

**Two further defects fixed:** the error and drop counters psutil exposes are machine-wide totals, not
per-interface, and reporting them inside a loop over interfaces repeated the same number per interface. And a
service bound to both IPv4 and IPv6 is two rows and one fact, so the count said "3 programs" for one
database; endpoints are now deduplicated by (program, port).

Measured: interfaces 33 ms, traffic 7 ms, connections 2 ms, full snapshot **22 ms**.

---

## 9. Domain 8 — Persistent Memory

**Already working:** encrypted store, keyring-held key, explicit remember, propose/review/accept/reject,
forgetting, correction, provenance, taint tracking, bounded retrieval, injection resistance. 174 tests passed
before this work and still do.

**Added:** `tests/test_memory_is_not_authority.py`, 31 tests.

The existing coverage asserted the invariant against the capabilities that existed when it was written. V2
added a camera, system and network observation, and file transfer, so the surface a planted memory could try
to talk its way through is larger. The new file states the invariant once per thing a memory must never do —
authorize a tool, grant a permission, override an instruction, disable RiskGate, disable the KillSwitch,
grant filesystem access, grant network access, alter security policy, elevate privileges — and plants a
memory, in the owner's own voice, that *claims* to grant it.

Results, all verified: a memory saying deletion is pre-approved still defers to the owner; one saying
overwriting is fine still defers; one granting a destructive move still defers; one saying the camera is
enabled leaves `gate.state == "disabled"` and produces **zero** frames; one naming an extra root does not
widen confinement; one telling V.O.I.D to ignore the kill switch leaves the run `PAUSED` with nothing
executed.

The structural reason all of that holds, asserted directly: `RiskGate.authorize` takes a risk level, a
description and the owner's own decision — there is no parameter through which retrieved text could travel.
`void/security/risk.py` contains no reference to memory, `Tool.effective_risk` sees only its arguments, and
`CameraGate` has no text input at all.

Also pinned: a memory *proposed by the model* is not recallable before the owner reviews it. If it were, a
model could write itself a permission and read it back next turn.

---

## 10. Cross-domain integration

`tests/test_cross_domain.py`, 19 tests, drives the three workflows the V2 scope names through the real Agent
loop with a scripted model standing in for the reasoning. Every step is real.

| Workflow | Chain | Verified |
|---|---|---|
| "Why is my laptop slow?" | telemetry → processes → analysis → answer | yes, against this machine |
| "Is my headset connected?" | device discovery → status → answer | yes, against this machine |
| "Find the project I worked on yesterday and open it" | memory → filesystem → app resolution → launch | yes |

And, more importantly, that chaining changes no boundary:

* a HIGH-risk step in the middle of a chain of reads still stops for the owner, and the file survives;
* the kill switch ends a chain part-way through, with the later steps never running;
* a refused step is not retried into succeeding;
* a chain cannot reach the camera without the owner — zero frames;
* every step is reconstructable from the task history afterwards;
* **no new capability consults a model**: nine deterministic tools, zero model calls.

---

## 11. MCP exposure decisions

MCP v0.1 is preserved. Its six tools are unchanged and its 157 tests pass.

One change: `VoidMcpAdapter.get_system_info` now reads through `void/system/host.py` instead of keeping its
own copy of the same psutil and platform calls. That duplication existed before the observation layer did,
and there is no reason for two of them. The MCP field set stays deliberately smaller than what the probe can
report — an interop client needs to know roughly what kind of machine it is talking to, not the owner's
process list.

**No new capability is exposed through MCP in this release.** Per-capability reasoning:

| Capability | Expose? | Reasoning |
|---|---|---|
| `get_system_status`, `diagnose_slowness` | 🟡 later | Read-only and low risk, and plausibly useful. But the implementation is days old; the rule is "only after the underlying implementation is stable". |
| `list_processes` | 🟡 later | A process list is a fingerprint of what the owner runs. More than an interop client needs, and a privacy decision rather than a capability one. |
| `list_devices`, `find_device` | 🟡 later | A device inventory names the owner's hardware and paired phone — identifying data about a person, not about a machine. |
| `get_network_status` | 🟡 later | The anomaly summary is useful; the listener names are environment detail. |
| `list_connections` | 🔴 not yet | Connection metadata reveals which services the owner uses and when. |
| `get_file_info` | 🟡 later | Read-only and confined, but the path surface needs the same review `open_path` got. |
| `copy_file`, `move_file` | 🟡 later | Write operations over MCP need a trusted-client story that does not exist yet. |
| `get_active_window` | 🟡 later | A window title can be a document name the owner did not mean to publish. |
| `set_window_state` | 🟡 later | A desktop mutation; harmless but needs the same review. |
| **camera tools** | 🔴 **never in this form** | An MCP client is a program, not the owner. "The owner confirmed a camera session" does not transfer to a different caller, and the data is a picture of whoever is at the machine. Now that `look` can also send that picture to a cloud model, exposing it would let a remote caller trigger egress of the owner's room. |
| kill switch, RiskGate, policy, roots | 🔴 never | Unchanged from v0.1. MCP is not the security boundary. |

---

## 12. Telemetry

Three new events in the `void/perf/schema.py` allowlist — counts, durations and fixed enumerations only, so
the allowlist could not carry a device name, a process name, an address or image data even if a caller tried:

* `observe` — `probe`, `duration_s`, `values`, `unavailable`. The `unavailable` count is how "the GPU is
  idle" stays distinguishable from "there is no readable GPU here" in the telemetry as well as in the answer.
* `camera` — `op` (activate / deactivate / expire / capture / denied / **cloud_analysis**), `state`,
  `session_s`, `captures`, `cloud`, **`bytes_sent`**. The audit trail for a device that points at the owner,
  including how many bytes of image left the machine and when.
* `conversation` — `op`, `turns`, `window_s`, `why`. Makes "do follow-up windows get used, or mostly lapse?"
  answerable from real use.

Verified by emitting all of them for real and scanning the output: 11 events, `dropped_fields: 0`,
`unknown_event: 0`, and none of `ASUS`, `Realtek`, `Xiaomi`, `mysqld`, `pythonw`, `iQOO`, `192.168`, a
backslash, or a key-shaped prefix anywhere in the stream.

---

## 13. Configuration added

```yaml
camera:
  enabled: false                   # deny-by-default; the capability does not exist while false
  session_timeout_s: 120           # clamped 5..600; there is no permanent "on"
  allow_cloud_analysis: false      # a SEPARATE decision from looking locally; five preconditions are
                                   # re-checked immediately before any bytes leave

llm:
  gemini:
    vision_model: "gemini-3.5-flash"   # image requests get their own model: measured, text on the
                                       # configured model worked while an image returned 429, because a
                                       # photograph costs far more tokens. Empty = use `model`.
  device_index: 0
  max_width: 640                   # less incidental background detail, less to send if ever sent

voice:
  conversation_mode: true          # false restores a wake word before every sentence
  conversation_follow_up_s: 7.0    # clamped 1..30
  conversation_max_turns: 12       # clamped 1..100
```

Every value is clamped on load, and a malformed value falls back to a working default rather than leaving the
owner unable to talk or the camera in an undefined state.

`requirements-vision.txt` is new and optional, mirroring `requirements-voice.txt`. Installing it does not
turn the camera on.

---

## 14. Deferred

### 🟡 Later — useful, not needed to make V2 work

* **CPU temperature.** Would need a privileged helper (LibreHardwareMonitor or similar). V2 does not install
  a driver to read a number.
* **MCP exposure** of the capabilities marked 🟡 above, each after its own review.
* **Owner-requested frame save.** "Take a photo and keep it" is a separate, explicitly confirmed capability,
  not a flag on `look`.
* **Network rate measurement.** The counters are cumulative; a rate needs two samples and a wait, and no
  question V.O.I.D answers today needs one.
* **Recursive directory copy/move**, if a real use appears.
* **Bluetooth pairing / connecting.** V2 observes Bluetooth; it does not pair, trust or connect.

### 🔴 Do not build yet

* Any camera capability over MCP, or any remote camera access.
* Packet capture, traffic inspection, or anything needing a network driver.
* Active network response: blocking, disconnecting, firewall changes, countermeasures.
* Scanning, probing, or any traffic sent to another host.
* A privileged helper service, for temperatures or anything else.
* Arbitrary shell, unrestricted PowerShell, elevation, credential access.
* A second application resolver, a second filesystem security layer, a second execution engine, or
  LLM-based authorization anywhere.

---

## 15. How to re-measure

```bash
python -m pytest -q                                  # the whole suite
python -m pytest -q -m hardware                      # the tests that read this machine
python scripts/check_test_guard.py --junit junit.xml  # coverage has not silently shrunk
python scripts/bench/observe_bench.py                 # domain 3/6/7 probe latencies
python scripts/bench/mcp_overhead_bench.py            # MCP overhead, unchanged by this work
python -m compileall -q void                          # import/compile check
```
