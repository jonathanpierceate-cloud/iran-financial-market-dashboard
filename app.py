# -*- coding: utf-8 -*-
"""Persian financial dashboard. Run with: streamlit run app.py"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ------------------------------- Page setup -------------------------------
st.set_page_config(page_title="داشبورد بازارهای مالی ایران", page_icon="📊", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;600;700;800&display=swap');
html,body,[class*="css"],[data-testid="stAppViewContainer"]{font-family:'Vazirmatn',sans-serif;direction:rtl}
[data-testid="stAppViewContainer"],[data-testid="stSidebar"]{direction:rtl}
.block-container{padding-top:1.2rem;padding-bottom:2rem}
[data-testid="stMetric"]{direction:rtl;text-align:right;background:#142f48;padding:14px 17px;border-radius:12px;border:1px solid #254d69}
[data-testid="stMetricLabel"],[data-testid="stMetricValue"],[data-testid="stMetricDelta"]{justify-content:flex-start;text-align:right}
.stTabs [data-baseweb="tab-list"]{direction:rtl;gap:8px}
h1,h2,h3,p{ text-align:right }
</style>
""", unsafe_allow_html=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36", "Accept": "application/json,text/html,*/*"}
TROY_OUNCE_GRAMS = 31.1034768
DB_PATH = Path(__file__).with_name("market_flow.sqlite3")
browser_tse_feed = components.declare_component(
    "browser_tse_feed", path=str(Path(__file__).with_name("browser_tse_component"))
)


def http_get(url: str, params: dict | None = None, timeout: int = 15):
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        return exc


def json_get(url: str, params: dict | None = None):
    response = http_get(url, params=params)
    if isinstance(response, Exception):
        return None, str(response)
    try:
        return response.json(), None
    except (ValueError, requests.RequestException) as exc:
        return None, str(exc)


def num(row: dict, *keys, default: float = 0.0) -> float:
    for key in keys:
        try:
            value = row.get(key)
            if value is not None:
                return float(str(value).replace(",", ""))
        except (AttributeError, TypeError, ValueError):
            continue
    return default


def payload_rows(data, *keys):
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list):
                return value
    return data if isinstance(data, list) else []


def fmt(value, digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def rial_to_toman(value):
    return None if value is None else value / 10


def daily_delta(key: str, value, *, unit: str = "%", percentage: bool = True):
    """Return a Streamlit delta against this browser session's last value yesterday.

    Keep today's value fresh on reruns so it becomes yesterday's final observed
    value after the date rolls over. Never imply a comparison when a day is missing.
    """
    if value is None:
        return None
    try:
        current = float(value)
    except (TypeError, ValueError):
        return None
    today = datetime.now().date().isoformat()
    snapshots = st.session_state.setdefault("_daily_metric_snapshots", {})
    previous = snapshots.get(key)
    delta = None
    if previous and previous.get("date") == (datetime.now().date() - timedelta(days=1)).isoformat():
        old = previous.get("value")
        if old is not None:
            change = current - float(old)
            if percentage:
                change = change / abs(float(old)) * 100 if float(old) else None
                delta = f"{change:+.2f}%" if change is not None else None
            else:
                delta = f"{change:+,.2f} {unit}"
    snapshots[key] = {"date": today, "value": current}
    return delta


# --------------------------------- Markets ---------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def get_usdt():
    errors = []
    # Tabdeal's public spot API quotes USDTIRT in toman (IRT). Use its most
    # recent executed trade for the displayed last price; depth is a fallback.
    response = http_get("https://api1.tabdeal.org/r/api/v1/trades", {"symbol": "USDTIRT", "limit": 1})
    if not isinstance(response, Exception):
        try:
            trades = response.json()
            trade = trades[0] if isinstance(trades, list) and trades else {}
            price_toman = num(trade, "price")
            if price_toman > 0:
                # Tabdeal's documented public market API does not expose a
                # spot 24-hour ticker; do not mislabel a partial trade sample.
                return {"toman": price_toman, "high24": None, "low24": None, "volume24": None,
                        "rial": price_toman * 10, "source": "صرافی تبدیل · آخرین معاملهٔ USDT/IRT", "error": None}
            errors.append("پاسخ معاملات تبدیل قیمت معتبر نداشت.")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            errors.append(str(exc))
    else:
        errors.append(str(response))

    response = http_get("https://api1.tabdeal.org/r/api/v1/depth", {"symbol": "USDTIRT", "limit": 5})
    if not isinstance(response, Exception):
        try:
            data = response.json()
            bids, asks = data.get("bids", []), data.get("asks", [])
            bid = float(bids[0][0]) if bids else 0
            ask = float(asks[0][0]) if asks else 0
            price_toman = (bid + ask) / 2 if bid and ask else bid or ask
            if price_toman > 0:
                return {"toman": price_toman, "high24": None, "low24": None, "volume24": None,
                        "rial": price_toman * 10, "source": "صرافی تبدیل · میانگین بهترین خرید و فروش", "error": None}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            errors.append(str(exc))
    else:
        errors.append(str(response))

    # Fallbacks are used only if Tabdeal is unreachable, and the source is
    # explicitly labeled so the dashboard never implies otherwise.
    for url in ("https://apiv2.nobitex.ir/v3/orderbook/USDTIRT", "https://api.nobitex.ir/v2/orderbook/USDTIRT"):
        response = http_get(url)
        if isinstance(response, Exception):
            errors.append(str(response))
            continue
        try:
            data = response.json()
            bids, asks = data.get("bids", []), data.get("asks", [])
            bid = float(bids[0][0]) if bids else 0
            ask = float(asks[0][0]) if asks else 0
            price = (bid + ask) / 2 if bid and ask else bid or ask
            if price:
                return {"toman": price / 10, "high24": None, "low24": None, "volume24": None,
                        "rial": price, "source": "نوبیتکس (جایگزین تبدیل) · میانگین بهترین خرید و فروش", "error": None}
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            errors.append(str(exc))
    for url in ("https://api.wallex.ir/v1/markets", "https://api.wallex.ir/v1/markets/stats"):
        response = http_get(url)
        if isinstance(response, Exception):
            errors.append(str(response))
            continue
        try:
            data = response.json()
            result = data.get("result", {})
            markets = result.get("symbols", result) if isinstance(result, dict) else {}
            if isinstance(markets, dict):
                for symbol in ("USDTTMN", "USDTIRT"):
                    row = markets.get(symbol, {})
                    price = num(row, "lastPrice", "last", "price", "close") if isinstance(row, dict) else 0
                    if price:
                        rial = price * (10 if symbol == "USDTTMN" else 1)
                        return {"toman": rial / 10, "high24": None, "low24": None, "volume24": None,
                                "rial": rial, "source": "والکس (جایگزین تبدیل)", "error": None}
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(str(exc))
    return {"toman": None, "high24": None, "low24": None, "volume24": None,
            "rial": None, "source": "ناموجود", "error": "؛ ".join(errors[-3:])}


@st.cache_data(ttl=60, show_spinner=False)
def get_tgju_previous_daily_close(profile: str):
    """Return the latest completed TGJU daily candle (prices are in rial)."""
    url = f"https://api.tgju.org/v1/market/indicator/summary-table-data/{profile}"
    data, error = json_get(url)
    if error:
        return {"close": None, "date": None, "error": error}
    try:
        today = datetime.now(ZoneInfo("Asia/Tehran")).date()
        for row in data.get("data", []):
            if len(row) < 7:
                continue
            candle_date = datetime.strptime(str(row[6]), "%Y/%m/%d").date()
            if candle_date < today:
                close = float(str(row[3]).replace(",", ""))
                if close > 0:
                    return {"close": close, "date": candle_date.isoformat(), "error": None}
        return {"close": None, "date": None, "error": "کندل روزانهٔ بسته‌شده در تاریخچه پیدا نشد."}
    except (AttributeError, TypeError, ValueError, IndexError) as exc:
        return {"close": None, "date": None, "error": str(exc)}


@st.cache_data(ttl=60, show_spinner=False)
def get_previous_ounce_daily_close():
    """Return the last completed COMEX gold futures daily candle from Yahoo."""
    response = http_get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
                        {"range": "5d", "interval": "1d"})
    if isinstance(response, Exception):
        return {"close": None, "date": None, "error": str(response)}
    try:
        result = response.json()["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]
        zone = ZoneInfo(result["meta"].get("exchangeTimezoneName", "America/New_York"))
        active_date = datetime.now(zone).date()
        candles = []
        for stamp, close in zip(result.get("timestamp", []), quote.get("close", [])):
            if close is None:
                continue
            candle_date = datetime.fromtimestamp(stamp, zone).date()
            if candle_date < active_date:
                candles.append((candle_date, float(close)))
        if not candles:
            return {"close": None, "date": None, "error": "کندل روزانهٔ بسته‌شدهٔ انس پیدا نشد."}
        candle_date, close = candles[-1]
        return {"close": close, "date": candle_date.isoformat(), "error": None}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"close": None, "date": None, "error": str(exc)}


def change_from_close(current, previous, digits: int = 2):
    if current is None or previous in (None, 0):
        return None
    return f"{(float(current) / float(previous) - 1) * 100:+.{digits}f}%"


@st.cache_data(ttl=60, show_spinner=False)
def get_rahavard_gold():
    """Rahavard public gold page data: entity 2016 = gold ounce, 1560 = local 18k."""
    data, error = json_get("https://rahavard365.com/api/v2/gold/light-charts")
    if error:
        return {"ounce": None, "local18_rial": None, "ounce_time": None, "local_time": None, "error": error}
    try:
        values = data.get("data", [])
        ounce = next((x for x in values if str(x.get("entity_id")) == "2016"), None)
        local = next((x for x in values if str(x.get("entity_id")) == "1560"), None)
        if not ounce or not num(ounce, "close_price"):
            return {"ounce": None, "local18_rial": None, "ounce_time": None, "local_time": None, "error": "قیمت انس در پاسخ ره‌آورد نبود."}
        return {"ounce": num(ounce, "close_price"), "local18_rial": num(local, "close_price") or None,
                "ounce_time": ounce.get("last_date_time"), "local_time": local.get("last_date_time") if local else None,
                "error": None}
    except (TypeError, AttributeError, StopIteration) as exc:
        return {"ounce": None, "local18_rial": None, "ounce_time": None, "local_time": None, "error": str(exc)}


@st.cache_data(ttl=60, show_spinner=False)
def get_ounce_fallback():
    response = http_get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F", {"range": "1d", "interval": "5m"})
    if not isinstance(response, Exception):
        try:
            values = response.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            valid = [value for value in values if value is not None]
            if valid:
                return float(valid[-1]), "Yahoo Finance GC=F", None
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            error = str(exc)
    else:
        error = str(response)
    response = http_get("https://api.binance.com/api/v3/ticker/price", {"symbol": "PAXGUSDT"})
    if not isinstance(response, Exception):
        try:
            return float(response.json()["price"]), "Binance PAXG/USDT (proxy)", error
        except (ValueError, TypeError, KeyError) as exc:
            error += "; " + str(exc)
    else:
        error += "; " + str(response)
    return None, "ناموجود", error


@st.cache_data(ttl=60, show_spinner=False)
def get_tgju(profile: str):
    response = http_get("https://www.tgju.org/profile/" + profile)
    if isinstance(response, Exception):
        return None, str(response)
    try:
        html = response.content.decode(response.encoding or "utf-8", errors="replace")
        match = re.search(r'data-col=["\']info\.last_trade\.PDrCotVal["\'][^>]*>.*?([۰-۹٠-٩0-9][۰-۹٠-٩0-9,٬]*)', html, re.S | re.I)
        if not match:
            return None, "قیمت در ساختار صفحهٔ TGJU پیدا نشد."
        digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٬", "01234567890123456789,")
        return float(match.group(1).translate(digits).replace(",", "")), None
    except (ValueError, TypeError, UnicodeError, requests.RequestException) as exc:
        return None, str(exc)


def get_tse_data(browser_data: dict | None):
    """Parse TSETMC replies fetched directly by the visitor's browser."""
    errors = (browser_data or {}).get("errors", {}) if isinstance(browser_data, dict) else {}
    primary = (browser_data or {}).get("primary") if isinstance(browser_data, dict) else None
    if not isinstance(primary, dict):
        details = "؛ ".join(f"{key}: {value}" for key, value in errors.items()) if errors else ""
        return {"symbols": pd.DataFrame(), "index": None, "index_pct": None,
                "date": datetime.now().strftime("%Y%m%d"), "net_flow_toman": None,
                "flow_count": 0, "nav_candidates": [],
                "error": details or "در انتظار دریافت دادهٔ TSETMC از مرورگر شما هستیم."}

    watch = primary.get("watch", {})
    clients = primary.get("clients", {})
    indexes = primary.get("indexes", {})
    overview = primary.get("overview", {})
    market_rows = payload_rows(watch, "marketwatch", "marketWatch", "marketWatchDto")
    client_rows = payload_rows(clients, "clientTypeAllDto", "clientTypeAll")
    index_rows = payload_rows(indexes, "indexB1", "indexB1LastAll")
    client_map = {str(row.get("insCode", "")): row for row in client_rows if isinstance(row, dict)}
    index_row = next((row for row in index_rows if str(row.get("insCode")) in {"32097828799138957", "32097828820363860"}), None)
    summary = overview.get("marketOverview", {}) if isinstance(overview, dict) else {}
    if index_row:
        index_value = num(index_row, "xDrNivJIdx004", "xDrNivIn", "indexValue", "lastValue") or None
        index_pct = num(index_row, "xVarIdxJRfV", "xVarIdxPercent", "percent", "changePercent") or None
    else:
        index_value = num(summary, "indexLastValue") or None
        change = num(summary, "indexChange")
        index_pct = change / (index_value - change) * 100 if index_value and index_value != change else None
    date = str(int(num(summary, "marketActivityDEven"))) if num(summary, "marketActivityDEven") else datetime.now().strftime("%Y%m%d")
    if not market_rows:
        details = "؛ ".join(f"{key}: {value}" for key, value in errors.items()) if errors else ""
        return {"symbols": pd.DataFrame(), "index": index_value, "index_pct": index_pct, "date": date,
                "net_flow_toman": None, "flow_count": 0, "nav_candidates": [],
                "error": details or "مرورگر دادهٔ دیده‌بان TSETMC را دریافت نکرد."}

    nav_map = browser_data.get("nav", {}) if isinstance(browser_data.get("nav"), dict) else {}
    records = []
    net_flow_rial = 0.0
    flow_count = 0
    for row in market_rows:
        try:
            code = str(row.get("insCode", ""))
            client = client_map.get(code, {})
            last = num(row, "pdv", "pDrCotVal", "last")
            close = num(row, "pcl", "pClosing", "closingPrice", "close")
            yesterday = num(row, "py", "priceYesterday", "yesterdayPrice")
            first = num(row, "pf", "priceFirst", "firstPrice")
            volume = num(row, "qtj", "qTotTran5J", "totalVolume")
            base_volume = num(row, "bv", "baseVol", "qMv", "volumeBase")
            trades = num(row, "ztt", "zTotTran", "tradeCount")
            last_pct = (last / yesterday - 1) * 100 if last and yesterday else 0.0
            close_pct = (close / yesterday - 1) * 100 if close and yesterday else 0.0
            buy_volume = num(client, "buy_I_Volume", "buyIVolume")
            buy_count = num(client, "buy_CountI", "buyCountI")
            sell_volume = num(client, "sell_I_Volume", "sellIVolume")
            sell_count = num(client, "sell_CountI", "sellCountI")
            buy_avg = buy_volume / buy_count if buy_count else 0.0
            sell_avg = sell_volume / sell_count if sell_count else 0.0
            power = buy_avg / sell_avg if sell_avg else 0.0
            seller_power = sell_avg / buy_avg if buy_avg else 0.0
            real_buy_pct = buy_volume / volume * 100 if volume else 0.0
            yval = int(num(row, "yVal"))
            etf = yval == 305
            liquidity_score = (20 if trades > 50 and volume > 0 else 0) if etf else (20 if base_volume and volume >= base_volume else 0)
            score = (25 if power >= 1.5 else 0) + (15 if power >= 2.5 else 0) + (15 if real_buy_pct >= 65 else 0)
            score += liquidity_score + (15 if close and last >= close else 0) + (10 if first and last > first else 0)
            book = row.get("blDs") or []
            top = book[0] if book and isinstance(book[0], dict) else {}
            bid_volume = num(top, "qmd")
            ask_volume = num(top, "qmo")
            upper_limit = num(row, "pMax")
            lower_limit = num(row, "pMin")
            buy_locked = bool(trades > 50 and base_volume > 0 and volume >= base_volume and power >= 1 and
                               bid_volume > 0 and ask_volume == 0 and upper_limit and abs(last - upper_limit) < 0.001)
            sell_locked = bool(trades > 50 and seller_power > 1 and ask_volume > 0 and bid_volume == 0 and
                                lower_limit and abs(last - lower_limit) < 0.001)
            name = str(row.get("lva") or row.get("lVal18AFC") or code)
            full_name = str(row.get("lvc") or row.get("lVal30") or "")
            market_eligible = yval in {300, 303, 305}
            flow_toman = None
            if market_eligible and volume > 0 and last > 0 and code in client_map:
                flow_toman = (buy_volume - sell_volume) * last / 10
                net_flow_rial += flow_toman * 10
                flow_count += 1
            nav = nav_map.get(code, {}) if etf else {}
            nav_date = str(nav.get("date")) if isinstance(nav, dict) and nav.get("date") else None
            nav_redemption = num(nav, "redemption") if isinstance(nav, dict) else 0
            nav_redemption = nav_redemption if nav_date == date and nav_redemption else None
            nav_subscription = num(nav, "subscription") if isinstance(nav, dict) else 0
            nav_subscription = nav_subscription if nav_date == date and nav_subscription else None
            records.append({"code": code, "symbol": name, "full_name": full_name, "market_eligible": market_eligible, "yval": yval,
                            "last_pct": last_pct, "close_pct": close_pct, "last": last, "close": close, "first": first,
                            "volume": volume, "base": base_volume, "trades": trades, "buy_volume": buy_volume,
                            "sell_volume": sell_volume, "power": power, "seller_power": seller_power,
                            "buy_pct": real_buy_pct, "score": score, "etf": etf, "flow_toman": flow_toman,
                            "etf_nav_redemption": nav_redemption, "etf_nav_subscription": nav_subscription,
                            "etf_nav_pct": (last / nav_redemption - 1) * 100 if nav_redemption else None,
                            "bid_volume": bid_volume, "ask_volume": ask_volume,
                            "buy_locked": buy_locked, "sell_locked": sell_locked})
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    nav_candidates = [record for record in records if record["etf"] and record["score"] >= 60 and
                      record["trades"] > 50 and record["volume"] > 0 and record["power"] >= 1.5 and
                      record["buy_pct"] >= 65 and record["last"] >= record["close"] and
                      record["first"] > 0 and record["last"] > record["first"] and
                      record["buy_volume"] > record["sell_volume"] and record["last_pct"] >= record["close_pct"]]
    nav_candidates.sort(key=lambda record: (record["score"], record["power"], record["trades"]), reverse=True)
    nav_candidates = [record["code"] for record in nav_candidates[:50]]
    error_text = "؛ ".join(f"{key}: {value}" for key, value in errors.items()) if errors else None
    if nav_candidates and not all(code in nav_map for code in nav_candidates):
        error_text = (error_text + "؛ " if error_text else "") + "در انتظار دریافت NAV صندوق‌ها از مرورگر."
    return {"symbols": pd.DataFrame(records), "index": index_value, "index_pct": index_pct, "date": date,
            "net_flow_toman": net_flow_rial / 10, "flow_count": flow_count,
            "nav_candidates": nav_candidates, "error": error_text,
            "browser_fetched_at": browser_data.get("fetched_at")}


# ------------------------- Daily flow persistence -------------------------
def save_flow_snapshot(trade_date: str, net_flow_toman: float, symbol_count: int):
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as db:
            db.execute("CREATE TABLE IF NOT EXISTS daily_flow (trade_date TEXT PRIMARY KEY, net_flow_toman REAL NOT NULL, symbol_count INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("INSERT INTO daily_flow(trade_date,net_flow_toman,symbol_count,updated_at) VALUES(?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET net_flow_toman=excluded.net_flow_toman,symbol_count=excluded.symbol_count,updated_at=excluded.updated_at",
                       (trade_date, net_flow_toman, symbol_count, datetime.now().isoformat(timespec="seconds")))
            previous = db.execute("SELECT trade_date,net_flow_toman,symbol_count FROM daily_flow WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", (trade_date,)).fetchone()
        return previous
    except sqlite3.Error:
        return None


def render_stock_table(frame: pd.DataFrame, is_etf: bool = False):
    if frame.empty:
        st.info("با فیلتر فعلی نماد واجد شرایطی پیدا نشد.")
        return
    table = frame.copy()
    table["سیگنال"] = "خرید · عبور از فیلتر"
    table["قدرت خریدار حقیقی"] = table["power"].map(lambda value: f"{value:.2f}")
    table["نسبت حجم به مبنا"] = table.apply(lambda row: f"{row['volume'] / row['base']:.2f}" if row["base"] else "—", axis=1)
    table["آخرین قیمت (٪)"] = table["last_pct"].map(lambda value: f"{value:+.2f}%")
    table["قیمت پایانی (٪)"] = table["close_pct"].map(lambda value: f"{value:+.2f}%")
    table = table.rename(columns={"symbol": "نام نماد", "score": "امتیاز کل"})
    if is_etf:
        table["NAV ابطال"] = table["etf_nav_redemption"].map(fmt)
        table["اختلاف با NAV ابطال"] = table["etf_nav_pct"].map(lambda value: f"{value:+.2f}%")
        table["تعداد معاملات"] = table["trades"].map(lambda value: f"{value:,.0f}")
        columns = ["نام نماد", "سیگنال", "آخرین قیمت (٪)", "قیمت پایانی (٪)", "قدرت خریدار حقیقی", "تعداد معاملات", "NAV ابطال", "اختلاف با NAV ابطال", "امتیاز کل"]
    else:
        columns = ["نام نماد", "سیگنال", "آخرین قیمت (٪)", "قیمت پایانی (٪)", "قدرت خریدار حقیقی", "نسبت حجم به مبنا", "امتیاز کل"]
    st.dataframe(table[columns].reset_index(drop=True), width="stretch", hide_index=True)


# ------------------------------ UI sections -------------------------------
st.title("📊 داشبورد بازارهای مالی ایران")
st.caption("فیلترها و قیمت‌های زنده از منابع عمومی بازار · بروزرسانی ۶۰ ثانیه‌ای · دادهٔ بورس از مرورگر بازدیدکننده دریافت می‌شود")
st.caption("دلتاهای قیمت از کندل روزانهٔ بسته‌شدهٔ قبل محاسبه می‌شوند؛ دلتاهای شمارش فیلترها و جریان حقیقی به سابقهٔ نشست در روز قبل نیاز دارند.")
if st.button("🔄 بروزرسانی لحظه‌ای داده‌ها", type="primary"):
    get_usdt.clear()
    get_rahavard_gold.clear()
    get_ounce_fallback.clear()
    get_tgju.clear()
    st.session_state["_tse_manual_refresh"] = st.session_state.get("_tse_manual_refresh", 0) + 1
    st.rerun()

tab_tse, tab_gold = st.tabs(["🏛️ بورس تهران (TSETMC)", "🥇 بازار طلا، سکه و ارز"])

with tab_gold:
    st.subheader("انس ره‌آورد، ارزش جهانی طلای ۱۸ عیار و حباب داخلی")
    fx = get_usdt()
    rate_toman = fx["toman"]
    rah = get_rahavard_gold()
    fallback_ounce, fallback_source, fallback_error = get_ounce_fallback() if rah["ounce"] is None else (None, None, None)
    ounce_usd = rah["ounce"] if rah["ounce"] is not None else fallback_ounce
    ounce_source = "ره‌آورد · بازار طلا" if rah["ounce"] is not None else fallback_source
    local_rial = rah["local18_rial"]
    local_source = "ره‌آورد" if local_rial is not None else "TGJU"
    local_error = None
    if local_rial is None:
        local_rial, local_error = get_tgju("geram18")
    local_toman = rial_to_toman(local_rial)
    intrinsic_toman = (ounce_usd * rate_toman / TROY_OUNCE_GRAMS * 0.750) if ounce_usd and rate_toman else None
    previous_ounce = get_previous_ounce_daily_close()
    previous_gold = get_tgju_previous_daily_close("geram18")
    previous_usd = get_tgju_previous_daily_close("price_dollar_rl")
    previous_gold_toman = rial_to_toman(previous_gold["close"])
    previous_usd_toman = rial_to_toman(previous_usd["close"])
    previous_intrinsic = (
        previous_ounce["close"] * previous_usd_toman / TROY_OUNCE_GRAMS * 0.750
        if previous_ounce["close"] and previous_usd_toman else None
    )
    a, b, c = st.columns(3)
    a.metric("انس جهانی از ره‌آورد (دلار/اونس)", fmt(ounce_usd, 2), change_from_close(ounce_usd, previous_ounce["close"]), delta_description=f"نسبت به کندل بسته‌شدهٔ {previous_ounce['date'] or 'ناموجود'} · Yahoo GC=F")
    b.metric("طلای ۱۸ عیار داخل (تومان/گرم)", fmt(local_toman), change_from_close(local_toman, previous_gold_toman), delta_description=f"نسبت به کندل بسته‌شدهٔ {previous_gold['date'] or 'ناموجود'} · TGJU")
    c.metric("نرخ دلار/تتر (تومان)", fmt(rate_toman), change_from_close(rate_toman, previous_usd_toman), delta_description=f"مبنای دلتا: کندل دلار آزاد TGJU در {previous_usd['date'] or 'ناموجود'}؛ قیمت فعلی از تبدیل است")
    st.caption(f"منبع انس: {ounce_source or 'ناموجود'} · منبع قیمت داخلی: {local_source} · منبع دلار/تتر: {fx['source']} · زمان پاسخ ره‌آورد: {rah['ounce_time'] or '—'}")
    st.caption("درصدهای کنار کارت‌های قیمت از آخرین کندل روزانهٔ بسته‌شده محاسبه می‌شوند. نرخ روز قبل دلار از کندل دلار آزاد TGJU است؛ چون API عمومی تبدیل در مستنداتش کندل روزانهٔ USDT/IRT ارائه نمی‌کند.")
    st.info("نرخ دلار/تتر در همین بخش برای تبدیل ارزش جهانی طلا استفاده می‌شود. API عمومی تبدیل آمار تجمیعی سقف، کف و حجم ۲۴ساعته را ارائه نمی‌کند.")
    if fx["error"]:
        st.warning(fx["error"])
    if intrinsic_toman is not None:
        st.metric("ارزش محاسباتی طلای ۱۸ عیار جهانی (تومان/گرم)", fmt(intrinsic_toman), change_from_close(intrinsic_toman, previous_intrinsic), delta_description=f"تغییر بر پایهٔ انس {previous_ounce['date'] or 'ناموجود'} و دلار {previous_usd['date'] or 'ناموجود'}")
        if local_toman is not None:
            bubble_toman = local_toman - intrinsic_toman
            bubble_pct = bubble_toman / intrinsic_toman * 100 if intrinsic_toman else 0
            previous_bubble_pct = (
                (previous_gold_toman - previous_intrinsic) / previous_intrinsic * 100
                if previous_gold_toman is not None and previous_intrinsic else None
            )
            bubble_delta = f"{bubble_pct - previous_bubble_pct:+.2f} واحد درصد" if previous_bubble_pct is not None else None
            st.metric("حباب/اختلاف بازار ایران با ارزش جهانی", f"{bubble_toman:+,.0f} تومان", bubble_delta, delta_description=f"تغییر حباب نسبت به کندل‌های قبلی: طلا {previous_gold['date'] or 'ناموجود'} · انس {previous_ounce['date'] or 'ناموجود'}")
            if bubble_pct > 0:
                st.warning(f"قیمت داخلی حدود {bubble_pct:.2f}٪ بالاتر از ارزش محاسباتی جهانی است.")
            elif bubble_pct < 0:
                st.success(f"قیمت داخلی حدود {abs(bubble_pct):.2f}٪ پایین‌تر از ارزش محاسباتی جهانی است.")
            else:
                st.info("قیمت داخلی با ارزش محاسباتی برابر است.")
    else:
        st.info("برای محاسبهٔ ارزش جهانی، قیمت انس و تتر باید هر دو در دسترس باشند.")
    if rah["error"]:
        st.caption("خطای ره‌آورد: " + rah["error"])
    if fallback_error:
        st.caption("خطای منبع جایگزین انس: " + fallback_error)
    if local_error:
        st.caption("خطای دریافت قیمت داخلی: " + local_error)


@st.fragment(run_every="60s")
def render_tse_tab():
    st.subheader("فیلتر نمادها و برآورد جهت بازار فردا")
    refresh_key = f"{int(datetime.now().timestamp() // 60)}:{st.session_state.get('_tse_manual_refresh', 0)}"
    nav_codes = st.session_state.get("_tse_nav_candidates", [])
    browser_data = browser_tse_feed(refresh_key=refresh_key, nav_codes=nav_codes, default=None,
                                    key="tsetmc-browser-feed")
    if isinstance(browser_data, dict) and browser_data.get("refresh_key") == refresh_key:
        market = get_tse_data(browser_data)
        discovered = market.get("nav_candidates", [])
        if discovered != nav_codes:
            st.session_state["_tse_nav_candidates"] = discovered
            st.rerun()
    else:
        market = get_tse_data(None)
    symbols = market["symbols"]
    k1, k2, k3 = st.columns(3)
    k1.metric("شاخص کل", fmt(market["index"]), f"{market['index_pct']:+.2f}%" if market["index_pct"] is not None else None, delta_description="تغییر از قیمت پایانی کندل قبلی TSETMC")
    k2.metric("تغییر روزانه شاخص", f"{market['index_pct']:+.2f}%" if market["index_pct"] is not None else "—")
    equity_market = symbols[symbols["market_eligible"]] if not symbols.empty else symbols
    active_count = int((equity_market["volume"] > 0).sum()) if not equity_market.empty else None
    k3.metric("نمادهای سهام و ETF دارای معامله", fmt(active_count), daily_delta("tse.active_count", active_count, percentage=False, unit="نماد"), delta_description="تغییر تعداد نسبت به روز قبل")
    if market["error"]:
        st.warning("بخشی از داده دریافت نشد: " + market["error"])
    if symbols.empty:
        st.info("در انتظار دریافت دیده‌بان، شاخص و اطلاعات حقیقی TSETMC از مرورگر بازدیدکننده هستیم. مرورگر باید بتواند cdn.tsetmc.com را باز کند.")
        return
    if market.get("browser_fetched_at"):
        st.caption(f"دادهٔ بورس مستقیماً از مرورگر دریافت شد · زمان دریافت: {market['browser_fetched_at']}")

    pressure_buy = equity_market[(equity_market["trades"] > 50) & (equity_market["base"] > 0) & (equity_market["volume"] >= equity_market["base"]) &
                                 (((equity_market["last_pct"] - equity_market["close_pct"]) >= 1.5) | equity_market["buy_locked"]) & (equity_market["power"] >= 1)]
    pressure_sell = equity_market[(equity_market["trades"] > 50) &
                                  (((equity_market["close_pct"] - equity_market["last_pct"]) >= 1.5) | equity_market["sell_locked"]) &
                                  (equity_market["seller_power"] > 1)]

    st.markdown("#### پیش‌بینی فردا با فیلتر فشار تقاضا و عرضه")
    demand_col, supply_col = st.columns(2)
    demand_col.metric("تعداد نمادهای فشار تقاضا", f"{len(pressure_buy):,}", daily_delta("tse.demand_count", len(pressure_buy), percentage=False, unit="نماد"), delta_description="تغییر تعداد نسبت به روز قبل")
    supply_col.metric("تعداد نمادهای فشار عرضه", f"{len(pressure_sell):,}", daily_delta("tse.supply_count", len(pressure_sell), percentage=False, unit="نماد"), delta_description="تغییر تعداد نسبت به روز قبل")
    if len(pressure_buy) > len(pressure_sell):
        st.success(f"سیگنال فیلتر: صعودی · تقاضا {len(pressure_buy)} نماد در برابر عرضه {len(pressure_sell)} نماد")
    elif len(pressure_sell) > len(pressure_buy):
        st.error(f"سیگنال فیلتر: نزولی · عرضه {len(pressure_sell)} نماد در برابر تقاضا {len(pressure_buy)} نماد")
    else:
        st.warning(f"سیگنال فیلتر: متعادل · تقاضا {len(pressure_buy)} نماد و عرضه {len(pressure_sell)} نماد")
    st.caption("فیلترها از تعداد معامله، حجم/حجم مبنا، فاصلهٔ بازده آخرین و پایانی، قدرت حقیقی و صف قفل‌شده با حد مجاز واقعی استفاده می‌کنند. پیش‌بینی آماری است، قطعی نیست؛ API تجمیعی تاریخچهٔ دقیق ۳۰ دقیقه پایانی را تضمین نمی‌کند.")

    st.markdown("#### برآورد خروج/ورود خالص پول حقیقی")
    net = market["net_flow_toman"]
    if net is not None:
        previous = save_flow_snapshot(market["date"], net, market["flow_count"])
        flow_symbols = equity_market[equity_market["flow_toman"].notna()].copy()
        inflow_symbols = flow_symbols[flow_symbols["flow_toman"] > 0]
        outflow_symbols = flow_symbols[flow_symbols["flow_toman"] < 0]
        buy_share = len(inflow_symbols) / len(flow_symbols) * 100 if len(flow_symbols) else 0
        f1, f2, f3, f4 = st.columns(4)
        net_delta = None
        if previous:
            old_date, old_net, old_count = previous
            if old_net:
                change_pct = (net - old_net) / abs(old_net) * 100
                net_delta = f"{change_pct:+.2f}%"
        f1.metric("خالص ورود/خروج پول حقیقی امروز (میلیارد تومان)", f"{net / 1_000_000_000:+,.2f}", net_delta,
                  delta_description=f"درصد تغییر جهت‌دار نسبت به جلسهٔ {old_date}" if previous and old_net else "برای محاسبهٔ درصد، خالص روز قبل باید غیرصفر باشد")
        f2.metric("نمادهای با ورود خالص حقیقی", f"{len(inflow_symbols):,}", daily_delta("tse.inflow_count", len(inflow_symbols), percentage=False, unit="نماد"), delta_description="تغییر تعداد نسبت به روز قبل")
        f3.metric("نمادهای با خروج خالص حقیقی", f"{len(outflow_symbols):,}", daily_delta("tse.outflow_count", len(outflow_symbols), percentage=False, unit="نماد"), delta_description="تغییر تعداد نسبت به روز قبل")
        f4.metric("سهم نمادهای وروددار", f"{buy_share:.1f}%", daily_delta("tse.inflow_share", buy_share, percentage=False, unit="واحد درصد"), delta_description="تغییر نسبت به روز قبل")

        movers = st.columns(2)
        for col, frame, title, ascending in (
            (movers[0], inflow_symbols, "بیشترین ورود خالص حقیقی", False),
            (movers[1], outflow_symbols, "بیشترین خروج خالص حقیقی", True),
        ):
            col.markdown(f"##### {title}")
            if frame.empty:
                col.caption("نمادی ثبت نشده است.")
            else:
                top = frame.assign(flow_display=frame["flow_toman"].map(lambda value: f"{value / 1_000_000_000:+,.2f}"))
                top = top.sort_values("flow_toman", ascending=ascending).head(5)
                col.dataframe(top[["symbol", "flow_display"]].rename(columns={"symbol": "نماد", "flow_display": "جریان خالص حقیقی (میلیارد تومان)"}).reset_index(drop=True),
                              width="stretch", hide_index=True)
        if previous:
            st.caption(f"مقایسهٔ تغییر جریان خالص با جلسهٔ ثبت‌شدهٔ قبل: {old_date}؛ در مبنا {old_count:,} نماد دارای داده بودند.")
        st.caption(f"روش برآورد: (حجم خرید حقیقی − حجم فروش حقیقی) × آخرین قیمت، تبدیل ریال به تومان · {market['flow_count']:,} نماد با دادهٔ حقیقی. برآورد، خالص انتقال بین حقیقی و حقوقی است و به‌معنای برداشت وجه از کل بازار نیست.")
    else:
        st.info("آمار حقیقی برای محاسبهٔ جریان پول در دسترس نیست.")

    st.markdown("#### بهترین نمادها از سمت خریداران")
    # A buy signal is issued only when every base scoring condition is met.
    # The power >= 2.5 tier remains an extra 15-point bonus, not a required base condition.
    buy_side = equity_market[
        (equity_market["score"] >= 60) &
        (equity_market["power"] >= 1.5) &
        (equity_market["buy_pct"] >= 65) &
        (equity_market["base"] > 0) & (equity_market["volume"] >= equity_market["base"]) &
        (equity_market["last"] >= equity_market["close"]) &
        (equity_market["first"] > 0) & (equity_market["last"] > equity_market["first"]) &
        (equity_market["buy_volume"] > equity_market["sell_volume"]) &
        (equity_market["last_pct"] >= equity_market["close_pct"])
    ].copy()
    buy_side = buy_side.sort_values(["score", "power", "last_pct"], ascending=False)
    st.markdown("##### بهترین سهام بازار")
    stock_buy_side = buy_side[~buy_side["etf"]].head(50)
    st.caption(f"شرط سهام: قدرت خریدار ≥ ۱٫۵، سهم خرید حقیقی ≥ ۶۵٪، حجم ≥ حجم مبنا، آخرین ≥ پایانی، کندل سبز، خالص خرید حقیقی مثبت و امتیاز ≥ ۶۰ · {len(stock_buy_side)} نماد")
    render_stock_table(stock_buy_side)
    st.markdown("##### بهترین صندوق‌های ETF")
    etf_buy_side = equity_market[
        equity_market["etf"] & (equity_market["score"] >= 60) &
        (equity_market["power"] >= 1.5) & (equity_market["buy_pct"] >= 65) &
        (equity_market["trades"] > 50) & (equity_market["volume"] > 0) &
        (equity_market["last"] >= equity_market["close"]) &
        (equity_market["first"] > 0) & (equity_market["last"] > equity_market["first"]) &
        (equity_market["buy_volume"] > equity_market["sell_volume"]) &
        (equity_market["last_pct"] >= equity_market["close_pct"]) &
        equity_market["etf_nav_redemption"].notna() & (equity_market["etf_nav_pct"] <= 1.0)
    ].copy()
    etf_buy_side = etf_buy_side.sort_values(["score", "etf_nav_pct", "trades"], ascending=[False, True, False]).head(50)
    st.caption(f"شرط ETF: همان معیارهای قدرت خریدار، خرید حقیقی و کندل مثبت؛ به‌جای حجم مبنا حداقل ۵۰ معامله، NAV ابطال به‌روز در {market['date']} و قیمت حداکثر ۱٪ بالاتر از NAV · {len(etf_buy_side)} صندوق واجد سیگنال")
    render_stock_table(etf_buy_side, is_etf=True)
    st.caption("امتیاز از ۱۰۰: قدرت حقیقی (۲۵ امتیاز از ۱٫۵ و ۱۵ امتیاز اضافه از ۲٫۵)، خرید حقیقی (۱۵)، نقدشوندگی (۲۰)، آخرین ≥ پایانی (۱۵)، کندل سبز (۱۰). امتیاز نقدشوندگی سهام با حجم مبنا و ETF با تعداد معامله سنجیده می‌شود. سیگنال نتیجهٔ فیلتر کمی است و بازده را تضمین نمی‌کند. دادهٔ فید: " + str(market["date"]) + ".")


with tab_tse:
    render_tse_tab()

st.divider()
st.caption("قیمت‌ها و فیلترها بر پایهٔ آخرین پاسخ منابع عمومی‌اند؛ محدودیت شبکه، روز تعطیل و تأخیر منبع ممکن است روی نتیجه اثر بگذارد.")
