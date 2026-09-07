"""Fault-injection checkpoints for tests.

Services call ``checkpoint("<name>")`` at points where a failure or a pause is
interesting (e.g. right before commit). Tests install a callable for a name to
raise an exception or block until another session has acted; production never
installs anything, so the calls are no-ops.
"""

from collections.abc import Callable

_hooks: dict[str, Callable[[], None]] = {}


def checkpoint(name: str) -> None:
    hook = _hooks.get(name)
    if hook is not None:
        hook()


def install(name: str, hook: Callable[[], None]) -> None:
    _hooks[name] = hook


def clear() -> None:
    _hooks.clear()
