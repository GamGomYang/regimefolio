# The Inflation Compass (David Varadi) — 전략 요약

참고 자료:
- https://cssanalytics.wordpress.com/2026/07/27/the-inflation-compass-model/
- https://allocatesmartly.com/taming-the-wildcard-david-varadis-inflation-compass/

## 핵심 아이디어

성장(Growth)과 인플레이션(Inflation) 두 축으로 4가지 매크로 국면(regime)을 정의하고,
매월 그 국면에 가장 적합한 섹터 ETF **하나에 전액 투자**하는 전략.

기존 버전("Growth and Inflation")과 다른 점은, 인플레이션을 섹터 상대성과로 간접
추정하지 않고 **채권시장이 직접 반영하는 기대인플레이션(breakeven rate)을 사용**한다는 것.

## 국면 판단 신호 (정확한 규칙)

모든 판단은 **매월 마지막 거래일**에 이루어지고, 결정된 포지션을 **다음 한 달간** 보유. 항상 포지션 1개.

### Confirming Inflation Indicator (Growth/Inflation Model에서 차용)

일별 수익률 가중합 → 누적곱으로 합성 지수를 만든 뒤 비율을 취함.

- 포지티브 바스켓(리플레이션) 일별수익률 = 0.5·XLE + (1/6)·XLI + (1/6)·XLF + (1/6)·XLB
- 네거티브 바스켓(방어주) 일별수익률 = (1/3)·XLU + (1/3)·XLV + (1/3)·XLP
- Indicator = 포지티브 바스켓 누적성장 ÷ 네거티브 바스켓 누적성장

### 4개 조건 (월말 기준)

| # | 조건 | 정의 |
|---|---|---|
| 1 | Growth up | SPY 종가 > SPY 200일 SMA |
| 2 | Level | T5YIE > 2.0% |
| 3 | Breakeven momentum | T5YIE(오늘) > T5YIE(60거래일 전) |
| 4 | Asset momentum | Indicator의 60거래일 선형회귀 기울기 > 0 |

### 결합

```
Inflation-on = 조건2 AND (조건3 OR 조건4)
```

조건1(Growth)은 독립된 축으로 아래 국면표에서 별도 결합.

## 4계절 배분 (매월 리밸런싱, 월말 기준)

| 성장 | 인플레이션 | 보유자산 | 의미 |
|---|---|---|---|
| 상승 | 상승 | XLE (에너지) | 리플레이션 |
| 상승 | 하락 | XLK (기술) | 디스인플레 확장 |
| 하락 | 상승 | XLU (유틸리티) | 스태그플레이션 |
| 하락 | 하락 | XLP + IEF 50/50 | 디스인플레 침체 |

## 백테스트 성과 (2003~2026, cssanalytics 기준)

- CAGR 23.5% vs S&P500 11.6%
- Sharpe 1.41
- MDD -16.2% vs S&P500 -50.8%
- allocatesmartly는 1990년까지 확장 검증, "매우 변동성 크고 한 섹터에 집중" 특성 지적

## allocatesmartly의 핵심 비판 (구현 시 고려사항)

1. **섹터 예측은 자산군 예측보다 훨씬 어렵다** — 4개 섹터 중 하나를 맞추는 건 broad asset
   class 로테이션보다 노이즈에 취약함
2. **TIPS breakeven의 구조적 왜곡** — 2008-09, 2020-21처럼 유동성 스트레스 시기엔 TIPS/명목국채
   유동성 프리미엄 차이로 breakeven이 왜곡됨. 대체 인플레 지표(inflation swap, Fed survey)로
   테스트하면 성과가 크게 약화 → **TIPS 특이 이상현상에 대한 과적합 가능성** 제기

## 구현에 필요한 데이터

실제 매매 대상은 5개(XLE/XLK/XLU/XLP/IEF)뿐이지만, confirming indicator 계산에
XLI/XLF/XLB/XLV 4개가 추가로 필요 — 총 9개 섹터 ETF + SPY + T5YIE.

| 데이터 | 용도 | 소스(예) |
|---|---|---|
| SPY (또는 S&P 500 지수) 일별 종가 | 200일 SMA → 성장 신호 | Yahoo Finance |
| T5YIE (5년 기대인플레이션) 일별 | 인플레 레벨/모멘텀 신호 | FRED API (`T5YIE`) |
| XLE, XLK, XLU, XLP 일별 total return | **매매 대상** | Yahoo Finance |
| IEF (7-10년 국채 ETF) 일별 total return | 디스인플레 침체 국면 블렌드, **매매 대상** | Yahoo Finance |
| XLI, XLF, XLB 일별 total return | confirming indicator 포지티브 바스켓 | Yahoo Finance |
| XLV 일별 total return | confirming indicator 네거티브 바스켓 | Yahoo Finance |

**주의**: 위 9개 섹터 ETF 전부 1998-12-16 상장이라, confirming indicator 자체가
1999년 이전에는 계산 불가능 (앞서 논의한 데이터 제약이 보조지표에도 그대로 적용,
오히려 대상 티커가 늘어서 제약이 더 강해짐).

**참고**: FRED T5YIE는 2003년부터 제공되므로, cssanalytics의 2003년 이후 백테스트는 이 제약에
맞춘 것으로 보임. allocatesmartly가 언급한 1990년까지의 확장 테스트는 다른 인플레 프록시(또는
계산된 breakeven)를 썼을 가능성이 있으나, 원문에 상세 방법론이 없어 추가 확인 필요.
