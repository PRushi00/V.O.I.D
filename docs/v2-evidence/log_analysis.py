"""Read-only analysis of V.O.I.D's real void.log (stage markers only; the log by
design never holds transcripts/audio/args). Prints aggregate counts + latency
stats. Nothing is written to the log or to ~/.void."""
import re, sys, statistics as st, collections, os
from datetime import datetime

LOG = os.path.join(os.environ["USERPROFILE"], ".void", "void.log")
ts_re = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) (\w+) ([\w.]+): (.*)$")

def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").timestamp()

events = []
with open(LOG, encoding="utf-8", errors="replace") as f:
    for line in f:
        m = ts_re.match(line.rstrip("\n"))
        if m:
            events.append((parse_ts(m.group(1)), m.group(1), m.group(3), m.group(4)))

print(f"parsed events: {len(events)}  span: {events[0][1]} -> {events[-1][1]}")

def q(vals, p):
    vals = sorted(vals)
    if not vals: return float('nan')
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

def summarize(name, vals):
    if not vals:
        print(f"  {name:38s} n=0"); return
    print(f"  {name:38s} n={len(vals):4d} min={min(vals):6.2f} p50={q(vals,.5):6.2f} "
          f"p90={q(vals,.9):6.2f} max={max(vals):6.2f} mean={st.mean(vals):6.2f}")

# ---- 1. STT invocations: empty (0 samples) vs real ------------------------
stt_started = [(t, s, m) for t, s, c, m in events if m.startswith("STT_STARTED")]
zero = [e for e in stt_started if "audio_samples=0)" in e[2]]
real = [e for e in stt_started if "audio_samples=0)" not in e[2]]
print(f"\nSTT_STARTED total={len(stt_started)}  zero-sample={len(zero)}  with-audio={len(real)}")
by_day = collections.Counter(e[1][:10] for e in zero)
print("  zero-sample STT starts per day:", dict(sorted(by_day.items())))
by_day_r = collections.Counter(e[1][:10] for e in real)
print("  with-audio STT starts per day:  ", dict(sorted(by_day_r.items())))

# Was a wake-initiated capture in progress? (COMMAND_CAPTURE_STARTED within 20s before)
cap_starts = [t for t, s, c, m in events if m.startswith("COMMAND_CAPTURE_STARTED")]
def wake_before(t, window=20.0):
    return any(0 <= t - c <= window for c in cap_starts)
z_wake = sum(1 for e in zero if wake_before(e[0]))
r_wake = sum(1 for e in real if wake_before(e[0]))
print(f"  zero-sample STT preceded by wake capture (<=20s): {z_wake}/{len(zero)}")
print(f"  with-audio  STT preceded by wake capture (<=20s): {r_wake}/{len(real)}")

# ---- 2. LLM call + tool call durations ------------------------------------
llm_ok = []; llm_fail = []; tool = collections.defaultdict(list); n_tool_calls = collections.Counter()
for t, s, c, m in events:
    mm = re.match(r"LLM_CALL_DONE attempt=(\d+) duration=([\d.]+)s tool_calls=(\d+)", m)
    if mm: llm_ok.append((float(mm.group(2)), int(mm.group(3)))); continue
    mm = re.match(r"LLM_CALL_FAILED attempt=(\d+) duration=([\d.]+)s (\S+)", m)
    if mm: llm_fail.append((float(mm.group(2)), mm.group(3))); continue
    mm = re.match(r"TOOL_CALL_DONE name=(\S+) risk=(\S+) ok=(\S+) duration=([\d.]+)s", m)
    if mm: tool[mm.group(1)].append(float(mm.group(4)))
print(f"\nLLM calls ok={len(llm_ok)} failed={len(llm_fail)}")
summarize("LLM call duration (all)", [d for d, _ in llm_ok])
summarize("LLM call duration (returns tool calls)", [d for d, n in llm_ok if n > 0])
summarize("LLM call duration (text only)", [d for d, n in llm_ok if n == 0])
if llm_fail:
    print("  LLM failures by exception type:", dict(collections.Counter(x for _, x in llm_fail)))
    summarize("LLM failed-call duration", [d for d, _ in llm_fail])
print("\nTool call durations by tool:")
for name, vals in sorted(tool.items(), key=lambda kv: -len(kv[1])):
    summarize(name, vals)
print("  AUTO_COMPLETE (final LLM call skipped):", sum(1 for *_, m in events if m.startswith("AUTO_COMPLETE")))

# ---- 3. Full voice interactions (wake -> ... -> speak done) ---------------
# An interaction = an STT with audio (>0 samples) and non-empty result, followed by DISPATCH.
print("\nReconstructing voice interactions (STT with audio -> DISPATCH -> SPEAK):")
idx = {i: e for i, e in enumerate(events)}
rows = []
i = 0
n = len(events)
while i < n:
    t, s, c, m = events[i]
    if m.startswith("STT_STARTED") and "audio_samples=0)" not in m:
        rec = {"stt_start": t, "samples": int(re.search(r"audio_samples=(\d+)", m).group(1))}
        # look ahead up to 120s for the chain
        j = i + 1
        while j < n and events[j][0] - t < 120:
            tj, sj, cj, mj = events[j]
            if mj.startswith("STT_DONE") and "stt_done" not in rec: rec["stt_done"] = tj; rec["stt_empty"] = "empty=True" in mj
            elif mj.startswith("DISPATCH_STARTED") and "disp_start" not in rec: rec["disp_start"] = tj
            elif mj.startswith("LLM_CALL_DONE"):
                rec.setdefault("llm", []).append(float(re.search(r"duration=([\d.]+)s", mj).group(1)))
            elif mj.startswith("TOOL_CALL_DONE"):
                rec.setdefault("tool", []).append(float(re.search(r"duration=([\d.]+)s", mj).group(1)))
            elif mj.startswith("AUTO_COMPLETE"): rec["auto"] = True
            elif mj.startswith("DISPATCH_OK") and "disp_ok" not in rec: rec["disp_ok"] = tj
            elif mj.startswith("SPEAK_STARTED") and "speak_start" not in rec: rec["speak_start"] = tj
            elif mj.startswith("TTS_SPEAK_DONE") and "tts_done" not in rec: rec["tts_done"] = tj; break
            elif mj.startswith("STT_STARTED"): break
            j += 1
        # find wake accept + endpoint before stt_start (within 25s)
        wk = [tt for tt, ss, cc, mm in events[max(0, i-400):i] if mm.startswith("WAKE_DETECTED accepted") and 0 <= t - tt <= 25]
        ep = [(tt, mm) for tt, ss, cc, mm in events[max(0, i-400):i] if mm.startswith("COMMAND_ENDPOINT") and 0 <= t - tt <= 5]
        if wk: rec["wake"] = wk[-1]
        if ep: rec["endpoint"] = ep[-1][0]; rec["endpoint_reason"] = re.search(r"reason=(\w+)", ep[-1][1]).group(1)
        rows.append(rec)
    i += 1

print(f"  candidate interactions (STT with audio): {len(rows)}")
ok = [r for r in rows if r.get("stt_done") and not r.get("stt_empty") and r.get("disp_start")]
print(f"  of which reached dispatch (non-empty transcript): {len(ok)}")
def d(r, a, b): return (r[b] - r[a]) if a in r and b in r else None
def col(a, b, rs=ok): return [x for x in (d(r, a, b) for r in rs) if x is not None]
print("\nSegment latencies (seconds) over interactions that reached dispatch:")
summarize("wake -> endpoint (capture duration)", col("wake", "endpoint"))
summarize("endpoint -> stt_start (finalize)", col("endpoint", "stt_start"))
summarize("STT decode (stt_start -> stt_done)", col("stt_start", "stt_done"))
summarize("stt_done -> dispatch_start", col("stt_done", "disp_start"))
summarize("dispatch (agent total)", col("disp_start", "disp_ok"))
summarize("dispatch_ok -> speak_start", col("disp_ok", "speak_start"))
summarize("speak duration (speak_start -> tts_done)", col("speak_start", "tts_done"))
summarize("END-TO-END endpoint -> speak_start", col("endpoint", "speak_start"))
summarize("END-TO-END endpoint -> dispatch_ok", col("endpoint", "disp_ok"))
summarize("END-TO-END wake -> dispatch_ok", col("wake", "disp_ok"))
print("  endpoint reasons:", dict(collections.Counter(r.get("endpoint_reason") for r in ok)))
print("  samples (audio) seconds @16k:", end=" ")
summarize("command audio length", [r["samples"] / 16000 for r in ok])
print("  interactions using AUTO_COMPLETE:", sum(1 for r in ok if r.get("auto")),
      " / with >=2 LLM calls:", sum(1 for r in ok if len(r.get("llm", [])) >= 2))
summarize("agent-internal LLM time per interaction", [sum(r["llm"]) for r in ok if r.get("llm")])
summarize("agent-internal tool time per interaction", [sum(r["tool"]) for r in ok if r.get("tool")])

# ---- 4. Wake stats ---------------------------------------------------------
wk_acc = [e for e in events if e[3].startswith("WAKE_DETECTED accepted")]
wk_ign = [e for e in events if e[3].startswith("WAKE ignored")]
print(f"\nWAKE accepted={len(wk_acc)} ignored={len(wk_ign)}")
ends = collections.Counter(re.search(r"reason=(\w+)", m).group(1) for _, _, _, m in events if m.startswith("COMMAND_ENDPOINT"))
print("  command endpoint reasons:", dict(ends))

# ---- 5. mic health ---------------------------------------------------------
mic = collections.Counter()
for _, _, _, m in events:
    for k in ("MIC_UNAVAILABLE_DETECTED", "MIC_RECOVERY_ATTEMPT", "MIC_RECOVERY_SUCCEEDED", "MIC_RECOVERY_FAILED"):
        if m.startswith(k): mic[k] += 1
print("  mic health markers:", dict(mic))
