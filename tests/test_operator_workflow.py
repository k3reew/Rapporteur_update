import json
from pathlib import Path

from fastapi.testclient import TestClient

from rapporteur import processing
from rapporteur.app import create_app

from .workflow_support import WorkflowClient


def test_operator_requires_registry_and_batch_screening_confirmation(tmp_path: Path) -> None:
    llm = WorkflowClient(
        [
            json.dumps(
                {
                    "title": "Кэширование",
                    "speakers": [],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Текст доклада",
                }
            )
        ]
    )
    app = create_app(tmp_path, client=lambda _: llm, process=False)
    with TestClient(app) as client:
        dep = client.post(
            "/api/v1/departments",
            json={
                "name": "Отдел",
                "description": "Системы хранения",
                "functions": "Кэширование",
            },
        )
        assert dep.status_code == 201, dep.text
        cid = client.post("/api/v1/conferences", json={"title": "Конференция"}).json()["id"]
        base = f"/api/v1/conferences/{cid}"
        upload = client.post(
            base + "/materials",
            params={"path": "доклад.txt"},
            headers={"Content-Type": "application/octet-stream"},
            content="Тема доклада: кэширование. Анализ систем хранения.".encode(),
        )
        assert upload.status_code == 201
        started = client.post(
            base + "/preparation", json={"workflow": "operator", "auto_start": True}
        )
        assert started.status_code == 202, started.text
        assert app.state.processor.run_once()
        preparation = client.get(base + "/preparation").json()
        assert preparation["job"] == "done", preparation
        assert client.post(base + "/processing").status_code == 409
        assert client.post(base + "/screening").status_code == 409
        assert (
            client.put(
                base + "/preparation",
                json={
                    "version": preparation["version"],
                    "talks": preparation["talks"],
                },
            ).status_code
            == 200
        )
        assert client.get(base + "/screening").json()[0]["state"] == "queued"
        assert client.put(base + "/screening", json={"talks": []}).status_code == 409
        assert app.state.processor.run_once()
        row = client.get(base + "/screening").json()[0]
        assert row["state"] == "relevant"
        assert row["items"][0]["interest"] == 90
        assert row["items"][0]["evidence"][0]["source_url"].endswith("/content")
        assert row["items"][0]["paragraphs"][0]["label"].startswith("p0.")
        progress = client.get(base + "/progress").json()
        assert not progress["screening_confirmed"]
        assert progress["talks"][0]["job"] is None
        assert progress["talks"][0]["summary_number"] is None
        assert not app.state.processor.run_once()
        result = client.put(
            base + "/screening",
            json={
                "talks": [
                    {
                        "talk_id": row["talk_id"],
                        "version": row["version"],
                        "department_ids": [],
                    }
                ]
            },
        )
        assert result.status_code == 200, result.text
        assert client.get(base + "/progress").json()["screening_confirmed"]
        assert app.state.processor.run_once()
        assert client.get(base + "/progress").json()["talks"][0]["card_only"]
        updated = client.post(
            "/api/v1/departments",
            json={
                "department_id": dep.json()["id"],
                "name": "Отдел",
                "description": "Системы хранения",
                "functions": "Другое положение",
            },
        )
        assert updated.status_code == 201
        assert not client.get(base + "/progress").json()["screening_confirmed"]
        assert client.get(base + "/screening").json()[0]["state"] == "stale"
        assert client.get(base).json()["resume_step"] == "screening"


def test_restart_resumes_operator_jobs_and_keeps_legacy_jobs_paused(tmp_path: Path) -> None:
    app = create_app(tmp_path, process=False)
    with TestClient(app) as client:
        operator_id = client.post(
            "/api/v1/conferences",
            json={"title": "Новая", "workflow": "operator"},
        ).json()["id"]
        legacy_id = client.post("/api/v1/conferences", json={"title": "Архив"}).json()["id"]
    with app.state.database.tx() as conn:
        conn.execute("UPDATE preparations SET job='running' WHERE conference_id=?", (operator_id,))
        conn.execute(
            "INSERT INTO preparations(conference_id,job) VALUES (?,'running')",
            (legacy_id,),
        )
        processing.pause_unfinished(conn)
        states = dict(conn.execute("SELECT conference_id,job FROM preparations"))
    assert states == {operator_id: "queued", legacy_id: "paused"}


def test_operator_groups_content_across_provisional_folders(tmp_path: Path) -> None:
    llm = WorkflowClient(
        [
            json.dumps(
                {
                    "title": "Кэширование",
                    "speakers": [],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Тема первого файла",
                }
            ),
            json.dumps(
                {
                    "title": "Сравнение кэшей",
                    "speakers": [],
                    "talk_index": 0,
                    "confidence": 1,
                    "reason": "Тот же эксперимент",
                }
            ),
            json.dumps(
                {
                    "title": "Приветствие",
                    "speakers": [],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Организационный материал",
                    "role": "context",
                }
            ),
            *[
                json.dumps(
                    {
                        "title": f"Доклад номер {number}",
                        "speakers": [],
                        "talk_index": 0,
                        "confidence": 1,
                        "reason": "Совпадение",
                    }
                )
                for number in range(11)
            ],
        ]
    )
    app = create_app(tmp_path, client=lambda _: llm, process=False)
    with TestClient(app) as client:
        from .workflow_support import seed_regulation

        seed_regulation(app.state.database)
        cid = client.post(
            "/api/v1/conferences",
            json={
                "title": "Конференция",
                "workflow": "operator",
            },
        ).json()["id"]
        base = f"/api/v1/conferences/{cid}"
        uploaded = {}
        for path, content in (
            ("01_Секция/01_Иванов/метод.txt", "Кэширование. Описание эксперимента LRU."),
            (
                "02_Другая/02_Доклад/документ.txt",
                "Сравнение кэшей. Приложение к докладу «Кэширование».",
            ),
            ("03_Встреча/03_Открытие/слово.txt", "Приветствие. Организационное открытие."),
            *(
                (f"04_Исследования/{n:02}.txt", f"Доклад номер {n}. Изучение волн.")
                for n in range(11)
            ),
        ):
            upload = client.post(
                base + "/materials",
                params={"path": path},
                headers={"Content-Type": "application/octet-stream"},
                content=content.encode(),
            )
            assert upload.status_code == 201, upload.text
            uploaded[path] = upload.json()["id"]
        assert client.post(base + "/preparation").status_code == 202
        assert app.state.processor.run_once()
        state = client.get(base + "/preparation").json()
        assert state["job"] == "done", state
        assert len(state["talks"]) == 12
        assert len(state["talks"][0]["links"]) == 2
        assert all(len(talk["links"]) == 1 for talk in state["talks"][1:])
        assert state["context_file_ids"] == [uploaded["03_Встреча/03_Открытие/слово.txt"]]
