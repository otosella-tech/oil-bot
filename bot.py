# -*- coding: utf-8 -*-
"""
בוט עדכוני נפט — רץ כל 5 דקות דרך GitHub Actions.
שולח לערוץ טלגרם: חדשות קריטיות עם ניתוח בעברית, תנועות מחיר חדות,
התראות לפני/אחרי פרסומי נתונים, וסיכום יומי.
"""

import html
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

TZ_IL = ZoneInfo("Asia/Jerusalem")
TZ_ET = ZoneInfo("America/New_York")

DIGEST_HOUR_IL = 8          # שעת הסיכום היומי (שעון ישראל)
PRICE_ALERT_PCT = 2.0       # אחוז תנועה שמצדיק התראה
NEWS_SCORE_THRESHOLD = 4    # ציון מינימלי לחדשות קריטיות (סינון ראשוני)
IMPACT_MIN = 7              # ציון השפעה 0-10 מהמודל שנדרש לשליחת פוש
MEMORY_DAYS = 3             # כמה ימים לזכור סיפורים שנשלחו (נגד חזרות)
MAX_SEEN = 600              # כמה כתבות לזכור למניעת כפילויות
MAX_LLM_PER_DAY = 60        # תקרת קריאות יומית למודל השפה (בתוך המכסה החינמית)

GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

RSS_FEEDS = [
    ("https://oilprice.com/rss/main", "OilPrice.com"),
    ("https://news.google.com/rss/search?q=crude+oil+OR+OPEC+OR+%22oil+prices%22&hl=en-US&gl=US&ceid=US:en", ""),
]

# מקורות מובילים — מקבלים בונוס ניקוד ומשקל מלא אצל השופט
TIER1_SOURCES = (
    "reuters", "bloomberg", "wall street journal", "wsj", "financial times",
    "cnbc", "marketwatch", "associated press", "ap news", "yahoo finance",
    "oilprice.com", "investing.com", "s&p global", "platts", "argus",
    "barron", "new york times", "washington post", "bbc", "cnn",
)

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
    # רעש — פרשנות וניתוח, לא אירועים (ניקוד שלילי)
    "analysis": -4, "outlook": -4, "forecast": -4, "prediction": -4,
    "week ahead": -5, "explainer": -5, "opinion": -5, "what to watch": -4,
    "here's": -3, "why ": -2, "how ": -2, "could ": -2, "preview": -4,
}

# לוח פרסומי נתונים קבועים (שעון ניו יורק): (יום בשבוע 0=שני, שעה, דקה, שם)
DATA_RELEASES = [
    (1, 16, 30, "דוח מלאים של מכון הנפט האמריקאי (API)"),
    (2, 10, 30, "דוח מלאים רשמי של ממשל האנרגיה (EIA) — הפרסום הכי מזיז בשבוע"),
    (4, 13, 0,  "ספירת אסדות קידוח (Baker Hughes)"),
    (4, 15, 30, "דוח פוזיציות ספקולנטים (CFTC COT)"),
]

PRE_WINDOW_MIN = 35  # כמה דקות מראש להתריע לפני פרסום נתונים

# ============ עזרים ============


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (oil-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_post_json(url, payload, timeout=40):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


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


# ============ מודל שפה (תרגום וניתוח) ============


def llm_quota_ok(state):
    today = datetime.now(TZ_IL).strftime("%Y-%m-%d")
    q = state.setdefault("llm", {"date": today, "count": 0})
    if q.get("date") != today:
        q["date"], q["count"] = today, 0
    return q["count"] < MAX_LLM_PER_DAY


def llm_call(prompt, state):
    """קריאה למודל השפה של גוגל. מחזיר טקסט או None."""
    if not GEMINI_API_KEY or not llm_quota_ok(state):
        return None
    state["llm"]["count"] += 1
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    for model in GEMINI_MODELS:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={GEMINI_API_KEY}")
            resp = http_post_json(url, payload)
            return resp["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"שגיאת מודל {model}:", e)
    return None


def extract_json(text):
    try:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def hebrewize(title, state):
    """תרגום כותרת + ניתוח השפעה. מחזיר (כותרת בעברית, ניתוח) או None."""
    prompt = (
        "אתה אנליסט שוקי נפט. לפניך כותרת חדשות באנגלית.\n"
        "1. תרגם אותה לעברית טבעית וקצרה.\n"
        "2. כתוב משפט ניתוח אחד: איך זה צפוי להשפיע על חוזי הנפט העתידיים "
        "(כיוון — עלייה/ירידה/תנודתיות — והסיבה).\n"
        'החזר JSON בלבד בפורמט: {"he": "...", "impact": "..."}\n'
        f"הכותרת: {title}")
    resp = llm_call(prompt, state)
    if not resp:
        return None
    data = extract_json(resp)
    if data and data.get("he") and data.get("impact"):
        return data["he"].strip(), data["impact"].strip()
    return None


def market_overview(titles, state):
    """פסקת מבט-שוק קצרה לסיכום היומי."""
    if not titles:
        return None
    joined = "\n".join(f"- {t}" for t in titles[:8])
    prompt = (
        "אתה אנליסט שוקי נפט. לפניך כותרות החדשות הבולטות מהיממה האחרונה.\n"
        "כתוב 2-3 משפטים בעברית: מה מצב שוק הנפט כרגע, מהם הכוחות המרכזיים "
        "שמשפיעים על המחיר, ולאן הסיכון נוטה. בלי הקדמות, ישר לעניין.\n"
        f"הכותרות:\n{joined}")
    resp = llm_call(prompt, state)
    return resp.strip() if resp else None


# ============ ניתוח מבוסס חוקים (כשאין מודל שפה) ============

RULE_IMPACTS = [
    (("ceasefire", "truce", "deal reached", "tensions ease"),
     "⬇️ הרגעה גיאופוליטית — מקטינה את פרמיית הסיכון, לחץ לירידת מחיר"),
    (("production increase", "output hike", "raise output", "boost production",
      "supply increase"),
     "⬇️ הגדלת היצע — לחץ לירידת מחיר"),
    (("production cut", "output cut", "supply cut", "quota"),
     "⬆️ צמצום היצע יזום — תומך בעליית מחיר"),
    (("hormuz", "tanker", "strait", "shipping"),
     "⬆️ סיכון לנתיבי אספקה ימיים — תומך בעליית מחיר ובפרמיית סיכון"),
    (("attack", "missile", "drone", "explosion", "war", "strike", "seize"),
     "⬆️ הסלמה ביטחונית באזור ייצור — תומך בעליית מחיר"),
    (("sanction", "embargo"),
     "⬆️ הגבלת ייצוא ממדינת מפתח — מצמצם היצע, תומך בעליית מחיר"),
    (("hurricane", "outage", "force majeure", "pipeline", "refinery"),
     "⬆️ שיבוש תשתיות ייצור/זיקוק — לרוב תומך בעליית מחיר"),
    (("spr", "strategic petroleum"),
     "⬇️ שחרור מהמאגר האסטרטגי מגדיל היצע זמין — לחץ לירידת מחיר"),
    (("plunge", "crash", "slump", "sink"),
     "📉 תיאור ירידה חדה שכבר מתרחשת בשוק"),
    (("surge", "soar", "spike", "jump", "rally"),
     "📈 תיאור עלייה חדה שכבר מתרחשת בשוק"),
]


def impact_rule(title):
    low = title.lower()
    for kws, msg in RULE_IMPACTS:
        if any(k in low for k in kws):
            return msg
    return "⚠️ אירוע רלוונטי לשוק הנפט — שווה מעקב"


# ============ מחירים ============


def price_from_stooq(sym):
    csv = http_get(f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv")
    row = csv.strip().splitlines()[1].split(",")
    return float(row[6])  # עמודת Close


def price_from_yahoo(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(sym)}?range=1d&interval=15m")
    data = json.loads(http_get(url))
    return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])


def get_prices():
    """מחירי נפט משני מקורות חינמיים — אם אחד נופל השני מחליף."""
    out = {}
    symbols = {"WTI": ("cl.f", "CL=F"), "Brent": ("cb.f", "BZ=F")}
    for name, (stooq_sym, yahoo_sym) in symbols.items():
        for fetch, sym in ((price_from_stooq, stooq_sym),
                           (price_from_yahoo, yahoo_sym)):
            try:
                price = fetch(sym)
                if price > 0:
                    out[name] = price
                    break
            except Exception as e:
                print(f"שגיאת מחיר {name} ({sym}):", e)
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
                direction = "עלייה" if pct > 0 else "ירידה"
                alerts.append(
                    f"{arrow} <b>תנועה חדה בנפט {name}</b>\n"
                    f"{prev:.2f} ➜ {price:.2f} דולר ({pct:+.1f}%)\n"
                    f"💡 {direction} חריגה בפרק זמן קצר — כנראה בתגובה "
                    f"לאירוע או פרסום נתונים. בדוק את החדשות האחרונות בערוץ.")
                base[name] = price
        else:
            base[name] = price
    return alerts


# ============ חדשות ============


def parse_rss(xml_text, default_source=""):
    """חילוץ כותרות, לינקים ומקור מ-RSS בלי ספריות חיצוניות."""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml_text, re.S):
        block = m.group(1)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
        s = re.search(r"<source[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</source>", block, re.S)
        if t and l:
            title = html.unescape(re.sub(r"\s+", " ", t.group(1)).strip())
            source = html.unescape(s.group(1).strip()) if s else default_source
            items.append((title, l.group(1).strip(), source))
    return items


def score_title(title):
    low = title.lower()
    return sum(pts for kw, pts in KEYWORDS.items() if kw in low)


def source_bonus(source):
    """בונוס למקור מוביל, קנס קטן למקור אלמוני."""
    if not source:
        return 0
    low = source.lower()
    if any(t1 in low for t1 in TIER1_SOURCES):
        return 2
    return -1


def item_id(title, link):
    return hashlib.md5((title + link).encode()).hexdigest()[:16]


def norm_title(title):
    """נרמול כותרת לזיהוי אותה ידיעה ממקורות שונים."""
    t = re.sub(r"\s*-\s*[^-]+$", "", title)  # הסרת שם המקור בסוף
    return re.sub(r"[^a-z0-9]", "", t.lower())[:60]


def linkify(title, link):
    return f'<a href="{html.escape(link, quote=True)}">{html.escape(title)}</a>'


def story_memory(state):
    """סיפורים שנשלחו בימים האחרונים — לחסימת חזרות אצל השופט."""
    today = datetime.now(TZ_IL).date()
    mem = state.setdefault("sent_stories", [])
    mem[:] = [e for e in mem
              if (today - datetime.strptime(e["d"], "%Y-%m-%d").date()).days
              <= MEMORY_DAYS]
    return mem


def remember_stories(state, items):
    today = datetime.now(TZ_IL).strftime("%Y-%m-%d")
    mem = state.setdefault("sent_stories", [])
    for title, _, _ in items:
        mem.append({"d": today, "t": title})


def llm_impact_filter(items, state):
    """שופט השפעה: מדרג כל מועמדת 0-10 לפי חשיבות, אמינות המקור,
    וזיכרון סיפורים שכבר נשלחו. אם המודל לא זמין — מחזיר את כולן."""
    if not GEMINI_API_KEY or not items:
        return items
    recent = [e["t"] for e in story_memory(state)][-15:]
    recent_txt = ("\n".join(f"- {t}" for t in recent)) if recent else "(אין)"
    joined = "\n".join(f"{i+1}. [{s or 'מקור לא ידוע'}] {t}"
                       for i, (t, _, s) in enumerate(items))
    prompt = (
        "אתה סוחר חוזי נפט. דרג כל כותרת מ-0 עד 10: עד כמה זהו אירוע חדש "
        "שצפוי להזיז את מחיר חוזי הנפט באופן מיידי.\n"
        "10 = אירוע דרמטי חדש (סגירת מצרי הורמוז, החלטת אופ\"ק מפתיעה).\n"
        "0-4 = פרשנות, ניתוח, תחזית, סקירה, או לא קשור.\n"
        "שקלל את המקור (בסוגריים המרובעים): סוכנות או כלי תקשורת מוביל — "
        "משקל מלא; מקור קטן או לא מוכר — הורד 2-3 נקודות, אלא אם האירוע "
        "דרמטי במיוחד.\n"
        "אלה סיפורים שכבר נשלחו לערוץ בימים האחרונים. כותרת שהיא חזרה, "
        "עדכון שולי או זווית חדשה על אחד מהם — דרג 0-4:\n"
        f"{recent_txt}\n"
        'החזר JSON בלבד: {"scores": [מספר לכל כותרת לפי הסדר]}\n'
        f"הכותרות:\n{joined}")
    resp = llm_call(prompt, state)
    if not resp:
        return items
    data = extract_json(resp)
    try:
        scores = [float(x) for x in data["scores"]]
        if len(scores) != len(items):
            return items
    except Exception:
        return items
    kept = [it for it, s in zip(items, scores) if s >= IMPACT_MIN]
    print(f"שופט השפעה: {len(items)} מועמדות ← {len(kept)} עברו (ציונים: {scores})")
    return kept


def batch_hebrew(titles, state):
    """תרגום מקבץ + שקלול נטו בקריאת מודל אחת. מחזיר (נטו, [כותרות]) או None."""
    joined = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "אתה אנליסט שוקי נפט. לפניך כמה כותרות חדשות שהגיעו יחד.\n"
        "1. שקלל אותן וכתוב שורה תחתונה אחת: לאן המכלול מושך את מחיר "
        "חוזי הנפט נטו (עלייה/ירידה/מנוגד-תנודתיות) ולמה. פתח בחץ ⬆️ או ⬇️ או ↕️.\n"
        "2. תרגם כל כותרת לעברית קצרה.\n"
        'החזר JSON בלבד: {"net": "...", "items": ["...", "..."]}\n'
        f"הכותרות:\n{joined}")
    resp = llm_call(prompt, state)
    if not resp:
        return None
    data = extract_json(resp)
    if (data and data.get("net") and isinstance(data.get("items"), list)
            and len(data["items"]) == len(titles)):
        return data["net"].strip(), [str(x).strip() for x in data["items"]]
    return None


def rule_net(titles):
    """שקלול כיוון מבוסס חוקים כשאין מודל."""
    up = down = 0
    for t in titles:
        imp = impact_rule(t)
        if imp.startswith(("⬆️", "📈")):
            up += 1
        elif imp.startswith(("⬇️", "📉")):
            down += 1
    if up and not down:
        return "⬆️ כל הידיעות מושכות לכיוון עליית מחיר"
    if down and not up:
        return "⬇️ כל הידיעות מושכות לכיוון ירידת מחיר"
    if up and down:
        return f"↕️ אותות מנוגדים ({up} לעלייה, {down} לירידה) — צפויה תנודתיות"
    return "⚠️ אירועים רלוונטיים לשוק — כיוון לא חד-משמעי"


def format_news(title, link, source, state):
    """בניית הודעת חדשות: עברית + ניתוח אם יש מודל, אחרת חוקים."""
    src_line = f"\n📰 {html.escape(source)}" if source else ""
    heb = hebrewize(title, state)
    if heb:
        he_title, impact = heb
        return (f"🚨 <b>חדשות נפט</b>\n"
                f"{linkify(he_title, link)}\n"
                f"💡 {html.escape(impact)}{src_line}")
    return (f"🚨 <b>חדשות נפט</b>\n"
            f"{linkify(title, link)}\n"
            f"💡 {impact_rule(title)}{src_line}")


def format_news_batch(items, state):
    """הודעה משוקללת אחת לכמה ידיעות שהגיעו באותה ריצה."""
    titles = [t for t, _, _ in items]
    heb = batch_hebrew(titles, state)
    lines = [f"🚨 <b>מקבץ חדשות נפט ({len(items)} ידיעות)</b>"]
    if heb:
        net, he_titles = heb
        lines.append(f"⚖️ <b>שורה תחתונה:</b> {html.escape(net)}")
        lines.append("")
        for (_, link, _), he_t in zip(items, he_titles):
            lines.append(f"• {linkify(he_t, link)}")
    else:
        lines.append(f"⚖️ <b>שורה תחתונה:</b> {rule_net(titles)}")
        lines.append("")
        for t, link, _ in items:
            lines.append(f"• {linkify(t, link)}\n  💡 {impact_rule(t)}")
    return "\n".join(lines)


def fetch_all_news():
    items = []
    for url, default_source in RSS_FEEDS:
        try:
            items.extend(parse_rss(http_get(url), default_source))
        except Exception as e:
            print("שגיאת פיד:", url, e)
    return items


def check_critical_news(state, items):
    """מחזיר הודעות: ידיעה בודדת כפוש רגיל, כמה ידיעות כהודעה משוקללת אחת."""
    seen = state.setdefault("seen", [])
    fresh = []
    batch_titles = set()
    for title, link, source in items:
        iid = item_id(title, link)
        nt = norm_title(title)
        if iid in seen or nt in seen or nt in batch_titles:
            continue
        score = score_title(title) + source_bonus(source)
        if score >= NEWS_SCORE_THRESHOLD and len(fresh) < 6:
            fresh.append((title, link, source))
            batch_titles.add(nt)
        seen += [iid, nt]
    state["seen"] = seen[-MAX_SEEN:]
    fresh = llm_impact_filter(fresh, state)
    if not fresh:
        return []
    remember_stories(state, fresh)
    if len(fresh) == 1:
        return [format_news(fresh[0][0], fresh[0][1], fresh[0][2], state)]
    return [format_news_batch(fresh, state)]


# ============ פרסומי נתונים מתוזמנים ============


def check_data_releases(state, now_et, post_window):
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
        if 0 < delta_min <= PRE_WINDOW_MIN and key_pre not in done:
            msgs.append(f"⏰ <b>בקרוב ({int(delta_min)} דק'):</b> {name}\n"
                        f"צפויה תנודתיות סביב הפרסום.")
            done[key_pre] = 1
        elif -post_window <= delta_min <= 0 and key_post not in done:
            msgs.append(f"📊 <b>פורסם עכשיו:</b> {name}\n"
                        f"שווה לבדוק את המספרים מול הצפי — הפתעה במלאים "
                        f"(ירידה=חיובי למחיר, עלייה=שלילי) מזיזה את השוק מיד.")
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
                lines.append(f"🛢 {name}: {p:.2f} דולר ({pct:+.1f}% מאתמול)")
            else:
                lines.append(f"🛢 {name}: {p:.2f} דולר")
    state["digest_prices"] = dict(prices) or ref

    scored = sorted(((score_title(t) + source_bonus(s), t, l)
                     for t, l, s in items), reverse=True)
    top, used = [], set()
    for s, t, l in scored:
        nt = norm_title(t)
        if s > 0 and nt not in used:
            top.append((s, t, l))
            used.add(nt)
        if len(top) == 5:
            break

    overview = market_overview([t for _, t, _ in top], state)
    if overview:
        lines += ["", f"🧭 <b>מבט על השוק:</b>\n{html.escape(overview)}"]

    if top:
        lines += ["", "<b>כותרות בולטות:</b>"]
        for _, t, l in top:
            heb = hebrewize(t, state)
            if heb:
                he_title, impact = heb
                lines.append(f"• {linkify(he_title, l)}\n  💡 {html.escape(impact)}")
            else:
                lines.append(f"• {linkify(t, l)}\n  💡 {impact_rule(t)}")

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


def minutes_since_last_run(state, now_il):
    """חלון אחורה לזיהוי אירועים — לפי הזמן שעבר מהריצה הקודמת בפועל."""
    try:
        last = datetime.fromisoformat(state["last_run"])
        gap = (now_il - last).total_seconds() / 60
        return max(6.0, min(gap + 3, 120.0))
    except Exception:
        return 35.0


def main():
    state = load_state()
    now_il = datetime.now(TZ_IL)
    now_et = datetime.now(TZ_ET)
    print("ריצה:", now_il.isoformat())
    print("מודל שפה:", "פעיל" if GEMINI_API_KEY else "כבוי (ניתוח מבוסס חוקים)")

    post_window = minutes_since_last_run(state, now_il)
    state["last_run"] = now_il.isoformat()

    prices = get_prices()
    items = fetch_all_news()
    print(f"מחירים: {prices} | כתבות: {len(items)}")

    out = []
    out += check_price_alerts(state, prices)
    out += check_critical_news(state, items)
    out += check_data_releases(state, now_et, post_window)
    if should_send_digest(state, now_il):
        out.append(build_digest(state, prices, items, now_il, now_et))

    for msg in out:
        send(msg)
    print(f"נשלחו {len(out)} הודעות")

    save_state(state)


if __name__ == "__main__":
    main()
