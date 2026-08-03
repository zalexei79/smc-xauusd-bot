import os
import time
import threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
import requests
import pandas as pd
import numpy as np
import yfinance as yf

app = Flask(__name__)

# --- НАСТРОЙКИ СИСТЕМЫ И TELEGRAM ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XauProBot")
YAHOO_TICKER = "GC=F"
FUTURES_SPOT_OFFSET = 5.4

# Память для защиты от повторной отправки одинаковых сигналов в TG
last_sent_signal = {"buy_entry": 0.0, "sell_entry": 0.0}

# Глобальное состояние сигнала для REST API (cTrader)
latest_signal = {
    "symbol": "XAUUSD",
    "action": "",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "status": "NONE",
    "timestamp": 0
}

lock = threading.Lock()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_market_open():
    """Проверяет, открыт ли рынок (Пн-Пт)."""
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0 - Пн, 6 - Вс
    return weekday < 5

def get_market_data():
    """Загрузка данных H1 (тренд) и M15 (структура) с таймаутом"""
    try:
        gold = yf.Ticker(YAHOO_TICKER)
        df_h1 = gold.history(period="7d", interval="1h", timeout=15)
        df_m15 = gold.history(period="3d", interval="15m", timeout=15)
        
        if df_h1.empty or df_m15.empty:
            print("[-] [WARNING] Yahoo вернул пустые данные. Пропуск итерации...")
            return None, None
            
        for df in [df_h1, df_m15]:
            df['High'] -= FUTURES_SPOT_OFFSET
            df['Low'] -= FUTURES_SPOT_OFFSET
            df['Close'] -= FUTURES_SPOT_OFFSET
            df['Open'] -= FUTURES_SPOT_OFFSET
            
        return df_h1, df_m15
    except Exception as e:
        print(f"[-] Ошибка загрузки данных Yahoo: {e}")
        return None, None

def determine_h1_trend(df_h1):
    """Определение тренда на H1 с помощью скользящих средних"""
    df_h1['EMA50'] = df_h1['Close'].ewm(span=50, adjust=False).mean()
    df_h1['EMA200'] = df_h1['Close'].ewm(span=200, adjust=False).mean()
    
    last_close = df_h1['Close'].iloc[-1]
    ema50 = df_h1['EMA50'].iloc[-1]
    ema200 = df_h1['EMA200'].iloc[-1]
    
    if last_close > ema50 and ema50 > ema200:
        return "BULLISH"
    elif last_close < ema50 and ema50 < ema200:
        return "BEARISH"
    return "SIDEWAYS"

def send_telegram(text):
    """Отправка сообщения в Telegram канал с таймаутом"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try: 
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        print("[+] Сигнал успешно отправлен в Telegram!")
    except Exception as e:
        print(f"[-] Ошибка отправки в Telegram: {e}")

def validate_and_adjust_signal(action, entry_price, raw_sl, raw_tp):
    """Корректирует SL ($5-$10) и TP ($12-$40) под реальную волатильность Золота (XAUUSD)."""
    MIN_SL_DOLLARS = 5.0
    MAX_SL_DOLLARS = 10.0
    MIN_TP_DOLLARS = 12.0
    MAX_TP_DOLLARS = 40.0

    if action == "BUY":
        sl_dist = entry_price - raw_sl
        tp_dist = raw_tp - entry_price

        if sl_dist < MIN_SL_DOLLARS:
            adjusted_sl = entry_price - MIN_SL_DOLLARS
        elif sl_dist > MAX_SL_DOLLARS:
            adjusted_sl = entry_price - MAX_SL_DOLLARS
        else:
            adjusted_sl = raw_sl

        if tp_dist < MIN_TP_DOLLARS:
            adjusted_tp = entry_price + MIN_TP_DOLLARS
        elif tp_dist > MAX_TP_DOLLARS:
            adjusted_tp = entry_price + MAX_TP_DOLLARS
        else:
            adjusted_tp = raw_tp

    elif action == "SELL":
        sl_dist = raw_sl - entry_price
        tp_dist = entry_price - raw_tp

        if sl_dist < MIN_SL_DOLLARS:
            adjusted_sl = entry_price + MIN_SL_DOLLARS
        elif sl_dist > MAX_SL_DOLLARS:
            adjusted_sl = entry_price + MAX_SL_DOLLARS
        else:
            adjusted_sl = raw_sl

        if tp_dist < MIN_TP_DOLLARS:
            adjusted_tp = entry_price - MIN_TP_DOLLARS
        elif tp_dist > MAX_TP_DOLLARS:
            adjusted_tp = entry_price - MAX_TP_DOLLARS
        else:
            adjusted_tp = raw_tp
    else:
        return None, None, None

    return round(entry_price, 2), round(adjusted_sl, 2), round(adjusted_tp, 2)

def update_api_signal(action, entry, sl, tp):
    """Обновляет состояние API моста для передачи сигнала в cTrader"""
    global latest_signal
    with lock:
        latest_signal = {
            "symbol": "XAUUSD",
            "action": action,
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "status": "NEW",
            "timestamp": int(time.time())
        }
    print(f"✅ Новый сигнал по Gold зафиксирован для cTrader: {action} @ {entry}")

def send_startup_analytics():
    """Отправляет стартовую аналитику при запуске сервера"""
    print("[+] [STARTUP] Генерация стартового сигнала аналитики...")
    df_h1, df_m15 = get_market_data()
    if df_h1 is None or df_m15 is None:
        return

    current_price = round(float(df_m15['Close'].iloc[-1]), 2)
    trend = determine_h1_trend(df_h1)
    
    if trend == "BEARISH":
        action = "SELL"
        entry_price = round(current_price + 6.0, 2)
        raw_sl = entry_price + 6.0
        raw_tp = entry_price - 20.0
    else:
        action = "BUY"
        entry_price = round(current_price - 6.0, 2)
        raw_sl = entry_price - 6.0
        raw_tp = entry_price + 20.0

    entry, sl, tp = validate_and_adjust_signal(action, entry_price, raw_sl, raw_tp)
    if not entry:
        return

    icon = "🟢 BUY (ПО ТРЕНДУ)" if action == "BUY" else "🔴 SELL (ПО ТРЕНДУ)"
    text = (
        f"⚡ **SMART MONEY SWEEP** (СТАРТОВАЯ АНАЛИТИКА)\n"
        f"🎯 Направление: **{icon}**\n"
        f"-----------------------------------------\n"
        f"📍 Вход: **{entry}**\n"
        f"🛡️ Stop-Loss: **{sl}**\n"
        f"🎯 Take-Profit: **{tp}**\n\n"
        f"💡 *Стартовый ордер аналитики от зоны ликвидности.*"
    )
    send_telegram(text)
    
    # Также передаем стартовый сигнал в cTrader для мгновенного теста связи
    update_api_signal(action, entry, sl, tp)

def analyze_smart_money_trend_sweep():
    global last_sent_signal
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] [GOLD] Сканирование рынка (SMC Trend Sweep)...")
    
    if not is_market_open():
        print(f"[{now_str}] [GOLD] Выходные дни. Рынок закрыт. Пропуск.")
        return

    try:
        df_h1, df_m15 = get_market_data()
        if df_h1 is None or df_m15 is None: 
            return

        trend = determine_h1_trend(df_h1)
        print(f"[{now_str}] [GOLD] Тренд H1: {trend}")
        
        if trend == "SIDEWAYS":
            print(f"[{now_str}] [GOLD] Флэт/Нет выраженного тренда на H1. Пропуск.")
            return

        m15_window = df_m15.tail(20)
        box_high = float(m15_window['High'].iloc[:-2].max())
        box_low = float(m15_window['Low'].iloc[:-2].min())
        
        last_candle = m15_window.iloc[-1]
        prev_candle = m15_window.iloc[-2]
        
        signal_type = None
        raw_entry = raw_sl = raw_tp = 0.0

        if trend == "BULLISH":
            if prev_candle['Low'] < box_low and last_candle['Close'] > box_low:
                raw_entry = float(last_candle['Close'])
                raw_sl = float(prev_candle['Low']) - 1.5
                risk = raw_entry - raw_sl
                
                if risk > 1.0:
                    raw_tp = raw_entry + (risk * 2.5)
                    signal_type = "BUY"

        elif trend == "BEARISH":
            if prev_candle['High'] > box_high and last_candle['Close'] < box_high:
                raw_entry = float(last_candle['Close'])
                raw_sl = float(prev_candle['High']) + 1.5
                risk = raw_sl - raw_entry
                
                if risk > 1.0:
                    raw_tp = raw_entry - (risk * 2.5)
                    signal_type = "SELL"

        if signal_type:
            entry, sl, tp = validate_and_adjust_signal(signal_type, raw_entry, raw_sl, raw_tp)

            if not entry:
                return

            if signal_type == "BUY" and abs(entry - last_sent_signal["buy_entry"]) < 0.8:
                print(f"[{now_str}] [GOLD] Повторный BUY сигнал без существенных изменений. Пропуск.")
                return
            if signal_type == "SELL" and abs(entry - last_sent_signal["sell_entry"]) < 0.8:
                print(f"[{now_str}] [GOLD] Повторный SELL сигнал без существенных изменений. Пропуск.")
                return

            last_sent_signal.update({"buy_entry": entry if signal_type == "BUY" else last_sent_signal["buy_entry"], 
                                     "sell_entry": entry if signal_type == "SELL" else last_sent_signal["sell_entry"]})

            icon = "🟢 BUY (ПО ТРЕНДУ)" if signal_type == "BUY" else "🔴 SELL (ПО ТРЕНДУ)"
            text = (
                f"⚡ **SMART MONEY SWEEP**\n"
                f"🎯 Направление: **{icon}**\n"
                f"-----------------------------------------\n"
                f"📍 Вход: **{entry}**\n"
                f"🛡️ Stop-Loss: **{sl}**\n"
                f"🎯 Take-Profit: **{tp}**\n\n"
                f"💡 *Захвачена ликвидность {('снизу' if signal_type=='BUY' else 'сверху')}.*"
            )
            send_telegram(text)
            update_api_signal(signal_type, entry, sl, tp)

    except Exception as e:
        print(f"[-] Ошибка в процессе анализа: {e}")

# --- ФОНОВЫЙ ПОТОК СКАНИРОВАНИЯ ---
def market_scanner_loop():
    print("[+] ИИ GOLD SMC Trend Sweep запущен в круглосуточном режиме (Пн-Пт)")
    try:
        send_startup_analytics()
    except Exception as e:
        print(f"[-] Первичная ошибка стартовой аналитики: {e}")

    while True:
        try:
            now = datetime.now()
            minute_offset = now.minute % 15
            
            if minute_offset == 1 and now.second < 10:
                analyze_smart_money_trend_sweep()
                time.sleep(60)
            else:
                minutes_to_wait = (1 - minute_offset) % 15
                if minutes_to_wait == 0: 
                    minutes_to_wait = 15
                
                seconds_to_wait = (minutes_to_wait * 60) - now.second
                time.sleep(seconds_to_wait)
                
        except Exception as fatal_error:
            print(f"[-] Critical error in scanner loop: {fatal_error}")
            time.sleep(10)

# Запуск сканера в фоновом потоке
scanner_thread = threading.Thread(target=market_scanner_loop, daemon=True)
scanner_thread.start()

# --- ENDPOINTS ДЛЯ REST API (cTrader) ---
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "symbol": "XAUUSD"})

@app.route('/signal', methods=['GET'])
def get_signal():
    with lock:
        return jsonify(latest_signal)

@app.route('/ack', methods=['POST'])
def acknowledge_signal():
    global latest_signal
    data = request.get_json(silent=True) or {}
    ts = data.get('timestamp')

    with lock:
        if latest_signal["timestamp"] == ts or latest_signal["status"] == "NEW":
            latest_signal["status"] = "PROCESSED"
            print(f"👍 Сигнал по XAUUSD (ts={ts}) подтвержден cTrader и переведен в PROCESSED")
            return jsonify({"status": "acknowledged"})
            
    return jsonify({"status": "not_found"}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)