"""Strategy description and calculation methodology - static reference page."""

import streamlit as st

st.markdown(
    """
    <style>
    .ic-intro { font-size: 15px; color: #52564d; margin: 0 0 4px; line-height: 1.6; }
    .ic-quad { display: grid; grid-template-columns: minmax(82px,120px) minmax(0,1fr) minmax(0,1fr); gap: 8px; margin: 16px 0 6px; width: min(100%,620px); }
    .ic-quad .hdr { display: flex; align-items: center; justify-content: center; font-size: 11.5px; font-weight: 600;
        color: #8a8d84; text-transform: uppercase; letter-spacing: 0.06em; text-align: center; }
    .ic-quad .rowhdr { display: flex; align-items: center; font-size: 13px; font-weight: 600; color: #52564d; }
    .ic-quad .cell { min-width: 0; border-radius: 6px; padding: 14px 10px; text-align: center; overflow-wrap: anywhere; }
    .ic-quad .cell .ticker { font-size: 19px; font-weight: 700; color: #16191a; line-height: 1.3; }
    .ic-quad .cell .kr { font-size: 12px; color: #52564d; }
    .ic-quad .cell .label { font-size: 11.5px; color: #8a8d84; font-style: italic; }
    .ic-body p, .ic-body ul { margin: 0 0 8px; font-size: 14px; line-height: 1.48; }
    .ic-body ul { padding-left: 20px; }
    .ic-body li { margin-bottom: 3px; }
    .ic-body h4 { font-size: 14px; margin: 10px 0 4px; font-weight: 700; color: #16191a; }
    .ic-body h4:first-child { margin-top: 0; }
    .ic-body code { font-size: 13px; background: #f2f3ee; padding: 1px 5px; border-radius: 3px; }
    .ic-body .formula { max-width: 100%; background: #f2f3ee; border-radius: 5px; padding: 8px 12px; font-size: 13px; margin: 4px 0 8px; line-height: 1.6; overflow-wrap: anywhere; }
    @media (max-width: 600px) {
        .ic-quad { grid-template-columns: 68px minmax(0,1fr) minmax(0,1fr); gap: 5px; }
        .ic-quad .hdr { font-size: 9.5px; letter-spacing: 0; line-height: 1.25; }
        .ic-quad .rowhdr { font-size: 11px; line-height: 1.25; }
        .ic-quad .cell { padding: 10px 4px; }
        .ic-quad .cell .ticker { font-size: 15px; }
        .ic-quad .cell .kr, .ic-quad .cell .label { font-size: 10px; }
        .ic-body p, .ic-body ul { font-size: 13px; }
        .ic-body .formula { padding: 7px 8px; font-size: 11.5px; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

tab_strategy, tab_signals, tab_cost = st.tabs(["전략설명", "신호", "매매비용"])

with tab_strategy:
    st.markdown(
        """
        <div class="ic-body">
        <ul>
        <li><b>성장(Growth)</b>과 <b>인플레이션(Inflation)</b> 두 축으로 매크로 국면을 4가지로 나눕니다.</li>
        <li>매월 마지막 거래일에 국면을 판정해서 해당하는 섹터 ETF 하나에 전액 투자하고, 다음 한 달간 보유합니다.</li>
        <li>포지션은 항상 하나입니다 (디스인플레 침체 국면만 예외로 2개 자산을 절반씩 보유).</li>
        <li>매달 국면이 바뀌지 않으면 포지션도 그대로 유지됩니다.</li>
        </ul>
        <div class="ic-quad">
          <div></div>
          <div class="hdr">인플레이션 상승</div>
          <div class="hdr">인플레이션 하락</div>
          <div class="rowhdr">성장 ↑</div>
          <div class="cell" style="background:#dbe8f8"><div class="ticker">XLE</div><div class="kr">에너지</div><div class="label">reflation</div></div>
          <div class="cell" style="background:#fbe3d8"><div class="ticker">XLK</div><div class="kr">기술</div><div class="label">goldilocks</div></div>
          <div class="rowhdr">성장 ↓</div>
          <div class="cell" style="background:#e6e1f3"><div class="ticker">XLU</div><div class="kr">유틸리티</div><div class="label">stagflation</div></div>
          <div class="cell" style="background:linear-gradient(90deg,#fbedd0 50%,#fbe1ea 50%)"><div class="ticker">XLP + IEF</div><div class="kr">필수소비재 + 7-10년 국채</div><div class="label">disinflation</div></div>
        </div>
        <p style="margin-top:20px">참고: <a href="https://cssanalytics.wordpress.com/2026/07/27/the-inflation-compass-model/" target="_blank">cssanalytics.wordpress.com</a></p>
        </div>
        """,
        unsafe_allow_html=True,
    )

with tab_signals:
    st.markdown(
        """
        <div class="ic-body">
        <h4>성장 신호</h4>
        <p>SPY(S&amp;P500 추종 ETF) 종가가 200일 이동평균보다 위에 있으면 "성장 상승", 아래에 있으면
        "성장 하락"으로 판정합니다.</p>

        <h4>인플레이션 신호</h4>
        <p>T5YIE(5년 기대인플레이션율, FRED 발표)가 연준의 목표치인 2.0%보다 높아야 하고, 그 위에
        다음 두 모멘텀 조건 중 <b>하나라도</b> 충족되면 "인플레이션 상승"으로 판정합니다 (둘 다 계산해서
        OR로 결합 — 하나를 골라 쓰는 게 아니라 서로 보완하는 이중 확인 장치입니다):</p>
        <ul>
        <li><b>Breakeven 모멘텀</b> — T5YIE 자체가 60거래일 전보다 높다</li>
        <li><b>Asset 모멘텀</b> — confirming indicator의 60일 선형회귀 기울기가 양수다</li>
        </ul>
        <div class="formula">inflation-on = (T5YIE &gt; 2.0%) AND (breakeven 모멘텀 OR asset 모멘텀)</div>

        <h4>Confirming indicator</h4>
        <p>T5YIE는 채권시장 가격이라 유동성 스트레스 시기(2008-09, 2020-21 등)에 왜곡될 수 있습니다.
        이를 보완하기 위해 인플레이션에 <b>수혜를 받는 섹터</b>와 <b>피해를 받는 섹터</b>의 누적수익률
        비율을 별도로 계산해서, 시장이 실제로 리플레이션을 가격에 반영하고 있는지 다시 확인합니다.</p>
        <div class="formula">
        positive(수혜 바스켓) = 0.5·XLE(에너지) + 1/6·XLI(산업재) + 1/6·XLF(금융) + 1/6·XLB(소재)<br/>
        negative(피해 바스켓) = 1/3·XLU(유틸리티) + 1/3·XLV(헬스케어) + 1/3·XLP(필수소비재)<br/>
        indicator = 누적수익률(positive) / 누적수익률(negative)
        </div>
        <p>이 비율이 상승 추세(60일 회귀기울기 &gt; 0)라는 건 리플레이션 수혜 섹터가 방어 섹터 대비
        계속 아웃퍼폼하고 있다는 뜻입니다.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

with tab_cost:
    st.markdown(
        """
        <div class="ic-body">
        <h4>매매비용 가정</h4>
        <p>매매비용은 턴오버 × 입력%로, 리밸런싱이 실제로 포지션을 바꾼 다음 거래일에만 적용됩니다
        (같은 포지션을 유지하는 달은 비용이 0).</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
