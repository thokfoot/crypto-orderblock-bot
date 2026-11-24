from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
SECRET_KEYS = (
    "FYERS_CLIENT_ID",
    "FYERS_ACCESS_TOKEN",
    "FYERS_SECRET_KEY",
    "FYERS_PIN",
    "FYERS_TOTP_KEY",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "AUTOMATION_TELEGRAM_BOT_TOKEN",
    "AUTOMATION_TELEGRAM_CHAT_ID",
    "USA_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def env_first(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def redact(value):
    text = str(value)
    for key in SECRET_KEYS:
        secret = os.getenv(key, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:800]


def request_json(url, headers=None, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"message": raw}
        return {"ok": False, "http_error": exc.code, "response": parsed}
    except Exception as exc:
        return {"ok": False, "message": redact(exc)}


def check_fyers():
    client_id = env_first("FYERS_CLIENT_ID")
    token = env_first("FYERS_ACCESS_TOKEN")
    if not client_id or not token:
        return {"ok": False, "status": "missing_credentials"}
    symbols = urllib.parse.urlencode({"symbols": "NSE:NIFTY-INDEX"})
    url = "https://api-t1.fyers.in/data/quotes?" + symbols
    headers = {
        "Authorization": client_id + ":" + token,
        "Content-Type": "application/json",
        "version": "3",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    response = request_json(url, headers)
    nested = response.get("response") if isinstance(response.get("response"), dict) else {}
    code = response.get("code", nested.get("code"))
    ok = response.get("s") == "ok" or response.get("status") == "success" or code == 200
    payload = {
        "ok": bool(ok),
        "code": code,
        "status": response.get("s") or response.get("status") or nested.get("s") or "",
        "message": redact(response.get("message") or nested.get("message") or ""),
    }
    if ok:
        data = response.get("data") or {}
        item = data.get("NSE:NIFTY-INDEX") if isinstance(data, dict) else None
        if not isinstance(item, dict) and isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict) and ("lp" in value or "ltp" in value):
                    item = value
                    break
        if isinstance(item, dict):
            payload["ltp"] = item.get("lp", item.get("ltp"))
    return payload


def check_alpaca():
    key = env_first("APCA_API_KEY_ID")
    secret = env_first("APCA_API_SECRET_KEY")
    if not key or not secret:
        return {"ok": False, "status": "missing_credentials"}
    url = "https://data.alpaca.markets/v2/stocks/QQQ/quotes/latest"
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    response = request_json(url, headers)
    nested = response.get("response") if isinstance(response.get("response"), dict) else {}
    quote = response.get("quote") if isinstance(response.get("quote"), dict) else {}
    bid = quote.get("bp")
    ask = quote.get("ap")
    ok = bool(bid or ask)
    payload = {
        "ok": bool(ok),
        "code": response.get("http_error", 200 if ok else None),
        "status": "ok" if ok else "error",
        "symbol": "QQQ",
        "bid": bid,
        "ask": ask,
        "timestamp": quote.get("t"),
        "message": redact(response.get("message") or nested.get("message") or ""),
    }
    return payload


def send_telegram(text):
    if env_first("DRY_RUN_DATA_CHECK_NO_TELEGRAM") == "1":
        return {"ok": True, "status": "disabled"}
    token = env_first(
        "AUTOMATION_TELEGRAM_BOT_TOKEN",
        "USA_TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    )
    chat_id = env_first("AUTOMATION_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"ok": False, "status": "missing_credentials"}
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    response = request_json(url, headers, body)
    ok = response.get("ok") is True
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return {
        "ok": ok,
        "status": "sent" if ok else "failed",
        "message_id": result.get("message_id") if ok else None,
        "error": redact(response.get("message") or "") if not ok else "",
    }


def main():
    fyers = check_fyers()
    alpaca = check_alpaca()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    stamp = now.strftime("%Y-%m-%d %H:%M:%S IST")
    fyers_status = "OK" if fyers.get("ok") else "FAIL"
    alpaca_status = "OK" if alpaca.get("ok") else "FAIL"
    fyers_detail = redact(fyers.get("code") or fyers.get("message") or fyers.get("status") or "unknown")
    alpaca_detail = redact(alpaca.get("code") or alpaca.get("message") or alpaca.get("status") or "unknown")
    text = (
        "[INFO] DRY-RUN DATA CHECK "
        + stamp
        + " Fyers="
        + fyers_status
        + "("
        + fyers_detail
        + ") Alpaca="
        + alpaca_status
        + "("
        + alpaca_detail
        + ")"
    )
    telegram = send_telegram(text)
    result = {
        "timestamp": stamp,
        "fyers": fyers,
        "alpaca": alpaca,
        "telegram": telegram,
        "message": text,
        "note": "This is an independent Fyers/Alpaca connectivity check. The current v3.2.5 NSE/US signal runners use Yahoo Finance 1-minute bars and do not place broker orders.",
    }
    print(json.dumps(result, indent=2))
    # A successful connectivity check must include the alert channel as well.
    return 0 if fyers.get("ok") and alpaca.get("ok") and telegram.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
