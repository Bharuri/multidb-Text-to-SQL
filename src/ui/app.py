"""Streamlit front end. One panel per stage, filled in as each stage finishes. The LLM reply streams into stage 4."""

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Streamlit runs this file as a script, so the project root must be importable for `src`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.query.catalog import resolve  # noqa: E402
from src.query.executor import ExecuteError, run_manual_sql  # noqa: E402
from src.query.pipeline import (  # noqa: E402
    STAGE_NAMES,
    Credentials,
    Request,
    StageResult,
    _default_row_limit,
    database_names,
    final_status,
    run,
)

TRACE = "trace"


def credentials_from_form(access_key_id: str, secret_access_key: str, session_token: str) -> Credentials | None:
    """No keys means the default AWS credential chain is used."""
    key, secret, token = access_key_id.strip(), secret_access_key.strip(), session_token.strip()
    if not (key or secret or token):
        return None
    return Credentials(access_key_id=key, secret_access_key=secret, session_token=token or None)


def stage_number(name: str) -> int:
    return STAGE_NAMES.index(name) + 1


def render_waiting(slot, name: str) -> None:
    slot.caption(f"{stage_number(name)}. {name}: waiting")


def render_stage(slot, result: StageResult) -> None:
    number = stage_number(result.stage)
    with slot.container():
        with st.expander(f"{number}. {result.stage}: {result.status}", expanded=result.status != "skipped"):
            if result.status == "skipped":
                st.caption("Skipped because an earlier stage failed.")
                return
            if result.reason:
                st.error(result.reason)
            st.json(result.output)


def ok_output(trace: list[StageResult], name: str) -> dict | None:
    return next((r.output for r in trace if r.stage == name and r.status == "ok"), None)


def render_answer(slot, trace: list[StageResult]) -> None:
    """Final SQL and result, shown under the Ask button. Stage panels live in the Step plan tab."""
    with slot.container():
        generated = ok_output(trace, "sql_generate")
        if generated:
            st.subheader("SQL")
            st.code(generated["sql"], language="sql")
            if generated["params"]:
                st.caption(f"Parameters: {generated['params']}")
        executed = ok_output(trace, "execute")
        if executed is not None:
            st.subheader("Result")
            if "value" in executed:
                st.metric(executed["columns"][0], str(executed["value"]))
            elif executed["rows"]:
                st.dataframe([dict(zip(executed["columns"], row)) for row in executed["rows"]], hide_index=True)
            else:
                st.info("The query returned no rows.")
            if executed["truncated"]:
                st.caption(f"Showing the first {executed['row_count']} rows. The query matched more.")
            return
        failed = next((r for r in trace if r.status == "failed"), None)
        if failed:
            st.error(f"{failed.stage}: {failed.reason}")


st.set_page_config(page_title="Text-to-SQL", layout="wide")
st.title("Ask your data")

with st.sidebar:
    database = st.selectbox("Database", database_names())
    st.header("AWS keys")
    st.caption("Used for this request only. Never logged or shown in the trace.")
    access_key_id = st.text_input("AWS access key ID", key="aws_access_key_id")
    secret_access_key = st.text_input("AWS secret access key", type="password", key="aws_secret_access_key")
    session_token = st.text_input("AWS session token (optional)", type="password", key="aws_session_token")

ask_tab, plan_tab, sql_tab = st.tabs(["Ask", "Step plan", "Run SQL"])

with ask_tab:
    question = st.text_area(
        "Question",
        placeholder="How many orders are from the phone subcategory?",
    )
    ask_clicked = st.button("Ask", type="primary")
    final_slot = st.empty()
    answer_slot = st.container()

with plan_tab:
    slots = {name: st.empty() for name in STAGE_NAMES}

with sql_tab:
    manual_sql = st.text_area(
        "SQL",
        placeholder="SELECT \"Category\", COUNT(*) FROM sample_superstore.sample_superstore.\"Orders\" GROUP BY 1",
        help="One read-only SELECT or WITH statement. Table names are schema.schema.table, as in the catalog.",
    )
    run_sql_clicked = st.button("Run SQL")
    sql_answer_slot = st.container()

if run_sql_clicked:
    with sql_answer_slot:
        try:
            manual_result = run_manual_sql(manual_sql, resolve(database).db_file, _default_row_limit())
        except ExecuteError as error:
            st.error(error.message)
        else:
            if manual_result["rows"]:
                st.dataframe(
                    [dict(zip(manual_result["columns"], row)) for row in manual_result["rows"]],
                    hide_index=True,
                )
            else:
                st.info("The query returned no rows.")
            if manual_result["truncated"]:
                st.caption(f"Showing the first {manual_result['row_count']} rows. The query matched more.")

if ask_clicked:
    question_text = question.strip()
    if not question_text:
        for name in STAGE_NAMES:
            render_waiting(slots[name], name)
        final_slot.warning("Enter a question first.")
    else:
        for name in STAGE_NAMES:
            render_waiting(slots[name], name)
        planner_parts: list[str] = []
        sql_parts: list[str] = []

        def stream_into(name: str, parts: list[str]):
            def show(text: str) -> None:
                parts.append(text)
                with slots[name].container():
                    st.caption(f"{stage_number(name)}. {name}: streaming")
                    st.code("".join(parts), language="json" if name == "llm" else "sql")

            return show

        request = Request(
            database=database,
            question=question_text,
            credentials=credentials_from_form(access_key_id, secret_access_key, session_token),
            on_chunk=stream_into("llm", planner_parts),
            on_sql_chunk=stream_into("sql_generate", sql_parts),
        )
        trace = run(request, on_stage_done=lambda result: render_stage(slots[result.stage], result))
        st.session_state[TRACE] = trace
        final_slot.write(f"Final status: {final_status(trace)}")
        render_answer(answer_slot, trace)
elif TRACE in st.session_state:
    trace = st.session_state[TRACE]
    for result in trace:
        render_stage(slots[result.stage], result)
    final_slot.write(f"Final status: {final_status(trace)}")
    render_answer(answer_slot, trace)
else:
    for name in STAGE_NAMES:
        render_waiting(slots[name], name)
