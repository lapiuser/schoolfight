from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import func, select

from .db import SessionLocal
from .models import City, Country, Institution, InstitutionMedia, PhotoSubmission

COUNTRIES = {
    "RU": ("Россия", "🇷🇺"),
}

# Rows produced by parser UI elements rather than real institutions.
GARBAGE_EXACT = {
    "другие города",
    "по городам",
    "сбросить фильтр",
    "очно",
    "заочно",
    "очно заочно",
    "очно заочно ",
    "дистанционно",
    "ниу",
    "бюджет общежитие",
    "доступная среда овз",
    "после 9 класса",
    "с военной кафедрой",
}
GARBAGE_CONTAINS = (
    "баллом",
    "балл до",
    "проходные балл",
    "проходные в колледжи",
)


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\wа-яa-z0-9№]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    # Collapse obvious scraper separators / fragments.
    name = re.sub(r"\s+—\s+", " — ", name)
    return name.strip(" .,-")


def is_garbage_name(name: str, type_code: str) -> bool:
    n = norm(name)
    if not n:
        return True
    if n in GARBAGE_EXACT:
        return True
    if any(fragment in n for fragment in GARBAGE_CONTAINS):
        return True
    if type_code in {"university", "college"} and n in {
        "очно",
        "заочно",
        "очно заочно",
        "дистанционно",
        "после 9 класса",
        "бюджет общежитие",
        "доступная среда овз",
        "с военной кафедрой",
        "ниу",
    }:
        return True
    # Common parser garbage such as "школа с 4.0 баллом".
    if re.search(r"\b\d(?:[.,]\d)?\s*балл", n):
        return True
    return False


def school_number(name: str) -> str | None:
    m = re.search(r"(?:№|\bномер\b)\s*([0-9]{1,4})\b", name, re.IGNORECASE)
    return m.group(1) if m else None


def generic_school_signature(name: str) -> str | None:
    n = norm(name)
    number = school_number(n)
    if not number:
        return None
    # Only merge clear parser variants of ordinary schools; keep lyceums,
    # gymnasiums, correctional/sanatorium/boarding schools separate.
    protected = ("гимназ", "лицей", "началь", "коррекц", "санатор", "интернат", "вечер")
    if any(x in n for x in protected):
        return None
    n = re.sub(r"\b(маоу|мбоу|моу|гбоу|гкоу|муниципальн\w*|бюджетн\w*|автономн\w*)\b", " ", n)
    n = re.sub(r"(?:№|\bномер\b)\s*" + re.escape(number) + r"\b", " ", n)
    n = n.replace("сош", "средняя школа")
    n = re.sub(r"\bсредняя общеобразовательная школа\b", "школа", n)
    n = re.sub(r"\bобщеобразовательная школа\b", "школа", n)
    n = re.sub(r"\bсредняя школа\b", "школа", n)
    n = re.sub(r"\bшкола\b", "", n)
    n = re.sub(r"\bимени\b|\bим\b", "", n)
    n = re.sub(r"\s+", " ", n).strip(" .,-")
    if not n:
        return f"school#{number}"
    return f"school#{number}#{n}"


def prepare_seed_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    import csv

    unique: dict[tuple[str, str, str, str], dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            code = (raw.get("country_code") or "RU").strip() or "RU"
            if code != "RU":
                continue
            country = clean_name(raw.get("country") or "Россия") or "Россия"
            city = clean_name(raw.get("city") or "")
            typ = (raw.get("type_code") or raw.get("type") or "general").strip().lower() or "general"
            name = clean_name(raw.get("name") or "")
            if not city or is_garbage_name(name, typ):
                continue
            key = (code, city, typ, norm(name))
            prev = unique.get(key)
            if prev is None or len(name) > len(prev["name"]):
                unique[key] = {"country_code": code, "country": country, "city": city, "type_code": typ, "name": name}

    # Conservative second pass for clear school-number parser variants.
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    for row in unique.values():
        sig = generic_school_signature(row["name"]) if row["type_code"] in {"general", "private"} else None
        if sig:
            groups.setdefault((row["country_code"], row["city"], row["type_code"], sig), []).append(row)
    removed: set[int] = set()
    rows = list(unique.values())
    index_by_id = {id(r): r for r in rows}
    for members in groups.values():
        if len(members) < 2:
            continue
        # The signature already preserves the school number and meaningful
        # locality/name tokens. Names that differ only by parser abbreviations
        # (МАОУ СОШ vs Средняя школа) are therefore treated as one institution.
        canonical = max(members, key=lambda r: (len(r["name"]), r["name"]))
        for row in members:
            if row is not canonical:
                removed.add(id(row))
    cleaned = [r for r in rows if id(r) not in removed]
    cleaned.sort(key=lambda r: (r["city"].lower(), r["type_code"], norm(r["name"])))
    return cleaned


def seed_countries(db):
    for code, (name, flag) in COUNTRIES.items():
        row = db.scalar(select(Country).where(Country.code == code))
        if not row:
            db.add(Country(code=code, name=name, flag=flag))


def import_seed_csv(db, path: Path):
    if db.scalar(select(func.count(Institution.id))) or not path.exists():
        return 0
    countries = {c.code: c for c in db.scalars(select(Country)).all()}
    cities_cache: dict[tuple[str, str], City] = {}
    added = 0
    for row in prepare_seed_rows(path):
        code = row["country_code"]
        country = countries.get(code)
        if not country:
            cname, flag = COUNTRIES.get(code, (row["country"], "🌍"))
            country = Country(code=code, name=cname, flag=flag)
            db.add(country)
            db.flush()
            countries[code] = country
        key = (code, row["city"])
        city = cities_cache.get(key)
        if city is None:
            city = db.scalar(select(City).where(City.country_id == country.id, City.name == row["city"]))
            if not city:
                city = City(country_id=country.id, name=row["city"])
                db.add(city)
                db.flush()
            cities_cache[key] = city
        normalized = norm(row["name"])
        existing = db.scalar(select(Institution).where(
            Institution.city_id == city.id,
            Institution.type_code == row["type_code"],
            Institution.normalized_name == normalized,
        ))
        if existing:
            continue
        db.add(Institution(city_id=city.id, type_code=row["type_code"], name=row["name"], normalized_name=normalized))
        added += 1
        if added % 500 == 0:
            db.flush()
    db.flush()
    return added


def dedupe_database(db) -> int:
    """Remove exact normalized duplicates and conservative numbered-school aliases.
    Real clicks and event points are merged into the canonical row before deletion.
    """
    removed = 0
    # Exact normalized duplicates first.
    duplicate_keys = db.execute(
        select(Institution.city_id, Institution.type_code, Institution.normalized_name)
        .group_by(Institution.city_id, Institution.type_code, Institution.normalized_name)
        .having(func.count(Institution.id) > 1)
    ).all()
    for city_id, type_code, normalized in duplicate_keys:
        members = db.scalars(select(Institution).where(
            Institution.city_id == city_id,
            Institution.type_code == type_code,
            Institution.normalized_name == normalized,
        ).order_by(Institution.id)
        ).all()
        if len(members) < 2:
            continue
        keep = members[0]
        for dup in members[1:]:
            keep.real_clicks += dup.real_clicks
            keep.event_points += dup.event_points
            dup_media = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == dup.id))
            keep_media = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == keep.id))
            if dup_media and keep_media:
                db.delete(dup_media)
            elif dup_media:
                dup_media.institution_id = keep.id
            db.query(PhotoSubmission).filter_by(institution_id=dup.id).update({"institution_id": keep.id})
            db.delete(dup)
            removed += 1
    # Re-evaluate obvious school-number aliases in each city/type.
    rows = db.scalars(select(Institution).where(Institution.type_code.in_(["general", "private"]))).all()
    groups: dict[tuple[int, str], list[Institution]] = {}
    for inst in rows:
        sig = generic_school_signature(inst.name)
        if sig:
            groups.setdefault((inst.city_id, sig), []).append(inst)
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda x: (len(x.name), -x.id))
        for dup in members:
            if dup is keep:
                continue
            keep.real_clicks += dup.real_clicks
            keep.event_points += dup.event_points
            dup_media = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == dup.id))
            keep_media = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == keep.id))
            if dup_media and keep_media:
                db.delete(dup_media)
            elif dup_media:
                dup_media.institution_id = keep.id
            db.query(PhotoSubmission).filter_by(institution_id=dup.id).update({"institution_id": keep.id})
            db.delete(dup)
            removed += 1
    if removed:
        db.flush()
    return removed
