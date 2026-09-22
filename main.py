from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import settings
from agent import ask

app = FastAPI(title="Data Copilot")


def require_token(authorization: str | None = Header(default=None)) -> None:
    """No-op when COPILOT_API_TOKEN is unset (the current localhost-only, no-login trust
    model). Set it before exposing this API beyond localhost."""
    if not settings.API_TOKEN:
        return
    if authorization != f"Bearer {settings.API_TOKEN}":
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token.")


class Question(BaseModel):
    question: str
    history: list[dict] = []  # earlier turns: [{"question": ..., "answer": ...}], oldest first


@app.post("/ask", dependencies=[Depends(require_token)])
def ask_endpoint(q: Question):
    result = ask(q.question, q.history)
    # The trace holds every tool call and its result, including the generated SQL, for audit.
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
