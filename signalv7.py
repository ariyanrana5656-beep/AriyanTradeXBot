import os
import requests
import asyncio
import sqlite3
import time
import threading
import concurrent.futures

from datetime import datetime, timedelta

from telegram import Bot
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from flask import Flask

# ======================================
# TELEGRAM SETTINGS
# ======================================

# Set these in Render > Environment. Do not hardcode your private token in GitHub.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

FOOTER = "\n\n🤖Developed by @Devilback12"

request = HTTPXRequest(
    connect_timeout=60,
    read_timeout=60,
    write_timeout=60,
    pool_timeout=60
)

bot = Bot(
    token=BOT_TOKEN,
    request=request
) if BOT_TOKEN else None

def with_footer(text):
    text = str(text).rstrip()
    if "🤖Developed by @Devilback12" in text:
        return text
    return text + FOOTER

# ======================================
# RENDER WEB SERVICE KEEP-ALIVE SERVER
# ======================================

app = Flask(__name__)
bot_started = False

@app.route("/")
def home():
    return "Advanced Quotex Signal Bot is running ✅"

@app.route("/health")
def health():
    return "OK", 200

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

async def safe_send_message(text):

    if not bot or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID. Set them in Render Environment.")
        return None

    text = with_footer(text)

    retries = 5

    for attempt in range(retries):

        try:

            sent = await bot.send_message(
                chat_id=CHAT_ID,
                text=text
            )

            return sent

        except NetworkError as e:

            print(f"Telegram Network Error: {e}")
            await asyncio.sleep(5)

        except Exception as e:

            print(f"Telegram Error: {e}")
            await asyncio.sleep(5)

    return None

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

        for candle in data:

            opens.append(float(candle[1]))
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))

        return opens, highs, lows, closes

    except Exception as e:

        print(f"Market Data Error: {e}")
        return [], [], [], []

# ======================================
# EMA
# ======================================


def ema(prices, period):

    if len(prices) < period:
        return 0

    multiplier = 2 / (period + 1)

    ema_value = sum(prices[:period]) / period

    for price in prices[period:]:

        ema_value = (
            (price - ema_value)
            * multiplier
        ) + ema_value

    return ema_value

# ======================================
# RSI
# ======================================


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

# ======================================
# MACD
# ======================================


def macd(prices):
    return ema(prices, 12) - ema(prices, 26)

# ======================================
# STOCHASTIC
# ======================================


def stochastic(prices):

    if len(prices) < 14:
        return 50

    highest = max(prices[-14:])
    lowest = min(prices[-14:])

    current = prices[-1]

    if highest == lowest:
        return 50

    return (
        (current - lowest)
        /
        (highest - lowest)
    ) * 100

# ======================================
# MOMENTUM
# ======================================


def momentum(prices):

    if len(prices) < 10:
        return 0

    return prices[-1] - prices[-10]

# ======================================
# SUPPORT / RESISTANCE
# ======================================


def support_resistance(highs, lows):

    if len(highs) < 20:
        return 0, 0

    resistance = max(highs[-20:])
    support = min(lows[-20:])

    return support, resistance

# ======================================
# CANDLE PATTERN
# ======================================


def candle_pattern(opens, closes, highs, lows):

    if not opens:
        return "NONE"

    last_open = opens[-1]
    last_close = closes[-1]

    last_high = highs[-1]
    last_low = lows[-1]

    body = abs(last_close - last_open)
    wick = last_high - last_low

    if (
        last_close > last_open
        and wick > body * 2
    ):
        return "BULLISH"

    if (
        last_open > last_close
        and wick > body * 2
    ):
        return "BEARISH"

    return "NONE"

# ======================================
# MARTINGALE
# ======================================

martingale_step = 0


def martingale_amount(base_amount=1):

    global martingale_step

    return base_amount * (
        2 ** martingale_step
    )

# ======================================
# SAVE SIGNAL
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
        (
            pair,
            signal,
            confidence,
            "PENDING",
            str(datetime.now())
        )
    )

    conn.commit()

    return cursor.lastrowid

# ======================================
# UPDATE MESSAGE ID
# ======================================


def update_message_id(signal_id, message_id):

    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE signals
        SET message_id=?
        WHERE id=?
        """,
        (
            message_id,
            signal_id
        )
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

# ======================================
# GET STATS
# ======================================


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

# ======================================
# WIN RATE
# ======================================


def get_win_rate():

    stats = get_profit_stats()

    total = (
        stats['wins']
        + stats['losses']
    )

    if total == 0:
        return 0

    return round(
        (stats['wins'] / total) * 100,
        2
    )

# ======================================
# DASHBOARD
# ======================================


def dashboard():

    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM signals"
    )

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
# ANALYZE MARKET
# ======================================


def analyze_pair(pair):

    opens, highs, lows, closes = get_market_data(pair)

    if len(closes) < 50:
        return None

    current_price = closes[-1]

    rsi_value = rsi(closes)

    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)

    macd_value = macd(closes)

    stochastic_value = stochastic(closes)

    momentum_value = momentum(closes)

    support, resistance = support_resistance(
        highs,
        lows
    )

    pattern = candle_pattern(
        opens,
        closes,
        highs,
        lows
    )

    signal = None
    confidence = 0

    trend_strength = abs(
        ema_fast - ema_slow
    )

    # percentage based filter
    trend_percent = (
        trend_strength / current_price
    ) * 100

    if trend_percent < 0.05:
        return None

    buy_conditions = 0
    sell_conditions = 0

    # BUY CONDITIONS

    if ema_fast > ema_slow:
        buy_conditions += 1

    if rsi_value < 35:
        buy_conditions += 1

    if macd_value > 0:
        buy_conditions += 1

    if stochastic_value < 40:
        buy_conditions += 1

    if momentum_value > 0:
        buy_conditions += 1

    # SELL CONDITIONS

    if ema_fast < ema_slow:
        sell_conditions += 1

    if rsi_value > 65:
        sell_conditions += 1

    if macd_value < 0:
        sell_conditions += 1

    if stochastic_value > 60:
        sell_conditions += 1

    if momentum_value < 0:
        sell_conditions += 1

    # candle confirmation

    if pattern == "BULLISH":
        buy_conditions += 1

    if pattern == "BEARISH":
        sell_conditions += 1

    # SIGNAL

    if buy_conditions >= 4:

        signal = "BUY"

        confidence = min(
            95,
            buy_conditions * 18
        )

    elif sell_conditions >= 4:

        signal = "SELL"

        confidence = min(
            95,
            sell_conditions * 18
        )

    if signal is None:
        return None

    return {
        "pair": pair,
        "signal": signal,
        "confidence": confidence,
        "rsi": round(rsi_value, 2),
        "stochastic": round(stochastic_value, 2),
        "momentum": round(momentum_value, 2),
        "pattern": pattern,
        "support": support,
        "resistance": resistance,
        "price": current_price
    }

# ======================================
# RESULT CHECKER
# ======================================


def check_signal_result(
    signal_id,
    pair,
    signal,
    entry_price,
    message_id
):

    try:

        now = datetime.now()

        wait_seconds = 60 - now.second

        time.sleep(wait_seconds + 2)

        url = (
            "https://api.binance.com/api/v3/klines"
            f"?symbol={pair}"
            "&interval=1m&limit=1"
        )

        response = requests.get(
            url,
            timeout=20
        )

        data = response.json()

        close_price = float(data[0][4])

        result = "LOSS"

        if (
            signal == "BUY"
            and close_price > entry_price
        ):
            result = "WIN"

        elif (
            signal == "SELL"
            and close_price < entry_price
        ):
            result = "WIN"

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE signals
            SET result=?
            WHERE id=?
            """,
            (
                result,
                signal_id
            )
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

        async def send_result():

            await bot.send_message(
                chat_id=CHAT_ID,
                text=with_footer(result_message),
                reply_to_message_id=message_id
            )

        loop = asyncio.new_event_loop()

        asyncio.set_event_loop(loop)

        loop.run_until_complete(send_result())

        loop.close()

        print(f"Result Sent: {signal_id}")

    except Exception as e:

        print(f"Result Error: {e}")

# ======================================
# SEND SIGNAL
# ======================================


async def send_signal(data, signal_id):

    amount = martingale_amount()

    entry_time = (
        datetime.now()
        + timedelta(minutes=1)
    )

    formatted_time = entry_time.strftime(
        "%I:%M %p"
    )

    candle_time = entry_time.strftime(
        "%H:%M"
    )

    stats = get_profit_stats()

    message = f"""

📊 ADVANCED QUOTEX SIGNAL

🆔 Signal ID: {signal_id}

💹 Pair: {data['pair']}

🚀 Signal: {data['signal']}

🎯 Confidence: {data['confidence']}%

📈 RSI: {data['rsi']}
📉 Stochastic: {data['stochastic']}
⚡ Momentum: {data['momentum']}

🕯 Pattern: {data['pattern']}

🟢 Support: {round(data['support'], 2)}
🔴 Resistance: {round(data['resistance'], 2)}

💰 Martingale Amount: ${amount}

📊 Total Profit: ${stats['profit']}

🏆 Wins: {stats['wins']}
❌ Losses: {stats['losses']}

⏰ Timeframe: 1 Minute

🕒 Entry Time: {formatted_time}

🕯 Entry Candle: {candle_time} Candle

"""

    sent_message = await safe_send_message(message)

    if sent_message is None:
        return

    update_message_id(
        signal_id,
        sent_message.message_id
    )

    executor.submit(
        check_signal_result,
        signal_id,
        data['pair'],
        data['signal'],
        data['price'],
        sent_message.message_id
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

🔥 LIVE SIGNAL ENGINE ACTIVE

"""

    await safe_send_message(message)

# ======================================
# MAIN LOOP
# ======================================


async def main():

    await startup_message()

    last_signal_time = {}
    last_signal_direction = {}

    while True:

        try:

            for pair in PAIRS:

                result = analyze_pair(pair)

                if result is None:

                    print(f"No signal: {pair}")
                    continue

                current_time = time.time()

                last_time = last_signal_time.get(
                    pair,
                    0
                )

                last_direction = (
                    last_signal_direction.get(pair)
                )

                if (
                    current_time - last_time >= SIGNAL_COOLDOWN
                    and result['signal'] != last_direction
                ):

                    signal_id = save_signal(
                        pair,
                        result['signal'],
                        result['confidence']
                    )

                    await send_signal(
                        result,
                        signal_id
                    )

                    last_signal_time[pair] = current_time

                    last_signal_direction[pair] = (
                        result['signal']
                    )

                    print(
                        f"Signal Sent: "
                        f"{pair} "
                        f"{result['signal']}"
                    )

                else:

                    print(
                        f"Skipped: {pair}"
                    )

            print(dashboard())

        except Exception as e:

            print(f"Main Loop Error: {e}")

        # better scan interval
        await asyncio.sleep(60)

# ======================================
# START BOT + WEB SERVER FOR RENDER WEB SERVICE
# ======================================

def start_bot_once():

    global bot_started

    if bot_started:
        return

    bot_started = True

    def runner():
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"Bot Runner Error: {e}")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


if __name__ == "__main__":

    start_bot_once()

    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
