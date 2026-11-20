# 날씨 수집 · 병해충 위험 판단

기상청 단기예보 API로 시군구 격자별 날씨를 모으고, 병해충별 위험 단계(0 좋음 / 1 주의 / 2 위험)를 계산해 MySQL에 저장합니다.

| 파일                   | 역할                                                                                           |
| ---------------------- | ---------------------------------------------------------------------------------------------- |
| `weather_collector.py` | 3시간마다 실황·예보를 받아 `weather_hourly`, `weather_forecast`에 저장하고, 위험 판단까지 실행 |
| `disease_risk.py`      | 지난 24시간 실황 + 앞으로 24시간 예보로 `disease_risk`를 갱신 (단독 실행 가능)                 |

두 파일은 서로 import하므로 같은 폴더에 둡니다.

## 1. 준비

- Python 3.8 이상
- MySQL 8.0.16 이상
- 공공데이터포털 **기상청\_단기예보 조회서비스** 활용신청 → 일반 인증키 **(Decoding)**

```bash
pip install requests pymysql
```

## 2. DB 준비

아래 테이블과 데이터가 DB에 있어야 합니다. 생성 SQL은 백엔드 저장소의 `schema.sql`, `seed_data.sql`에 있습니다.

- 테이블: `region_grid`, `weather_hourly`, `weather_forecast`, `disease_risk`, `crop_disease`
- 데이터: `region_grid`(시군구 격자 256행), `crop_disease`(병해충 label 17행)

`crop_disease`에 판단 규칙의 label이 하나라도 없으면 실행할 때 `LookupError`가 납니다.

## 3. 환경변수

키와 비밀번호는 코드나 깃에 넣지 말고 환경변수로만 설정합니다.

| 이름              | 설명                             |
| ----------------- | -------------------------------- |
| `KMA_SERVICE_KEY` | 공공데이터포털 인증키 (Decoding) |
| `DB_HOST`         | MySQL 호스트                     |
| `DB_PORT`         | 포트 (생략하면 3306)             |
| `DB_USER`         | DB 사용자                        |
| `DB_PASSWORD`     | DB 비밀번호                      |
| `DB_NAME`         | DB 이름                          |

PowerShell:

```powershell
$env:KMA_SERVICE_KEY="발급받은_Decoding_키"
$env:DB_HOST="localhost"; $env:DB_USER="user"; $env:DB_PASSWORD="password"; $env:DB_NAME="dbname"
```

Linux / macOS:

```bash
export KMA_SERVICE_KEY="발급받은_Decoding_키"
export DB_HOST=localhost DB_USER=user DB_PASSWORD=password DB_NAME=dbname
```

## 4. 실행

```bash
python weather_collector.py --once   # 가장 최근 회차를 지금 한 번만 수집 (처음 동작 확인용)
python weather_collector.py          # 계속 실행: 매일 02:10, 05:10, … 23:10 (KST, 3시간 간격)에 수집
python disease_risk.py               # API 호출 없이, DB에 쌓인 날씨로 위험 단계만 다시 계산
```

`weather_collector.py`는 스스로 다음 수집 시각까지 기다리므로 cron이 필요 없습니다. 서버에서는 백그라운드로 계속 돌게 둡니다.

```bash
nohup python weather_collector.py > collector.log 2>&1 &
```

## 5. 동작 확인

```sql
SELECT COUNT(*) FROM weather_hourly;
SELECT COUNT(*) FROM weather_forecast;
SELECT risk_level, COUNT(*) FROM disease_risk GROUP BY risk_level;
```

## 참고

- API 호출량은 한 회차에 격자 수(최대 256) × (실황 3회 + 예보 1회 이상) 정도입니다. 기능별 하루 한도는 10,000회입니다.
- 실황은 이미 있는 시각을 건너뛰고(`INSERT IGNORE`), 30일보다 오래된 행은 지웁니다.
- 예보와 위험 단계는 격자별로 매 회차 덮어씁니다.
- 위험 판단 기준값은 임의로 정한 값이라 실제 발생 데이터로 조정해야 합니다.
