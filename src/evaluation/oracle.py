"""Independent reference SQL for the answerable benchmark questions.

Each oracle is plain DuckDB SQL run directly against the benchmark database,
bypassing the planner, validators, and executor. It is the expected answer that
pipeline output is compared against. Interpretation choices are recorded in
each OracleSpec.interpretation so they can be reviewed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


class OracleError(RuntimeError):
    """Raised when an oracle cannot run against a database."""


@dataclass(frozen=True)
class OraclePart:
    name: str
    sql: str
    key_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class OracleSpec:
    question_id: str
    database: str
    parts: tuple[OraclePart, ...]
    interpretation: str


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _discover_schema(con: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    rows = con.execute(
        "SELECT DISTINCT database_name, schema_name FROM duckdb_tables() WHERE NOT internal"
    ).fetchall()
    if len(rows) != 1:
        raise OracleError(f"expected exactly one user schema, found {rows}")
    return rows[0][0], rows[0][1]


def run_oracle(spec: OracleSpec, db_path: Path) -> dict[str, pd.DataFrame]:
    """Run every part of an oracle against a read-only DuckDB file."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        database, schema = _discover_schema(con)
        con.execute(f"USE {_quote(database)}.{_quote(schema)}")
        return {part.name: con.execute(part.sql).df() for part in spec.parts}
    finally:
        con.close()


def _scalar(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _normalise(frame: pd.DataFrame, sort_by: list[str]) -> pd.DataFrame:
    return frame.sort_values(sort_by, kind="mergesort", na_position="last").reset_index(drop=True)


def compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    key_columns: tuple[str, ...] = (),
    rtol: float = 1e-9,
    atol: float = 1e-9,
) -> list[str]:
    """Return human-readable mismatches; an empty list means the frames agree.

    Rows are matched by key_columns when given, otherwise by every column. Numeric
    columns use np.isclose with the given tolerance. Dates are compared as dates.
    """
    if list(expected.columns) != list(actual.columns):
        return [f"columns differ: expected {list(expected.columns)}, got {list(actual.columns)}"]
    if len(expected) != len(actual):
        return [f"row count differs: expected {len(expected)}, got {len(actual)}"]

    sort_by = list(key_columns) or list(expected.columns)
    for label, frame in (("expected", expected), ("actual", actual)):
        if key_columns and frame.duplicated(subset=list(key_columns)).any():
            return [f"{label} frame has duplicate keys for {list(key_columns)}"]

    exp = _normalise(expected, sort_by)
    act = _normalise(actual, sort_by)
    problems: list[str] = []

    for column in expected.columns:
        e, a = exp[column], act[column]
        e_null, a_null = e.isna(), a.isna()
        if not (e_null == a_null).all():
            problems.append(f"{column}: NULL positions differ")
            continue
        keep = ~e_null
        if keep.sum() == 0:
            continue
        if pd.api.types.is_bool_dtype(e) or pd.api.types.is_bool_dtype(a):
            same = e[keep].astype(bool).to_numpy() == a[keep].astype(bool).to_numpy()
        elif pd.api.types.is_numeric_dtype(e) and pd.api.types.is_numeric_dtype(a):
            same = np.isclose(
                e[keep].astype(float).to_numpy(),
                a[keep].astype(float).to_numpy(),
                rtol=rtol,
                atol=atol,
            )
        else:
            same = np.array(
                [_scalar(x) == _scalar(y) for x, y in zip(e[keep], a[keep])],
                dtype=bool,
            )
        if not same.all():
            bad = np.flatnonzero(~same)[:3]
            examples = [
                f"row {int(exp.index[keep][i])}: expected {_scalar(exp[column][keep].iloc[i])!r}, "
                f"got {_scalar(act[column][keep].iloc[i])!r}"
                for i in bad
            ]
            problems.append(f"{column}: {int((~same).sum())} mismatches; " + "; ".join(examples))
    return problems


# ---------------------------------------------------------------------------
# Superstore shared CTEs
# ---------------------------------------------------------------------------

_SS03_BASE = """
WITH events AS (
  SELECT DISTINCT Customer_ID, Order_ID, CAST(Order_Date AS DATE) AS order_date FROM Orders
),
seq AS (
  SELECT Customer_ID, Order_ID, order_date,
    ROW_NUMBER() OVER (PARTITION BY Customer_ID ORDER BY order_date, Order_ID) AS rn
  FROM events
),
per_customer AS (
  SELECT Customer_ID,
    MAX(CASE WHEN rn = 1 THEN order_date END) AS first_order_date,
    MAX(CASE WHEN rn = 2 THEN order_date END) AS second_order_date,
    date_diff('day', MAX(CASE WHEN rn = 1 THEN order_date END),
                     MAX(CASE WHEN rn = 2 THEN order_date END)) AS days_to_second_order
  FROM seq GROUP BY Customer_ID
)
"""

_SS10_BASE = """
WITH cust AS (
  SELECT Customer_ID,
    COUNT(DISTINCT Order_ID) AS orders,
    SUM(Sales) AS sales,
    MIN(CAST(Order_Date AS DATE)) AS first_order_date,
    MAX(CAST(Order_Date AS DATE)) AS last_order_date
  FROM Orders GROUP BY Customer_ID
),
scored AS (
  SELECT *,
    sales / SUM(sales) OVER () AS sales_share,
    CASE WHEN orders = 1 THEN 'one_order' ELSE 'repeat_order' END AS customer_class
  FROM cust
)
"""

_SS11_BASE = """
WITH monthly AS (
  SELECT CAST(date_trunc('month', Order_Date) AS DATE) AS month,
    SUM(Sales) AS sales, SUM(Profit) AS profit
  FROM Orders GROUP BY ALL
),
joined AS (
  SELECT m.month, m.sales, m.profit,
    p.sales AS prior_month_sales, p.profit AS prior_month_profit,
    m.sales - p.sales AS mom_sales_change,
    (m.sales - p.sales) / NULLIF(p.sales, 0) AS mom_sales_pct,
    y.sales AS prior_year_sales, y.profit AS prior_year_profit,
    m.sales - y.sales AS yoy_sales_change,
    (m.sales - y.sales) / NULLIF(y.sales, 0) AS yoy_sales_pct,
    m.profit - y.profit AS yoy_profit_change
  FROM monthly m
  LEFT JOIN monthly p ON p.month = CAST(m.month - INTERVAL 1 MONTH AS DATE)
  LEFT JOIN monthly y ON y.month = CAST(m.month - INTERVAL 12 MONTH AS DATE)
)
"""

_SS12_BASE = """
WITH order_level AS (
  SELECT Order_ID,
    ANY_VALUE(Segment) AS segment,
    SUM(Sales) AS sales,
    SUM(Profit) AS profit,
    CAST(SUM(Quantity) AS BIGINT) AS quantity,
    SUM(Discount * Sales) / NULLIF(SUM(Sales), 0) AS weighted_discount
  FROM Orders GROUP BY Order_ID
),
deciled AS (
  SELECT *,
    NTILE(10) OVER (ORDER BY weighted_discount DESC, Order_ID) AS discount_decile,
    NTILE(10) OVER (ORDER BY profit ASC, Order_ID) AS profit_decile
  FROM order_level
)
"""

_SS14_BASE = """
WITH yearly AS (
  SELECT Region, Sub_Category, CAST(year(Order_Date) AS BIGINT) AS yr, SUM(Sales) AS sales
  FROM Orders GROUP BY ALL
),
shares AS (
  SELECT Region, Sub_Category, yr, sales,
    sales / SUM(sales) OVER (PARTITION BY Region, yr) AS share
  FROM yearly
),
lagged AS (
  SELECT Region, Sub_Category, yr, sales, share,
    LAG(share) OVER w AS prior_share,
    LAG(yr) OVER w AS prior_yr
  FROM shares
  WINDOW w AS (PARTITION BY Region, Sub_Category ORDER BY yr)
),
shifts AS (
  SELECT Region, Sub_Category, yr, sales, share, prior_share,
    share - prior_share AS share_shift
  FROM lagged
  WHERE prior_yr = yr - 1
)
"""

_SS15_BASE = """
WITH order_events AS (
  SELECT DISTINCT Customer_ID, Order_ID, CAST(Order_Date AS DATE) AS order_date FROM Orders
),
order_seq AS (
  SELECT Customer_ID, Order_ID,
    ROW_NUMBER() OVER (PARTITION BY Customer_ID ORDER BY order_date, Order_ID) AS seq
  FROM order_events
),
line_seq AS (
  SELECT o.Customer_ID, o.Category, s.seq
  FROM Orders o
  JOIN order_seq s ON s.Customer_ID = o.Customer_ID AND s.Order_ID = o.Order_ID
),
per_customer AS (
  SELECT Customer_ID, COUNT(DISTINCT Category) AS total_categories
  FROM line_seq GROUP BY Customer_ID
  HAVING COUNT(DISTINCT Category) >= 3
),
first_cnt AS (
  SELECT Customer_ID, COUNT(DISTINCT Category) AS first_order_categories
  FROM line_seq WHERE seq = 1 GROUP BY Customer_ID
),
later_cnt AS (
  SELECT Customer_ID, COUNT(DISTINCT Category) AS later_order_categories
  FROM line_seq WHERE seq > 1 GROUP BY Customer_ID
),
multi_category AS (
  SELECT p.Customer_ID, p.total_categories,
    COALESCE(f.first_order_categories, 0) AS first_order_categories,
    COALESCE(l.later_order_categories, 0) AS later_order_categories
  FROM per_customer p
  LEFT JOIN first_cnt f ON f.Customer_ID = p.Customer_ID
  LEFT JOIN later_cnt l ON l.Customer_ID = p.Customer_ID
)
"""

_SS17_BASE = """
WITH lines AS (
  SELECT CAST(date_trunc('month', Order_Date) AS DATE) AS order_month,
    CAST(date_trunc('month', Ship_Date) AS DATE) AS ship_month,
    Order_ID, Sales
  FROM Orders
),
order_months AS (
  SELECT order_month AS month, SUM(Sales) AS sales_by_order_month FROM lines GROUP BY order_month
),
ship_months AS (
  SELECT ship_month AS month, SUM(Sales) AS sales_by_ship_month FROM lines GROUP BY ship_month
),
monthly AS (
  SELECT o.month, o.sales_by_order_month, s.sales_by_ship_month,
    o.sales_by_order_month - s.sales_by_ship_month AS difference
  FROM order_months o
  JOIN ship_months s ON s.month = o.month
),
both_roles AS (
  SELECT l.* FROM lines l
  WHERE l.order_month IN (SELECT month FROM monthly)
    AND l.ship_month IN (SELECT month FROM monthly)
)
"""

_SS18_BASE = """
WITH orders_level AS (
  SELECT Order_ID,
    ANY_VALUE(Region) AS region,
    ANY_VALUE(Segment) AS segment,
    CAST(MIN(Order_Date) AS DATE) AS order_date,
    CAST(MIN(Ship_Date) AS DATE) AS ship_date,
    SUM(Sales) AS sales,
    SUM(Profit) AS profit
  FROM Orders GROUP BY Order_ID
),
classed AS (
  SELECT *,
    date_diff('day', order_date, ship_date) AS delay_days,
    CAST((year(ship_date) - year(order_date)) * 12 + month(ship_date) - month(order_date) AS BIGINT) AS month_gap,
    CASE
      WHEN (year(ship_date) - year(order_date)) * 12 + month(ship_date) - month(order_date) < 0 THEN 'ship_before_order'
      WHEN (year(ship_date) - year(order_date)) * 12 + month(ship_date) - month(order_date) = 0 THEN 'same_month'
      WHEN (year(ship_date) - year(order_date)) * 12 + month(ship_date) - month(order_date) = 1 THEN 'next_month'
      ELSE 'later_month'
    END AS fulfilment_class
  FROM orders_level
)
"""

# ---------------------------------------------------------------------------
# Netflix shared CTEs
# ---------------------------------------------------------------------------

_NF01_BASE = """
WITH added AS (
  SELECT type, CAST(show_id AS BIGINT) AS show_id,
    CAST(year(TRY_CAST(date_added AS TIMESTAMP)) AS BIGINT) AS added_year
  FROM netflix_titles
  WHERE type IN ('Movie', 'TV Show') AND TRY_CAST(date_added AS TIMESTAMP) IS NOT NULL
),
counts AS (
  SELECT type, added_year, COUNT(DISTINCT show_id) AS titles_added
  FROM added GROUP BY type, added_year
),
bounds AS (
  SELECT MIN(added_year) AS first_year, MAX(added_year) AS last_year FROM added
),
spine AS (
  SELECT t.type, y.added_year
  FROM (SELECT DISTINCT type FROM added) t
  CROSS JOIN (SELECT UNNEST(range(first_year, last_year + 1)) AS added_year FROM bounds) y
),
filled AS (
  SELECT s.type, s.added_year, COALESCE(c.titles_added, 0) AS titles_added
  FROM spine s
  LEFT JOIN counts c ON c.type = s.type AND c.added_year = s.added_year
),
changes AS (
  SELECT type, added_year, titles_added,
    LAG(titles_added) OVER w AS prior_year_titles,
    titles_added - LAG(titles_added) OVER w AS abs_change,
    (titles_added - LAG(titles_added) OVER w) / NULLIF(LAG(titles_added) OVER w, 0) AS pct_change
  FROM filled
  WINDOW w AS (PARTITION BY type ORDER BY added_year)
),
ranked AS (
  SELECT *,
    CASE WHEN abs_change IS NOT NULL
      THEN RANK() OVER (PARTITION BY type ORDER BY abs_change DESC NULLS LAST) END AS growth_rank,
    CASE WHEN abs_change IS NOT NULL
      THEN RANK() OVER (PARTITION BY type ORDER BY abs_change ASC NULLS LAST) END AS decline_rank
  FROM changes
)
"""

_NF04_BASE = """
WITH titles AS (
  SELECT CAST(show_id AS BIGINT) AS show_id, type,
    TRY_CAST(duration_minutes AS DOUBLE) AS minutes,
    TRY_CAST(duration_seasons AS DOUBLE) AS seasons
  FROM netflix_titles
  WHERE type IN ('Movie', 'TV Show')
),
pair_titles AS (
  SELECT DISTINCT c.country, cat.listed_in AS category, t.show_id, t.type, t.minutes, t.seasons
  FROM netflix_titles_countries c
  JOIN netflix_titles_category cat ON cat.show_id = c.show_id
  JOIN titles t ON t.show_id = c.show_id
),
pairs AS (
  SELECT country, category,
    COUNT(DISTINCT show_id) AS titles,
    COUNT(DISTINCT CASE WHEN type = 'Movie' THEN show_id END) AS movies,
    COUNT(DISTINCT CASE WHEN type = 'TV Show' THEN show_id END) AS tv_shows,
    MEDIAN(CASE WHEN type = 'Movie' THEN minutes END) AS median_movie_minutes,
    AVG(CASE WHEN type = 'TV Show' THEN seasons END) AS avg_tv_seasons
  FROM pair_titles GROUP BY country, category
),
category_baselines AS (
  SELECT category, AVG(titles) AS avg_pair_titles, COUNT(*) AS pair_count
  FROM pairs GROUP BY category
),
movie_minutes AS (
  SELECT DISTINCT category, show_id, minutes FROM pair_titles WHERE type = 'Movie'
),
category_movie_baseline AS (
  SELECT category, MEDIAN(minutes) AS category_median_movie_minutes
  FROM movie_minutes GROUP BY category
),
flagged AS (
  SELECT p.country, p.category, p.titles, p.median_movie_minutes,
    b.avg_pair_titles, m.category_median_movie_minutes
  FROM pairs p
  JOIN category_baselines b ON b.category = p.category
  JOIN category_movie_baseline m ON m.category = p.category
  WHERE p.titles > b.avg_pair_titles
    AND p.median_movie_minutes < m.category_median_movie_minutes
)
"""

_NF05_BASE = """
WITH titles AS (
  SELECT CAST(show_id AS BIGINT) AS show_id, type, CAST(release_year AS INTEGER) AS release_year
  FROM netflix_titles
  WHERE type IN ('Movie', 'TV Show')
),
cast_titles AS (
  SELECT DISTINCT c."cast" AS cast_member, t.show_id, t.type, t.release_year
  FROM netflix_titles_cast c
  JOIN titles t ON t.show_id = c.show_id
  WHERE c."cast" IS NOT NULL
),
cast_summary AS (
  SELECT cast_member,
    COUNT(DISTINCT show_id) AS titles,
    COUNT(DISTINCT release_year) AS distinct_release_years,
    COUNT(DISTINCT CASE WHEN type = 'Movie' THEN show_id END) AS movie_titles,
    COUNT(DISTINCT CASE WHEN type = 'TV Show' THEN show_id END) AS tv_titles,
    MIN(release_year) AS first_release_year,
    MAX(release_year) AS latest_release_year
  FROM cast_titles GROUP BY cast_member
),
per_year AS (
  SELECT cast_member, release_year, COUNT(DISTINCT show_id) AS appearances
  FROM cast_titles GROUP BY cast_member, release_year
)
"""


ORACLES: dict[str, OracleSpec] = {
    "SS01": OracleSpec(
        question_id="SS01",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH monthly AS (
  SELECT Sub_Category, CAST(date_trunc('month', Order_Date) AS DATE) AS month,
    SUM(Sales) AS sales, SUM(Profit) AS profit,
    COUNT(DISTINCT Order_ID) AS orders, COUNT(DISTINCT Customer_ID) AS customers
  FROM Orders GROUP BY ALL
),
with_prev AS (
  SELECT Sub_Category, month, sales, profit, orders, customers,
    LAG(month) OVER w AS prev_month,
    LAG(sales) OVER w AS prev_sales
  FROM monthly
  WINDOW w AS (PARTITION BY Sub_Category ORDER BY month)
)
SELECT Sub_Category, month, prev_month, sales, prev_sales,
  sales - prev_sales AS sales_change,
  (sales - prev_sales) / NULLIF(prev_sales, 0) AS sales_pct_change,
  profit, orders, customers,
  COALESCE((sales - prev_sales) / NULLIF(prev_sales, 0) < -0.15, FALSE) AS sales_fell_over_15pct
FROM with_prev
WHERE prev_sales IS NOT NULL
ORDER BY Sub_Category, month
""",
                ("Sub_Category", "month"),
            ),
        ),
        interpretation=(
            "Month-over-month per sub-category over observed months (LAG over observed rows). "
            "Flag is true when the change is below -15%."
        ),
    ),
    "SS02": OracleSpec(
        question_id="SS02",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH sub AS (
  SELECT Region, Sub_Category,
    SUM(Sales) AS sales, SUM(Profit) AS profit,
    SUM(Profit) / NULLIF(SUM(Sales), 0) AS profit_margin,
    COUNT(DISTINCT Order_ID) AS orders
  FROM Orders GROUP BY ALL
),
ranked AS (
  SELECT *, RANK() OVER (PARTITION BY Region ORDER BY profit DESC) AS profit_rank FROM sub
)
SELECT * FROM ranked WHERE profit_rank <= 3 ORDER BY Region, profit_rank, Sub_Category
""",
                ("Region", "Sub_Category"),
            ),
        ),
        interpretation="Top 3 sub-categories by profit per region; RANK keeps ties.",
    ),
    "SS03": OracleSpec(
        question_id="SS03",
        database="sample_superstore",
        parts=(
            OraclePart(
                "per_customer",
                _SS03_BASE + "SELECT * FROM per_customer ORDER BY Customer_ID",
                ("Customer_ID",),
            ),
            OraclePart(
                "summary",
                _SS03_BASE + """
SELECT COUNT(*) AS customers,
  COUNT(second_order_date) AS returning_customers,
  COUNT(*) - COUNT(second_order_date) AS customers_without_second_order,
  AVG(days_to_second_order) AS avg_days_to_second_order
FROM per_customer
""",
            ),
        ),
        interpretation=(
            "Order-level events (distinct Customer_ID, Order_ID, date). Days from first to "
            "second order; averages cover returning customers only."
        ),
    ),
    "SS05": OracleSpec(
        question_id="SS05",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH prod AS (
  SELECT Category, Product_ID, SUM(Sales) AS sales FROM Orders GROUP BY ALL
),
ranked AS (
  SELECT Category, Product_ID, sales,
    SUM(sales) OVER (PARTITION BY Category) AS category_sales,
    ROW_NUMBER() OVER (PARTITION BY Category ORDER BY sales DESC, Product_ID) AS sales_rank,
    SUM(sales) OVER (
      PARTITION BY Category ORDER BY sales DESC, Product_ID
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_sales
  FROM prod
),
shares AS (
  SELECT *, sales / category_sales AS share,
    cum_sales / category_sales AS cum_share,
    COALESCE((cum_sales - sales) / category_sales, 0) AS prior_cum_share
  FROM ranked
)
SELECT Category, Product_ID, sales, share, cum_share, sales_rank
FROM shares
WHERE prior_cum_share < 0.8
ORDER BY Category, sales_rank
""",
                ("Category", "Product_ID"),
            ),
        ),
        interpretation=(
            "Products forming the top 80% of sales per category. The product that crosses 80% "
            "is included."
        ),
    ),
    "SS07": OracleSpec(
        question_id="SS07",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH lines AS (
  SELECT Region, Ship_Mode, Sales, Profit,
    date_diff('day', CAST(Order_Date AS DATE), CAST(Ship_Date AS DATE)) AS delay_days
  FROM Orders
),
mode_stats AS (
  SELECT Region, Ship_Mode, COUNT(*) AS lines, SUM(Sales) AS sales, SUM(Profit) AS profit,
    AVG(delay_days) AS avg_delay_days, MEDIAN(delay_days) AS median_delay_days
  FROM lines GROUP BY ALL
),
region_avg AS (
  SELECT Region, AVG(delay_days) AS region_avg_delay_days FROM lines GROUP BY Region
)
SELECT m.Region, m.Ship_Mode, m.lines, m.sales, m.profit, m.avg_delay_days,
  m.median_delay_days,
  RANK() OVER (PARTITION BY m.Region ORDER BY m.median_delay_days DESC) AS median_delay_rank,
  r.region_avg_delay_days,
  m.avg_delay_days > r.region_avg_delay_days AS slower_than_region_avg
FROM mode_stats m
JOIN region_avg r ON r.Region = m.Region
ORDER BY m.Region, median_delay_rank, m.Ship_Mode
""",
                ("Region", "Ship_Mode"),
            ),
        ),
        interpretation=(
            "Delay is Ship_Date minus Order_Date in days. Mode rank by median delay (1 = slowest). "
            "The region average is line-weighted."
        ),
    ),
    "SS09": OracleSpec(
        question_id="SS09",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH products AS (
  SELECT Product_ID, ANY_VALUE(Category) AS Category, ANY_VALUE(Sub_Category) AS Sub_Category,
    SUM(Sales) AS sales, SUM(Profit) AS profit,
    COUNT(DISTINCT Order_ID) AS orders, COUNT(DISTINCT Customer_ID) AS customers
  FROM Orders GROUP BY Product_ID
),
totals AS (
  SELECT SUM(-profit) AS total_loss FROM products WHERE profit < 0
)
SELECT p.Product_ID, p.Category, p.Sub_Category, p.sales, p.profit,
  p.profit / NULLIF(p.sales, 0) AS margin, p.orders, p.customers,
  -p.profit AS loss,
  -p.profit / t.total_loss AS loss_share,
  ROW_NUMBER() OVER (ORDER BY p.profit ASC, p.Product_ID) AS loss_rank
FROM products p
CROSS JOIN totals t
WHERE p.sales > 0 AND p.profit < 0
ORDER BY loss_rank
""",
                ("Product_ID",),
            ),
        ),
        interpretation=(
            "Product grain. loss_share divides by the total loss of all negative-profit products."
        ),
    ),
    "SS10": OracleSpec(
        question_id="SS10",
        database="sample_superstore",
        parts=(
            OraclePart(
                "customers",
                _SS10_BASE + "SELECT * FROM scored ORDER BY Customer_ID",
                ("Customer_ID",),
            ),
            OraclePart(
                "classes",
                _SS10_BASE + """
SELECT customer_class, COUNT(*) AS customers, SUM(sales) AS sales,
  AVG(sales) AS avg_customer_sales, SUM(sales_share) AS sales_share
FROM scored GROUP BY customer_class ORDER BY customer_class
""",
                ("customer_class",),
            ),
        ),
        interpretation=(
            "Customers classed by distinct orders (one_order vs repeat_order). "
            "Share is each customer's sales divided by total sales."
        ),
    ),
    "SS11": OracleSpec(
        question_id="SS11",
        database="sample_superstore",
        parts=(
            OraclePart("monthly", _SS11_BASE + "SELECT * FROM joined ORDER BY month", ("month",)),
            OraclePart(
                "top_positive_yoy",
                _SS11_BASE
                + "SELECT * FROM joined WHERE yoy_sales_change > 0 ORDER BY yoy_sales_change DESC LIMIT 5",
                ("month",),
            ),
            OraclePart(
                "top_negative_yoy",
                _SS11_BASE
                + "SELECT * FROM joined WHERE yoy_sales_change < 0 ORDER BY yoy_sales_change ASC LIMIT 5",
                ("month",),
            ),
        ),
        interpretation=(
            "Prior month and prior year are matched by calendar key, not by row position. "
            "Months with no data have NULL comparisons."
        ),
    ),
    "SS12": OracleSpec(
        question_id="SS12",
        database="sample_superstore",
        parts=(
            OraclePart(
                "segment_summary",
                _SS12_BASE + """
SELECT segment, COUNT(*) AS orders,
  SUM(sales) AS sales, SUM(profit) AS profit,
  AVG(sales) AS avg_order_sales, AVG(profit) AS avg_order_profit,
  SUM(profit) / NULLIF(SUM(sales), 0) AS margin,
  AVG(weighted_discount) AS avg_weighted_discount,
  CAST(SUM(CASE WHEN discount_decile = 1 AND profit_decile = 1 THEN 1 ELSE 0 END) AS BIGINT)
    AS flagged_orders
FROM deciled GROUP BY segment ORDER BY segment
""",
                ("segment",),
            ),
            OraclePart(
                "flagged_orders",
                _SS12_BASE + """
SELECT Order_ID, segment, sales, profit, weighted_discount, discount_decile, profit_decile
FROM deciled
WHERE discount_decile = 1 AND profit_decile = 1
ORDER BY Order_ID
""",
                ("Order_ID",),
            ),
        ),
        interpretation=(
            "Order grain. weighted_discount = SUM(Discount*Sales)/SUM(Sales). Decile 1 is the "
            "highest discount and the lowest profit. Flagged orders are in both decile 1s. "
            "This oracle uses NTILE, which the pipeline allowlist does not yet permit (see F7)."
        ),
    ),
    "SS14": OracleSpec(
        question_id="SS14",
        database="sample_superstore",
        parts=(
            OraclePart(
                "shifts",
                _SS14_BASE + "SELECT * FROM shifts ORDER BY Region, Sub_Category, yr",
                ("Region", "Sub_Category", "yr"),
            ),
            OraclePart(
                "extremes",
                _SS14_BASE + """, ranked AS (
  SELECT Region, Sub_Category, yr, share_shift,
    ROW_NUMBER() OVER (PARTITION BY Region ORDER BY share_shift DESC, Sub_Category, yr) AS up_rank,
    ROW_NUMBER() OVER (PARTITION BY Region ORDER BY share_shift ASC, Sub_Category, yr) AS down_rank
  FROM shifts
)
SELECT Region, 'largest_gain' AS direction, Sub_Category, yr, share_shift
FROM ranked WHERE up_rank = 1
UNION ALL
SELECT Region, 'largest_loss' AS direction, Sub_Category, yr, share_shift
FROM ranked WHERE down_rank = 1
ORDER BY Region, direction
""",
                ("Region", "direction"),
            ),
        ),
        interpretation=(
            "Share of each sub-category within its region-year. Shift is compared with the "
            "previous year only. Largest gain and loss are taken per region over all transitions."
        ),
    ),
    "SS15": OracleSpec(
        question_id="SS15",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                _SS15_BASE + "SELECT * FROM multi_category ORDER BY Customer_ID",
                ("Customer_ID",),
            ),
        ),
        interpretation=(
            "Customers with at least 3 distinct categories. First order is the earliest by "
            "(date, Order_ID). Later-order categories are distinct across all later orders."
        ),
    ),
    "SS16": OracleSpec(
        question_id="SS16",
        database="sample_superstore",
        parts=(
            OraclePart(
                "result",
                """
WITH sub AS (
  SELECT Category, Sub_Category,
    SUM(Sales) AS sales, SUM(Profit) AS profit,
    SUM(Profit) / NULLIF(SUM(Sales), 0) AS margin,
    AVG(Discount) AS avg_discount,
    COUNT(DISTINCT Order_ID) AS orders
  FROM Orders GROUP BY Category, Sub_Category
),
cat AS (
  SELECT Category, SUM(Profit) / NULLIF(SUM(Sales), 0) AS category_margin
  FROM Orders GROUP BY Category
),
overall AS (
  SELECT SUM(Profit) / NULLIF(SUM(Sales), 0) AS overall_margin FROM Orders
),
med AS (
  SELECT MEDIAN(sales) AS median_sales FROM sub
)
SELECT s.Category, s.Sub_Category, s.sales, s.profit, s.margin, s.avg_discount, s.orders,
  c.category_margin, o.overall_margin, m.median_sales
FROM sub s
JOIN cat c ON c.Category = s.Category
CROSS JOIN overall o
CROSS JOIN med m
WHERE s.sales > m.median_sales AND s.margin < o.overall_margin
ORDER BY s.profit ASC, s.Sub_Category
""",
                ("Category", "Sub_Category"),
            ),
        ),
        interpretation=(
            "Sub-category grain. Keeps sub-categories above the median sub-category sales and "
            "below the overall margin. Category margin is informational."
        ),
    ),
    "SS17": OracleSpec(
        question_id="SS17",
        database="sample_superstore",
        parts=(
            OraclePart(
                "monthly",
                _SS17_BASE + "SELECT * FROM monthly ORDER BY month",
                ("month",),
            ),
            OraclePart(
                "summary",
                _SS17_BASE + """
SELECT COUNT(*) AS total_lines,
  CAST(SUM(CASE WHEN order_month <> ship_month THEN 1 ELSE 0 END) AS BIGINT)
    AS lines_shipped_different_month,
  COUNT(DISTINCT Order_ID) AS total_orders,
  COUNT(DISTINCT CASE WHEN order_month <> ship_month THEN Order_ID END)
    AS orders_shipped_different_month
FROM both_roles
""",
            ),
        ),
        interpretation=(
            "Sales by order month and by ship month, joined on month. Summary is restricted to "
            "lines whose order and ship months both appear in the monthly table. An order counts "
            "as shipped in a different month if any of its lines does."
        ),
    ),
    "SS18": OracleSpec(
        question_id="SS18",
        database="sample_superstore",
        parts=(
            OraclePart(
                "fulfilment_groups",
                _SS18_BASE + """
SELECT region, segment, fulfilment_class,
  COUNT(*) AS orders,
  SUM(sales) AS sales,
  SUM(profit) AS profit,
  AVG(sales) AS avg_order_sales,
  AVG(delay_days) AS avg_delay_days,
  AVG(CASE WHEN profit < 0 THEN 1.0 ELSE 0.0 END) AS loss_order_rate
FROM classed
GROUP BY region, segment, fulfilment_class
ORDER BY region, segment, fulfilment_class
""",
                ("region", "segment", "fulfilment_class"),
            ),
        ),
        interpretation=(
            "Order grain. Class is the calendar-month difference between ship and order "
            "(ship_before_order, same_month, next_month, later_month)."
        ),
    ),
    "NF01": OracleSpec(
        question_id="NF01",
        database="netflix_titles",
        parts=(
            OraclePart(
                "changes",
                _NF01_BASE + "SELECT * FROM ranked ORDER BY type, added_year",
                ("type", "added_year"),
            ),
        ),
        interpretation=(
            "Year of date_added (not release_year). Unparseable or missing date_added is excluded. "
            "Missing years are zero-filled. Ranks are within each type by absolute change. The "
            "spec says 'rank years'; per-type ranking is an interpretation."
        ),
    ),
    "NF04": OracleSpec(
        question_id="NF04",
        database="netflix_titles",
        parts=(
            OraclePart(
                "pairs",
                _NF04_BASE + "SELECT * FROM pairs ORDER BY country, category",
                ("country", "category"),
            ),
            OraclePart(
                "category_baselines",
                _NF04_BASE + """
SELECT b.category, b.avg_pair_titles, b.pair_count, m.category_median_movie_minutes
FROM category_baselines b
JOIN category_movie_baseline m ON m.category = b.category
ORDER BY b.category
""",
                ("category",),
            ),
            OraclePart(
                "flagged_pairs",
                _NF04_BASE + "SELECT * FROM flagged ORDER BY country, category",
                ("country", "category"),
            ),
        ),
        interpretation=(
            "Pairs are (country, category). Baselines are per category: the average titles per "
            "pair, and the median movie duration over distinct movies in the category. Flagged "
            "pairs have above-average titles and below-baseline median movie duration."
        ),
    ),
    "NF05": OracleSpec(
        question_id="NF05",
        database="netflix_titles",
        parts=(
            OraclePart(
                "summary",
                _NF05_BASE + """
SELECT *,
  CASE WHEN movie_titles > 0 THEN RANK() OVER (ORDER BY movie_titles DESC) END AS movie_rank,
  CASE WHEN tv_titles > 0 THEN RANK() OVER (ORDER BY tv_titles DESC) END AS tv_rank
FROM cast_summary
ORDER BY cast_member
""",
                ("cast_member",),
            ),
            OraclePart(
                "consecutive_year_changes",
                _NF05_BASE + """
SELECT y.cast_member, y.release_year, y.appearances,
  p.appearances AS prior_year_appearances,
  y.appearances - p.appearances AS appearance_change
FROM per_year y
JOIN per_year p ON p.cast_member = y.cast_member AND p.release_year = y.release_year - 1
ORDER BY y.cast_member, y.release_year
""",
                ("cast_member", "release_year"),
            ),
        ),
        interpretation=(
            "Distinct (show, cast member) pairs; each title counted once per cast member. Ranks "
            "are within movie or TV type by title count. Consecutive-year change is reported only "
            "when the member appears in both years."
        ),
    ),
}
