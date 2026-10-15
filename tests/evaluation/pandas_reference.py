"""Independent pandas recomputations of the oracle SQL in src/evaluation/oracle.py.

Each reference loads raw rows through DuckDB and computes results in pandas.
It does not reuse oracle SQL. Column names and order match the oracle output so
tests can compare them directly.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a read-only connection and select the single schema in the file."""
    stem = Path(db_path).stem
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute(f'USE "{stem}"."{stem}"')
    return con


def _as_date(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for col in columns:
        out[col] = pd.to_datetime(out[col]).dt.date
    return out


def _orders(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute("SELECT * FROM Orders").df()


def _month(series: pd.Series) -> pd.Series:
    return series.dt.to_period("M").dt.to_timestamp()


def ref_ss01(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    o["month"] = _month(o["Order_Date"])
    g = (
        o.groupby(["Sub_Category", "month"], as_index=False)
        .agg(
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            orders=("Order_ID", "nunique"),
            customers=("Customer_ID", "nunique"),
        )
        .sort_values(["Sub_Category", "month"])
        .reset_index(drop=True)
    )
    grouped = g.groupby("Sub_Category")
    g["prev_month"] = grouped["month"].shift(1)
    g["prev_sales"] = grouped["sales"].shift(1)
    g = g[g["prev_sales"].notna()].copy()
    g["sales_change"] = g["sales"] - g["prev_sales"]
    g["sales_pct_change"] = g["sales_change"] / g["prev_sales"]
    g["sales_fell_over_15pct"] = (g["sales_pct_change"] < -0.15).fillna(False).astype(bool)
    result = g[
        [
            "Sub_Category", "month", "prev_month", "sales", "prev_sales",
            "sales_change", "sales_pct_change", "profit", "orders", "customers",
            "sales_fell_over_15pct",
        ]
    ]
    result = result.sort_values(["Sub_Category", "month"]).reset_index(drop=True)
    return {"result": _as_date(result, ["month", "prev_month"])}


def ref_ss02(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    s = (
        o.groupby(["Region", "Sub_Category"], as_index=False)
        .agg(
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            orders=("Order_ID", "nunique"),
        )
    )
    s["profit_margin"] = s["profit"] / s["sales"]
    s["profit_rank"] = (
        s.groupby("Region")["profit"].rank(method="min", ascending=False).astype("int64")
    )
    result = s[s["profit_rank"] <= 3]
    result = result[
        ["Region", "Sub_Category", "sales", "profit", "profit_margin", "orders", "profit_rank"]
    ]
    result = result.sort_values(["Region", "profit_rank", "Sub_Category"]).reset_index(drop=True)
    return {"result": result}


def ref_ss03(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    events = o[["Customer_ID", "Order_ID", "Order_Date"]].drop_duplicates()
    events = events.assign(order_date=events["Order_Date"].dt.normalize())
    events = events.sort_values(["Customer_ID", "order_date", "Order_ID"])
    events["seq"] = events.groupby("Customer_ID").cumcount() + 1
    first = events[events["seq"] == 1].set_index("Customer_ID")["order_date"]
    second = events[events["seq"] == 2].set_index("Customer_ID")["order_date"]
    per = pd.DataFrame({"first_order_date": first})
    per["second_order_date"] = second.reindex(per.index)
    per["days_to_second_order"] = (
        (per["second_order_date"] - per["first_order_date"]).dt.days
    ).astype("Int64")
    per = per.reset_index()
    per = per.sort_values("Customer_ID").reset_index(drop=True)
    per = per[["Customer_ID", "first_order_date", "second_order_date", "days_to_second_order"]]
    returning = per["second_order_date"].notna()
    summary = pd.DataFrame(
        {
            "customers": [len(per)],
            "returning_customers": [int(returning.sum())],
            "customers_without_second_order": [int((~returning).sum())],
            "avg_days_to_second_order": [float(per.loc[returning, "days_to_second_order"].mean())],
        }
    )
    return {
        "per_customer": _as_date(per, ["first_order_date", "second_order_date"]),
        "summary": summary,
    }


def ref_ss05(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    p = o.groupby(["Category", "Product_ID"], as_index=False).agg(sales=("Sales", "sum"))
    p = p.sort_values(["Category", "sales", "Product_ID"], ascending=[True, False, True])
    p["sales_rank"] = p.groupby("Category").cumcount() + 1
    cat_sales = p.groupby("Category")["sales"].transform("sum")
    p["share"] = p["sales"] / cat_sales
    p["cum_share"] = p.groupby("Category")["sales"].cumsum() / cat_sales
    p["prior_cum_share"] = p["cum_share"] - p["share"]
    result = p[p["prior_cum_share"] < 0.8]
    result = result[["Category", "Product_ID", "sales", "share", "cum_share", "sales_rank"]]
    result = result.sort_values(["Category", "sales_rank"]).reset_index(drop=True)
    return {"result": result}


def ref_ss07(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    o = o.assign(
        delay=(o["Ship_Date"].dt.normalize() - o["Order_Date"].dt.normalize()).dt.days
    )
    g = (
        o.groupby(["Region", "Ship_Mode"], as_index=False)
        .agg(
            lines=("Order_ID", "size"),
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            avg_delay_days=("delay", "mean"),
            median_delay_days=("delay", "median"),
        )
    )
    region_avg = o.groupby("Region")["delay"].mean().rename("region_avg_delay_days")
    g = g.join(region_avg, on="Region")
    g["median_delay_rank"] = (
        g.groupby("Region")["median_delay_days"].rank(method="min", ascending=False).astype("int64")
    )
    g["slower_than_region_avg"] = g["avg_delay_days"] > g["region_avg_delay_days"]
    g = g[
        ["Region", "Ship_Mode", "lines", "sales", "profit", "avg_delay_days", "median_delay_days",
         "median_delay_rank", "region_avg_delay_days", "slower_than_region_avg"]
    ]
    g = g.sort_values(["Region", "Ship_Mode"]).reset_index(drop=True)
    return {"result": g}


def ref_ss09(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    p = (
        o.groupby("Product_ID", as_index=False)
        .agg(
            Category=("Category", "first"),
            Sub_Category=("Sub_Category", "first"),
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            orders=("Order_ID", "nunique"),
            customers=("Customer_ID", "nunique"),
        )
    )
    total_loss = -p.loc[p["profit"] < 0, "profit"].sum()
    p["margin"] = p["profit"] / p["sales"].where(p["sales"] != 0)
    p["loss"] = -p["profit"]
    p["loss_share"] = p["loss"] / total_loss
    result = p[(p["sales"] > 0) & (p["profit"] < 0)].copy()
    result = result.sort_values(["profit", "Product_ID"], ascending=[True, True])
    result["loss_rank"] = np.arange(1, len(result) + 1)
    result = result[
        ["Product_ID", "Category", "Sub_Category", "sales", "profit", "margin",
         "orders", "customers", "loss", "loss_share", "loss_rank"]
    ].reset_index(drop=True)
    return {"result": result}


def ref_ss10(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    c = (
        o.groupby("Customer_ID", as_index=False)
        .agg(
            orders=("Order_ID", "nunique"),
            sales=("Sales", "sum"),
            first_order_date=("Order_Date", "min"),
            last_order_date=("Order_Date", "max"),
        )
    )
    c["sales_share"] = c["sales"] / c["sales"].sum()
    c["customer_class"] = np.where(c["orders"] == 1, "one_order", "repeat_order")
    c = c.sort_values("Customer_ID").reset_index(drop=True)
    c = _as_date(c, ["first_order_date", "last_order_date"])
    classes = (
        c.groupby("customer_class", as_index=False)
        .agg(
            customers=("Customer_ID", "size"),
            sales=("sales", "sum"),
            avg_customer_sales=("sales", "mean"),
            sales_share=("sales_share", "sum"),
        )
        .sort_values("customer_class")
        .reset_index(drop=True)
    )
    return {"customers": c, "classes": classes}


def _monthly_with_prior(o: pd.DataFrame) -> pd.DataFrame:
    m = (
        o.assign(month=_month(o["Order_Date"]))
        .groupby("month", as_index=False)
        .agg(sales=("Sales", "sum"), profit=("Profit", "sum"))
        .sort_values("month")
        .reset_index(drop=True)
    )
    by_month = m.set_index("month")
    for lag, label in ((1, "prior_month"), (12, "prior_year")):
        prior_keys = pd.DatetimeIndex(m["month"] - pd.DateOffset(months=lag))
        prior = by_month.reindex(prior_keys)
        m[f"{label}_sales"] = prior["sales"].to_numpy()
        m[f"{label}_profit"] = prior["profit"].to_numpy()
    m["mom_sales_change"] = m["sales"] - m["prior_month_sales"]
    m["mom_sales_pct"] = m["mom_sales_change"] / m["prior_month_sales"]
    m["yoy_sales_change"] = m["sales"] - m["prior_year_sales"]
    m["yoy_sales_pct"] = m["yoy_sales_change"] / m["prior_year_sales"]
    m["yoy_profit_change"] = m["profit"] - m["prior_year_profit"]
    return m[
        ["month", "sales", "profit", "prior_month_sales", "prior_month_profit",
         "mom_sales_change", "mom_sales_pct", "prior_year_sales", "prior_year_profit",
         "yoy_sales_change", "yoy_sales_pct", "yoy_profit_change"]
    ]


def ref_ss11(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    m = _monthly_with_prior(_orders(con))
    positive = m[m["yoy_sales_change"] > 0].sort_values(
        ["yoy_sales_change", "month"], ascending=[False, True]
    ).head(5)
    negative = m[m["yoy_sales_change"] < 0].sort_values(
        ["yoy_sales_change", "month"], ascending=[True, True]
    ).head(5)
    return {
        "monthly": _as_date(m, ["month"]),
        "top_positive_yoy": _as_date(positive.reset_index(drop=True), ["month"]),
        "top_negative_yoy": _as_date(negative.reset_index(drop=True), ["month"]),
    }


def _ntile_positions(n: int, k: int) -> np.ndarray:
    """Bucket number (1-based) for each 0-based position, matching SQL NTILE(k)."""
    q, r = divmod(n, k)
    idx = np.arange(n)
    big = r * (q + 1)
    return np.where(idx < big, idx // (q + 1) + 1, r + (idx - big) // q + 1)


def _ntile_by(frame: pd.DataFrame, columns: list[str], ascending: list[bool], k: int) -> pd.Series:
    ordered = frame.sort_values(columns, ascending=ascending, kind="mergesort")
    buckets = pd.Series(_ntile_positions(len(ordered), k), index=ordered.index)
    return buckets.reindex(frame.index).astype("int64")


def ref_ss12(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con).assign(disc_sales=lambda d: d["Discount"] * d["Sales"])
    od = (
        o.groupby("Order_ID", as_index=False)
        .agg(
            segment=("Segment", "first"),
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            disc_sales=("disc_sales", "sum"),
        )
    )
    od["weighted_discount"] = od["disc_sales"] / od["sales"]
    od["discount_decile"] = _ntile_by(od, ["weighted_discount", "Order_ID"], [False, True], 10)
    od["profit_decile"] = _ntile_by(od, ["profit", "Order_ID"], [True, True], 10)
    od["flag"] = (od["discount_decile"] == 1) & (od["profit_decile"] == 1)
    seg = (
        od.groupby("segment", as_index=False)
        .agg(
            orders=("Order_ID", "size"),
            sales=("sales", "sum"),
            profit=("profit", "sum"),
            avg_order_sales=("sales", "mean"),
            avg_order_profit=("profit", "mean"),
            avg_weighted_discount=("weighted_discount", "mean"),
            flagged_orders=("flag", "sum"),
        )
        .sort_values("segment")
        .reset_index(drop=True)
    )
    seg["margin"] = seg["profit"] / seg["sales"]
    seg["flagged_orders"] = seg["flagged_orders"].astype("int64")
    seg = seg[
        ["segment", "orders", "sales", "profit", "avg_order_sales", "avg_order_profit",
         "margin", "avg_weighted_discount", "flagged_orders"]
    ]
    flagged = od[od["flag"]].sort_values("Order_ID").reset_index(drop=True)
    flagged = flagged[
        ["Order_ID", "segment", "sales", "profit", "weighted_discount",
         "discount_decile", "profit_decile"]
    ]
    return {"segment_summary": seg, "flagged_orders": flagged}


def ref_ss14(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con).assign(yr=lambda d: d["Order_Date"].dt.year.astype("int64"))
    s = o.groupby(["Region", "Sub_Category", "yr"], as_index=False).agg(sales=("Sales", "sum"))
    s["share"] = s["sales"] / s.groupby(["Region", "yr"])["sales"].transform("sum")
    s = s.sort_values(["Region", "Sub_Category", "yr"]).reset_index(drop=True)
    grouped = s.groupby(["Region", "Sub_Category"])
    s["prior_yr"] = grouped["yr"].shift(1)
    s["prior_share"] = grouped["share"].shift(1)
    shifts = s[s["prior_yr"] == s["yr"] - 1].copy()
    shifts["share_shift"] = shifts["share"] - shifts["prior_share"]
    shifts = shifts[
        ["Region", "Sub_Category", "yr", "sales", "share", "prior_share", "share_shift"]
    ].reset_index(drop=True)
    up = shifts.sort_values(["Region", "share_shift", "Sub_Category", "yr"], ascending=[True, False, True, True])
    down = shifts.sort_values(["Region", "share_shift", "Sub_Category", "yr"], ascending=[True, True, True, True])
    gain = up.groupby("Region").head(1).assign(direction="largest_gain")
    loss = down.groupby("Region").head(1).assign(direction="largest_loss")
    extremes = pd.concat([gain, loss])[["Region", "direction", "Sub_Category", "yr", "share_shift"]]
    extremes = extremes.sort_values(["Region", "direction"]).reset_index(drop=True)
    return {"shifts": shifts, "extremes": extremes}


def ref_ss15(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    events = o[["Customer_ID", "Order_ID", "Order_Date"]].drop_duplicates()
    events = events.assign(order_date=events["Order_Date"].dt.normalize())
    events = events.sort_values(["Customer_ID", "order_date", "Order_ID"])
    events["seq"] = events.groupby("Customer_ID").cumcount() + 1
    lines = o[["Customer_ID", "Order_ID", "Category"]].merge(
        events[["Customer_ID", "Order_ID", "seq"]], on=["Customer_ID", "Order_ID"]
    )
    total = lines.groupby("Customer_ID")["Category"].nunique().rename("total_categories")
    first = lines[lines["seq"] == 1].groupby("Customer_ID")["Category"].nunique()
    later = lines[lines["seq"] > 1].groupby("Customer_ID")["Category"].nunique()
    out = pd.DataFrame({"total_categories": total})
    out["first_order_categories"] = first.reindex(out.index).fillna(0).astype("int64")
    out["later_order_categories"] = later.reindex(out.index).fillna(0).astype("int64")
    out["total_categories"] = out["total_categories"].astype("int64")
    result = out[out["total_categories"] >= 3].reset_index()
    result = result.sort_values("Customer_ID").reset_index(drop=True)
    return {"result": result}


def ref_ss16(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    sub = (
        o.groupby(["Category", "Sub_Category"], as_index=False)
        .agg(
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
            avg_discount=("Discount", "mean"),
            orders=("Order_ID", "nunique"),
        )
    )
    sub["margin"] = sub["profit"] / sub["sales"]
    cat = o.groupby("Category").agg(cp=("Profit", "sum"), cs=("Sales", "sum"))
    sub["category_margin"] = sub["Category"].map(cat["cp"] / cat["cs"])
    overall_margin = o["Profit"].sum() / o["Sales"].sum()
    median_sales = sub["sales"].median()
    res = sub[(sub["sales"] > median_sales) & (sub["margin"] < overall_margin)].copy()
    res["overall_margin"] = overall_margin
    res["median_sales"] = median_sales
    res = res.sort_values(["profit", "Sub_Category"]).reset_index(drop=True)
    res = res[
        ["Category", "Sub_Category", "sales", "profit", "margin", "avg_discount", "orders",
         "category_margin", "overall_margin", "median_sales"]
    ]
    return {"result": res}


def ref_ss17(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    o = o.assign(order_month=_month(o["Order_Date"]), ship_month=_month(o["Ship_Date"]))
    by_order = o.groupby("order_month").agg(sales_by_order_month=("Sales", "sum"))
    by_ship = o.groupby("ship_month").agg(sales_by_ship_month=("Sales", "sum"))
    monthly = by_order.join(by_ship, how="inner").reset_index()
    monthly = monthly.rename(columns={"order_month": "month"})
    monthly["difference"] = monthly["sales_by_order_month"] - monthly["sales_by_ship_month"]
    monthly = monthly.sort_values("month").reset_index(drop=True)
    months = set(monthly["month"])
    both = o[o["order_month"].isin(months) & o["ship_month"].isin(months)]
    summary = pd.DataFrame(
        {
            "total_lines": [len(both)],
            "lines_shipped_different_month": [int((both["order_month"] != both["ship_month"]).sum())],
            "total_orders": [both["Order_ID"].nunique()],
            "orders_shipped_different_month": [
                int(
                    both.loc[both["order_month"] != both["ship_month"], "Order_ID"].nunique()
                )
            ],
        }
    )
    return {"monthly": _as_date(monthly, ["month"]), "summary": summary}


def ref_ss18(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    o = _orders(con)
    orders = (
        o.groupby("Order_ID", as_index=False)
        .agg(
            region=("Region", "first"),
            segment=("Segment", "first"),
            order_date=("Order_Date", "min"),
            ship_date=("Ship_Date", "min"),
            sales=("Sales", "sum"),
            profit=("Profit", "sum"),
        )
    )
    om = orders["order_date"].dt.year * 12 + orders["order_date"].dt.month
    sm = orders["ship_date"].dt.year * 12 + orders["ship_date"].dt.month
    diff = sm - om
    orders["fulfilment_class"] = np.select(
        [diff < 0, diff == 0, diff == 1],
        ["ship_before_order", "same_month", "next_month"],
        default="later_month",
    )
    orders["delay_days"] = (
        orders["ship_date"].dt.normalize() - orders["order_date"].dt.normalize()
    ).dt.days
    orders["is_loss"] = orders["profit"] < 0
    res = (
        orders.groupby(["region", "segment", "fulfilment_class"], as_index=False)
        .agg(
            orders=("Order_ID", "size"),
            sales=("sales", "sum"),
            profit=("profit", "sum"),
            avg_order_sales=("sales", "mean"),
            avg_delay_days=("delay_days", "mean"),
            loss_order_rate=("is_loss", "mean"),
        )
        .sort_values(["region", "segment", "fulfilment_class"])
        .reset_index(drop=True)
    )
    return {"fulfilment_groups": res}


def _netflix_titles(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        "SELECT CAST(show_id AS BIGINT) AS show_id, type, duration_minutes, duration_seasons, "
        "date_added, CAST(release_year AS INTEGER) AS release_year FROM netflix_titles"
    ).df()


def ref_nf01(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    n = _netflix_titles(con)
    n = n[n["type"].isin(["Movie", "TV Show"])].copy()
    n["added"] = pd.to_datetime(n["date_added"], errors="coerce")
    added = n[n["added"].notna()].assign(added_year=lambda d: d["added"].dt.year.astype("int64"))
    counts = (
        added.groupby(["type", "added_year"])["show_id"].nunique().rename("titles_added").reset_index()
    )
    first, last = int(added["added_year"].min()), int(added["added_year"].max())
    years = np.arange(first, last + 1, dtype="int64")
    spine = pd.MultiIndex.from_product([sorted(added["type"].unique()), years], names=["type", "added_year"])
    filled = counts.set_index(["type", "added_year"]).reindex(spine, fill_value=0).reset_index()
    filled["titles_added"] = filled["titles_added"].astype("int64")
    g = filled.groupby("type")["titles_added"]
    filled["prior_year_titles"] = g.shift(1)
    filled["abs_change"] = filled["titles_added"] - filled["prior_year_titles"]
    prior = filled["prior_year_titles"]
    filled["pct_change"] = filled["abs_change"] / prior.where(prior != 0)
    has = filled["abs_change"].notna()
    filled["growth_rank"] = pd.Series(pd.NA, index=filled.index, dtype="Int64")
    filled["decline_rank"] = pd.Series(pd.NA, index=filled.index, dtype="Int64")
    for t, sub in filled[has].groupby("type"):
        filled.loc[sub.index, "growth_rank"] = sub["abs_change"].rank(ascending=False, method="min").astype("int64")
        filled.loc[sub.index, "decline_rank"] = sub["abs_change"].rank(ascending=True, method="min").astype("int64")
    out = filled.sort_values(["type", "added_year"]).reset_index(drop=True)
    return {"changes": out}


def ref_nf04(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    n = _netflix_titles(con)
    n = n[n["type"].isin(["Movie", "TV Show"])].copy()
    n["minutes"] = pd.to_numeric(n["duration_minutes"], errors="coerce")
    n["seasons"] = pd.to_numeric(n["duration_seasons"], errors="coerce")
    countries = con.execute("SELECT country, show_id FROM netflix_titles_countries").df()
    cats = con.execute("SELECT listed_in AS category, show_id FROM netflix_titles_category").df()
    pt = countries.merge(cats, on="show_id").merge(n[["show_id", "type", "minutes", "seasons"]], on="show_id")
    pt = pt.drop_duplicates(["country", "category", "show_id", "type", "minutes", "seasons"])
    pairs = (
        pt.groupby(["country", "category"], as_index=False)
        .agg(
            titles=("show_id", "nunique"),
            movies=("show_id", lambda s: s[pt.loc[s.index, "type"] == "Movie"].nunique()),
            tv_shows=("show_id", lambda s: s[pt.loc[s.index, "type"] == "TV Show"].nunique()),
        )
    )
    mv = pt[pt["type"] == "Movie"].groupby(["country", "category"])["minutes"].median().rename("median_movie_minutes")
    tv = pt[pt["type"] == "TV Show"].groupby(["country", "category"])["seasons"].mean().rename("avg_tv_seasons")
    pairs = pairs.set_index(["country", "category"]).join(mv).join(tv).reset_index()
    pairs = pairs.sort_values(["country", "category"]).reset_index(drop=True)
    base = pairs.groupby("category", as_index=False).agg(
        avg_pair_titles=("titles", "mean"), pair_count=("titles", "size")
    )
    movie_rows = pt[pt["type"] == "Movie"].drop_duplicates(["category", "show_id"])
    cat_med = movie_rows.groupby("category")["minutes"].median().rename("category_median_movie_minutes").reset_index()
    cb = base.merge(cat_med, on="category").sort_values("category").reset_index(drop=True)
    flagged = pairs.merge(base[["category", "avg_pair_titles"]], on="category").merge(cat_med, on="category")
    flagged = flagged[
        (flagged["titles"] > flagged["avg_pair_titles"])
        & (flagged["median_movie_minutes"] < flagged["category_median_movie_minutes"])
    ]
    flagged = flagged[
        ["country", "category", "titles", "median_movie_minutes", "avg_pair_titles",
         "category_median_movie_minutes"]
    ].sort_values(["country", "category"]).reset_index(drop=True)
    return {"pairs": pairs, "category_baselines": cb, "flagged_pairs": flagged}


def ref_nf05(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    n = _netflix_titles(con)
    n = n[n["type"].isin(["Movie", "TV Show"])]
    cast = con.execute('SELECT "cast" AS cast_member, show_id FROM netflix_titles_cast').df()
    cast = cast[cast["cast_member"].notna()]
    ct = cast.merge(n[["show_id", "type", "release_year"]], on="show_id").drop_duplicates()
    summary = (
        ct.groupby("cast_member", as_index=False)
        .agg(
            titles=("show_id", "nunique"),
            distinct_release_years=("release_year", "nunique"),
            movie_titles=("show_id", lambda s: s[ct.loc[s.index, "type"] == "Movie"].nunique()),
            tv_titles=("show_id", lambda s: s[ct.loc[s.index, "type"] == "TV Show"].nunique()),
            first_release_year=("release_year", "min"),
            latest_release_year=("release_year", "max"),
        )
    )
    summary["movie_rank"] = pd.Series(pd.NA, index=summary.index, dtype="Int64")
    summary["tv_rank"] = pd.Series(pd.NA, index=summary.index, dtype="Int64")
    mm = summary["movie_titles"] > 0
    summary.loc[mm, "movie_rank"] = summary.loc[mm, "movie_titles"].rank(ascending=False, method="min").astype("int64")
    tt = summary["tv_titles"] > 0
    summary.loc[tt, "tv_rank"] = summary.loc[tt, "tv_titles"].rank(ascending=False, method="min").astype("int64")
    summary = summary.sort_values("cast_member").reset_index(drop=True)
    per_year = (
        ct.groupby(["cast_member", "release_year"], as_index=False)
        .agg(appearances=("show_id", "nunique"))
    )
    prior = per_year.assign(release_year=per_year["release_year"] + 1).rename(
        columns={"appearances": "prior_year_appearances"}
    )
    changes = per_year.merge(prior, on=["cast_member", "release_year"])
    changes["appearance_change"] = changes["appearances"] - changes["prior_year_appearances"]
    changes = changes.sort_values(["cast_member", "release_year"]).reset_index(drop=True)
    return {"summary": summary, "consecutive_year_changes": changes}


REFERENCES = {
    "SS01": ref_ss01,
    "SS02": ref_ss02,
    "SS03": ref_ss03,
    "SS05": ref_ss05,
    "SS07": ref_ss07,
    "SS09": ref_ss09,
    "SS10": ref_ss10,
    "SS11": ref_ss11,
    "SS12": ref_ss12,
    "SS14": ref_ss14,
    "SS15": ref_ss15,
    "SS16": ref_ss16,
    "SS17": ref_ss17,
    "SS18": ref_ss18,
    "NF01": ref_nf01,
    "NF04": ref_nf04,
    "NF05": ref_nf05,
}
