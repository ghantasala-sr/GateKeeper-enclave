"""A per-request trace of what the backend did, returned with each /chat answer.

It powers the frontend's "Under the hood" panel. Everything in it is either
the caller's own data (their claims, chunks they're allowed to see) or fixed
facts about the system, so showing it to the caller leaks nothing new.
"""

import time
from contextlib import contextmanager


class RequestTrace:
    def __init__(self):
        self._start = time.perf_counter()
        self.steps: list[dict] = []

    def _now_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 1)

    @contextmanager
    def step(self, name: str, layer: str):
        """Time a step. Fill the yielded dict with details; set ok=False if it failed."""
        started = self._now_ms()
        entry = {"name": name, "layer": layer, "ok": True, "start_ms": started, "details": {}}
        self.steps.append(entry)
        try:
            yield entry
        except Exception as e:
            entry["ok"] = False
            entry["details"].setdefault("error", str(e) or type(e).__name__)
            raise
        finally:
            entry["ms"] = round(self._now_ms() - started, 1)

    def to_dict(self) -> dict:
        return {"total_ms": self._now_ms(), "steps": self.steps}
