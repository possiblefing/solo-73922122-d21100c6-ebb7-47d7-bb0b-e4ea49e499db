"""关键功能自测：导入、同站传递、异站约束、撤销重分组、统一号版本、事务原子性。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.errors import ConflictError, NotFoundError, ValidationError
from app.importer import import_stations_csv
from app import queries, service
from app.models import (
    CurrentGroup,
    DiffEvidence,
    GroupMember,
    SameEvidence,
    Station,
    UnifiedNumber,
)

CSV_A = (
    "source,station_code,name\n"
    "INS-A,001,城北观测站\n"
    "INS-A,002,滨河观测站\n"
    "INS-B,B-11,城北站旧名\n"
    "INS-B,B-12,临河区观测点\n"
    "INS-C,CX-7,城北监测站\n"
    "INS-C,CX-8,滨河监测点\n"
).encode()


def _ids(session):
    return {
        f"{s.source_id}:{s.source_code}": s.id
        for s in session.scalars(select(Station))
    }


def _group_of(session, station_id):
    return session.scalar(
        select(GroupMember.group_id).where(GroupMember.station_id == station_id)
    )


def _count_groups(session):
    return session.scalar(select(func.count(CurrentGroup.id)))


# ---------------------------------------------------------------- 导入
def test_import_creates_independent_groups_and_numbers(session):
    reports = import_stations_csv(session, CSV_A)
    total_inserted = sum(r["inserted"] for r in reports)
    assert total_inserted == 6
    # 初始每站独立成组、每站一个统一号。
    assert _count_groups(session) == 6
    assert session.scalar(select(func.count(UnifiedNumber.id))) == 6
    numbers = session.scalars(select(UnifiedNumber.number)).all()
    assert numbers == [f"U-{i:06d}" for i in range(1, 7)]


def test_import_is_idempotent_and_immutable(session):
    import_stations_csv(session, CSV_A)
    # 同文件再导：全部跳过，不新增组/号。
    reports2 = import_stations_csv(session, CSV_A)
    assert all(r["inserted"] == 0 and r["skipped_existing"] == r["total_for_source"] for r in reports2)
    assert _count_groups(session) == 6

    # 名称冲突：原记录不改，整次失败。
    bad = CSV_A.replace("城北观测站".encode(), "被篡改的名字".encode())
    with pytest.raises(ConflictError) as ei:
        import_stations_csv(session, bad)
    assert "immutable" in str(ei.value.details)
    station = session.scalar(select(Station).where(Station.source_code == "001"))
    assert station.name == "城北观测站"


def test_import_limit_30_per_source(session):
    rows = "source,station_code,name\n" + "".join(
        f"INS-X,S{i:03d},站{i}\n" for i in range(1, 32)
    )
    with pytest.raises(ValidationError) as ei:
        import_stations_csv(session, rows.encode())
    assert "30" in ei.value.message
    # 整次回滚：一个站都没进库。
    assert session.scalar(select(func.count(Station.id))) == 0


def test_import_bad_header_and_duplicate_rows(session):
    with pytest.raises(ValidationError):
        import_stations_csv(session, b"foo,bar,baz\n1,2,3\n")
    dup = (
        "source,station_code,name\n"
        "INS-A,001,甲\nINS-A,001,乙\n"
    ).encode()
    with pytest.raises(ValidationError):
        import_stations_csv(session, dup)


def test_import_chinese_header(session):
    csv_zh = "来源,源站号,名称\nINS-Z,Z-1,中文表头站\n".encode("utf-8-sig")
    reports = import_stations_csv(session, csv_zh)
    assert reports[0]["inserted"] == 1
    station = session.scalar(select(Station).where(Station.source_code == "Z-1"))
    assert station.name == "中文表头站"


# ---------------------------------------------------------------- 同站传递
def test_same_merge_transitive_and_new_number(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11, cx7 = ids["1:001"], ids["2:B-11"], ids["3:CX-7"]

    r1 = service.confirm_same(session, a1, b11, "名称相似")
    assert len(r1["new_numbers"]) == 1
    merged_number = r1["new_numbers"][0]
    g = _group_of(session, a1)
    assert _group_of(session, b11) == g

    # 再并入第三站：同站关系传递成组，三站同组，再次签发新号。
    r2 = service.confirm_same(session, b11, cx7, None)
    # 合并作废旧组、签发新组，三站应在同一个新组内。
    g2 = _group_of(session, a1)
    assert _group_of(session, b11) == g2
    assert _group_of(session, cx7) == g2
    assert g2 != g
    assert len(r2["new_numbers"]) == 1
    assert r2["new_numbers"][0] != merged_number
    members = session.scalars(
        select(GroupMember.station_id).where(GroupMember.group_id == g2)
    ).all()
    assert sorted(members) == sorted([a1, b11, cx7])


def test_same_group_one_station_per_source(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    # 同来源两站合并必须被拒。
    with pytest.raises(ConflictError) as ei:
        service.confirm_same(session, ids["1:001"], ids["1:002"], None)
    assert ei.value.details["conflicts"][0]["rule"] == "one_station_per_source_per_group"
    # 整次不变：仍是两个独立组，没有同站依据。
    assert _count_groups(session) == 6
    assert session.scalar(select(func.count(SameEvidence.id))) == 0


def test_same_is_idempotent_no_new_number(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11 = ids["1:001"], ids["2:B-11"]
    service.confirm_same(session, a1, b11, None)
    again = service.confirm_same(session, a1, b11, None)
    assert again["new_numbers"] == [] and again["already_same"] is True
    # 只有一条同站依据。
    assert session.scalar(select(func.count(SameEvidence.id))) == 1


# ---------------------------------------------------------------- 异站约束
def test_diff_blocks_later_merge_and_is_listed(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, a2, b11, cx7 = ids["1:001"], ids["1:002"], ids["2:B-11"], ids["3:CX-7"]

    # 先判 a2 与 b11 异站。
    d = service.confirm_diff(session, a2, b11, "坐标相距过远")
    assert d["active"] is True

    # 再把 a1 与 b11 同站、cx7 与 a1 同站，构造一个含 a1/b11/cx7 的组。
    service.confirm_same(session, a1, b11, None)
    service.confirm_same(session, a1, cx7, None)

    # 试图把 a2 并入该组：组间存在异站判定（a2 vs b11），必须被拦截。
    with pytest.raises(ConflictError) as ei:
        service.confirm_same(session, a2, cx7, None)
    conflicts = ei.value.details["conflicts"]
    assert conflicts and conflicts[0]["diff_evidence_id"] == d["evidence_id"]

    # 整次回滚：a2 仍独立成组，组数量与依据数量不变。
    assert _group_of(session, a2) != _group_of(session, cx7)
    assert session.scalar(select(func.count(SameEvidence.id))) == 2


def test_diff_inside_same_group_rejected_with_chain(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11, cx7 = ids["1:001"], ids["2:B-11"], ids["3:CX-7"]
    service.confirm_same(session, a1, b11, None)
    service.confirm_same(session, b11, cx7, None)
    with pytest.raises(ConflictError) as ei:
        service.confirm_diff(session, a1, cx7, None)
    chain = ei.value.details["conflicts"][0]["same_chain"]
    # 链路覆盖三个站点，提示应先撤销哪条依据。
    assert set(chain) == {a1, b11, cx7}


# ---------------------------------------------------------------- 撤销重分组
def test_revoke_recomputes_and_keeps_diff_judgement(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11, cx7, b12 = ids["1:001"], ids["2:B-11"], ids["3:CX-7"], ids["2:B-12"]

    e1 = service.confirm_same(session, a1, b11, None)
    e2 = service.confirm_same(session, b11, cx7, None)
    # 异站判定：b12 与 b11 不可同组。
    diff = service.confirm_diff(session, b12, b11, None)

    # 撤销 a1-b11 这条依据：剩余边 b11-cx7 仍连通，a1 独立出去，重分组。
    r = service.revoke_same(session, e1["evidence_id"], None)
    assert _group_of(session, a1) != _group_of(session, b11)
    assert _group_of(session, b11) == _group_of(session, cx7)
    # 拆开后两个组都签发了新号。
    assert len(r["new_numbers"]) == 2
    # 异站判定仍然有效，且现在阻止 b12 与 (b11,cx7) 合并。
    assert session.get(DiffEvidence, diff["evidence_id"]) is not None
    with pytest.raises(ConflictError):
        service.confirm_same(session, b12, cx7, None)


def test_revoke_then_reconfirm_reuses_evidence_row(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11 = ids["1:001"], ids["2:B-11"]
    e = service.confirm_same(session, a1, b11, None)
    service.revoke_same(session, e["evidence_id"], None)
    again = service.confirm_same(session, a1, b11, "重新核实")
    assert again["evidence_id"] == e["evidence_id"]
    row = session.get(SameEvidence, e["evidence_id"])
    assert row.revoked is False and row.revoked_at is None


def test_double_revoke_rejected(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    e = service.confirm_same(session, ids["1:001"], ids["2:B-11"], None)
    service.revoke_same(session, e["evidence_id"], None)
    with pytest.raises(ValidationError):
        service.revoke_same(session, e["evidence_id"], None)


# ---------------------------------------------------------------- 统一号历史
def test_old_number_snapshot_and_branch_lookup(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11, cx7 = ids["1:001"], ids["2:B-11"], ids["3:CX-7"]
    first = service.confirm_same(session, a1, b11, None)
    old_number = first["new_numbers"][0]
    service.confirm_same(session, b11, cx7, None)

    # 查旧号：快照固定为当时两站，不随后续合并改变。
    look = queries.lookup_number(session, old_number)
    assert {m["station_id"] for m in look["snapshot_members"]} == {a1, b11}
    # 这两个成员现在所在的完整组是包含三站的同一组。
    assert look["branch_count"] == 1
    assert {m["id"] for m in look["current_branches"][0]["members"]} == {a1, b11, cx7}


def test_old_number_after_split_lists_all_branches(session):
    import_stations_csv(session, CSV_A)
    ids = _ids(session)
    a1, b11, cx7 = ids["1:001"], ids["2:B-11"], ids["3:CX-7"]
    service.confirm_same(session, a1, b11, None)
    e2 = service.confirm_same(session, b11, cx7, None)
    # 三站合并时签发的号，快照为三站。
    three_station_number = e2["new_numbers"][0]

    # 撤销中间边 b11-cx7：三站拆为 {a1,b11} 与 {cx7} 两个分支。
    service.revoke_same(session, e2["evidence_id"], None)
    look = queries.lookup_number(session, three_station_number)
    # 旧号快照固定为该号签发当时的三站，拆组不改写历史。
    assert {m["station_id"] for m in look["snapshot_members"]} == {a1, b11, cx7}
    # 现属组必须把两个分支都列出来，不能静默只返回一个。
    assert look["split"] is True and look["branch_count"] == 2
    branch_members = [
        {m["id"] for m in b["members"]} for b in look["current_branches"]
    ]
    assert {frozenset(s) for s in branch_members} == {frozenset({a1, b11}), frozenset({cx7})}


def test_lookup_unknown_number_404(session):
    with pytest.raises(NotFoundError):
        queries.lookup_number(session, "U-999999")


# ---------------------------------------------------------------- 失败整次不变
def test_failed_operation_leaves_no_trace(session):
    from tests.conftest import TestingSessionLocal

    import_stations_csv(session, CSV_A)
    session.commit()  # 先提交导入，再用独立事务制造失败，验证失败不影响已提交状态。
    ids = _ids(session)
    other = TestingSessionLocal()
    try:
        groups_before = _count_groups(other)
        numbers_before = other.scalar(select(func.count(UnifiedNumber.id)))
        with pytest.raises(ConflictError):
            service.confirm_same(other, ids["1:001"], ids["1:002"], None)
        other.rollback()
        other.expire_all()
        assert _count_groups(other) == groups_before
        assert other.scalar(select(func.count(UnifiedNumber.id))) == numbers_before
        assert other.scalar(select(func.count(SameEvidence.id))) == 0
    finally:
        other.close()


def test_missing_station_raises_not_found(session):
    import_stations_csv(session, CSV_A)
    with pytest.raises(NotFoundError):
        service.confirm_same(session, 999, 1000, None)
