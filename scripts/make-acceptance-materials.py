"""Generate the fictional materials shipped in the on-premises acceptance kit."""

import json
import subprocess
import sys
from pathlib import Path

from docx import Document
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

root = Path(sys.argv[1])
departments = root / "departments"
materials = root / "materials"
departments.mkdir(parents=True, exist_ok=True)
materials.mkdir(parents=True, exist_ok=True)

rules = {
    "Отдел хранения": "1. Отдел исследует политики кэширования запросов и ускоряет "
    "запросы в базах данных. 2. Отдел оценивает задержки и нагрузку хранилищ данных.",
    "Отдел измерений": "1. Отдел разрабатывает методы стендовых измерений "
    "задержки программных систем. 2. Отдел проверяет воспроизводимость нагрузочных испытаний.",
}
for number, text in enumerate(rules.values(), 1):
    (departments / f"{number:02}.txt").write_text(text + "\n", encoding="utf-8")

talks = [
    (
        "Кэширование запросов",
        "Иванов И. И., Институт примера",
        "Политика LFU снизила задержку чтения на 23 % при 1000 запросах в секунду. "
        "Сравнение LRU и LFU провели на стенде из четырёх узлов с 32 ГБ памяти.",
        list(rules),
        "01-cache.pptx",
    ),
    (
        "Архив афиш",
        "Романова В. В., Музей примера",
        "Коллекция содержит 14 афиш художественных выставок. Исследование посвящено "
        "технике гравюры и истории выставочных залов. Подготовлен каталог иллюстраций.",
        [],
        "02-posters.docx",
    ),
    (
        "Воспроизводимость измерений задержки",
        "Орлов А. А., Лаборатория примера",
        "Сто повторов стендового измерения показали разброс 3 мс. "
        "В отчёте приведён метод калибровки таймера и описана погрешность измерения.",
        ["Отдел измерений"],
        "03-timing.pdf",
    ),
    (
        "Кэш с ограничением памяти",
        "Токарева Е. Ю., Лаборатория примера",
        "Испытание базы данных на 8 ГБ показало рост доли попаданий в кэш с 52 % до 71 %. "
        "Сравнивались политики вытеснения страничного кэша.",
        ["Отдел хранения"],
        "04-memory.docx",
    ),
    (
        "Анализ холста",
        "Громов С. И., Музей примера",
        "Под микроскопом исследованы 8 образцов грунтовки холста. "
        "Фотографии образцов добавлены в атлас живописных материалов.",
        [],
        "05-canvas.docx",
    ),
    (
        "Проект будущего исследования",
        "Зайцев Р. О., Институт примера",
        "Материалы проекта ещё собираются. Тема и методы будут определены позже. "
        "Отдельный эксперимент пока не проведён.",
        None,
        "06-proposal.docx",
    ),
    (
        "Кэш запросов при второй нагрузке",
        "Гагарина О. А., Лаборатория примера",
        "Политика вытеснения запросов уменьшила число обращений к хранилищу на 18 %. "
        "Сравнение выполнено на двух конфигурациях базы данных.",
        ["Отдел хранения"],
        "07-workload.docx",
    ),
    (
        "Оценка погрешности таймера",
        "Лапин Е. И., Институт примера",
        "В двадцати сериях измерений задержки программной системы "
        "погрешность таймера не превысила 2 мс. Серии повторили в одном стенде.",
        ["Отдел измерений"],
        "08-timer.docx",
    ),
]
expected = []
for title, author, text, relevant, filename in talks:
    target = materials / filename
    expected.append({"file": filename, "title": title, "departments": relevant, "evidence": text})
    if target.suffix == ".pptx":
        deck = Presentation()
        cover = deck.slides.add_slide(deck.slide_layouts[0])
        cover.shapes.title.text = title
        cover.placeholders[1].text = author
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Стендовое измерение задержки кэша"
        chart = CategoryChartData()
        chart.categories = ["LRU", "LFU"]
        chart.add_series("Задержка чтения, мс", (100, 77))
        slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.6), Inches(1.35), Inches(8.8), Inches(4), chart
        ).chart.plots[0].has_data_labels = True
        slide.shapes.add_textbox(
            Inches(0.6), Inches(5.65), Inches(8.8), Inches(1.3)
        ).text_frame.text = text
        deck.save(target)
        continue
    report = Document()
    report.add_heading(title, 0)
    report.add_paragraph(author)
    if filename == "02-posters.docx":
        report.add_paragraph("Должность: архивист. Контакт: romanova@example.org")
    report.add_paragraph(text)
    source = target.with_suffix(".docx")
    report.save(source)
    if target != source:
        subprocess.run(
            [
                "libreoffice",
                "-env:UserInstallation=file:///tmp/rapporteur-fixture-office",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(materials),
                str(source),
            ],
            check=True,
        )
        source.unlink()

(materials / "event-program.txt").write_text(
    "Программа конференции «Лаборатория примера»\n"
    "Пленарное заседание, 20 апреля 2026 г., 10:00.\n"
    "10:00 Вступительное слово.\n"
    "10:10 Кэширование запросов. Иванов И. И., Институт примера.\n"
    "Секция «Стендовые измерения», 20 апреля 2026 г., 12:00.\n"
    "12:00 Воспроизводимость измерений задержки. Орлов А. А.\n",
    encoding="utf-8",
)
opening = Document()
opening.add_heading("Вступительное слово", 0)
opening.add_paragraph("Добро пожаловать на конференцию «Лаборатория примера».")
opening.save(materials / "event-opening.docx")
paper = Document()
paper.add_heading(talks[0][0], 0)
paper.add_paragraph(talks[0][1])
paper.add_paragraph(talks[0][2])
paper.save(materials / "supplement.docx")
expected[0]["additional_files"] = ["supplement.docx"]
(root / "expected.json").write_text(
    json.dumps(
        {
            "departments": list(rules),
            "talks": expected,
            "context": ["event-program.txt", "event-opening.docx"],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
