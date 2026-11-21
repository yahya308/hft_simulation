from fractions import Fraction
import unittest

from dashboard.portfolio_risk import MarkedPosition, parse_shock_percent, price_shock


class PortfolioRiskTest(unittest.TestCase):
    def test_gradient_and_uniform_revaluation_of_long_and_short(self):
        positions = [
            MarkedPosition("ASELS.E", -15, Fraction("410.125"), Fraction("5340.75")),
            MarkedPosition("THYAO.E", 100, Fraction("288.125"), Fraction("-29441.75")),
        ]

        def direct_revaluation(shocks):
            return sum(item.cash + item.position * item.mark_price * (1 + shift / 100)
                       for item, shift in zip(positions, shocks))

        shock = Fraction(-1)
        result = price_shock(positions, shock)
        initial = direct_revaluation((Fraction(0), Fraction(0)))
        shocked = direct_revaluation((shock, shock))
        self.assertEqual(result["base_pnl"], float(initial))
        self.assertEqual(result["stressed_pnl"], float(shocked))
        self.assertEqual(result["pnl_change"], float(shocked - initial))
        self.assertEqual([row["sensitivity_per_lira"] for row in result["rows"]], [-15, 100])

        step = Fraction("0.001")
        for index, row in enumerate(result["rows"]):
            up = [Fraction(0), Fraction(0)]
            up[index] = step
            down = [-change for change in up]
            derivative = (direct_revaluation(up) - direct_revaluation(down)) / (2 * step)
            self.assertEqual(row["sensitivity_per_percent"], float(derivative))

    def test_offsetting_exposures_and_realized_pnl_on_flat_instrument(self):
        positions = [
            MarkedPosition("A.E", 10, Fraction(100), Fraction(-1020)),
            MarkedPosition("B.E", -5, Fraction(200), Fraction(990)),
            MarkedPosition("C.E", 0, None, Fraction(5)),
        ]
        result = price_shock(positions, Fraction(-25))
        self.assertEqual([row["pnl_change"] for row in result["rows"]], [-250.0, 250.0, 0.0])
        self.assertEqual((result["base_pnl"], result["stressed_pnl"], result["sensitivity_per_percent"]),
                         (-25.0, -25.0, 0.0))
        self.assertEqual(result["missing_marks"], [])

    def test_open_unmarked_position_makes_portfolio_totals_unavailable(self):
        positions = [
            MarkedPosition("A.E", 12, None, Fraction(-40)),
            MarkedPosition("B.E", 2, Fraction(10), Fraction(-18)),
        ]
        result = price_shock(positions, Fraction(1))
        for key in ("base_pnl", "pnl_change", "stressed_pnl", "sensitivity_per_percent"):
            self.assertIsNone(result[key])
        self.assertEqual(result["missing_marks"], ["A.E"])
        self.assertEqual(result["rows"][0]["sensitivity_per_lira"], 12)
        self.assertIsNone(result["rows"][0]["sensitivity_per_percent"])
        self.assertEqual(result["rows"][1]["pnl_change"], 0.2)

    def test_shock_input_keeps_decimal_precision_and_rejects_invalid_values(self):
        self.assertEqual(parse_shock_percent(-1.25), Fraction(-5, 4))
        self.assertEqual(parse_shock_percent(-100), Fraction(-100))
        self.assertEqual(parse_shock_percent(100), Fraction(100))
        for value in (True, "-1", None, -101, 101, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_shock_percent(value)


if __name__ == "__main__":
    unittest.main()
