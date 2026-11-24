"""Compare repeated measurements from the offline acceptance scenario."""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import median


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def measure(path: Path) -> dict:
    timeline = read(path / "timeline.json")
    classification = {
        file: row["match"] for file, row in read(path / "classification.json").items()
    }
    gpu = {}
    with (path / "gpu.csv").open() as trace:
        for row in csv.reader(trace):
            if len(row) < 5 or not row[1].strip().startswith("GPU-"):
                continue
            uuid = row[1].strip()
            peaks = gpu.setdefault(uuid, {"memory_mib": 0, "utilization_percent": 0})
            for column, key in [(2, "utilization_percent"), (3, "memory_mib")]:
                number = re.search(r"\d+", row[column])
                if number:
                    peaks[key] = max(peaks[key], int(number.group()))
    vision_calls = re.findall(
        r"stage=vision[^\n]*?seconds=([\d.]+)", (path / "stages.txt").read_text(encoding="utf-8")
    )
    service_log = (path / "services.log").read_text(encoding="utf-8")
    return {
        "path": str(path),
        "seconds": {
            "full": timeline["drafts_finished"] - timeline["materials_ready"],
            "preparation": timeline["preparation_finished"] - timeline["materials_ready"],
            "vision_total": sum(map(float, vision_calls)),
            "screening": timeline["screening_finished"] - timeline["screening_requested"],
            "annotations": timeline["drafts_finished"] - timeline["approved_fixture_routes"],
            "exports": timeline["exports_downloaded"] - timeline["drafts_finished"],
        },
        "classification": classification,
        "chart": read(path / "chart.json")["match"],
        "gpu_peaks": gpu,
        "oom": bool(re.search(r"out of memory|OutOfMemoryError", service_log, re.IGNORECASE)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", metavar="ИМЯ=КАТАЛОГ")
    args = parser.parse_args()
    modes = defaultdict(list)
    for entry in args.runs:
        name, sep, path = entry.partition("=")
        if not sep or not name or not path:
            parser.error(f"Ожидается ИМЯ=КАТАЛОГ: {entry}")
        modes[name].append(measure(Path(path)))
    if not modes.get("ollama"):
        parser.error("Укажите как минимум один замер ollama=КАТАЛОГ")
    baseline_good = {
        file
        for case in modes["ollama"]
        for file, passed in case["classification"].items()
        if passed
    }
    baseline_time = median(case["seconds"]["full"] for case in modes["ollama"])
    result = {}
    for name, cases in modes.items():
        times = {
            key: round(median(case["seconds"][key] for case in cases), 3)
            for key in cases[0]["seconds"]
        }
        result[name] = {
            "seconds_median": times,
            "speedup_vs_ollama": round(baseline_time / max(0.001, times["full"]), 3),
            "regressions": sorted(
                {
                    file
                    for case in cases
                    for file in baseline_good
                    if not case["classification"].get(file, False)
                }
            ),
            "chart_matches": all(case["chart"] for case in cases),
            "oom": any(case["oom"] for case in cases),
            "runs": cases,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
