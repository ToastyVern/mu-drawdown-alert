#!/usr/bin/env python3
"""
MU drawdown alert.

Checks the live Micron price against per-account reference levels and alerts
when the price sits at or below `drop_pct` under a level.

Price sources, tried in order:
  1. TradingView scanner  (same data the TradingView site shows)
  2. Yahoo Finance chart API
  3. Stooq CSV

Alerting:
  - email over SMTP
  - optional phone push via ntfy.sh

State is kept in state.json so a breached level alerts once, not every 20
minutes. A level re-arms when the price recovers `rearm_buffer_pct` above its
trigger.

Env:
  SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS MAIL_FROM MAIL_TO
  NTFY_TOPIC          optional, e.g. "werner-mu-alerts"
  TEST_PRICE          optional, force a price for a dry run
  FORCE_RUN=1         optional, skip the market-hours guard
"""

import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "levels.json"
STATE_PATH = ROOT / "state.json"

NY = ZoneInfo("America/New_York")
JHB = ZoneInfo("Africa/Johannesburg")
UA = "Mozilla/5.0 (compatible; mu-drawdown-alert/1.0)"


# ----------------------------------------------------------------- utilities


def log(msg: str) -> None:
    print(f"[{datetime.now(JHB):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# -------------------------------------------------------------- price feeds


def price_tradingview(symbol: str):
    fields = "close,change,change_abs,volume,update_mode,description"
    url = (
        "https://scanner.tradingview.com/symbol?symbol="
        f"{urllib.parse.quote(symbol)}&fields={fields}&no_404=true"
    )
    data = json.loads(_get(url))
    px = float(data["close"])
    mode = data.get("update_mode", "")
    delay = "real-time" if "realtime" in mode else "15-min delayed"
    return px, f"TradingView ({delay})", float(data.get("change") or 0.0)


def price_yahoo(ticker: str):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?range=1d&interval=1m"
    )
    res = json.loads(_get(url))["chart"]["result"][0]
    meta = res["meta"]
    px = float(meta["regularMarketPrice"])
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or px)
    chg = (px / prev - 1) * 100 if prev else 0.0
    return px, "Yahoo Finance", chg


def price_stooq(ticker: str):
    url = f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlc&h&e=csv"
    rows = _get(url).decode().strip().splitlines()
    cells = rows[1].split(",")
    px = float(cells[6])
    open_ = float(cells[3])
    chg = (px / open_ - 1) * 100 if open_ else 0.0
    return px, "Stooq", chg


def get_price(cfg):
    if os.environ.get("TEST_PRICE"):
        p = float(os.environ["TEST_PRICE"])
        log(f"TEST_PRICE set: using {p}")
        return p, "TEST_PRICE (dry run)", 0.0

    attempts = [
        (price_tradingview, cfg["tv_symbol"]),
        (price_yahoo, cfg["ticker"]),
        (price_stooq, cfg["ticker"]),
    ]
    errors = []
    for fn, arg in attempts:
        try:
            px, src, chg = fn(arg)
            if px and px > 0:
                log(f"price {px:.2f} from {src} ({chg:+.2f}% on day)")
                return px, src, chg
            errors.append(f"{fn.__name__}: returned {px!r}")
        except Exception as exc:  # noqa: BLE001 - any source may fail
            errors.append(f"{fn.__name__}: {exc}")
    raise RuntimeError("all price sources failed -> " + " | ".join(errors))


# ------------------------------------------------------------ market window


def market_is_open(now_ny: datetime) -> bool:
    if now_ny.weekday() > 4:
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


# ----------------------------------------------------------------- alerting


def send_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if not host or not to:
        log("email skipped: SMTP_HOST or MAIL_TO not set")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = to
    msg.set_content(body)

    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
            if user and pwd:
                server.login(user, pwd)
            server.send_message(msg)
        log(f"email sent to {to}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"EMAIL FAILED: {exc}")
        return False


def send_push(title: str, body: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode(),
            headers={
                "Title": title,
                "Priority": "urgent",
                "Tags": "chart_with_downwards_trend",
                "User-Agent": UA,
            },
        )
        urllib.request.urlopen(req, timeout=20).read()
        log(f"push sent to ntfy topic {topic}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"PUSH FAILED: {exc}")
        return False


# --------------------------------------------------------------------- main


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    drop = cfg["drop_pct"] / 100.0
    rearm = cfg["rearm_buffer_pct"] / 100.0

    now_ny = datetime.now(NY)
    forced = os.environ.get("FORCE_RUN") == "1" or bool(os.environ.get("TEST_PRICE"))
    if not forced and not market_is_open(now_ny):
        log(f"market closed ({now_ny:%a %H:%M %Z}), nothing to do")
        return 0

    price, source, day_chg = get_price(cfg)

    state = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    breached = state.get("breached", {})

    fired, still_breached, recovered, standing = [], [], [], []

    for acct in cfg["accounts"]:
        name = acct["name"]
        ref = float(acct["reference_level"])
        trigger = ref * (1 - drop)
        rearm_at = trigger * (1 + rearm)
        was = bool(breached.get(name))

        if price <= trigger:
            if was:
                still_breached.append(name)
            else:
                fired.append((name, ref, trigger, price))
                breached[name] = {
                    "at": now_ny.isoformat(timespec="seconds"),
                    "price": round(price, 4),
                }
        else:
            if was and price >= rearm_at:
                recovered.append(name)
                breached.pop(name, None)
            standing.append((name, ref, trigger, price / trigger - 1))

    for name, ref, trig, gap in standing:
        log(f"  {name:<12} ref {ref:>9.2f}  trigger {trig:>9.2f}  {gap:+.2%} above")
    for name in still_breached:
        log(f"  {name:<12} still breached, already alerted")
    for name in recovered:
        log(f"  {name:<12} recovered above re-arm level, alert re-armed")

    if fired:
        lines = [
            f"Micron ({cfg['ticker']}) is {price:,.2f}  ({day_chg:+.2f}% on the day)",
            f"Source: {source}",
            f"Time:   {now_ny:%Y-%m-%d %H:%M %Z}  /  "
            f"{now_ny.astimezone(JHB):%H:%M} SAST",
            "",
            f"{cfg['drop_pct']:.0f}% drawdown triggers hit:",
        ]
        for name, ref, trig, px in fired:
            lines.append(
                f"  {name:<12} level {ref:,.2f} -> trigger {trig:,.2f} "
                f"| now {px:,.2f} ({px / ref - 1:+.2%} vs level)"
            )
        if still_breached:
            lines += ["", "Already below (alerted earlier): " + ", ".join(still_breached)]
        lines += [
            "",
            "Each level alerts once. It re-arms if MU recovers "
            f"{cfg['rearm_buffer_pct']:.0f}% above its trigger.",
        ]
        body = "\n".join(lines)
        names = ", ".join(n for n, *_ in fired)
        subject = f"MU ALERT: {price:,.2f} — {cfg['drop_pct']:.0f}% trigger hit ({names})"

        print("\n" + body + "\n")
        ok_mail = send_email(subject, body)
        ok_push = send_push(f"MU {price:,.2f} — trigger hit", body)

        if not (ok_mail or ok_push):
            log("NO ALERT CHANNEL SUCCEEDED — not saving state, will retry next run")
            return 1

    state["breached"] = breached
    state["last_check"] = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price": round(price, 4),
        "source": source,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
