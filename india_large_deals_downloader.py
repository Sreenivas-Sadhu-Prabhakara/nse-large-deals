#!/usr/bin/env python3
"""
Download and merge Indian NSE/BSE bulk and block deals.

Default coverage:
    2020-01-01 through today
Sources:
    NSE historical bulk deals
    NSE historical block deals
    BSE historical bulk deals
    BSE historical block deals

The downloader:
- requests data month by month
- preserves every raw JSON response
- checkpoints successful windows in SQLite
- safely resumes after interruption
- normalizes NSE and BSE schemas
- calculates transaction value in INR and crore
- removes duplicate disclosures
- exports one merged, Excel-friendly CSV

Usage:
    python india_large_deals_downloader.py
    python india_large_deals_downloader.py --start 2020-01-01 --end 2026-07-15
    python india_large_deals_downloader.py --buys-only
    python india_large_deals_downloader.py --min-value-crore 5
    python india_large_deals_downloader.py --self-test
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError as exc:
    raise SystemExit(
        "Missing dependency 'requests'. Run: pip install -r requirements.txt"
    ) from exc


VERSION = "1.0.0"

NSE_HOME = "https://www.nseindia.com"
NSE_REPORT_PAGE = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"
NSE_ENDPOINTS = {
    "bulk": "https://www.nseindia.com/api/historical/bulk-deals",
    "block": "https://www.nseindia.com/api/historical/block-deals",
}

BSE_ENDPOINTS = {
    "bulk": "https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w",
    "block": "https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

CSV_FIELDS = [
    "deal_date",
    "exchange",
    "deal_type",
    "side",
    "is_purchase",
    "symbol",
    "security_code",
    "security_name",
    "client_name",
    "quantity",
    "price_inr",
    "trade_value_inr",
    "trade_value_crore",
    "remarks",
    "source_window_start",
    "source_window_end",
    "source_url",
    "downloaded_at_utc",
]


@dataclass(frozen=True)
class Window:
    start: date
    end: date


@dataclass
class Deal:
    deal_date: str
    exchange: str
    deal_type: str
    side: str
    symbol: str
    security_code: str
    security_name: str
    client_name: str
    quantity: float
    price_inr: float
    remarks: str
    source_window_start: str
    source_window_end: str
    source_url: str
    downloaded_at_utc: str

    @property
    def trade_value_inr(self) -> float:
        return self.quantity * self.price_inr

    @property
    def trade_value_crore(self) -> float:
        return self.trade_value_inr / 10_000_000

    @property
    def is_purchase(self) -> int:
        return 1 if self.side == "BUY" else 0

    def identity_hash(self) -> str:
        # Exchange disclosures are normally aggregated at this grain.
        components = [
            self.deal_date,
            self.exchange,
            self.deal_type,
            self.side,
            self.symbol,
            self.security_code,
            normalize_text(self.security_name),
            normalize_text(self.client_name),
            decimal_identity(self.quantity),
            decimal_identity(self.price_inr),
            normalize_text(self.remarks),
        ]
        return hashlib.sha256("\x1f".join(components).encode("utf-8")).hexdigest()


def decimal_identity(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return ""


def parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip()
    if not cleaned:
        return 0.0
    cleaned = cleaned.replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    cleaned = re.sub(r"[^0-9eE+\-.]", "", cleaned)
    if cleaned in {"", "-", ".", "-."}:
        return 0.0
    return float(cleaned)


def parse_date(value: Any) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    raw = raw.replace(".", "-")
    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d-%B-%Y",
        "%d %B %Y",
        "%Y/%m/%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue

    # Handles ISO timestamps such as 2026-07-14T18:30:00.000Z.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw


def normalize_side(value: Any) -> str:
    side = normalize_text(value).upper()
    if side in {"B", "BUY", "PURCHASE", "BOUGHT"} or side.startswith("B"):
        return "BUY"
    if side in {"S", "SELL", "SALE", "SOLD"} or side.startswith("S"):
        return "SELL"
    return side or "UNKNOWN"


def month_windows(start: date, end: date) -> Iterator[Window]:
    current = start
    while current <= end:
        last_day = calendar.monthrange(current.year, current.month)[1]
        window_end = min(date(current.year, current.month, last_day), end)
        yield Window(current, window_end)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


class DealStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deals (
                row_hash TEXT PRIMARY KEY,
                deal_date TEXT NOT NULL,
                exchange TEXT NOT NULL,
                deal_type TEXT NOT NULL,
                side TEXT NOT NULL,
                is_purchase INTEGER NOT NULL,
                symbol TEXT,
                security_code TEXT,
                security_name TEXT,
                client_name TEXT,
                quantity REAL NOT NULL,
                price_inr REAL NOT NULL,
                trade_value_inr REAL NOT NULL,
                trade_value_crore REAL NOT NULL,
                remarks TEXT,
                source_window_start TEXT,
                source_window_end TEXT,
                source_url TEXT,
                downloaded_at_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS download_status (
                exchange TEXT NOT NULL,
                deal_type TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                raw_file TEXT,
                completed_at_utc TEXT NOT NULL,
                PRIMARY KEY (exchange, deal_type, window_start, window_end)
            );

            CREATE INDEX IF NOT EXISTS idx_deals_date
                ON deals(deal_date);
            CREATE INDEX IF NOT EXISTS idx_deals_value
                ON deals(trade_value_inr);
            CREATE INDEX IF NOT EXISTS idx_deals_client
                ON deals(client_name);
            """
        )
        self.conn.commit()

    def is_complete(self, exchange: str, deal_type: str, window: Window) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM download_status
            WHERE exchange=? AND deal_type=? AND window_start=? AND window_end=?
            """,
            (exchange, deal_type, window.start.isoformat(), window.end.isoformat()),
        ).fetchone()
        return row is not None

    def clear_status(self, exchange: str, deal_type: str, window: Window) -> None:
        self.conn.execute(
            """
            DELETE FROM download_status
            WHERE exchange=? AND deal_type=? AND window_start=? AND window_end=?
            """,
            (exchange, deal_type, window.start.isoformat(), window.end.isoformat()),
        )
        self.conn.commit()

    def insert_deals(self, deals: Iterable[Deal]) -> int:
        inserted = 0
        for deal in deals:
            before = self.conn.total_changes
            self.conn.execute(
                """
                INSERT OR IGNORE INTO deals (
                    row_hash, deal_date, exchange, deal_type, side, is_purchase,
                    symbol, security_code, security_name, client_name,
                    quantity, price_inr, trade_value_inr, trade_value_crore,
                    remarks, source_window_start, source_window_end,
                    source_url, downloaded_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deal.identity_hash(),
                    deal.deal_date,
                    deal.exchange,
                    deal.deal_type,
                    deal.side,
                    deal.is_purchase,
                    deal.symbol,
                    deal.security_code,
                    deal.security_name,
                    deal.client_name,
                    deal.quantity,
                    deal.price_inr,
                    deal.trade_value_inr,
                    deal.trade_value_crore,
                    deal.remarks,
                    deal.source_window_start,
                    deal.source_window_end,
                    deal.source_url,
                    deal.downloaded_at_utc,
                ),
            )
            if self.conn.total_changes > before:
                inserted += 1
        self.conn.commit()
        return inserted

    def mark_complete(
        self,
        exchange: str,
        deal_type: str,
        window: Window,
        row_count: int,
        raw_file: Path,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO download_status (
                exchange, deal_type, window_start, window_end,
                row_count, raw_file, completed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exchange,
                deal_type,
                window.start.isoformat(),
                window.end.isoformat(),
                row_count,
                str(raw_file),
                utc_now(),
            ),
        )
        self.conn.commit()

    def export_csv(
        self,
        path: Path,
        buys_only: bool = False,
        min_value_crore: float = 0.0,
    ) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        conditions = ["trade_value_crore >= ?"]
        params: list[Any] = [min_value_crore]
        if buys_only:
            conditions.append("side = 'BUY'")
        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT {", ".join(CSV_FIELDS)}
            FROM deals
            WHERE {where_clause}
            ORDER BY deal_date DESC, trade_value_inr DESC,
                     exchange, deal_type, symbol, client_name
        """
        cursor = self.conn.execute(sql, params)
        count = 0
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_FIELDS)
            for row in cursor:
                writer.writerow(row)
                count += 1
        return count

    def stats(self) -> list[tuple[Any, ...]]:
        return self.conn.execute(
            """
            SELECT exchange, deal_type, side,
                   COUNT(*) AS rows,
                   ROUND(SUM(trade_value_inr) / 10000000.0, 2) AS value_crore
            FROM deals
            GROUP BY exchange, deal_type, side
            ORDER BY exchange, deal_type, side
            """
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


class OptionalPlaywrightNSE:
    """Persistent browser fallback used only when requests is blocked."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None

    def available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": NSE_REPORT_PAGE,
                "Accept-Language": "en-IN,en;q=0.9",
            },
            viewport={"width": 1440, "height": 900},
        )
        self._page = context.new_page()
        self._page.goto(NSE_HOME, wait_until="domcontentloaded", timeout=60_000)

    def fetch_json(self, url: str) -> Any:
        self.start()
        assert self._page is not None
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if response is None:
            raise RuntimeError("Playwright received no NSE response")
        if not response.ok:
            raise RuntimeError(f"Playwright NSE HTTP {response.status}")
        return response.json()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()


class Downloader:
    def __init__(
        self,
        raw_dir: Path,
        retries: int,
        delay_seconds: float,
        browser_fallback: bool,
    ) -> None:
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.retries = retries
        self.delay_seconds = delay_seconds
        self.browser_fallback = browser_fallback
        self.nse_session = requests.Session()
        self.bse_session = requests.Session()
        self.playwright = OptionalPlaywrightNSE()

        self.nse_session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": NSE_REPORT_PAGE,
                "Connection": "keep-alive",
            }
        )
        self.bse_session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-IN,en;q=0.9",
                "Referer": "https://www.bseindia.com/markets/equity/EQReports/BulknBlockDeals.aspx",
                "Origin": "https://www.bseindia.com",
                "Connection": "keep-alive",
            }
        )
        self._prime_nse()

    def _prime_nse(self) -> None:
        try:
            self.nse_session.get(NSE_HOME, timeout=20)
            self.nse_session.get(NSE_REPORT_PAGE, timeout=20)
        except requests.RequestException:
            # Actual API request will retry and can use browser fallback.
            pass

    def _request_json(
        self,
        session: requests.Session,
        url: str,
        *,
        allow_playwright_nse: bool = False,
    ) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = session.get(url, timeout=45)
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code in {401, 403, 429} and allow_playwright_nse:
                    self._prime_nse()
                if response.status_code == 429:
                    raise RuntimeError("HTTP 429 rate limited")
                response.raise_for_status()

                text_head = response.text[:200].lower()
                if "html" in content_type or "<html" in text_head or "<!doctype" in text_head:
                    raise RuntimeError("Received HTML instead of JSON; likely anti-bot response")
                return response.json()
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    sleep_for = min(30.0, (2 ** (attempt - 1)) + random.random())
                    logging.warning(
                        "Attempt %s/%s failed: %s; retrying",
                        attempt,
                        self.retries,
                        exc,
                    )
                    time.sleep(sleep_for)
                    if allow_playwright_nse:
                        self._prime_nse()

        if allow_playwright_nse and self.browser_fallback and self.playwright.available():
            logging.warning("Requests was blocked; trying persistent Playwright NSE fallback")
            return self.playwright.fetch_json(url)

        if allow_playwright_nse and self.browser_fallback and not self.playwright.available():
            raise RuntimeError(
                f"NSE requests failed: {last_error}. "
                "Install the optional browser fallback with "
                "'pip install -r requirements-browser.txt' and "
                "'playwright install chromium'."
            ) from last_error

        raise RuntimeError(f"Request failed after {self.retries} attempts: {last_error}") from last_error

    def fetch(self, exchange: str, deal_type: str, window: Window) -> tuple[Any, str, Path]:
        downloaded_at = utc_now()
        if exchange == "NSE":
            params = {
                "from": window.start.strftime("%d-%m-%Y"),
                "to": window.end.strftime("%d-%m-%Y"),
            }
            url = f"{NSE_ENDPOINTS[deal_type]}?{urlencode(params)}"
            payload = self._request_json(
                self.nse_session,
                url,
                allow_playwright_nse=True,
            )
        elif exchange == "BSE":
            params = {
                "quotetype": "EQ",
                "strdate": window.start.strftime("%Y%m%d"),
                "todate": window.end.strftime("%Y%m%d"),
                "segment": "D",
            }
            url = f"{BSE_ENDPOINTS[deal_type]}?{urlencode(params)}"
            payload = self._request_json(self.bse_session, url)
        else:
            raise ValueError(f"Unsupported exchange: {exchange}")

        raw_path = (
            self.raw_dir
            / exchange.lower()
            / deal_type
            / f"{window.start.isoformat()}_{window.end.isoformat()}.json"
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_document = {
            "_metadata": {
                "exchange": exchange,
                "deal_type": deal_type.upper(),
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "source_url": url,
                "downloaded_at_utc": downloaded_at,
                "downloader_version": VERSION,
            },
            "payload": payload,
        }
        raw_path.write_text(
            json.dumps(raw_document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload, url, raw_path

    def close(self) -> None:
        self.nse_session.close()
        self.bse_session.close()
        self.playwright.close()


def extract_list(payload: Any, preferred_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]

    # Schema-drift fallback: use the first list of dictionaries.
    for value in payload.values():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return [row for row in value if isinstance(row, dict)]
    return []


def parse_nse(
    payload: Any,
    deal_type: str,
    window: Window,
    source_url: str,
    downloaded_at: str,
) -> list[Deal]:
    rows = extract_list(payload, ("data", "Data", "records"))
    deals: list[Deal] = []

    for row in rows:
        symbol = normalize_text(
            first_value(row, "BD_SYMBOL", "symbol", "SYMBOL")
        )
        security_name = normalize_text(
            first_value(row, "BD_SCRIP_NAME", "name", "securityName", "Security Name")
        )
        client_name = normalize_text(
            first_value(row, "BD_CLIENT_NAME", "clientName", "CLIENT_NAME", "Client Name")
        )
        side = normalize_side(
            first_value(row, "BD_BUY_SELL", "buySell", "buyOrSell", "Buy / Sell")
        )
        quantity = parse_number(
            first_value(row, "BD_QTY_TRD", "qty", "quantityTraded", "QUANTITY")
        )
        price = parse_number(
            first_value(row, "BD_TP_WATP", "watp", "tradedPrice", "PRICE")
        )
        deal_date = parse_date(
            first_value(row, "mTIMESTAMP", "date", "BD_DATE", "TIMESTAMP")
        )
        remarks = normalize_text(first_value(row, "remarks", "REMARKS"))

        if not deal_date or not client_name or quantity <= 0 or price <= 0:
            logging.debug("Skipping incomplete NSE row: %r", row)
            continue

        deals.append(
            Deal(
                deal_date=deal_date,
                exchange="NSE",
                deal_type=deal_type.upper(),
                side=side,
                symbol=symbol,
                security_code="",
                security_name=security_name,
                client_name=client_name,
                quantity=quantity,
                price_inr=price,
                remarks=remarks,
                source_window_start=window.start.isoformat(),
                source_window_end=window.end.isoformat(),
                source_url=source_url,
                downloaded_at_utc=downloaded_at,
            )
        )
    return deals


def parse_bse(
    payload: Any,
    deal_type: str,
    window: Window,
    source_url: str,
    downloaded_at: str,
) -> list[Deal]:
    rows = extract_list(payload, ("Table", "Table1", "data", "Data"))
    deals: list[Deal] = []

    for row in rows:
        security_code = normalize_text(
            first_value(row, "SCRIP_CODE", "ScripCode", "scrip_code")
        )
        symbol = normalize_text(
            first_value(row, "SYMBOL", "Symbol", "symbol")
        ) or security_code
        security_name = normalize_text(
            first_value(row, "ScripName", "SCRIP_NAME", "securityName", "CompanyName")
        )
        client_name = normalize_text(
            first_value(row, "CLIENT_NAME", "ClientName", "clientName")
        )
        side = normalize_side(
            first_value(row, "TRANSACTION_TYPE", "BuySell", "buySell")
        )
        quantity = parse_number(
            first_value(row, "QUANTITY", "Quantity", "qty")
        )
        price = parse_number(
            first_value(row, "PRICE", "Price", "tradePrice")
        )
        deal_date = parse_date(
            first_value(row, "DEAL_DATE", "DealDate", "date")
        )
        remarks = normalize_text(
            first_value(row, "REMARKS", "Remarks", "SENDTOWEBSITE")
        )

        if not deal_date or not client_name or quantity <= 0 or price <= 0:
            logging.debug("Skipping incomplete BSE row: %r", row)
            continue

        deals.append(
            Deal(
                deal_date=deal_date,
                exchange="BSE",
                deal_type=deal_type.upper(),
                side=side,
                symbol=symbol,
                security_code=security_code,
                security_name=security_name,
                client_name=client_name,
                quantity=quantity,
                price_inr=price,
                remarks=remarks,
                source_window_start=window.start.isoformat(),
                source_window_end=window.end.isoformat(),
                source_url=source_url,
                downloaded_at_utc=downloaded_at,
            )
        )
    return deals


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Use YYYY-MM-DD."
        ) from exc


def self_test() -> None:
    window = Window(date(2020, 1, 1), date(2020, 1, 31))
    nse_payload = {
        "data": [
            {
                "BD_SYMBOL": "TESTNSE",
                "BD_SCRIP_NAME": "TEST NSE LTD",
                "BD_CLIENT_NAME": "EXAMPLE FUND",
                "BD_BUY_SELL": "B",
                "BD_QTY_TRD": "1,000,000",
                "BD_TP_WATP": "125.50",
                "mTIMESTAMP": "15-Jan-2020",
            }
        ]
    }
    bse_payload = {
        "Table": [
            {
                "DEAL_DATE": "16/01/2020",
                "SCRIP_CODE": 500000,
                "ScripName": "TEST BSE LTD",
                "CLIENT_NAME": "EXAMPLE INVESTOR",
                "TRANSACTION_TYPE": "S",
                "QUANTITY": 500000,
                "PRICE": 200.0,
            }
        ]
    }
    nse = parse_nse(nse_payload, "bulk", window, "nse-test", utc_now())
    bse = parse_bse(bse_payload, "block", window, "bse-test", utc_now())
    assert len(nse) == 1
    assert nse[0].trade_value_inr == 125_500_000
    assert nse[0].side == "BUY"
    assert len(bse) == 1
    assert bse[0].trade_value_inr == 100_000_000
    assert bse[0].side == "SELL"
    print("Self-test passed: NSE/BSE parsing and value calculation are working.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and merge NSE/BSE bulk and block deal disclosures."
    )
    parser.add_argument(
        "--start",
        type=parse_iso_date,
        default=date(2020, 1, 1),
        help="Start date, YYYY-MM-DD. Default: 2020-01-01",
    )
    parser.add_argument(
        "--end",
        type=parse_iso_date,
        default=date.today(),
        help="End date, YYYY-MM-DD. Default: today",
    )
    parser.add_argument(
        "--exchanges",
        default="NSE,BSE",
        help="Comma-separated exchanges. Default: NSE,BSE",
    )
    parser.add_argument(
        "--deal-types",
        default="bulk,block",
        help="Comma-separated types. Default: bulk,block",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("india_large_deals_output"),
        help="Output directory.",
    )
    parser.add_argument(
        "--output-name",
        default="india_bulk_block_deals_2020_to_today.csv",
        help="Merged CSV filename.",
    )
    parser.add_argument(
        "--buys-only",
        action="store_true",
        help="Export only BUY disclosures. Downloaded database still keeps BUY and SELL.",
    )
    parser.add_argument(
        "--min-value-crore",
        type=float,
        default=0.0,
        help="Only export deals at or above this INR crore value.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.25,
        help="Polite delay between successful requests in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="HTTP retry attempts per monthly window.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download windows already marked complete.",
    )
    parser.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help="Disable optional Playwright fallback for NSE anti-bot responses.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed logs.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run offline parser tests and exit.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.start > args.end:
        raise SystemExit("--start must be on or before --end")
    if args.end > date.today():
        logging.warning("End date is in the future; exchange data will only exist through today.")

    exchanges = [item.strip().upper() for item in args.exchanges.split(",") if item.strip()]
    deal_types = [item.strip().lower() for item in args.deal_types.split(",") if item.strip()]

    invalid_exchanges = set(exchanges) - {"NSE", "BSE"}
    invalid_types = set(deal_types) - {"bulk", "block"}
    if invalid_exchanges:
        raise SystemExit(f"Unsupported exchanges: {sorted(invalid_exchanges)}")
    if invalid_types:
        raise SystemExit(f"Unsupported deal types: {sorted(invalid_types)}")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    db_path = output_dir / "india_large_deals.sqlite"
    csv_path = output_dir / args.output_name

    store = DealStore(db_path)
    downloader = Downloader(
        raw_dir=raw_dir,
        retries=max(1, args.retries),
        delay_seconds=max(0.0, args.delay),
        browser_fallback=not args.no_browser_fallback,
    )

    failures: list[str] = []
    try:
        windows = list(month_windows(args.start, args.end))
        total_jobs = len(windows) * len(exchanges) * len(deal_types)
        job_number = 0

        for window in windows:
            for exchange in exchanges:
                for deal_type in deal_types:
                    job_number += 1
                    label = (
                        f"[{job_number}/{total_jobs}] {exchange} {deal_type.upper()} "
                        f"{window.start} to {window.end}"
                    )

                    if store.is_complete(exchange, deal_type, window) and not args.force:
                        logging.info("%s — already complete, skipping", label)
                        continue
                    if args.force:
                        store.clear_status(exchange, deal_type, window)

                    logging.info("%s — downloading", label)
                    try:
                        downloaded_at = utc_now()
                        payload, source_url, raw_path = downloader.fetch(
                            exchange, deal_type, window
                        )
                        if exchange == "NSE":
                            deals = parse_nse(
                                payload,
                                deal_type,
                                window,
                                source_url,
                                downloaded_at,
                            )
                        else:
                            deals = parse_bse(
                                payload,
                                deal_type,
                                window,
                                source_url,
                                downloaded_at,
                            )

                        inserted = store.insert_deals(deals)
                        store.mark_complete(
                            exchange,
                            deal_type,
                            window,
                            len(deals),
                            raw_path,
                        )
                        logging.info(
                            "%s — parsed %s rows, inserted %s new rows",
                            label,
                            len(deals),
                            inserted,
                        )
                        if args.delay > 0:
                            time.sleep(args.delay)
                    except Exception as exc:
                        message = f"{label}: {exc}"
                        failures.append(message)
                        logging.error("%s", message)

        exported = store.export_csv(
            csv_path,
            buys_only=args.buys_only,
            min_value_crore=max(0.0, args.min_value_crore),
        )

        print()
        print("=" * 78)
        print(f"Merged CSV: {csv_path.resolve()}")
        print(f"SQLite checkpoint/database: {db_path.resolve()}")
        print(f"Rows exported: {exported:,}")
        print("Stored rows by exchange/type/side:")
        for exchange, deal_type, side, rows, value_crore in store.stats():
            print(
                f"  {exchange:3} {deal_type:5} {side:7} "
                f"{rows:8,} rows | ₹{value_crore:,.2f} crore disclosed value"
            )
        if failures:
            failure_path = output_dir / "failed_windows.txt"
            failure_path.write_text("\n".join(failures) + "\n", encoding="utf-8")
            print(f"Failed windows: {len(failures)} — see {failure_path.resolve()}")
            print("Re-run the same command; completed windows will be skipped.")
        else:
            print("All requested windows completed successfully.")
        print("=" * 78)
        return 0 if not failures else 2
    finally:
        downloader.close()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
