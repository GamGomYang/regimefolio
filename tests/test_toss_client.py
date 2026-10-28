import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.request import Request, urlopen

import pytest

from broker.toss import BrokerError, NoRedirect, TossClient

TOKEN = 'FAKE_TOKEN_DO_NOT_LOG'
SECRET = 'FAKE_SECRET_DO_NOT_LOG'
AUTH = {'access_token': TOKEN, 'token_type': 'Bearer', 'expires_in': 86400}


def order(identifier='order-1'):
    return {'orderId': identifier, 'symbol': 'XLK', 'side': 'BUY', 'orderType': 'LIMIT', 'timeInForce': 'DAY',
            'status': 'PENDING', 'quantity': '2', 'currency': 'USD', 'orderedAt': '2025-04-01T15:00:00Z',
            'execution': {'filledQuantity': '0', 'averageFilledPrice': None, 'filledAmount': None,
                          'commission': None, 'tax': None, 'filledAt': None, 'settlementDate': None}}


class Responses:
    def __init__(self, *responses):
        self.responses, self.calls, self.delays = list(responses), [], []
    def send(self, method, path, headers, body):
        self.calls.append((method, path, headers, body))
        if path == '/oauth2/token':
            return 200, {}, json.dumps(AUTH).encode()
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple):
            return result
        return 200, {}, json.dumps({'result': result}).encode()
    def client(self, **kwargs):
        return TossClient('fake-client', SECRET, '1', send=self.send, sleep=self.delays.append, **kwargs)


def test_accounts_prices_and_headers():
    response = Responses([{'accountSeq': 1}], [{'symbol': 'XLK', 'lastPrice': '100', 'currency': 'USD', 'timestamp': '2025-04-01T15:00:00Z'}])
    client = response.client()
    assert client.accounts() == [{'accountSeq': 1}]
    assert client.prices(['XLK'])[0]['lastPrice'] == '100'
    assert sum(call[1] == '/oauth2/token' for call in response.calls) == 1
    assert response.calls[1][2]['Authorization'] == 'Bearer ' + TOKEN


def test_pagination_deduplicates_overlap_and_sends_account():
    response = Responses({'orders': [order('one')], 'hasNext': True, 'nextCursor': 'next'},
                         {'orders': [order('one'), order('two')], 'hasNext': False, 'nextCursor': None})
    assert len(response.client().orders('CLOSED')) == 2
    assert 'cursor=next' in response.calls[-1][1]
    assert response.calls[-1][2]['X-Tossinvest-Account'] == '1'


def test_pagination_cycle_rejected():
    page = {'orders': [], 'hasNext': True, 'nextCursor': 'same'}
    response = Responses(page, page)
    with pytest.raises(BrokerError, match='CURSOR'):
        response.client().orders('CLOSED')


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_get_retries_bounded(status):
    response = Responses(*[(status, {'Retry-After': '99999'}, b'')]*3)
    with pytest.raises(BrokerError):
        response.client().accounts()
    assert len(response.calls) == 4  # One token request plus three GETs.
    assert response.delays == [10, 10]


def test_network_error_redacted_and_bounded(caplog, capsys):
    response = Responses(*[TimeoutError(SECRET + TOKEN)]*3)
    with pytest.raises(BrokerError) as exc:
        response.client().accounts()
    text = str(exc.value) + caplog.text + capsys.readouterr().out
    assert TOKEN not in text and SECRET not in text
    assert response.delays == [1, 2]


@pytest.mark.parametrize('status', [401, 403])
def test_auth_error_no_infinite_refresh(status):
    response = Responses((status, {}, (SECRET + TOKEN).encode()))
    with pytest.raises(BrokerError, match='AUTHORIZATION'):
        response.client().accounts()
    assert len(response.calls) == 2


@pytest.mark.parametrize('body', [b'bad json', b'[]', b'{"error":"fake"}', b'{"result":null}', b'x'*2_000_001])
def test_bad_response_rejected(body):
    response = Responses((200, {}, body))
    with pytest.raises(BrokerError):
        response.client().accounts()


@pytest.mark.parametrize('price', ['NaN', 'Infinity', '-1', '0'])
def test_invalid_quote_values(price):
    response = Responses([{'symbol': 'XLK', 'lastPrice': price, 'currency': 'USD', 'timestamp': '2025-04-01T15:00:00Z'}])
    with pytest.raises(BrokerError):
        response.client().prices(['XLK'])


def test_missing_quote_blocks():
    with pytest.raises(BrokerError):
        Responses([]).client().prices(['XLK'])


def test_order_identity_mismatch():
    with pytest.raises(BrokerError, match='MISMATCH'):
        Responses(order('different')).client().order('requested')


def test_unsupported_live_methods_make_no_request():
    response = Responses()
    client = response.client()
    for method in [client.submit, client.cancel]:
        with pytest.raises(BrokerError, match='DISABLED'):
            method({})
    assert response.calls == []


def test_concurrent_auth_refresh_once():
    response = Responses()
    client = response.client()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: client._auth(), range(8))) == [TOKEN]*8
    assert len(response.calls) == 1


def test_expiring_token_refreshed():
    clock = [0]
    response = Responses()
    client = response.client(clock=lambda: clock[0])
    client._auth()
    clock[0] = 86400
    client._auth()
    assert len(response.calls) == 2


def test_redirects_disabled():
    assert NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://evil.example') is None


def test_read_only_http_blackbox():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            assert self.path == '/oauth2/token'
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(AUTH).encode())
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'result': [{'accountSeq': 1}]}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def send(method, path, headers, body):
        request = Request(f'http://127.0.0.1:{server.server_port}' + path, data=body, headers=headers, method=method)
        with urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    try:
        client = TossClient('fake', SECRET, '1', send=send)
        assert client.accounts() == [{'accountSeq': 1}]
        assert calls == ['/oauth2/token', '/api/v1/accounts']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_missing_credentials_without_network(monkeypatch):
    monkeypatch.delenv('TOSS_CLIENT_ID',raising=False)
    monkeypatch.delenv('TOSS_CLIENT_SECRET',raising=False)
    with pytest.raises(BrokerError,match='CREDENTIALS'):
        TossClient()


def test_missing_account_without_auth_request():
    response=Responses()
    client=response.client()
    client.account=''
    with pytest.raises(BrokerError,match='ACCOUNT'):
        client.orders()
    assert not response.calls


@pytest.mark.parametrize('auth', [
    {'access_token':TOKEN,'token_type':'Bearer','expires_in':0},
    {'access_token':TOKEN,'token_type':'wrong','expires_in':1000},
    {'access_token':None,'token_type':'Bearer','expires_in':1000},
])
def test_bad_auth_schema(auth):
    def send(*args):
        return 200,{},json.dumps(auth).encode()
    with pytest.raises(BrokerError,match='AUTH_RESPONSE'):
        TossClient('fake',SECRET,'1',send=send).accounts()
