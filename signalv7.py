
import os
import requests
import asyncio
import sqlite3
import time
import concurrent.futures
import threading
import math

from datetime import datetime, timedelta
from flask import Flask

# ======================================
# WEB SERVICE FOR RENDER FREE
# ======================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Ariyan TradeX Bot is running ✅"

@app.route("/health")
def health():
    return "OK"

# ======================================
# TELEGRAM SETTINGS
# ======================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

if not BOT_TOKEN or not CHAT_ID:
    print("WARNING: BOT_TOKEN or CHAT_ID missing. Add them in Render Environment Variables.")

FOOTER = "\n\n━━━━━━━━━━━━\n🤖Developed by @Devilback12"

# ======================================
# SETTINGS
# ======================================

PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT"
]

TIMEFRAME = "1m"
LIMIT = 150
SIGNAL_COOLDOWN = 90
MAX_THREADS = 30

# AI WEIGHTED REAL MODE SETTINGS
ALWAYS_SIGNAL_MODE = True       # True = real market data থেকে best pair বাছাই করে signal দিবে
MIN_AI_SCORE = 45               # score এর নিচে হলে still best signal দিবে, কিন্তু Low confidence দেখাবে
SCAN_INTERVAL = 60              # প্রতি 60 sec scan
FORCE_BEST_SIGNAL_EVERY = 180   # 3 মিনিটেও signal না হলে best real signal পাঠাবে
AVOID_SAME_DIRECTION = False    # always mode এ same direction block করা হবে না

executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_THREADS
)

# ======================================
# DATABASE
# ======================================

conn = sqlite3.connect(
    "signals.db",
    check_same_thread=False
)

conn.execute("PRAGMA journal_mode=WAL")
conn.commit()

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT,
    signal TEXT,
    confidence INTEGER,
    result TEXT,
    time TEXT,
    message_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY,
    total_profit REAL DEFAULT 0,
    total_win INTEGER DEFAULT 0,
    total_loss INTEGER DEFAULT 0
)
""")

cursor.execute(
    "INSERT OR IGNORE INTO stats (id) VALUES (1)"
)

conn.commit()

# ======================================
# SAFE TELEGRAM MESSAGE
# ======================================

def send_telegram_message(text, reply_to_message_id=None):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured.")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text + FOOTER,
        "disable_web_page_preview": True
    }

    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, timeout=30)
            data = r.json()
            if data.get("ok"):
                return data.get("result")
            print("Telegram API Error:", data)
            time.sleep(5)
        except Exception as e:
            print(f"Telegram Error: {e}")
            time.sleep(5)

    return None

async def safe_send_message(text):
    return await asyncio.to_thread(send_telegram_message, text)

# ======================================
# MARKET DATA
# ======================================

def get_market_data(symbol):
    try:
        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol={symbol}"
            f"&interval={TIMEFRAME}"
            f"&limit={LIMIT}"
        )

        response = requests.get(url, timeout=20)
        data = response.json()

        closes = []
        highs = []
        lows = []
        opens = []
        volumes = []

        for candle in data:
            opens.append(float(candle[1]))
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))
            volumes.append(float(candle[5]))

        return opens, highs, lows, closes, volumes

    except Exception as e:
        print(f"Market Data Error: {e}")
        return [], [], [], [], []

# ======================================
# INDICATORS
# ======================================

def ema(prices, period):
    if len(prices) < period:
        return 0

    multiplier = 2 / (period + 1)
    ema_value = sum(prices[:period]) / period

    for price in prices[period:]:
        ema_value = ((price - ema_value) * multiplier) + ema_value

    return ema_value

def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i - 1]

        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0.01
    avg_loss = sum(losses) / period if losses else 0.01

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def macd(prices):
    return ema(prices, 12) - ema(prices, 26)

def stochastic(prices):
    if len(prices) < 14:
        return 50

    highest = max(prices[-14:])
    lowest = min(prices[-14:])
    current = prices[-1]

    if highest == lowest:
        return 50

    return ((current - lowest) / (highest - lowest)) * 100

def momentum(prices):
    if len(prices) < 10:
        return 0
    return prices[-1] - prices[-10]

def support_resistance(highs, lows):
    if len(highs) < 20:
        return 0, 0

    resistance = max(highs[-20:])
    support = min(lows[-20:])
    return support, resistance

def candle_pattern(opens, closes, highs, lows):
    if not opens:
        return "NONE"

    last_open = opens[-1]
    last_close = closes[-1]
    last_high = highs[-1]
    last_low = lows[-1]

    body = abs(last_close - last_open)
    wick = last_high - last_low

    if last_close > last_open and wick > body * 2:
        return "BULLISH"

    if last_open > last_close and wick > body * 2:
        return "BEARISH"

    return "NONE"

def volume_strength(volumes):
    if len(volumes) < 21:
        return 0
    avg_vol = sum(volumes[-21:-1]) / 20
    if avg_vol <= 0:
        return 0
    return ((volumes[-1] - avg_vol) / avg_vol) * 100

def normalize(value, max_abs):
    if max_abs == 0:
        return 0
    return max(-1, min(1, value / max_abs))

# ======================================
# AI WEIGHTED SIGNAL ENGINE
# ======================================

def ai_weighted_score(pair):
    opens, highs, lows, closes, volumes = get_market_data(pair)

    if len(closes) < 50:
        return None

    current_price = closes[-1]
    rsi_value = rsi(closes)
    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)
    macd_value = macd(closes)
    stochastic_value = stochastic(closes)
    momentum_value = momentum(closes)
    support, resistance = support_resistance(highs, lows)
    pattern = candle_pattern(opens, closes, highs, lows)
    vol_strength = volume_strength(volumes)

    trend_strength = abs(ema_fast - ema_slow)
    trend_percent = (trend_strength / current_price) * 100 if current_price else 0

    buy_score = 0
    sell_score = 0
    reasons = []

    # EMA trend weight
    if ema_fast > ema_slow:
        buy_score += 22
        reasons.append("EMA trend bullish")
    elif ema_fast < ema_slow:
        sell_score += 22
        reasons.append("EMA trend bearish")

    # RSI weight
    if rsi_value < 35:
        buy_score += 18
        reasons.append("RSI oversold")
    elif rsi_value > 65:
        sell_score += 18
        reasons.append("RSI overbought")
    elif rsi_value > 50:
        buy_score += 8
    elif rsi_value < 50:
        sell_score += 8

    # MACD weight
    if macd_value > 0:
        buy_score += 18
        reasons.append("MACD positive")
    elif macd_value < 0:
        sell_score += 18
        reasons.append("MACD negative")

    # Stochastic weight
    if stochastic_value < 40:
        buy_score += 14
        reasons.append("Stochastic buy zone")
    elif stochastic_value > 60:
        sell_score += 14
        reasons.append("Stochastic sell zone")

    # Momentum weight
    if momentum_value > 0:
        buy_score += 14
        reasons.append("Momentum up")
    elif momentum_value < 0:
        sell_score += 14
        reasons.append("Momentum down")

    # Candle pattern weight
    if pattern == "BULLISH":
        buy_score += 10
        reasons.append("Bullish candle")
    elif pattern == "BEARISH":
        sell_score += 10
        reasons.append("Bearish candle")

    # Volume confirms current side
    if vol_strength > 10:
        if buy_score >= sell_score:
            buy_score += 8
            reasons.append("Volume confirms buy")
        else:
            sell_score += 8
            reasons.append("Volume confirms sell")

    # Low trend penalty, but no blocking in always mode
    if trend_percent < 0.02:
        buy_score -= 8
        sell_score -= 8
        reasons.append("Low trend market")

    signal = "BUY" if buy_score >= sell_score else "SELL"
    raw_score = buy_score if signal == "BUY" else sell_score

    # Confidence range 45-95
    confidence = int(max(45, min(95, raw_score)))

    # Quality label
    if confidence >= 75:
        quality = "HIGH"
    elif confidence >= 60:
        quality = "MEDIUM"
    else:
        quality = "LOW"

    return {
        "pair": pair,
        "signal": signal,
        "confidence": confidence,
        "quality": quality,
        "ai_score": raw_score,
        "buy_score": round(buy_score, 2),
        "sell_score": round(sell_score, 2),
        "rsi": round(rsi_value, 2),
        "stochastic": round(stochastic_value, 2),
        "momentum": round(momentum_value, 6),
        "pattern": pattern,
        "support": support,
        "resistance": resistance,
        "price": current_price,
        "trend_percent": round(trend_percent, 4),
        "volume_strength": round(vol_strength, 2),
        "reasons": reasons[:5]
    }

def analyze_pair(pair):
    # Original function name kept, now powered by AI weighted real market scoring.
    return ai_weighted_score(pair)

def get_best_real_signal():
    results = []

    for pair in PAIRS:
        data = analyze_pair(pair)
        if data:
            results.append(data)
            print(
                f"{pair} | BUY:{data['buy_score']} SELL:{data['sell_score']} "
                f"SIGNAL:{data['signal']} CONF:{data['confidence']} "
                f"TREND:{data['trend_percent']}%"
            )
        else:
            print(f"No market data: {pair}")

    if not results:
        return None

    # best confidence + stronger score gap
    results.sort(
        key=lambda x: (x["confidence"], abs(x["buy_score"] - x["sell_score"])),
        reverse=True
    )

    return results[0]

# ======================================
# MARTINGALE
# ======================================

martingale_step = 0

def martingale_amount(base_amount=1):
    global martingale_step
    return base_amount * (2 ** martingale_step)

# ======================================
# SAVE / UPDATE SIGNAL
# ======================================

def save_signal(pair, signal, confidence):
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO signals (
            pair,
            signal,
            confidence,
            result,
            time
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (pair, signal, confidence, "PENDING", str(datetime.now()))
    )

    conn.commit()
    return cursor.lastrowid

def update_message_id(signal_id, message_id):
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE signals
        SET message_id=?
        WHERE id=?
        """,
        (message_id, signal_id)
    )

    conn.commit()

# ======================================
# PROFIT SYSTEM
# ======================================

def update_profit(result):
    global martingale_step
    cursor = conn.cursor()

    if result == "WIN":
        profit = 0.85
        martingale_step = 0

        cursor.execute(
            """
            UPDATE stats
            SET
                total_profit = total_profit + ?,
                total_win = total_win + 1
            WHERE id = 1
            """,
            (profit,)
        )

    else:
        profit = -1
        martingale_step += 1

        cursor.execute(
            """
            UPDATE stats
            SET
                total_profit = total_profit + ?,
                total_loss = total_loss + 1
            WHERE id = 1
            """,
            (profit,)
        )

    conn.commit()

def get_profit_stats():
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            total_profit,
            total_win,
            total_loss
        FROM stats
        WHERE id = 1
        """
    )

    row = cursor.fetchone()

    return {
        "profit": round(row[0], 2),
        "wins": row[1],
        "losses": row[2]
    }

def get_win_rate():
    stats = get_profit_stats()
    total = stats['wins'] + stats['losses']

    if total == 0:
        return 0

    return round((stats['wins'] / total) * 100, 2)

def dashboard():
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM signals")
    total = cursor.fetchone()[0]

    stats = get_profit_stats()
    win_rate = get_win_rate()

    return f"""

📊 DASHBOARD

📡 Total Signals: {total}

🏆 Wins: {stats['wins']}
❌ Losses: {stats['losses']}

💰 Total Profit: ${stats['profit']}

🎯 Win Rate: {win_rate}%

🔥 Martingale Step: {martingale_step}

"""

# ======================================
# RESULT CHECKER
# ======================================

def check_signal_result(signal_id, pair, signal, entry_price, message_id):
    try:
        now = datetime.now()
        wait_seconds = 60 - now.second
        time.sleep(wait_seconds + 2)

        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol={pair}"
            "&interval=1m&limit=1"
        )

        response = requests.get(url, timeout=20)
        data = response.json()
        close_price = float(data[0][4])

        result = "LOSS"

        if signal == "BUY" and close_price > entry_price:
            result = "WIN"
        elif signal == "SELL" and close_price < entry_price:
            result = "WIN"

        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE signals
            SET result=?
            WHERE id=?
            """,
            (result, signal_id)
        )

        conn.commit()
        update_profit(result)
        stats = get_profit_stats()

        result_message = f"""

📢 SIGNAL RESULT

🆔 Signal ID: {signal_id}

💹 Pair: {pair}

📈 Signal: {signal}

🎯 Result: {result}

📍 Entry Price: {entry_price}
📍 Close Price: {close_price}

💰 Total Profit: ${stats['profit']}

🏆 Wins: {stats['wins']}
❌ Losses: {stats['losses']}

"""

        send_telegram_message(result_message, reply_to_message_id=message_id)
        print(f"Result Sent: {signal_id}")

    except Exception as e:
        print(f"Result Error: {e}")

# ======================================
# SEND SIGNAL
# ======================================

async def send_signal(data, signal_id):
    amount = martingale_amount()

    entry_time = datetime.now() + timedelta(minutes=1)
    formatted_time = entry_time.strftime("%I:%M %p")
    candle_time = entry_time.strftime("%H:%M")

    stats = get_profit_stats()
    reasons_text = "\n".join([f"✅ {r}" for r in data.get("reasons", [])])

    message = f"""

📊 AI WEIGHTED REAL QUOTEX SIGNAL

🆔 Signal ID: {signal_id}

💹 Pair: {data['pair']}

🚀 Signal: {data['signal']}

🤖 AI Confidence: {data['confidence']}%
📌 Signal Quality: {data.get('quality', 'MEDIUM')}

📈 Buy Score: {data.get('buy_score')}
📉 Sell Score: {data.get('sell_score')}

📈 RSI: {data['rsi']}
📉 Stochastic: {data['stochastic']}
⚡ Momentum: {data['momentum']}

🕯 Pattern: {data['pattern']}

🟢 Support: {round(data['support'], 2)}
🔴 Resistance: {round(data['resistance'], 2)}

📊 Trend Strength: {data.get('trend_percent')}%
📦 Volume Strength: {data.get('volume_strength')}%

💰 Martingale Amount: ${amount}

📊 Total Profit: ${stats['profit']}

🏆 Wins: {stats['wins']}
❌ Losses: {stats['losses']}

⏰ Timeframe: 1 Minute

🕒 Entry Time: {formatted_time}

🕯 Entry Candle: {candle_time} Candle

🔎 AI Reasons:
{reasons_text}

⚠️ Real market signal. No signal is 100% guaranteed.

"""

    sent_message = await safe_send_message(message)

    if sent_message is None:
        return

    message_id = sent_message.get("message_id")
    update_message_id(signal_id, message_id)

    executor.submit(
        check_signal_result,
        signal_id,
        data['pair'],
        data['signal'],
        data['price'],
        message_id
    )

# ======================================
# STARTUP MESSAGE
# ======================================

async def startup_message():
    message = """

✅ ADVANCED QUOTEX BOT RUNNING

🤖 Bot Status: ACTIVE

📡 Market Scanner: ENABLED

📊 Multi Pair Scan: RUNNING

🔥 AI WEIGHTED REAL SIGNAL ENGINE ACTIVE

♻️ Always Signal Mode: ON

"""

    await safe_send_message(message)

# ======================================
# MAIN LOOP
# ======================================

async def main():
    await startup_message()

    last_signal_time = {}
    last_signal_direction = {}
    last_any_signal_time = 0

    while True:
        try:
            current_time = time.time()

            if ALWAYS_SIGNAL_MODE:
                result = get_best_real_signal()

                if result is None:
                    print("No real market data found.")
                else:
                    pair = result["pair"]
                    last_time = last_signal_time.get(pair, 0)
                    last_direction = last_signal_direction.get(pair)

                    cooldown_ok = current_time - last_time >= SIGNAL_COOLDOWN
                    forced_ok = current_time - last_any_signal_time >= FORCE_BEST_SIGNAL_EVERY
                    direction_ok = True if not AVOID_SAME_DIRECTION else result["signal"] != last_direction

                    if (cooldown_ok and direction_ok) or forced_ok:
                        signal_id = save_signal(
                            pair,
                            result['signal'],
                            result['confidence']
                        )

                        await send_signal(result, signal_id)

                        last_signal_time[pair] = current_time
                        last_signal_direction[pair] = result['signal']
                        last_any_signal_time = current_time

                        print(f"AI Real Signal Sent: {pair} {result['signal']} {result['confidence']}%")
                    else:
                        print(f"Cooldown active. Best real signal skipped: {pair}")

            else:
                for pair in PAIRS:
                    result = analyze_pair(pair)

                    if result is None:
                        print(f"No signal: {pair}")
                        continue

                    current_time = time.time()
                    last_time = last_signal_time.get(pair, 0)
                    last_direction = last_signal_direction.get(pair)

                    if (
                        current_time - last_time >= SIGNAL_COOLDOWN
                        and result['signal'] != last_direction
                    ):
                        signal_id = save_signal(
                            pair,
                            result['signal'],
                            result['confidence']
                        )

                        await send_signal(result, signal_id)
                        last_signal_time[pair] = current_time
                        last_signal_direction[pair] = result['signal']
                        print(f"Signal Sent: {pair} {result['signal']}")
                    else:
                        print(f"Skipped: {pair}")

            print(dashboard())

        except Exception as e:
            print(f"Main Loop Error: {e}")

        await asyncio.sleep(SCAN_INTERVAL)

# ======================================
# START BOT + WEB SERVER
# ======================================

def run_bot():
    asyncio.run(main())

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
