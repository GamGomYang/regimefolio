from pathlib import Path
from datetime import date
import sqlite3
import sys

import pytest

from streamlit.testing.v1 import AppTest

import live_core
from test_live_blackbox import seed_market


@pytest.fixture(autouse=True)
def restore_main_module():
    # Streamlit's script runner replaces __main__. Spawned processes must import
    # pytest's original entry point, not the UI script against the user's DB.
    original = sys.modules['__main__']
    try:
        yield
    finally:
        sys.modules['__main__'] = original


def test_manual_trade_failure_in_ui_does_not_persist(tmp_path,monkeypatch):
    path=tmp_path/'ui.db'
    seed_market(path)
    monkeypatch.setattr(live_core,'DEFAULT_DB',path)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'views/live_testing.py')).run(timeout=15)
    assert not app.exception
    next(widget for widget in app.date_input if widget.label=='거래일').set_value(date(2025,3,28))
    next(widget for widget in app.number_input if widget.label=='수량').set_value(2000.0)
    next(button for button in app.button if button.label=='거래 저장').click().run(timeout=15)
    assert not app.exception
    assert any('거래를 저장하지 못했습니다' in error.value for error in app.error)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM live_trades').fetchone()[0]==0
