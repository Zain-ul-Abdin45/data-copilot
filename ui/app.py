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

import render  # noqa: E402

if os.getenv("COPILOT_UI_STUB"):
    from stub import ask  # noqa: E402
else:
    from agent import ask  # noqa: E402

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


@cl.on_chat_start
async def start():
    cl.user_session.set("history", [])


@cl.on_message
async def on_message(message: cl.Message):
    history = cl.user_session.get("history") or []
    async with cl.Step(name="Working on it", type="run"):
        try:
            result = await cl.make_async(ask)(message.content, history[-render.MAX_TURNS:])
        except Exception as e:  # noqa: BLE001  the person sees a sentence, the log has the detail
            await cl.Message(content=f"I could not answer that: {' '.join(str(e).split())[:200]}").send()
            return

    view = render.build_view(result)
    elements = [cl.Plotly(name=f"chart_{i}", figure=render.make_figure(spec, THEME), display="inline")
                for i, spec in enumerate(view["charts"])]
    elements.append(cl.Text(name="How this was calculated", content=view["details_md"], display="side"))
    await cl.Message(content=render.compose(view), elements=elements).send()

    history.append(render.history_entry(message.content, result))
    cl.user_session.set("history", history)
