"""PROTOTYPE (scratch): does 'resolve() then is_relative_to(protected)' hold up against Windows path tricks?
Temp dirs only. Junctions/hardlinks created with mklink (no admin needed)."""
import os, subprocess, sys, tempfile, ctypes
from pathlib import Path

base = Path(tempfile.mkdtemp(prefix="void_pathproto_")).resolve()
allowed = base / "allowed"; state = base / "Void State Dir"; allowed.mkdir(); state.mkdir()
secret = state / "device_key.pem"; secret.write_text("DUMMY")
protected = state.resolve()

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

# junction (no privilege) and hardlink (same volume)
r = sh(f'mklink /J "{allowed / "jn"}" "{state}"'); junction_ok = r.returncode == 0
r2 = sh(f'mklink /H "{allowed / "hl.pem"}" "{secret}"'); hardlink_ok = r2.returncode == 0

def short(p):
    try:
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024); return buf.value
    except Exception: return None
sp = short(secret)
drive = str(base)[:2]                      # e.g. 'C:'
rest = str(secret)[2:]
unc = rf"\\localhost\{drive[0]}$" + rest

cases = [
 ("exact child",                    str(secret)),
 ("case variant",                   str(secret).upper()),
 ("extended prefix \\\\?\\",        "\\\\?\\" + str(secret)),
 ("junction alias (existing file)", str(allowed / "jn" / "device_key.pem")),
 ("junction alias (NEW file)",      str(allowed / "jn" / "planted_new.json")),
 ("trailing dot on dir",            str(state) + "." + "\\device_key.pem"),
 ("trailing space on dir",          str(state) + " " + "\\device_key.pem"),
 ("ADS :$DATA",                     str(secret) + "::$DATA"),
 ("dir ADS ::$INDEX_ALLOCATION",    str(state) + "::$INDEX_ALLOCATION\\device_key.pem"),
 ("8.3 short name",                 sp if sp and sp.lower() != str(secret).lower() else "(8.3 names not available on this volume)"),
 ("UNC admin share \\\\localhost\\C$", unc),
 ("relative traversal ..\\",        str(allowed / ".." / state.name / "device_key.pem")),
 ("mixed slashes",                  str(secret).replace("\\", "/")),
 ("hardlink alias (pre-existing)",  str(allowed / "hl.pem")),
]

def naive_protected(candidate: str) -> bool:
    try:
        p = Path(candidate).resolve()
    except Exception as e:
        return True                       # fail closed on any error
    return p == protected or p.is_relative_to(protected)

def identity_protected(candidate: str) -> bool:
    """Second, independent check: same file/dir by OS identity (catches hardlinks, UNC, junction, 8.3)."""
    try:
        if os.path.exists(candidate):
            cur = Path(candidate)
            if os.path.samefile(candidate, secret): return True
            for parent in [cur] + list(cur.parents):
                try:
                    if os.path.samefile(parent, state): return True
                except OSError: pass
    except Exception:
        return True
    return False

print(f"junction created: {junction_ok} | hardlink created: {hardlink_ok} | 8.3 name: {sp}")
print(f"{'case':38s} {'resolve+prefix':>15s} {'+ identity':>11s}  resolved ->")
for label, cand in cases:
    if cand.startswith("(8.3"):
        print(f"{label:38s} {'n/a':>15s} {'n/a':>11s}  {cand}"); continue
    n = naive_protected(cand); i = n or identity_protected(cand)
    try: res = str(Path(cand).resolve())
    except Exception as e: res = f"<{type(e).__name__}>"
    print(f"{label:38s} {('DENIED' if n else 'BYPASS!'):>15s} {('DENIED' if i else 'BYPASS!'):>11s}  {res[:70]}")
