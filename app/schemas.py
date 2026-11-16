"""HTTP 层使用的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------- 基础视图 ----------
class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    created_at: datetime


class StationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    source_code: str
    name: str
    imported_at: datetime


class StationDetail(StationOut):
    source_code_label: str = Field(description="来源机构站点号，形如 INS-A/001")
    source_name: str
    current_group_id: int | None
    current_number: str | None


class SameEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    station_a_id: int
    station_b_id: int
    note: str | None
    revoked: bool
    created_at: datetime
    revoked_at: datetime | None


class DiffEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    station_a_id: int
    station_b_id: int
    note: str | None
    created_at: datetime


# ---------- 请求体 ----------
class MergeRequest(BaseModel):
    station_a_id: int = Field(gt=0)
    station_b_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=512)


class DiffRequest(BaseModel):
    station_a_id: int = Field(gt=0)
    station_b_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=512)


class RevokeRequest(BaseModel):
    evidence_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=512)


# ---------- 操作结果 ----------
class EvidenceResult(BaseModel):
    evidence_id: int
    kind: str
    station_a_id: int
    station_b_id: int
    active: bool
    affected_group_ids: list[int]
    new_numbers: list[str]
    conflicts: list[dict] = Field(default_factory=list)


class NumberMemberOut(BaseModel):
    station_id: int
    source_code: str
    source_name: str
    name: str


class NumberLookup(BaseModel):
    number: str
    reason: str
    created_at: datetime
    detail: dict | None = None
    snapshot_members: list[NumberMemberOut]
    # 旧号成员现在归属的全部当前组；拆开后会出现多个分支，全部列出。
    current_branches: list[dict]
    branch_count: int
    split: bool


class GroupOut(BaseModel):
    group_id: int
    current_number: str | None
    created_at: datetime
    members: list[StationDetail]


class ImportReport(BaseModel):
    source_code: str
    source_name: str
    inserted: int
    skipped_existing: int
    total_for_source: int
    new_numbers: list[str]
    rows: list[StationOut]
