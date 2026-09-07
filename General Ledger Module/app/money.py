"""Monetary amounts: exact ``Decimal`` values with at most two decimal places.

``as_money`` is the single canonicalisation used at every boundary that accepts
an amount (Pydantic schemas, service dataclasses, ORM line attributes): it
accepts ints, strings and Decimals, rejects anything non-finite, negative or
with sub-cent precision, and returns the value quantised to two places.
Quantising never discards information because sub-cent inputs are rejected
first - ``5``, ``"5.0"`` and ``Decimal("5.00")`` are the same amount and
produce the same canonical value, while ``5.001`` is an error, not ``5.00``.
"""

from decimal import Decimal, InvalidOperation

CENT = Decimal("0.01")


class MoneyError(ValueError):
    pass


def as_money(value, *, field: str = "amount", allow_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MoneyError(f"{field}: amount is required")
    if isinstance(value, float):
        raise MoneyError(f"{field}: floats are not accepted for money; use a string or Decimal")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"{field}: {value!r} is not a valid amount") from exc
    if not amount.is_finite():
        raise MoneyError(f"{field}: {value!r} is not a finite amount")
    if amount < 0 and not allow_negative:
        raise MoneyError(f"{field}: amount must not be negative")
    if amount != amount.quantize(CENT):
        raise MoneyError(f"{field}: {value!r} has more than two decimal places")
    return amount.quantize(CENT)
