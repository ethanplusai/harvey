"""Opt-in: build the wheel from a copy of the sources, install it into an
empty target, and use it with no working-tree imports.

    SELLBIRD_PACKAGE_TESTS=1 pytest tests/test_packaging.py

Needs setuptools in the running environment (no network: the build runs
without isolation). Covers the CLI entry point, the version preflight, a
Brain call through the installed Codex adapter to a scripted `codex` found
on PATH (a real executable, no monkeypatching), and the dashboard assets.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).with_name("fake_codex.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("SELLBIRD_PACKAGE_TESTS") != "1" or sys.platform == "win32",
    reason="set SELLBIRD_PACKAGE_TESTS=1 (needs setuptools); Codex runs on macOS/Linux only",
)

CHECK = textwrap.dedent('''
    import asyncio, base64, contextlib, io, json, os, sys, tempfile, time
    from datetime import datetime, timezone
    SITE, FAKE, WORK = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, SITE)
    # An editable install of the checkout must not shadow the wheel.
    sys.meta_path[:] = [f for f in sys.meta_path
                        if "editable" not in type(f).__name__.lower()
                        and "editable" not in getattr(f, "__module__", "")]
    import sellbird, sellbird.codex as hc
    from sellbird.brain import Brain
    from sellbird.config import BrainConfig
    from sellbird.state import StateManager
    assert os.path.realpath(sellbird.__file__).startswith(os.path.realpath(SITE)), sellbird.__file__

    def cli(*argv):
        from sellbird.cli import main
        out = io.StringIO()
        sys.argv = ["sellbird", *argv]
        with contextlib.redirect_stdout(out):
            try:
                main()
            except SystemExit:
                pass
        return out.getvalue()

    assert "train" in cli("--help")
    train = cli("train", "--help")
    assert "--provider" in train and "--model" in train

    bindir = os.path.join(WORK, "bin dir ü")
    os.makedirs(bindir)
    if os.name == "nt":
        with open(os.path.join(bindir, "codex.cmd"), "w") as f:
            f.write(f'@"{sys.executable}" "{FAKE}" smart "{WORK}\\\\fake.log" %*\\n')
    else:
        path = os.path.join(bindir, "codex")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\\nexec "{sys.executable}" "{FAKE}" smart "{WORK}/fake.log" "$@"\\n')
        os.chmod(path, 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
    native = os.path.join(WORK, "native")
    os.makedirs(native)
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    jwt = lambda c: f"{enc({'alg': 'none'})}.{enc(c)}.sig"
    auth = os.path.join(native, "auth.json")
    with open(auth, "w") as f:
        json.dump({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
            "id_token": jwt({"email": "x@example.invalid"}),
            "access_token": jwt({"exp": time.time() + 86400}),
            "refresh_token": "fake-refresh", "account_id": "a"},
            "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, f)
    os.chmod(auth, 0o600)
    os.environ["CODEX_HOME"] = native
    tempfile.tempdir = os.path.join(WORK, "tmp")
    os.makedirs(tempfile.tempdir)

    assert hc.check_cli_version() == "0.159.3"

    async def main():
        state = StateManager(os.path.join(WORK, "sellbird.db"))
        await state.init_db()
        brain = Brain(state, BrainConfig(provider="codex", model="gpt-5.5"))
        assert await brain.preflight() == ""
        answer = await brain.think("Classify this email reply: could you share pricing?")
        assert answer == "interested", answer
        assert await state.get_usage_today("codex") == 1

    asyncio.run(main())
    assert os.listdir(tempfile.tempdir) == []
    import sellbird.dashboard as dash
    from sellbird import claude_runtime  # noqa: F401
    web = os.path.join(os.path.dirname(sellbird.__file__), "web")
    assert os.path.exists(os.path.join(web, "app.js"))
    assert os.path.exists(os.path.join(web, "auth.js"))
    # The installed dashboard is locked: the token lives under WORK here.
    from fastapi.testclient import TestClient
    dash.token_store.configure(os.path.join(WORK, "data", "dashboard-token"))
    dash.DB_PATH = __import__("pathlib").Path(WORK, "dash.db")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        assert c.get("/").status_code == 200 and token not in c.get("/").text
        assert c.get("/static/auth.js").status_code == 200
        assert c.get("/api/companies").status_code == 401
        assert c.get("/openapi.json").status_code == 401
        ok = {"Authorization": "Bearer " + token}
        assert c.get("/api/companies", headers=ok).status_code == 200
        assert c.get("/openapi.json", headers=ok).status_code == 404
    print("WHEEL-OK")
''')


def test_installed_wheel_runs_codex_without_the_checkout(tmp_path):
    pytest.importorskip("setuptools")
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(ROOT / name, src / name)
    shutil.copytree(ROOT / "sellbird", src / "sellbird",
                    ignore=shutil.ignore_patterns("__pycache__"))
    dist, site, work = tmp_path / "dist", tmp_path / "site", tmp_path / "work"
    work.mkdir()
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps",
                    "--no-build-isolation", "-w", str(dist), str(src)],
                   check=True, cwd=tmp_path, timeout=300)
    wheel = next(dist.glob("sellbird-*.whl"))
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                    "--target", str(site), str(wheel)], check=True, timeout=300)
    script = tmp_path / "check.py"
    script.write_text(CHECK)
    out = subprocess.run([sys.executable, "-I", str(script), str(site), str(FAKE), str(work)],
                         cwd=work, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "WHEEL-OK" in out.stdout
