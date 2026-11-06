"""Harvey is now Sellbird.

This package only keeps the old ``harvey`` command and ``python -m harvey``
working for a while after the rename, including for clones that ran
``git pull`` without reinstalling. Everything lives in ``sellbird``.
"""

NOTICE = ("Harvey is now Sellbird. Run `pip install -e .` once, then use `sellbird` "
          "instead of `harvey`. The `harvey` command will be removed in a later release.")
