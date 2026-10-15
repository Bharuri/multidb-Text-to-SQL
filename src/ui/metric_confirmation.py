"""Streamlit panel that proposes, confirms, or rejects a metric definition.

Only an accepted proposal is written to the database's business file. A rejected proposal is
discarded for the session. This panel does not depend on the query pipeline.
"""

from __future__ import annotations

from typing import Any

from src.query import metrics as mx
from src.query.catalog import Catalog
from src.query.config import DatabaseEntry


def _base_key(entry: DatabaseEntry, term: str) -> str:
    return f"metric::{entry.key}::{term.strip().lower()}"


def _propose(st: Any, key: str, catalog: Catalog, term: str, question: str, proposer: mx.Proposer) -> None:
    st.session_state.pop(f"{key}::rejected", None)
    try:
        st.session_state[f"{key}::proposal"] = mx.propose_metric(catalog, term, question, proposer)
        st.session_state.pop(f"{key}::message", None)
    except mx.MetricError as exc:
        st.session_state[f"{key}::message"] = ("error", f"No proposal shown: {exc}")


def _accept(st: Any, key: str, catalog: Catalog, entry: DatabaseEntry) -> None:
    pending = st.session_state.get(f"{key}::proposal")
    if pending is None:
        return
    try:
        saved = mx.accept_proposal(catalog, entry, pending)
    except mx.MetricError as exc:
        st.session_state[f"{key}::message"] = ("error", f"Not saved: {exc}")
        return
    st.session_state.pop(f"{key}::proposal", None)
    st.session_state[f"{key}::message"] = ("success", f"Saved to {saved}.")


def _reject(st: Any, key: str) -> None:
    st.session_state.pop(f"{key}::proposal", None)
    st.session_state[f"{key}::rejected"] = True
    st.session_state[f"{key}::message"] = ("info", "Rejected. Nothing was saved.")


def render_metric_confirmation(
    st: Any,
    catalog: Catalog,
    entry: DatabaseEntry,
    term: str,
    question: str,
    proposer: mx.Proposer,
) -> None:
    resolution = mx.resolve_term(catalog, term)
    if resolution.outcome == mx.APPROVED:
        st.caption(f"Metric '{term}' is approved ({resolution.metric.name}).")
        return

    key = _base_key(entry, term)
    if resolution.outcome == mx.UNDEFINED:
        st.button(
            "Propose a definition",
            key=f"{key}::propose",
            on_click=_propose,
            args=(st, key, catalog, term, question, proposer),
        )

    pending = st.session_state.get(f"{key}::proposal")
    if pending is None and resolution.outcome == mx.PROPOSED and not st.session_state.get(f"{key}::rejected"):
        pending = resolution.metric.raw

    message = st.session_state.get(f"{key}::message")
    if message:
        getattr(st, message[0])(message[1])

    if pending is None:
        if resolution.outcome == mx.UNDEFINED:
            st.caption(f"'{term}' has no metric definition yet.")
        return

    st.warning(f"Proposed definition for '{term}'. It is not used in SQL until you accept it.")
    st.json(pending)
    accept_col, reject_col = st.columns(2)
    accept_col.button("Accept", key=f"{key}::accept", on_click=_accept, args=(st, key, catalog, entry))
    reject_col.button("Reject", key=f"{key}::reject", on_click=_reject, args=(st, key))
