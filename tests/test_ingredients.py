"""Tests for ingredients.py.

The first sections mirror MenuPlanner's packages/shared/tests/ingredients.test.ts
case for case, so both implementations combine shopping lists identically.
"""
import math

import pytest

from ingredients import (
    aggregate_ingredients,
    combine_quantity_text,
    format_qty,
    format_quantity,
    ingredient_key,
    normalize_unit,
    parse_ingredient_line,
    parse_qty,
    parse_quantity_text,
    scale_qty,
    singularize,
)


def by_key(rows, key):
    return next(r for r in rows if r["key"] == key)


def qty_text(lines):
    return aggregate_ingredients(lines)[0]["quantity_text"]


def line(name, qty, unit="", source=None):
    d = {"name": name, "qty": qty, "unit": unit}
    if source:
        d["source"] = source
    return d


# ---------------------------------------------------------------------------
# Parity with MenuPlanner
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_singularizes_common_grocery_plurals(self):
        assert singularize("tomatoes") == "tomato"
        assert singularize("berries") == "berry"
        assert singularize("tortillas") == "tortilla"
        assert singularize("green onions") == "green onion"
        assert singularize("peaches") == "peach"
        assert singularize("hummus") == "hummus"
        assert singularize("molasses") == "molasses"
        assert singularize("asparagus") == "asparagus"

    def test_ingredient_keys_ignore_case_spacing_plurals_and_punctuation(self):
        assert ingredient_key("  Yellow  Onions, ") == ingredient_key("yellow onion")

    def test_unit_aliases(self):
        assert normalize_unit("Tablespoons") == "tbsp"
        assert normalize_unit("T") == "tbsp"
        assert normalize_unit("t") == "tsp"
        assert normalize_unit("lbs.") == "lb"
        assert normalize_unit("cans") == "can"
        assert normalize_unit("cloves") == "clove"
        assert normalize_unit("") == ""
        assert normalize_unit("each") == ""


class TestAggregateIngredients:
    def test_sums_same_item_across_meals_and_remembers_sources(self):
        rows = aggregate_ingredients([
            line("Chicken breast", 1, "lb", "Fajitas"),
            line("chicken breasts", 16, "oz", "Lemon chicken"),
        ])
        assert len(rows) == 1
        assert rows[0]["name"] == "Chicken breast"
        assert rows[0]["quantity_text"] == "2 lb"
        assert rows[0]["sources"] == ["Fajitas", "Lemon chicken"]

    def test_converts_volumes_and_picks_readable_unit(self):
        assert qty_text([line("olive oil", 2, "tbsp"), line("olive oil", 0.25, "cup")]) == "6 tbsp"

    def test_clean_cup_amounts_stay_in_cups(self):
        assert qty_text([line("milk", 1, "cup"), line("milk", 8, "tbsp")]) == "1½ cups"
        assert qty_text([line("flour", 0.5, "cup")]) == "½ cup"

    def test_small_volumes_stay_in_spoons(self):
        assert qty_text([line("cumin", 1, "tsp"), line("cumin", 2, "tsp")]) == "1 tbsp"
        assert qty_text([line("salt", 0.5, "tsp")]) == "½ tsp"

    def test_metric_only_inputs_stay_metric(self):
        assert qty_text([line("rice", 600, "g"), line("rice", 0.5, "kg")]) == "1.1 kg"
        assert qty_text([line("feta", 200, "g")]) == "200 g"

    def test_mixing_metric_into_imperial_reports_imperial(self):
        assert qty_text([line("ground turkey", 1, "lb"), line("ground turkey", 227, "g")]) == "1½ lb"

    def test_non_convertible_units_summed_per_unit(self):
        assert qty_text([
            line("black beans", 1, "can"),
            line("black beans", 1, "cans"),
            line("black beans", 8, "oz"),
        ]) == "8 oz + 2 cans"

    def test_counts_without_a_unit(self):
        assert qty_text([line("eggs", 6, ""), line("egg", 2, "each")]) == "8"

    def test_unquantified_lines_flagged_without_number(self):
        rows = aggregate_ingredients([line("salt", None), line("Limes", 2), line("lime", None)])
        salt = by_key(rows, "salt")
        assert salt["quantity_text"] == "" and salt["has_unquantified"] is True
        lime = by_key(rows, "lime")
        assert lime["quantity_text"] == "2" and lime["has_unquantified"] is True

    def test_mass_and_volume_of_same_item_both_kept(self):
        assert qty_text([line("cheddar", 8, "oz"), line("cheddar", 1, "cup")]) == "8 oz + 1 cup"

    def test_skips_blank_names(self):
        assert aggregate_ingredients([line("  ", 1)]) == []


class TestFormatting:
    def test_kitchen_fractions(self):
        assert format_qty(1.5) == "1½"
        assert format_qty(0.333) == "⅓"
        assert format_qty(2) == "2"
        assert format_qty(1.98) == "2"
        assert format_qty(2.4) == "2.4"

    def test_scales_by_servings(self):
        assert scale_qty(2, 4, 6) == 3
        assert scale_qty(2, 4, None) == 2
        assert scale_qty(None, 4, 6) is None

    def test_format_quantity_pluralizes_countable_units(self):
        assert format_quantity(2, "can") == "2 cans"
        assert format_quantity(2, "bunch") == "2 bunches"
        assert format_quantity(1, "can") == "1 can"
        assert format_quantity(2, "lb") == "2 lb"


class TestParseQty:
    def test_accepts_what_people_type(self):
        assert parse_qty("2") == 2
        assert parse_qty(" 1.5 ") == 1.5
        assert parse_qty("1/2") == 0.5
        assert parse_qty("1 1/2") == 1.5
        assert parse_qty("1½") == 1.5
        assert parse_qty("½") == 0.5
        assert parse_qty("") is None

    def test_rejects_nonsense(self):
        assert math.isnan(parse_qty("a few"))
        assert math.isnan(parse_qty("1/0"))
        assert math.isnan(parse_qty("11/2"))


# ---------------------------------------------------------------------------
# GroceryList additions
# ---------------------------------------------------------------------------


class TestParseIngredientLine:
    @pytest.mark.parametrize("text, expected", [
        ("1 1/2 cups flour, sifted", {"name": "flour", "qty": 1.5, "unit": "cup", "note": "sifted"}),
        ("½ cup milk", {"name": "milk", "qty": 0.5, "unit": "cup", "note": ""}),
        ("1½ cups milk", {"name": "milk", "qty": 1.5, "unit": "cup", "note": ""}),
        ("2-3 cloves garlic, minced", {"name": "garlic", "qty": 2, "unit": "clove", "note": "minced"}),
        ("2 (14 oz) cans tomatoes", {"name": "tomatoes", "qty": 2, "unit": "can", "note": "14 oz"}),
        ("1 lb. ground beef", {"name": "ground beef", "qty": 1, "unit": "lb", "note": ""}),
        ("2 T soy sauce", {"name": "soy sauce", "qty": 2, "unit": "tbsp", "note": ""}),
        ("2 cups of rice", {"name": "rice", "qty": 2, "unit": "cup", "note": ""}),
        ("8 fl oz cream", {"name": "cream", "qty": 8, "unit": "fl oz", "note": ""}),
        ("3 large eggs", {"name": "large eggs", "qty": 3, "unit": "", "note": ""}),
        ("2 onions", {"name": "onions", "qty": 2, "unit": "", "note": ""}),
        ("Salt to taste", {"name": "Salt to taste", "qty": None, "unit": "", "note": ""}),
        ("Pepper, freshly ground", {"name": "Pepper", "qty": None, "unit": "", "note": "freshly ground"}),
    ])
    def test_parses_common_lines(self, text, expected):
        assert parse_ingredient_line(text) == expected

    def test_blank(self):
        assert parse_ingredient_line("   ") == {"name": "", "qty": None, "unit": "", "note": ""}

    def test_garbage_falls_back_to_name(self):
        assert parse_ingredient_line("!!! ???") == {"name": "!!! ???", "qty": None, "unit": "", "note": ""}

    def test_amount_without_name_falls_back(self):
        assert parse_ingredient_line("2 cups") == {"name": "2 cups", "qty": None, "unit": "", "note": ""}

    def test_ambiguous_fraction_left_unparsed(self):
        result = parse_ingredient_line("11/2 cups sugar")
        assert result["qty"] is None
        assert result["name"] == "11/2 cups sugar"

    def test_long_names_truncated(self):
        assert len(parse_ingredient_line("x" * 500)["name"]) == 200

    def test_none_input(self):
        assert parse_ingredient_line(None)["name"] == ""


class TestParseQuantityText:
    def test_parses_amounts(self):
        assert parse_quantity_text("2 lb") == [(2, "lb")]
        assert parse_quantity_text("1½ cups") == [(1.5, "cup")]
        assert parse_quantity_text("3") == [(3, "")]
        assert parse_quantity_text("8 oz + 2 cans") == [(8, "oz"), (2, "can")]

    def test_blank_and_garbage(self):
        assert parse_quantity_text("") is None
        assert parse_quantity_text(None) is None
        assert parse_quantity_text("a few") is None
        assert parse_quantity_text("2 lb + some") is None
        assert parse_quantity_text("11/2 cups") is None
        assert parse_quantity_text("2 lb (for tacos, extra)") is None


class TestCombineQuantityText:
    def test_blank_existing_uses_new(self):
        assert combine_quantity_text("", [(2, "lb")]) == "2 lb"
        assert combine_quantity_text(None, [(2, "can")]) == "2 cans"

    def test_sums_compatible_amounts(self):
        assert combine_quantity_text("1 lb", [(2, "lb")]) == "3 lb"
        assert combine_quantity_text("1 lb", [(16, "oz")]) == "2 lb"
        assert combine_quantity_text("2", [(1, "")]) == "3"
        assert combine_quantity_text("1 can", [(8, "oz")]) == "8 oz + 1 can"

    def test_unparseable_existing_returns_none(self):
        assert combine_quantity_text("a couple", [(2, "lb")]) is None

    def test_nothing_new_keeps_existing(self):
        assert combine_quantity_text("2 lb", []) == "2 lb"
