# Extensions

An extension is a separate Python package that adds to Sellbird without
patching it. It can add:

- **dashboard routes**: served behind the dashboard's sign-in, after every
  built-in route, so an extension can add paths but never replace one;
- **tick steps**: async functions run once at the end of each heartbeat cycle;
- **commands**: `sellbird <name>` subcommands. A name the built-in CLI
  already uses is refused.

A plain install has no extensions and behaves exactly as without them.

## Writing one

Declare an entry point in the `sellbird.extensions` group. Its value is a
function that takes the registry:

```toml
# your package's pyproject.toml
[project.entry-points."sellbird.extensions"]
myext = "myext.sellbird_plugin:install"
```

```python
# myext/sellbird_plugin.py
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/myext/hello")
async def hello():
    return {"hello": "world"}


async def sync(ctx):              # ctx.config, ctx.state (a StateManager)
    ...


def configure(parser):            # add arguments, return the function to run
    parser.add_argument("--dry-run", action="store_true")
    return lambda args: print("ran", args.dry_run)


def install(registry):
    registry.add_router(router)
    registry.add_tick_step("myext.sync", sync)
    registry.add_cli_command("myext", configure, help="Run myext")
```

## The rules

- **Extensions load once per process,** on first use, in entry-point name
  order.
- **A failing `install` stops Sellbird with the extension's name.** It never
  runs with half an extension installed.
- **Tick steps are isolated.** A step that raises, or runs past two minutes,
  is logged and skipped. It never stops the heartbeat or the other steps.
- **Names are short lowercase words:** letters, digits, `.`, `_` or `-`.
  Prefix tick steps with your extension's name (`myext.sync`).
- **Extensions are trusted code.** They run in Sellbird's process with
  Sellbird's access to your data and keys. Install only ones you would
  install as any other Python package.

## Running Sellbird for someone else

`sellbird.dashboard.build_app(guard=, routers=)` returns a new app that
serves every built-in route, with your routers first and your own sign-in
middleware in place of the dashboard token. Set `app.state.home` to a
`sellbird.workspace_home.Home`, and each request runs with that home's files
and database. Features that only make sense on the user's own computer call
`require_self_hosted()`, which answers `409` on a hosted home.
