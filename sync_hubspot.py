"""
Заливка контактов в HubSpot из CSV — чтобы /handle-reply узнавал отправителей.

Без этого шага ответ на твоё письмо вернётся с action:"ignore", Telegram
промолчит, и ты узнаешь об ответе только случайно.

Запуск:
    $env:HUBSPOT_API_KEY = "pat-..."
    python sync_hubspot.py send-ready.csv --dry-run     # посмотреть, что будет
    python sync_hubspot.py send-ready.csv               # залить

Идемпотентен: контакт с таким email уже есть -> обновляется, не дублируется.
Строки без email пропускаются (их ещё предстоит найти).
"""

import argparse
import csv
import os
import re
import sys
import time

import requests

API = "https://api.hubapi.com/crm/v3/objects/contacts"
KEY = os.environ.get("HUBSPOT_API_KEY")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

FIELDS = {
    "email":    ["email", "Email", "work_email"],
    "person":   ["person", "Person", "contact_name", "name"],
    "title":    ["title", "Title", "job_title"],
    "company":  ["company", "Company"],
    "domain":   ["domain", "Domain", "Website"],
    "linkedin": ["linkedin", "LinkedIn", "linkedin_url"],
    "role":     ["role", "Role"],
}


def pick(row, names):
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def split_name(full):
    parts = (full or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def search(email):
    r = requests.post(
        f"{API}/search",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email"], "limit": 1},
        timeout=15,
    )
    r.raise_for_status()
    res = r.json().get("results") or []
    return res[0]["id"] if res else None


def upsert(props, contact_id=None):
    url = f"{API}/{contact_id}" if contact_id else API
    method = requests.patch if contact_id else requests.post
    r = method(
        url,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not KEY and not args.dry_run:
        print("HUBSPOT_API_KEY не задан.", file=sys.stderr)
        return 1

    with open(args.infile, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    created = updated = skipped = failed = 0
    for r in rows:
        email = pick(r, FIELDS["email"]).lower()
        if not EMAIL_RE.match(email):
            skipped += 1
            continue

        first, last = split_name(pick(r, FIELDS["person"]))
        props = {
            "email": email,
            "firstname": first,
            "lastname": last,
            "jobtitle": pick(r, FIELDS["title"]),
            "company": pick(r, FIELDS["company"]),
            "website": pick(r, FIELDS["domain"]),
            "hs_lead_status": "NEW",
        }
        li = pick(r, FIELDS["linkedin"])
        if li:
            # стандартное свойство HubSpot для профиля LinkedIn
            props["hs_linkedin_url"] = li
        props = {k: v for k, v in props.items() if v}

        if args.dry_run:
            print(f"[dry] {email:32} {props.get('firstname','')} {props.get('lastname','')} — {props.get('company','')}")
            continue

        try:
            existing = search(email)
            cid = upsert(props, existing)
            if existing:
                updated += 1
                print(f"upd  {email:32} id={cid}")
            else:
                created += 1
                print(f"NEW  {email:32} id={cid}")
        except requests.exceptions.RequestException as e:
            failed += 1
            body = getattr(e.response, "text", "")[:200] if getattr(e, "response", None) else ""
            print(f"FAIL {email:32} {e} {body}")
        time.sleep(0.25)   # HubSpot: не упираться в rate limit

    print(f"\nсоздано: {created} | обновлено: {updated} | "
          f"без email (пропущено): {skipped} | ошибок: {failed}")
    if skipped:
        print("Пропущенные — это строки, где адрес ещё не найден. "
              "Допиши email в CSV и запусти снова, дубликатов не будет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
