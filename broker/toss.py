"""Toss OpenAPI 1.2.24 adapter. Writes require explicit constructor opt-in.

POST operations are never automatically retried. Use TradingService for durable
operation tracking and risk checks; merely enabling this client is not readiness.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import ssl

from safety import SafetyError, decimal, utc

BASE = "https://openapi.tossinvest.com"
SYMBOL = re.compile(r"[A-Za-z0-9.\-]{1,30}\Z")


class BrokerError(SafetyError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(method, path, headers, body):
    # Host is fixed; redirects cannot carry Authorization to another origin.
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    request = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read(2_000_001)
    except HTTPError as error:
        return error.code, dict(error.headers), b""  # Never propagate response bodies.
    except (URLError, OSError, TimeoutError):
        raise BrokerError("NETWORK_UNAVAILABLE") from None


class TossClient:
    def __init__(self, client_id=None, client_secret=None, account=None, *, send=transport, sleep=time.sleep, clock=time.monotonic, allow_orders=False):
        self.allow_orders = allow_orders is True
        self._id = client_id or os.environ.get("TOSS_CLIENT_ID")
        self._secret = client_secret or os.environ.get("TOSS_CLIENT_SECRET")
        self.account = str(account or os.environ.get("TOSS_ACCOUNT_SEQ", ""))
        if not self._id or not self._secret:
            raise BrokerError("CREDENTIALS_REQUIRED")
        if self.account and not self.account.isdigit():
            raise BrokerError("INVALID_ACCOUNT")
        self._send, self._sleep, self._clock = send, sleep, clock
        self._token, self._expires = None, 0
        self._lock = threading.Lock()

    def _request(self, method, path, headers, body=None):
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                status, response_headers, raw = self._send(method, path, headers, body)
            except Exception:
                if method == "GET" and attempt + 1 < attempts:
                    self._sleep(2 ** attempt)
                    continue
                raise BrokerError("NETWORK_UNAVAILABLE") from None
            if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                try:
                    delay = max(1, min(10, float(response_headers.get("Retry-After", 2 ** attempt))))
                except (ValueError, TypeError):
                    delay = 2 ** attempt
                self._sleep(delay)
                continue
            if status in (401, 403):
                self._token = None
                raise BrokerError("AUTHORIZATION_FAILED")
            if status == 429:
                raise BrokerError("RATE_LIMITED")
            if not 200 <= status < 300:
                raise BrokerError("BROKER_HTTP_ERROR")
            try:
                if len(raw) > 2_000_000:
                    raise ValueError
                result = json.loads(raw)
                if not isinstance(result, dict) or "error" in result:
                    raise ValueError
                return result
            except (ValueError, TypeError):
                raise BrokerError("INVALID_RESPONSE") from None
        raise BrokerError("RETRIES_EXHAUSTED")

    def _auth(self):
        with self._lock:
            if self._token and self._clock() < self._expires:
                return self._token
            body = urlencode({"grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret}).encode()
            data = self._request("POST", "/oauth2/token", {"Content-Type": "application/x-www-form-urlencoded"}, body)
            if (not isinstance(data.get("access_token"), str) or not data["access_token"]
                    or data.get("token_type") != "Bearer" or type(data.get("expires_in")) is not int
                    or data["expires_in"] <= 60):
                raise BrokerError("INVALID_AUTH_RESPONSE")
            self._token = data["access_token"]
            self._expires = self._clock() + data["expires_in"] - 60
            return self._token

    def _get(self, path, params=None, account=False):
        if account and not self.account:
            raise BrokerError("ACCOUNT_REQUIRED")
        headers = {"Authorization": "Bearer " + self._auth(), "Accept": "application/json"}
        if account:
            headers["X-Tossinvest-Account"] = self.account
        data = self._request("GET", path + ("?" + urlencode(params) if params else ""), headers)
        if "result" not in data:
            raise BrokerError("INVALID_RESPONSE")
        return data["result"]

    def accounts(self):
        rows = self._get("/api/v1/accounts")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get("accountSeq")) is not int for r in rows):
            raise BrokerError("INVALID_ACCOUNTS")
        return rows

    def prices(self, symbols):
        if not symbols or len(symbols) > 200 or any(not SYMBOL.fullmatch(s) for s in symbols):
            raise BrokerError("INVALID_SYMBOL")
        rows = self._get("/api/v1/prices", {"symbols": ",".join(symbols)})
        try:
            if not isinstance(rows, list) or len(rows) != len(set(symbols)) or {r["symbol"] for r in rows} != set(symbols):
                raise ValueError
            for row in rows:
                decimal(row["lastPrice"])
                utc(row["timestamp"])
                if row["currency"] not in {"USD", "KRW"}:
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            raise BrokerError("INVALID_PRICES") from None
        return rows

    def holdings(self):
        result = self._get("/api/v1/holdings", account=True)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise BrokerError("INVALID_HOLDINGS")
        return result

    def buying_power(self, currency="USD"):
        if currency not in {"USD", "KRW"}:
            raise BrokerError("INVALID_CURRENCY")
        result = self._get("/api/v1/buying-power", {"currency": currency}, account=True)
        try:
            if result["currency"] != currency:
                raise ValueError
            return decimal(result["cashBuyingPower"], zero=True)
        except (KeyError, TypeError, ValueError):
            raise BrokerError("INVALID_BUYING_POWER") from None

    def sellable(self, symbol):
        if not SYMBOL.fullmatch(symbol):
            raise BrokerError("INVALID_SYMBOL")
        result = self._get("/api/v1/sellable-quantity", {"symbol": symbol}, account=True)
        try:
            return decimal(result["sellableQuantity"], zero=True)
        except (KeyError, TypeError, ValueError):
            raise BrokerError("INVALID_SELLABLE_QUANTITY") from None

    @staticmethod
    def validate_order(row, source="REST"):
        try:
            if (not isinstance(row["orderId"], str) or not row["orderId"]
                    or not SYMBOL.fullmatch(row["symbol"]) or row["side"] not in {"BUY", "SELL"}
                    or row["currency"] not in {"USD", "KRW"} or not isinstance(row["status"], str)):
                raise ValueError
            quantity = decimal(row["quantity"], zero=True)
            utc(row["orderedAt"])
            execution = row["execution"]
            filled = decimal(execution["filledQuantity"], zero=True)
            if filled > quantity:
                raise ValueError
            if filled:
                for key in ("averageFilledPrice", "filledAmount"):
                    if execution.get(key) is not None:
                        decimal(execution[key])
                if source == "REST" and execution.get("filledAt") is not None:
                    utc(execution["filledAt"])
            for key in ("commission", "tax"):
                if execution.get(key) is not None:
                    decimal(execution[key], zero=True)
            return row
        except (KeyError, TypeError, ValueError):
            raise BrokerError("INVALID_ORDER") from None

    def orders(self, status="OPEN"):
        if status not in {"OPEN", "CLOSED"}:
            raise BrokerError("INVALID_STATUS")
        params, seen, rows = {"status": status, "limit": 100}, set(), {}
        for _ in range(1000):
            page = self._get("/api/v1/orders", params, account=True)
            if not isinstance(page, dict) or not isinstance(page.get("orders"), list) or type(page.get("hasNext")) is not bool:
                raise BrokerError("INVALID_ORDER_PAGE")
            for row in page["orders"]:
                self.validate_order(row)
                if row["orderId"] in rows and rows[row["orderId"]] != row:
                    raise BrokerError("INCONSISTENT_ORDER_PAGES")
                rows[row["orderId"]] = row
            if not page["hasNext"]:
                return list(rows.values())
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise BrokerError("INVALID_CURSOR")
            seen.add(cursor)
            params["cursor"] = cursor
        raise BrokerError("PAGE_LIMIT_EXCEEDED")

    def order(self, order_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise BrokerError("INVALID_ORDER_ID")
        result = self.validate_order(self._get("/api/v1/orders/" + order_id, account=True))
        if result["orderId"] != order_id:
            raise BrokerError("ORDER_ID_MISMATCH")
        return result

    def _write(self, path, payload):
        if not self.allow_orders:
            raise BrokerError("LIVE_ORDER_TRANSPORT_DISABLED")
        if not self.account:
            raise BrokerError("ACCOUNT_REQUIRED")
        headers = {"Authorization": "Bearer " + self._auth(),
                   "X-Tossinvest-Account": self.account, "Content-Type": "application/json"}
        data = self._request("POST", path, headers, json.dumps(payload).encode())
        result = data.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("orderId"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", result["orderId"]):
            raise BrokerError("INVALID_ORDER_RESPONSE")
        return result

    @staticmethod
    def _limit_price(value):
        from decimal import Decimal
        price = decimal(value)
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        if price % tick:
            raise BrokerError("INVALID_ORDER_INCREMENT")
        return format(price, "f")

    def submit(self, intent):
        if not self.allow_orders:
            raise BrokerError("LIVE_ORDER_TRANSPORT_DISABLED")
        import live_core
        if intent.get("account") != self.account:
            raise BrokerError("ACCOUNT_MISMATCH")
        if intent.get("symbol") not in live_core.TRADE_TICKERS or intent.get("side") not in {"BUY","SELL"} or intent.get("currency") != "USD":
            raise BrokerError("UNSUPPORTED_ORDER")
        key = intent.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", key):
            raise BrokerError("INVALID_CLIENT_ORDER_ID")
        quantity = decimal(intent["quantity"])
        if quantity != quantity.to_integral_value():
            raise BrokerError("INVALID_ORDER_INCREMENT")
        result = self._write("/api/v1/orders", {"clientOrderId":key, "symbol":intent["symbol"],
            "side":intent["side"], "orderType":"LIMIT", "timeInForce":"DAY",
            "quantity":format(quantity,"f"), "price":self._limit_price(intent["price"])})
        if result.get("clientOrderId") != key:
            raise BrokerError("CLIENT_ORDER_ID_MISMATCH")
        return result["orderId"]

    def cancel(self, order_id):
        if not self.allow_orders:
            raise BrokerError("LIVE_ORDER_TRANSPORT_DISABLED")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise BrokerError("INVALID_ORDER_ID")
        result = self._write("/api/v1/orders/" + order_id + "/cancel", {})
        if result["orderId"] == order_id:
            raise BrokerError("OPERATION_ID_MISMATCH")
        return result["orderId"]

    def modify(self, order_id, price, *, quantity=None):
        if not self.allow_orders:
            raise BrokerError("LIVE_ORDER_TRANSPORT_DISABLED")
        if quantity is not None:
            raise BrokerError("US_MODIFY_QUANTITY_NOT_SUPPORTED")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", order_id):
            raise BrokerError("INVALID_ORDER_ID")
        result = self._write("/api/v1/orders/" + order_id + "/modify", {"orderType":"LIMIT", "price":self._limit_price(price)})
        if result["orderId"] == order_id:
            raise BrokerError("OPERATION_ID_MISMATCH")
        return result["orderId"]
