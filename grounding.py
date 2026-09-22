"""
Deterministic check that every figure in an answer came from a tool result.

Catches the failure that prompts could not: a model that multiplies two returned
numbers and reports the product as if it had been queried. Allowed figures are
the cells of the query results, simple sums, differences and shares of them,
and numbers the user wrote in the question. Products are deliberately not
allowed. This finds invented figures; it does not prove the arithmetic behind
an allowed one is meaningful.
"""
import itertools
import re

_FENCE = re.compile(r"```.*?```", re.S)
_CODE = re.compile(r"`[^`]*`")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?\b")
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s")
_NUM = re.compile(
    r"(?<![\w.])([$€£]?)([-−]?\d[\d,]*(?:\.\d+)?)\s?(%|k\b|K\b|m\b|M\b|million\b|thousand\b|bn\b|billion\b)?")
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
_MAX_POOL = 40  # cap on values combined pairwise


def _strip(text: str) -> str:
    text = _FENCE.sub(" ", text)
    text = _CODE.sub(" ", text)
    text = _ISO_DATE.sub(" ", text)
    return _LIST_MARKER.sub(" ", text)


def stated_figures(text: str) -> list[dict]:
    """Figures a reader would take as claims: value, decimals, percent flag, raw text."""
    out = []
    for m in _NUM.finditer(_strip(text)):
        cur, digits, suffix = m.group(1), m.group(2).rstrip(","), (m.group(3) or "")
        num = digits.replace(",", "").replace("−", "-")
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = float(num)
        plain_int = decimals == 0 and not cur and not suffix
        if plain_int and abs(value) <= 9:
            continue  # "3 customers", "1 of 5": too small to be a data claim
        if plain_int and "," not in digits and 1990 <= value <= 2100:
            continue  # a year
        scale = _MULT.get(suffix.lower(), 1)
        out.append({"raw": m.group(0).strip(), "value": value * scale, "decimals": decimals,
                    "pct": suffix == "%", "scale": scale})
    return out


def _date_ints(text: str) -> set[float]:
    ints = set()
    for m in _ISO_DATE.finditer(text):
        ints.update(float(int(g)) for g in m.groups() if g)
    return ints


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _allowed(question: str, trace: list[dict]) -> tuple[set[float], set[float]]:
    exact: set[float] = {f["value"] for f in stated_figures(question)}
    exact |= _date_ints(question)
    singles: list[float] = []  # figures from one-row results: the ones a model reports and compares
    others: list[float] = []
    column_totals: set[float] = set()

    for step in trace:
        exact |= _date_ints(str(step["args"]))
        exact.update(float(v) for v in step["args"].values() if _numeric(v))
        result = step["result"]
        if "error" in result or "rows" not in result:
            continue
        rows = result["rows"]
        exact.add(float(result.get("row_count", len(rows))))
        exact |= _date_ints(str(rows))
        cells = [float(v) for row in rows for v in row if _numeric(v)]
        exact.update(cells)
        # A fraction reported as a percent: not bounded to +/-1, since a period-over-period
        # growth metric can legitimately exceed 100% (net_revenue_growth_mom of 1.66 is "166%").
        exact.update(c * 100 for c in cells)
        (singles if len(rows) == 1 else others).extend(cells)
        for col in zip(*rows):
            nums = [float(v) for v in col if _numeric(v)]
            if len(nums) > 1:
                column_totals.add(sum(nums))

    # sums, differences and shares across everything returned (gross and net may come from
    # separate calls), but never products
    pool = list(dict.fromkeys(sorted(singles, key=abs, reverse=True) +
                              sorted(others, key=abs, reverse=True)))[:_MAX_POOL]
    derived: set[float] = set(column_totals)
    for a, b in itertools.combinations(pool, 2):
        derived.update((a + b, abs(a - b)))
        hi, lo = (a, b) if abs(a) >= abs(b) else (b, a)
        if hi:
            derived.add(lo / hi * 100)  # a share, as a percent
    return exact, derived


def _matches(fig: dict, candidates: set[float]) -> bool:
    tol = 0.5 * 10 ** -fig["decimals"] * fig["scale"] + 1e-9
    # sign is ignored: "down 2,147.43" and "-2,147.43" both refer to the same difference
    return any(abs(abs(c) - abs(fig["value"])) <= tol for c in candidates)


def ungrounded(text: str, question: str, trace: list[dict]) -> list[str]:
    """Figures in `text` that appear in no tool result and cannot be derived from them."""
    figures = stated_figures(text)
    if not figures:
        return []
    exact, derived = _allowed(question, trace)
    bad = []
    for fig in figures:
        if _matches(fig, exact) or _matches(fig, derived):
            continue
        if fig["raw"] not in bad:
            bad.append(fig["raw"])
    return bad


def matches(fig: dict, candidates) -> bool:
    """Public form of the figure comparison: is this stated figure one of the candidates,
    to the precision it was stated in?"""
    return _matches(fig, set(candidates))
