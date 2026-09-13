# Regimefolio 화면 자료

2026-09-13에 캡처한 실제 앱 화면 5장을 원본 해상도로 보관합니다.
이미지를 생성하거나 데이터·표시 내용을 수정하지 않았습니다.

| 파일 | 화면 | 해상도 |
|---|---|---|
| [backtest-analysis.png](screenshots/backtest-analysis.png) | SPY·전략의 누적 성과 및 낙폭 비교 | 2882 × 1284 |
| [regime-allocation.png](screenshots/regime-allocation.png) | 성장·인플레이션 국면별 ETF 배분 | 1366 × 696 |
| [portfolio-weights.png](screenshots/portfolio-weights.png) | 시간에 따른 ETF 보유 비중 | 2882 × 968 |
| [live-signals.png](screenshots/live-signals.png) | 운용 설정과 현재 신호 | 2952 × 1598 |
| [trade-entry.png](screenshots/trade-entry.png) | 수동 거래 입력과 실행 이력 | 2908 × 1562 |

루트 [README](../../README.md)는 저장소 상대경로로 이미지를 참조합니다.
화면의 Inflation Compass는 분석 대상 전략명이며 저장소 이름은 Regimefolio입니다.

실전 테스트 화면의 마지막 가격 날짜는 2026-08-12입니다. 실행 성공 상태는 데이터 최신성을 의미하지 않습니다.
백테스트 이미지는 시뮬레이션 결과이며, 거래 입력 화면은 체결 내역을 입력하는 UI 예시입니다.
화면을 교체할 때는 README의 날짜·비용 가정·설명도 함께 갱신합니다.

## 폴더 구성

- `screenshots/`: 실제 웹 화면 캡처. 루트 README에서 사용합니다.
- `charts/`: `scripts/plot_results.py`로 생성하는 백테스트 차트.

| 차트 | 내용 |
|---|---|
| [equity-drawdown.png](charts/equity-drawdown.png) | 누적 성과와 낙폭 |
| [portfolio-weights.png](charts/portfolio-weights.png) | 일별 ETF 비중 |
| [relative-performance.png](charts/relative-performance.png) | SPY 대비 상대성과와 상대낙폭 |

저장소 루트에서 `python -m scripts.plot_results`를 실행하면 기존 DB의 백테스트 결과로 `charts/`의 세 파일을 갱신합니다.
파일명은 영문 소문자와 하이픈을 사용합니다.
