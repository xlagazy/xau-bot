"""
XAU Alert Bot – เช็คจังหวะเข้าซื้อทองทุก 1 ชม. แล้วส่งข้อความเข้า Telegram (หรือ Slack)
มี 2 โหมด สลับด้วย GitHub Variable ENTRY_PRICE
  - ว่าง / 0  = ยังไม่มีของ → หาจังหวะซื้อ
  - ใส่ราคาซื้อ = ถือของอยู่ → บอกว่าขายได้หรือยัง (เป้าแรก TAKE_PROFIT % แล้วเลื่อนจุดขายตามถ้ายังขึ้นต่อ)
ใช้ Pivot Point จากแท่งวันก่อนหน้า คำนวณใหม่ทุกครั้ง ไม่ต้องกำหนดราคาล่วงหน้า
แจ้งสถานการณ์เท่านั้น ไม่ใช่คำแนะนำการลงทุน
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

SYMBOL = os.getenv("SYMBOL", "GC=F")            # GC=F = ทองฟิวเจอร์ส COMEX (ใกล้เคียง Spot)
ONLY_SIGNALS = os.getenv("ONLY_SIGNALS", "false").lower() == "true"
# ชั่วโมง (เวลาไทย) ที่ส่งข้อความเต็มเสมอ รอบอื่นที่ตั้งเวลาไว้จะส่งเฉพาะตอนเป็น 🟢 หรือ 🔴
FULL_HOURS = {int(h) for h in os.getenv("FULL_HOURS", "7,20").split(",") if h.strip()}
NEWS_FILE = os.getenv("NEWS_FILE", "news_times.txt")
BKK = timezone(timedelta(hours=7))


def f(n):
    return f"{n:,.2f}"


# ---------------------------------------------------------------- market data
def market_closed(now_utc):
    """ทองปิดตลาดราว ศุกร์ 22:00 UTC ถึง อาทิตย์ 22:00 UTC"""
    wd = now_utc.weekday()  # จันทร์=0 … อาทิตย์=6
    if wd == 5:
        return True
    if wd == 4 and now_utc.hour >= 22:
        return True
    if wd == 6 and now_utc.hour < 22:
        return True
    return False


SPOT_SOURCES = (
    # (ชื่อ, url, ฟังก์ชันอ่านราคา) – ฟรี ไม่ต้องใช้ key ราคา Spot XAU/USD แบบ real time
    ("Swissquote", "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD",
     lambda j: (j[0]["spreadProfilePrices"][0]["bid"] + j[0]["spreadProfilePrices"][0]["ask"]) / 2),
    ("gold-api.com", "https://api.gold-api.com/price/XAU", lambda j: j["price"]),
)


def fetch_spot():
    """ราคา Spot ทองล่าสุด (real time) ลองทีละแหล่ง ถ้าไม่ได้ทั้งหมดคืน (None, None)"""
    for name, url, parse in SPOT_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                px = float(parse(json.loads(resp.read().decode())))
            if 500 < px < 20000:
                return px, name
        except Exception as exc:  # noqa: BLE001
            print(f"ดึงราคา Spot จาก {name} ไม่สำเร็จ: {exc}")
    return None, None


def to_spot(d):
    """Yahoo GC=F ช้า ~10 นาที และเป็นราคาฟิวเจอร์ส (แพงกว่า Spot เล็กน้อย)
    ใช้ราคา Spot real time เป็นราคาปัจจุบัน แล้วเลื่อนข้อมูลย้อนหลังลงมาเท่าส่วนต่าง ให้แนวต่างๆ อยู่ในสเกลเดียวกัน"""
    spot, src = fetch_spot()
    if spot is None:
        print("ใช้ราคา GC=F จาก Yahoo แทน (ช้า ~10 นาที)")
        d["src"] = "Yahoo GC=F (ช้า ~10 นาที)"
        return d
    basis = d["px"] - spot
    if abs(basis) > 0.03 * spot:  # ส่วนต่างผิดปกติ (เช่นตลาดเพิ่งเปิด) ไม่เลื่อนข้อมูล
        print(f"ส่วนต่างฟิวเจอร์ส-Spot ผิดปกติ ({basis:.1f}) ไม่เลื่อนข้อมูลย้อนหลัง")
        basis = 0.0
    for k in ("yH", "yL", "yC", "tH", "tL"):
        d[k] -= basis
    for k in ("closes", "highs", "lows"):
        d[k] = [x - basis for x in d[k]]
    d["px"], d["stamp"], d["src"] = spot, datetime.now(timezone.utc), src
    print(f"ราคา Spot {spot:.2f} จาก {src} (ส่วนต่างกับ GC=F {basis:+.1f})")
    return d


def fetch_data():
    import yfinance as yf
    tk = yf.Ticker(SYMBOL)
    daily = tk.history(period="4mo", interval="1d").dropna()  # ย้อนหลังพอให้ AI ดูเทรนด์
    if len(daily) < 2:
        raise RuntimeError("ดึงข้อมูลรายวันไม่พอ")
    intra = tk.history(period="1d", interval="5m").dropna()
    y, t = daily.iloc[-2], daily.iloc[-1]
    if len(intra):
        price = float(intra["Close"].iloc[-1])
        stamp = intra.index[-1].to_pydatetime()
    else:
        price = float(t["Close"])
        stamp = daily.index[-1].to_pydatetime()
    return {
        "yH": float(y["High"]), "yL": float(y["Low"]), "yC": float(y["Close"]),
        "tH": float(t["High"]), "tL": float(t["Low"]),
        "px": price, "stamp": stamp,
        "closes": [round(float(c), 1) for c in daily["Close"].iloc[-60:]],
        "highs": [float(h) for h in daily["High"].iloc[-60:]],
        "lows": [float(x) for x in daily["Low"].iloc[-60:]],
        "dates": [ix.date() for ix in daily.index[-60:]],
    }


# ---------------------------------------------------------------- AI (Gemini)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # Flash-Lite: โควต้าฟรีต่อวันพอสำหรับรันทุกชั่วโมง
NEWS_RSS = ("https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+OR+Fed+when:1d"
            "&hl=en-US&gl=US&ceid=US:en")


def fetch_news(limit=10):
    """ดึงหัวข้อข่าวล่าสุดจาก Google News RSS (ฟรี ไม่ต้องใช้ key)"""
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(NEWS_RSS, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            root = ET.fromstring(resp.read())
        items = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            date = (it.findtext("pubDate") or "").strip()[:22]
            if title:
                items.append(f"- {title} ({date})")
            if len(items) >= limit:
                break
        return "\n".join(items) or "(ไม่พบข่าว)"
    except Exception as exc:  # noqa: BLE001
        print(f"ดึงข่าวไม่สำเร็จ: {exc}")
        return "(ดึงข่าวไม่สำเร็จรอบนี้)"


def ai_analysis(d, r):
    """ให้ Gemini อธิบายสัญญาณ สรุปข่าว และประเมินเทรนด์ ถ้าไม่มี key หรือเรียกไม่สำเร็จ จะคืน None"""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    closes = d["closes"]
    sma20 = sum(closes[-20:]) / min(20, len(closes))
    sma50 = sum(closes[-50:]) / min(50, len(closes))
    if r.get("mode") == "hold":
        style = (f"ผู้ใช้ถือทองอยู่ ซื้อที่ {r['entry']:.1f} อยากขายทำกำไรให้ได้มากที่สุด "
                 f"เป้าแรก +{TAKE_PROFIT:.0f}% ถ้าแนวโน้มยังขึ้นให้ถือต่อแล้วเลื่อนจุดขายตาม")
        levels = ("🟢 ขายได้ / 🔵 ถึงเป้าแล้ว ยังขึ้นต่อ ถือต่อได้ / ⚪ ถือต่อ ยังไม่ถึงเป้า / "
                  "🟠 ระวัง กำไรลดลง / 🔴 ตัดขาดทุน")
        rule = ("กฎ: ถ้าบอทบอก 🟢 หรือ 🔴 ห้ามเปลี่ยนเป็นถือต่อ ถ้าบอทบอก 🔵 ให้ประเมินว่าเป้าถัดไปที่บอทเสนอ "
                "สมเหตุสมผลไหม หรือควรทยอยขายบางส่วน")
        topics = ("📈 เทรนด์: แนวโน้มยังหนุนให้ถือต่อไหม เป้าถัดไปที่น่าจะไปถึงได้กี่ % "
                  "และสัญญาณอะไรที่บอกว่าควรขาย")
    else:
        style = ("ซื้อตอนย่อลึก หวังกำไร 3% ขึ้นไป (ถือได้หลายวัน)" if r.get('mode') == 'dip3'
                 else "ย่อซื้อ/เบรกขึ้น ตามแนว Pivot รายวัน")
        levels = "🟢 เข้าได้ / ⚪ ยังไม่ใช่จังหวะ / 🟠 ไม่ควรเข้า / 🔴 ห้ามซื้อ"
        rule = ("กฎ: ระดับที่เลือกต้องเท่ากับหรือเข้มกว่าผลของบอทเท่านั้น (ลำดับจากหลวมไปเข้ม: 🟢 → ⚪ → 🟠 → 🔴) "
                "ห้ามเลือกระดับที่หลวมกว่าบอท เช่น บอทบอก ⚪ ห้ามตอบ 🟢")
        topics = "📈 เทรนด์: ประเมินเทรนด์ระยะสั้นและกลางจากราคาย้อนหลังและ SMA ว่าเทรนด์หนุนหรือขัดกับการซื้อ"
    prompt = f"""คุณเป็นผู้ช่วยวิเคราะห์ทองคำ XAU/USD เขียนภาษาไทยสั้น กระชับ อ่านง่ายบนมือถือ

ข้อมูลจากบอท (ราคา Spot XAU/USD real time ข้อมูลย้อนหลังปรับจาก GC=F ให้อยู่สเกล Spot):
- ราคาตอนนี้ {d['px']:.1f} เวลา {d['stamp'].astimezone(BKK):%Y-%m-%d %H:%M} (เวลาไทย)
- เมื่อวาน High {d['yH']:.1f} Low {d['yL']:.1f} Close {d['yC']:.1f}
- {r.get('levels_text') or f"Pivot P {r['P']:.1f} / S1 {r['S1']:.1f} / S2 {r['S2']:.1f} / R1 {r['R1']:.1f} / จุดเบรก {r['trig']:.1f}"}
- สไตล์ผู้ใช้: {style}
- ผลตามกฎของบอท: {r['icon']} {r['title']} – {r['why']}
- SMA20 {sma20:.1f} / SMA50 {sma50:.1f}
- ราคาปิดรายวันย้อนหลัง (เก่า→ใหม่): {closes}

หัวข้อข่าวล่าสุด 24 ชม. (ภาษาอังกฤษ จาก Google News):
{fetch_news()}

ระดับที่ใช้: {levels}
ผลของบอทตอนนี้คือ: {verdict(r)}

ตอบเป็น 4 หัวข้อ หัวข้อละไม่เกิน 2 บรรทัด ใช้รูปแบบนี้เท่านั้น:
🧭 ความเห็น AI: เลือก 1 ระดับจากระดับข้างบน แล้วบอกเหตุผลสั้นๆ
   {rule}
📊 สัญญาณ: อธิบายผลของบอทแบบภาษาคน และความเสี่ยงที่ต้องระวัง
📰 ข่าว: จากหัวข้อข่าวข้างบน สรุปเฉพาะข่าวที่กระทบทอง (Fed, ดอลลาร์, ข้อมูลเศรษฐกิจสหรัฐฯ, ภูมิรัฐศาสตร์) แล้วสรุปผลต่อราคา ถ้ามีข่าวสำคัญกำลังจะออกให้บอกเวลา (เวลาไทย)
{topics}

ห้ามสั่งให้ซื้อหรือขายแบบยืนยัน ให้พูดเป็นสถานการณ์และความเสี่ยงเท่านั้น ไม่ต้องมีคำนำหรือคำลงท้าย"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    base = {"contents": [{"parts": [{"text": prompt}]}]}
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, json.dumps(base).encode(), headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode())
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text[:1800]
        except urllib.error.HTTPError as exc:
            print(f"Gemini ตอบ HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
            if exc.code in (400, 401, 403, 404):
                break  # key หรือชื่อโมเดลผิด ลองซ้ำก็ไม่ช่วย
        except Exception as exc:  # noqa: BLE001
            print(f"เรียก Gemini ไม่สำเร็จ: {exc}")
        time.sleep(10)
    return None


def news_soon(now_utc, path=NEWS_FILE):
    """อ่านเวลาข่าวสำคัญจากไฟล์ (บรรทัดละ 1 เวลา รูปแบบ 2026-11-12 13:30 เวลาไทย)"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#")[0].strip()
            if not line:
                continue
            try:
                when = datetime.strptime(line[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BKK)
            except ValueError:
                continue
            delta = (when - now_utc).total_seconds() / 60
            if -30 <= delta <= 60:
                return line
    return None


# ---------------------------------------------------------------- rules
def evaluate(d, news=False):
    H, L, C, px = d["yH"], d["yL"], d["yC"], d["px"]
    tH, tL = max(d["tH"], px), min(d["tL"], px)
    R = H - L
    P = (H + L + C) / 3
    S1, S2, R1 = 2 * P - H, P - R, 2 * P - L
    trig = H + 0.1 * R
    lv = dict(R=R, P=P, S1=S1, S2=S2, R1=R1, trig=trig, tH=tH, tL=tL)

    def out(icon, title, why, plan=None):
        return dict(icon=icon, title=title, why=why, plan=plan, signal=icon in ("🟢", "🔴"), **lv)

    if R <= 0:
        return out("⚠️", "ข้อมูลผิดปกติ", "High กับ Low เมื่อวานเท่ากัน คำนวณไม่ได้")
    if news:
        return out("🟠", "ห้ามเข้า: ช่วงข่าวสำคัญ", f"มีข่าว {news}")
    if px < S2:
        return out("🔴", "งดซื้อ: ราคาหลุด S2", f"ราคาต่ำกว่า S2 ({f(S2)}) แรงขายแรง")
    if (tH - tL) > 1.5 * R:
        return out("🟠", "ห้ามเข้า: วันนี้ผันผวนผิดปกติ",
                   f"แกว่ง {f(tH - tL)} เกิน 1.5×Range ({f(1.5 * R)})")

    if tL <= S1 and (px - tL) >= 0.2 * R and tL >= S2:
        e, st = px, tL - 0.25 * R
        t1, t2 = (P, R1) if e < P else (R1, None)
        rr = (R1 - e) / (e - st)   # วัดความคุ้มถึง R1 (P ใช้แบ่งขายบางส่วน)
        plan = dict(entry=e, stop=st, t1=t1, t2=t2, rr=rr)
        if rr >= 1.5:
            return out("🟢", "สัญญาณซื้อ A: ย่อซื้อ",
                       f"แตะ S1 แล้วเด้ง {f(px - tL)} จาก Low – เปิดกราฟ 15m ยืนยันก่อนเข้า", plan)
        return out("🟠", "เข้าเงื่อนไข A แต่ไม่คุ้มเสี่ยง", f"R:R เพียง 1:{rr:.2f} รอย่อใกล้ S1", plan)

    if px >= trig and tL >= P:
        e, st, t1 = H, P, H + R
        rr = (t1 - e) / (e - st)
        plan = dict(entry=e, stop=st, t1=t1, t2=None, rr=rr)
        if rr >= 1.5:
            return out("🟢", "สัญญาณซื้อ B: เบรกขึ้น",
                       f"ยืนเหนือ High เมื่อวาน – ตั้งรอย่อที่ {f(H)} ไม่ไล่ราคา", plan)
        return out("🟠", "เข้าเงื่อนไข B แต่ไม่คุ้มเสี่ยง", f"R:R เพียง 1:{rr:.2f}", plan)

    if S1 <= px <= R1 and abs(px - P) <= 0.25 * R:
        return out("⚪", "ไม่มีจังหวะ: ราคากลางกรอบ",
                   f"ห่าง P เพียง {f(abs(px - P))} รอแตะ S1 {f(S1)} หรือเบรก {f(trig)}")

    hints = []
    if tL > S1:
        hints.append(f"รอแตะ S1 {f(S1)}")
    elif px - tL < 0.2 * R:
        hints.append(f"แตะ S1 แล้ว รอเด้งถึง {f(tL + 0.2 * R)}")
    if px < trig:
        hints.append(f"หรือรอเบรก {f(trig)}")
    return out("⚪", "ไม่มีจังหวะ: ยังไม่เข้าเงื่อนไข", " ".join(hints))


def r0(n):
    return f"{n:,.0f}"


def verdict(r):
    """แปลงผลของกฎเป็น 4 ระดับ: เข้าได้ / ยังไม่ใช่จังหวะ / ไม่ควรเข้า / ห้ามซื้อ"""
    t = r["title"]
    if r.get("mode") == "hold":
        return {"🟢": "🟢 ขายได้", "🔵": "🔵 ถึงเป้าแล้ว ยังขึ้นต่อ ถือต่อได้", "🟠": "🟠 ระวัง กำไรลดลง",
                "🔴": "🔴 ตัดขาดทุน", "⚪": "⚪ ถือต่อ ยังไม่ถึงเป้า"}.get(r["icon"], "⚠️ ข้อมูลผิดปกติ")
    if r["icon"] == "🟢":
        return "🟢 เข้าได้"
    if r["icon"] == "🔴" or t.startswith("ข้อมูลผิดปกติ"):
        return "🔴 ห้ามซื้อ"
    if r["icon"] == "🟠":
        return "🟠 ไม่ควรเข้า"
    return "⚪ ยังไม่ใช่จังหวะ"


def format_message(d, r):
    """ข้อความแบบอ่านง่าย บอกระดับชัดเจน พร้อมรายละเอียดว่าทำไมและต้องรออะไร"""
    when = d["stamp"].astimezone(BKK).strftime("%H:%M")
    v = verdict(r)
    S1, S2, P, R1, trig, R = r["S1"], r["S2"], r["P"], r["R1"], r["trig"], r["R"]
    lines = [f"{v}", f"ราคาตอนนี้ {r0(d['px'])} ({when} น.)", ""]
    p = r.get("plan")
    t = r["title"]

    if v.startswith("🟢") and p:
        kind = "ย่อซื้อที่แนวรับ" if "A" in t else "ซื้อตามการเบรกขึ้น"
        lines += [f"รูปแบบ: {kind}",
                  f"✅ จุดเข้า {r0(p['entry'])}" + (" (ตั้งรอย่อ ไม่ไล่ราคา)" if "B" in t else ""),
                  f"🛑 ตัดขาดทุน {r0(p['stop'])} (เสี่ยง {r0(abs(p['entry'] - p['stop']))})",
                  f"🎯 เป้า 1: {r0(p['t1'])}" + (f" / เป้า 2: {r0(p['t2'])}" if p["t2"] else ""),
                  f"⚖️ คุ้มเสี่ยง 1 : {p['rr']:.1f}",
                  "",
                  "ก่อนกดซื้อ เช็ค 3 ข้อ:",
                  "1) กราฟ 15 นาที แท่งล่าสุดปิดเขียว หรือมีไส้ล่างยาว",
                  "2) ไม่มีข่าวแรงใน 1 ชม.ข้างหน้า",
                  "3) ขนาดไม้ เสียได้ไม่เกิน 1–2% ของพอร์ตถ้าโดนตัดขาดทุน"]
    elif v.startswith("⚪"):
        lines.append("เหตุผล: ราคายังไม่ถึงจุดที่ได้เปรียบ เข้าตอนนี้เสี่ยงโดนสะบัด")
        lines.append("")
        lines.append("จังหวะที่จะเปลี่ยนเป็น 🟢 เข้าได้:")
        if r["tL"] > S1:
            lines.append(f"• ย่อซื้อ: ราคาลงแตะ {r0(S1)} แล้วเด้งกลับขึ้นเหนือ {r0(S1 + 0.2 * R)}")
        else:
            lines.append(f"• ย่อซื้อ: ราคาลงแตะ {r0(S1)} ไปแล้ว รอเด้งกลับขึ้นไปยืนเหนือ {r0(r['tL'] + 0.2 * R)}")
        lines.append(f"• เบรกขึ้น: ราคายืนเหนือ {r0(trig)} ได้ แล้วรอย่อซื้อแถว {r0(d['yH'])}")
        lines.append("")
        lines.append(f"⚠️ ถ้าหลุด {r0(S2)} จะเปลี่ยนเป็น 🔴 ห้ามซื้อ")
    elif v.startswith("🟠"):
        reason = {
            "ห้ามเข้า: ช่วงข่าวสำคัญ": f"ใกล้เวลาข่าวสำคัญ ({r['why'].replace('มีข่าว ', '')}) ราคามักวิ่งแรงทั้งสองทาง",
            "ห้ามเข้า: วันนี้ผันผวนผิดปกติ": "วันนี้ราคาแกว่งแรงผิดปกติ แนวรับแนวต้านใช้ไม่ค่อยได้",
            "เข้าเงื่อนไข A แต่ไม่คุ้มเสี่ยง": "ราคาเด้งจากแนวรับมาไกลแล้ว ซื้อตอนนี้กำไรที่เหลือไม่คุ้มความเสี่ยง",
            "เข้าเงื่อนไข B แต่ไม่คุ้มเสี่ยง": "ทะลุขึ้นแล้ว แต่จุดตัดขาดทุนไกลเกินไป ไม่คุ้ม",
        }.get(t, r["why"])
        lines.append(f"เหตุผล: {reason}")
        if p:
            lines.append(f"(ถ้าเข้าตอนนี้ คุ้มเสี่ยงแค่ 1 : {p['rr']:.1f} ต้องได้อย่างน้อย 1 : 1.5)")
        lines.append("")
        lines.append("รออะไร:")
        if "ข่าว" in t:
            lines.append("• รอข่าวออกแล้วอย่างน้อย 30 นาที ให้ราคานิ่งก่อน")
        elif "ผันผวน" in t:
            lines.append("• รอพรุ่งนี้ ให้แนว Pivot คำนวณใหม่")
        else:
            lines.append(f"• รอราคาย่อกลับมาใกล้ {r0(S1)} หรือ {r0(P)} แล้วค่อยประเมินใหม่")
    else:  # 🔴
        lines.append(f"เหตุผล: {r['why'] if t.startswith('ข้อมูลผิดปกติ') else 'ราคาหลุดแนวรับสุดท้าย ' + r0(S2) + ' แรงขายแรง มีโอกาสลงต่อ'}")
        lines.append("")
        lines.append("รออะไร:")
        lines.append(f"• รอราคากลับขึ้นมายืนเหนือ {r0(S2)} ได้ก่อน หรือรอวันใหม่")
        lines.append("• ห้ามรับมีดที่กำลังตก แม้ราคาจะดูถูก")

    lines.append("")
    lines.append(f"แนวสำคัญวันนี้: S2 {r0(S2)} | S1 {r0(S1)} | P {r0(P)} | R1 {r0(R1)} | เบรก {r0(trig)}")
    lines.append("แจ้งสถานการณ์ตามกฎ ไม่ใช่คำแนะนำการลงทุน")
    return "\n".join(lines)


# ---------------------------------------------------------------- กลยุทธ์ซื้อตอนย่อลึก หวังกำไร ≥ 3%
STRATEGY = os.getenv("STRATEGY", "dip3")      # dip3 = ย่อลึกหวัง 3%+ / pivot = แนว Pivot รายวันแบบเดิม
MIN_GAIN = float(os.getenv("MIN_GAIN", "3"))  # กำไรขั้นต่ำที่ต้องการ (%)
MIN_RR = 1.5


def _rsi(closes, n=14):
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        diff = b - a
        gains += max(diff, 0)
        losses += max(-diff, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _atr(highs, lows, closes, n=14):
    trs = []
    for i in range(len(closes) - n, len(closes)):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return sum(trs) / len(trs)


def evaluate_dip(d, news=False):
    """หาจังหวะซื้อตอนราคาย่อลึกพอที่จะมีโอกาสเด้งกลับได้อย่างน้อย MIN_GAIN %"""
    px = d["px"]
    highs, lows, closes = d["highs"][:], d["lows"][:], d["closes"][:]
    closes[-1] = px                                   # ใช้ราคาล่าสุดแทนราคาปิดวันนี้
    highs[-1], lows[-1] = max(highs[-1], px), min(lows[-1], px)
    h20, l20 = max(highs[-20:]), min(lows[-20:])
    tL = lows[-1]
    atr = _atr(highs, lows, closes)
    rsi = _rsi(closes)
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / min(50, len(closes))
    dd = (h20 - px) / h20 * 100                       # ย่อจากยอด 20 วันกี่ %
    target = l20 + 0.5 * (h20 - l20)                  # เป้า = ครึ่งทางของกรอบ 20 วัน (เด้งกลับ 50%)
    gain = (target - px) / px * 100
    stop = min(tL, l20) - 0.5 * atr
    rr = (target - px) / (px - stop) if px > stop else 0
    buy_below = target / (1 + MIN_GAIN / 100)         # ต้องซื้อต่ำกว่านี้ถึงจะได้กำไร ≥ MIN_GAIN
    bounce_need = tL + 0.3 * atr                      # เด้งจาก low วันนี้อย่างน้อย 0.3 ATR
    downtrend = sma20 < sma50 and px < sma50
    lv = dict(mode="dip3", h20=h20, l20=l20, atr=atr, rsi=rsi, sma20=sma20, sma50=sma50, dd=dd,
              target=target, gain=gain, stop=stop, rr=rr, buy_below=buy_below,
              bounce_need=bounce_need, tL=tL, downtrend=downtrend,
              levels_text=(f"ยอด 20 วัน {h20:.1f} / ฐาน 20 วัน {l20:.1f} / ATR14 {atr:.1f} / RSI14 {rsi:.0f} / "
                           f"เป้าเด้ง {target:.1f} / ต้องซื้อต่ำกว่า {buy_below:.1f} ถึงได้ {MIN_GAIN:.0f}%"))

    def out(icon, title, why, plan=None):
        return dict(icon=icon, title=title, why=why, plan=plan, signal=icon in ("🟢", "🔴"), **lv)

    plan = dict(entry=px, stop=stop, t1=target, t2=h20, rr=rr, gain=gain)
    if news:
        return out("🟠", "ห้ามเข้า: ช่วงข่าวสำคัญ", f"มีข่าว {news}", plan)
    if px < l20 - 0.5 * atr or (px <= l20 * 1.002 and px - tL < 0.3 * atr):
        return out("🔴", "ห้ามซื้อ: กำลังทำ low ใหม่", f"ราคาหลุดฐาน 20 วัน {r0(l20)} และยังไม่เด้ง")
    if gain < MIN_GAIN:
        return out("⚪", "ยังไม่ลงต่ำพอ", f"ถ้าซื้อตอนนี้ เด้งถึงเป้าได้แค่ {gain:.1f}%")
    if px < bounce_need:
        return out("⚪", "ลงต่ำพอแล้ว รอเด้งยืนยัน", f"ยังไม่เด้งจาก low วันนี้ ({r0(tL)})")
    if rr < MIN_RR:
        return out("🟠", "เด้งแล้วแต่ไม่คุ้มเสี่ยง", f"คุ้มเสี่ยงแค่ 1:{rr:.1f}", plan)
    return out("🟢", "เข้าได้: ย่อลึกแล้วเริ่มเด้ง", f"ย่อจากยอด {dd:.1f}% RSI {rsi:.0f}", plan)


def format_dip(d, r):
    when = d["stamp"].astimezone(BKK).strftime("%H:%M")
    v = verdict(r)
    p = r.get("plan")
    lines = [v, f"ราคาตอนนี้ {r0(d['px'])} ({when} น.)", ""]
    t = r["title"]
    if v.startswith("🟢"):
        lines += ["รูปแบบ: ซื้อตอนย่อลึก หวังเด้งกลับ (ถือได้หลายวัน)",
                  f"✅ จุดเข้า {r0(p['entry'])}",
                  f"🛑 ตัดขาดทุน {r0(p['stop'])} (−{(p['entry'] - p['stop']) / p['entry'] * 100:.1f}%)",
                  f"🎯 เป้า 1: {r0(p['t1'])} (+{p['gain']:.1f}%)",
                  f"🎯 เป้า 2: {r0(p['t2'])} (+{(p['t2'] - p['entry']) / p['entry'] * 100:.1f}%) ยอดเดิม 20 วัน",
                  f"⚖️ คุ้มเสี่ยง 1 : {p['rr']:.1f}",
                  "",
                  "ก่อนกดซื้อ เช็ค 3 ข้อ:",
                  "1) กราฟ 1 ชม. แท่งล่าสุดปิดเขียว ไม่ทำ low ใหม่",
                  "2) ไม่มีข่าวแรงใน 1 ชม.ข้างหน้า",
                  "3) ขนาดไม้ เสียได้ไม่เกิน 1–2% ของพอร์ตถ้าโดนตัดขาดทุน"]
        if r["downtrend"]:
            lines.append("⚠️ เทรนด์ใหญ่ยังเป็นขาลง (ต่ำกว่า SMA50) เป็นการเล่นเด้ง ควรลดขนาดไม้")
    elif v.startswith("⚪"):
        if "ไม่ลงต่ำพอ" in t:
            lines += [f"เหตุผล: ถ้าซื้อตอนนี้ เด้งถึงเป้าได้แค่ {r['gain']:.1f}% ไม่ถึง {MIN_GAIN:.0f}%",
                      "",
                      "จังหวะที่จะเปลี่ยนเป็น 🟢 เข้าได้:",
                      f"• ราคาลงไปต่ำกว่า {r0(r['buy_below'])} (อีก {(d['px'] - r['buy_below']) / d['px'] * 100:.1f}%)",
                      "• แล้วเด้งกลับขึ้นจาก low ของวันนั้นอย่างน้อย " + r0(0.3 * r['atr']) + " ดอลลาร์"]
        else:
            lines += [f"เหตุผล: ราคาลงต่ำพอแล้ว (เป้าเด้ง +{r['gain']:.1f}%) แต่ยังไม่เห็นแรงซื้อกลับ",
                      "",
                      "จังหวะที่จะเปลี่ยนเป็น 🟢 เข้าได้:",
                      f"• ราคาเด้งกลับขึ้นไปยืนเหนือ {r0(r['bounce_need'])}"]
        lines += ["", f"⚠️ ถ้าหลุด {r0(r['l20'] - 0.5 * r['atr'])} จะเปลี่ยนเป็น 🔴 ห้ามซื้อ"]
    elif v.startswith("🟠"):
        reason = {"ห้ามเข้า: ช่วงข่าวสำคัญ": "ใกล้เวลาข่าวสำคัญ ราคามักวิ่งแรงทั้งสองทาง รอข่าวออกแล้ว 30 นาที",
                  "เด้งแล้วแต่ไม่คุ้มเสี่ยง": f"จุดตัดขาดทุนไกลเกินไปเทียบกับกำไร (1 : {r['rr']:.1f}) รอย่อกลับมาใกล้ low ก่อน"}.get(t, r["why"])
        lines.append(f"เหตุผล: {reason}")
    else:
        lines += [f"เหตุผล: ราคาหลุดฐาน 20 วัน ({r0(r['l20'])}) และยังไม่มีแรงซื้อกลับ มีโอกาสลงต่อ",
                  "",
                  "รออะไร:",
                  f"• รอราคาหยุดทำ low ใหม่ แล้วเด้งขึ้นอย่างน้อย {r0(0.3 * r['atr'])} ดอลลาร์",
                  "• ห้ามรับมีดที่กำลังตก แม้ราคาจะดูถูก"]
    trend = "ขาลง" if r["downtrend"] else ("ขาขึ้น" if r["sma20"] > r["sma50"] else "ไซด์เวย์")
    lines += ["",
              f"ภาพรวม: ย่อจากยอด 20 วัน {r['dd']:.1f}% | RSI {r['rsi']:.0f} | เทรนด์ {trend}",
              f"กรอบ 20 วัน: {r0(r['l20'])} – {r0(r['h20'])} | ซื้อได้กำไร {MIN_GAIN:.0f}% ต้องต่ำกว่า {r0(r['buy_below'])}",
              "แจ้งสถานการณ์ตามกฎ ไม่ใช่คำแนะนำการลงทุน"]
    return "\n".join(lines)


# ---------------------------------------------------------------- โหมดถือของอยู่: บอกว่าขายได้หรือยัง
# ตั้งค่าใน GitHub → Settings → Secrets and variables → Actions → Variables
ENTRY_PRICE = float(os.getenv("ENTRY_PRICE") or 0)  # ราคาที่ซื้อ (USD/ออนซ์) ว่างหรือ 0 = ยังไม่มีของ → โหมดหาจังหวะซื้อ
BUY_DATE = (os.getenv("BUY_DATE") or "").strip()    # วันที่ซื้อ YYYY-MM-DD ใช้หาราคาสูงสุดตั้งแต่ซื้อ (แนะนำให้ใส่)
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT") or 3)  # เป้ากำไรขั้นแรก (%)
STOP_ATR = float(os.getenv("STOP_ATR") or 1.5)      # ตัดขาดทุน = ราคาซื้อ − STOP_ATR × ATR
TRAIL_ATR = float(os.getenv("TRAIL_ATR") or 2)      # ถึงเป้าแล้ว จุดขายตาม = ราคาสูงสุดตั้งแต่ซื้อ − TRAIL_ATR × ATR
TIERS = [5, 7, 10, 15, 20, 30, 50]                  # เป้าถัดไป (%) ที่บอทจะเสนอเมื่อยังขึ้นต่อ


def evaluate_hold(d, news=False):
    """ถือของอยู่: ถึงเป้าแรกแล้วยังขึ้นต่อ = ถือต่อและเลื่อนจุดขายตาม / แรงขึ้นหมดหรือหลุดจุดขายตาม = ขายได้"""
    e, px = ENTRY_PRICE, d["px"]
    highs, lows, closes = d["highs"][:], d["lows"][:], d["closes"][:]
    closes[-1] = px
    highs[-1], lows[-1] = max(highs[-1], px), min(lows[-1], px)
    atr, rsi = _atr(highs, lows, closes), _rsi(closes)
    sma20 = sum(closes[-20:]) / 20
    sma20_prev = sum(closes[-25:-5]) / 20              # SMA20 เมื่อ 5 วันก่อน ใช้ดูว่าเส้นยังชันขึ้นไหม
    sma50 = sum(closes[-50:]) / min(50, len(closes))

    since = highs[-1:]                                  # ไม่รู้วันที่ซื้อ ใช้ยอดของวันนี้
    if BUY_DATE:
        try:
            bd = datetime.strptime(BUY_DATE, "%Y-%m-%d").date()
            idx = next((i for i, dt in enumerate(d["dates"]) if dt >= bd), len(highs) - 1)
            since = highs[idx:]
        except ValueError:
            print(f"BUY_DATE ไม่ถูกรูปแบบ (ต้องเป็น YYYY-MM-DD): {BUY_DATE}")
    peak = max(since + [px, e])

    gain = (px - e) / e * 100
    peak_gain = (peak - e) / e * 100
    tp_price = e * (1 + TAKE_PROFIT / 100)
    stop = e - STOP_ATR * atr
    reached = peak_gain >= TAKE_PROFIT                  # เคยแตะเป้าแรกแล้ว
    # ถึงเป้าแล้ว: เลื่อนจุดขายตามราคาสูงสุด แต่ล็อกกำไรไว้อย่างน้อยครึ่งหนึ่งของเป้าแรก
    trail = max(peak - TRAIL_ATR * atr, e * (1 + TAKE_PROFIT / 200)) if reached else None
    uptrend = px > sma20 and sma20 > sma20_prev
    near_peak = px >= peak - atr
    overbought = rsi >= 80
    nxt = next((t for t in TIERS if t > max(gain, TAKE_PROFIT) + 0.5), TIERS[-1])
    nxt_price = e * (1 + nxt / 100)
    res = [h for h in (max(highs[-20:]), max(highs)) if h > px * 1.002]
    resistance = min(res) if res else None

    lv = dict(mode="hold", entry=e, gain=gain, peak=peak, peak_gain=peak_gain, tp_price=tp_price,
              stop=stop, trail=trail, atr=atr, rsi=rsi, sma20=sma20, sma50=sma50, uptrend=uptrend,
              near_peak=near_peak, overbought=overbought, nxt=nxt, nxt_price=nxt_price,
              resistance=resistance, news=news,
              levels_text=(f"ราคาซื้อ {e:.1f} / กำไรตอนนี้ {gain:+.1f}% / สูงสุดตั้งแต่ซื้อ {peak:.1f} ({peak_gain:+.1f}%) / "
                           f"เป้าแรก {tp_price:.1f} / ตัดขาดทุน {stop:.1f} / "
                           + (f"จุดขายตาม {trail:.1f} / " if trail else "")
                           + f"เป้าถัดไป +{nxt}% = {nxt_price:.1f} / ATR14 {atr:.1f} / RSI14 {rsi:.0f}"))

    def out(icon, title, why):
        return dict(icon=icon, title=title, why=why, plan=None, signal=icon in ("🟢", "🔴", "🔵"), **lv)

    if e <= 0:
        return out("⚠️", "ข้อมูลผิดปกติ", "ยังไม่ได้ใส่ราคาซื้อ")
    if not reached and px <= stop:
        return out("🔴", "ตัดขาดทุน", f"ราคาหลุดจุดตัดขาดทุน {r0(stop)}")
    if reached and px <= trail:
        return out("🟢", "ขายได้: หลุดจุดขายตาม", f"ราคาย่อจากยอด {r0(peak)} ลงมาหลุด {r0(trail)}")
    if gain >= TAKE_PROFIT:
        if overbought:
            return out("🟢", "ขายได้: ร้อนแรงเกินไป", f"RSI {rsi:.0f} สูงมาก มักพักตัวหรือย่อแรง")
        if uptrend and near_peak:
            return out("🔵", "ถึงเป้าแล้ว ยังขึ้นต่อ", f"ราคาเหนือ SMA20 ที่ยังชันขึ้น และใกล้ยอดใหม่")
        return out("🟢", "ขายได้: กำไรถึงเป้า แรงขึ้นเริ่มหมด",
                   "ราคาไม่ทำยอดใหม่" if not near_peak else "ราคาหลุด SMA20 หรือเส้นเริ่มแบน")
    if reached:
        return out("🟠", "กำไรลดลงจากยอด", f"เคยกำไร {peak_gain:+.1f}% ตอนนี้เหลือ {gain:+.1f}%")
    return out("⚪", "ถือต่อ", f"อีก {TAKE_PROFIT - gain:.1f}% ถึงเป้าแรก")


def format_hold(d, r):
    when = d["stamp"].astimezone(BKK).strftime("%H:%M")
    v, t, e = verdict(r), r["title"], r["entry"]
    lines = [v, f"ราคาตอนนี้ {r0(d['px'])} ({when} น.) | ซื้อที่ {r0(e)} | กำไร {r['gain']:+.1f}%", ""]
    nxt_line = f"🎯 เป้าถัดไป: +{r['nxt']}% = {r0(r['nxt_price'])}"
    if r["resistance"] and r["resistance"] < r["nxt_price"]:
        nxt_line += f" (มีแนวต้าน {r0(r['resistance'])} คั่นอยู่)"

    if v.startswith("🟢"):
        reason = {"ขายได้: หลุดจุดขายตาม": f"ราคาย่อจากยอด {r0(r['peak'])} ({r['peak_gain']:+.1f}%) ลงมาหลุดจุดขายตาม {r0(r['trail'])} แรงขึ้นจบรอบแล้ว",
                  "ขายได้: ร้อนแรงเกินไป": f"RSI {r['rsi']:.0f} สูงเกินไป ราคามักพักตัวหรือย่อแรงหลังจากนี้",
                  "ขายได้: กำไรถึงเป้า แรงขึ้นเริ่มหมด": f"กำไรถึงเป้า {TAKE_PROFIT:.0f}% แล้ว แต่{r['why']} โอกาสขึ้นต่อน้อยลง"}.get(t, r["why"])
        lines += [f"เหตุผล: {reason}", "", f"💰 ถ้าขายตอนนี้ได้กำไร {r['gain']:+.1f}%"]
        if "หลุดจุดขายตาม" in t:
            lines.append("• ขายเก็บกำไรที่เหลือ อย่ารอให้กำไรหายไปมากกว่านี้")
        else:
            lines.append(f"• ขายทั้งหมด หรือทยอยขายครึ่งหนึ่ง แล้วถือที่เหลือโดยตั้งขายที่ {r0(r['trail'])}")
    elif v.startswith("🔵"):
        lines += [f"เหตุผล: กำไรเกินเป้า {TAKE_PROFIT:.0f}% แล้ว และแนวโน้มยังขึ้น ({r['why']})",
                  "",
                  nxt_line,
                  f"🛡️ จุดขายตาม {r0(r['trail'])} (ล็อกกำไร {(r['trail'] - e) / e * 100:+.1f}%) ถ้าหลุดจะเปลี่ยนเป็น 🟢 ขายได้",
                  "",
                  "จะเปลี่ยนเป็น 🟢 ขายได้ ถ้า:",
                  f"• ราคาหลุด {r0(r['trail'])}",
                  f"• RSI ขึ้นถึง 80 (ตอนนี้ {r['rsi']:.0f})",
                  f"• ราคาหลุด SMA20 {r0(r['sma20'])} หรือย่อจากยอดเกิน {r0(r['atr'])} ดอลลาร์",
                  "",
                  "ไม่อยากเสี่ยงคืนกำไร: ขายครึ่งหนึ่งตอนนี้ แล้วปล่อยอีกครึ่งวิ่งตามเป้าถัดไป"]
    elif v.startswith("🟠"):
        lines += [f"เหตุผล: {r['why']} ราคาเริ่มย่อจากยอด {r0(r['peak'])}",
                  "",
                  f"🛡️ จุดขายตาม {r0(r['trail'])} ถ้าหลุดควรขายเก็บกำไรที่เหลือ",
                  f"• ถ้ากลับขึ้นไปเหนือ {r0(r['tp_price'])} (+{TAKE_PROFIT:.0f}%) จะประเมินถือต่อใหม่"]
    elif v.startswith("🔴"):
        lines += [f"เหตุผล: ราคาลงถึงจุดตัดขาดทุน {r0(r['stop'])} (ราคาซื้อ − {STOP_ATR:g}×ATR)",
                  f"ขาดทุนตอนนี้ {r['gain']:.1f}%",
                  "",
                  "• ตัดขาดทุนตามแผน ดีกว่าถือรอแล้วติดลึก",
                  "• ขายแล้วอย่าลืมลบ ENTRY_PRICE ให้บอทกลับไปหาจังหวะซื้อ"]
    elif v.startswith("⚪"):
        lines += [f"เหตุผล: กำไรยังไม่ถึงเป้า {TAKE_PROFIT:.0f}%",
                  "",
                  f"🎯 เป้าแรก {r0(r['tp_price'])} (+{TAKE_PROFIT:.0f}% อีก {r0(r['tp_price'] - d['px'])} ดอลลาร์)",
                  f"🛑 ตัดขาดทุน {r0(r['stop'])} ({(r['stop'] - e) / e * 100:.1f}%)"]
    else:
        lines.append(f"เหตุผล: {r['why']}")
    if r["news"]:
        lines += ["", f"📰 ใกล้ข่าวสำคัญ ({r['news']}) ราคาอาจวิ่งแรง ถ้ามีกำไรแล้วพิจารณาขายบางส่วนก่อนข่าว"]
    trend = "ขาขึ้น" if r["uptrend"] else ("ขาลง" if r["sma20"] < r["sma50"] else "เริ่มอ่อนแรง")
    lines += ["",
              f"ภาพรวม: สูงสุดตั้งแต่ซื้อ {r0(r['peak'])} ({r['peak_gain']:+.1f}%) | RSI {r['rsi']:.0f} | เทรนด์ {trend}",
              "แจ้งสถานการณ์ตามกฎ ไม่ใช่คำแนะนำการลงทุน"]
    if not BUY_DATE:
        lines.append("ℹ️ ใส่ BUY_DATE (วันที่ซื้อ) ใน Variables ให้บอทจำยอดสูงสุดตั้งแต่ซื้อได้แม่นขึ้น")
    return "\n".join(lines)


def format_any(d, r):
    if r.get("mode") == "hold":
        return format_hold(d, r)
    return format_dip(d, r) if r.get("mode") == "dip3" else format_message(d, r)


# ---------------------------------------------------------------- senders
def post(url, data, as_json=False):
    if as_json:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url, urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        # แสดงเหตุผลที่ปลายทางตอบกลับ (ไม่มี token อยู่ในข้อความนี้)
        body = exc.read().decode("utf-8", "replace")
        print(f"ส่งข้อความไม่สำเร็จ HTTP {exc.code}: {body}")
        raise


def send(text):
    sent = False
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        # ใส่ได้หลายปลายทาง คั่นด้วยจุลภาค เช่น 5888071832,-1001234567890
        errors = []
        for cid in [c.strip() for c in chat.split(",") if c.strip()]:
            try:
                post(f"https://api.telegram.org/bot{tok}/sendMessage", {"chat_id": cid, "text": text})
                sent = True
            except Exception as exc:  # noqa: BLE001  ปลายทางหนึ่งพัง ไม่ให้กระทบปลายทางอื่น
                errors.append(exc)
                print(f"ส่งไปที่ {cid} ไม่สำเร็จ: {exc}")
        if errors and not sent:
            raise errors[0]
    hook = os.getenv("SLACK_WEBHOOK_URL")
    if hook:
        post(hook, {"text": text}, as_json=True)
        sent = True
    if not sent:
        print("ยังไม่ได้ตั้งค่า TELEGRAM_* หรือ SLACK_WEBHOOK_URL – แสดงผลในหน้าจอแทน\n")
    print(text)


def main():
    now = datetime.now(timezone.utc)
    if market_closed(now):
        print("ตลาดปิด ข้ามรอบนี้")
        return
    d, last_exc = None, None
    for attempt in range(3):  # ลองสูงสุด 3 ครั้ง เผื่อ Yahoo จำกัดจำนวนครั้ง
        try:
            d = to_spot(fetch_data())
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"ดึงราคาไม่สำเร็จ ครั้งที่ {attempt + 1}: {exc}")
            if attempt < 2:
                time.sleep(20 * (attempt + 1))
    if d is None:
        send(f"⚠️ ดึงราคาทองไม่สำเร็จรอบนี้ ({last_exc}) จะลองใหม่รอบหน้า")
        return
    age_h = (now - d["stamp"].astimezone(timezone.utc)).total_seconds() / 3600
    if age_h > 2:
        print(f"ข้อมูลเก่า {age_h:.1f} ชม. (อาจเป็นช่วงพักตลาด) ข้ามรอบนี้")
        return
    if ENTRY_PRICE > 0:
        r = evaluate_hold(d, news=news_soon(now))
    elif STRATEGY == "pivot":
        r = evaluate(d, news=news_soon(now))
    else:
        r = evaluate_dip(d, news=news_soon(now))
    th_hour = now.astimezone(BKK).hour
    # รอบตามตาราง = GitHub schedule หรือสั่งจาก cron-job.org ด้วย mode=auto / กดรันเองได้ข้อความเต็มเสมอ
    scheduled = os.getenv("GITHUB_EVENT_NAME") == "schedule" or os.getenv("RUN_MODE") == "auto"
    only_signals = ONLY_SIGNALS or (scheduled and th_hour not in FULL_HOURS)
    if only_signals and not r["signal"]:
        print("ไม่มีสัญญาณ ไม่ส่งข้อความ\n" + format_any(d, r))
        return
    msg = format_any(d, r)
    ai = ai_analysis(d, r)
    if ai:
        msg += "\n\n🤖 AI วิเคราะห์ (Gemini)\n" + ai
    send(msg)


if __name__ == "__main__":
    sys.exit(main())
