# Dependency manifests

## Why this directory exists

The owner deliberately moved the project's top-level files — `README.md`, `pytest.ini`, `.gitignore` and
the original `requirements*.txt` — into `workspace/`, so the repository root is not cluttered with them.
Those files were **not** moved back, and nothing here replaces them.

That left the repository with no *tracked* dependency manifest: `workspace/` is untracked, so the pins for
what V3 added would have lived nowhere in git history. A dedicated directory is the clean answer — the
root stays uncluttered, the owner's files stay where they put them, and the versions V.O.I.D actually
depends on are recorded somewhere a reviewer can find them.

| File | Contents |
| --- | --- |
| `v3-capabilities.txt` | Browser, desktop, artifact and perception dependencies (V3 capability layers) |
| `v3-interop.txt` | Observability and interoperability (OpenTelemetry, AG-UI, A2A) |

The owner's base manifests remain at `workspace/requirements.txt`, `workspace/requirements.lock` and the
three `workspace/requirements-*.txt`. Install those first; these are additive.

```bash
pip install -r workspace/requirements.txt
pip install -r requirements/v3-capabilities.txt
pip install -r requirements/v3-interop.txt
```

## Pinning policy

Everything here is pinned to an exact version, not a range.

- **OpenTelemetry** must agree across API, SDK and exporter: a version skew between them is a data-model
  mismatch, not just a different feature set. The API was already present at 1.45.0, so the SDK and
  exporter are pinned to 1.45.0 to match.
- **Protocol packages** (AG-UI, A2A) define event and message schemas. A schema that changes under a
  running front end or a connected agent is a broken integration, so these track a chosen release rather
  than latest.
- **Capability libraries** (Playwright, uiautomation, the document libraries) are pinned because they
  drive real browsers and real windows, where a behaviour change is a correctness and a safety matter.

Every entry was checked for Python 3.14 compatibility on this machine before being added, and each is the
official package from PyPI — no forks, no vendored copies, no git URLs.
