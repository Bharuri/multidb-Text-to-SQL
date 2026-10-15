"""Streamlit front end. Each pipeline stage gets a panel that fills in as the stage finishes.

The planner and SQL generator replies stream into their stage panels. The UI holds no planning or
SQL rules: it renders pipeline results and hands undefined business terms to the metric panel.
Credentials come from the AWS profile chain (for example AWS_PROFILE) unless AWS keys are entered in the
sidebar. Entered keys are held in this browser session only and are never logged or displayed.
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Streamlit runs this file as a script, so the project root must be importable for `src`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.query.catalog import load_catalog
from src.query.config import Registry, load_registry
from src.query.llm import Credentials, LlmClient, LlmError, make_client, profile_status
from src.query.metrics import Proposer, bedrock_proposer
from src.query.observability import Response, StageResult
from src.query.pipeline import STAGES, run_pipeline
from src.query.sql_generator import build_sql_generator
from src.ui.metric_confirmation import render_metric_confirmation

LAST_RUN_KEY = "last_run"
MAX_STREAM_CHARS = 4000
STATUS_LABELS = {
    "needs_clarification": "Needs clarification",
    "cannot_answer": "Cannot answer",
    "rejected": "Rejected",
    "error": "Error",
}


class _StreamingClient:
    """Forwards complete() to the real client and mirrors the reply into a bounded placeholder."""

    def __init__(self, inner: LlmClient, placeholder: Any):
        self._inner = inner
        self._placeholder = placeholder

    def complete(self, prompt: str, on_chunk: Callable[[str], None] | None = None) -> str:
        buffer = ""

        def show(chunk: str) -> None:
            nonlocal buffer
            buffer = (buffer + chunk)[-MAX_STREAM_CHARS:]
            self._placeholder.code(buffer, language="text")

        return self._inner.complete(prompt, on_chunk=show)


@st.cache_resource(show_spinner=False)
def _registry() -> Registry:
    return load_registry()


def credentials_from_form(access_key_id: str, secret_access_key: str, session_token: str) -> Credentials | None:
    """Both keys are needed to use the form. Blank or incomplete input falls back to the profile chain."""
    key, secret, token = access_key_id.strip(), secret_access_key.strip(), session_token.strip()
    if not (key and secret):
        return None
    return Credentials(access_key_id=key, secret_access_key=secret, session_token=token or None)


def _sidebar_credentials() -> Credentials | None:
    with st.sidebar:
        st.header("AWS keys")
        access_key_id = st.text_input("AWS access key ID", key="aws_access_key_id")
        secret_access_key = st.text_input("AWS secret access key", type="password", key="aws_secret_access_key")
        session_token = st.text_input("AWS session token (optional)", type="password", key="aws_session_token")
        credentials = credentials_from_form(access_key_id, secret_access_key, session_token)
        if (access_key_id.strip() or secret_access_key.strip()) and credentials is None:
            st.warning("Enter both the access key ID and the secret access key. Until then, the profile chain is used.")
        if credentials is None:
            status = profile_status()
            if status:
                st.caption(f"{status} The secret stays on the server and is not shown here.")
            else:
                st.caption("No AWS profile found. Enter keys here, or set AWS_PROFILE.")
        else:
            st.caption("Using the keys entered here. Kept in this session only.")
    return credentials


def _stage_label(number: int, result: StageResult) -> str:
    return f"{number}. {result.stage} · {result.status} · {result.duration_ms:.0f} ms"


DETAIL_LABELS = {
    "question": "Question",
    "database": "Database",
    "database_file": "Database file",
    "tables": "Catalog tables",
    "metrics": "Metrics",
    "retrieved_context": "Retrieved context",
    "planner_prompt": "Planner prompt",
    "first_reply": "First planner reply (rejected, then repaired)",
    "repair_reason": "Why the planner was asked to repair",
    "planner_reply": "Planner reply",
    "analysis": "Analysis",
    "clarification": "Clarification",
    "sql_prompt": "SQL generator prompt",
    "generator_reply": "SQL generator reply",
    "first_sql": "First generated SQL (rejected, then repaired)",
    "sql": "SQL",
    "params": "Parameters",
    "ast_summary": "SQL AST summary",
    "preview_rows": "Result preview",
}
# Shown inside the preview table instead of as their own blocks.
_PREVIEW_ONLY_KEYS = frozenset({"columns", "preview_row_limit"})


def _render_detail(detail: dict[str, Any]) -> None:
    for key, value in detail.items():
        if key in _PREVIEW_ONLY_KEYS:
            continue
        if key == "preview_rows":
            st.caption(DETAIL_LABELS[key])
            st.dataframe(pd.DataFrame(value, columns=detail.get("columns", [])))
            st.caption(f"Up to the first {detail.get('preview_row_limit', len(value))} rows.")
            continue
        st.caption(DETAIL_LABELS.get(key, key))
        if key == "sql":
            st.code(value, language="sql")
        elif isinstance(value, str):
            st.code(value, language="text")
        else:
            st.json(value)


def _stage_panel(container: Any, number: int, result: StageResult) -> None:
    info = result.to_dict()
    with container.expander(_stage_label(number, result), expanded=True):
        if info["reason"]:
            st.caption(info["reason"])
        if info["output"]:
            st.json(info["output"])
        if result.detail:
            _render_detail(result.detail)


def _render_stage(slot: Any, number: int, result: StageResult) -> None:
    _stage_panel(slot.container(), number, result)


def _run_live(registry: Registry, database: str, question: str, credentials: Credentials | None) -> dict[str, Any]:
    """Run the pipeline, filling one panel per stage as each stage finishes."""
    slots: dict[str, Any] = {}
    for number, name in enumerate(STAGES, 1):
        slots[name] = st.empty()
        slots[name].caption(f"{number}. {name} · pending")

    def on_stage(result: StageResult) -> None:
        _render_stage(slots[result.stage], STAGES.index(result.stage) + 1, result)

    planner = _stream_client(
        lambda: make_client(registry.planner_llm, credentials=credentials), slots["planner_llm"]
    )
    sql_generator = _stream_client(
        lambda: build_sql_generator(registry, credentials=credentials), slots["sql_generator_llm"]
    )
    response = run_pipeline(
        question,
        database,
        registry=registry,
        planner=planner,
        sql_generator=sql_generator,
        on_stage=on_stage,
    )
    return {"database": database, "question": question, "response": response}


def _stream_client(factory: Callable[[], LlmClient], placeholder: Any) -> _StreamingClient | None:
    """Return None when the client cannot be built, so the pipeline reports the llm_error stage itself."""
    try:
        return _StreamingClient(factory(), placeholder)
    except LlmError:
        return None


def _render_trace(trace: tuple[StageResult, ...]) -> None:
    for number, result in enumerate(trace, 1):
        _stage_panel(st.container(), number, result)


def _missing_terms(response: Response) -> list[str]:
    for result in response.trace:
        if result.stage == "parse_analysis":
            return list(dict.fromkeys(str(term) for term in result.output.get("missing", [])))
    return []


def _proposer(registry: Registry, credentials: Credentials | None) -> Proposer | None:
    try:
        return bedrock_proposer(registry.planner_llm, credentials=credentials)
    except Exception as exc:  # noqa: BLE001 - AWS profile or region problems; show the type only
        st.error(f"Metric proposals are unavailable ({type(exc).__name__}).")
        return None


def _render_clarification(registry: Registry, record: dict[str, Any], credentials: Credentials | None) -> None:
    response: Response = record["response"]
    terms = _missing_terms(response)
    st.info("This question uses business terms that have no approved metric definition yet.")
    if not terms:
        st.warning(response.reasons[0] if response.reasons else "The question needs clarification.")
        return
    proposer = _proposer(registry, credentials)
    try:
        catalog = load_catalog(registry, record["database"])
    except Exception as exc:  # noqa: BLE001 - metadata problems; show the type only
        st.error(f"Metric definitions could not be loaded ({type(exc).__name__}).")
        return
    entry = registry.databases[record["database"]]
    for term in terms:
        if proposer is None:
            st.caption(f"'{term}' has no metric definition yet.")
            continue
        render_metric_confirmation(st, catalog, entry, term, record["question"], proposer)


def _render_final_sql(response: Response) -> None:
    if not response.sql:
        return
    with st.expander("Final SQL", expanded=False):
        st.code(response.sql, language="sql")
        if response.params:
            st.caption(f"Parameters: {list(response.params)}")


def _render_outcome(registry: Registry, record: dict[str, Any], credentials: Credentials | None) -> None:
    response: Response = record["response"]
    _render_final_sql(response)
    if response.status == "ok":
        result = response.result
        rows = result["rows"]
        st.success(f"{result['row_count']} row(s) returned from '{record['database']}'.")
        st.dataframe(pd.DataFrame(rows, columns=result["columns"]))
        if result["truncated"]:
            st.caption(f"Showing the first {len(rows)} rows. More rows matched the query.")
        return
    if response.status == "needs_clarification":
        _render_clarification(registry, record, credentials)
        return
    label = STATUS_LABELS.get(response.status, response.status)
    reason = response.reasons[0] if response.reasons else "The question could not be answered."
    st.error(f"{label}: {reason}")


def _render_step_plan(record: dict[str, Any] | None) -> None:
    if record is None:
        st.info("Run a question on the Ask tab to see its steps here.")
        return
    response: Response = record["response"]
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "stage": r.stage,
                    "status": r.status,
                    "duration_ms": round(r.duration_ms, 1),
                    "category": r.category or "",
                    "reason": r.to_dict()["reason"] or "",
                }
                for r in response.trace
            ]
        )
    )
    if response.sql:
        st.code(response.sql, language="sql")
        if response.params:
            st.caption(f"Parameters: {list(response.params)}")


def main() -> None:
    st.set_page_config(page_title="MultiDB", layout="wide")
    st.title("MultiDB: ask a question")
    try:
        registry = _registry()
    except Exception as exc:  # noqa: BLE001 - configuration problems; show the type only
        st.error(f"Configuration could not be loaded ({type(exc).__name__}).")
        st.stop()

    tab_ask, tab_plan, tab_sql = st.tabs(["Ask", "Steps", "Run SQL"])
    credentials = _sidebar_credentials()
    with tab_ask:
        database = st.selectbox("Database", options=list(registry.databases), key="database")
        question = st.text_area("Question", key="question", height=100)
        ask_clicked = st.button("Ask", type="primary")

        record = st.session_state.get(LAST_RUN_KEY)
        if ask_clicked:
            if question.strip():
                with tab_plan:
                    record = _run_live(registry, database, question.strip(), credentials)
                st.session_state[LAST_RUN_KEY] = record
            else:
                st.warning("Enter a question first.")
        elif record is not None:
            with tab_plan:
                _render_trace(record["response"].trace)

        if record is not None:
            _render_outcome(registry, record, credentials)

    with tab_plan:
        _render_step_plan(st.session_state.get(LAST_RUN_KEY))

    with tab_sql:
        st.info("Manual SQL is not part of this build yet. This tab is a placeholder.")


main()
