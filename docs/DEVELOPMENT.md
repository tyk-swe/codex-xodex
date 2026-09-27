# Development and verification

## Local checks

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q -W error
python -m compileall -q src scripts tests
bash -n scripts/install-user.sh scripts/build-worker.sh
```

Asyncio debug mode is enabled in `pyproject.toml`. Tests exercise task lifecycle, idempotency, confinement, publication invariants,
real local Git objects/refs, subprocesses, PTYs and private Unix-socket HTTP.
Setup tests use temporary directories and mocked host commands to exercise fresh
configuration, cancellation, reruns, secret modes/redaction, worker failures,
service conflicts/startup and incomplete tunnels. The bootstrap and compatibility
wrappers are also exercised. Setup never creates tasks or publication records.
Ordinary execution uses `LocalTestBackend` under `tests/`; GitHub responses are
fake or mocked. These tests neither open live PRs nor prove Podman isolation.

Two opt-in Podman tests require a configured unprivileged host and built worker:

```bash
xodex doctor --probe
XODEX_PODMAN_TESTS=1 python -m pytest -q -m podman
```

Do not count skipped integration checks as successes. Complete the disposable
GitHub/tunnel/ChatGPT acceptance flow in [deployment](DEPLOYMENT.md). CI covers
Python 3.11–3.13; a result on one interpreter does not establish the entire matrix.

## Package and test the deliverables

```bash
python -m pip wheel --no-deps . -w dist
python scripts/release.py
```

The ZIP includes selected source files, deterministic timestamps, executable
script modes and a generated `SHA256SUMS`. From an extracted archive root:

```bash
sha256sum -c SHA256SUMS
python -m pytest -q -W error
```

Checksums detect changes; they are not signed provenance. Build products, caches
and verification logs are not source documentation. Keep run-specific output as
CI artifacts or outside the project, not as checked-in pass-count claims.

Test the installed wheel without a source import path:

```bash
root="$PWD"
python=$(python -c 'import sys; print(sys.executable)')
wheel_dir=$(mktemp -d)
"$python" -m pip install --no-deps --target "$wheel_dir" "$root"/dist/*.whl
(cd "$wheel_dir" && PYTHONPATH="$wheel_dir" "$python" -m pytest -q -W error -o pythonpath= "$root/tests")
```

This reuses the development interpreter and its third-party test dependencies; it is not a hermetic install.
Release tests still inspect source packaging inputs. The CI workflow runs source,
installed-wheel and extracted-archive checks and retains their results. Installed
wheel checks must also run the copied setup tests and all new command help screens
from a temporary directory, without a checkout on the import path. The packaged
assets must be available through `importlib.resources`; there are no runtime
copies in `deploy/systemd` or `worker`.

## Tool schemas and versions

`src/xodex/tools.py` defines the catalog used by MCP discovery and validation.
Export it when needed rather than committing a duplicate snapshot:

```bash
python -c 'import json; from xodex.tools import CATALOG; print(json.dumps([tool.wire() for tool in CATALOG], indent=2))'
```

`xodex.__version__` is the application version authority; package metadata,
CLI/MCP reporting and worker builds use it. Update the deployment image example
when changing it. `SCHEMA_VERSION` in `store.py` independently identifies SQLite
storage. The initial baseline is application `0.1.0`, storage `1`, engine API `v1`.
There is no migration framework. MCP, GitHub REST and tunnel versions describe
upstream contracts and must not be reset with the application release.
