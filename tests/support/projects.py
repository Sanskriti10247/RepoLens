"""Deterministic fixture project used by impact, selection and execution tests.

Relationships it exercises:
- inventory.pricing.with_tax  ← quote ← reports.summary (direct and transitive calls)
- inventory.models.Item.total ← Perishable.total via super() (inheritance + override)
- pricing.quote calls ``item.total(qty)`` on a parameter: statically UNRESOLVED (a hidden caller)
- inventory.plugins.load uses importlib/getattr: DYNAMIC
- tests/test_util.py is unrelated to everything else
"""

from __future__ import annotations

INVENTORY: dict[str, str] = {
    "inventory/__init__.py": "from .models import Item, Perishable\n",
    "inventory/models.py": (
        "class Item:\n"
        "    def __init__(self, sku, price):\n"
        "        self.sku = sku\n"
        "        self.price = price\n"
        "\n"
        "    def total(self, qty):\n"
        "        return self.price * qty\n"
        "\n"
        "\n"
        "class Perishable(Item):\n"
        "    def total(self, qty):\n"
        "        return super().total(qty) * 0.9\n"
    ),
    "inventory/pricing.py": (
        "TAX = 0.2\n"
        "\n"
        "\n"
        "def with_tax(amount):\n"
        "    return round(amount * (1 + TAX), 2)\n"
        "\n"
        "\n"
        "def quote(item, qty):\n"
        "    return with_tax(item.total(qty))\n"
    ),
    "inventory/reports.py": (
        "from .pricing import quote\n"
        "\n"
        "\n"
        "def summary(items):\n"
        "    return [quote(item, 1) for item in items]\n"
    ),
    "inventory/plugins.py": (
        "import importlib\n"
        "\n"
        "\n"
        "def load(name):\n"
        '    return getattr(importlib.import_module(name), "run")()\n'
    ),
    "inventory/util.py": (
        'def slugify(text):\n    return text.strip().lower().replace(" ", "-")\n'
    ),
    "tests/test_models.py": (
        "from inventory.models import Item, Perishable\n"
        "\n"
        "\n"
        "def test_item_total():\n"
        '    assert Item("a", 2.0).total(3) == 6.0\n'
        "\n"
        "\n"
        "def test_perishable_total():\n"
        '    assert Perishable("b", 10.0).total(1) == 9.0\n'
    ),
    "tests/test_pricing.py": (
        "from inventory.models import Item\n"
        "from inventory.pricing import quote, with_tax\n"
        "\n"
        "\n"
        "def test_with_tax():\n"
        "    assert with_tax(10) == 12.0\n"
        "\n"
        "\n"
        "def test_quote():\n"
        '    assert quote(Item("a", 5.0), 2) == 12.0\n'
    ),
    "tests/test_reports.py": (
        "from inventory.models import Item\n"
        "from inventory.reports import summary\n"
        "\n"
        "\n"
        "def test_summary():\n"
        '    assert summary([Item("a", 1.0)]) == [1.2]\n'
    ),
    "tests/test_util.py": (
        "from inventory.util import slugify\n"
        "\n"
        "\n"
        "def test_slugify():\n"
        '    assert slugify(" Hello World ") == "hello-world"\n'
    ),
    "pyproject.toml": '[project]\nname = "inventory"\nversion = "0.1.0"\n',
    "README.md": "# inventory\n",
}

# Controlled changes, applied on top of INVENTORY.
CHANGE_WITH_TAX_BODY = {
    "inventory/pricing.py": INVENTORY["inventory/pricing.py"].replace(
        "round(amount * (1 + TAX), 2)", "round(amount + amount * TAX, 2)"
    )
}
REGRESSION_WITH_TAX = {
    "inventory/pricing.py": INVENTORY["inventory/pricing.py"].replace(
        "round(amount * (1 + TAX), 2)", "round(amount * TAX, 2)"
    )
}
CHANGE_ITEM_TOTAL = {
    "inventory/models.py": INVENTORY["inventory/models.py"].replace(
        "        return self.price * qty\n", "        return float(self.price) * qty\n"
    )
}
CHANGE_UTIL_ONLY = {
    "inventory/util.py": INVENTORY["inventory/util.py"].replace(".strip()", '.strip(" ")')
}
CHANGE_CONFIG_ONLY = {"pyproject.toml": INVENTORY["pyproject.toml"] + 'description = "x"\n'}
CHANGE_DOCS_ONLY = {"README.md": "# inventory\n\nMore words.\n"}
CHANGE_TAX_CONSTANT = {
    "inventory/pricing.py": INVENTORY["inventory/pricing.py"].replace("TAX = 0.2", "TAX = 0.21")
}
