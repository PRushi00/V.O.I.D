"""Scratch pytest plugin: in-memory keyring that RECORDS which tests would have hit the real
Windows Credential Manager (and whether they set/delete, i.e. mutate it)."""
import os, collections, atexit, json
import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

CALLS = collections.defaultdict(lambda: collections.Counter())

def _who():
    cur = os.environ.get("PYTEST_CURRENT_TEST", "<import/collection>")
    return cur.split(" ")[0]

class SpyKeyring(KeyringBackend):
    priority = 100
    def __init__(self): super().__init__(); self._d = {}
    def get_password(self, s, u): CALLS[_who()]["get"] += 1; return self._d.get((s, u))
    def set_password(self, s, u, p): CALLS[_who()]["set"] += 1; self._d[(s, u)] = p
    def delete_password(self, s, u):
        CALLS[_who()]["delete"] += 1
        try: del self._d[(s, u)]
        except KeyError: raise PasswordDeleteError("nf")

keyring.set_keyring(SpyKeyring())

@atexit.register
def _dump():
    by_file = collections.defaultdict(lambda: collections.Counter()); tests_by_file = collections.defaultdict(set)
    for tid, c in CALLS.items():
        f = tid.split("::")[0]; by_file[f].update(c); tests_by_file[f].add(tid)
    out = {f: {"tests_touching_keyring": len(tests_by_file[f]), **dict(c)} for f, c in sorted(by_file.items())}
    with open(os.path.join(os.path.dirname(__file__), "keyring_spy_result.json"), "w") as fh:
        json.dump(out, fh, indent=1)
