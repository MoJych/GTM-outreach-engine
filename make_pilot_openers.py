"""
Опенеры для пилотного списка (без Clay).

Читает CSV пилота, для каждой строки дёргает /personalize-opener,
кладёт результат в "Personalized opener" и переписывает тот же файл.
Остальные колонки сохраняются как есть.

Три проверки поверх quality_gate, добавлены 3 сен 2026 после разбора
первого прогона — гейт пропустил всё, а из десяти строк семь врали:

  1. ОТНОСИТЕЛЬНОЕ ВРЕМЯ. Модель не знает сегодняшнюю дату и пишет
     "just", "this month", "last month" про события восьмимесячной
     давности. Строка про судью, пришедшую в сентябре 2025, вышла как
     "this September". Сервер такое не поймает — это не признак плохого
     текста, это отсутствие у модели календаря.
  2. ПОВТОР ЗАЧИНА. Семи разным людям одинаковое начало незаметно.
     Здесь один человек читает все десять подряд, и четыре
     "Congratulations on..." видно сразу.
  3. ДЛИНА. Опенер длиннее ~220 знаков перестаёт быть первой строкой.

Проверки не блокируют запись — они проставляют пометку в колонку
"Review flag", чтобы ты посмотрел глазами. Блокирует только quality_gate
на сервере: не прошло — ячейка остаётся пустой.

Запуск (PowerShell):
    $env:DOMAIN_CHECKER_API_KEY = "..."
    $env:GTM_SERVICE_URL = "https://gtm-outreach-engine-w652.onrender.com"
    python make_pilot_openers.py pointone-pilot-10.csv

Ключ только из окружения — репозиторий публичный.
"""

import argparse
import csv
import datetime
import os
import re
import shutil
import sys
import time

import requests

BASE_URL = os.environ.get("GTM_SERVICE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("DOMAIN_CHECKER_API_KEY")

COL_OPENER = "Personalized opener"
COL_CONTEXT = "Signal"
COL_WHY = "Why they fit"
COL_FLAG = "Review flag"

# Слова, которые превращают восьмимесячное событие во вчерашнее.
RELATIVE_TIME = [
    r"\bjust\b", r"\bthis (month|week|quarter)\b", r"\blast (month|week)\b",
    r"\brecently\b", r"\byesterday\b", r"\bcurrently\b", r"\bnow\b",
    r"\bthis (january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
]

MAX_OPENER_LEN = 220


def build_context(row: dict, today: str) -> str:
    """Факт + дата сегодня + одна строка про компанию.

    Инструкций сюда класть нельзя: в main.py есть REFUSAL_PATTERNS ровно
    потому, что модель, получив в поле "факт" мета-указания, отвечает
    отказом вместо опенера. Поэтому дата подаётся как факт, а не как
    команда "учитывай дату".
    """
    parts = [(row.get(COL_CONTEXT) or "").strip()]
    why = (row.get(COL_WHY) or "").strip()
    if why:
        parts.append(why)
    parts.append(f"Today's date is {today}.")
    return " ".join(p for p in parts if p)


def opener_for(company: str, domain: str, context: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/personalize-opener",
        headers={"x-api-key": API_KEY or ""},
        params={
            "company_name": company,
            "domain": domain,
            "intent": "service",
            "context": context,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def review_flags(opener: str) -> list:
    flags = []
    low = opener.lower()
    for pat in RELATIVE_TIME:
        hit = re.search(pat, low)
        if hit:
            flags.append(f"относительное время: '{hit.group(0)}' — сверь с датой в Signal")
            break
    if len(opener) > MAX_OPENER_LEN:
        flags.append(f"длина {len(opener)} — первая строка письма должна быть короче")
    return flags


def repetition_report(rows: list) -> list:
    """Одинаковые зачины в списке, который человек читает подряд."""
    starts = {}
    for r in rows:
        op = (r.get(COL_OPENER) or "").strip()
        if not op:
            continue
        key = " ".join(op.split()[:2]).lower().strip(",.—-")
        starts.setdefault(key, []).append(r["Company"])
    return [(k, v) for k, v in starts.items() if len(v) > 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?", default="pointone-pilot-10.csv")
    ap.add_argument("--only-empty", action="store_true",
                    help="не трогать строки, где опенер уже заполнен")
    ap.add_argument("--today", default=None,
                    help="дата для контекста, по умолчанию сегодняшняя")
    args = ap.parse_args()

    if not API_KEY:
        print("DOMAIN_CHECKER_API_KEY не задан — сервер вернёт 401.", file=sys.stderr)
        return 1

    today = args.today or datetime.date.today().strftime("%d %B %Y")

    with open(args.infile, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)

    for need in (COL_OPENER, COL_CONTEXT, "Company", "Domain"):
        if need not in fields:
            print(f"нет колонки {need!r} — проверь файл", file=sys.stderr)
            return 1
    if COL_FLAG not in fields:
        fields.append(COL_FLAG)

    shutil.copyfile(args.infile, args.infile + ".bak")
    print(f"бэкап: {args.infile}.bak")
    print(f"дата в контексте: {today}\n")

    ok = failed = errored = skipped = flagged = 0
    for i, r in enumerate(rows, 1):
        r.setdefault(COL_FLAG, "")
        name = r["Company"][:34]

        if args.only_empty and (r.get(COL_OPENER) or "").strip():
            print(f"[{i:>2}/{len(rows)}] {name:<36} уже заполнен, пропуск")
            skipped += 1
            continue

        context = build_context(r, today)
        if not (r.get(COL_CONTEXT) or "").strip():
            print(f"[{i:>2}/{len(rows)}] {name:<36} пустой Signal, пропуск")
            errored += 1
            continue

        try:
            res = opener_for(r["Company"], r["Domain"], context)
        except requests.exceptions.RequestException as e:
            print(f"[{i:>2}/{len(rows)}] {name:<36} ОШИБКА {e}")
            errored += 1
            continue

        passed = bool(res.get("passed_quality_check"))
        notes = "; ".join(res.get("quality_notes") or [])
        opener = (res.get("opener") or "").strip()

        if passed:
            r[COL_OPENER] = opener
            fl = review_flags(opener)
            r[COL_FLAG] = "; ".join(fl)
            flagged += bool(fl)
            ok += 1
            print(f"[{i:>2}/{len(rows)}] {name:<36} {'FLAG' if fl else 'OK  '} {opener[:58]}")
            for f_ in fl:
                print(f"     ! {f_}")
        else:
            # Не прошло гейт — ячейку не трогаем, чтобы плохой опенер
            # не уехал клиенту только потому, что колонка непустая.
            failed += 1
            print(f"[{i:>2}/{len(rows)}] {name:<36} GATE {opener[:58]}")
            if notes:
                print(f"     -> {notes}")
        time.sleep(0.4)

    with open(args.infile, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nготово: {args.infile}")
    print(f"прошло гейт: {ok} | не прошло: {failed} | ошибок: {errored} | пропущено: {skipped}")
    if flagged:
        print(f"помечено на проверку глазами: {flagged} — смотри колонку '{COL_FLAG}'")

    reps = repetition_report(rows)
    if reps:
        print("\nодинаковые зачины (список читают подряд, это видно):")
        for key, companies in reps:
            print(f"  «{key}...» — {', '.join(companies)}")

    if failed:
        print("\nНепрошедшие оставлены пустыми. Перезапуск только по ним:")
        print("  python make_pilot_openers.py --only-empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
