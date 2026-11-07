"""use_config: settings that come from somewhere other than sellbird.yaml."""

import asyncio
from pathlib import Path

import pytest
import yaml

from sellbird import config as config_mod
from sellbird.config import ConfigFileNotFoundError, current_config, load_config, use_config
from sellbird.workspace_home import Home, use_home

TEMPLATE = Path(__file__).resolve().parent.parent / "sellbird.yaml"


def _write(folder: Path, company: str) -> Path:
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    data["persona"]["company"] = company
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "sellbird.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _config(folder: Path, company: str = "Acme Roofing"):
    return load_config(str(_write(folder, company)))


def test_use_config_wins_over_the_file_and_resets(tmp_path):
    given = _config(tmp_path, "Given Co")
    assert current_config() is None
    with use_config(given):
        assert load_config() is given and current_config() is given
    assert current_config() is None


def test_an_explicit_path_still_reads_that_file(tmp_path):
    given = _config(tmp_path / "given", "Given Co")
    other = _write(tmp_path / "other", "Acme Roofing")
    with use_config(given):
        assert load_config(str(other)).persona.company == "Acme Roofing"


def test_a_hosted_home_without_loaded_settings_is_an_error(tmp_path):
    with use_home(Home.for_root(tmp_path, mode="hosted")):
        with pytest.raises(ConfigFileNotFoundError, match="weren't loaded"):
            load_config()
        given = _config(tmp_path)
        with use_config(given):
            assert load_config() is given


@pytest.mark.asyncio
async def test_each_task_and_thread_keeps_its_own(tmp_path):
    a, b = _config(tmp_path / "a", "A Co"), _config(tmp_path / "b", "B Co")

    async def run(cfg):
        with use_config(cfg):
            await asyncio.sleep(0)
            in_thread = await asyncio.to_thread(load_config)
            return load_config().persona.company, in_thread.persona.company

    assert await asyncio.gather(run(a), run(b)) == [("A Co", "A Co"), ("B Co", "B Co")]
    assert config_mod.current_config() is None
