"""Revalue a fixed simulated portfolio under a common percentage price shock."""

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True, slots=True)
class MarkedPosition:
    symbol: str
    position: int
    mark_price: Fraction | None
    cash: Fraction


def parse_shock_percent(value: object) -> Fraction:
    if type(value) not in (int, float) or not -100 <= value <= 100:
        raise ValueError("Fiyat şoku −100 ile 100 arasında bir yüzde olmalı.")
    return Fraction(str(value))


def price_shock(positions: Iterable[MarkedPosition], percent: Fraction) -> dict:
    base_pnl = Fraction(0)
    sensitivity = Fraction(0)
    rows = []
    missing_marks = []

    for item in positions:
        mark = item.mark_price
        if mark is None and item.position:
            missing_marks.append(item.symbol)
            per_percent = None
        else:
            exposure = item.position * mark if mark is not None else Fraction(0)
            base_pnl += item.cash + exposure
            # Chain rule: ∂P&L/∂mark = position and d(mark)/d(percent) = mark / 100.
            per_percent = exposure / 100
            sensitivity += per_percent
        rows.append({
            "symbol": item.symbol,
            "position": item.position,
            "mark_price": float(mark) if mark is not None else None,
            "sensitivity_per_lira": item.position,
            "sensitivity_per_percent": float(per_percent) if per_percent is not None else None,
            "pnl_change": float(per_percent * percent) if per_percent is not None else None,
        })

    change = sensitivity * percent
    return {
        "shock_percent": float(percent),
        "base_pnl": None if missing_marks else float(base_pnl),
        "sensitivity_per_percent": None if missing_marks else float(sensitivity),
        "pnl_change": None if missing_marks else float(change),
        "stressed_pnl": None if missing_marks else float(base_pnl + change),
        "rows": rows,
        "missing_marks": missing_marks,
    }
