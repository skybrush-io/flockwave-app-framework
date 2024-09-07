from __future__ import annotations

import sys

from functools import partial
from typing import Callable


__all__ = ("install_unraisable_hook",)


def _ignore_irrelevant_unraisable_exceptions(
    log_unraisable: Callable[[sys.UnraisableHookArgs], None],
    args: sys.UnraisableHookArgs,
) -> None:
    if args.exc_type is AttributeError and args.exc_value is not None:
        # AttributeError: 'NoneType' object has no attribute '_alive',
        # probably from a WeakMethod
        exc_args = args.exc_value.args
        if len(exc_args) == 1 and "_alive" in exc_args[0]:
            return

    log_unraisable(args)


def install_unraisable_hook() -> None:
    """Installs a system-wide "unraisable exception" hook handler that is used
    to hide some harmless messages that sometimes appear when shutting down.
    """

    # During shutdown, we frequently see harmless exceptions like this,
    # especially with Python 3.12.
    #
    # Exception ignored in: <function WeakMethod.__new__.<locals>._cb at 0x10bc19700>
    # Traceback (most recent call last):
    #   File "weakref", line 58, in _cb
    #   AttributeError: 'NoneType' object has no attribute '_alive'
    #
    # We attempt to hide these here.

    sys.unraisablehook = partial(
        _ignore_irrelevant_unraisable_exceptions, sys.unraisablehook
    )
