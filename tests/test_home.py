"""The home folder: where config, knowledge and state live for each install.

A checkout stays in place, a pipx install gets a per-user folder seeded from
the wheel's shared data, Docker pins /app, and SELLBIRD_HOME beats them all.
The config lookup and the setup check must follow the home, not whatever
sellbird.yaml happens to sit in the working directory.
"""

import sys
from pathlib import Path

import pytest

import sellbird.config as config_mod
import sellbird.home as home_mod
import sellbird.main as main_mod
from sellbird import paths
from sellbird.home import bundled_defaults, seed_home
from sellbird.paths import resolve_home, user_home_default

ROOT = Path(__file__).resolve().parent.parent
REAL_SEED = seed_home

TRAINED = "persona:\n  company: Acme Roofing\n"
UNTRAINED = "persona:\n  company: Your Company\n"


def _make_checkout(root: Path) -> Path:
    (root / "sellbird").mkdir(parents=True)
    (root / "sellbird" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    return root


# ── resolve_home ──

def test_sellbird_home_env_wins(tmp_path):
    assert resolve_home(env={"SELLBIRD_HOME": str(tmp_path)},
                        package_root=tmp_path / "x") == tmp_path.resolve()


def test_sellbird_home_beats_a_checkout(tmp_path):
    checkout = _make_checkout(tmp_path / "repo")
    home = tmp_path / "home"
    assert resolve_home(env={"SELLBIRD_HOME": str(home)}, package_root=checkout) == home.resolve()


def test_blank_sellbird_home_is_ignored(tmp_path):
    checkout = _make_checkout(tmp_path / "repo")
    assert resolve_home(env={"SELLBIRD_HOME": "  "}, package_root=checkout) == checkout


def test_checkout_stays_in_place(tmp_path):
    (tmp_path / "sellbird").mkdir()
    (tmp_path / "sellbird" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert resolve_home(env={}, package_root=tmp_path) == tmp_path


def test_site_packages_is_never_home(tmp_path):
    root = _make_checkout(tmp_path / "lib" / "site-packages")
    got = resolve_home(env={}, package_root=root, platform="linux", home=tmp_path / "u")
    assert got != root
    assert got == tmp_path / "u" / ".local" / "share" / "sellbird"


@pytest.mark.parametrize("platform,expected_tail", [
    ("darwin", ("Library", "Application Support", "sellbird")),
    ("win32", ("Roaming", "sellbird")),
    ("linux", (".local", "share", "sellbird")),
])
def test_user_home_default_per_platform(platform, expected_tail, tmp_path):
    env = {"APPDATA": str(tmp_path / "AppData" / "Roaming")} if platform == "win32" else {}
    got = user_home_default(platform, env, tmp_path)
    assert got.parts[-len(expected_tail):] == expected_tail
    assert str(got).startswith(str(tmp_path))


def test_windows_without_appdata_falls_back_under_home(tmp_path):
    assert (user_home_default("win32", {}, tmp_path)
            == tmp_path / "AppData" / "Roaming" / "sellbird")


def test_windows_home_from_injected_env(tmp_path):
    appdata = tmp_path / "Users" / "e" / "AppData" / "Roaming"
    got = resolve_home(env={"APPDATA": str(appdata)}, package_root=tmp_path / "site-packages",
                       platform="win32", home=tmp_path / "ignored")
    assert got == appdata / "sellbird"


def test_linux_honours_xdg_data_home(tmp_path):
    xdg = tmp_path / "xdg"
    assert user_home_default("linux", {"XDG_DATA_HOME": str(xdg)}, tmp_path) == xdg / "sellbird"


def test_the_test_run_uses_the_checkout():
    # conftest drops an exported SELLBIRD_HOME before sellbird is imported .
    assert paths.PROJECT_ROOT == paths.PACKAGE_ROOT == ROOT


# ── Docker ──

def test_dockerfile_pins_home():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "SELLBIRD_HOME=/app" in text


def test_docker_app_is_not_a_checkout_but_pinned_home_wins(tmp_path):
    app = tmp_path / "app"      # sellbird/, prompts/, skills/, sellbird.yaml; no pyproject.toml
    (app / "sellbird").mkdir(parents=True)
    (app / "sellbird" / "__init__.py").write_text("", encoding="utf-8")
    assert not paths.is_checkout(app)
    assert resolve_home(env={}, package_root=app, platform="linux", home=tmp_path) != app
    assert resolve_home(env={"SELLBIRD_HOME": str(app)}, package_root=app) == app.resolve()


# ── seeding ──

def _source(root: Path) -> Path:
    (root / "prompts").mkdir(parents=True)
    (root / "skills").mkdir()
    (root / "prompts" / "writer.md").write_text("w", encoding="utf-8")
    (root / "skills" / "a.md").write_text("a", encoding="utf-8")
    (root / "sellbird.yaml").write_text("template", encoding="utf-8")
    (root / ".env.example").write_text("X=", encoding="utf-8")
    return root


def test_seed_copies_missing_and_never_overwrites(tmp_path):
    source = _source(tmp_path / "share")
    home = tmp_path / "home"
    home.mkdir()
    (home / "sellbird.yaml").write_text("mine", encoding="utf-8")
    created = seed_home(home, source)
    assert "prompts/writer.md" in created and "sellbird.yaml" not in created
    assert set(created) == {"prompts/writer.md", "skills/a.md", ".env.example"}
    assert (home / "sellbird.yaml").read_text(encoding="utf-8") == "mine" and (home / "data").is_dir()
    assert (home / "skills" / "a.md").read_text(encoding="utf-8") == "a"
    # A second run (an upgrade) changes nothing the user edited.
    (home / "prompts" / "writer.md").write_text("edited", encoding="utf-8")
    assert seed_home(home, source) == []
    assert (home / "prompts" / "writer.md").read_text(encoding="utf-8") == "edited"


def test_seed_into_itself_only_makes_data(tmp_path):
    home = _source(tmp_path / "repo")
    assert seed_home(home, home) == []
    assert (home / "data").is_dir()
    assert sorted(p.name for p in home.iterdir()) == sorted(
        ["prompts", "skills", "sellbird.yaml", ".env.example", "data"])


def test_seed_without_a_source_only_makes_data(tmp_path):
    home = tmp_path / "home"
    assert seed_home(home, None) == []
    assert [p.name for p in home.iterdir()] == ["data"]


def test_bundled_defaults(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "repo")
    monkeypatch.setattr(paths, "PACKAGE_ROOT", checkout)
    monkeypatch.setattr(paths, "PROJECT_ROOT", checkout)
    assert bundled_defaults() == checkout
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path / "lib" / "site-packages")
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path / "home")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert bundled_defaults() is None
    (tmp_path / "venv" / "share" / "sellbird").mkdir(parents=True)
    assert bundled_defaults() == tmp_path / "venv" / "share" / "sellbird"


def test_checkout_with_sellbird_home_seeds_from_the_checkout(tmp_path, monkeypatch):
    # A checkout run with SELLBIRD_HOME=<scratch>/home: the home is elsewhere,
    # but the defaults still come from the checkout.
    checkout = _source(_make_checkout(tmp_path / "repo"))
    (checkout / ".env").write_text("SECRET=x", encoding="utf-8")
    home = tmp_path / "scratch" / "home"
    monkeypatch.setattr(paths, "PACKAGE_ROOT", checkout)
    monkeypatch.setattr(paths, "PROJECT_ROOT", home)
    assert bundled_defaults() == checkout
    created = seed_home(home, bundled_defaults())
    assert set(created) == {"prompts/writer.md", "skills/a.md", "sellbird.yaml", ".env.example"}
    assert not (home / ".env").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_seed_never_writes_through_a_dangling_symlink(tmp_path):
    source = _source(tmp_path / "share")
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "outside" / "sellbird.yaml"
    (home / "sellbird.yaml").symlink_to(target)
    created = seed_home(home, source)
    assert "sellbird.yaml" not in created
    assert not target.exists() and (home / "sellbird.yaml").is_symlink()


def test_cli_seeding_error_is_one_stderr_line_and_the_command_runs(capsys, tmp_path, monkeypatch):
    import sellbird.cli as cli

    def broken(home, source):
        raise PermissionError(13, "Permission denied", str(home / "data"))

    monkeypatch.setattr(home_mod, "seed_home", broken)
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path / "home")
    monkeypatch.setattr(home_mod, "bundled_defaults", lambda: None)
    ran = []
    monkeypatch.setattr(cli, "cmd_status", lambda args: ran.append(True))
    monkeypatch.setattr(sys, "argv", ["sellbird", "status"])
    try:
        cli.main()
    except SystemExit as e:
        assert not e.code
    assert ran == [True]
    out, err = capsys.readouterr()
    assert out == ""
    lines = [line for line in err.splitlines() if line.strip()]
    assert len(lines) == 1 and "Traceback" not in err
    assert "Permission denied" in lines[0] and str(tmp_path / "home") in lines[0]


def test_cli_seeding_prints_nothing(capsys, tmp_path, monkeypatch):
    import sellbird.cli as cli

    source = _source(tmp_path / "share")
    home = tmp_path / "home"
    monkeypatch.setattr(home_mod, "seed_home", REAL_SEED)  # conftest stubs it out
    monkeypatch.setattr(paths, "PROJECT_ROOT", home)
    monkeypatch.setattr(home_mod, "bundled_defaults", lambda: source)
    ran = []
    monkeypatch.setattr(cli, "cmd_status", lambda args: ran.append(True))
    monkeypatch.setattr(sys, "argv", ["sellbird", "status"])
    try:
        cli.main()
    except SystemExit as e:
        assert not e.code
    assert ran == [True]
    assert capsys.readouterr().out == ""
    assert (home / "prompts" / "writer.md").is_file() and (home / "data").is_dir()


# ── the config lookup follows the home ──

def _cwd_with_config(tmp_path, monkeypatch) -> Path:
    elsewhere = tmp_path / "some-other-checkout"
    elsewhere.mkdir()
    (elsewhere / "sellbird.yaml").write_text(UNTRAINED, encoding="utf-8")
    (elsewhere / "sellbird.local.yaml").write_text(UNTRAINED, encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    return elsewhere


def test_sellbird_home_config_beats_the_working_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "sellbird.yaml").write_text(TRAINED, encoding="utf-8")
    _cwd_with_config(tmp_path, monkeypatch)
    monkeypatch.setenv("SELLBIRD_HOME", str(home))
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    assert Path(config_mod._find_config_file()) == home / "sellbird.yaml"
    (home / "sellbird.local.yaml").write_text(TRAINED, encoding="utf-8")
    assert Path(config_mod._find_config_file()) == home / "sellbird.local.yaml"


def test_pipx_home_config_beats_the_working_directory(tmp_path, monkeypatch):
    home = tmp_path / "Application Support" / "sellbird"   # not a checkout, no env
    home.mkdir(parents=True)
    (home / "sellbird.yaml").write_text(TRAINED, encoding="utf-8")
    _cwd_with_config(tmp_path, monkeypatch)
    monkeypatch.delenv("SELLBIRD_HOME", raising=False)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    assert Path(config_mod._find_config_file()) == home / "sellbird.yaml"


def test_missing_home_config_never_falls_back_to_the_working_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _cwd_with_config(tmp_path, monkeypatch)
    monkeypatch.setenv("SELLBIRD_HOME", str(home))
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    with pytest.raises(config_mod.ConfigFileNotFoundError):
        config_mod._find_config_file()


def test_checkout_still_reads_the_working_directory_first(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "repo")
    (checkout / "sellbird.yaml").write_text(TRAINED, encoding="utf-8")
    cwd = _cwd_with_config(tmp_path, monkeypatch)
    monkeypatch.delenv("SELLBIRD_HOME", raising=False)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", checkout)
    assert Path(config_mod._find_config_file()) == cwd / "sellbird.local.yaml"


def test_docker_app_finds_its_mounted_config(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "sellbird").mkdir(parents=True)
    (app / "sellbird.yaml").write_text(TRAINED, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SELLBIRD_HOME", str(app))
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", resolve_home(package_root=app))
    assert Path(config_mod._find_config_file()) == (app / "sellbird.yaml").resolve()


# ── the setup check reads the home's config, never the working directory's ──

def test_trained_home_with_env_needs_no_setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "sellbird.yaml").write_text(TRAINED, encoding="utf-8")
    (home / ".env").write_text("X=1\n", encoding="utf-8")
    _cwd_with_config(tmp_path, monkeypatch)            # untrained config in cwd
    monkeypatch.setenv("SELLBIRD_HOME", str(home))
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    monkeypatch.setattr(paths, "PROJECT_ROOT", home)
    assert main_mod._needs_setup() is False


def test_untrained_or_missing_home_config_needs_setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("X=1\n", encoding="utf-8")
    _cwd_with_config(tmp_path, monkeypatch)
    monkeypatch.setenv("SELLBIRD_HOME", str(home))
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    monkeypatch.setattr(paths, "PROJECT_ROOT", home)
    assert main_mod._needs_setup() is True
    (home / "sellbird.yaml").write_text(UNTRAINED, encoding="utf-8")
    assert main_mod._needs_setup() is True


# ── an installed app skips the checkout-only steps ──

class _Done:
    returncode = 0


def test_install_outside_a_checkout_skips_pip(tmp_path, monkeypatch, capsys):
    import argparse

    import sellbird.cli as cli

    calls = []
    monkeypatch.setattr(paths, "PACKAGE_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _Done())
    cli.cmd_install(argparse.Namespace(_project_root=str(tmp_path / "home")))
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]
    out = capsys.readouterr().out
    assert "already part of this install" in out and "Python packages installed" not in out


def test_install_in_a_checkout_still_runs_pip_there(tmp_path, monkeypatch):
    import argparse

    import sellbird.cli as cli

    checkout = _make_checkout(tmp_path / "repo")
    calls = []
    monkeypatch.setattr(paths, "PACKAGE_ROOT", checkout)
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw.get("cwd"))) or _Done())
    cli.cmd_install(argparse.Namespace(_project_root=str(tmp_path / "elsewhere")))
    assert calls[0] == ([sys.executable, "-m", "pip", "install", "-e", "."], str(checkout))


@pytest.mark.asyncio
async def test_setup_status_venv_check_is_done_outside_a_checkout(tmp_path, monkeypatch):
    import sellbird.dashboard as dash

    async def venv_done():
        checks = (await dash.get_setup_status())["checks"]
        return next(c for c in checks if c["id"] == "venv")["done"]

    monkeypatch.setattr(dash, "PACKAGE_ROOT", tmp_path / "site-packages")
    assert await venv_done() is True
    checkout = _make_checkout(tmp_path / "repo")
    monkeypatch.setattr(dash, "PACKAGE_ROOT", checkout)
    assert await venv_done() is False
    (checkout / ".venv").mkdir()
    assert await venv_done() is True


# ── an installed app ships the defaults it seeds from ──

def test_the_package_ships_every_prompt_and_skill():
    import tomllib

    data_files = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["data-files"]
    assert set(data_files["share/sellbird"]) == {"sellbird.yaml", ".env.example"}
    assert data_files["share/sellbird/prompts"] == ["prompts/*.md"]
    assert data_files["share/sellbird/skills"] == ["skills/*.md"]
    assert set(home_mod.SEED_GLOBS) == {"prompts/*.md", "skills/*.md", "sellbird.yaml",
                                        ".env.example"}
    assert list((ROOT / "prompts").glob("*.md")) and list((ROOT / "skills").glob("*.md"))
