"""Shared fail-closed validation; no broker credentials or network access."""
from decimal import Decimal, InvalidOperation

import pandas as pd


class SafetyError(ValueError):
    """A stable, non-sensitive error code safe to show in the UI."""


def decimal(value, *, zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0):
            raise ValueError
        if len(str(value)) > 64 or abs(result.adjusted()) > 18:
            raise ValueError
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise SafetyError("INVALID_NUMBER") from None


def utc(value=None):
    result = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise SafetyError("TIMEZONE_REQUIRED")
    return result.tz_convert("UTC")


def fresh(observed, now, seconds):
    age = (utc(now) - utc(observed)).total_seconds()
    if not 0 <= age <= seconds:
        raise SafetyError("STALE_OR_FUTURE_DATA")
