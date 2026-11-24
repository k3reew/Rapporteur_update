"""Run the fictional acceptance conference in an empty, isolated instance."""

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASELINE_STATES = {"relevant", "not_relevant", "uncertain"}


def run(url: str, out: Path, fixture: Path, timeout: int) -> None:
    started = time.monotonic()
    timeline = {}

    def mark(stage: str) -> None:
        timeline[stage] = round(time.monotonic() - started, 3)
        write("timeline.json", timeline)

    def write(name: str, data: object) -> None:
        (out / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def request(path: str, *, method: str = "GET", data=None, file: Path | None = None):
        headers = {}
        body = None
        if file:
            headers["Content-Type"] = "application/octet-stream"
            body = file.read_bytes()
        elif data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        query = urllib.request.Request(
            url + "/api/v1" + path, data=body, headers=headers, method=method
        )
        with urllib.request.urlopen(query, timeout=60) as response:
            raw = response.read()
            if response.headers.get_content_type() == "application/json":
                return json.loads(raw)
            return raw

    def wait(description, probe, done):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = probe()
            if done(value):
                return value
            time.sleep(1)
        raise TimeoutError(f"Не завершён этап: {description}")

    if request("/conferences") or request("/departments"):
        raise ValueError("Для приёмки нужен отдельный экземпляр с пустым хранилищем")
    system = request("/system")
    write("system.json", system)
    if not system["inference_available"] or any(
        not replica["ready"] for replica in system.get("inference_replicas", [])
    ):
        raise RuntimeError("Дождитесь готовности всех экземпляров выбранного профиля")

    expected = json.loads((fixture / "expected.json").read_text(encoding="utf-8"))
    departments = {}
    for index, name in enumerate(expected["departments"], 1):
        file = fixture / "departments" / f"{index:02}.txt"
        query = urllib.parse.urlencode({"name": name, "filename": file.name})
        department = request("/departments?" + query, method="POST", file=file)
        departments[name] = department["id"]
    conference = request(
        "/conferences",
        method="POST",
        data={
            "title": "Приёмочная конференция «Лаборатория примера»",
            "workflow": "operator",
            "place": "Учебное рабочее пространство",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )
    base = f"/conferences/{conference['id']}"
    file_ids = {}
    for material in sorted((fixture / "materials").iterdir()):
        params = {"path": material.name}
        if material.name == "event-program.txt":
            params["purpose"] = "program"
        if material.suffix == ".pdf":
            params["purpose"] = "document"
        source = request(
            base + "/materials?" + urllib.parse.urlencode(params), method="POST", file=material
        )
        file_ids[material.name] = source["id"]
    mark("materials_ready")
    request(base + "/preparation", method="POST", data={"auto_start": True, "workflow": "operator"})
    proposal = wait(
        "подготовка материалов",
        lambda: request(base + "/preparation"),
        lambda item: item["job"] in {"done", "confirmed", "failed", "paused"},
    )
    write("preparation.json", proposal)
    mark("preparation_finished")
    if proposal["job"] != "done":
        raise RuntimeError(f"Ожидалось подтверждение реестра: {proposal['job']}")
    proposed = {
        frozenset(link["file_id"] for link in talk["links"]): talk for talk in proposal["talks"]
    }
    groups = {
        frozenset(
            file_ids[name] for name in [spec["file"], *spec.get("additional_files", [])]
        ): spec
        for spec in expected["talks"]
    }
    context = {file_ids[name] for name in expected["context"]}
    if (
        set(proposed) != set(groups)
        or len(proposed) != len(proposal["talks"])
        or set(proposal["context_file_ids"]) != context
    ):
        raise RuntimeError("Сверьте группировку в preparation.json с expected.json")
    proposal = request(
        base + "/preparation",
        method="PUT",
        data={
            "version": proposal["version"],
            "talks": proposal["talks"],
            "context_file_ids": sorted(context),
        },
    )
    talks = {
        talk["talk_id"]: groups[frozenset(link["file_id"] for link in talk["links"])]
        for talk in proposal["talks"]
    }
    mark("screening_requested")

    def progress():
        return request(base + "/progress")["talks"]

    screening = wait(
        "отбор докладов",
        progress,
        lambda rows: rows and all(row["screening"]["state"] in BASELINE_STATES for row in rows),
    )
    mark("screening_finished")
    write("screening.json", screening)
    score = {}
    for row in screening:
        specification = talks[row["id"]]
        result = row["screening"]
        predicted = [
            item["department_name"] for item in result["items"] if item["status"] == "relevant"
        ]
        wanted = specification["departments"]
        expected_state = "uncertain" if wanted is None else "relevant" if wanted else "not_relevant"
        score[specification["file"]] = {
            "expected": wanted,
            "expected_state": expected_state,
            "predicted": predicted,
            "state": result["state"],
            "error": result["error"],
            "match": not result["error"]
            and result["state"] == expected_state
            and sorted(predicted) == sorted(wanted or []),
        }
    write("classification.json", score)
    if any(row.get("summary_number") is not None for row in screening):
        raise RuntimeError("Аннотация началась до подтверждения назначения")
    request(
        base + "/screening",
        method="PUT",
        data={
            "talks": [
                {
                    "talk_id": row["id"],
                    "version": row["screening"]["version"],
                    "department_ids": [
                        departments[name] for name in (talks[row["id"]]["departments"] or [])
                    ],
                }
                for row in screening
            ]
        },
    )
    mark("approved_fixture_routes")

    terminal_jobs = {"done", "failed", "paused"}
    reviews = wait(
        "черновики",
        progress,
        lambda rows: rows and all(row["job"] in terminal_jobs for row in rows),
    )
    mark("drafts_finished")
    write("drafts.json", reviews)
    selected = []
    for row in reviews:
        talk_id = row["id"]
        specification = talks[talk_id]
        if row["job"] != "done":
            raise RuntimeError(
                f"Не завершён вымышленный доклад {specification['file']}: {row['job']}"
            )
        if specification["departments"]:
            detail = request(f"/talks/{talk_id}/summary")
            write(specification["file"] + ".summary.json", detail)
            summary = detail["summary"]
            if not summary["sentences"] or row["card_only"]:
                raise RuntimeError(f"Пустая сводка: {specification['file']}")
            if specification["file"] == "01-cache.pptx":
                chart_observations = [
                    fragment["text"]
                    for fragment in detail["pack"]["fragments"]
                    if fragment["kind"] == "slide_description"
                    and fragment["source_file_id"] == file_ids[specification["file"]]
                ]
                chart_text = " ".join(chart_observations)
                chart_matches = all(
                    re.search(rf"(?<!\d){value}(?!\d)", chart_text) for value in ("77", "100")
                )
                write("chart.json", {"observations": chart_observations, "match": chart_matches})
            fragment_revisions = {
                fragment["label"]: fragment["revision_id"]
                for fragment in detail["pack"]["fragments"]
            }
            claims = {claim["key"]: claim for claim in summary["claims"]}
            for line in [*summary["sentences"], *summary["conclusions"]]:
                for key in line["claim_ids"]:
                    if not any(
                        fragment_revisions.get(evidence["label"])
                        == evidence["fragment_revision_id"]
                        for evidence in claims[key]["evidence"]
                    ):
                        raise RuntimeError(
                            f"Нет проверяемого источника {key}: {specification['file']}"
                        )
            request(
                f"/talks/{talk_id}/summary/review",
                method="POST",
                data={"version": summary["version"]},
            )
        elif not row["card_only"]:
            raise RuntimeError(f"Ожидалась карточка: {specification['file']}")
        selected.append(talk_id)

    draft = request(base + "/report-draft")
    draft = request(
        base + "/report-draft",
        method="PUT",
        data={
            "revision": draft["revision"],
            "fields": {
                **draft["fields"],
                "participation": "Участвовали представители Института примера",
                "program": (fixture / "materials/event-program.txt").read_text(encoding="utf-8"),
                "contacts": [
                    {
                        "organization": "Институт примера",
                        "person": "Иванов И. И.",
                        "talk": "Кэширование запросов",
                        "cooperation": "Согласовать стенд испытаний",
                    }
                ],
            },
        },
    )
    write("report-draft.json", draft)
    job = request(
        base + "/report-jobs",
        method="POST",
        data={
            "talk_ids": selected,
            "draft_revision": draft["revision"],
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    job = wait(
        "выпуск справки",
        lambda: request(f"/report-jobs/{job['id']}"),
        lambda item: item["status"] in terminal_jobs,
    )
    write("report-job.json", job)
    if job["status"] != "done":
        raise RuntimeError(f"Справка не выпущена: {job['status']}")
    for kind, magic in {"pdf": b"%PDF-", "docx": b"PK", "md": b""}.items():
        document = request(f"/report-versions/{job['report_version_id']}/files/{kind}")
        if not document or not document.startswith(magic):
            raise RuntimeError(f"Повреждённый экспорт {kind}")
        (out / f"report.{kind}").write_bytes(document)
    mark("exports_downloaded")

    write(
        "exports.json",
        {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in out.glob("report.*")},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Используйте локальный HTTP URL отдельной тестовой установки")
    fixture = Path(__file__).resolve().parents[1] / "docs/examples/acceptance-0.5.0"
    try:
        run(args.url.rstrip("/"), args.output, fixture, args.timeout)
    except urllib.error.HTTPError as error:
        (args.output / "http-error.txt").write_text(
            error.read().decode(errors="replace"), encoding="utf-8"
        )
        raise


if __name__ == "__main__":
    main()
