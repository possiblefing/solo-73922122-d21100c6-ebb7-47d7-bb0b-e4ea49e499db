"""通过 FastAPI TestClient 走完整 HTTP 链路，验证响应模型与错误码。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import Base
from app.main import app

from .conftest import TestingSessionLocal, engine


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 覆盖 get_session 依赖，使其使用测试引擎的会话。
    def _override():
        s = TestingSessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    from app.db import get_session
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


CSV = (
    "source,station_code,name\n"
    "INS-A,001,甲站\nINS-A,002,乙站\n"
    "INS-B,B-1,甲站B名\nINS-C,C-1,甲站C名\n"
).encode()


def _upload(client):
    r = client.post("/api/import", files={"file": ("s.csv", CSV, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_http_workflow(client):
    reports = _upload(client)
    assert sum(r["inserted"] for r in reports) == 4

    stations = client.get("/api/stations").json()
    assert len(stations) == 4
    a1 = next(s["id"] for s in stations if s["source_code_label"] == "INS-A/001")
    b1 = next(s["id"] for s in stations if s["source_code_label"] == "INS-B/B-1")
    c1 = next(s["id"] for s in stations if s["source_code_label"] == "INS-C/C-1")
    a2 = next(s["id"] for s in stations if s["source_code_label"] == "INS-A/002")

    # 同站合并。
    r = client.post("/api/evidences/same", json={"station_a_id": a1, "station_b_id": b1})
    assert r.status_code == 200, r.text
    number1 = r.json()["new_numbers"][0]

    # 异站判定。
    r = client.post("/api/evidences/diff", json={"station_a_id": a2, "station_b_id": b1})
    assert r.status_code == 200

    # 异站拦截合并，409 且带回冲突依据。
    r = client.post("/api/evidences/same", json={"station_a_id": a2, "station_b_id": c1})
    # c1 尚未与 b1 同组，先让 c1 入组再试。
    assert r.status_code == 200
    r = client.post("/api/evidences/same", json={"station_a_id": a2, "station_b_id": a1})
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"]

    # 组与号查询。
    groups = client.get("/api/groups").json()
    assert any(g["current_number"] for g in groups)
    look = client.get(f"/api/numbers/{number1}").json()
    assert look["number"] == number1
    assert len(look["snapshot_members"]) == 2

    # 撤销。
    ev = client.get("/api/evidences").json()
    same_id = ev["same"][0]["id"]
    r = client.post("/api/evidences/same/revoke", json={"evidence_id": same_id})
    assert r.status_code == 200
    assert r.json()["new_numbers"]  # 拆开后签发新号

    # 网页可打开。
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_import_bad_csv_returns_400(client):
    r = client.post("/api/import", files={"file": ("x.csv", b"a,b,c\n1,2,3", "text/csv")})
    assert r.status_code == 400


def test_revoke_unknown_404(client):
    _upload(client)
    r = client.post("/api/evidences/same/revoke", json={"evidence_id": 999})
    assert r.status_code == 404
