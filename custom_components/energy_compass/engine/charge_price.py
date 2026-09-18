"""Purchase-price eligibility for battery charging from the grid."""

from .models import Problem, Slot


def price_allows_grid_charge(problem: Problem, slot: Slot) -> bool:
    """Compare final tariff prices, allowing only floating-point rounding noise."""
    ceiling = problem.maximum_grid_charge_price
    return ceiling is None or slot.buy_per_kwh <= ceiling + 1e-9
