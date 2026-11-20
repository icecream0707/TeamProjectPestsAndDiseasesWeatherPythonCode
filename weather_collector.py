"""기상청 단기예보 조회서비스 → weather_hourly / weather_forecast 수집기.

매일 02:10, 05:10, 08:10, 11:10, 14:10, 17:10, 20:10, 23:10 (KST)에 실행:
  - 초단기실황(getUltraSrtNcst): 지난 3시간(정시 3개)을 격자별로 받아 weather_hourly에 INSERT IGNORE
  - 단기예보(getVilageFcst): 방금 발표된 회차(02:10이면 0200 발표)를 격자별로 받아, 앞 24시간만 weather_forecast에 덮어씀
  - 위험 판단(disease_risk.py): 위 두 저장과 같은 트랜잭션에서 disease_risk(risk_level, in_season)를 덮어씀
격자는 region_grid의 DISTINCT (nx, ny).

필요: pip install requests pymysql
환경변수: KMA_SERVICE_KEY(공공데이터포털 일반 인증키 Decoding), DB_HOST, DB_PORT(기본 3306), DB_USER, DB_PASSWORD, DB_NAME
실행:  python weather_collector.py          # 계속 돌면서 정해진 시각마다 수집
       python weather_collector.py --once   # 가장 최근 회차를 지금 한 번만 수집
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import disease_risk

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋으로 충분
BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"  # 공공데이터포털 (기능별 하루 10000회)
SLOT_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)  # 단기예보 Base_time
SLOT_MINUTE = 10                            # API 제공 시간 HH:10 이후
HOURLY_KEEP_DAYS = 30
FCST_HOURS = 24  # 예보는 발표 시각 다음 24시간만 저장 (호출 한 번에 약 3일치가 오지만 판단에 24시간만 씀)

log = logging.getLogger("weather")


class ApiError(Exception):
    pass


# ---------- 시간 계산 ----------

def latest_slot(now):
    """now 이전(포함)의 가장 최근 수집 시각 (HH:10)."""
    for back in range(0, 25):
        t = (now - timedelta(hours=back)).replace(minute=SLOT_MINUTE, second=0, microsecond=0)
        if t.hour in SLOT_HOURS and t <= now:
            return t
    raise AssertionError("unreachable")


def next_slot(now):
    """now 이후(초과)의 다음 수집 시각."""
    t = now.replace(minute=SLOT_MINUTE, second=0, microsecond=0)
    while t <= now or t.hour not in SLOT_HOURS:
        t += timedelta(hours=1)
    return t


# ---------- 값 변환 (활용가이드 기준) ----------

def to_number(raw):
    """숫자로 변환. +900 이상, -900 이하는 Missing 값 → None."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return None if v >= 900 or v <= -900 else v


def to_int(raw):
    v = to_number(raw)
    return None if v is None else int(v)


def pcp_to_rn1(raw):
    """단기예보 PCP 범주 문자열 → 1시간 강수량(mm). 범주는 활용가이드 표의 아래쪽 경계값으로:
    '강수없음'·'-'·null·0 → 0, '1mm 미만'(0.1~1.0mm 미만) → 0.1, '6.2mm' → 6.2,
    '30.0~50.0mm' → 30.0, '50.0mm 이상' → 50.0. Missing 값과 모르는 형식 → None."""
    s = "" if raw is None else str(raw).strip()
    if s in ("", "-", "강수없음"):
        return 0.0
    if "미만" in s:
        return 0.1
    return to_number(s.replace("mm", "").replace("이상", "").split("~")[0])


def pivot_ncst(items):
    """초단기실황 item 목록 → {observed_at: {t1h, rn1, reh, pty}}"""
    rows = {}
    for it in items:
        at = datetime.strptime(it["baseDate"] + it["baseTime"], "%Y%m%d%H%M")
        row = rows.setdefault(at, {"t1h": None, "rn1": None, "reh": None, "pty": None})
        cat, val = it["category"], it["obsrValue"]
        if cat == "T1H":
            row["t1h"] = to_number(val)
        elif cat == "RN1":
            row["rn1"] = to_number(val)
        elif cat == "REH":
            row["reh"] = to_int(val)
        elif cat == "PTY":
            row["pty"] = to_int(val)
    return rows


def pivot_fcst(items):
    """단기예보 item 목록 → {forecast_at: {t1h, rn1, reh, pty}} (TMP는 t1h, PCP는 숫자로 바꿔 rn1 컬럼에 저장)"""
    rows = {}
    for it in items:
        cat = it["category"]
        if cat not in ("TMP", "PCP", "REH", "PTY"):
            continue  # TMN, TMX, SKY, POP, 바람 등은 저장하지 않음
        at = datetime.strptime(it["fcstDate"] + it["fcstTime"], "%Y%m%d%H%M")
        row = rows.setdefault(at, {"t1h": None, "rn1": None, "reh": None, "pty": None})
        val = it["fcstValue"]
        if cat == "TMP":
            row["t1h"] = to_number(val)
        elif cat == "PCP":
            row["rn1"] = pcp_to_rn1(val)
        elif cat == "REH":
            row["reh"] = to_int(val)
        elif cat == "PTY":
            row["pty"] = to_int(val)
    return rows


def next_hours(rows, base, hours):
    """{시각: 행}에서 base 다음 hours시간(base < 시각 <= base + hours)만 남김."""
    return {at: r for at, r in rows.items() if base < at <= base + timedelta(hours=hours)}


# ---------- API 호출 ----------

def call_api(session, op, params):
    """한 회차·한 격자의 item을 전부(페이지 넘겨가며) 가져옴. 데이터 없음(03)은 빈 목록."""
    items, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{op}", timeout=15, params={
            "serviceKey": os.environ["KMA_SERVICE_KEY"], "dataType": "JSON",  # 키는 Decoding 키 (requests가 인코딩함)
            "numOfRows": 1000, "pageNo": page, **params})
        r.raise_for_status()
        try:
            body = r.json()["response"]
        except (ValueError, KeyError):
            raise ApiError(f"{op} 응답이 JSON이 아님: {r.text[:200]}")
        code = str(body["header"]["resultCode"])
        if code == "03":  # NODATA_ERROR
            return items
        if code not in ("00", "0"):
            raise ApiError(f"{op} resultCode={code} {body['header'].get('resultMsg')}")
        got = body["body"]["items"]["item"]
        items += got
        if not got or len(items) >= int(body["body"]["totalCount"]):
            return items
        page += 1


# ---------- DB ----------

def connect():
    import pymysql  # test_weather_collector.py가 pymysql 없이도 돌 수 있게 여기서 import
    return pymysql.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"], charset="utf8mb4", autocommit=False)


def collect(slot):
    """slot(HH:10, KST) 회차 하나를 수집."""
    obs_hours = [slot.replace(minute=0) - timedelta(hours=h) for h in (2, 1, 0)]
    fcst_base = slot.replace(minute=0)
    ok_h = ok_f = ok_r = fail = 0

    with connect() as db, requests.Session() as session:
        with db.cursor() as cur:
            ids = disease_risk.rule_ids(cur)
            cur.execute("SELECT DISTINCT nx, ny FROM region_grid")
            grids = cur.fetchall()
        log.info("회차 %s: 격자 %d개", slot.strftime("%Y-%m-%d %H:%M"), len(grids))

        for nx, ny in grids:
            # 1) API 받기. 한쪽이 실패해도 나머지는 진행 (실패한 쪽은 DB의 기존 값을 그대로 둠)
            ncst = fcst = None
            try:
                ncst = {}
                for h in obs_hours:
                    ncst.update(pivot_ncst(call_api(session, "getUltraSrtNcst", {
                        "base_date": h.strftime("%Y%m%d"), "base_time": h.strftime("%H%M"),
                        "nx": nx, "ny": ny})))
            except Exception:
                ncst = None
                fail += 1
                log.exception("실황 실패 nx=%s ny=%s", nx, ny)
            try:
                fcst = next_hours(pivot_fcst(call_api(session, "getVilageFcst", {
                    "base_date": fcst_base.strftime("%Y%m%d"), "base_time": fcst_base.strftime("%H%M"),
                    "nx": nx, "ny": ny})), fcst_base, FCST_HOURS) or None  # 비어 있으면 기존 예보 유지
            except Exception:
                fail += 1
                log.exception("예보 실패 nx=%s ny=%s", nx, ny)

            # 2) 실황 저장 + 예보 덮어쓰기 + 위험 판단을 한 트랜잭션으로
            try:
                with db.cursor() as cur:
                    if ncst:  # 이미 있는 시각은 UNIQUE(nx, ny, observed_at)로 건너뜀
                        cur.executemany(
                            "INSERT IGNORE INTO weather_hourly (nx, ny, observed_at, t1h, rn1, reh, pty) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [(nx, ny, at, r["t1h"], r["rn1"], r["reh"], r["pty"]) for at, r in ncst.items()])
                        ok_h += 1
                    if fcst:
                        cur.execute("DELETE FROM weather_forecast WHERE nx = %s AND ny = %s", (nx, ny))
                        cur.executemany(
                            "INSERT INTO weather_forecast (nx, ny, forecast_at, t1h, rn1, reh, pty) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [(nx, ny, at, r["t1h"], r["rn1"], r["reh"], r["pty"]) for at, r in fcst.items()])
                        ok_f += 1
                    # 방금 넣은 행 + 이전 23시간 실황 + 24시간 예보로 판단 (같은 트랜잭션이라 새 행도 보임)
                    disease_risk.update_grid(cur, nx, ny, fcst_base, ids)
                db.commit()
                ok_r += 1
            except Exception:
                db.rollback()
                fail += 1
                log.exception("저장 실패 nx=%s ny=%s", nx, ny)

        with db.cursor() as cur:
            cur.execute("DELETE FROM weather_hourly WHERE observed_at < %s",
                        (slot.replace(tzinfo=None) - timedelta(days=HOURLY_KEEP_DAYS),))
        db.commit()

    log.info("완료: 실황 %d격자, 예보 %d격자, 위험판단 %d격자, 실패 %d건", ok_h, ok_f, ok_r, fail)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--once" in sys.argv:
        collect(latest_slot(datetime.now(KST)).replace(tzinfo=None))
        return
    while True:
        slot = next_slot(datetime.now(KST))
        log.info("다음 수집: %s", slot.strftime("%Y-%m-%d %H:%M"))
        # 1분 여유: HH:10 정각에는 방금 정시 실황이 아직 없을 수 있음
        time.sleep(max(0, (slot - datetime.now(KST)).total_seconds()) + 60)
        try:
            collect(slot.replace(tzinfo=None))
        except Exception:
            log.exception("회차 전체 실패")  # DB 연결 실패 등. 다음 회차에 다시 시도


if __name__ == "__main__":
    main()
