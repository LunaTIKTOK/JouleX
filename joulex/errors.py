"""Shared error type for the exchange service.

Lives here (not in ``exchange.py``) so ``joulex/accounts.py`` can raise
it without a circular import: ``exchange.py`` imports the account
store, and the account store raises this error.
"""

from __future__ import annotations


class ExchangeError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason
