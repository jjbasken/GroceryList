"""Ingredient parsing, scaling and combining for recipes and meal plans.

Ported from MenuPlanner's packages/shared/src/ingredients.ts so shopping lists
combine the same way. Quantities in the same unit family (volume, mass) are
converted to a common base and summed, so "1 tbsp olive oil" + "1/4 cup olive
oil" becomes one line. Units that can't be converted (cans, cloves, bunches...)
are summed per unit and listed side by side: "8 oz + 2 cans".

Pure functions only -- no database or Flask access -- so they are easy to test.
"""
import math
import re

UNITS = {
    "tsp": ("volume", 4.92892, "imperial"),
    "tbsp": ("volume", 14.7868, "imperial"),
    "fl oz": ("volume", 29.5735, "imperial"),
    "cup": ("volume", 236.588, "imperial"),
    "pint": ("volume", 473.176, "imperial"),
    "quart": ("volume", 946.353, "imperial"),
    "gallon": ("volume", 3785.41, "imperial"),
    "ml": ("volume", 1, "metric"),
    "l": ("volume", 1000, "metric"),
    "oz": ("mass", 28.3495, "imperial"),
    "lb": ("mass", 453.592, "imperial"),
    "g": ("mass", 1, "metric"),
    "kg": ("mass", 1000, "metric"),
}

UNIT_ALIASES = {
    "t": "tsp", "tsp": "tsp", "tsps": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tbsp": "tbsp", "tbsps": "tbsp", "tbs": "tbsp", "tbl": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "fl oz": "fl oz", "fl. oz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
    "c": "cup", "cup": "cup", "cups": "cup",
    "pt": "pint", "pint": "pint", "pints": "pint",
    "qt": "quart", "quart": "quart", "quarts": "quart",
    "gal": "gallon", "gallon": "gallon", "gallons": "gallon",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "": "", "each": "", "whole": "", "ea": "",
}

# Words that look plural but aren't (or have no useful singular).
UNCOUNTABLE = {"molasses", "asparagus", "hummus", "couscous", "swiss", "grits", "series", "species", "cheese"}

# Countable "units" that can't be converted but are worth recognizing when
# parsing free text, so "2 cans tomatoes" sums with "1 can tomatoes".
COUNT_UNITS = {
    "can", "clove", "bunch", "package", "jar", "slice", "head", "sprig", "stalk", "bottle",
    "piece", "pinch", "stick", "block", "loaf", "bag", "box", "handful",
}

PLURALIZE_UNITS = {
    "cup", "pint", "quart", "gallon", "can", "clove", "bunch", "package", "bag", "jar", "box",
    "slice", "head", "sprig", "stalk", "bottle", "piece", "pinch", "handful", "stick", "block", "loaf",
}

METRIC_UNITS = {"g", "kg", "ml", "l"}

FRACTIONS = [(0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.5, "½"), (2 / 3, "⅔"), (0.75, "¾")]

UNICODE_FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}

MAX_NAME = 200
MAX_UNIT = 40
MAX_NOTE = 500


def _round_half_up(x):
    # Python's round() is banker's rounding; JS Math.round rounds .5 up.
    return math.floor(x + 0.5)


def normalize_unit(unit):
    """Canonical form of a unit: known units map to their abbreviation, others are singularized."""
    u = re.sub(r"\s+", " ", re.sub(r"\.$", "", unit.strip().lower()))
    # Capital T conventionally means tablespoon; check before lowercasing loses it.
    if unit.strip() == "T":
        return "tbsp"
    if u in UNIT_ALIASES:
        return UNIT_ALIASES[u]
    return singularize(u)


def singularize(word):
    """Simple English singularization for the last word -- good enough for groceries."""
    parts = word.split(" ")
    last = parts.pop()
    s = last
    if last in UNCOUNTABLE:
        s = last
    elif re.search(r"[^aeiou]ies$", last):
        s = last[:-3] + "y"
    elif re.search(r"(tomato|potato|mango)es$", last):
        s = last[:-2]
    elif re.search(r"(ch|sh|x|ss|z)es$", last):
        s = last[:-2]
    elif re.search(r"[^s]s$", last) and not re.search(r"(us|is|ss)$", last):
        s = last[:-1]
    return " ".join(parts + [s])


def ingredient_key(name):
    """Key used to decide whether two ingredient names are the same item."""
    s = re.sub(r"[.,;:!]+$", "", name.strip().lower())
    return singularize(re.sub(r"\s+", " ", s))


def aggregate_ingredients(lines):
    """Combine ingredient lines ({name, qty, unit, source?}) into one row per item.

    Returns dicts with key, name (first spelling seen), quantities [(qty, unit)],
    has_unquantified, quantity_text and sources (where each line came from).
    """
    by_key = {}
    for line in lines:
        key = ingredient_key(line.get("name") or "")
        if not key:
            continue
        acc = by_key.get(key)
        if acc is None:
            acc = {"name": line["name"].strip(), "family": {}, "other": {}, "has_unquantified": False, "sources": []}
            by_key[key] = acc
        source = line.get("source")
        if source and source not in acc["sources"]:
            acc["sources"].append(source)

        qty = line.get("qty")
        if qty is None or not math.isfinite(qty) or qty <= 0:
            acc["has_unquantified"] = True
            continue
        unit = normalize_unit(line.get("unit") or "")
        definition = UNITS.get(unit)
        if definition:
            family, to_base, system = definition
            f = acc["family"].setdefault(family, {"base": 0.0, "imperial": False})
            f["base"] += qty * to_base
            f["imperial"] = f["imperial"] or system == "imperial"
        else:
            acc["other"][unit] = acc["other"].get(unit, 0) + qty

    rows = []
    for key, acc in by_key.items():
        quantities = []
        for family in ("mass", "volume"):
            f = acc["family"].get(family)
            if f:
                quantities.append(_display_quantity(family, f["base"], f["imperial"]))
        for unit, qty in acc["other"].items():
            quantities.append((qty, unit))
        rows.append({
            "key": key,
            "name": acc["name"],
            "quantities": quantities,
            "has_unquantified": acc["has_unquantified"],
            "quantity_text": " + ".join(format_quantity(q, u) for q, u in quantities),
            "sources": acc["sources"],
        })
    return rows


def _display_quantity(family, base, imperial):
    """Picks a readable unit for a summed base amount. Imperial wins if any input was imperial."""
    if family == "mass":
        if imperial:
            lb = UNITS["lb"][1]
            return (base / lb, "lb") if base >= lb else (base / UNITS["oz"][1], "oz")
        return (base / 1000, "kg") if base >= 1000 else (base, "g")
    if imperial:
        cups = base / UNITS["cup"][1]
        # 0.375 cup reads better as 6 tbsp; only use cups for amounts a measuring cup shows.
        if cups >= 1 or (cups >= 0.25 - 0.001 and _is_nice_fraction(cups)):
            return (cups, "cup")
        if base >= UNITS["tbsp"][1] - 0.01:
            return (base / UNITS["tbsp"][1], "tbsp")
        return (base / UNITS["tsp"][1], "tsp")
    return (base / 1000, "l") if base >= 1000 else (base, "ml")


def _is_nice_fraction(qty):
    frac = qty - math.floor(qty)
    return frac < 0.04 or frac > 0.96 or any(abs(frac - v) < 0.04 for v, _ in FRACTIONS)


def _plain_number(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def format_qty(qty):
    """"1½", "⅓", "2.5" -- kitchen-friendly numbers."""
    whole = math.floor(qty + 1e-9)
    frac = qty - whole
    if frac < 0.04:
        return str(whole)
    if frac > 0.96:
        return str(whole + 1)
    for value, glyph in FRACTIONS:
        if abs(frac - value) < 0.04:
            return f"{whole}{glyph}" if whole else glyph
    return _plain_number(_round_half_up(qty * 10) / 10)


def format_quantity(qty, unit):
    # Metric amounts read as decimals: 450 g, 1.1 kg -- never "1⅛ kg".
    if unit in METRIC_UNITS:
        n = str(_round_half_up(qty)) if unit in ("g", "ml") else _plain_number(_round_half_up(qty * 10) / 10)
    else:
        n = format_qty(qty)
    if not unit:
        return n
    u = unit
    if qty > 1 + 1e-9 and unit in PLURALIZE_UNITS:
        u = f"{unit}es" if re.search(r"(ch|sh|x|s)$", unit) else f"{unit}s"
    return f"{n} {u}"


def scale_qty(qty, recipe_servings, servings):
    """Scales a recipe's quantity for a different number of servings."""
    if qty is None or not servings or not recipe_servings:
        return qty
    return qty * servings / recipe_servings


def _digits(s):
    return s.isascii() and s.isdigit()


def _decimal(s):
    """"2" or "1.5" -> float; anything else -> None."""
    whole, dot, frac = s.partition(".")
    if _digits(whole) and (not dot or _digits(frac)):
        return float(s)
    return None


def parse_qty(text):
    """Parses what people type into a quantity box: "2", "1.5", "1/2", "1 1/2",
    "1½", "½". Returns None for blank input and NaN for anything unparseable.
    """
    s = (text or "").strip()
    if not s:
        return None
    total = 0.0
    if s[-1] in UNICODE_FRACTIONS:
        total += UNICODE_FRACTIONS[s[-1]]
        s = s[:-1].strip()
        if not s:
            return total
    # Parsed with string operations rather than a regex: the grammar is tiny,
    # and a pattern with optional whole/fraction parts backtracks
    # quadratically on long runs of digits.
    if "/" not in s:
        value = _decimal(s)
        return math.nan if value is None else total + value
    head, _, den_text = s.partition("/")
    head, den_text = head.strip(), den_text.strip()
    if not _digits(den_text):
        return math.nan
    parts = head.split()
    if len(parts) == 2:
        whole = _decimal(parts[0])
        num_text = parts[1]
    elif len(parts) == 1:
        whole = 0.0
        num_text = parts[0]
        # "11/2" is ambiguous (1 1/2 or 11/2?), as is any numerator of two or
        # more digits written without a space before it.
        if len(num_text) > 1:
            return math.nan
    else:
        return math.nan
    if whole is None or not _digits(num_text):
        return math.nan
    num, den = int(num_text), int(den_text)
    if den == 0:
        return math.nan
    return total + whole + num / den


# A leading amount: "1 1/2", "1/2", "1.5", "1½", "1 ½", "½".
_FRAC_CHARS = "".join(UNICODE_FRACTIONS)
_NUMBER = rf"(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?(?:\s*[{_FRAC_CHARS}])?|[{_FRAC_CHARS}])"
_LEADING_QTY = re.compile(rf"^({_NUMBER})(?:\s*(?:-|–|to)\s*{_NUMBER})?\s*")
_MULTI_WORD_UNITS = ("fluid ounces", "fluid ounce", "fl. oz.", "fl. oz", "fl oz.", "fl oz")


def _known_unit(word):
    """Canonical unit for a word if it's a measurement or countable unit, else None."""
    if not word:
        return None
    unit = normalize_unit(word)
    if unit in UNITS or unit in COUNT_UNITS:
        return unit
    return None


def parse_ingredient_line(text):
    """Split a free-text line ("1 1/2 cups flour, sifted") into name/qty/unit/note.

    Ranges ("2-3 cloves") use the first number. A parenthetical right after the
    amount ("2 (14 oz) cans tomatoes") becomes part of the note. Never raises:
    anything it can't make sense of comes back as a name with no quantity.
    """
    raw = (text or "").strip()
    fallback = {"name": raw[:MAX_NAME], "qty": None, "unit": "", "note": ""}
    if not raw:
        return fallback
    try:
        qty = None
        rest = raw
        notes = []
        m = _LEADING_QTY.match(rest)
        if m:
            parsed = parse_qty(m.group(1))
            if parsed is not None and math.isfinite(parsed) and parsed > 0:
                qty = parsed
                rest = rest[m.end():]

        unit = ""
        if qty is not None:
            paren = re.match(r"^\(([^)]*)\)\s*", rest)
            if paren:
                if paren.group(1).strip():
                    notes.append(paren.group(1).strip())
                rest = rest[paren.end():]
            lowered = rest.lower()
            for phrase in _MULTI_WORD_UNITS:
                if lowered.startswith(phrase + " ") or lowered == phrase:
                    unit = "fl oz"
                    rest = rest[len(phrase):].lstrip()
                    break
            else:
                word_match = re.match(r"^([A-Za-z]+\.?)(?=\s|,|$)\s*", rest)
                if word_match:
                    # "T" must reach normalize_unit uppercase to mean tablespoon.
                    candidate = _known_unit(word_match.group(1))
                    if candidate is not None:
                        unit = candidate
                        rest = rest[word_match.end():]
            rest = re.sub(r"^of\s+", "", rest, flags=re.I)

        name, _, comma_note = rest.partition(",")
        name = name.strip()
        if comma_note.strip():
            notes.append(comma_note.strip())
        if not name:
            return fallback
        return {
            "name": name[:MAX_NAME],
            "qty": qty,
            "unit": unit[:MAX_UNIT],
            "note": ", ".join(notes)[:MAX_NOTE],
        }
    except Exception:  # noqa: BLE001 -- parsing must never break a save
        return fallback


def parse_quantity_text(text):
    """Parse a grocery item's free-text quantity ("2 lb", "8 oz + 2 cans", "3").

    Returns [(qty, unit)], or None when blank or any part isn't a plain amount
    with an optional unit.
    """
    if not text or not text.strip():
        return None
    out = []
    for part in text.split("+"):
        part = part.strip()
        m = re.match(rf"^({_NUMBER})\s*([^\d]*)$", part)
        if not m:
            return None
        qty = parse_qty(m.group(1))
        if qty is None or not math.isfinite(qty) or qty <= 0:
            return None
        unit_text = m.group(2).strip()
        if len(unit_text.split()) > 2 or re.search(r"[/,()]", unit_text):
            return None
        out.append((qty, normalize_unit(unit_text)))
    return out


def combine_quantity_text(existing, quantities):
    """Quantity text for an existing item after adding `quantities` to it.

    Returns None when the existing text can't be parsed, so the caller can fall
    back to noting the extra amount instead of overwriting what someone typed.
    """
    new_text = " + ".join(format_quantity(q, u) for q, u in quantities)
    if not existing or not existing.strip():
        return new_text
    if not quantities:
        return existing.strip()
    parsed = parse_quantity_text(existing)
    if parsed is None:
        return None
    lines = [{"name": "item", "qty": q, "unit": u} for q, u in parsed + list(quantities)]
    return aggregate_ingredients(lines)[0]["quantity_text"]
