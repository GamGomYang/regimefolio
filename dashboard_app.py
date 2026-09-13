"""Entry point for the Inflation Compass Streamlit app.

Run with: streamlit run dashboard_app.py
"""

import streamlit as st

st.set_page_config(page_title="Inflation Compass", layout="wide")

st.markdown(
    """
    <style>
    html, body, [class*="css"] { overflow-wrap: anywhere; }
    [data-testid="stAppViewContainer"] { overflow-x: hidden; }
    div.block-container {
        width: 100%; max-width: 100%; padding: 2.8rem clamp(1rem, 3vw, 4rem) 3rem;
    }
    .ic-fixed-title {
        position: fixed; top: 0.85rem; left: 4rem; z-index: 1000000;
        font-size: 1.15rem; font-weight: 600; pointer-events: none;
    }
    [data-testid="stMetric"] {
        min-width: 0; padding: clamp(.65rem, 1.5vw, 1rem);
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricValue"], [data-testid="stMetricDelta"] {
        min-width: 0; white-space: normal; overflow-wrap: anywhere;
    }
    [data-testid="stMetricValue"] > div { font-size: clamp(1.15rem, 2.2vw, 2rem); }
    [data-testid="stHorizontalBlock"] { min-width: 0; }
    [data-testid="column"] { min-width: 0 !important; }
    [data-testid="stPlotlyChart"], [data-testid="stDataFrame"] { max-width: 100%; overflow: hidden; }
    .stTabs [data-baseweb="tab-list"] {
        gap: .25rem; overflow-x: auto; scrollbar-width: thin; flex-wrap: nowrap;
    }
    .stTabs [data-baseweb="tab"] {
        flex: 0 0 auto; white-space: nowrap; padding-left: .75rem; padding-right: .75rem;
    }
    @media (max-width: 768px) {
        div.block-container { padding: 1.25rem .85rem 2rem; }
        .ic-fixed-title { display: none; }
        [data-testid="stMetric"] { padding: .55rem .65rem; }
        [data-testid="stMetricLabel"] p { font-size: .78rem; line-height: 1.25; }
        [data-testid="stMetricValue"] > div { font-size: 1.25rem; line-height: 1.25; }
        [data-testid="stForm"] { padding: .75rem; }
        .stTabs [data-baseweb="tab"] { font-size: .85rem; }
    }
    @media (max-width: 480px) {
        div.block-container { padding-left: .65rem; padding-right: .65rem; }
        h1 { font-size: 1.65rem !important; }
        h2 { font-size: 1.3rem !important; }
        h3 { font-size: 1.08rem !important; }
    }
    </style>
    <div class="ic-fixed-title">Inflation Compass</div>
    """,
    unsafe_allow_html=True,
)

pages = [
    st.Page("views/dashboard.py", title="대시보드", default=True),
    st.Page("views/live_testing.py", title="실전 테스트"),
    st.Page("views/methodology.py", title="전략 설명"),
]
st.navigation(pages).run()
