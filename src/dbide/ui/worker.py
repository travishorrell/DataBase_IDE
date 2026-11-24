"""Run blocking database calls off the GUI thread."""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set["_Task"] = set()  # keep tasks alive until their signals fire


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001
            orig = getattr(exc, "orig", None)
            msg = f"{type(orig or exc).__name__}: {orig or exc}"
            traceback.print_exc()
            self.signals.failed.emit(msg)
        else:
            self.signals.done.emit(result)


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    task = _Task(fn)
    _live.add(task)

    def finish_ok(result):
        _live.discard(task)
        if on_done:
            _call(on_done, result)

    def finish_err(msg):
        _live.discard(task)
        if on_error:
            _call(on_error, msg)

    task.signals.done.connect(finish_ok)
    task.signals.failed.connect(finish_err)
    QThreadPool.globalInstance().start(task)


def _call(fn, arg) -> None:
    try:
        fn(arg)
    except RuntimeError as exc:
        # The widget that asked for this result was closed while it was loading.
        if "already deleted" not in str(exc):
            raise
