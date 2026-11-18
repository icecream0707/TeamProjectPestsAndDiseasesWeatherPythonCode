"""격자별 병해충 위험 단계(disease_risk) 판단.

판단 창: 지금 기준 지난 24시간 실황(weather_hourly) + 앞으로 24시간 예보(weather_forecast) = 최대 48시간.
병해충마다 "발생하기 좋은 시간"의 조건이 있고, 창 안에서 그 조건이 끊기지 않고 이어진 가장 긴 시간으로 단계를 정함:
  최장 연속 >= 위험 기준 → 2(위험), >= 주의 기준 → 1(주의), 그 외 → 0(좋음)
  실황 마지막 시각(지금)과 예보 첫 시각(지금+1시간)은 이어진 것으로 봄. 빠진 시각(API 실패 등)에서는 끊김.
기준은 임의로 정한 값이라 실제 발생 데이터로 다듬어야 함.

단독 실행:  python disease_risk.py   # DB에 쌓인 날씨로 모든 격자를 다시 판단 (기상청 API 호출 없음)
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("disease_risk")

PAST = timedelta(hours=24)   # 실황: 전날 같은 시각부터
FUTURE = timedelta(hours=24)  # 예보: 앞으로 24시간 (weather_collector.FCST_HOURS와 같게)
HOUR = timedelta(hours=1)

# 젖음(wet): 비가 오거나 강수형태가 비 계열. 눈(3), 눈날림(7)은 제외
NCST_RAIN_PTY = {1, 2, 4, 5, 6}   # 초단기실황 PTY: 비, 비/눈, 소나기, 빗방울, 빗방울눈날림
FCST_RAIN_PTY = {1, 2, 4}         # 단기예보 PTY: 비, 비/눈, 소나기
PCP_NO_RAIN = {None, "", "강수없음", "-", "0", "0.0"}  # 활용가이드: -, null, 0은 강수없음


def between(v, lo, hi):
    return v is not None and lo <= v <= hi


def humid_or_wet(h, rh):
    return (h["h"] is not None and h["h"] >= rh) or h["w"]


# (crop_disease.label, 이름, 조건(시간 1개 → bool), 주의 기준 연속시간, 위험 기준 연속시간)
# h = {"at": 시각, "t": 기온℃, "h": 습도%, "w": 젖음 여부}
RULES = [
    ("Potato___Late_blight", "감자 역병",
     lambda h: between(h["t"], 10, 25) and humid_or_wet(h, 90), 6, 12),
    ("Tomato___Late_blight", "토마토 역병",
     lambda h: between(h["t"], 10, 25) and humid_or_wet(h, 90), 6, 12),
    ("Potato___Early_blight", "감자 겹둥근무늬병",
     lambda h: between(h["t"], 20, 30) and humid_or_wet(h, 90), 6, 12),
    ("Tomato___Early_blight", "토마토 겹둥근무늬병",
     lambda h: between(h["t"], 20, 30) and humid_or_wet(h, 90), 6, 12),
    ("Tomato___Bacterial_spot", "토마토 세균점무늬병",   # 빗물이 튀며 퍼져서 '비 오는 따뜻한 시간'만 셈
     lambda h: between(h["t"], 24, 30) and h["w"], 3, 6),
    ("Tomato___Septoria_leaf_spot", "토마토 셉토리아 잎점무늬병",
     lambda h: between(h["t"], 15, 27) and humid_or_wet(h, 90), 6, 12),
    ("Tomato___Target_Spot", "토마토 타깃점무늬병",
     lambda h: between(h["t"], 20, 28) and h["h"] is not None and h["h"] >= 90, 6, 12),
    ("Tomato___Spider_mites Two-spotted_spider_mite", "토마토 점박이응애",   # 덥고 건조할 때
     lambda h: h["t"] is not None and h["t"] >= 27 and h["h"] is not None and h["h"] <= 55 and not h["w"], 6, 12),
    ("Corn_(maize)___Cercospora_leaf_spot Gray_leaf_spot", "옥수수 회색점무늬병",  # 긴 고습 필요
     lambda h: between(h["t"], 22, 30) and h["h"] is not None and h["h"] >= 90, 8, 14),
    ("Corn_(maize)___Common_rust_", "옥수수 녹병",
     lambda h: between(h["t"], 16, 23) and humid_or_wet(h, 95), 6, 12),
    ("Corn_(maize)___Northern_Leaf_Blight", "옥수수 그을음무늬병",
     lambda h: between(h["t"], 18, 27) and humid_or_wet(h, 90), 6, 12),
]


def level(hours, cond, caution, danger):
    """hours(시각순)에서 조건을 만족한 가장 긴 연속 시간으로 단계 결정."""
    best = run = 0
    prev = None
    for h in hours:
        if not cond(h):
            run = 0
        elif run and h["at"] - prev == HOUR:
            run += 1
        else:
            run = 1  # 새로 시작하거나, 앞 시각이 빠져 있어서 끊김
        prev = h["at"]
        best = max(best, run)
    return 2 if best >= danger else 1 if best >= caution else 0


def load_hours(cur, nx, ny, now):
    """판단 창의 시간별 날씨를 시각순 {"at", "t", "h", "w"} 목록으로. 같은 트랜잭션에서 방금 넣은 행도 보임."""
    cur.execute("SELECT observed_at, t1h, rn1, reh, pty FROM weather_hourly "
                "WHERE nx = %s AND ny = %s AND observed_at > %s AND observed_at <= %s ORDER BY observed_at",
                (nx, ny, now - PAST, now))
    hours = [{"at": at, "t": float(t) if t is not None else None, "h": h,
              "w": pty in NCST_RAIN_PTY or (rn1 is not None and rn1 > 0)}
             for at, t, rn1, h, pty in cur.fetchall()]
    cur.execute("SELECT forecast_at, t1h, pcp, reh, pty FROM weather_forecast "
                "WHERE nx = %s AND ny = %s AND forecast_at > %s AND forecast_at <= %s ORDER BY forecast_at",
                (nx, ny, now, now + FUTURE))
    hours += [{"at": at, "t": float(t) if t is not None else None, "h": h,
               "w": pty in FCST_RAIN_PTY or pcp not in PCP_NO_RAIN}
              for at, t, pcp, h, pty in cur.fetchall()]
    return hours


def rule_ids(cur):
    """RULES의 label → crop_disease.id. 없는 label이 있으면 바로 알림."""
    labels = [r[0] for r in RULES]
    cur.execute("SELECT label, id FROM crop_disease WHERE label IN (%s)" % ",".join(["%s"] * len(labels)), labels)
    ids = dict(cur.fetchall())
    missing = [l for l in labels if l not in ids]
    if missing:
        raise LookupError(f"crop_disease에 없는 label: {missing}")
    return ids


def update_grid(cur, nx, ny, now, ids):
    """한 격자의 위험 단계를 계산해 덮어씀. commit은 호출한 쪽이 함 (날씨 저장과 같은 트랜잭션)."""
    hours = load_hours(cur, nx, ny, now)
    if not hours:
        log.warning("날씨 없음 nx=%s ny=%s - 위험 단계 갱신 안 함", nx, ny)
        return
    cur.executemany(
        "INSERT INTO disease_risk (nx, ny, crop_disease_id, risk_level, calculated_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE risk_level = VALUES(risk_level), calculated_at = VALUES(calculated_at)",
        [(nx, ny, ids[label], level(hours, cond, c, d), now) for label, _, cond, c, d in RULES])


def recalculate(db, now):
    """모든 격자를 다시 판단 (단독 실행용)."""
    with db.cursor() as cur:
        ids = rule_ids(cur)
        cur.execute("SELECT DISTINCT nx, ny FROM region_grid")
        grids = cur.fetchall()
    for nx, ny in grids:
        try:
            with db.cursor() as cur:
                update_grid(cur, nx, ny, now, ids)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("위험 판단 실패 nx=%s ny=%s", nx, ny)


if __name__ == "__main__":
    import weather_collector as wc
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with wc.connect() as db:
        recalculate(db, datetime.now(wc.KST).replace(tzinfo=None, minute=0, second=0, microsecond=0))
    log.info("완료")
