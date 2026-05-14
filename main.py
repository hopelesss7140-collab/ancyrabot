import os
import time
import requests
import pandas as pd
import ta
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

# ==================================================
# CONFIG
# ==================================================

SYMBOL       = "XRPUSDT"
INTERVAL     = "15"        # 15dk → en iyi scalp TF
RISK_PERCENT = 0.01
LEVERAGE     = 10
SL_MULT      = 2.5
TP_MULT      = 2.75
ATR_PERIOD   = 11
COOLDOWN_SEC = 900         # Sinyal sonrası 15dk bekleme

# EMA
EMA_FAST  = 15
EMA_SLOW  = 65
EMA_TREND = 150

# RSI
RSI_PERIOD = 18
RSI_HIGH   = 70
RSI_LOW    = 30
RSI_MID    = 50

# ADX
ADX_PERIOD = 15
ADX_MIN    = 19

# ==================================================
# STATE
# ==================================================

state = {
    "in_position": False,
    "pos_type":    None,       # "LONG" / "SHORT"
    "entry":       0.0,
    "sl":          0.0,
    "tp":          0.0,
    "last_signal": 0,          # timestamp
    "trades":      0,
    "wins":        0,
}

# ==================================================
# TELEGRAM
# ==================================================

bot     = Bot(token=os.getenv("TELEGRAM_TOKEN"))
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="HTML")
    except Exception as e:
        print(f"Telegram Error: {e}")

# ==================================================
# GET KLINES
# ==================================================

def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=300):
    url = (
        f"https://api.bybit.com/v5/market/kline"
        f"?category=linear&symbol={symbol}"
        f"&interval={interval}&limit={limit}"
    )
    r        = requests.get(url, timeout=10).json()
    data     = r["result"]["list"]
    df       = pd.DataFrame(data)
    df       = df.iloc[::-1].reset_index(drop=True)
    df.columns = ["time","open","high","low","close","volume","turnover"]
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

# ==================================================
# INDICATORS
# ==================================================

def add_indicators(df):
    df["ema_fast"]  = ta.trend.ema_indicator(df["close"], window=EMA_FAST)
    df["ema_slow"]  = ta.trend.ema_indicator(df["close"], window=EMA_SLOW)
    df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=EMA_TREND)
    df["rsi"]       = ta.momentum.rsi(df["close"], window=RSI_PERIOD)
    adx_ind         = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=ADX_PERIOD)
    df["adx"]       = adx_ind.adx()
    df["atr"]       = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=ATR_PERIOD)
    df["vwap"]      = ta.volume.volume_weighted_average_price(
                          df["high"], df["low"], df["close"], df["volume"], window=20)
    df["vol_sma"]   = df["volume"].rolling(20).mean()
    return df

# ==================================================
# POSITION CHECK
# ==================================================

def check_position(last_close):
    if not state["in_position"]:
        return

    ptype = state["pos_type"]
    sl    = state["sl"]
    tp    = state["tp"]
    entry = state["entry"]

    hit_sl = (ptype == "LONG"  and last_close <= sl) or \
             (ptype == "SHORT" and last_close >= sl)
    hit_tp = (ptype == "LONG"  and last_close >= tp) or \
             (ptype == "SHORT" and last_close <= tp)

    if hit_sl:
        pnl_pct = -abs(entry - sl) / entry * 100 * LEVERAGE
        state["in_position"] = False
        state["trades"]     += 1
        msg = (
            f"🔴 <b>STOP LOSS — {ptype}</b>\n\n"
            f"Sembol: {SYMBOL}\n"
            f"Giriş:  {entry:.4f}\n"
            f"SL:     {sl:.4f}\n"
            f"PnL:    {pnl_pct:.2f}%\n\n"
            f"📊 Toplam işlem: {state['trades']} | "
            f"Win: {state['wins']} | "
            f"WR: {state['wins']/max(1,state['trades'])*100:.1f}%"
        )
        send_telegram(msg)

    elif hit_tp:
        pnl_pct = abs(tp - entry) / entry * 100 * LEVERAGE
        state["in_position"] = False
        state["trades"]     += 1
        state["wins"]       += 1
        msg = (
            f"✅ <b>TAKE PROFIT — {ptype}</b>\n\n"
            f"Sembol: {SYMBOL}\n"
            f"Giriş:  {entry:.4f}\n"
            f"TP:     {tp:.4f}\n"
            f"PnL:    +{pnl_pct:.2f}%\n\n"
            f"📊 Toplam işlem: {state['trades']} | "
            f"Win: {state['wins']} | "
            f"WR: {state['wins']/max(1,state['trades'])*100:.1f}%"
        )
        send_telegram(msg)

# ==================================================
# ANALYZE
# ==================================================

def analyze():
    df   = get_klines()
    df   = add_indicators(df)
    df   = df.dropna().reset_index(drop=True)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Pozisyon kontrolü
    check_position(last["close"])

    # Cooldown kontrolü
    now = time.time()
    if (now - state["last_signal"]) < COOLDOWN_SEC:
        remaining = int(COOLDOWN_SEC - (now - state["last_signal"]))
        print(f"Cooldown: {remaining}s kaldı")
        return

    # Açık pozisyon varsa yeni sinyal verme
    if state["in_position"]:
        print(f"Pozisyon açık: {state['pos_type']} @ {state['entry']:.4f}")
        return

    atr = last["atr"]

    # ──────────────────────────────
    # LONG koşulları
    # ──────────────────────────────
    long_trend  = last["close"] > last["ema_trend"] and last["ema_fast"] > last["ema_slow"]
    long_rsi    = RSI_MID < last["rsi"] < RSI_HIGH
    long_adx    = last["adx"] > ADX_MIN
    long_vol    = last["volume"] > last["vol_sma"]
    long_vwap   = prev["close"] < prev["vwap"] and last["close"] > last["vwap"]

    long_signal = long_trend and long_rsi and long_adx and long_vol and long_vwap

    # ──────────────────────────────
    # SHORT koşulları
    # ──────────────────────────────
    short_trend = last["close"] < last["ema_trend"] and last["ema_fast"] < last["ema_slow"]
    short_rsi   = RSI_LOW < last["rsi"] < RSI_MID
    short_adx   = last["adx"] > ADX_MIN
    short_vol   = last["volume"] > last["vol_sma"]
    short_vwap  = prev["close"] > prev["vwap"] and last["close"] < last["vwap"]

    short_signal = short_trend and short_rsi and short_adx and short_vol and short_vwap

    # ──────────────────────────────
    # LONG SİNYAL
    # ──────────────────────────────
    if long_signal:
        entry = last["close"]
        sl    = round(entry - atr * SL_MULT, 4)
        tp    = round(entry + atr * TP_MULT, 4)
        rr    = round(TP_MULT / SL_MULT, 2)

        state["in_position"] = True
        state["pos_type"]    = "LONG"
        state["entry"]       = entry
        state["sl"]          = sl
        state["tp"]          = tp
        state["last_signal"] = now

        msg = (
            f"🚀 <b>LONG SİNYAL</b>\n\n"
            f"Sembol:   {SYMBOL}\n"
            f"TF:       {INTERVAL}dk\n"
            f"Fiyat:    {entry:.4f}\n\n"
            f"🛡 SL:    {sl:.4f} ({SL_MULT}x ATR)\n"
            f"🎯 TP:    {tp:.4f} ({TP_MULT}x ATR)\n"
            f"⚖️ R/R:   1:{rr}\n\n"
            f"📊 RSI: {last['rsi']:.1f} | ADX: {last['adx']:.1f}\n"
            f"📈 EMA Trend: YUKARI\n"
            f"💧 VWAP: KIRILDI YUKARI\n"
            f"📦 Hacim: GÜÇLÜ"
        )
        send_telegram(msg)
        print(f"LONG → {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}")

    # ──────────────────────────────
    # SHORT SİNYAL
    # ──────────────────────────────
    elif short_signal:
        entry = last["close"]
        sl    = round(entry + atr * SL_MULT, 4)
        tp    = round(entry - atr * TP_MULT, 4)
        rr    = round(TP_MULT / SL_MULT, 2)

        state["in_position"] = True
        state["pos_type"]    = "SHORT"
        state["entry"]       = entry
        state["sl"]          = sl
        state["tp"]          = tp
        state["last_signal"] = now

        msg = (
            f"🔻 <b>SHORT SİNYAL</b>\n\n"
            f"Sembol:   {SYMBOL}\n"
            f"TF:       {INTERVAL}dk\n"
            f"Fiyat:    {entry:.4f}\n\n"
            f"🛡 SL:    {sl:.4f} ({SL_MULT}x ATR)\n"
            f"🎯 TP:    {tp:.4f} ({TP_MULT}x ATR)\n"
            f"⚖️ R/R:   1:{rr}\n\n"
            f"📊 RSI: {last['rsi']:.1f} | ADX: {last['adx']:.1f}\n"
            f"📉 EMA Trend: AŞAĞI\n"
            f"💧 VWAP: KIRILDI AŞAĞI\n"
            f"📦 Hacim: GÜÇLÜ"
        )
        send_telegram(msg)
        print(f"SHORT → {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}")

    else:
        print(f"Sinyal yok | Fiyat: {last['close']:.4f} | RSI: {last['rsi']:.1f} | ADX: {last['adx']:.1f}")

# ==================================================
# MAIN LOOP
# ==================================================

send_telegram(
    f"✅ <b>ANCYRA SCALP BOT BAŞLADI</b>\n\n"
    f"Sembol: {SYMBOL}\n"
    f"TF: {INTERVAL}dk\n"
    f"SL: {SL_MULT}x ATR\n"
    f"TP: {TP_MULT}x ATR\n"
    f"Kaldıraç: {LEVERAGE}x"
)

while True:
    try:
        analyze()
    except Exception as e:
        print(f"ERROR: {e}")
        send_telegram(f"⚠️ <b>BOT HATA</b>\n{e}")
    time.sleep(60)
