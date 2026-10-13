"""Stage 6: match every plan item to the metadata. A plan passes only when every item matches exactly one candidate."""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from src.query.catalog import Resolved
from src.query.shapes import CALCULATION_TYPES, DATE_GRAINS, METRIC_FUNCTIONS, SORT_DIRECTIONS

if TYPE_CHECKING:
    from src.query.business_semantics import BusinessSemantics

NUMERIC_AGGREGATES = ("SUM", "AVG", "MIN", "MAX")
DATE_PREFIXES = ("DATE", "TIMESTAMP")
MAX_CLOSEST = 3
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

# Returns the stored values that equal the text ignoring case. Used only for columns with no stored value list.
ValueLookup = Callable[[str, str, str], list[str]]


class MatchError(Exception):
    """The plan cannot be matched. `reasons` lists every problem found."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


class _ItemError(Exception):
    """One plan item failed. The message is recorded with the item's label."""


@dataclass(frozen=True)
class _Candidate:
    name: str
    synonyms: tuple[str, ...] = ()


def normalize(text: str) -> str:
    """Lowercase, drop spaces and underscores, then drop one trailing 's'."""
    cleaned = str(text).lower().replace(" ", "").replace("_", "")
    if len(cleaned) > 1 and cleaned.endswith("s"):
        return cleaned[:-1]
    return cleaned


def _closest(item: str, names: Sequence[str]) -> list[str]:
    by_key: dict[str, str] = {}
    for name in names:
        by_key.setdefault(normalize(name), name)
    keys = difflib.get_close_matches(normalize(item), list(by_key), n=MAX_CLOSEST, cutoff=0.5)
    return [by_key[key] for key in keys]


def _pick(item: str, kind: str, candidates: Sequence[_Candidate]) -> tuple[str, str]:
    """Return (name, rung). A rung that finds two candidates rejects the item instead of falling through."""
    needle = normalize(item)
    rungs: tuple[tuple[str, Callable[[_Candidate], bool]], ...] = (
        ("exact", lambda c: c.name == item),
        ("normalized", lambda c: normalize(c.name) == needle),
        ("synonym", lambda c: any(normalize(s) == needle for s in c.synonyms)),
    )
    for source, test in rungs:
        hits = [c.name for c in candidates if test(c)]
        if len(hits) == 1:
            return hits[0], source
        if len(hits) > 1:
            raise _ItemError(f"'{item}' matches more than one {kind}: {', '.join(hits)}")
    closest = _closest(item, [c.name for c in candidates])
    hint = f" Closest: {', '.join(closest)}." if closest else ""
    raise _ItemError(f"'{item}' does not match any {kind}.{hint}")


def _is_date(meta: dict[str, Any]) -> bool:
    return str(meta.get("data_type", "")).upper().startswith(DATE_PREFIXES)


def _is_numeric(meta: dict[str, Any]) -> bool:
    return meta.get("column_class") == "numeric"


def _value_candidates(meta: dict[str, Any]) -> list[_Candidate]:
    """One candidate per stored value. Boolean expressions are its synonyms, but only when the target is stored."""
    values = [str(value) for value in meta.get("values") or []]
    words: dict[str, list[str]] = {value: [] for value in values}
    for expressions_key, target_key in (("true_expressions", "true_value"), ("false_expressions", "false_value")):
        target = meta.get(target_key)
        if target is not None and str(target) in words:
            words[str(target)].extend(str(word) for word in meta.get(expressions_key) or [])
    return [_Candidate(value, tuple(synonyms)) for value, synonyms in words.items()]


def _canonical(raw: Any, allowed: Sequence[Any], label: str = "operator") -> str:
    if isinstance(raw, str):
        wanted = raw.strip().lower()
        for option in allowed:
            if str(option).lower() == wanted:
                return str(option)
    raise _ItemError(f"{label} {raw!r} is not allowed here. Allowed: {', '.join(map(str, allowed)) or 'none'}")


def _number(raw: Any, column: str) -> int | float:
    message = f"value for {column} must be a number, got {raw!r}"
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            raise _ItemError(message) from None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise _ItemError(message)
    return raw


def _whole(raw: Any, label: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ItemError(f"{label} must be a whole number, got {raw!r}")
    if not low <= raw <= high:
        raise _ItemError(f"{label} must be from {low} to {high}, got {raw}")
    return raw


def _iso_date(raw: Any, column: str) -> date:
    if not isinstance(raw, str) or not ISO_DATE.fullmatch(raw):
        raise _ItemError(f"date for {column} must be YYYY-MM-DD, got {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise _ItemError(f"date for {column} is not a real date: {raw}") from None


def _numeric_value(operator: str, raw: Any, column: str) -> Any:
    if operator == "BETWEEN":
        if not isinstance(raw, list) or len(raw) != 2:
            raise _ItemError(f"BETWEEN on {column} needs [low, high]")
        low, high = _number(raw[0], column), _number(raw[1], column)
        if low > high:
            raise _ItemError(f"range for {column} is reversed: {low} is above {high}")
        return [low, high]
    return _number(raw, column)


def _date_value(operator: str, raw: Any, column: str) -> Any:
    if operator == "year":
        return _whole(raw, f"year for {column}", 1, 9999)
    if operator == "month":
        return _whole(raw, f"month for {column}", 1, 12)
    if operator not in ("year_month", "date_between"):
        raise _ItemError(f"operator {operator} has no value rule")
    if not isinstance(raw, list) or len(raw) != 2:
        raise _ItemError(f"{operator} on {column} needs a list of two values")
    if operator == "year_month":
        return [_whole(raw[0], f"year for {column}", 1, 9999), _whole(raw[1], f"month for {column}", 1, 12)]
    start, end = _iso_date(raw[0], column), _iso_date(raw[1], column)
    if start > end:
        raise _ItemError(f"date range for {column} is reversed: {start} is after {end}")
    return [start.isoformat(), end.isoformat()]


def _limit(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise _ItemError(f"must be a positive whole number or null, got {raw!r}")
    return raw


class _PlanMatcher:
    def __init__(
        self,
        resolved: Resolved,
        lookup: ValueLookup | None,
        business: BusinessSemantics | None,
        capabilities: dict[str, bool] | None,
    ) -> None:
        physical, semantic = resolved.physical, resolved.semantic
        self.lookup = lookup
        self.business = business
        self.capabilities = capabilities
        self.tables = [table["name"] for table in physical["tables"]]
        self.columns = {table["name"]: [col["name"] for col in table["columns"]] for table in physical["tables"]}
        self.meta = {(col["table"], col["column"]): col for col in semantic.get("columns") or []}
        self.numeric_ops = [op["operator"] for op in semantic.get("numeric_operators") or []]
        self.matches: list[dict[str, Any]] = []
        self.reasons: list[str] = []

    def run(self, plan: Any) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise MatchError(["plan must be a JSON object"])
        table = self._attempt("table", self._table, plan.get("table"))
        if table is None:
            raise MatchError(self.reasons)
        if plan.get("repeat_purchase") is not None:
            return self._repeat(plan, table)

        metrics = self._list("metrics", plan.get("metrics"), self._metric, table)
        aggregate = bool(metrics)
        aliases = [metric["alias"] for metric in metrics if metric]
        for alias in sorted({alias for alias in aliases if aliases.count(alias) > 1}):
            self.reasons.append(f"metrics: alias '{alias}' is used more than once")

        dimensions: list[Any] = []
        select: list[str] = []
        if aggregate:
            if plan.get("select"):
                self.reasons.append("select: cannot be used together with metrics; use dimensions instead")
            dimensions = self._list("dimensions", plan.get("dimensions"), self._column, table)
        else:
            if plan.get("dimensions"):
                self.reasons.append("dimensions: need at least one metric")
            select = self._select(plan.get("select"), table)
        dimension_names = [name for name in dimensions if name]
        for alias in aliases:
            if alias in dimension_names:
                self.reasons.append(f"metrics: alias '{alias}' is also a dimension name")

        filters = self._list("filters", plan.get("filters"), self._filter, table)

        group_by: list[Any] = []
        having: list[Any] = []
        calculations: list[Any] = []
        if aggregate:
            group_by = self._list("group_by", plan.get("group_by"), self._column, table)
            having = self._list("having", plan.get("having"), self._having, aliases)
            calculations = self._list("calculations", plan.get("calculations"), self._calculation, table, aliases)
            if None not in group_by and None not in dimensions and sorted(group_by) != sorted(dimensions):
                self.reasons.append("group_by: must list the same columns as dimensions")
        else:
            for key in ("group_by", "having", "calculations"):
                if plan.get(key):
                    self.reasons.append(f"{key}: needs at least one metric")

        outputs = dimension_names if aggregate else [name for name in select if name]
        order_by: list[Any] = []
        if aggregate and not dimensions and plan.get("order_by"):
            self.reasons.append("order_by: a single-value aggregate has nothing to order")
        else:
            order_by = self._list("order_by", plan.get("order_by"), self._order_by, table, aliases, outputs)

        limit = None
        if plan.get("limit") is not None:
            limit = self._attempt("limit", _limit, plan.get("limit"))
            if aggregate and not dimensions and limit is not None:
                self.reasons.append("limit: a single-value aggregate has no limit")

        self._check_features(group_by, having, order_by, calculations)
        if self.reasons:
            raise MatchError(self.reasons)
        return {
            "table": table,
            "select": select,
            "dimensions": dimensions,
            "metrics": metrics,
            "filters": filters,
            "group_by": group_by,
            "having": having,
            "order_by": order_by,
            "calculations": calculations,
            "limit": limit,
            "repeat_purchase": None,
            "matches": self.matches,
        }

    def _repeat(self, plan: dict[str, Any], table: str) -> dict[str, Any]:
        """A repeat-purchase question is one fixed analysis, so no other plan part may be used with it."""
        for key in ("select", "metrics", "dimensions", "filters", "group_by", "having", "order_by", "calculations"):
            if plan.get(key):
                self.reasons.append(f"{key}: cannot be used with repeat_purchase")
        if plan.get("limit") is not None:
            self.reasons.append("limit: cannot be used with repeat_purchase")
        repeat = self._attempt("repeat_purchase", self._repeat_columns, plan["repeat_purchase"], table)
        if self.reasons:
            raise MatchError(self.reasons)
        return {
            "table": table,
            "select": [],
            "dimensions": [],
            "metrics": [],
            "filters": [],
            "group_by": [],
            "having": [],
            "order_by": [],
            "calculations": [],
            "limit": None,
            "repeat_purchase": repeat,
            "matches": self.matches,
        }

    def _repeat_columns(self, raw: Any, table: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with customer, date and dimension")
        customer = self._column(raw.get("customer"), table)
        date_column = self._column(raw.get("date"), table)
        if not _is_date(self.meta_of(table, date_column)):
            raise _ItemError(f"date {date_column} is not a date column")
        dimension = raw.get("dimension")
        if dimension is not None:
            dimension = self._column(dimension, table)
            if dimension in (customer, date_column):
                raise _ItemError("dimension must be different from the customer and date columns")
        return {"customer": customer, "date": date_column, "dimension": dimension}

    def _check_features(self, group_by: list[Any], having: list[Any], order_by: list[Any], calculations: list[Any]) -> None:
        if self.capabilities is None:
            return
        used = [
            ("group_by", group_by),
            ("having", having),
            ("order_by", order_by),
            ("calculations", calculations),
            ("date_grains", [calc for calc in calculations if calc and calc.get("grain")]),
        ]
        for flag, items in used:
            if items and not self.capabilities.get(flag, False):
                self.reasons.append(f"{flag}: is turned off in config.yaml capabilities")

    def _list(self, label: str, raw: Any, fn: Callable[..., Any], *args: Any) -> list[Any]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.reasons.append(f"{label}: must be a list")
            return []
        return [self._attempt(f"{label}[{i}]", fn, item, *args) for i, item in enumerate(raw)]

    def _alias(self, raw: Any, aliases: Sequence[str], label: str) -> str:
        if not isinstance(raw, str):
            raise _ItemError(f"{label} must be text, got {raw!r}")
        name, source = _pick(raw, f"{label} alias", [_Candidate(alias) for alias in aliases])
        self.record(raw, "alias", name, source)
        return name

    def record(self, item: Any, kind: str, result: Any, source: str) -> None:
        self.matches.append({"item": item, "kind": kind, "result": result, "source": source})

    def meta_of(self, table: str, column: str) -> dict[str, Any]:
        return self.meta.get((table, column), {})

    def _attempt(self, label: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except _ItemError as error:
            self.reasons.append(f"{label}: {error}")
            return None

    def _select(self, raw: Any, table: str) -> list[str]:
        if not isinstance(raw, list) or not raw:
            self.reasons.append("select: a row query needs at least one column, or use an aggregation")
            return []
        if raw == ["*"]:
            self.record("*", "column", f"all {len(self.columns[table])} columns", "rule")
            return list(self.columns[table])
        if "*" in raw:
            self.reasons.append("select: '*' cannot be combined with other columns")
            return []
        return [self._attempt(f"select[{i}]", self._column, item, table) for i, item in enumerate(raw)]

    def _table(self, item: Any) -> str:
        if not isinstance(item, str):
            raise _ItemError(f"must be text, got {item!r}")
        name, source = _pick(item, "table", [_Candidate(table) for table in self.tables])
        self.record(item, "table", name, source)
        return name

    def _column(self, item: Any, table: str) -> str:
        if not isinstance(item, str):
            raise _ItemError(f"column must be text, got {item!r}")
        candidates = [_Candidate(name, self._synonyms(table, name)) for name in self.columns[table]]
        name, source = _pick(item, f"column of {table}", candidates)
        self.record(item, "column", name, source)
        return name

    def _synonyms(self, table: str, column: str) -> tuple[str, ...]:
        words = [str(word) for word in self.meta_of(table, column).get("synonyms") or []]
        if self.business is not None:
            words += self.business.synonyms_for(table, column)
        return tuple(words)

    def _metric(self, raw: Any, table: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with function, column and alias")
        function = raw.get("function")
        if not isinstance(function, str) or function.upper() not in METRIC_FUNCTIONS:
            raise _ItemError(f"function {function!r} is not allowed. Allowed: {', '.join(METRIC_FUNCTIONS)}")
        function = function.upper()
        self.record(function, "aggregation", function, "allowed list")

        alias = raw.get("alias")
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise _ItemError(f"alias {alias!r} must start with a letter or underscore and use only letters, digits and underscores")
        self.record(alias, "alias", alias, "rule")

        target = raw.get("column")
        where = raw.get("where")
        if function == "SHARE":
            return self._share(target, where, table, alias)
        if where is not None:
            raise _ItemError("where is only allowed with the SHARE function")
        if target == "*":
            if function != "COUNT":
                raise _ItemError(f"{function} needs a column, '*' is only for COUNT")
            self.record("*", "aggregation", "*", "rule")
            return {"function": function, "column": "*", "alias": alias}
        column = self._column(target, table)
        if function in NUMERIC_AGGREGATES and not _is_numeric(self.meta_of(table, column)):
            raise _ItemError(f"{function} needs a numeric column, but {column} is not numeric")
        return {"function": function, "column": column, "alias": alias}

    def _share(self, target: Any, raw_where: Any, table: str, alias: str) -> dict[str, Any]:
        """SHARE is the fraction of rows that match every where filter. It counts rows, so its column is '*'."""
        if target != "*":
            raise _ItemError("SHARE counts rows, so its column must be '*'")
        if not isinstance(raw_where, list) or not raw_where:
            raise _ItemError("SHARE needs a non-empty where list of filters")
        conditions = [self._attempt(f"where[{i}]", self._filter, item, table) for i, item in enumerate(raw_where)]
        if None in conditions:
            raise _ItemError("a where filter does not match the metadata")
        self.record("*", "aggregation", "*", "rule")
        return {"function": "SHARE", "column": "*", "alias": alias, "where": conditions}

    def _having(self, raw: Any, aliases: Sequence[str]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with metric, operator and value")
        metric = self._alias(raw.get("metric"), aliases, "metric")
        operator = _canonical(raw.get("operator"), self.numeric_ops)
        self.record(raw.get("operator"), "operator", operator, "allowed list")
        value = _numeric_value(operator, raw.get("value"), metric)
        return {"metric": metric, "operator": operator, "value": value}

    def _order_by(self, raw: Any, table: str, aliases: Sequence[str], outputs: Sequence[str]) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with ref and direction")
        ref = raw.get("ref")
        if not isinstance(ref, str):
            raise _ItemError(f"ref must be text, got {ref!r}")
        candidates = [_Candidate(alias) for alias in aliases]
        candidates += [_Candidate(name, self._synonyms(table, name)) for name in outputs]
        name, source = _pick(ref, "output column or metric alias", candidates)
        self.record(ref, "output", name, source)
        direction = _canonical(raw.get("direction"), SORT_DIRECTIONS, "direction")
        return {"ref": name, "direction": direction}

    def _calculation(self, raw: Any, table: str, aliases: Sequence[str]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with type, metric, date_column, grain and year")
        kind = _canonical(raw.get("type"), CALCULATION_TYPES, "calculation type")
        metric = self._alias(raw.get("metric"), aliases, "metric")
        column = self._column(raw.get("date_column"), table)
        if not _is_date(self.meta_of(table, column)):
            raise _ItemError(f"date_column {column} is not a date column")
        grain = _canonical(raw.get("grain"), DATE_GRAINS, "grain")
        year = _whole(raw.get("year"), f"year for {column}", 1, 9999)
        self.record(kind, "calculation", kind, "allowed list")
        return {"type": kind, "metric": metric, "date_column": column, "grain": grain, "year": year}

    def _filter(self, raw: Any, table: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _ItemError("must be an object with column, operator and value")
        column = self._column(raw.get("column"), table)
        meta = self.meta_of(table, column)

        if _is_date(meta):
            operator = _canonical(raw.get("operator"), list(meta.get("date_operators") or []))
            value = _date_value(operator, raw.get("value"), column)
            value_source = "rule"
        elif _is_numeric(meta):
            operator = _canonical(raw.get("operator"), self.numeric_ops)
            value = _numeric_value(operator, raw.get("value"), column)
            value_source = "rule"
        else:
            if raw.get("operator") != "=":
                raise _ItemError(f"operator {raw.get('operator')!r} is not allowed for text column {column}. Only '=' is allowed")
            operator = "="
            value, value_source = self._text_value(table, column, meta, raw.get("value"))

        self.record(raw.get("operator"), "operator", operator, "allowed list")
        self.record(raw.get("value"), "value", value, value_source)
        return {"column": column, "operator": operator, "value": value}

    def _text_value(self, table: str, column: str, meta: dict[str, Any], raw: Any) -> tuple[str, str]:
        if not isinstance(raw, str) or not raw.strip():
            raise _ItemError(f"value for {column} must be non-empty text")
        if "values" in meta:
            return _pick(raw, f"stored value of {column}", _value_candidates(meta))
        if self.lookup is None:
            raise _ItemError(f"cannot check '{raw}' for {column}: it has no stored values and no lookup is available")
        found = self.lookup(table, column, raw)
        if len(found) == 1:
            return found[0], "lookup"
        if not found:
            raise _ItemError(f"'{raw}' is not a value of {column}")
        raise _ItemError(f"'{raw}' matches more than one value of {column}: {', '.join(found)}")


def match_plan(
    plan: Any,
    resolved: Resolved,
    lookup: ValueLookup | None = None,
    business: BusinessSemantics | None = None,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return the matched plan. Raise MatchError with every problem found when any item fails to match.

    When capabilities is given, features turned off in it reject the plan. None disables that check.
    """
    return _PlanMatcher(resolved, lookup, business, capabilities).run(plan)
