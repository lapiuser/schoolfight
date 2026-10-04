from __future__ import annotations

import asyncio
import csv
import io
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from .antibot import verify_turnstile
from .config import *
from .data_import import COUNTRIES, clean_name, dedupe_database, import_seed_csv, is_garbage_name, norm, seed_countries
from .db import Base, SessionLocal, engine
from .models import AdminAudit, AdminEvent, City, Country, Institution, InstitutionMedia, PhotoSubmission, SiteStats, Visitor
from .photo import sanitize_image, save_final
from .ranking import city_rankings, country_rankings, top_institutions
from .security import VISIT_LIMITER, client_ip, consume_rate_limit, hash_ip, session_token, validate_session

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
INDEX = STATIC_DIR / "index.html"

app = FastAPI(title=APP_NAME)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="bitva_admin",
    same_site="lax",
    https_only=APP_ENV == "production",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# WebSocket subscriptions let all users receive the same live snapshot without a
# separate HTTP ranking request every few seconds.
WS_CLIENTS: dict[WebSocket, dict] = {}
LIVE_LOCK = asyncio.Lock()
LIVE_CLICKS = 0
LIVE_CLICKS_BY_INST: dict[int, int] = {}
LAST_BROADCAST = datetime.utcnow()
LAST_CPS_BY_INST: dict[int, float] = {}
ONLINE_LOCK = asyncio.Lock()
ONLINE_SESSIONS: dict[str, float] = {}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_public_session(request: Request):
    token = request.cookies.get("ksl_session")
    ip = client_ip(request, TRUST_PROXY)
    ua = request.headers.get("user-agent", "")
    if not validate_session(token, ip, ua):
        raise HTTPException(401, "session_required")
    return True


def admin_required(request: Request):
    if not request.session.get("admin_auth"):
        raise HTTPException(401, "admin_auth_required")
    return True


def get_or_create_stats(db: Session) -> SiteStats:
    row = db.get(SiteStats, 1)
    if not row:
        row = SiteStats(id=1, total_visits=0)
        db.add(row)
        db.flush()
    return row


def visitor_hash(raw: str) -> str:
    return hash_ip("visitor|" + raw)


def ensure_visitor_token(request: Request) -> str:
    return request.cookies.get("bitva_visitor") or secrets.token_urlsafe(24)


async def register_visit_if_needed(request: Request, raw_visitor: str, db: Session) -> bool:
    vhash = visitor_hash(raw_visitor)
    exists = db.scalar(select(Visitor).where(Visitor.visitor_hash == vhash))
    if exists:
        return False
    ip_key = hash_ip(client_ip(request, TRUST_PROXY))
    if not await VISIT_LIMITER.allow(ip_key, VISIT_RATE_LIMIT_PER_IP, VISIT_RATE_WINDOW_SECONDS):
        return False
    db.add(Visitor(visitor_hash=vhash))
    stats = get_or_create_stats(db)
    stats.total_visits += 1
    db.commit()
    return True


async def touch_online(request: Request, token: str | None = None) -> int:
    raw = token or request.cookies.get("bitva_visitor") or request.cookies.get("ksl_session")
    if not raw:
        return 0
    key = hash_ip(raw)
    now = time.time()
    async with ONLINE_LOCK:
        ONLINE_SESSIONS[key] = now + ONLINE_TTL_SECONDS
        expired = [k for k, exp in ONLINE_SESSIONS.items() if exp <= now]
        for k in expired:
            ONLINE_SESSIONS.pop(k, None)
        return len(ONLINE_SESSIONS)


async def online_now() -> int:
    now = time.time()
    async with ONLINE_LOCK:
        expired = [k for k, exp in ONLINE_SESSIONS.items() if exp <= now]
        for k in expired:
            ONLINE_SESSIONS.pop(k, None)
        return len(ONLINE_SESSIONS)


def public_country_query(stmt):
    if PUBLIC_COUNTRIES:
        stmt = stmt.where(Country.code.in_(PUBLIC_COUNTRIES))
    return stmt


@app.on_event("startup")
async def startup():
    DATA_DIR.mkdir(exist_ok=True)
    STATIC_DIR.mkdir(exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_countries(db)
        db.commit()
        try:
            if engine.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_lock(7842231)"))
            removed = dedupe_database(db)
            added = import_seed_csv(db, DATA_DIR / "institutions.csv")
            get_or_create_stats(db)
            db.commit()
            if engine.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_unlock(7842231)"))
                db.commit()
            print(f"Seed cleanup removed: {removed}; institutions added: {added}")
        except Exception as e:
            db.rollback()
            print("Seed import/cleanup failed:", e)


@app.get("/", response_class=HTMLResponse)
async def gate():
    return (STATIC_DIR / "gate.html").read_text(encoding="utf-8")


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    # Do not allow a direct /app URL to bypass the entrance Turnstile gate.
    token = request.cookies.get("ksl_session")
    ip = client_ip(request, TRUST_PROXY)
    ua = request.headers.get("user-agent", "")
    if not validate_session(token, ip, ua):
        return RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
    return INDEX.read_text(encoding="utf-8")


@app.get("/legal", response_class=HTMLResponse)
async def legal():
    return (STATIC_DIR / "legal.html").read_text(encoding="utf-8")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/enter")
async def enter(request: Request, db: Session = Depends(db_dep)):
    body = await request.json()
    if TURNSTILE_ENABLED and APP_ENV == "production":
        ok = await verify_turnstile(body.get("token"), client_ip(request, TRUST_PROXY), "enter")
        if not ok:
            raise HTTPException(403, "Проверка Cloudflare не пройдена")
    ip = client_ip(request, TRUST_PROXY)
    ua = request.headers.get("user-agent", "")
    session = session_token(ip, ua)
    visitor = ensure_visitor_token(request)
    await register_visit_if_needed(request, visitor, db)
    online = await touch_online(request, visitor)
    response = JSONResponse({"ok": True, "online_now": online, "visits": get_or_create_stats(db).total_visits}, headers={"Cache-Control": "no-store"})
    response.set_cookie("ksl_session", session, max_age=SESSION_MINUTES * 60, httponly=True, samesite="lax", secure=APP_ENV == "production")
    response.set_cookie("bitva_visitor", visitor, max_age=VISIT_COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=APP_ENV == "production")
    return response


@app.get("/api/config")
async def public_config():
    return {
        "app_name": APP_NAME,
        "turnstile_enabled": TURNSTILE_ENABLED,
        "turnstile_site_key": TURNSTILE_SITE_KEY,
        "donation_url": DONATION_URL,
        "update_seconds": PUBLIC_UPDATE_SECONDS,
        "public_countries": sorted(PUBLIC_COUNTRIES),
        "admin_login_path": ADMIN_LOGIN_PATH,
        "admin_turnstile_required": bool(ADMIN_TURNSTILE_ENABLED and APP_ENV == "production"),
    }


@app.get("/api/heartbeat")
async def heartbeat(request: Request, db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    visitor = ensure_visitor_token(request)
    await register_visit_if_needed(request, visitor, db)
    online = await touch_online(request, visitor)
    response = JSONResponse({"online_now": online, "visits": get_or_create_stats(db).total_visits}, headers={"Cache-Control": "no-store"})
    response.set_cookie("bitva_visitor", visitor, max_age=VISIT_COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=APP_ENV == "production")
    return response


@app.get("/api/countries")
async def countries(db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    stmt = public_country_query(select(Country)).order_by(Country.name)
    rows = db.scalars(stmt).all()
    return [{"id": r.id, "code": r.code, "name": r.name, "flag": r.flag, "score": r.real_clicks + r.event_points} for r in rows]


@app.get("/api/cities")
async def cities(country: str, q: str = "", db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    country_row = db.scalar(select(Country).where(Country.code == country))
    if not country_row or (PUBLIC_COUNTRIES and country_row.code not in PUBLIC_COUNTRIES):
        return []
    q = norm(q.strip())
    stmt = select(City).where(City.country_id == country_row.id)
    rows = db.scalars(stmt.order_by(City.name)).all()
    if q:
        rows = [c for c in rows if q in norm(c.name)]
        rows.sort(key=lambda c: (0 if norm(c.name).startswith(q) else 1, norm(c.name)))
    rows = rows[:25]
    return [{"id": c.id, "name": c.name, "country": country_row.code, "count": len(c.institutions)} for c in rows]


@app.get("/api/types")
async def types(country: str | None = None, city_id: int | None = None, db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    stmt = select(Institution.type_code, func.count(Institution.id)).join(City).join(Country)
    if city_id is not None:
        visible_city = db.scalar(select(City).options(joinedload(City.country)).where(City.id == city_id))
        if not visible_city or (PUBLIC_COUNTRIES and visible_city.country.code not in PUBLIC_COUNTRIES):
            return []
        stmt = stmt.where(City.id == city_id)
    elif country:
        if PUBLIC_COUNTRIES and country not in PUBLIC_COUNTRIES:
            return []
        stmt = stmt.where(Country.code == country)
    elif PUBLIC_COUNTRIES:
        stmt = stmt.where(Country.code.in_(PUBLIC_COUNTRIES))
    stmt = stmt.group_by(Institution.type_code).order_by(func.count(Institution.id).desc())
    rows = db.execute(stmt).all()
    return [{"code": t, "name_ru": TYPE_LABELS_RU.get(t, t), "name_en": TYPE_LABELS_EN.get(t, t), "count": int(c)} for t, c in rows]


@app.get("/api/institutions/search")
async def institution_search(country: str, city_id: int, type_code: str | None = None, q: str = "", db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    city = db.get(City, city_id)
    if not city or city.country.code != country or (PUBLIC_COUNTRIES and country not in PUBLIC_COUNTRIES):
        return []
    stmt = select(Institution).where(Institution.city_id == city_id)
    if type_code:
        stmt = stmt.where(Institution.type_code == type_code)
    q = norm(q.strip())
    if q:
        stmt = stmt.where(Institution.normalized_name.ilike(f"%{q}%"))
        order = case((Institution.normalized_name.ilike(f"{q}%"), 0), else_=1)
    else:
        order = Institution.name
    rows = db.scalars(stmt.order_by(order, Institution.name).limit(60)).all()
    return [{"id": r.id, "name": r.name, "type": r.type_code, "score": r.score} for r in rows]


@app.get("/api/institutions/{institution_id}")
async def institution(institution_id: int, db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    r = db.scalar(select(Institution).options(joinedload(Institution.city).joinedload(City.country)).where(Institution.id == institution_id))
    if not r or (PUBLIC_COUNTRIES and r.city.country.code not in PUBLIC_COUNTRIES):
        raise HTTPException(404, "institution_not_found")
    media = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == r.id))
    cps = LAST_CPS_BY_INST.get(r.id, 0)
    return {
        "id": r.id,
        "country": {"name": r.city.country.name, "code": r.city.country.code, "flag": r.city.country.flag},
        "city": {"id": r.city.id, "name": r.city.name},
        "type": r.type_code,
        "type_label": TYPE_LABELS_RU.get(r.type_code, r.type_code),
        "name": r.name,
        "score": r.score,
        "real_clicks": r.real_clicks,
        "cps": cps,
        "photos": {
            "desktop": bool(media and (media.desktop_bytes or media.desktop_path)),
            "mobile": bool(media and (media.mobile_bytes or media.mobile_path)),
        },
    }


@app.get("/api/media/{institution_id}/{kind}")
async def media(institution_id: int, kind: str, db: Session = Depends(db_dep)):
    if kind not in {"desktop", "mobile"}:
        raise HTTPException(400)
    m = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == institution_id))
    if not m:
        raise HTTPException(404)
    data = getattr(m, f"{kind}_bytes")
    path = getattr(m, f"{kind}_path")
    if data:
        return Response(data, media_type=getattr(m, f"{kind}_mime") or "image/jpeg", headers={"Cache-Control": "public,max-age=3600"})
    if path and Path(path).exists():
        return Response(Path(path).read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "public,max-age=3600"})
    raise HTTPException(404)


@app.post("/api/photo-submissions")
async def submit_photo(
    request: Request,
    institution_id: int = Form(...),
    consent: bool = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(db_dep),
    _: bool = Depends(get_public_session),
):
    if not consent:
        raise HTTPException(400, "consent_required")
    ip = client_ip(request, TRUST_PROXY)
    ip_hash = hash_ip(ip)
    recent = db.scalar(select(func.count(PhotoSubmission.id)).where(PhotoSubmission.uploader_ip_hash == ip_hash, PhotoSubmission.created_at > datetime.utcnow() - timedelta(hours=1))) or 0
    if recent >= 5:
        raise HTTPException(429, "Слишком много заявок на фото")
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, "file_too_large")
    try:
        clean, mime = sanitize_image(data)
    except Exception as e:
        raise HTTPException(400, str(e))
    inst = db.get(Institution, institution_id)
    if not inst or (PUBLIC_COUNTRIES and inst.city.country.code not in PUBLIC_COUNTRIES):
        raise HTTPException(404)
    sub = PhotoSubmission(
        institution_id=institution_id,
        image_bytes=clean,
        mime=mime,
        original_name=(file.filename or "upload.jpg")[:255],
        uploader_ip_hash=ip_hash,
        consent_at=datetime.utcnow(),
    )
    db.add(sub)
    db.commit()
    return {"ok": True, "message": "Фото отправлено на модерацию"}


@app.get("/api/ranking")
async def ranking(
    scope: str = "schools",
    limit: int = 100,
    focus_id: int | None = None,
    country_id: int | None = None,
    city_id: int | None = None,
    db: Session = Depends(db_dep),
    _: bool = Depends(get_public_session),
):
    if scope == "city_schools":
        if city_id is None:
            raise HTTPException(400, "city_id_required")
        selected_city = db.scalar(select(City).options(joinedload(City.country)).where(City.id == city_id))
        if not selected_city or (PUBLIC_COUNTRIES and selected_city.country.code not in PUBLIC_COUNTRIES):
            raise HTTPException(404, "city_not_found")
        return {"scope": scope, "items": top_institutions(db, 5000, focus_id, city_id=city_id, full=True, cps_map=LAST_CPS_BY_INST)}
    limit = min(max(limit, 1), 100)
    if scope == "schools":
        return {"scope": scope, "items": top_institutions(db, limit, focus_id, cps_map=LAST_CPS_BY_INST)}
    if scope == "country_schools":
        if country_id is None or not db.scalar(select(Country.id).where(Country.id == country_id, Country.code.in_(PUBLIC_COUNTRIES))):
            raise HTTPException(404, "country_not_found")
        return {"scope": scope, "items": top_institutions(db, limit, focus_id, country_id=country_id, cps_map=LAST_CPS_BY_INST)}
    if scope == "countries":
        return {"scope": scope, "items": country_rankings(db, limit, focus_id, country_codes=PUBLIC_COUNTRIES)}
    if scope == "cities":
        return {"scope": scope, "items": city_rankings(db, limit, focus_city_id=focus_id, country_codes=PUBLIC_COUNTRIES)}
    if scope == "country_cities":
        if country_id is None or not db.scalar(select(Country.id).where(Country.id == country_id, Country.code.in_(PUBLIC_COUNTRIES))):
            raise HTTPException(404, "country_not_found")
        return {"scope": scope, "items": city_rankings(db, limit, country_id=country_id, focus_city_id=focus_id, country_codes=PUBLIC_COUNTRIES)}
    raise HTTPException(400, "unknown_scope")


async def _stats_fast(db: Session):
    schools = int(db.scalar(select(func.count(Institution.id)).join(City).join(Country).where(Country.code.in_(PUBLIC_COUNTRIES))) or 0) if PUBLIC_COUNTRIES else int(db.scalar(select(func.count(Institution.id))) or 0)
    countries_count = int(db.scalar(public_country_query(select(func.count(Country.id)))) or 0)
    cities_count = int(db.scalar(select(func.count(City.id)).join(Country).where(Country.code.in_(PUBLIC_COUNTRIES))) or 0) if PUBLIC_COUNTRIES else int(db.scalar(select(func.count(City.id))) or 0)
    total_real = int(db.scalar(select(func.sum(Institution.real_clicks)).join(City).join(Country).where(Country.code.in_(PUBLIC_COUNTRIES))) or 0) if PUBLIC_COUNTRIES else int(db.scalar(select(func.sum(Institution.real_clicks))) or 0)
    active_schools = int(db.scalar(select(func.count(Institution.id)).join(City).join(Country).where(Country.code.in_(PUBLIC_COUNTRIES), Institution.real_clicks > 0)) or 0) if PUBLIC_COUNTRIES else int(db.scalar(select(func.count(Institution.id)).where(Institution.real_clicks > 0)) or 0)
    first_stmt = select(Institution).join(City).join(Country).where(Country.code.in_(PUBLIC_COUNTRIES)).order_by((Institution.real_clicks + Institution.event_points).desc(), Institution.id).limit(1)
    first = db.scalar(first_stmt)
    site = get_or_create_stats(db)
    return {
        "schools": schools,
        "countries": countries_count,
        "cities": cities_count,
        "total_real": total_real,
        "active_schools": active_schools,
        "first": first.name if first else "—",
        "online_now": await online_now(),
        "visits": site.total_visits,
    }


@app.get("/api/stats")
async def stats(db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    return await _stats_fast(db)


@app.post("/api/clicks")
async def clicks(request: Request, db: Session = Depends(db_dep), _: bool = Depends(get_public_session)):
    global LIVE_CLICKS
    body = await request.json()
    items = body.get("clicks") or []
    if not isinstance(items, list) or len(items) > 25:
        raise HTTPException(400, "invalid_batch")
    total = 0
    parsed: list[tuple[int, int]] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        try:
            iid = int(x.get("institution_id", 0))
            count = int(x.get("count", 0))
        except (TypeError, ValueError):
            continue
        count = max(0, min(count, MAX_BATCH_CLICKS))
        if iid > 0 and count:
            parsed.append((iid, count))
            total += count
    if total < 1 or total > MAX_BATCH_CLICKS:
        raise HTTPException(400, "batch_limit")
    ip = client_ip(request, TRUST_PROXY)
    raw_session = request.cookies.get("ksl_session") or ""
    session_key = hash_ip(raw_session or f"{ip}|{request.headers.get('user-agent','')}")
    ip_key = hash_ip(ip)
    accepted = 0
    accepted_by_inst: dict[int, int] = {}
    active = db.scalar(select(AdminEvent).where(AdminEvent.active == True, AdminEvent.ends_at > datetime.utcnow()).order_by(AdminEvent.id.desc()).limit(1))
    if active is None:
        # Expire old event rows opportunistically.
        for old in db.scalars(select(AdminEvent).where(AdminEvent.active == True, AdminEvent.ends_at <= datetime.utcnow())).all():
            old.active = False
    event_delta = active.per_click if active else 1
    event_adjust = event_delta - 1 if active else 0
    for iid, count in parsed:
        inst = db.get(Institution, iid)
        if not inst or (PUBLIC_COUNTRIES and inst.city.country.code not in PUBLIC_COUNTRIES):
            continue
        if not await consume_rate_limit(ip_key, session_key, iid, count):
            continue
        accepted += count
        accepted_by_inst[iid] = accepted_by_inst.get(iid, 0) + count
        inst.real_clicks += count
        if event_delta != 1:
            inst.event_points += count * event_adjust
            inst.city.event_points += count * event_adjust
            inst.city.country.event_points += count * event_adjust
        inst.city.real_clicks += count
        inst.city.country.real_clicks += count
    db.commit()
    async with LIVE_LOCK:
        LIVE_CLICKS += accepted
        for iid, count in accepted_by_inst.items():
            LIVE_CLICKS_BY_INST[iid] = LIVE_CLICKS_BY_INST.get(iid, 0) + count
    return {
        "accepted": accepted,
        "event": {
            "active": bool(active),
            "per_click": active.per_click if active else 1,
            "title": active.title if active else "",
            "message": active.message if active else "",
            "id": active.id if active else None,
            "has_audio": bool(active and active.audio_bytes),
        },
    }


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    token = ws.cookies.get("ksl_session")
    ip = ws.headers.get("CF-Connecting-IP", "").strip() if TRUST_PROXY else (ws.client.host if ws.client else "0.0.0.0")
    ua = ws.headers.get("user-agent", "")
    if not validate_session(token, ip, ua):
        await ws.close(code=4401)
        return
    await ws.accept()
    WS_CLIENTS[ws] = {"scope": "schools", "country_id": None, "city_id": None, "focus_id": None}
    await ws.send_json({"type": "ready", "update_seconds": PUBLIC_UPDATE_SECONDS})
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            if msg.get("subscribe"):
                WS_CLIENTS[ws] = {
                    "scope": str(msg.get("scope") or "schools"),
                    "country_id": int(msg["country_id"]) if msg.get("country_id") else None,
                    "city_id": int(msg["city_id"]) if msg.get("city_id") else None,
                    "focus_id": int(msg["focus_id"]) if msg.get("focus_id") else None,
                }
    except WebSocketDisconnect:
        WS_CLIENTS.pop(ws, None)
    except Exception:
        WS_CLIENTS.pop(ws, None)


async def build_live_payload(
    sub: dict,
    db: Session,
    elapsed: float,
    clicks_window: int,
    cps_window: dict[int, int],
    base_stats: dict,
    active_event,
):
    scope = sub.get("scope", "schools")
    focus_id = sub.get("focus_id")
    country_id = sub.get("country_id")
    city_id = sub.get("city_id")
    if scope == "city_schools":
        items = top_institutions(
            db, 5000, focus_id, city_id=city_id, full=True,
            cps_map=LAST_CPS_BY_INST, delta_map=cps_window
        )
    elif scope == "country_schools":
        items = top_institutions(
            db, 100, focus_id, country_id=country_id,
            cps_map=LAST_CPS_BY_INST, delta_map=cps_window
        )
    elif scope == "countries":
        items = country_rankings(db, 100, focus_id)
    elif scope == "cities":
        items = city_rankings(db, 100, focus_city_id=focus_id)
    elif scope == "country_cities":
        items = city_rankings(db, 100, country_id=country_id, focus_city_id=focus_id)
    else:
        items = top_institutions(
            db, 100, focus_id, cps_map=LAST_CPS_BY_INST, delta_map=cps_window
        )
    current = db.get(Institution, focus_id) if focus_id else None
    stats = dict(base_stats)
    if current:
        stats["focus"] = {
            "id": current.id,
            "score": current.score,
            "real_clicks": current.real_clicks,
            "cps": LAST_CPS_BY_INST.get(current.id, 0),
        }
    return {
        "type": "snapshot",
        "leaderboard": items,
        "cps": round(clicks_window / max(elapsed, 0.5), 2),
        "stats": stats,
        "event": (
            {
                "id": active_event.id,
                "title": active_event.title,
                "per_click": active_event.per_click,
                "message": active_event.message,
                "ends_at": active_event.ends_at.isoformat(),
                "has_audio": bool(active_event.audio_bytes),
            }
            if active_event
            else None
        ),
    }


@app.on_event("startup")
async def start_broadcast():
    asyncio.create_task(broadcast_loop())


async def broadcast_loop():
    global LIVE_CLICKS, LAST_BROADCAST, LIVE_CLICKS_BY_INST, LAST_CPS_BY_INST
    await asyncio.sleep(0.5)
    while True:
        started = datetime.utcnow()
        await asyncio.sleep(max(PUBLIC_UPDATE_SECONDS, 2))
        async with LIVE_LOCK:
            clicks_window = LIVE_CLICKS
            cps_window = LIVE_CLICKS_BY_INST
            LIVE_CLICKS = 0
            LIVE_CLICKS_BY_INST = {}
        elapsed = max((datetime.utcnow() - LAST_BROADCAST).total_seconds(), 0.5)
        LAST_BROADCAST = datetime.utcnow()
        LAST_CPS_BY_INST = {iid: round(count / elapsed, 2) for iid, count in cps_window.items()}
        db = SessionLocal()
        try:
            # Expensive aggregate stats and event lookup are calculated once per
            # broadcast, not once for every distinct city/rating subscription.
            base_stats = await _stats_fast(db)
            active_event = db.scalar(
                select(AdminEvent)
                .where(AdminEvent.active == True, AdminEvent.ends_at > datetime.utcnow())
                .order_by(AdminEvent.id.desc())
                .limit(1)
            )
            groups: dict[tuple, list[WebSocket]] = {}
            for ws, sub in list(WS_CLIENTS.items()):
                key = (sub.get("scope"), sub.get("country_id"), sub.get("city_id"), sub.get("focus_id"))
                groups.setdefault(key, []).append(ws)
            for key, clients in groups.items():
                sub = {
                    "scope": key[0],
                    "country_id": key[1],
                    "city_id": key[2],
                    "focus_id": key[3],
                }
                payload = await build_live_payload(
                    sub, db, elapsed, clicks_window, cps_window, base_stats, active_event
                )
                dead = []
                for ws in clients:
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    WS_CLIENTS.pop(ws, None)
        finally:
            db.close()


@app.get(ADMIN_LOGIN_PATH, response_class=HTMLResponse)
async def admin_login():
    return (STATIC_DIR / "admin_login.html").read_text(encoding="utf-8")


@app.post(ADMIN_LOGIN_PATH)
async def admin_login_post(request: Request):
    ip = client_ip(request, TRUST_PROXY)
    now = time.time()
    attempts = LOGIN_ATTEMPTS.setdefault(ip, [])
    attempts[:] = [t for t in attempts if now - t < 60]
    if len(attempts) >= 5:
        raise HTTPException(429, "Слишком много попыток входа")
    attempts.append(now)
    data = await request.json()
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    token = data.get("token")
    # Local development does not require an external Turnstile challenge.
    # Production always verifies it server-side.
    if ADMIN_TURNSTILE_ENABLED and APP_ENV == "production":
        if not await verify_turnstile(token, ip, "admin_login"):
            raise HTTPException(403, "Проверка Cloudflare не пройдена")
    if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
        request.session.clear()
        request.session["admin_auth"] = True
        LOGIN_ATTEMPTS.pop(ip, None)
        return {"ok": True}
    raise HTTPException(401, "Неверные данные")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not request.session.get("admin_auth"):
        return RedirectResponse(ADMIN_LOGIN_PATH, status_code=303)
    return (STATIC_DIR / "admin.html").read_text(encoding="utf-8")


@app.get("/api/admin/photo-submissions")
async def admin_submissions(db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    rows = db.scalars(
        select(PhotoSubmission)
        .options(joinedload(PhotoSubmission.institution))
        .where(PhotoSubmission.status == "pending")
        .order_by(PhotoSubmission.created_at.asc())
        .limit(100)
    ).all()
    return [
        {
            "id": r.id,
            "institution_id": r.institution_id,
            "institution": r.institution.name,
            "city": r.institution.city.name,
            "created_at": r.created_at.isoformat(),
            "original_name": r.original_name,
        }
        for r in rows
    ]


@app.get("/api/admin/photo-submissions/{submission_id}/download")
async def admin_submission_download(submission_id: int, db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    r = db.get(PhotoSubmission, submission_id)
    if not r:
        raise HTTPException(404)
    return Response(r.image_bytes, media_type=r.mime, headers={"Content-Disposition": f'attachment; filename="submission-{r.id}.jpg"'})


@app.post("/api/admin/photo-submissions/{submission_id}/reject")
async def admin_reject(submission_id: int, request: Request, db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    data = await request.json()
    r = db.get(PhotoSubmission, submission_id)
    if not r:
        raise HTTPException(404)
    r.status = "rejected"
    r.moderator_note = str(data.get("note", ""))[:1000]
    db.add(AdminAudit(action="photo_reject", target=str(submission_id), ip_hash=hash_ip(client_ip(request, TRUST_PROXY))))
    db.commit()
    return {"ok": True}


@app.post("/api/admin/photo-submissions/{submission_id}/approve")
async def admin_approve(
    submission_id: int,
    request: Request,
    desktop: UploadFile = File(...),
    mobile: UploadFile = File(...),
    db: Session = Depends(db_dep),
    _: bool = Depends(admin_required),
):
    r = db.get(PhotoSubmission, submission_id)
    if not r:
        raise HTTPException(404)
    inst = db.get(Institution, r.institution_id)
    if not inst:
        raise HTTPException(404)
    d = await desktop.read()
    m = await mobile.read()
    if len(d) > MAX_UPLOAD_MB * 1024 * 1024 or len(m) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413)
    try:
        d, mime1 = sanitize_image(d)
        m, mime2 = sanitize_image(m)
    except Exception as e:
        raise HTTPException(400, str(e))
    media_row = db.scalar(select(InstitutionMedia).where(InstitutionMedia.institution_id == inst.id))
    if not media_row:
        media_row = InstitutionMedia(institution_id=inst.id)
        db.add(media_row)
    if MEDIA_BACKEND == "filesystem":
        media_row.desktop_path = save_final(inst.id, "desktop", d, mime1)
        media_row.mobile_path = save_final(inst.id, "mobile", m, mime2)
        media_row.desktop_bytes = None
        media_row.mobile_bytes = None
    else:
        media_row.desktop_bytes = d
        media_row.mobile_bytes = m
        media_row.desktop_path = None
        media_row.mobile_path = None
    media_row.desktop_mime = mime1
    media_row.mobile_mime = mime2
    r.status = "approved"
    db.add(AdminAudit(action="photo_approve", target=str(submission_id), ip_hash=hash_ip(client_ip(request, TRUST_PROXY))))
    db.commit()
    return {"ok": True}


@app.post("/api/admin/events")
async def create_event(
    request: Request,
    title: str = Form("Админское событие"),
    duration_minutes: int = Form(...),
    per_click: int = Form(...),
    message: str = Form(""),
    audio: UploadFile | None = File(None),
    db: Session = Depends(db_dep),
    _: bool = Depends(admin_required),
):
    if duration_minutes < 1 or duration_minutes > 240:
        raise HTTPException(400, "duration")
    if per_click == 0 or abs(per_click) > 1000:
        raise HTTPException(400, "per_click")
    audio_data = None
    audio_mime = None
    if audio and audio.filename:
        audio_data = await audio.read()
        if len(audio_data) > MAX_EVENT_AUDIO_MB * 1024 * 1024:
            raise HTTPException(413)
        if audio.content_type not in {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg"}:
            raise HTTPException(400, "Только MP3/WAV/OGG")
        audio_mime = audio.content_type
    for e in db.scalars(select(AdminEvent).where(AdminEvent.active == True)).all():
        e.active = False
    now = datetime.utcnow()
    ev = AdminEvent(
        title=title[:160],
        duration_seconds=duration_minutes * 60,
        per_click=per_click,
        message=message[:1000],
        audio_bytes=audio_data,
        audio_mime=audio_mime,
        started_at=now,
        ends_at=now + timedelta(minutes=duration_minutes),
        active=True,
    )
    db.add(ev)
    db.add(AdminAudit(action="event_create", target=title[:160], ip_hash=hash_ip(client_ip(request, TRUST_PROXY))))
    db.commit()
    return {"ok": True, "id": ev.id}


@app.post("/api/admin/events/stop")
async def stop_event(request: Request, db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    ev = db.scalar(select(AdminEvent).where(AdminEvent.active == True).order_by(AdminEvent.id.desc()).limit(1))
    if ev:
        ev.active = False
    db.add(AdminAudit(action="event_stop", target=str(ev.id if ev else ""), ip_hash=hash_ip(client_ip(request, TRUST_PROXY))))
    db.commit()
    return {"ok": True}


@app.get("/api/public/event-audio/{event_id}")
async def event_audio(event_id: int, db: Session = Depends(db_dep)):
    e = db.get(AdminEvent, event_id)
    if not e or not e.audio_bytes:
        raise HTTPException(404)
    return Response(e.audio_bytes, media_type=e.audio_mime or "audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/admin/overview")
async def admin_overview(db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    return await _stats_fast(db)


@app.get("/api/admin/institutions/search")
async def admin_institution_search(q: str = "", db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    q = norm(q.strip())
    if not q:
        return []
    rows = db.scalars(
        select(Institution)
        .join(City)
        .join(Country)
        .where(Country.code.in_(PUBLIC_COUNTRIES), Institution.normalized_name.ilike(f"%{q}%"))
        .order_by(Institution.name)
        .limit(30)
    ).all()
    return [{"id": r.id, "name": r.name, "city": r.city.name, "score": r.score, "real_clicks": r.real_clicks} for r in rows]


@app.post("/api/admin/institutions/{institution_id}/remove-votes")
async def remove_votes(institution_id: int, request: Request, db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    data = await request.json()
    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount < 1 or amount > 10_000_000:
        raise HTTPException(400, "Некорректное количество")
    inst = db.get(Institution, institution_id)
    if not inst:
        raise HTTPException(404)
    removed = min(inst.real_clicks, amount)
    inst.real_clicks -= removed
    inst.city.real_clicks = max(0, inst.city.real_clicks - removed)
    inst.city.country.real_clicks = max(0, inst.city.country.real_clicks - removed)
    db.add(AdminAudit(action="votes_remove", target=f"{institution_id}:{removed}", ip_hash=hash_ip(client_ip(request, TRUST_PROXY))))
    db.commit()
    return {"ok": True, "removed": removed, "remaining_real_clicks": inst.real_clicks}


@app.post("/api/admin/import")
async def admin_import(file: UploadFile = File(...), db: Session = Depends(db_dep), _: bool = Depends(admin_required)):
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413)
    text_data = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_data))
    required = {"country", "city", "type", "name"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise HTTPException(400, "CSV columns: country,city,type,name")
    added = 0
    countries = {c.name: c for c in db.scalars(select(Country)).all()}
    for row in reader:
        cname = clean_name(row.get("country", ""))
        city_name = clean_name(row.get("city", ""))
        typ = row.get("type", "").strip()
        name = clean_name(row.get("name", ""))
        if not cname or not city_name or not name or is_garbage_name(name, typ):
            continue
        code = (row.get("country_code") or "").strip().upper()
        if not code:
            code = next((k for k, v in COUNTRIES.items() if v[0] == cname), "")
        country = countries.get(cname)
        if not country:
            used = {c.code for c in countries.values()}
            if not code:
                idx = 1
                while f"C{idx}" in used:
                    idx += 1
                code = f"C{idx}"
            country = Country(code=code, name=cname, flag=(next((v[1] for k, v in COUNTRIES.items() if k == code), "🌍")))
            db.add(country)
            db.flush()
            countries[cname] = country
        city = db.scalar(select(City).where(City.country_id == country.id, City.name == city_name))
        if not city:
            city = City(country_id=country.id, name=city_name)
            db.add(city)
            db.flush()
        type_map = {
            "Школа/Лицей/Гимназия": "general",
            "Школа-интернат": "boarding",
            "Частная школа": "private",
            "Колледж/Техникум": "college",
            "ВУЗ": "university",
        }
        type_code = type_map.get(typ, typ or "general")
        normalized = norm(name)
        if db.scalar(select(Institution).where(Institution.city_id == city.id, Institution.type_code == type_code, Institution.normalized_name == normalized)):
            continue
        db.add(Institution(city_id=city.id, type_code=type_code, name=name, normalized_name=normalized))
        added += 1
        if added % 500 == 0:
            db.flush()
    db.commit()
    return {"ok": True, "added": added}
