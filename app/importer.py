"""CSV 导入：来源、源站号、名称。

规则：
* 每个来源至多 30 站（含库内已有的累计计数）；
* 原始站点记录不可变：已存在的 (来源, 源站号) 只做存在性核对，绝不更新名称；
* 单次导入是一个事务，任何一行失败整次回滚；
* 新站点初始独立成组，并立即签发首个统一号。
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .errors import ConflictError, ValidationError
from .models import CurrentGroup, GroupMember, Source, Station
from .service import _issue_number

# 支持中英文表头。
_HEADER_ALIASES = {
    "来源": "source",
    "source": "source",
    "source_code_org": "source",
    "源站号": "source_code",
    "源站点号": "source_code",
    "station_code": "source_code",
    "code": "source_code",
    "名称": "name",
    "name": "name",
    "station_name": "name",
}


def _parse_csv(content: bytes) -> list[dict[str, str]]:
    # 兼容带 BOM 的 UTF-8。
    text = content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 内容为空")
    def _map_column(h: str) -> str | None:
        h = h.strip()
        return _HEADER_ALIASES.get(h.lower()) or _HEADER_ALIASES.get(h)

    cols = [_map_column(h) for h in header]
    if not all(cols) or len(set(cols)) != 3:
        raise ValidationError(
            f"CSV 表头必须包含 来源/source、源站号/station_code、名称/name 三列，实际为: {header}"
        )
    rows: list[dict[str, str]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue  # 跳过空行
        if len(raw) != len(header):
            raise ValidationError(f"第 {line_no} 行列数与表头不一致")
        record = {cols[i]: raw[i].strip() for i in range(len(cols))}
        if not all(record.values()):
            raise ValidationError(f"第 {line_no} 行存在空字段: {raw}")
        record["_line"] = str(line_no)
        rows.append(record)
    if not rows:
        raise ValidationError("CSV 没有任何数据行")
    return rows


def import_stations_csv(session: Session, content: bytes) -> list[dict]:
    """导入一个或多个来源的站点，按来源汇总导入报告。"""
    rows = _parse_csv(content)

    # 先按来源分组并做文件内重复检查。
    grouped: dict[str, list[dict]] = {}
    source_display: dict[str, str] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["source"], row["source_code"])
        if key in seen_pairs:
            raise ValidationError(
                f"CSV 内部存在重复站点: 来源={row['source']} 源站号={row['source_code']}"
            )
        seen_pairs.add(key)
        grouped.setdefault(row["source"], []).append(row)
        source_display[row["source"]] = row["source"]

    reports: list[dict] = []

    # 来源按 code 排序处理，保证并发下加锁顺序稳定（来源行锁）。
    for source_code in sorted(grouped):
        source_rows = grouped[source_code]
        source = session.scalar(select(Source).where(Source.code == source_code))
        if source is None:
            source = Source(code=source_code, name=source_code)
            session.add(source)
            session.flush()

        existing_count = session.scalar(
            select(func.count(Station.id)).where(Station.source_id == source.id)
        )
        if existing_count + len(source_rows) > settings.MAX_STATIONS_PER_SOURCE:
            raise ValidationError(
                f"来源 {source_code} 站点数将达到 {existing_count + len(source_rows)}，"
                f"超过每源上限 {settings.MAX_STATIONS_PER_SOURCE}"
            )

        existing = {
            s.source_code: s
            for s in session.scalars(
                select(Station).where(Station.source_id == source.id)
            )
        }

        inserted: list[Station] = []
        skipped = 0
        new_numbers: list[str] = []
        for row in source_rows:
            code, name = row["source_code"], row["name"]
            if code in existing:
                # 原记录不改：名称不一致只报错，绝不覆盖。
                if existing[code].name != name:
                    raise ConflictError(
                        f"站点 {source_code}/{code} 已存在且名称为 "
                        f"「{existing[code].name}」，原始记录不可修改，"
                        f"CSV 中为「{name}」",
                        [
                            {
                                "rule": "immutable_original_record",
                                "source": source_code,
                                "source_code": code,
                                "existing_name": existing[code].name,
                                "csv_name": name,
                            }
                        ],
                    )
                skipped += 1
                continue
            station = Station(source_id=source.id, source_code=code, name=name)
            session.add(station)
            session.flush()
            existing[code] = station
            inserted.append(station)

            # 初始独立成组并签发首个统一号。
            group = CurrentGroup()
            session.add(group)
            session.flush()
            session.add(GroupMember(group_id=group.id, station_id=station.id))
            number = _issue_number(
                session,
                reason="import",
                member_ids=[station.id],
                detail={"trigger": "csv_import", "source": source_code},
            )
            group.current_number_id = number.id
            session.flush()
            new_numbers.append(number.number)

        reports.append(
            {
                "source_code": source.code,
                "source_name": source.name,
                "inserted": len(inserted),
                "skipped_existing": skipped,
                "total_for_source": existing_count + len(inserted),
                "new_numbers": new_numbers,
                "rows": [
                    {
                        "id": s.id,
                        "source_id": s.source_id,
                        "source_code": s.source_code,
                        "name": s.name,
                        "imported_at": s.imported_at,
                    }
                    for s in inserted
                ],
            }
        )

    return reports
