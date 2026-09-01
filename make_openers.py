"""
Генерация опенеров по CSV без Clay.

Читает экспорт из Clay, дедуплицирует по домену, сортирует по fit_score,
берёт верхние N и для каждой строки дёргает твой /personalize-opener
с intent=job. Пишет новый CSV с опенером и результатом quality gate.

Запуск:
    $env:DOMAIN_CHECKER_API_KEY = "..."          # PowerShell
    python make_openers.py clay-export.csv --limit 20

Нужен только requests. Кредиты Clay не тратятся — платишь копейки Anthropic.
"""

import argparse
import csv
import os
import re
import sys
import time

import requests

BASE_URL = os.environ.get("GTM_SERVICE_URL", "http://localhost:8000")
API_KEY = os.environ.get("DOMAIN_CHECKER_API_KEY")

# Имена колонок в экспорте Clay отличаются от прогона к прогону.
# Здесь перечислены кандидаты: скрипт берёт первый существующий.
FIELDS = {
    "company":  ["company", "Company", "Name", "company_name"],
    "domain":   ["domain", "Domain", "Website", "domain_final"],
    "role":     ["role", "Role", "Job Title"],
    "location": ["location", "Locality", "Location"],
    "score":    ["fit_score", "Formula", "GTM Fit Score", "Fit Score"],
    "context":  ["context", "hook", "Outbound Fit Summary", "GTM Fit Score", "Description", "notes"],
}


def pick(row: dict, names) -> str:
    """Первая непустая колонка из списка кандидатов."""
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def to_score(raw: str) -> int:
    """'5', 'Fit score (1-5): 4 - Outb...' и пустое -> целое."""
    m = re.search(r"([1-5])", raw or "")
    return int(m.group(1)) if m else 0


def extract_fact(text: str) -> str:
    """Промпт шага 7 просил закончить вывод одним конкретным фактом.
    Берём последнее непустое предложение — оно и есть факт.
    Строку 'Fit score (1-5): N' выбрасываем, это не факт."""
    text = re.sub(r"Fit score\s*\(1-5\)\s*:\s*[1-5]", " ", text or "")
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if len(p.strip()) > 30]
    return parts[-1] if parts else (text or "").strip()


def opener_for(company: str, domain: str, context: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/personalize-opener",
        headers={"x-api-key": API_KEY or ""},
        params={
            "company_name": company,
            "domain": domain,
            "intent": "job",
            "context": context,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", help="CSV, выгруженный из Clay")
    ap.add_argument("--limit", type=int, default=20, help="сколько компаний обработать")
    ap.add_argument("--out", default="openers.csv")
    ap.add_argument("--min-score", type=int, default=0)
    args = ap.parse_args()

    if not API_KEY:
        print("DOMAIN_CHECKER_API_KEY не задан — сервер вернёт 401.", file=sys.stderr)
        return 1

    with open(args.infile, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("Файл пустой.", file=sys.stderr)
        return 1

    # --- нормализация + дедупликация по домену -------------------------
    # Одна компания может стоять в списке дважды (две вакансии). Оставляем
    # строку с наибольшим баллом: скоринг шумит на +-1, и брать максимум
    # честнее, чем брать ту, что попалась первой.
    best: dict[str, dict] = {}
    for r in rows:
        domain = pick(r, FIELDS["domain"]).lower().replace("https://", "").replace("http://", "").strip("/")
        if not domain:
            continue
        item = {
            "company": pick(r, FIELDS["company"]) or domain,
            "domain": domain,
            "role": pick(r, FIELDS["role"]),
            "location": pick(r, FIELDS["location"]),
            "score": to_score(pick(r, FIELDS["score"])),
            "context": extract_fact(pick(r, FIELDS["context"])),
        }
        if domain not in best or item["score"] > best[domain]["score"]:
            best[domain] = item

    picked = sorted(best.values(), key=lambda x: -x["score"])
    picked = [p for p in picked if p["score"] >= args.min_score][: args.limit]

    dropped = len(rows) - len(best)
    print(f"строк в файле: {len(rows)} | уникальных доменов: {len(best)} "
          f"(схлопнуто дублей: {dropped}) | в работу: {len(picked)}")

    out_rows, ok, failed, errored = [], 0, 0, 0
    for i, p in enumerate(picked, 1):
        if not p["context"]:
            print(f"[{i}/{len(picked)}] {p['domain']}: пустой context, пропуск")
            errored += 1
            continue
        try:
            res = opener_for(p["company"], p["domain"], p["context"])
        except requests.exceptions.RequestException as e:
            print(f"[{i}/{len(picked)}] {p['domain']}: ОШИБКА {e}")
            errored += 1
            continue

        passed = bool(res.get("passed_quality_check"))
        ok, failed = ok + passed, failed + (not passed)
        notes = "; ".join(res.get("quality_notes") or [])
        print(f"[{i}/{len(picked)}] {p['domain']}: {'OK ' if passed else 'GATE'} "
              f"{(res.get('opener') or '')[:70]}")
        if notes:
            print(f"      -> {notes}")

        out_rows.append({**p, "opener": res.get("opener", ""),
                         "passed_quality_check": passed, "quality_notes": notes})
        time.sleep(0.4)   # не долбить свой же сервер

    if out_rows:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)

    print(f"\nготово: {args.out} | прошло гейт: {ok} | не прошло: {failed} | ошибок: {errored}")
    if failed:
        print("Непрошедшие смотри в quality_notes — почти всегда виноват context, а не модель.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
