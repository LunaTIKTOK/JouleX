"""Persistent challenge store (M3).

The replay defence must survive a restart: a challenge seen before
the restart is still rejected after it, and an issued-but-unused
challenge still verifies after it. State lives in one atomic JSON file
(``data/challenges.json``) written through :class:`JsonStore`, which
already writes temp-file + fsync + ``os.replace``.

File format::

    {"issued": {nonce: expiry_iso, ...}, "seen": {nonce: expiry_iso, ...}}

``seen`` carries each challenge's expiry (not the time it was seen):
a seen challenge only needs remembering while its challenge could
still be presented — once the challenge expires, a replay is rejected
as unknown anyway, so expired entries are pruned on load and on save
and the file stays bounded. A corrupt file refuses startup
(fail closed): the exchange must never trade with a forgotten replay
set.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import JsonStore

FILENAME = "challenges.json"


def _parse_expiry(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class PersistentChallengeStore:
    """Load/save for the verifier's issued + seen challenge maps."""

    def __init__(self, store: JsonStore, *, now_fn=None):
        self._store = store
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def load(self) -> tuple[dict[str, datetime], dict[str, datetime]]:
        """Return (issued, seen) with expired entries pruned.

        Missing file means a first start: empty maps. Corrupt or
        wrong-shaped content raises RuntimeError — the caller refuses
        to start rather than trade with a forgotten replay set."""
        try:
            state = self._store.load_json(FILENAME, None)
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                f"refusing to start: unreadable challenge store: {exc}"
            ) from exc
        if state is None:
            return {}, {}
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("issued"), dict)
            or not isinstance(state.get("seen"), dict)
        ):
            raise RuntimeError(
                "refusing to start: corrupt challenge store "
                "(not an object with issued/seen maps)"
            )
        try:
            issued = {str(k): _parse_expiry(v) for k, v in state["issued"].items()}
            seen = {str(k): _parse_expiry(v) for k, v in state["seen"].items()}
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"refusing to start: corrupt challenge store: {exc}"
            ) from exc
        now = self._now_fn()
        return (
            {n: e for n, e in issued.items() if e > now},
            {n: e for n, e in seen.items() if e > now},
        )

    def save(
        self, issued: dict[str, datetime], seen: dict[str, datetime]
    ) -> None:
        """Atomically persist the maps, pruning expired entries first."""
        now = self._now_fn()
        state = {
            "issued": {
                n: e.isoformat() for n, e in issued.items() if e > now
            },
            "seen": {n: e.isoformat() for n, e in seen.items() if e > now},
        }
        self._store.save_json(FILENAME, state)
