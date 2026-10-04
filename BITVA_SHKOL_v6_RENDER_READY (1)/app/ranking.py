from __future__ import annotations

from sqlalchemy import desc, func, or_, select

from .models import City, Country, Institution


def score_expr(model):
    return model.real_clicks + model.event_points


def _focus_rank(db, model, focus, city_id=None, country_id=None) -> int:
    if not focus:
        return 0
    stmt = select(func.count(model.id)).where(
        or_(
            score_expr(model) > focus.score,
            (score_expr(model) == focus.score) & (model.id < focus.id),
        )
    )
    if model is Institution:
        if city_id is not None:
            stmt = stmt.where(Institution.city_id == city_id)
        elif country_id is not None:
            stmt = stmt.join(City).where(City.country_id == country_id)
    elif model is City and country_id is not None:
        stmt = stmt.where(City.country_id == country_id)
    return int(db.scalar(stmt) or 0) + 1


def institution_payload(inst: Institution, rank: int, delta: int = 0, focus: bool = False, cps: float = 0):
    city = inst.city
    country = city.country
    return {
        "id": inst.id,
        "rank": rank,
        "country": country.name,
        "country_code": country.code,
        "flag": country.flag,
        "city": city.name,
        "name": inst.name,
        "type": inst.type_code,
        "score": inst.score,
        "real_clicks": inst.real_clicks,
        "delta_clicks": delta,
        "cps": cps,
        "focus": focus,
    }


def top_institutions(db, limit=100, focus_id=None, city_id=None, country_id=None, full=False, cps_map=None, delta_map=None):
    q = select(Institution)
    if city_id is not None:
        q = q.where(Institution.city_id == city_id)
    elif country_id is not None:
        q = q.join(City).where(City.country_id == country_id)
    q = q.order_by(desc(score_expr(Institution)), Institution.id)
    if full:
        rows = list(db.scalars(q.limit(min(max(limit, 1), 5000))).all())
    else:
        rows = list(db.scalars(q.limit(min(max(limit, 1), 100))).all())
    focus = db.get(Institution, focus_id) if focus_id else None
    out = []
    cps_map = cps_map or {}
    delta_map = delta_map or {}
    for i, inst in enumerate(rows, 1):
        out.append(
            institution_payload(
                inst,
                i,
                delta=delta_map.get(inst.id, 0),
                focus=(focus_id == inst.id),
                cps=cps_map.get(inst.id, 0),
            )
        )
    if focus and all(x.id != focus.id for x in rows):
        rank = _focus_rank(db, Institution, focus, city_id=city_id, country_id=country_id)
        out.append(
            institution_payload(
                focus,
                rank,
                delta=delta_map.get(focus.id, 0),
                focus=True,
                cps=cps_map.get(focus.id, 0),
            )
        )
    return out


def country_rankings(db, limit=100, focus_country_id=None, country_codes=None):
    q = select(Country)
    if country_codes:
        q = q.where(Country.code.in_(country_codes))
    rows = db.scalars(q.order_by(desc(score_expr(Country)), Country.id).limit(min(max(limit, 1), 100))).all()
    focus = db.get(Country, focus_country_id) if focus_country_id else None
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": r.id,
            "rank": i,
            "name": r.name,
            "code": r.code,
            "flag": r.flag,
            "score": score_expr(r),
            "real_clicks": r.real_clicks,
            "focus": bool(focus_country_id == r.id),
        })
    if focus and all(x["id"] != focus.id for x in rows):
        out.append({
            "id": focus.id,
            "rank": _focus_rank(db, Country, focus),
            "name": focus.name,
            "code": focus.code,
            "flag": focus.flag,
            "score": score_expr(focus),
            "real_clicks": focus.real_clicks,
            "focus": True,
        })
    return out


def city_rankings(db, limit=100, country_id=None, focus_city_id=None, country_codes=None):
    q = select(City)
    if country_id is not None:
        q = q.where(City.country_id == country_id)
    elif country_codes:
        q = q.join(Country).where(Country.code.in_(country_codes))
    q = q.order_by(desc(score_expr(City)), City.id)
    rows = db.scalars(q.limit(min(max(limit, 1), 100))).all()
    focus = db.get(City, focus_city_id) if focus_city_id else None
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "id": r.id,
            "rank": i,
            "name": r.name,
            "score": score_expr(r),
            "real_clicks": r.real_clicks,
            "country": r.country.name,
            "flag": r.country.flag,
            "focus": bool(focus_city_id == r.id),
        })
    if focus and all(x["id"] != focus.id for x in rows):
        out.append({
            "id": focus.id,
            "rank": _focus_rank(db, City, focus, country_id=country_id),
            "name": focus.name,
            "score": score_expr(focus),
            "real_clicks": focus.real_clicks,
            "country": focus.country.name,
            "flag": focus.country.flag,
            "focus": True,
        })
    return out
