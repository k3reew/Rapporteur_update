"""Shared, explicit regulation and classifier fixtures for pre-screening regression scenarios."""

import json
from typing import Any

from rapporteur import db
from rapporteur.llm.client import ChatRequest, ChatResponse, ScriptedClient

DEPARTMENT = "fixture-department"


def seed_regulation(database: db.Database) -> None:
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO departments VALUES (?,?,?,?,?,?,?)",
            (
                DEPARTMENT,
                1,
                "Тестовый отдел",
                "положение.txt",
                "a" * 64,
                db.dumps(
                    [
                        {
                            "page": 1,
                            "text": "Отдел изучает представленные методы и "
                            "результаты исследований.",
                        }
                    ]
                ),
                db.now(),
            ),
        )


class WorkflowClient(ScriptedClient):
    """Existing tests script facts/summary; new screening has its own dedicated tests."""

    def chat(self, request: ChatRequest) -> ChatResponse:
        schema: dict[str, Any] = request.schema or {}
        if schema.get("title") == "InterestAnswer":
            data = json.loads(request.messages[1].content)
            source, regulation = data["sources"][0], data["regulations"][0]
            response = {
                "interest": 90,
                "reason": "Связь в вымышленном сценарии",
                "evidence": [{"label": source["label"], "quote": source["text"][:200]}],
                "paragraphs": [{"label": regulation["label"], "quote": regulation["text"][:200]}],
            }
            return ChatResponse(json.dumps(response, ensure_ascii=False), "", "stop", 100, 100, 0.0)
        if schema.get("title") == "Matches" and DEPARTMENT in request.messages[1].content:
            return ChatResponse('{"items":[]}', "", "stop", 100, 10, 0.0)
        return super().chat(request)
