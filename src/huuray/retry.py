"""Retry policy.

The v4 API exposes no idempotency key. ``RefID`` is a reference you choose for
your own reconciliation; it is not a server-side deduplication key. So a retried
``POST /v4/Order`` can create a second order, and a retried ``POST /v4/Resend``
can re-deliver a live gift card.

Because of that, retries are **opt-in per operation**, never inferred from the
HTTP method. Each resource method declares whether it is safe to repeat:

===========  ================================================================
retryable    Balance, ExchangeRates, Catalogue, Template, Stock, Search, Pdf
never        Order, Resend, Cancel, Upload
===========  ================================================================

Note that five of the retryable operations are POSTs. They are POSTs because
they take a request body, not because they change anything.

A ``429`` names its own wait in ``Retry-After``. A retryable operation waits
that long, at least 1 second, in place of the backoff. When that is longer than
``max_delay`` it does not wait: the error is raised at once, for the caller to
decide.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

#: HTTP statuses worth repeating a *read* for.
#:
#: ``429`` is a documented response on every v4 operation: the rate limit for
#: your IP address was exceeded and the request was not processed.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: The shortest wait between two requests, in seconds: a ``Retry-After: 0``
#: from a server or proxy never turns into back-to-back requests.
MIN_WAIT = 1

#: ``Retry-After`` in whole seconds. Anything else, the HTTP-date form included,
#: reads as absent.
_DELTA_SECONDS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class RetryOptions:
    """Retry knobs. Defaults are deliberately conservative.

    :param max_retries: Attempts after the first. ``0`` disables retrying.
    :param base_delay: Base delay in seconds; doubles per attempt, with jitter.
    :param max_delay: Ceiling for a single backoff wait, in seconds. Also the
        longest ``Retry-After`` waited on after a 429: a longer one raises
        :class:`~huuray.HuurayRateLimitError` at once.
    """

    max_retries: int = 2
    base_delay: float = 0.25
    max_delay: float = 4.0

    def __post_init__(self) -> None:
        # Clamped rather than rejected: a negative max_retries is a config typo,
        # and refusing to send at all would be a stranger failure than not
        # retrying. Frozen dataclass, so normalise through object.__setattr__.
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        object.__setattr__(self, "base_delay", max(0.0, float(self.base_delay)))
        object.__setattr__(self, "max_delay", max(0.0, float(self.max_delay)))


#: The policy applied when the client is built without a ``retry`` argument.
DEFAULT_RETRY = RetryOptions()


def is_retryable_status(status: int) -> bool:
    """Whether a response status should be retried.

    Only consulted for operations already known to be safe to repeat.
    """
    return status in _RETRYABLE_STATUS


def backoff_delay(attempt: int, options: RetryOptions) -> float:
    """Exponential backoff with full jitter, so parallel clients do not resonate."""
    # The float() is load-bearing, not decoration: ``int ** int`` is typed as
    # Any (a negative exponent would produce a float), and an Any leaking into
    # this arithmetic would disable type checking of the whole delay expression.
    exponential = min(options.base_delay * float(2**attempt), options.max_delay)
    # random, not secrets: this is jitter to desynchronise clients, not a secret.
    return random.random() * exponential


def retry_after_seconds(value: Optional[str]) -> Optional[int]:
    """``Retry-After`` as whole seconds, or ``None`` when absent or anything else."""
    text = "" if value is None else value.strip()
    if not _DELTA_SECONDS.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:  # more digits than int() converts
        return None
