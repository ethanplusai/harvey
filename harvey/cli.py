"""The old ``harvey`` command: says it moved, then runs ``sellbird``."""

import sys

from harvey import NOTICE


def main():
    print(NOTICE, file=sys.stderr)
    from sellbird.cli import main as sellbird_main
    sys.argv[0] = "sellbird"
    return sellbird_main()
