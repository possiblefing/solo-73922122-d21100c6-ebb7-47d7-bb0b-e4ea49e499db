"""真实 PostgreSQL 并发测试：行锁隔离、不丢依据、失败整次回滚。

仅在 PostgreSQL 下运行（SQLite 内存库无 FOR UPDATE 行锁与多连接语义）。
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.errors import ConflictError
from app.importer import import_stations_csv
from app import service
from app.models import DiffEvidence, GroupMember, SameEvidence, Station

from .conftest import TestingSessionLocal, dialect

pytestmark = pytest.mark.skipif(dialect != "postgresql", reason="并发测试需要 PostgreSQL 行锁")


def _fresh_session():
    # 每个线程使用独立连接/会话。
    return TestingSessionLocal()


def _seed(session):
    csv = (
        "source,station_code,name\n"
        + "".join(f"INS-A,SA{i},A站{i}\n" for i in range(1, 6))
        + "".join(f"INS-B,SB{i},B站{i}\n" for i in range(1, 6))
        + "".join(f"INS-C,SC{i},C站{i}\n" for i in range(1, 6))
    ).encode()
    import_stations_csv(session, csv)
    session.commit()
    return {
        f"{s.source_code}": s.id
        for s in session.scalars(select(Station))
    }


def _group_id(session, sid):
    return session.scalar(select(GroupMember.group_id).where(GroupMember.station_id == sid))


def test_parallel_disjoint_merges_all_succeed(session):
    """三组互不相交的合并并发执行：互不阻塞，全部成功，依据一条不丢。"""
    ids = _seed(session)
    jobs = [("SA1", "SB1"), ("SA2", "SB2"), ("SA3", "SB3")]
    errors: list[Exception] = []

    def worker(a, b):
        s = _fresh_session()
        try:
            service.confirm_same(s, ids[a], ids[b], "并发同站")
            s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    s = _fresh_session()
    try:
        assert s.scalar(select(func.count(SameEvidence.id)).where(SameEvidence.revoked.is_(False))) == 3
        for a, b in jobs:
            assert _group_id(s, ids[a]) == _group_id(s, ids[b])
    finally:
        s.close()


def test_concurrent_merge_against_diff_never_loses_evidence(session):
    """异站判定与合并并发：要么合并被拦、要么判定先到，最终约束必然生效，依据不丢。"""
    ids = _seed(session)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def merger():
        s = _fresh_session()
        barrier.wait()
        try:
            service.confirm_same(s, ids["SA1"], ids["SB1"], "并发合并")
            s.commit()
            outcomes.append("merged")
        except ConflictError:
            s.rollback()
            outcomes.append("blocked")
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            outcomes.append(f"error:{exc!r}")
        finally:
            s.close()

    def differ():
        s = _fresh_session()
        barrier.wait()
        try:
            service.confirm_diff(s, ids["SA1"], ids["SB1"], "并发异站")
            s.commit()
            outcomes.append("diffed")
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            outcomes.append(f"diff-error:{exc!r}")
        finally:
            s.close()

    t1 = threading.Thread(target=merger)
    t2 = threading.Thread(target=differ)
    t1.start(); t2.start(); t1.join(); t2.join()

    s = _fresh_session()
    try:
        diff_count = s.scalar(select(func.count(DiffEvidence.id)))
        same_count = s.scalar(
            select(func.count(SameEvidence.id)).where(SameEvidence.revoked.is_(False))
        )
        # 异站依据一定存在（不丢依据）。
        assert diff_count == 1
        # 同站与异站不能同时成立：若同站成功，则异站一定是后来被同组规则拒绝；
        # 若异站先成功，则同站合并必然被拦截。
        assert not (diff_count == 1 and same_count == 1 and
                    _group_id(s, ids["SA1"]) == _group_id(s, ids["SB1"]))
        if same_count == 1:
            assert "blocked" not in outcomes
        else:
            assert _group_id(s, ids["SA1"]) != _group_id(s, ids["SB1"])
    finally:
        s.close()


def test_concurrent_revoke_and_merge_keep_consistent_groups(session):
    """撤销与无关合并并发：最终每个站恰属于一个组，同站边与组划分一致。"""
    ids = _seed(session)
    s = _fresh_session()
    e = service.confirm_same(s, ids["SA1"], ids["SB1"], "先建组")
    s.commit()
    evidence_id = e["evidence_id"]
    s.close()

    barrier = threading.Barrier(2)

    def revoker():
        s = _fresh_session()
        barrier.wait()
        try:
            service.revoke_same(s, evidence_id, "并发撤销")
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
        finally:
            s.close()

    def merger():
        s = _fresh_session()
        barrier.wait()
        try:
            service.confirm_same(s, ids["SA2"], ids["SB2"], "无关组合并")
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
        finally:
            s.close()

    t1 = threading.Thread(target=revoker)
    t2 = threading.Thread(target=merger)
    t1.start(); t2.start(); t1.join(); t2.join()

    s = _fresh_session()
    try:
        # 每个站点恰好隶属一个组。
        total_stations = s.scalar(select(func.count(Station.id)))
        total_memberships = s.scalar(select(func.count(GroupMember.id)))
        assert total_memberships == total_stations
        # 撤销生效：SA1/SB1 不同组；无关合并生效：SA2/SB2 同组。
        assert _group_id(s, ids["SA1"]) != _group_id(s, ids["SB1"])
        assert _group_id(s, ids["SA2"]) == _group_id(s, ids["SB2"])
        # 有效同站边只剩一条。
        assert s.scalar(
            select(func.count(SameEvidence.id)).where(SameEvidence.revoked.is_(False))
        ) == 1
    finally:
        s.close()
