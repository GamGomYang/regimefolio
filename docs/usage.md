# 사용 가이드

## 데이터 갱신과 운용

저장소 루트에서 가상환경을 활성화한 뒤 실행합니다.

```bash
source .venv/bin/activate
python daily_job.py
```

`실전 테스트`에서 시작일·초기자금·Shadow 비용을 저장합니다. 시작일 이후의 월말 확정 신호와 다음 거래일 가격이 있어야 Shadow 결과가 생성됩니다.
월말 신호를 확인한 뒤 외부에서 직접 매매하고, 거래일·신호일·수량·체결가·수수료를 입력합니다.

`--skip-fetch`는 기존 DB로 계산하는 옵션입니다. 데이터를 수집하지 않지만 신호·NAV·실행 기록은 저장합니다.
백테스트 화면까지 새 데이터로 갱신하려면 `python backtest.py`를 실행하고 앱을 재시작합니다.

## macOS 자동 실행

```bash
./scripts/macos/install.sh
launchctl print "gui/$(id -u)/com.inflation-compass.daily"
```

설치 스크립트는 현재 프로젝트와 사용자 홈 경로로 LaunchAgent를 생성하고 즉시 작업을 시작합니다.
가상환경과 초기 데이터를 먼저 준비하세요. 설치된 프로젝트 폴더를 이동했다면 다시 설치해야 합니다.
Mac이 꺼져 있거나 잠든 동안에는 작업이 실행되지 않습니다.

수동 실행과 자동 실행 중지:

```bash
./scripts/macos/run_daily.sh
./scripts/macos/uninstall.sh
```

- 실행 로그: `logs/daily_job.log`, `logs/daily_job_error.log`
- 데이터 및 운용 기록: `data/inflation_compass.db`
- 날짜별 백업: `data/backups/`

## 분석 도구와 검증

```bash
# 기존 DB 결과로 문서용 차트 생성
python -m scripts.plot_results

# 신호 시차·비용별 실험
python -m research.t5yie_scenarios

# 과거 시점의 FRED 데이터로 분석 — 네트워크 사용
python -m research.alfred_vintage_backtest

# 임시 DB 기반 통합 테스트
python -m pytest -q
```

실험 산출물은 `results/`, 문서용 차트는 `docs/images/charts/`에 저장됩니다.
기존 테스트는 반복 실행 시 신호 중복 방지, 모의·실제 NAV 계산, 필수 가격 누락 시 실패 처리를 검증합니다.
외부 데이터 수집과 전략 수익성은 이 테스트의 검증 범위에 포함되지 않습니다.

## 폴더 구성

| 위치 | 내용 |
|---|---|
| 루트 Python 파일 | 데이터 수집, 백테스트, 일별 작업, 앱 진입점과 운용 계산 |
| `views/` | 분석·운용·전략 설명 화면 |
| `research/` | 시차와 과거 데이터 빈티지 실험 |
| `scripts/` | 차트 생성 및 macOS 자동 실행 |
| `docs/` | 사용 가이드와 이미지 |
| `tests/` | 통합 테스트 |

DB·로그·백업·실험 결과와 개인 메모는 Git에서 제외합니다.

## 계산 참고

기본 백테스트는 수정종가 수익률을 사용하며, 데이터와 지표 준비 기간에 따라 분석 구간이 정해집니다.
Sharpe는 현재 구현에서 무위험수익률을 차감하지 않습니다.
턴오버는 실제 비중의 일중 변동이 아니라 이전 목표 비중과 새 목표 비중의 차이로 계산합니다.
Shadow의 XLP·IEF는 매월 목표 비중을 다시 설정하므로 국면이 같아도 비중 조정이 발생할 수 있습니다.

## 출처

전략 아이디어는 David Varadi의 [Inflation Compass](https://cssanalytics.wordpress.com/2026/07/27/the-inflation-compass-model/)에 기반합니다.
기존 공개 코드의 최초 원본 저장소·참고 커밋·라이선스는 현재 기록으로 특정되지 않았습니다.
전략 글과 코드의 출처는 별개이며, 이 문서는 전체 코드를 독자 구현으로 주장하거나 새로운 라이선스를 부여하지 않습니다.
