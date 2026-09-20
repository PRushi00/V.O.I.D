"""A real, separate OS process for the persistence tests (not a test module).

Usage: python tests/_memory_child.py <spec.json>

The two processes share nothing except files on disk: the encrypted database and a
file-backed keyring (the stand-in for Windows Credential Manager, which also outlives a
process). Each step is a JSON object; results are printed as one JSON line.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from keyring.backend import KeyringBackend


class FileKeyring(KeyringBackend):
    priority = 1

    def __init__(self, path):
        super().__init__()
        self._path = Path(path)

    def _load(self):
        return json.loads(self._path.read_text()) if self._path.exists() else {}

    def get_password(self, service, username):
        return self._load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        d = self._load()
        d[f"{service}/{username}"] = password
        self._path.write_text(json.dumps(d))

    def delete_password(self, service, username):
        d = self._load()
        d.pop(f"{service}/{username}", None)
        self._path.write_text(json.dumps(d))


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    keyring.set_keyring(FileKeyring(spec["keyring_file"]))
    from void.config import Config
    from void.memory.service import MemoryService

    cfg = Config({"app": {"state_dir": spec["state_dir"]}, "security": {"allowed_roots": []}})
    out = {}
    if spec["op"] == "remember":
        svc = MemoryService(Path(spec["state_dir"]) / "memory.sqlite")
        res = svc.remember(spec["text"], channel="cli")
        out = {"status": res.status, "id": res.item.id if res.item else None}
    elif spec["op"] == "inspect":
        svc = MemoryService(Path(spec["state_dir"]) / "memory.sqlite")
        items = svc.list(include_superseded=True)
        ctx = svc.build_context(spec["query"])
        out = {"items": [{"id": i.id, "text": i.text, "kind": i.kind, "origin": i.origin, "status": i.status,
                          "sensitivity": i.sensitivity, "cloud_ok": i.cloud_ok, "created_at": i.created_at}
                         for i in items],
               "context": ctx.text if ctx else None, "pid": __import__("os").getpid()}
    elif spec["op"] == "assistant_run":
        from tests.helpers import FakeProvider
        from void.app import Assistant
        from void.providers.base import LLMResponse
        from void.providers.registry import ProviderRegistry

        a = Assistant(config=cfg)
        provider = FakeProvider([LLMResponse(text="ok")])
        a.providers = ProviderRegistry({"fake": provider}, ["fake"])
        res = a.run(spec["goal"])
        out = {"status": res.status, "result": res.result, "provider_calls": provider.calls,
               "first_call": provider.seen_messages[0] if provider.seen_messages else None,
               "pid": __import__("os").getpid()}
    print("RESULT" + json.dumps(out))


main()
