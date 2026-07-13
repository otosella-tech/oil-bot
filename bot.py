# -*- coding: utf-8 -*-
"""
בוט עדכוני נפט — רץ כל 30 דקות דרך GitHub Actions.
שולח לערוץ טלגרם: חדשות קריטיות, תנועות מחיר חדות,
התראות לפני/אחרי פרסומי נתונים, וסיכום יומי.
"""

import json
import os
import re
import sys
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ============ הגדרות ============

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "@nefet_push")

TZ_IL = ZoneInfo("Asia/Jerusalem")
TZ_ET = ZoneInfo("America/New_York")

DIGEST_HOUR_IL = 8          # שעת הסיכום היומי (שעון ישראל)
PRICE_ALERT_PCT = 2.0       # אחוז תנועה שמצדיק התראה
NEWS_SCORE_THRESHOLD = 4    # ציון מינימלי לחדשות קריטיות
MAX_SEEN = 600              # כמה כתבות לזכור למניעת כפילויות

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

RSS_FEEDS = [
    "https://oilprice.com/rss/main",
    "https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+%22oil+prices%22&hl=en-US&gl=US&ceid=US:en",
]

# מילות מפתח וניקוד — כותרת שצוברת מספיק נקודות נחשבת קריטית
KEYWORDS = {
    # גיאופוליטיקה חמה
    "hormuz": 5, "strait": 3, "tanker": 4, "attack": 4, "strike": 3,
    "war": 3, "missile": 4, "drone": 3, "explosion": 4, "seize": 4,
    "sanction": 4, "embargo": 5, "ceasefire": 4,
    # אופ"ק והחלטות היצע
    "opec": 4, "opec+": 5, "output cut": 5, "production cut": 5,
    "quota": 4, "supply cut": 5, "output hike": 4, "production increase": 4,
    # מדינות מפתח
    "iran": 3, "russia": 2, "saudi": 3, "libya": 3, "venezuela": 3, "iraq": 3,
    # תשתיות ומזג אוויר
    "pipeline": 3, "hurricane": 4, "refinery": 2, "outage": 3, "spr": 4,
    "strategic petroleum": 4, "force majeure": 5,
    # שוק
    "surge": 3, "plunge": 3, "soar": 3, "crash": 3, "spike": 3,
}

# לוח פרסומי נתונים קבועים (שעון ניו יורק): (יום בשבוע 0=שני, שעה, דקה, שם)
DATA_RELEASES = [
    (1, 16, 30, "דוח מלאים של מכון הנפט האמריקאי (API)"),
    (2, 10, 30, "דוח מלאים רשמי של ממשל האנרגיה (EIA) — הפרסום הכי מזיז בשבוע"),
    (4, 13, 0,  "ספירת אסדות קידוח (Baker Hughes)"),
    (4, 15, 30, "דוח פוזיציות ספקולנטים (CFTC COT)"),
]

RUN_INTERVAL_MIN = 35  # חלון זיהוי אירועים (קצת יותר מ-30 בגלל עיכובי תזמון)

# ============ עזרים ============


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (oil-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send(text):
    """שליחת הודעה לערוץ."""
    if not BOT_TOKEN:
        print("[DRY-RUN] היה נשלח:\n" + text + "\n" + "-" * 40)
        return True
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            ok = json.load(r).get("ok", False)
            if not ok:
                print("שליחה נכשלה:", text[:80])
            return ok
    except Exception as e:
        print("שגיאת טלגרם:", e)
        return False


# ============ מחירים ============


def get_prices():
    """מחירי נפט מ-stooq (חינמי, בלי מפתח). מחזיר {'WTI': מחיר, 'Brent': מחיר}."""
    out = {}
    for sym, name in (("cl.f", "WTI"), ("cb.f", "Brent")):
        try:
            csv = http_get(f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv")
            row = csv.strip().splitlines()[1].split(",")
            price = float(row[6])  # עמודת Close
            if price > 0:
                out[name] = price
        except Exception as e:
            print(f"שגיאת מחיר {name}:", e)
    return out


def check_price_alerts(state, prices):
    """התראה אם המחיר זז יותר מהסף מאז נקודת הבסיס האחרונה."""
    base = state.setdefault("price_base", {})
    alerts = []
    for name, price in prices.items():
        prev = base.get(name)
        if prev:
            pct = (price - prev) / prev * 100
            if abs(pct) >= PRICE_ALERT_PCT:
                arrow = "📈" if pct > 0 else "📉"
                alerts.append(
                    f"{arrow} <b>תנועה חדה בנפט {name}</b>\n"
                    f"{prev:.2f} ➜ {price:.2f} דולר ({pct:+.1f}%)")
                base[name] = price
        else:
            base[name] = price
    return alerts


# ============ חדשות ============


def parse_rss(xml_text):
    """חילוץ כותרות ולינקים מ-RSS בלי ספריות חיצוניות."""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml_text, re.S):
        block = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
        if t and l:
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            items.append((title, l.group(1).strip()))
    return items


def score_title(title):
    low = title.lower()
    return sum(pts for kw, pts in KEYWORDS.items() if kw in low)


def item_id(title, link):
    return hashlib.md5((title + link).encode()).hexdigest()[:16]


def fetch_all_news():
    items = []
    for url in RSS_FEEDS:
        try:
            items.extend(parse_rss(http_get(url)))
        except Exception as e:
            print("שגיאת פיד:", url, e)
    return items


def check_critical_news(state, items):
    seen = state.setdefault("seen", [])
    alerts = []
    for title, link in items:
        iid = item_id(title, link)
        if iid in seen:
            continue
        score = score_title(title)
        if score >= NEWS_SCORE_THRESHOLD:
            alerts.append(f"🚨 <b>חדשות נפט</b>\n{title}\n{link}")
        seen.append(iid)
    state["seen"] = seen[-MAX_SEEN:]
    return alerts[:5]  # מקסימום 5 התראות בריצה כדי לא להציף


# ============ פרסומי נתונים מתוזמנים ============


def check_data_releases(state, now_et):
    """התראה לפני ואחרי כל פרסום נתונים קבוע."""
    msgs = []
    done = state.setdefault("releases_done", {})
    today_key = now_et.strftime("%Y-%m-%d")
    for wd, hh, mm, name in DATA_RELEASES:
        if now_et.weekday() != wd:
            continue
        event = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta_min = (event - now_et).total_seconds() / 60
        key_pre = f"{today_key}|{name}|pre"
        key_post = f"{today_key}|{name}|post"
        if 0 < delta_min <= RUN_INTERVAL_MIN and key_pre not in done:
            msgs.append(f"⏰ <b>בקרוב ({int(delta_min)} דק'):</b> {name}\n"
                        f"צפויה תנודתיות סביב הפרסום.")
            done[key_pre] = 1
        elif -RUN_INTERVAL_MIN <= delta_min <= 0 and key_post not in done:
            msgs.append(f"📊 <b>פורסם עכשיו:</b> {name}\n"
                        f"שווה לבדוק את המספרים מול הצפי.")
            done[key_post] = 1
    # ניקוי מפתחות ישנים
    state["releases_done"] = {k: v for k, v in done.items()
                              if k.startswith(today_key)}
    return msgs


def upcoming_releases_text(now_et):
    """אירועים ב-24 השעות הקרובות, לסיכום היומי."""
    lines = []
    for d in range(2):
        day = now_et + timedelta(days=d)
        for wd, hh, mm, name in DATA_RELEASES:
            if day.weekday() != wd:
                continue
            event = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if 0 < (event - now_et).total_seconds() <= 86400:
                il = event.astimezone(TZ_IL)
                lines.append(f"• {il.strftime('%H:%M')} (שעון ישראל) — {name}")
    return "\n".join(lines) if lines else "• אין פרסומים מתוזמנים ב-24 השעות הקרובות"


# ============ סיכום יומי ============


def build_digest(state, prices, items, now_il, now_et):
    lines = ["☀️ <b>סיכום נפט יומי</b>", ""]
    ref = state.get("digest_prices", {})
    for name in ("WTI", "Brent"):
        if name in prices:
            p = prices[name]
            if name in ref and ref[name]:
                pct = (p - ref[name]) / ref[name] * 100
                lines.append(f"🛢 {name}: {p:.2f}$ ({pct:+.1f}% מאתמול)")
            else:
                lines.append(f"🛢 {name}: {p:.2f}$")
    state["digest_prices"] = dict(prices) or ref

    scored = sorted(((score_title(t), t, l) for t, l in items), reverse=True)
    top, used = [], set()
    for s, t, l in scored:
        if s > 0 and l not in used:
            top.append((s, t, l))
            used.add(l)
        if len(top) == 5:
            break
    if top:
        lines += ["", "<b>כותרות בולטות:</b>"]
        for _, t, l in top:
            lines.append(f"• {t}\n  {l}")

    lines += ["", "<b>פרסומים צפויים היום:</b>", upcoming_releases_text(now_et)]
    return "\n".join(lines)


def should_send_digest(state, now_il):
    today = now_il.strftime("%Y-%m-%d")
    if state.get("last_digest") == today:
        return False
    if now_il.hour < DIGEST_HOUR_IL:
        return False
    state["last_digest"] = today
    return True


# ============ ראשי ============


def main():
    state = load_state()
    now_il = datetime.now(TZ_IL)
    now_et = datetime.now(TZ_ET)
    print("ריצה:", now_il.isoformat())

    prices = get_prices()
    items = fetch_all_news()
    print(f"מחירים: {prices} | כתבות: {len(items)}")

    out = []
    out += check_price_alerts(state, prices)
    out += check_critical_news(state, items)
    out += check_data_releases(state, now_et)
    if should_send_digest(state, now_il):
        out.append(build_digest(state, prices, items, now_il, now_et))

    for msg in out:
        send(msg)
    print(f"נשלחו {len(out)} הודעות")

    save_state(state)


if __name__ == "__main__":
    main()
