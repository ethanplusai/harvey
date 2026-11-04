"""Opt-in live discovery probe; prints counts only, never writes prospects.

Run from the repository: python -m scripts.check_profile_search --engine bing
Without --config, no local config, .env, browser, LLM, or database is used.
--config explicitly uses that file and the existing environment keys.
"""
import argparse
import asyncio
import json
import logging
from types import SimpleNamespace

from harvey.agents.scout import Scout
from harvey.config import EnvConfig, SearchConfig, load_config, load_env


class ProbeState:
    def __init__(self):
        self.count = 0

    async def prospect_exists(self, **kwargs):
        return False

    async def add_prospect(self, prospect):
        self.count += 1  # Do not retain personal data or write the database.


async def probe(args):
    if args.config:
        config = load_config(args.config)
        env = load_env()
    else:
        config = SimpleNamespace(
            search=SearchConfig(),
            icp=SimpleNamespace(titles=["Head of Growth"], geography=[], industries=[]),
        )
        env = EnvConfig()
    if args.engine:
        config.search.engine = args.engine
    config.search.fallback = False  # One backend, one query, one attempt.
    config.icp.titles = [args.title or (config.icp.titles or ["Head of Growth"])[0]]
    state = ProbeState()
    scout = Scout(None, state, config, env)
    fetch = scout._fetch

    async def once(url, **kwargs):
        kwargs["retries"] = 1
        kwargs["timeout"] = 12.0
        return await fetch(url, **kwargs)

    scout._fetch = once
    count = await scout._prospect_via_profile_search()
    print(json.dumps({"engine": config.search.engine, "profiles_found": count,
                      "queries": scout._queries_this_cycle, "persistent_writes": 0}))
    return 0 if count else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["auto", "serper", "tinyfish", "duckduckgo", "bing", "google"])
    parser.add_argument("--title")
    parser.add_argument("--config", help="Opt in to an existing config and environment keys")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
