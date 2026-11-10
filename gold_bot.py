"""
XAU Alert Bot – เช็คจังหวะเข้าซื้อทองทุก 1 ชม. แล้วส่งข้อความเข้า Telegram (หรือ Slack)
ใช้ Pivot Point จากแท่งวันก่อนหน้า คำนวณใหม่ทุกครั้ง ไม่ต้องกำหนดราคาล่วงหน้า
แจ้งสถานการณ์เท่านั้น ไม่ใช่คำแนะนำการลงทุน
"""
import os
import sys
import json
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
    daily = tk.history(period="10d", interval="1d").dropna()
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
    }


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


def format_message(d, r):
    """ข้อความแบบอ่านง่าย ปัดเป็นจำนวนเต็ม ไม่ใช้ศัพท์เทคนิค"""
    when = d["stamp"].astimezone(BKK).strftime("%H:%M")
    head = {
        "สัญญาณซื้อ A: ย่อซื้อ": "🟢 จังหวะซื้อ (ย่อซื้อ)",
        "สัญญาณซื้อ B: เบรกขึ้น": "🟢 จังหวะซื้อ (เบรกขึ้น)",
    }.get(r["title"], f"{r['icon']} {r['title'].split(':')[0] if r['icon']=='⚪' else r['title']}")
    if r["icon"] == "⚪":
        head = "⚪ ยังไม่ใช่จังหวะ"
    lines = [head, f"ราคาตอนนี้ {r0(d['px'])} ({when} น.)"]
    p = r.get("plan")
    if r["icon"] == "🟢" and p:
        lines += [f"✅ ซื้อที่ {r0(p['entry'])}",
                  f"🛑 ตัดขาดทุน {r0(p['stop'])}",
                  f"🎯 ขายทำกำไร {r0(p['t1'])}" + (f" / {r0(p['t2'])}" if p["t2"] else ""),
                  f"⚖️ คุ้มเสี่ยง 1 : {p['rr']:.1f}",
                  "เปิดกราฟ 15 นาทีดูว่าราคาเด้งจริงก่อนเข้า"]
    elif r["icon"] == "⚪":
        lines += [f"👉 รอซื้อถ้าราคาลงไปแถว {r0(r['S1'])} แล้วเด้งขึ้น",
                  f"👉 หรือรอราคาทะลุ {r0(r['trig'])}"]
    else:
        easy = {
            "ห้ามเข้า: ช่วงข่าวสำคัญ": "ใกล้เวลาข่าวสำคัญ ราคามักวิ่งแรง รอข่าวออกก่อน",
            "งดซื้อ: ราคาหลุด S2": f"ราคาลงต่ำกว่า {r0(r['S2'])} แรงขายแรง รอให้นิ่งก่อน",
            "ห้ามเข้า: วันนี้ผันผวนผิดปกติ": "วันนี้ราคาแกว่งแรงผิดปกติ แนวรับแนวต้านใช้ไม่ค่อยได้",
            "เข้าเงื่อนไข A แต่ไม่คุ้มเสี่ยง": "ราคาเด้งมาไกลแล้ว ซื้อตอนนี้ไม่คุ้ม รอย่อก่อน",
            "เข้าเงื่อนไข B แต่ไม่คุ้มเสี่ยง": "ทะลุขึ้นแล้วแต่จุดตัดขาดทุนไกลเกินไป ไม่คุ้ม",
        }
        lines.append(easy.get(r["title"], r["why"]))
    lines.append("แจ้งสถานการณ์ ไม่ใช่คำแนะนำการลงทุน")
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
    try:
        d = fetch_data()
    except Exception as exc:  # noqa: BLE001
        send(f"⚠️ ดึงราคาทองไม่สำเร็จรอบนี้ ({exc}) จะลองใหม่รอบหน้า")
        return
    age_h = (now - d["stamp"].astimezone(timezone.utc)).total_seconds() / 3600
    if age_h > 2:
        print(f"ข้อมูลเก่า {age_h:.1f} ชม. (อาจเป็นช่วงพักตลาด) ข้ามรอบนี้")
        return
    r = evaluate(d, news=news_soon(now))
    if ONLY_SIGNALS and not r["signal"]:
        print("ไม่มีสัญญาณ ไม่ส่งข้อความ\n" + format_message(d, r))
        return
    send(format_message(d, r))


if __name__ == "__main__":
    sys.exit(main())
