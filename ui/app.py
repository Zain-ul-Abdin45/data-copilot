"""
Chat interface for the data copilot (Chainlit, self-hosted). Deliberately thin: all the
layout decisions live in render.py, which is tested without a browser.

    sh ui/run.sh                      # then open http://127.0.0.1:8000
    COPILOT_UI_STUB=1 sh ui/run.sh    # canned answers, no model needed
    COPILOT_UI_THEME=dark ...         # chart colours for dark mode (set Chainlit's theme to match)

It binds to localhost only and has no login by default: do not expose it on a network without
setting COPILOT_UI_USER/COPILOT_UI_PASSWORD first (see below).
"""
import os
import sys
from pathlib import Path

import chainlit as cl

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

def _ensure_files_dir() -> None:
    """Chainlit's local element-file store: every element send/update (a chart, the calculation
    panel, the table-focus sidebar, column info) persists into a per-session folder under here via
    a plain (non-recursive) mkdir, so any of them crashes if this parent folder does not already
    exist. It usually does — chainlit creates it once at import time too — but its own server.py
    wipes the WHOLE directory (shutil.rmtree) during a process's shutdown sequence, and that
    sequence can run well after a newer process has already started serving real sessions if the
    old process was slow to actually exit. Found live: a crash ~10 minutes into an otherwise-
    working session, long after the one-time import-time creation should have made this
    impossible. Call this right before anything that sends or updates an element, not just once
    at import — it does not depend on ever winning that race, only on re-checking before use."""
    (HERE / ".files").mkdir(exist_ok=True)


_ensure_files_dir()

import render  # noqa: E402

SCHEMA_LABEL = "analytics"

if os.getenv("COPILOT_UI_STUB"):
    from stub import ask  # noqa: E402

    def _columns_by_table() -> dict[str, list[str]]:
        # kept independent of the database, like the rest of stub mode
        return {t: [] for t in ("fct_orders", "dim_customers", "stg_orders", "stg_payments", "stg_customers")}
else:
    from agent import ask  # noqa: E402

    def _columns_by_table() -> dict[str, list[str]]:
        try:
            import catalog
            return {t: [c for c, _ in cols] for t, cols in catalog.table_columns().items()}
        except Exception:  # noqa: BLE001  the sidebar just offers nothing to pick from
            return {}


def available_tables() -> list[str]:
    """All table names, independent of their columns. Chat start needs both together and must
    not query twice for it — see _open_table_sidebar, which calls _columns_by_table() itself
    rather than this."""
    return sorted(_columns_by_table())


def grouped_tables(tables: list[str], columns_by_table: dict[str, list[str]] | None = None) -> list[dict]:
    """Group flat table names into the tree the sidebar renders. There is only one SQL
    schema exposed to the agent (settings.SCHEMA/DUCKDB_SCHEMA/TRINO_SCHEMA are all a single
    schema, not raw/staging/marts), so "schema > tables" is expressed as schema > table-kind
    (by dbt naming convention) > tables, rather than a second, non-existent schema level.
    Each table carries its column names (empty in stub mode, or if the database is unreachable)
    so the sidebar can show them without a second round-trip per table."""
    columns_by_table = columns_by_table or {}
    kinds = [("Facts", "fct_"), ("Dimensions", "dim_"), ("Staging", "stg_")]
    groups: dict[str, list[dict]] = {label: [] for label, _ in kinds}
    groups["Reference"] = []
    for table in tables:
        label = next((label for label, prefix in kinds if table.startswith(prefix)), "Reference")
        groups[label].append({"name": table, "columns": columns_by_table.get(table, [])})
    return [{"label": label, "tables": sorted(entries, key=lambda e: e["name"])}
            for label, entries in groups.items() if entries]


THEME = os.getenv("COPILOT_UI_THEME", "light")

# Off by default, matching "localhost only, no login". Set both env vars to require a login
# before exposing this beyond localhost (a single shared password, not per-user accounts).
UI_USER = os.getenv("COPILOT_UI_USER")
UI_PASSWORD = os.getenv("COPILOT_UI_PASSWORD")

if UI_USER and UI_PASSWORD:
    @cl.password_auth_callback
    def auth(username: str, password: str):
        if username == UI_USER and password == UI_PASSWORD:
            return cl.User(identifier=username)
        return None

STARTERS = [
    ("Net revenue by month", "Show net revenue by month."),
    ("Top customers", "Who are our top 3 customers by net revenue?"),
    ("Refund rate", "What percentage of orders get refunded?"),
    ("How is revenue defined?", "How exactly is net revenue calculated?"),
]


@cl.set_starters
async def starters():
    return [cl.Starter(label=label, message=message) for label, message in STARTERS]


async def _open_table_sidebar() -> bool:
    """(Re-)populate the persistent table-focus sidebar from the current session's selection.
    Used at chat start and by the "Tables" action button, so closing the sidebar (or a page
    reload dropping it) is never a dead end — the button always brings it back with whatever was
    already checked, not reset to empty. The element is kept in the session so the "(i)" column
    button (a separate action, arriving later, possibly much later) can update THIS SAME element
    in place rather than needing to rebuild the whole tree from scratch. Returns whether there was
    anything to show, so a caller (chat start) that needs to know does not have to ask again —
    _columns_by_table() is one real database round trip; on_chat_start used to make three."""
    _ensure_files_dir()
    columns_by_table = _columns_by_table()
    groups = grouped_tables(sorted(columns_by_table), columns_by_table)
    if not groups:
        return False
    selected = sorted(cl.user_session.get("focus_tables") or [])
    element = cl.CustomElement(name="TableFocus",
                               props={"schema": SCHEMA_LABEL, "groups": groups, "selected": selected})
    cl.user_session.set("sidebar_element", element)
    await cl.ElementSidebar.set_title("Tables")
    await cl.ElementSidebar.set_elements([element])
    return True


@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])
    cl.user_session.set("focus_tables", set())
    has_tables = await _open_table_sidebar()
    # The sidebar above arrives after an async round-trip, so it can be genuinely absent from the
    # very first paint (found live: a screenshot taken right at page load showed no sidebar at
    # all). This message is not subject to that timing — it is always there from the first
    # screen, in plain text, with a real button, not a hope that the panel has arrived by now.
    if has_tables:
        await cl.Message(
            content=("There is a table list on the right (or click \"Tables\" below). Checking "
                     "boxes there tells me which tables to prefer, but I can still look elsewhere "
                     "if none of them answer your question."),
            actions=[tables_action()],
        ).send()


@cl.action_callback("set_focus_tables")
async def set_focus_tables(action: cl.Action):
    cl.user_session.set("focus_tables", set(action.payload.get("selected") or []))


@cl.action_callback("show_tables")
async def show_tables(action: cl.Action):
    await _open_table_sidebar()


@cl.action_callback("column_info")
async def column_info(action: cl.Action):
    """Fill rate and a small sample for one column, fetched on click rather than upfront for
    every column of every table (most will never be looked at). Updates the SAME sidebar element
    already on screen, in place, rather than rebuilding the whole tree — checked boxes and
    already-fetched column info for other columns must survive this."""
    if os.getenv("COPILOT_UI_STUB"):
        return
    element = cl.user_session.get("sidebar_element")
    if element is None:
        return
    _ensure_files_dir()
    table, column = action.payload.get("table"), action.payload.get("column")
    import catalog
    info = catalog.column_info(table, column)
    element.props = {**element.props,
                     "columnInfo": {**(element.props.get("columnInfo") or {}), f"{table}.{column}": info}}
    await element.update()


def tables_action() -> cl.Action:
    # a fresh instance per message: Action.send() mutates self.forId, so one shared instance
    # reused across messages would have every earlier message's button silently repoint to
    # whichever message sent it last
    return cl.Action(name="show_tables", payload={}, label="Tables",
                     tooltip="Reopen the table-focus panel")


@cl.on_message
async def on_message(message: cl.Message):
    _ensure_files_dir()  # this message's charts/calculation panel are about to be sent as elements
    history = cl.user_session.get("history") or []
    focus = cl.user_session.get("focus_tables") or None
    async with cl.Step(name="Working on it", type="run"):
        try:
            result = await cl.make_async(ask)(message.content, history[-render.MAX_TURNS:], focus)
        except Exception as e:  # noqa: BLE001  the person sees a sentence, the log has the detail
            await cl.Message(content=f"I could not answer that: {' '.join(str(e).split())[:200]}").send()
            return

    view = render.build_view(result)
    elements = [cl.Plotly(name=f"chart_{i}", figure=render.make_figure(spec, THEME), display="inline")
                for i, spec in enumerate(view["charts"])]
    details = view["details_md"]
    if focus:
        details += f"\n\n_Focused on: {', '.join(sorted(focus))} (a preference, not a restriction)_"
    # inline, not "side": a side element on every message shares the same panel as the
    # persistent ElementSidebar (the table-focus tree) and was found to take it over the moment
    # the first answer arrived, making the tree vanish right after chat start instead of staying
    # reachable for the rest of the conversation
    elements.append(cl.Text(name="How this was calculated", content=details, display="inline"))
    # a manual escape hatch back to the table-focus panel on every answer, independent of
    # whatever state the browser's own sidebar happens to be in
    await cl.Message(content=render.compose(view), elements=elements, actions=[tables_action()]).send()

    history.append(render.history_entry(message.content, result))
    cl.user_session.set("history", history)
