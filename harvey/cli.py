"""The old ``harvey`` command: says it moved, then runs ``sellbird``."""

import sys
from pathlib import Path

from harvey import NOTICE


def _find_sellbird():
    """An editable install made before the rename only maps the ``harvey``
    package, so after a plain ``git pull`` Python can't see ``sellbird``
    until ``pip install -e .`` runs again. It sits next to this package in
    the same checkout, so point Python at the checkout."""
    try:
        import sellbird  # noqa: F401
    except ModuleNotFoundError:
        checkout = Path(__file__).resolve().parent.parent
        if (checkout / "sellbird" / "__init__.py").is_file():
            sys.path.insert(0, str(checkout))


def main():
    print(NOTICE, file=sys.stderr)
    _find_sellbird()
    from sellbird.cli import main as sellbird_main
    sys.argv[0] = "sellbird"
    return sellbird_main()
