"""Slows down password guessing: too many failed logins block that login for a while.

Two limits: per username and IP (`login_throttle`), and per username over all IPs
(`account_throttle`, higher).

The failures are kept in memory, so they are per process and reset on restart. That is
enough for a single small app instance.
"""

import time
from collections.abc import Callable

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
# With more keys than this, expired ones are dropped on the next failure, so made-up
# usernames can't grow the memory without bound.
PRUNE_ABOVE_KEYS = 1000


class LoginThrottle:
    """Counts failed logins per key (username and IP) in a sliding time window.

    `clock` is injectable so tests don't have to wait.
    """

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, list[float]] = {}

    def is_blocked(self, key: str) -> bool:
        """True after `max_failures` failed attempts within the last `window_seconds`."""
        return len(self._recent_failures(key)) >= self._max_failures

    def try_attempt(self, key: str) -> bool:
        """False if blocked; otherwise counts the attempt as failed until `reset`.

        Counting it before the password check (not after) matters: the check awaits, and
        meanwhile many more guesses could get past `is_blocked`.
        """
        if self.is_blocked(key):
            return False
        self.record_failure(key)
        return True

    def record_failure(self, key: str) -> None:
        """Remember a failed attempt now."""
        if len(self._failures) > PRUNE_ABOVE_KEYS:
            for old in list(self._failures):
                self._recent_failures(old)
        self._failures.setdefault(key, []).append(self._clock())

    def reset(self, key: str) -> None:
        """Forget all failures, after a successful login."""
        self._failures.pop(key, None)

    def _recent_failures(self, key: str) -> list[float]:
        """Failures inside the window; older ones are dropped so memory doesn't grow."""
        cutoff = self._clock() - self._window_seconds
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent


# Per username and IP.
login_throttle = LoginThrottle()
# Per username from all IPs together, against guessing from many addresses. Higher, so an
# attacker can't lock someone out as easily.
ACCOUNT_MAX_FAILURES = 20
account_throttle = LoginThrottle(max_failures=ACCOUNT_MAX_FAILURES)
