"""只读查询服务。

查询只读取已提交数据，天然只见完整提交状态。按旧统一号查询时，固定返回
签发当时的成员快照，并展开这些成员「现在」所属的全部当前组；拆组后会出现
多个分支，接口绝不静默挑选其中一组。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import NotFoundError
from .models import (
    CurrentGroup,
    DiffEvidence,
    GroupMember,
    NumberMember,
    SameEvidence,
    Source,
    Station,
    UnifiedNumber,
)


def _station_payload(session: Session, station: Station) -> dict:
    row = session.execute(
        select(CurrentGroup.id, UnifiedNumber.number)
        .select_from(Station)
        .join(GroupMember, GroupMember.station_id == Station.id)
        .join(CurrentGroup, CurrentGroup.id == GroupMember.group_id)
        .outerjoin(UnifiedNumber, UnifiedNumber.id == CurrentGroup.current_number_id)
        .where(Station.id == station.id)
    ).first()
    source = session.get(Source, station.source_id)
    return {
        "id": station.id,
        "source_id": station.source_id,
        "source_code": station.source_code,
        "name": station.name,
        "imported_at": station.imported_at,
        "source_code_label": f"{source.code}/{station.source_code}",
        "source_name": source.name,
        "current_group_id": row[0] if row else None,
        "current_number": row[1] if row else None,
    }


def get_station(session: Session, station_id: int) -> dict:
    station = session.get(Station, station_id)
    if station is None:
        raise NotFoundError(f"站点不存在: {station_id}")
    return _station_payload(session, station)


def list_stations(session: Session, source_code: str | None = None) -> list[dict]:
    stmt = select(Station).order_by(Station.id)
    if source_code:
        stmt = stmt.join(Source, Source.id == Station.source_id).where(
            Source.code == source_code
        )
    return [_station_payload(session, s) for s in session.scalars(stmt)]


def list_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.id)))


def _group_payload(session: Session, group: CurrentGroup) -> dict:
    stations = list(
        session.scalars(
            select(Station)
            .join(GroupMember, GroupMember.station_id == Station.id)
            .where(GroupMember.group_id == group.id)
            .order_by(Station.id)
        )
    )
    number = session.get(UnifiedNumber, group.current_number_id) if group.current_number_id else None
    return {
        "group_id": group.id,
        "current_number": number.number if number else None,
        "created_at": group.created_at,
        "members": [_station_payload(session, s) for s in stations],
    }


def list_groups(session: Session) -> list[dict]:
    groups = list(session.scalars(select(CurrentGroup).order_by(CurrentGroup.id)))
    return [_group_payload(session, g) for g in groups]


def get_group(session: Session, group_id: int) -> dict:
    group = session.get(CurrentGroup, group_id)
    if group is None:
        raise NotFoundError(f"组不存在: {group_id}")
    return _group_payload(session, group)


def list_evidences(session: Session, include_revoked: bool = True) -> dict:
    same_stmt = select(SameEvidence).order_by(SameEvidence.id)
    if not include_revoked:
        same_stmt = same_stmt.where(SameEvidence.revoked.is_(False))
    diffs = list(session.scalars(select(DiffEvidence).order_by(DiffEvidence.id)))
    return {
        "same": list(session.scalars(same_stmt)),
        "diff": diffs,
    }


def lookup_number(session: Session, number: str) -> dict:
    """按统一号查询：旧号返回固定快照 + 成员现属的完整组（全部分支）。"""
    unified = session.scalar(select(UnifiedNumber).where(UnifiedNumber.number == number))
    if unified is None:
        raise NotFoundError(f"统一号不存在: {number}")

    snapshot_rows = session.execute(
        select(Station, Source)
        .select_from(NumberMember)
        .join(Station, Station.id == NumberMember.station_id)
        .join(Source, Source.id == Station.source_id)
        .where(NumberMember.unified_number_id == unified.id)
        .order_by(Station.id)
    ).all()
    snapshot = [
        {
            "station_id": station.id,
            "source_code": f"{source.code}/{station.source_code}",
            "source_name": source.name,
            "name": station.name,
        }
        for station, source in snapshot_rows
    ]

    snapshot_station_ids = [m["station_id"] for m in snapshot]

    # 这些成员现在各自属于哪个当前组 —— 拆组后是多个分支，全部列出。
    branch_rows = session.execute(
        select(CurrentGroup, GroupMember.station_id)
        .select_from(GroupMember)
        .join(CurrentGroup, CurrentGroup.id == GroupMember.group_id)
        .where(GroupMember.station_id.in_(snapshot_station_ids))
        .order_by(CurrentGroup.id)
    ).all()
    branch_group_ids = sorted({g.id for g, _ in branch_rows})
    branches = [get_group(session, gid) for gid in branch_group_ids]

    return {
        "number": unified.number,
        "reason": unified.reason,
        "created_at": unified.created_at,
        "detail": unified.detail,
        "snapshot_members": snapshot,
        "current_branches": branches,
        "branch_count": len(branches),
        "split": len(branches) > 1,
    }
