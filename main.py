from fastapi import FastAPI
from pydantic import BaseModel

from agent import ask

app = FastAPI(title="Data Copilot")


class Question(BaseModel):
    question: str


@app.post("/ask")
def ask_endpoint(q: Question):
    result = ask(q.question)
    # The trace holds every tool call and its result, including the generated SQL, for audit.
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
