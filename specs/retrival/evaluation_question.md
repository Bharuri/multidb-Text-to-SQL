
# SQL Capability Benchmark — 30 Complex Natural-Language Questions

Version: 2.0  
Purpose: test reusable Text-to-SQL capabilities, not memorization.  
Dataset rule: every question is scoped to one selected database. Never join different workbook databases in one query.

## Evaluation instructions

- The benchmark contains 30 questions: 18 Sample - Superstore, 3 113th Congress, 4 Airbnb NYC, and 5 Netflix.
- Tags describe the SQL capabilities needed; they are not implementation instructions.
- A question is only answerable if the source fields and definitions support it. If the workbook does not contain returns, transaction-level purchase history, vote-by-vote records, or a required key, the system must identify the gap instead of inventing data.
- For each question, document output grain, expected columns, ordering, assumptions, and an independent oracle.
- Do not hard-code SQL by question ID or exact wording.
- For questions involving “growth,” “margin,” “active,” “popular,” or “outlier,” state the operational definition in metadata or request clarification.

## A. Sample - Superstore (18 questions)

Assume the usual Sample - Superstore order-line dataset, but verify actual headers and grain during ingestion. Candidate fields often include Order ID, Order Date, Ship Date, Customer ID, Customer Name, Segment, Region, State, Category, Sub-Category, Product ID, Sales, Quantity, Discount, and Profit. Use exact physical metadata names.

### SS01 — Monthly sales and profit with prior-month comparison

For each sub-category and calendar month, calculate total sales, total profit, distinct orders, and distinct customers. Include the previous calendar month's sales, absolute sales change, and percentage change. Return only months with a prior observed month, and flag months where sales fell by more than 15%.

**Tags:** CTEs, aggregation, date truncation, `LAG`, safe division, `CASE`, multi-metric output.  
**Caveat:** define whether missing months should be omitted or generated; default to observed months only.

### SS02 — Top three sub-categories per region with tie-aware ranking

For each region, rank sub-categories by total profit and return the top three ranks, including ties. Show sales, profit, profit margin defined as `SUM(Profit)/SUM(Sales)`, order count, and rank.

**Tags:** CTEs, grouped aggregation, window `RANK`/`DENSE_RANK`, ratio of aggregates, top-N per group.

### SS03 — Customer first order, second order, and elapsed days

For every customer with at least one order, identify first and second order dates, calculate days between them where a second order exists, report the average gap among returning customers, and count customers with no second order.

**Tags:** multi-stage CTEs, distinct order grain, `ROW_NUMBER` or order-level deduplication, conditional aggregation, date difference, null handling.  
**Caveat:** confirm order dates and order IDs represent order events; do not count line items as separate purchases.

### SS04 — Customer cohort retention by first-order month

Assign each customer to the month of their first order. For each cohort month and months-since-first-order, calculate cohort customer count, active customer count, and retention percentage.

**Tags:** CTEs, customer-level deduplication, date arithmetic, cross-period aggregation, safe division.  
**Caveat:** the dataset's observation window limits retention interpretation.

### SS05 — Pareto contribution of products within each category

For each category, rank products by sales descending and calculate each product's sales share and cumulative sales share. Return the smallest ranked product set whose cumulative share reaches at least 80%, including the row that crosses the threshold.

**Tags:** CTEs, aggregate, window cumulative sum, window total, safe division, threshold crossing.  
**Caveat:** define tie handling and cumulative order deterministically.

### SS06 — Discount bands and profitability

Create configurable discount bands (zero, low, medium, high, and full discount, with boundaries defined in metadata). For each category and band, calculate line count, order count, sales, profit, average profit per line, and aggregate profit margin. Identify bands with negative total profit.

**Tags:** `CASE`, conditional grouping, multiple aggregations, `HAVING`, semantic configuration.  
**Caveat:** band boundaries must be explicitly configured, not invented by SQL Generator.

### SS07 — Shipping delay and profitability

Calculate days between order and ship dates for each order line, then summarize average/median shipping delay, sales, and profit by ship mode and region. Rank ship modes within each region by median delay and flag modes whose average delay is above the region-wide average.

**Tags:** date difference, percentile/median, CTEs, window ranking, comparison to grouped benchmark.  
**Caveat:** median function must be enabled and tested for DuckDB.

### SS08 — State-level performance against regional baseline

For each state, calculate sales, profit, order count, customer count, and profit margin. Compare each state's profit margin with its region's aggregate profit margin and return states with positive sales but negative profit or a margin at least five percentage points below the regional margin.

**Tags:** CTEs, aggregate at multiple grains, join/CTE comparison, `CASE`, `OR` predicates.

### SS09 — Products with sales but persistent loss

Identify products with positive total sales and negative total profit across the entire dataset. Show their sales, profit, margin, order count, and number of distinct customers. Rank the products by total loss and show their contribution to total negative product profit.

**Tags:** aggregation, `HAVING`, CTEs, negative-only conditional aggregation, ranking, safe division.

### SS10 — Repeat-customer revenue concentration

For each customer, calculate distinct orders, total sales, first/last order date, and sales share. Classify customers as one-order or repeat-order based on distinct Order ID. Compare each class's customer count, sales, average customer sales, and share of total sales.

**Tags:** customer-level CTE, conditional classification, aggregation over aggregation, distinct counts, ratios.

### SS11 — Month-over-month and year-over-year comparison

For every month in every year, calculate sales and profit, prior month values, same-month-prior-year values, absolute changes, and percentage changes. Return the five largest positive and five largest negative year-over-year sales changes.

**Tags:** calendar grain, `LAG`, multiple window partitions/orderings, CTEs, safe division, top/bottom ranking.  
**Caveat:** same-month-prior-year comparisons should use a year/month key, not simply the prior observed row if periods are missing.

### SS12 — Order-level discount exposure and profitability

Aggregate line items to one row per order, calculating order sales, profit, quantity, and weighted discount exposure (define formula explicitly, e.g. sales-weighted discount when sales are nonzero). Then compare order-level metrics by segment and identify orders in the top decile of discount exposure and bottom decile of profit.

**Tags:** two-stage aggregation, derived expression, `NTILE` or percentile, CTEs, joins back only if needed.  
**Caveat:** discount exposure formula must be documented.

### SS13 — Customer purchase intervals and irregularity

Build one record per customer order, sequence orders by date, calculate days since previous order, and report each customer's median interval, longest interval, and interval variability. Rank customers by variability among customers with at least three orders.

**Tags:** deduplication, `LAG`, date difference, median, standard deviation/variance, `HAVING`, ranking.  
**Caveat:** only order events observed in the dataset can be sequenced.

### SS14 — Sub-category mix shift by year

For each year and region, calculate sub-category sales and each sub-category's percentage of regional sales. Compare the share with the previous year and identify the largest positive and negative mix shifts per region.

**Tags:** multi-grain aggregation, window total, `LAG`, safe division, top-N per group.

### SS15 — Customers buying across categories

Identify customers who purchased from at least three distinct categories. For each, report category count, distinct orders, total sales/profit, first/last order date, and the number of categories purchased in their first versus later orders.

**Tags:** multi-stage CTEs, distinct aggregation, conditional aggregation, date/order sequencing, anti-fan-out discipline.  
**Caveat:** if “first order” contains multiple line items/categories, define order-level treatment.

### SS16 — High sales but low profit anomaly detection

At sub-category grain, compute sales, profit, margin, average discount, and order count. Compare each sub-category's margin with the overall margin and its category average; return sub-categories with sales above the dataset median and margin below the overall margin, ordered by profit impact.

**Tags:** percentile, nested aggregate/CTE, comparison to global and category baselines, safe division.

### SS17 — Ship-date versus order-date seasonal analysis

Compare monthly sales by order month with monthly sales grouped by ship month. Show both time series and the difference between the two monthly totals for months represented in both date roles. Also report the number of lines/orders shipped in a different month from order placement.

**Tags:** date truncation, multiple date roles, CTEs, set/joins on month keys, conditional aggregation.  
**Caveat:** sales are line values attributed to two different time axes, not two separate sales amounts.

### SS18 — Conditional order fulfillment profile

Classify each order as same-month shipment, next-month shipment, or later-month shipment using order and ship dates. At order grain, calculate sales/profit and compare order counts, average order value, average delay, and loss-making order rate by region and segment.

**Tags:** order-level pre-aggregation, `CASE`, date arithmetic, conditional metrics, multi-stage aggregation, safe division.

## B. 113th Congress (3 questions)

The workbook has `Bills` and `Sunlight Info` sheets. Profile the actual row grain and candidate keys before attempting any cross-sheet join. Do not assume one row equals one bill or one vote.

### CO01 — Vote outcome patterns by party and session

Using the available vote-related rows, summarize counts and percentages of Yes, No, and Abstain by party, session, and body. Compare the observed vote outcome with `Vote Result` where meaningful.

**Tags:** conditional aggregation, percentages, multi-dimensional grouping, null handling.  
**Caveat:** confirm what each row represents and whether `Vote`, `Party`, `Count`, and total fields are individual/member-level or pre-aggregated.

### CO02 — Bill passage and majority margin

At the validated bill/vote grain, calculate yes/no/abstain totals, yes-vote share of counted votes, required majority, margin above/below majority, and outcome classification. Rank bills by closest vote margin within session and body.

**Tags:** derived metrics, `CASE`, safe division, ranking, CTEs.  
**Caveat:** use only fields whose meanings and units are verified; do not recompute totals from fields that are already aggregated without validating grain.

### CO03 — Sponsor and committee summary

If the source sheets can be joined on a verified key, summarize bills by sponsor party, sponsor state, and committee: number of distinct bills, vote outcomes, and average/median pages where available. Rank committees within sponsor party by bill count.

**Tags:** validated join, distinct aggregation, multi-stage grouping, median, window ranking.  
**Caveat:** if no stable key or suitable cardinality can be established between `Bills` and `Sunlight Info`, return a clear cannot-answer/metadata-gap outcome rather than guessing a join.

## C. Airbnb NYC (4 questions)

The workbook's `AirBnB_NYC` sheet includes host/listing fields, room type, neighbourhood, price, beds, review counts, and ratings. Confirm whether each row is a unique listing or an aggregated record before using `Number of Records`.

### AB01 — Host portfolio concentration

For each host, calculate listing count, neighbourhood count, average and median price, total reviews, and average rating where available. Rank hosts within each neighbourhood by listing count and report the top five ranks, including ties.

**Tags:** aggregation, multiple grains, joins/CTEs if needed, median, window ranking.  
**Caveat:** one host may have listings in multiple neighbourhoods; ranking must be at host-neighbourhood grain.

### AB02 — Price distribution and outliers

For each neighbourhood and room type, calculate listing count, median price, 25th and 75th percentile price, interquartile range, and number/share of listings above 1.5 times the third quartile. Exclude invalid/non-positive prices using a documented rule.

**Tags:** percentile functions, CTEs, conditional aggregation, safe division, filtering.  
**Caveat:** exact percentile function and interpolation semantics must be documented.

### AB03 — Host tenure and review performance

Group hosts by tenure bands calculated relative to a configured as-of date. For each tenure band and room type, calculate listing count, average price, total reviews, review rate proxy (reviews per listing; do not call it a time-based rate), and average rating. Compare each band with the room-type baseline.

**Tags:** date arithmetic, `CASE`, multi-stage aggregation, baseline comparison.  
**Caveat:** use an explicit as-of date; do not depend on the machine's current date for repeatable benchmark results.

### AB04 — Price versus review rating relationship

At neighbourhood and room-type grain, calculate average/median price and rating, review count, and the difference from each neighbourhood's overall room-type baseline. Rank groups by rating-adjusted price only if a formula is explicitly defined; otherwise return the component metrics and ask for clarification.

**Tags:** multi-stage aggregation, medians, baseline comparison, semantic ambiguity handling.

## D. Netflix Titles (5 questions)

The workbook contains `netflix_titles` and related director, country, cast, and category sheets keyed by `show_id`. One title may have multiple values in a relationship sheet. Pre-aggregate or use `COUNT(DISTINCT show_id)` to avoid join fan-out.

### NF01 — Catalogue growth and year-over-year change

By year of `date_added`, calculate new title count split by type, prior-year count, absolute change, and percentage change. Rank years by largest growth and decline. Distinguish date-added year from release year.

**Tags:** date extraction, conditional aggregation, `LAG`, safe division, ranking.

### NF02 — Genre/category growth and catalogue mix

For each year and category, calculate distinct title count, share of all titles added that year, prior-year count, and change in share. Return categories with the largest mix increase and decrease per year.

**Tags:** one-to-many joins, distinct counts, multi-stage aggregation, `LAG`, safe division.  
**Caveat:** category sheet grain and duplicate show/category pairs must be profiled.

### NF03 — Director portfolio analysis

For each director, calculate distinct titles, type breakdown, earliest/latest release year, median release year if meaningful, and the share of titles rated above a configured rating group. Rank directors within title type by title count, including ties.

**Tags:** join, distinct aggregation, conditional aggregation, median, window ranking.  
**Caveat:** director multi-value cells must be normalized through the director sheet when that is the intended source.

### NF04 — Country and category collaboration

For each country and category, calculate distinct titles, movie/TV split, median movie duration, and average TV seasons. Compare each country-category pair with the global category baseline and return pairs with above-baseline title counts but below-baseline movie duration.

**Tags:** multiple one-to-many relationships, pre-aggregation, `COUNT DISTINCT`, conditional aggregates, median, CTEs.  
**Caveat:** joining country, category, cast, and director sheets simultaneously can multiply rows. Do not aggregate after an uncontrolled fan-out.

### NF05 — Cast breadth and repeat appearances

For each cast member, calculate distinct titles, distinct release years, movie/TV breakdown, and first/latest release year. Rank cast members within movie/TV type by title count and show the change in appearance count across consecutive release years when the member appears in both years.

**Tags:** join, deduplication, CTEs, `LAG`, date/year sequencing, window ranking.  
**Caveat:** define title attribution when a title has multiple cast members and avoid counting duplicate cast/title pairs.

## Capability coverage checklist

The benchmark collectively targets:
- Basic filters, nulls, multiple dimensions and metrics.
- CTEs and dependent CTEs.
- Nested/subqueries and semi-/anti-join logic where appropriate.
- `CASE`, conditional aggregation, `HAVING`.
- Window ranking, tie handling, `LAG`, cumulative windows, `NTILE`.
- Date truncation, date differences, period comparisons, cohort calculations.
- Percentiles/median, ratios, safe division.
- Multiple join edges, one-to-many fan-out prevention, pre-aggregation.
- Ambiguity and missing-data rejection.
- Parameterized values, allowlisted tables/columns, safe SQL AST validation.
