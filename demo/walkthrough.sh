#!/bin/sh
# 端到端演示脚本：在已启动的服务上走完 导入→同站→异站拦截→撤销→旧号分支查询。
# 用法：./demo/walkthrough.sh [BASE_URL]
set -e
BASE="${1:-http://localhost:8000}"
CURL="curl -sf"

echo "=== 0. 健康检查 ==="
$CURL "$BASE/health"; echo

echo "=== 1. 导入演示 CSV ==="
$CURL -X POST "$BASE/api/import" -F "file=@$(dirname "$0")/stations_demo.csv" | python3 -m json.tool

echo "=== 2. 取站点 id ==="
STATIONS=$($CURL "$BASE/api/stations")
echo "$STATIONS" | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    print(s['id'], s['source_code_label'], s['name'])
"
A1=$(echo "$STATIONS" | python3 -c "import json,sys;print(next(s['id'] for s in json.load(sys.stdin) if s['source_code_label']=='INS-A/001'))")
B11=$(echo "$STATIONS" | python3 -c "import json,sys;print(next(s['id'] for s in json.load(sys.stdin) if s['source_code_label']=='INS-B/B-11'))")
CX7=$(echo "$STATIONS" | python3 -c "import json,sys;print(next(s['id'] for s in json.load(sys.stdin) if s['source_code_label']=='INS-C/CX-7'))")
A2=$(echo "$STATIONS" | python3 -c "import json,sys;print(next(s['id'] for s in json.load(sys.stdin) if s['source_code_label']=='INS-A/002'))")

echo "=== 3. 同站 A/001 ↔ B/B-11，再传递并入 C/CX-7 ==="
$CURL -X POST "$BASE/api/evidences/same" -H 'Content-Type: application/json' \
  -d "{\"station_a_id\":$A1,\"station_b_id\":$B11,\"note\":\"名称坐标吻合\"}" | python3 -m json.tool
MERGED=$($CURL -X POST "$BASE/api/evidences/same" -H 'Content-Type: application/json' \
  -d "{\"station_a_id\":$B11,\"station_b_id\":$CX7}")
echo "$MERGED" | python3 -m json.tool
THREE_NUM=$(echo "$MERGED" | python3 -c "import json,sys;print(json.load(sys.stdin)['new_numbers'][0])")

echo "=== 4. 异站判定 A/002 ↔ B/B-11（注意 B/B-11 已在大组内）==="
$CURL -X POST "$BASE/api/evidences/diff" -H 'Content-Type: application/json' \
  -d "{\"station_a_id\":$A2,\"station_b_id\":$B11,\"note\":\"坐标相距30km\"}" | python3 -m json.tool

echo "=== 5. 试图把 A/002 并入大组 → 预期 409，列出异站依据 ==="
curl -s -o /tmp/stationhub_conflict.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/evidences/same" \
  -H 'Content-Type: application/json' -d "{\"station_a_id\":$A2,\"station_b_id\":$A1}"
python3 -m json.tool /tmp/stationhub_conflict.json

echo "=== 6. 撤销 B/B-11 ↔ C/CX-7 的同站依据，按剩余关系重分组 ==="
EVID=$($CURL "$BASE/api/evidences?include_revoked=true" | python3 -c "
import json,sys
ev=json.load(sys.stdin)['same']
print(next(e['id'] for e in ev if e['station_a_id']==$B11 and e['station_b_id']==$CX7))")
$CURL -X POST "$BASE/api/evidences/same/revoke" -H 'Content-Type: application/json' \
  -d "{\"evidence_id\":$EVID,\"note\":\"现场复核不同址\"}" | python3 -m json.tool

echo "=== 7. 查旧号 $THREE_NUM：快照固定三站，现属组列出全部两个分支 ==="
$CURL "$BASE/api/numbers/$THREE_NUM" | python3 -m json.tool
