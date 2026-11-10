"""
XAU Alert Bot – เช็คจังหวะเข้าซื้อทองทุก 1 ชม. แล้วส่งข้อความเข้า Telegram (หรือ Slack)
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
    }


# ---------------------------------------------------------------- AI (Gemini)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")


def ai_analysis(d, r):
    """ให้ Gemini อธิบายสัญญาณ สรุปข่าว และประเมินเทรนด์ ถ้าไม่มี key หรือเรียกไม่สำเร็จ จะคืน None"""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    closes = d["closes"]
    sma20 = sum(closes[-20:]) / min(20, len(closes))
    sma50 = sum(closes[-50:]) / min(50, len(closes))
    prompt = f"""คุณเป็นผู้ช่วยวิเคราะห์ทองคำ XAU/USD เขียนภาษาไทยสั้น กระชับ อ่านง่ายบนมือถือ

ข้อมูลจากบอท (ราคาทองฟิวเจอร์ส GC=F):
- ราคาตอนนี้ {d['px']:.1f} เวลา {d['stamp'].astimezone(BKK):%Y-%m-%d %H:%M} (เวลาไทย)
- เมื่อวาน High {d['yH']:.1f} Low {d['yL']:.1f} Close {d['yC']:.1f}
- Pivot P {r['P']:.1f} / S1 {r['S1']:.1f} / S2 {r['S2']:.1f} / R1 {r['R1']:.1f} / จุดเบรก {r['trig']:.1f}
- ผลตามกฎของบอท: {r['icon']} {r['title']} – {r['why']}
- SMA20 {sma20:.1f} / SMA50 {sma50:.1f}
- ราคาปิดรายวันย้อนหลัง (เก่า→ใหม่): {closes}

ระดับที่ใช้มี 4 ระดับ: 🟢 เข้าได้ / ⚪ ยังไม่ใช่จังหวะ / 🟠 ไม่ควรเข้า / 🔴 ห้ามซื้อ
ผลของบอทตอนนี้คือ: {verdict(r)}

ตอบเป็น 4 หัวข้อ หัวข้อละไม่เกิน 2 บรรทัด ใช้รูปแบบนี้เท่านั้น:
🧭 ความเห็น AI: เลือก 1 ระดับจาก 4 ระดับข้างบน แล้วบอกเหตุผลสั้นๆ
   กฎ: ระดับที่เลือกต้องเท่ากับหรือเข้มกว่าผลของบอทเท่านั้น (ลำดับจากหลวมไปเข้ม: 🟢 → ⚪ → 🟠 → 🔴) ห้ามเลือกระดับที่หลวมกว่าบอท เช่น บอทบอก ⚪ ห้ามตอบ 🟢
📊 สัญญาณ: อธิบายผลของบอทแบบภาษาคน และความเสี่ยงที่ต้องระวัง
📰 ข่าว: ค้นข่าวล่าสุด 24 ชม. ที่กระทบทอง (Fed, ดอลลาร์, ข้อมูลเศรษฐกิจสหรัฐฯ, ภูมิรัฐศาสตร์) แล้วสรุปผลต่อราคา ถ้ามีข่าวสำคัญกำลังจะออกให้บอกเวลา (เวลาไทย)
📈 เทรนด์: ประเมินเทรนด์ระยะสั้นและกลางจากราคาย้อนหลังและ SMA ว่าเทรนด์หนุนหรือขัดกับการซื้อ

ห้ามสั่งให้ซื้อหรือขายแบบยืนยัน ให้พูดเป็นสถานการณ์และความเสี่ยงเท่านั้น ไม่ต้องมีคำนำหรือคำลงท้าย"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    base = {"contents": [{"parts": [{"text": prompt}]}]}
    # ลองแบบค้นข่าวด้วย Google Search ก่อน ถ้าไม่ได้ค่อยลองแบบไม่ค้น
    for body in ({**base, "tools": [{"google_search": {}}]}, base):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text[:1500]
        except urllib.error.HTTPError as exc:
            print(f"Gemini ตอบ HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
        except Exception as exc:  # noqa: BLE001
            print(f"เรียก Gemini ไม่สำเร็จ: {exc}")
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
            lines.append(f"• ย่อซื้อ: แตะ {r0(S1)} แล้ว รอเด้งให้ถึง {r0(r['tL'] + 0.2 * R)}")
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
        post(f"https://api.telegram.org/bot{tok}/sendMessage", {"chat_id": chat, "text": text})
        sent = True
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
            d = fetch_data()
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
    r = evaluate(d, news=news_soon(now))
    if ONLY_SIGNALS and not r["signal"]:
        print("ไม่มีสัญญาณ ไม่ส่งข้อความ\n" + format_message(d, r))
        return
    msg = format_message(d, r)
    ai = ai_analysis(d, r)
    if ai:
        msg += "\n\n🤖 AI วิเคราะห์ (Gemini)\n" + ai
    send(msg)


if __name__ == "__main__":
    sys.exit(main())
