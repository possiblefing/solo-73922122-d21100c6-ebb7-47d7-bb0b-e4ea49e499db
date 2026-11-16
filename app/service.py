"""核心领域服务：同站/异站判定、传递分组、撤销重算、统一号签发。

并发模型（PostgreSQL，READ COMMITTED）
--------------------------------------
1. 任何变更先按站点 id 升序 ``SELECT ... FOR UPDATE`` 锁定涉及的站点行，
   再锁定这些站点当前所在组的全部成员行。锁顺序全局一致（按 id 排序），
   因此不会出现环路死锁；数据库若仍报死锁，服务层按指数退避重试。
2. 行锁只覆盖受影响组的成员，不触碰无关组，因此「慢组重算不阻塞无关组
   确认」——两个互不相交的组可以并行确认。
3. 全部读写在同一事务内，异常即回滚，保证失败整次不变、依据不丢。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    CurrentGroup,
    DiffEvidence,
    GroupMember,
    NumberMember,
    SameEvidence,
    Station,
    UnifiedNumber,
)

# 统一号前缀，例如 U-000042。
_NUMBER_PREFIX = "U-"


# --------------------------------------------------------------------- 工具
def ordered_pair(a_id: int, b_id: int) -> tuple[int, int]:
    if a_id == b_id:
        raise ValidationError("不能对同一个站点建立同站/异站关系")
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def _lock_stations(session: Session, station_ids: list[int]) -> list[Station]:
    """按 id 升序锁定站点行，避免死锁并保证并发操作串行化。"""
    ordered = sorted(set(station_ids))
    stmt = select(Station).where(Station.id.in_(ordered)).order_by(Station.id)
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    rows = list(session.scalars(stmt))
    if len(rows) != len(ordered):
        found = {r.id for r in rows}
        missing = [i for i in ordered if i not in found]
        raise NotFoundError(f"站点不存在: {missing}")
    return rows


def _group_id_of(session: Session, station_id: int) -> int | None:
    return session.scalar(
        select(GroupMember.group_id).where(GroupMember.station_id == station_id)
    )


def _lock_groups_of(session: Session, station_ids: list[int]) -> set[int]:
    """锁定涉及站点当前组的全部成员行，返回被覆盖的组 id 集合。"""
    group_ids = {
        gid
        for gid in session.scalars(
            select(GroupMember.group_id).where(GroupMember.station_id.in_(station_ids))
        )
        if gid is not None
    }
    if group_ids and session.bind.dialect.name == "postgresql":
        # 锁这些组的成员行；按 id 排序加锁，杜绝交叉等待。
        session.scalars(
            select(GroupMember)
            .where(GroupMember.group_id.in_(group_ids))
            .order_by(GroupMember.id)
            .with_for_update()
        ).all()
    return group_ids


def _issue_number(
    session: Session,
    reason: str,
    member_ids: list[int],
    detail: dict | None = None,
) -> UnifiedNumber:
    """签发新统一号，并固化签发当时的成员快照。

    统一号本身取自该表自增主键（``U-000042``），因此先以占位号插入取回 id，
    再 UPDATE 为正式号；占位号也满足 NOT NULL 约束。
    """
    number = UnifiedNumber(number="PENDING", reason=reason, detail=detail)
    session.add(number)
    session.flush()  # 取得自增主键，作为统一号序号
    number.number = f"{_NUMBER_PREFIX}{number.id:06d}"
    session.flush()
    for sid in sorted(member_ids):
        session.add(NumberMember(unified_number_id=number.id, station_id=sid))
    return number


def _active_same_edges(session: Session) -> dict[int, set[int]]:
    """读取全部有效同站边，构建无向邻接表。"""
    # 先把会话内新建/改动的依据 flush 到数据库，保证本事务刚写入的边也能读到。
    session.flush()
    adj: dict[int, set[int]] = {}
    rows = session.execute(
        select(SameEvidence.station_a_id, SameEvidence.station_b_id).where(
            SameEvidence.revoked.is_(False)
        )
    )
    for a, b in rows:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _components(adj: dict[int, set[int]], roots: list[int]) -> list[list[int]]:
    """从 roots 出发做 BFS，返回受影响连通分量（每个分量成员按 id 排序）。"""
    seen: set[int] = set()
    result: list[list[int]] = []
    for root in roots:
        if root in seen:
            continue
        comp: list[int] = []
        queue: deque[int] = deque([root])
        seen.add(root)
        while queue:
            node = queue.popleft()
            comp.append(node)
            for nxt in adj.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        result.append(sorted(comp))
    return result


def _diff_blockers_between(
    session: Session, group_a: list[int], group_b: list[int]
) -> list[dict]:
    """两个待合并组之间所有有效的异站判定（用于列出冲突依据）。"""
    set_b = set(group_b)
    blockers: list[dict] = []
    rows = session.scalars(
        select(DiffEvidence).where(
            DiffEvidence.station_a_id.in_(group_a + group_b),
            DiffEvidence.station_b_id.in_(group_a + group_b),
        )
    )
    for d in rows:
        if (d.station_a_id in set_b) != (d.station_b_id in set_b):
            blockers.append(
                {
                    "diff_evidence_id": d.id,
                    "station_a_id": d.station_a_id,
                    "station_b_id": d.station_b_id,
                    "note": d.note,
                    "created_at": d.created_at.isoformat(),
                }
            )
    return blockers


def _same_chain(session: Session, a_id: int, b_id: int) -> list[int]:
    """返回 a 到 b 当前依据有效同站边的传递链（含端点）；不可达返回 []。"""
    adj = _active_same_edges(session)
    prev: dict[int, int] = {a_id: a_id}
    queue: deque[int] = deque([a_id])
    while queue:
        node = queue.popleft()
        if node == b_id:
            break
        for nxt in adj.get(node, ()):
            if nxt not in prev:
                prev[nxt] = node
                queue.append(nxt)
    if b_id not in prev:
        return []
    chain = [b_id]
    while chain[-1] != a_id:
        chain.append(prev[chain[-1]])
    chain.reverse()
    return chain


def _rebuild_groups(
    session: Session,
    adj: dict[int, set[int]],
    locked_station_ids: list[int],
    reason: str,
    detail_factory,
) -> tuple[list[int], list[str]]:
    """对受影响站点按当前同站边重算组。

    * 成员集合未变化的组原样保留（不换号）；
    * 成员发生变化（合并或拆开）的组删除旧组、建新组、签发新统一号；
    * 返回 (受影响组 id 列表, 新签发统一号列表)。
    """
    old_group_ids = _lock_groups_of(session, locked_station_ids)
    components = _components(adj, sorted(locked_station_ids))

    # 旧组 -> 成员集合，用于判断成员是否真的变化。
    old_membership: dict[int, frozenset[int]] = {}
    for gid in old_group_ids:
        old_membership[gid] = frozenset(
            session.scalars(
                select(GroupMember.station_id).where(GroupMember.group_id == gid)
            )
        )

    # 成员集合与某个新连通分量完全一致的旧组原样保留；其余旧组先删除，
    # 必须在插入新 GroupMember 之前完成，否则会撞 station_id 唯一约束。
    component_sets = [frozenset(c) for c in components]
    kept = {
        gid
        for gid, members in old_membership.items()
        if members in component_sets
    }
    stale = [gid for gid in old_group_ids if gid not in kept]
    if stale:
        session.execute(delete(GroupMember).where(GroupMember.group_id.in_(stale)))
        session.execute(delete(CurrentGroup).where(CurrentGroup.id.in_(stale)))
        session.flush()

    new_numbers: list[str] = []
    affected: list[int] = []
    for comp in components:
        comp_set = frozenset(comp)
        # 恰好等于某个旧组 → 成员未变，保留旧组与旧号。
        matching_old = next(
            (gid for gid, members in old_membership.items() if members == comp_set),
            None,
        )
        if matching_old is not None:
            affected.append(matching_old)
            continue

        # 防御性不变量：同一来源在一个组中至多一个站点。
        _assert_one_per_source(session, comp)

        group = CurrentGroup()
        session.add(group)
        session.flush()
        for sid in comp:
            session.add(GroupMember(group_id=group.id, station_id=sid))
        number = _issue_number(
            session,
            reason=reason,
            member_ids=comp,
            detail=detail_factory(comp),
        )
        group.current_number_id = number.id
        session.flush()
        new_numbers.append(number.number)
        affected.append(group.id)

    return sorted(affected), new_numbers


def _assert_one_per_source(session: Session, member_ids: list[int]) -> None:
    rows = session.execute(
        select(Station.source_id, Station.id).where(Station.id.in_(member_ids))
    ).all()
    seen: dict[int, int] = {}
    for source_id, station_id in rows:
        if source_id in seen:
            raise ConflictError(
                "同一来源的站点不能归入同一个统一站组",
                [
                    {
                        "rule": "one_station_per_source_per_group",
                        "source_id": source_id,
                        "station_ids": sorted([seen[source_id], station_id]),
                    }
                ],
            )
        seen[source_id] = station_id


# --------------------------------------------------------------------- 同站
def confirm_same(
    session: Session, a_id: int, b_id: int, note: str | None
) -> dict:
    a_id, b_id = ordered_pair(a_id, b_id)

    _lock_stations(session, [a_id, b_id])
    ga_id, gb_id = _group_id_of(session, a_id), _group_id_of(session, b_id)

    existing = session.scalar(
        select(SameEvidence).where(
            SameEvidence.station_a_id == a_id,
            SameEvidence.station_b_id == b_id,
        )
    )

    if ga_id == gb_id and ga_id is not None:
        # 已同组：幂等返回。若依据被撤销过则恢复，否则不重复建组、不换号。
        reactivated = False
        if existing is not None and existing.revoked:
            existing.revoked = False
            existing.revoked_at = None
            if note:
                existing.note = note
            session.flush()
            reactivated = True
        chain = _same_chain(session, a_id, b_id)
        return {
            "evidence_id": existing.id if existing else None,
            "kind": "same",
            "station_a_id": a_id,
            "station_b_id": b_id,
            "active": True,
            "affected_group_ids": [ga_id],
            "new_numbers": [],
            "already_same": True,
            "reactivated": reactivated,
            "conflicts": [],
            "same_chain": chain,
        }

    group_a = sorted(
        session.scalars(
            select(GroupMember.station_id).where(GroupMember.group_id == ga_id)
        )
        if ga_id is not None
        else [a_id]
    )
    group_b = sorted(
        session.scalars(
            select(GroupMember.station_id).where(GroupMember.group_id == gb_id)
        )
        if gb_id is not None
        else [b_id]
    )

    # 异站约束不得被后续合并绕过：跨组合并前逐一检查所有异站判定。
    blockers = _diff_blockers_between(session, group_a, group_b)
    if blockers:
        raise ConflictError(
            "合并被有效的异站判定拦截，本次操作整次回滚", blockers
        )

    # 同源约束：合并后每源至多一站。
    source_rows = session.execute(
        select(Station.id, Station.source_id).where(
            Station.id.in_(group_a + group_b)
        )
    ).all()
    by_source: dict[int, list[int]] = {}
    for sid, src in source_rows:
        by_source.setdefault(src, []).append(sid)
    dup = {src: sorted(ids) for src, ids in by_source.items() if len(ids) > 1}
    if dup:
        raise ConflictError(
            "合并后同一来源出现多个站点，违反每组同源最多一站",
            [
                {
                    "rule": "one_station_per_source_per_group",
                    "source_id": src,
                    "station_ids": ids,
                }
                for src, ids in sorted(dup.items())
            ],
        )

    # 写入/恢复同站依据（撤销后重新确认即复用原行，依据 id 保持稳定）。
    if existing is None:
        evidence = SameEvidence(
            station_a_id=a_id, station_b_id=b_id, note=note, revoked=False
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
    else:
        existing.revoked = False
        existing.revoked_at = None
        if note:
            existing.note = note
        session.flush()
        evidence_id = existing.id

    adj = _active_same_edges(session)
    affected, numbers = _rebuild_groups(
        session,
        adj,
        group_a + group_b,
        reason="merge",
        detail_factory=lambda comp: {
            "trigger": "confirm_same",
            "evidence_id": evidence_id,
            "members": comp,
        },
    )
    return {
        "evidence_id": evidence_id,
        "kind": "same",
        "station_a_id": a_id,
        "station_b_id": b_id,
        "active": True,
        "affected_group_ids": affected,
        "new_numbers": numbers,
        "already_same": False,
        "reactivated": existing is not None,
        "conflicts": [],
    }


# --------------------------------------------------------------------- 异站
def confirm_diff(
    session: Session, a_id: int, b_id: int, note: str | None
) -> dict:
    a_id, b_id = ordered_pair(a_id, b_id)
    _lock_stations(session, [a_id, b_id])

    existing = session.scalar(
        select(DiffEvidence).where(
            DiffEvidence.station_a_id == a_id,
            DiffEvidence.station_b_id == b_id,
        )
    )
    if existing is not None:
        return {
            "evidence_id": existing.id,
            "kind": "diff",
            "station_a_id": a_id,
            "station_b_id": b_id,
            "active": True,
            "affected_group_ids": [
                g for g in (_group_id_of(session, a_id), _group_id_of(session, b_id)) if g
            ],
            "new_numbers": [],
            "already_exists": True,
            "conflicts": [],
        }

    ga_id = _group_id_of(session, a_id)
    gb_id = _group_id_of(session, b_id)
    if ga_id is not None and ga_id == gb_id:
        # 同组内两站当前由同站依据传递相连，判异站必须先撤销相关同站依据。
        chain = _same_chain(session, a_id, b_id)
        raise ConflictError(
            "两站当前属于同一统一站组，不能判为异站；请先撤销链路上的同站依据",
            [
                {
                    "rule": "same_group_members",
                    "group_id": ga_id,
                    "same_chain": chain,
                }
            ],
        )

    evidence = DiffEvidence(station_a_id=a_id, station_b_id=b_id, note=note)
    session.add(evidence)
    session.flush()
    return {
        "evidence_id": evidence.id,
        "kind": "diff",
        "station_a_id": a_id,
        "station_b_id": b_id,
        "active": True,
        "affected_group_ids": [
            g for g in (ga_id, gb_id) if g is not None
        ],
        "new_numbers": [],
        "already_exists": False,
        "conflicts": [],
    }


# --------------------------------------------------------------------- 撤销
def revoke_same(session: Session, evidence_id: int, note: str | None) -> dict:
    evidence = session.get(SameEvidence, evidence_id)
    if evidence is None:
        raise NotFoundError(f"同站依据不存在: {evidence_id}")
    if evidence.revoked:
        raise ValidationError(f"同站依据 {evidence_id} 已处于撤销状态")

    a_id, b_id = evidence.station_a_id, evidence.station_b_id
    _lock_stations(session, [a_id, b_id])

    # 先标记撤销，再按剩余有效同站边重算。
    evidence.revoked = True
    evidence.revoked_at = datetime.now(timezone.utc)
    if note:
        evidence.note = (evidence.note + " | 撤销说明: " + note) if evidence.note else note
    session.flush()

    adj = _active_same_edges(session)
    affected, numbers = _rebuild_groups(
        session,
        adj,
        [a_id, b_id],
        reason="revoke",
        detail_factory=lambda comp: {
            "trigger": "revoke_same",
            "revoked_evidence_id": evidence_id,
            "members": comp,
        },
    )
    return {
        "evidence_id": evidence_id,
        "kind": "same",
        "station_a_id": a_id,
        "station_b_id": b_id,
        "active": False,
        "affected_group_ids": affected,
        "new_numbers": numbers,
        "conflicts": [],
    }
