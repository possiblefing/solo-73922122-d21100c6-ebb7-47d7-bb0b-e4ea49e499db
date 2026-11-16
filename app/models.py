"""ORM 模型。

事实来源设计：

* ``stations``           —— 导入后不可变的原始站点记录；
* ``same_evidences``     —— 「同站」依据（可撤销，撤销后行保留并标记时间）；
* ``diff_evidences``     —— 「异站」判定，持久生效，不允许撤销；
* ``current_groups``     —— 依据当前有效同站边重算出来的组（物化视图式维护）；
* ``group_members``      —— 组与站点的当前隶属关系；
* ``unified_numbers`` / ``number_members`` —— 统一号签发快照，旧号永远固定于
  签发当时的成员集合，之后拆组不改写历史。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    stations: Mapped[list["Station"]] = relationship(back_populates="source")


class Station(Base):
    __tablename__ = "stations"
    __table_args__ = (
        UniqueConstraint("source_id", "source_code", name="uq_station_source_code"),
        Index("ix_stations_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    # 来源机构自己的站点号，导入后永不修改。
    source_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="stations")


class SameEvidence(Base):
    """同站依据。每个无序站对最多一条；撤销后保留行，重新确认则恢复有效。"""

    __tablename__ = "same_evidences"
    __table_args__ = (
        CheckConstraint("station_a_id < station_b_id", name="ck_same_pair_ordered"),
        UniqueConstraint("station_a_id", "station_b_id", name="uq_same_pair"),
        Index("ix_same_active", "revoked"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_a_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )
    station_b_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DiffEvidence(Base):
    """异站判定。约束永久有效，后续合并若被它拦截则整次失败并回滚。"""

    __tablename__ = "diff_evidences"
    __table_args__ = (
        CheckConstraint("station_a_id < station_b_id", name="ck_diff_pair_ordered"),
        UniqueConstraint("station_a_id", "station_b_id", name="uq_diff_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_a_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )
    station_b_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CurrentGroup(Base):
    """当前组。组成员任何一次变化都会作废旧组并签发新统一号。"""

    __tablename__ = "current_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 当前生效统一号；历史号在 unified_numbers 中永久可查。
    current_number_id: Mapped[int | None] = mapped_column(
        ForeignKey("unified_numbers.id", ondelete="RESTRICT"), nullable=True
    )

    memberships: Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("station_id", name="uq_member_station"),
        Index("ix_group_members_group", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("current_groups.id", ondelete="CASCADE"), nullable=False
    )
    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )

    group: Mapped[CurrentGroup] = relationship(back_populates="memberships")


class UnifiedNumber(Base):
    """统一号签发记录。number_members 是签发当时的成员快照，永不变更。"""

    __tablename__ = "unified_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members: Mapped[list["NumberMember"]] = relationship(
        back_populates="unified_number", cascade="all, delete-orphan"
    )


class NumberMember(Base):
    __tablename__ = "number_members"
    __table_args__ = (
        Index("ix_number_members_station", "station_id"),
        Index("ix_number_members_number", "unified_number_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unified_number_id: Mapped[int] = mapped_column(
        ForeignKey("unified_numbers.id", ondelete="CASCADE"), nullable=False
    )
    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="RESTRICT"), nullable=False
    )

    unified_number: Mapped[UnifiedNumber] = relationship(back_populates="members")
