"""Карточка доклада: тема, авторы, организации и контакты."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur import db
from rapporteur.app import create_app
from rapporteur.llm.client import ScriptedClient

from .workflow_support import seed_regulation


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedClient]]:
    scripted = ScriptedClient([])
    application = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(application.state.database)
    with TestClient(application) as client:
        yield client, scripted


def _talk(client: TestClient) -> tuple[str, str]:
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    review = client.post(
        f"/api/v1/conferences/{conference}/talks", json={"title": "Кэширование"}
    ).json()
    return conference, review["talks"][0]["id"]


def test_card_fields_are_saved_and_kept_by_older_clients(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, _ = web
    _, talk_id = _talk(client)
    card = {
        "title": "Кэширование",
        "speakers": [
            {
                "name": "Иванов И. И.",
                "affiliation": "МГТУ им. Н. Э. Баумана",
                "contacts": ["ivanov@example.org"],
            }
        ],
        "organizations": ["ИСП РАН"],
        "contacts": ["+7 495 000-00-00"],
    }
    assert client.put(f"/api/v1/talks/{talk_id}", json=card).status_code == 204
    saved = client.get(f"/api/v1/talks/{talk_id}").json()
    assert saved["speakers"][0]["contacts"] == ["ivanov@example.org"]
    assert saved["speakers"][0]["affiliation"] == "МГТУ им. Н. Э. Баумана"
    assert saved["organizations"] == ["ИСП РАН"]
    assert saved["contacts"] == ["+7 495 000-00-00"]

    # Правка только темы и авторов (как в прежнем интерфейсе) не стирает остальное.
    rename = {"title": "Кэширование данных", "speakers": saved["speakers"]}
    assert client.put(f"/api/v1/talks/{talk_id}", json=rename).status_code == 204
    renamed = client.get(f"/api/v1/talks/{talk_id}").json()
    assert renamed["title"] == "Кэширование данных"
    assert renamed["organizations"] == ["ИСП РАН"]
    assert renamed["contacts"] == ["+7 495 000-00-00"]

    cleared = rename | {"organizations": [], "contacts": []}
    assert client.put(f"/api/v1/talks/{talk_id}", json=cleared).status_code == 204
    assert client.get(f"/api/v1/talks/{talk_id}").json()["organizations"] == []
    assert client.put(f"/api/v1/talks/{talk_id}", json=rename | {"contacts": [""]}).status_code == (
        422
    )


def test_confirmed_proposal_writes_card(web: tuple[TestClient, ScriptedClient]) -> None:
    client, _ = web
    conference = client.post("/api/v1/conferences", json={"title": "Без папок"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    uploaded = client.post(
        base + "/materials",
        params={"path": "речь.txt", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content="Тема доклада: кэширование.".encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    with client.app.state.database.tx() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO preparations(conference_id,version,job) VALUES (?,1,'done')", (conference,)
        )
    proposal = {
        "version": 1,
        "talks": [
            {
                "title": "Кэширование",
                "speakers": [{"name": "Иванов И. И.", "contacts": ["ivanov@example.org"]}],
                "organizations": ["ИСП РАН"],
                "contacts": ["https://example.org"],
                "links": [{"file_id": uploaded.json()["id"]}],
            }
        ],
    }
    response = client.put(base + "/preparation", json=proposal)
    assert response.status_code == 200, response.text
    talk_id = response.json()["talks"][0]["talk_id"]
    card = client.get(f"/api/v1/talks/{talk_id}").json()
    assert card["speakers"][0]["contacts"] == ["ivanov@example.org"]
    assert card["organizations"] == ["ИСП РАН"]
    assert card["contacts"] == ["https://example.org"]


def test_release_030_database_gets_card_columns_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "rapporteur.db"
    database = db.Database(path)
    conference, section, talk_id = db.new_id(), db.new_id(), db.new_id()
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO conferences(id, title, created_at) VALUES (?, 'Старая', ?)",
            (conference, db.now()),
        )
        conn.execute(
            "INSERT INTO sections(id, conference_id, title, position) VALUES (?, ?, 'С', 1)",
            (section, conference),
        )
        conn.execute(
            "INSERT INTO talks(id, section_id, position, title, speakers) "
            "VALUES (?, ?, 1, 'Доклад', '[{\"name\": \"Иванов И. И.\"}]')",
            (talk_id, section),
        )
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE talks DROP COLUMN organizations")
        conn.execute("ALTER TABLE talks DROP COLUMN contacts")
        conn.execute("ALTER TABLE talks DROP COLUMN card_only")
        conn.execute("ALTER TABLE talks DROP COLUMN worker_active")
        conn.execute("ALTER TABLE preparations DROP COLUMN auto_start")
        conn.execute("ALTER TABLE preparations DROP COLUMN input_snapshot")
        conn.execute("ALTER TABLE preparations DROP COLUMN review_reasons")
        # База выпуска 0.3.0: отбор и фоновые выпуски уже есть (v2), карточки ещё нет.
        conn.execute("ALTER TABLE preparations DROP COLUMN workflow")
        conn.execute("ALTER TABLE preparations DROP COLUMN screening_confirmed")
        conn.execute("ALTER TABLE preparations DROP COLUMN context_file_ids")
        conn.execute("DROP TABLE department_details")
        conn.execute("DROP TABLE report_drafts")
        conn.execute("PRAGMA user_version = 2")

    upgraded = db.Database(path)
    assert (tmp_path / "rapporteur.db.before-v3.bak").is_file()
    with upgraded.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        talk = db.talk(db.conference_talks(conn, conference)[0])
    assert talk.organizations == [] and talk.contacts == []
    assert talk.speakers[0].contacts == []
    assert talk.card_only is False


def test_contacts_are_found_without_model() -> None:
    from rapporteur.ingest.card import find_contacts

    text = (
        "E-mail: Ivanov@BMSTU.ru, тел. +7 (495) 123-45-67, 8 800 555-35-35; "
        "сайт https://example.org/lab. ORCID 0000-0002-1825-0097, t.me/lab_news. "
        "В 2026 году заслушано 8 докладов по 15 темам."
    )
    assert find_contacts(text) == [
        "ivanov@bmstu.ru",
        "+7 (495) 123-45-67",
        "8 800 555-35-35",
        "https://example.org/lab",
        "ORCID 0000-0002-1825-0097",
        "t.me/lab_news",
    ]


def test_card_hints_attribute_by_line_email_and_single_author() -> None:
    from rapporteur.ingest.card import card_hints

    title = "Кэширование в распределённых системах"
    hints = card_hints(
        [
            title,
            "Иванов И. И.¹, Петров П. П.²",
            "¹МГТУ им. Н. Э. Баумана",
            "²ИСП РАН",
            "ivanov@bmstu.ru",
            "+7 495 000-00-00",
        ],
        ["Иванов И. И.", "Петров П. П."],
        title,
    )
    assert hints.organizations == ["МГТУ им. Н. Э. Баумана", "ИСП РАН"]
    assert hints.affiliations == {}
    assert hints.speaker_contacts == {"Иванов И. И.": ["ivanov@bmstu.ru"]}
    assert hints.contacts == ["+7 495 000-00-00"]

    same_line = card_hints(
        ["Петров П. П., ИСП РАН, petrov@ispras.ru", "Иванов И. И., МГТУ им. Н. Э. Баумана"],
        ["Иванов И. И.", "Петров П. П."],
    )
    assert same_line.affiliations == {
        "Петров П. П.": "ИСП РАН",
        "Иванов И. И.": "МГТУ им. Н. Э. Баумана",
    }
    assert same_line.speaker_contacts == {"Петров П. П.": ["petrov@ispras.ru"]}
    assert same_line.organizations == []

    single = card_hints(
        [
            "Цифровой университет",
            "Сидоров Алексей Петрович",
            "АО «НПО Технология»",
            "Тел.: +7 (495) 123-45-67",
            "Спасибо за внимание!",
            "www.example.org",
        ],
        ["Сидоров Алексей Петрович"],
        "Цифровой университет",
    )
    assert single.affiliations == {"Сидоров Алексей Петрович": "АО «НПО Технология»"}
    assert single.speaker_contacts == {
        "Сидоров Алексей Петрович": ["+7 (495) 123-45-67", "www.example.org"]
    }
    assert single.organizations == [] and single.contacts == []


def test_email_matches_common_transliterations() -> None:
    from rapporteur.ingest.card import card_hints

    hints = card_hints(
        ["habarov.ae@example.org", "tsoy@example.org", "unknown@example.org"],
        ["Хабаров А. Е.", "Цой В. Р."],
    )
    assert hints.speaker_contacts == {
        "Хабаров А. Е.": ["habarov.ae@example.org"],
        "Цой В. Р.": ["tsoy@example.org"],
    }
    assert hints.contacts == ["unknown@example.org"]


def test_preparation_proposes_card_from_first_page(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    import json

    client, scripted = web
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    text = "\n".join(
        [
            "Кэширование в распределённых системах",
            "Иванов И. И.",
            "МГТУ им. Н. Э. Баумана",
            "ivanov@bmstu.ru",
            "Доклад описывает кэширование ответов в центре обработки данных.",
        ]
    )
    uploaded = client.post(
        base + "/materials",
        params={"path": "речь.txt", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content=text.encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    scripted.responses.append(
        json.dumps(
            {
                "title": "Кэширование в распределённых системах",
                "speakers": ["Иванов И. И."],
                "talk_index": None,
                "confidence": 1,
                "reason": "Титульная страница",
            }
        )
    )
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    talk = client.get(base + "/preparation").json()["talks"][0]
    assert talk["speakers"] == [
        {
            "name": "Иванов И. И.",
            "affiliation": "МГТУ им. Н. Э. Баумана",
            "role": "speaker",
            "contacts": ["ivanov@bmstu.ru"],
        }
    ]
    assert talk["organizations"] == [] and talk["contacts"] == []


def test_folder_talk_takes_authors_from_the_title_slide(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    """Доклад из папки заводится в реестре без авторов: подготовка берёт их с титульного
    слайда, и карточка относит к автору организацию и контакт."""
    import io

    import pptx

    client, scripted = web
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = "Кэширование запросов"
    slide.placeholders[1].text = "Петров Алексей Сергеевич\nИнститут примера\npetrov@example.org"
    buffer = io.BytesIO()
    deck.save(buffer)
    conference = client.post("/api/v1/conferences", json={"title": "Папки"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    uploaded = client.post(
        base + "/materials",
        params={"path": "01_Петров/доклад.pptx", "purpose": "presentation"},
        headers={"Content-Type": "application/octet-stream"},
        content=buffer.getvalue(),
    )
    assert uploaded.status_code == 201, uploaded.text
    assert client.get(base + "/materials").json()["talks"][0]["speakers"] == []

    # Подготовка описывает изображение слайда моделью.
    scripted.responses.append('{"description": "Титульный слайд с темой и автором"}')
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    state = client.get(base + "/preparation").json()
    assert state["job"] == "done", state
    assert state["talks"][0]["speakers"] == [
        {
            "name": "Петров Алексей Сергеевич",
            "affiliation": "Институт примера",
            "role": "speaker",
            "contacts": ["petrov@example.org"],
        }
    ]
    # Реестр уже знал доклад: модель спрашивалась только об изображении, не о теме и авторах.
    assert [request.schema["title"] for request in scripted.requests if request.schema] == [
        "Visual"
    ]


def test_card_keeps_registry_values_and_fills_only_gaps() -> None:
    from rapporteur.contracts import Speaker
    from rapporteur.contracts.enums import TalkStatus
    from rapporteur.materials import ReviewTalk
    from rapporteur.preparation import Link, ProposedTalk, with_card

    known = ReviewTalk(
        id="t",
        section_id="s",
        section="Секция",
        order=1,
        title="Кэширование",
        status=TalkStatus.MATCHED,
        speakers=[Speaker(name="Иванов И. И.", affiliation="ИСП РАН")],
        organizations=["Исправлено оператором"],
    )
    proposal = ProposedTalk(
        talk_id="t",
        title="Кэширование",
        speakers=[Speaker(name="Иванов И. И."), Speaker(name="Петров П. П.")],
        links=[Link(file_id="f")],
    )
    result = with_card(
        proposal,
        known,
        ["Иванов И. И., МГТУ", "Петров П. П., АО «Пример»", "petrov@example.org", "+7 495 1112233"],
    )
    assert [(s.name, s.affiliation, s.contacts) for s in result.speakers] == [
        ("Иванов И. И.", "ИСП РАН", []),
        ("Петров П. П.", "АО «Пример»", ["petrov@example.org"]),
    ]
    assert result.organizations == ["Исправлено оператором"]
    assert result.contacts == ["+7 495 1112233"]


FOOTNOTES = [
    "Кэширование в распределённых системах",
    "Иванов И. И.¹, Петров П. П.²",
    "¹ Институт системного программирования",
    "² Лаборатория «Пример»",
    "ivanov@example.org; +7 495 111-22-33",
]


def test_model_is_asked_only_when_rules_leave_gaps() -> None:
    from rapporteur.ingest.card import card_hints, needs_model

    both = ["Иванов И. И.", "Петров П. П."]
    hints = card_hints(FOOTNOTES, both, FOOTNOTES[0])
    assert needs_model(FOOTNOTES, both, both, hints)
    assert not needs_model(FOOTNOTES, both, [], hints)

    single = ["Сидоров А. П."]
    plain = ["Сидоров А. П.", "Спасибо за внимание"]
    assert not needs_model(plain, single, single, card_hints(plain, single))
    unknown = ["Сидоров А. П.", "«Яндекс»"]
    assert needs_model(unknown, single, single, card_hints(unknown, single))


def test_model_answer_is_grounded_in_pages() -> None:
    from rapporteur.ingest.card import CardAnswer, CardAuthor, grounded

    answer = CardAnswer(
        authors=[
            CardAuthor(
                name="Иванов",
                organizations=["¹ Институт системного программирования"],
                contacts=["ivanov@example.org", "ivanov@invented.org"],
            ),
            CardAuthor(name="Петров П. П.", organizations=["Придуманный институт"]),
            CardAuthor(name="Сидоров", organizations=["Лаборатория «Пример»"]),
        ],
        organizations=["Лаборатория «Пример»", "Министерство всего"],
        contacts=["+7 (495) 111-22-33"],
    )
    hints = grounded(answer, FOOTNOTES, ["Иванов И. И.", "Петров П. П."])
    assert hints.affiliations == {"Иванов И. И.": "Институт системного программирования"}
    assert hints.speaker_contacts == {"Иванов И. И.": ["ivanov@example.org"]}
    assert hints.organizations == ["Лаборатория «Пример»"]
    assert hints.contacts == ["+7 495 111-22-33"]


def test_card_uses_model_for_footnotes_and_rules_win() -> None:
    from rapporteur.contracts import Speaker
    from rapporteur.ingest.card import CardHints
    from rapporteur.preparation import Link, ProposedTalk, with_card

    asked: list[list[str]] = []

    def model(lines: list[str], names: list[str]) -> CardHints:
        asked.append(names)
        return CardHints(
            affiliations={
                "Иванов И. И.": "Институт системного программирования",
                "Петров П. П.": "Лаборатория «Пример»",
            },
            speaker_contacts={"Петров П. П.": ["ivanov@example.org"]},
        )

    proposal = ProposedTalk(
        title=FOOTNOTES[0],
        speakers=[Speaker(name="Иванов И. И."), Speaker(name="Петров П. П.")],
        links=[Link(file_id="f")],
    )
    result = with_card(proposal, None, FOOTNOTES, model)
    assert asked == [["Иванов И. И.", "Петров П. П."]]
    assert [(s.affiliation, s.contacts) for s in result.speakers] == [
        ("Институт системного программирования", ["ivanov@example.org"]),
        ("Лаборатория «Пример»", []),
    ]
    assert result.organizations == []
    assert result.contacts == ["+7 495 111-22-33"]


def test_preparation_asks_model_for_card_and_caches_answer(
    web: tuple[TestClient, ScriptedClient], tmp_path: Path
) -> None:
    import json

    client, scripted = web
    conference = client.post("/api/v1/conferences", json={"title": "Сноски"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    uploaded = client.post(
        base + "/materials",
        params={"path": "речь.txt", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content="\n".join(FOOTNOTES).encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    scripted.responses.extend(
        [
            json.dumps(
                {
                    "title": FOOTNOTES[0],
                    "speakers": ["Иванов И. И.", "Петров П. П."],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Титульная страница",
                }
            ),
            json.dumps(
                {
                    "authors": [
                        {"name": "Иванов И. И.", "organizations": [FOOTNOTES[2][2:]]},
                        {"name": "Петров П. П.", "organizations": ["Лаборатория «Пример»"]},
                    ],
                    "organizations": [],
                    "contacts": [],
                }
            ),
        ]
    )
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    state = client.get(base + "/preparation").json()
    assert state["job"] == "done", state
    speakers = state["talks"][0]["speakers"]
    assert [s["affiliation"] for s in speakers] == [
        "Институт системного программирования",
        "Лаборатория «Пример»",
    ]
    assert speakers[0]["contacts"] == ["ivanov@example.org"]
    assert state["talks"][0]["contacts"] == ["+7 495 111-22-33"]
    assert len(list((tmp_path / "cache" / "card").iterdir())) == 1
    assert not scripted.responses


def test_reapplying_preparation_keeps_manual_cards_and_rejects_stale_drafts(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, _ = web
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    url = base + "/preparation"
    proposed = []
    for number in (1, 2):
        uploaded = client.post(
            base + "/materials",
            params={"path": f"речь-{number}.txt", "purpose": "document"},
            headers={"Content-Type": "application/octet-stream"},
            content=f"Доклад {number}".encode(),
        )
        assert uploaded.status_code == 201, uploaded.text
        proposed.append(
            {"title": f"Доклад {number}", "links": [{"file_id": uploaded.json()["id"]}]}
        )
    database = client.app.state.database  # type: ignore[attr-defined]
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO preparations(conference_id,version,job) VALUES (?,1,'done')",
            (conference,),
        )
    confirmed = client.put(url, json={"version": 1, "talks": proposed})
    assert confirmed.status_code == 200, confirmed.text
    draft = {key: confirmed.json()[key] for key in ("version", "talks")}
    card_urls = [f"/api/v1/talks/{talk['talk_id']}" for talk in draft["talks"]]
    cards = []
    for number, card_url in enumerate(card_urls, 1):
        before = client.get(card_url).json()
        response = client.put(
            card_url,
            json={
                "title": f"Исправленный доклад {number}",
                "speakers": [{"name": f"Автор {number}"}],
                "organizations": [f"Институт {number}"],
                "contacts": [f"author{number}@example.org"],
                "revision": before["revision"],
            },
        )
        assert response.status_code == 204, response.text
        cards.append(client.get(card_url).json())
        assert cards[-1]["revision"] == before["revision"] + 1

    assert client.put(url, json=draft).status_code == 409
    assert [client.get(card_url).json() for card_url in card_urls] == cards

    current = client.get(url).json()
    assert current["version"] == draft["version"] + 2
    assert current["talks"][0]["contacts"] == ["author1@example.org"]
    assert current["talks"][1]["contacts"] == ["author2@example.org"]
    saved = client.put(url, json={key: current[key] for key in ("version", "talks")})
    assert saved.status_code == 200, saved.text
    for card_url, before in zip(card_urls, cards, strict=True):
        after = client.get(card_url).json()
        assert after == {**before, "revision": before["revision"] + 1}

    # Simulate a confirmed proposal persisted before card edits and preparation were synchronized.
    with database.tx() as conn:
        conn.execute(
            "UPDATE talks SET title=NULL, contacts=? WHERE id=?",
            (db.dumps(["legacy-edit@example.org"]), draft["talks"][0]["talk_id"]),
        )
    refreshed = client.get(url).json()
    assert refreshed["talks"][0]["contacts"] == ["legacy-edit@example.org"]
    assert (
        client.put(url, json={key: refreshed[key] for key in ("version", "talks")}).status_code
        == 200
    )
    legacy = client.get(card_urls[0]).json()
    assert legacy["title"] is None and legacy["contacts"] == ["legacy-edit@example.org"]


@pytest.mark.parametrize(
    ("suffix", "byline"),
    [
        ("docx", "Автор: Иванов Иван Иванович."),
        ("txt", "Автор: Иванов Иван Иванович."),
        ("txt", "Доклад подготовлен Иванов Иван Иванович"),
    ],
)
def test_folder_document_recovers_explicit_author(
    web: tuple[TestClient, ScriptedClient], suffix: str, byline: str
) -> None:
    from io import BytesIO

    from docx import Document

    client, scripted = web
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    lines = [
        "Тема доклада: Кэширование запросов.",
        byline,
        "Институт примера",
        "ivanov@example.org",
    ]
    if not byline.startswith("Автор:"):
        scripted.responses.append(
            '{"title":"Другая тема","speakers":["Иванов Иван Иванович","Неподтверждённый автор"],'
            '"talk_index":null,"confidence":1,"reason":"Строка об авторе"}'
        )
    content = "\n".join(lines).encode()
    if suffix == "docx":
        document = Document()
        for line in lines:
            document.add_paragraph(line)
        buffer = BytesIO()
        document.save(buffer)
        content = buffer.getvalue()
    uploaded = client.post(
        base + "/materials",
        params={"path": f"01_Кэширование/речь.{suffix}", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content=content,
    )
    assert uploaded.status_code == 201, uploaded.text
    talk = client.get(base + "/materials").json()["talks"][0]
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    state = client.get(base + "/preparation").json()
    assert state["job"] == "done", state
    assert state["talks"][0]["title"] == talk["title"]
    assert state["talks"][0]["speakers"] == [
        {
            "name": "Иванов Иван Иванович",
            "role": "speaker",
            "affiliation": "Институт примера",
            "contacts": ["ivanov@example.org"],
        }
    ]


def test_manual_card_edit_requeues_an_inflight_proposal(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, _ = web
    conference, talk = _talk(client)
    url = f"/api/v1/conferences/{conference}/preparation"
    assert client.post(url).status_code == 202
    database = client.app.state.database  # type: ignore[attr-defined]
    with database.tx() as conn:
        conn.execute("UPDATE preparations SET job='running' WHERE conference_id=?", (conference,))
    running = client.get(url).json()
    assert (
        client.put(
            f"/api/v1/talks/{talk}",
            json={"title": "Ручная правка", "speakers": [{"name": "Автор"}]},
        ).status_code
        == 204
    )
    queued = client.get(url).json()
    assert queued["job"] == "queued" and queued["version"] == running["version"] + 1
