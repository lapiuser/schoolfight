import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)
DB = PROJECT / "data" / "test_leaderboard.db"
for p in (DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm")):
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///./data/test_leaderboard.db"
os.environ["TURNSTILE_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "pass"
os.environ["PUBLIC_COUNTRIES"] = "RU"

from fastapi.testclient import TestClient

from app.main import app


def test_gate_and_config():
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        cfg = c.get("/api/config")
        assert cfg.status_code == 200
        assert cfg.json()["turnstile_enabled"] is False
        assert cfg.json()["app_name"] == "БИТВА ШКОЛ"


def test_enter_country_and_stats():
    with TestClient(app) as c:
        r = c.post("/api/enter", json={"token": ""})
        assert r.status_code == 200
        countries = c.get("/api/countries")
        assert countries.status_code == 200
        codes = {x["code"] for x in countries.json()}
        assert codes == {"RU"}
        stats = c.get("/api/stats").json()
        assert stats["schools"] > 10000
        assert "online_now" in stats and "visits" in stats


def test_click_and_live_cps_support():
    with TestClient(app) as c:
        c.post("/api/enter", json={"token": ""})
        city = c.get("/api/cities?country=RU&q=Калининград").json()[0]
        types = c.get(f"/api/types?country=RU&city_id={city['id']}").json()
        general = next(x for x in types if x["code"] == "general")
        schools = c.get(f"/api/institutions/search?country=RU&city_id={city['id']}&type_code=general&q=средняя школа №21").json()
        assert len(schools) == 1
        school = schools[0]
        before = c.get(f"/api/institutions/{school['id']}").json()["real_clicks"]
        r = c.post("/api/clicks", json={"clicks":[{"institution_id":school["id"],"count":3}]})
        assert r.status_code == 200
        assert r.json()["accepted"] == 3
        after = c.get(f"/api/institutions/{school['id']}").json()["real_clicks"]
        assert after == before + 3


def test_admin_remove_votes_and_secret_login():
    with TestClient(app) as c:
        c.post("/api/enter", json={"token": ""})
        login = c.post("/ljungberg-access", json={"username": "admin", "password": "pass", "token": ""})
        assert login.status_code == 200
        school = c.get("/api/institutions/search?country=RU&city_id=88&type_code=general&q=средняя школа №21").json()[0]
        c.post("/api/clicks", json={"clicks":[{"institution_id":school["id"],"count":4}]})
        before = c.get(f"/api/institutions/{school['id']}").json()["real_clicks"]
        r = c.post(f"/api/admin/institutions/{school['id']}/remove-votes", json={"amount":2})
        assert r.status_code == 200
        after = c.get(f"/api/institutions/{school['id']}").json()["real_clicks"]
        assert after == before - 2


def test_seed_contains_no_garbage_or_kaliningrad_duplicate():
    import csv
    from app.data_import import is_garbage_name
    rows = list(csv.DictReader((PROJECT / "data" / "institutions.csv").open(encoding="utf-8")))
    assert not any(is_garbage_name(r["name"], r["type_code"]) for r in rows)
    schools_21 = [r for r in rows if r["city"] == "Калининград" and r["type_code"] == "general" and "21" in r["name"].replace(" ", "")]
    assert len(schools_21) == 1


def test_direct_app_requires_gate_session():
    with TestClient(app) as c:
        r = c.get('/app', follow_redirects=False)
        assert r.status_code == 303
        assert r.headers['location'] == '/'


def test_public_country_filtering():
    with TestClient(app) as c:
        c.post('/api/enter', json={'token': ''})
        countries = c.get('/api/countries').json()
        assert {x['code'] for x in countries} == {'RU'}
        assert all(x['country_code'] == 'RU' for x in c.get('/api/ranking?scope=schools').json()['items'])

