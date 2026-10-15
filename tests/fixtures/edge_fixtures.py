"""Small deterministic DuckDB fixtures for edge-case tests.

Every table is hand-sized so expected results can be verified by hand. The expected
values live in EXPECTED and were worked out from the rows below, not taken from the
database's own output.
"""

import os

import duckdb

# order_id, customer_id, region, product_id, sales, quantity, profit, order_date
# Edge cases: a duplicate line (O2 twice), a NULL profit (O3), a zero-sales order (O4),
# and a tie on sales and date (O5/O6).
ORDERS_ROWS = [
    ("O1", "C1", "West", "P1", 100.0, 2, 20.0, "2024-01-05"),
    ("O2", "C1", "West", "P2", 50.0, 1, 10.0, "2024-01-20"),
    ("O2", "C1", "West", "P2", 50.0, 1, 10.0, "2024-01-20"),
    ("O3", "C2", "East", "P1", 200.0, 4, None, "2024-02-03"),
    ("O4", "C3", "East", "P3", 0.0, 0, 0.0, "2024-02-10"),
    ("O5", "C4", "South", "P2", 80.0, 2, 16.0, "2024-03-01"),
    ("O6", "C5", "South", "P2", 80.0, 2, 16.0, "2024-03-01"),
]

# C6 has no orders, which gives an anti-join case.
CUSTOMERS_ROWS = [
    ("C1", "Ann"),
    ("C2", "Ben"),
    ("C3", "Cara"),
    ("C4", "Dev"),
    ("C5", "Eli"),
    ("C6", "Fay"),
]

# C2 has one purchase only (no second order). C4 has a gap of 61 days.
PURCHASES_ROWS = [
    ("C1", "2024-01-05"),
    ("C1", "2024-01-20"),
    ("C2", "2024-02-03"),
    ("C4", "2024-03-01"),
    ("C4", "2024-05-01"),
]

# A stored monthly summary with a missing period: 2024-02 has no row.
MONTHLY_ROWS = [
    ("2024-01", 200.0),
    ("2024-03", 160.0),
    ("2024-04", 40.0),
]

EXPECTED = {
    # Duplicate line O2 counts twice in a line-level sum: 100 + 50 + 50 = 200.
    "region_sales": {"West": 200.0, "East": 200.0, "South": 160.0},
    # One value per order: O2's two identical lines collapse to a single 50.0.
    "order_sales": {"O1": 100.0, "O2": 50.0, "O3": 200.0, "O4": 0.0, "O5": 80.0, "O6": 80.0},
    # sum(profit) ignores the NULL in O3 and includes the 0.0 in O4.
    "east_profit_sum": 0.0,
    # count(*) includes the NULL-profit row: O3 and O4.
    "east_row_count": 2,
    # Ties on sales=80.0 both rank 1 within South.
    "south_rank_of_80": (1, 1),
    # Zero-sales order: profit/sales is undefined and must not be computed.
    "zero_sales_orders": ["O4"],
    # Customers with exactly one purchase.
    "single_purchase_customers": ["C2"],
    # C4's second purchase is 2024-03-01 to 2024-05-01: 31 + 30 = 61 days.
    "c4_gap_days": 61,
    # 2024-02 is absent from the monthly summary.
    "missing_months": ["2024-02"],
    # Customers with no orders (anti-join).
    "customers_without_orders": ["C6"],
}


def build_fixture(path):
    """Create the edge-case database at path, overwriting any existing file."""
    if os.path.exists(path):
        os.remove(path)
    connection = duckdb.connect(path)
    try:
        connection.execute(
            "CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, region VARCHAR, "
            "product_id VARCHAR, sales DOUBLE, quantity BIGINT, profit DOUBLE, order_date DATE)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ORDERS_ROWS
        )
        connection.execute("CREATE TABLE customers (customer_id VARCHAR, name VARCHAR)")
        connection.executemany("INSERT INTO customers VALUES (?, ?)", CUSTOMERS_ROWS)
        connection.execute("CREATE TABLE purchases (customer_id VARCHAR, purchase_date DATE)")
        connection.executemany("INSERT INTO purchases VALUES (?, ?)", PURCHASES_ROWS)
        connection.execute("CREATE TABLE monthly_sales (month VARCHAR, sales DOUBLE)")
        connection.executemany("INSERT INTO monthly_sales VALUES (?, ?)", MONTHLY_ROWS)
    finally:
        connection.close()
    return path

