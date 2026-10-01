import subprocess, sys, time, os
S = os.path.dirname(os.path.abspath(__file__)); py = r"C:\V.O.I.D\.venv\Scripts\python.exe"
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1")
for cfg in ["cpu0", "cpu8", "cpu16", "cuda_fp16", "cuda_i8f16"]:     # CPU first: cheap and safe; CUDA last
    t0 = time.time()
    try:
        r = subprocess.run([py, "-u", os.path.join(S, "stt_bench.py"), cfg], capture_output=True, text=True, timeout=120, env=env)
        out = (r.stdout.strip().splitlines() or ["<no stdout>"])
        print(f"### {cfg} (rc={r.returncode}, {time.time()-t0:.0f}s)", flush=True)
        for line in out: print("   ", line[:400], flush=True)
        if r.stderr.strip(): print("    stderr:", r.stderr.strip().splitlines()[-1][:200], flush=True)
    except subprocess.TimeoutExpired as e:
        print(f"### {cfg}: TIMEOUT after 120s (hang). partial stdout: {(e.stdout or b'').decode(errors='replace')[-300:] if isinstance(e.stdout,(bytes,bytearray)) else (e.stdout or '')[-300:]}", flush=True)
print("driver done", flush=True)
