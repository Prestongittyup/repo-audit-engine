from __future__ import annotations

from time import perf_counter


class Stopwatch:
    def __init__(self) -> None:
        self._start = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "Stopwatch":
        self._start = perf_counter()
        self.elapsed = 0.0
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.elapsed = perf_counter() - self._start
