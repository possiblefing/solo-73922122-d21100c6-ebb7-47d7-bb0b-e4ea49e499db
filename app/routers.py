"""HTTP 路由：导入、同站/异站确认、撤销、组与统一号查询、依据浏览。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from . import queries, service
from .db import get_session
from .errors import ServiceError
from .importer import import_stations_csv
from .retry import run_with_retry
from .schemas import (
    DiffRequest,
    EvidenceResult,
    GroupOut,
    ImportReport,
    MergeRequest,
    NumberLookup,
    RevokeRequest,
    SameEvidenceOut,
    DiffEvidenceOut,
    SourceOut,
    StationDetail,
)

router = APIRouter(prefix="/api")


def _run(action, *args):
    """把领域异常翻译成 HTTP 错误，其余异常由会话依赖统一回滚。"""
    try:
        return action(*args)
    except ServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": exc.message, **exc.details},
        )


# ---------------- 导入 ----------------
@router.post("/import", response_model=list[ImportReport], tags=["import"])
def import_csv(
    file: UploadFile = File(..., description="CSV：source,station_code,name"),
    session: Session = Depends(get_session),
):
    content = file.file.read()
    if not content:
        raise HTTPException(400, detail={"message": "上传文件为空"})
    return _run(run_with_retry, session, import_stations_csv, content)


# ---------------- 依据操作 ----------------
@router.post("/evidences/same", response_model=EvidenceResult, tags=["evidences"])
def create_same(body: MergeRequest, session: Session = Depends(get_session)):
    return _run(
        run_with_retry, session, service.confirm_same,
        body.station_a_id, body.station_b_id, body.note,
    )


@router.post("/evidences/diff", response_model=EvidenceResult, tags=["evidences"])
def create_diff(body: DiffRequest, session: Session = Depends(get_session)):
    return _run(
        run_with_retry, session, service.confirm_diff,
        body.station_a_id, body.station_b_id, body.note,
    )


@router.post("/evidences/same/revoke", response_model=EvidenceResult, tags=["evidences"])
def revoke_same(body: RevokeRequest, session: Session = Depends(get_session)):
    return _run(run_with_retry, session, service.revoke_same, body.evidence_id, body.note)


# ---------------- 查询 ----------------
@router.get("/sources", response_model=list[SourceOut], tags=["query"])
def get_sources(session: Session = Depends(get_session)):
    return queries.list_sources(session)


@router.get("/stations", response_model=list[StationDetail], tags=["query"])
def get_stations(
    source_code: str | None = Query(default=None),
    session: Session = Depends(get_session),
):
    return queries.list_stations(session, source_code)


@router.get("/stations/{station_id}", response_model=StationDetail, tags=["query"])
def get_station(station_id: int, session: Session = Depends(get_session)):
    return _run(queries.get_station, session, station_id)


@router.get("/groups", response_model=list[GroupOut], tags=["query"])
def get_groups(session: Session = Depends(get_session)):
    return queries.list_groups(session)


@router.get("/groups/{group_id}", response_model=GroupOut, tags=["query"])
def get_group(group_id: int, session: Session = Depends(get_session)):
    return _run(queries.get_group, session, group_id)


@router.get("/evidences", tags=["query"])
def get_evidences(
    include_revoked: bool = Query(default=True),
    session: Session = Depends(get_session),
):
    data = queries.list_evidences(session, include_revoked)
    return {
        "same": [SameEvidenceOut.model_validate(e) for e in data["same"]],
        "diff": [DiffEvidenceOut.model_validate(e) for e in data["diff"]],
    }


@router.get("/numbers/{number}", response_model=NumberLookup, tags=["query"])
def get_number(number: str, session: Session = Depends(get_session)):
    return _run(queries.lookup_number, session, number)
