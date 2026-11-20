"""격자별 병해충 위험 단계(disease_risk) 판단.

판단 창: 지금 기준 지난 24시간 실황(weather_hourly) + 앞으로 24시간 예보(weather_forecast) = 최대 48시간.
병해충마다 "발생하기 좋은 시간"의 조건이 있고, 창 안에서 그 조건이 끊기지 않고 이어진 가장 긴 시간으로 단계를 정함:
  최장 연속 >= 위험 기준 → 2(위험), >= 주의 기준 → 1(주의), 그 외 → 0(좋음)
  실황 마지막 시각(지금)과 예보 첫 시각(지금+1시간)은 이어진 것으로 봄. 빠진 시각(API 실패 등)에서는 끊김.
기준은 국내 자료(농촌진흥청, 농사로, KCI)에서 시간 기준까지 확인된 것만 씀. 근거가 없는 병해충은 판단하지 않음.
작물의 노지 재배 시기 안인지(in_season)를 따로 저장하고, 알림은 risk_level >= 1 AND in_season = 1일 때만 보냄.

단독 실행:  python disease_risk.py   # DB에 쌓인 날씨로 모든 격자를 다시 판단 (기상청 API 호출 없음)
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("disease_risk")

PAST = timedelta(hours=24)   # 실황: 전날 같은 시각부터
FUTURE = timedelta(hours=24)  # 예보: 앞으로 24시간 (weather_collector.FCST_HOURS와 같게)
HOUR = timedelta(hours=1)

# 젖음(wet): 강수형태가 비 계열이거나 1시간 강수량(rn1) > 0. 강수형태 눈(3), 눈날림(7)은 비 계열에서 뺌
#   (강수량에는 녹은 눈이 잡힐 수 있어, 눈 오는 시간도 강수량이 0보다 크면 젖음이 됨)
NCST_RAIN_PTY = {1, 2, 4, 5, 6}   # 초단기실황 PTY: 비, 비/눈, 소나기, 빗방울, 빗방울눈날림
FCST_RAIN_PTY = {1, 2, 4}         # 단기예보 PTY: 비, 비/눈, 소나기


def between(v, lo, hi):
    return v is not None and lo <= v <= hi


def humid_or_wet(h, rh):
    return (h["h"] is not None and h["h"] >= rh) or h["w"]


# 작물별 노지 재배 시기 (월, 일). 농사로 농작업일정의 노지 작형 파종(아주심기)~수확, 상순 1~10일 / 중순 11~20일 / 하순 21일~말일
#   감자 2월 상순~11월 하순 (봄·여름·가을재배, 겨울시설재배 제외)  cntntsNo=30699
#   토마토 5월 중순~10월 상순 (노지억제재배)                          cntntsNo=30646
#   옥수수 4월 중순~10월 상순 (보통직파재배·가을재배) - 지금 판단하는 병해충 없음  cntntsNo=30709
SEASONS = {"POTATO": ((2, 1), (11, 30)), "TOMATO": ((5, 11), (10, 10)), "CORN": ((4, 11), (10, 10))}

# (crop_disease.label, 이름, crop.code, 조건(시간 1개 → bool), 주의 기준 연속시간, 위험 기준 연속시간 또는 None)
# 주의 = 국내 연구의 "발생이 시작된 최소 잎결로 시간". 잎결로는 "습도 90% 이상 또는 젖음"으로 대신함.
# 위험 = 국내 자료에 더 심한 단계 수치가 없어 None(주의까지만). risk_level 0·1·2 양식은 확장과 알림 테스트를 위해 유지.
# h = {"at": 시각, "t": 기온℃, "h": 습도%, "w": 젖음 여부}
# 토마토 잎마름역병 발생 예측 연구(KCI, 2017): 15·20℃에서 잎결로 6시간 이상이면 발생 (ART002305179)
# 농촌진흥청 보도자료(2024.7.9): 역병은 10~24℃, 상대습도 90% 이상에서 잘 발생 (dataNo=100000796913)
RULES = [
    ("Tomato___Late_blight", "토마토 역병", "TOMATO",
     lambda h: between(h["t"], 15, 20) and humid_or_wet(h, 90), 6, None),
    # 감자 역병은 같은 병원균(Phytophthora infestans)이라 토마토 연구의 잎결로 시간을 적용
    ("Potato___Late_blight", "감자 역병", "POTATO",
     lambda h: between(h["t"], 15, 20) and humid_or_wet(h, 90), 6, None),
]


def in_season(crop, now):
    """now(한국 시각)의 월·일이 작물의 노지 재배 시기 안이면 1. 세 기간 모두 해를 넘기지 않아 월·일 비교로 충분."""
    start, end = SEASONS[crop]
    return 1 if start <= (now.month, now.day) <= end else 0


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
    return 2 if danger is not None and best >= danger else 1 if best >= caution else 0


def load_hours(cur, nx, ny, now):
    """판단 창의 시간별 날씨를 시각순 {"at", "t", "h", "w"} 목록으로. 같은 트랜잭션에서 방금 넣은 행도 보임."""
    cur.execute("SELECT observed_at, t1h, rn1, reh, pty FROM weather_hourly "
                "WHERE nx = %s AND ny = %s AND observed_at > %s AND observed_at <= %s ORDER BY observed_at",
                (nx, ny, now - PAST, now))
    hours = [{"at": at, "t": float(t) if t is not None else None, "h": h,
              "w": pty in NCST_RAIN_PTY or (rn1 is not None and rn1 > 0)}
             for at, t, rn1, h, pty in cur.fetchall()]
    cur.execute("SELECT forecast_at, t1h, rn1, reh, pty FROM weather_forecast "
                "WHERE nx = %s AND ny = %s AND forecast_at > %s AND forecast_at <= %s ORDER BY forecast_at",
                (nx, ny, now, now + FUTURE))
    hours += [{"at": at, "t": float(t) if t is not None else None, "h": h,
               "w": pty in FCST_RAIN_PTY or (rn1 is not None and rn1 > 0)}
              for at, t, rn1, h, pty in cur.fetchall()]
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
        "INSERT INTO disease_risk (nx, ny, crop_disease_id, risk_level, in_season, calculated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE risk_level = VALUES(risk_level), in_season = VALUES(in_season), "
        "calculated_at = VALUES(calculated_at)",
        [(nx, ny, ids[label], level(hours, cond, c, d), in_season(crop, now), now)
         for label, _, crop, cond, c, d in RULES])


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
