import os
import time
import requests
import threading
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, jsonify, request
from datetime import datetime, timezone

# ------------------------------------------------------------------
# CONFIGURATION & ENVIRONMENT VARIABLES
# ------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XauProBot")

YAHOO_TICKER = "GC=F"
FUTURES_SPOT_OFFSET = 5.4  # Коррекция цен GC=F под спот XAUUSD

# Параметры риск-менеджмента для Gold (XAUUSD)
RISK_REWARD_RATIO = 2.0
MIN_SL_DIST = 5.0   # Минимальный Стоп-Лосс ($5)
MAX_SL_DIST = 10.0  # Максимальный Стоп-Лосс ($10)
MIN_TP_DIST = 12.0  # Минимальный Тейк-Профит ($12)
MAX_TP_DIST = 40.0  # Максимальный Тейк-Профит ($40)

app = Flask(__name__)

# Глобальное хранилище текущего сигнала для cTrader
latest_signal = {
    "symbol": "XAUUSD",
    "signal": "NONE",
    "entry_price": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "timestamp": None,
    "status": "NONE"
}

# ------------------------------------------------------------------
# TELEGRAM NOTIFIER
# ------------------------------------------------------------------
def send_telegram(text):
    """Отправка сообщений в Telegram канал"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("[+] Сигнал успешно отправлен в Telegram!")
        else:
            print(f"[-] Ошибка отправки в Telegram: {res.text}")
    except Exception as e:
        print(f"[-] Исключение при отправке в Telegram: {e}")

# ------------------------------------------------------------------
# DATA FETCHING (YAHOO FINANCE RATE-LIMIT BYPASS)
# ------------------------------------------------------------------
def get_market_data():
    """Загрузка данных H1 и M15 с защитой от блокировок Yahoo Finance (429 Rate Limit)"""
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

        # Запрашиваем минимальный период для ускорения отклика
        df_h1 = yf.download(YAHOO_TICKER, period="5d", interval="1h", progress=False, session=session)
        df_m15 = yf.download(YAHOO_TICKER, period="2d", interval="15m", progress=False, session=session)
        
        if df_h1.empty or df_m15.empty:
            print("[-] [WARNING] Yahoo вернул пустые данные. Пропуск итерации...")
            return None, None

        # Разворачиваем MultiIndex если yfinance вернул вложенные колонки
        if isinstance(df_h1.columns, pd.MultiIndex):
            df_h1.columns = df_h1.columns.get_level_values(0)
        if isinstance(df_m15.columns, pd.MultiIndex):
            df_m15.columns = df_m15.columns.get_level_values(0)
            
        # Корректировка цен с фьючерса на спот XAUUSD
        for df in [df_h1, df_m15]:
            df['High'] -= FUTURES_SPOT_OFFSET
            df['Low'] -= FUTURES_SPOT_OFFSET
            df['Close'] -= FUTURES_SPOT_OFFSET
            df['Open'] -= FUTURES_SPOT_OFFSET
            
        return df_h1, df_m15
    except Exception as e:
        print(f"[-] Ошибка загрузки данных Yahoo: {e}")
        return None, None

# ------------------------------------------------------------------
# SMC TREND SWEEP STRATEGY LOGIC
# ------------------------------------------------------------------
def analyze_smart_money_trend_sweep():
    """Анализ рынка по алгоритму SMC Trend Sweep для Gold"""
    global latest_signal
    
    df_h1, df_m15 = get_market_data()
    if df_h1 is None or df_m15 is None:
        return

    # 1. Определение H1 Тренда
    h1_close = df_h1['Close'].iloc[-1]
    h1_sma = df_h1['Close'].rolling(window=20).mean().iloc[-1]
    h1_trend = "BULLISH" if h1_close > h1_sma else "BEARISH"

    # 2. Поиск Liquidity Sweep на M15 (последние 20 свечей)
    recent_m15 = df_m15.iloc[-20:-1]
    last_candle = df_m15.iloc[-1]
    
    swing_high = recent_m15['High'].max()
    swing_low = recent_m15['Low'].min()
    
    current_price = round(float(last_candle['Close']), 2)
    signal_type = "NONE"
    sl, tp = 0.0, 0.0

    # Покупка: Бычий тренд + Снятие лоев (Liquidity Sweep Low)
    if h1_trend == "BULLISH" and last_candle['Low'] < swing_low and last_candle['Close'] > swing_low:
        signal_type = "BUY"
        raw_sl_dist = current_price - float(last_candle['Low'])
        sl_dist = max(MIN_SL_DIST, min(raw_sl_dist + 1.0, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        
        sl = round(current_price - sl_dist, 2)
        tp = round(current_price + tp_dist, 2)

    # Продажа: Медвежий тренд + Снятие хаев (Liquidity Sweep High)
    elif h1_trend == "BEARISH" and last_candle['High'] > swing_high and last_candle['Close'] < swing_high:
        signal_type = "SELL"
        raw_sl_dist = float(last_candle['High']) - current_price
        sl_dist = max(MIN_SL_DIST, min(raw_sl_dist + 1.0, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        
        sl = round(current_price + sl_dist, 2)
        tp = round(current_price - tp_dist, 2)

    # Если сгенерирован новый сигнал
    if signal_type != "NONE":
        latest_signal = {
            "symbol": "XAUUSD",
            "signal": signal_type,
            "entry_price": current_price,
            "sl": sl,
            "tp": tp,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "status": "NEW"
        }
        
        # Отправка в Telegram
        msg = (
            f"⚡ <b>GOLD SMC TREND SWEEP SIGNAL</b> ⚡\n\n"
            f"<b>Инструмент:</b> XAUUSD\n"
            f"<b>Тип:</b> {signal_type}\n"
            f"<b>Вход:</b> {current_price}\n"
            f"<b>SL:</b> {sl}\n"
            f"<b>TP:</b> {tp}\n"
            f"<b>Время:</b> {latest_signal['timestamp']}"
        )
        send_telegram(msg)

# ------------------------------------------------------------------
# STARTUP TEST ANALYTICS SIGNAL
# ------------------------------------------------------------------
def send_startup_analytics():
    """Стартовый сигнал при перезапуске сервера для проверки связки с cTrader/Telegram"""
    global latest_signal
    df_h1, df_m15 = get_market_data()
    
    if df_m15 is not None and not df_m15.empty:
        current_price = round(float(df_m15['Close'].iloc[-1]), 2)
    else:
        current_price = 2650.00 # Запасное значение, если рынок закрыт или пуст
        
    signal_type = "BUY"
    sl = round(current_price - 6.0, 2)
    tp = round(current_price + 12.0, 2)
    
    latest_signal = {
        "symbol": "XAUUSD",
        "signal": signal_type,
        "entry_price": current_price,
        "sl": sl,
        "tp": tp,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "status": "NEW"
    }
    
    msg = (
        f"🚀 <b>[STARTUP] ИИ SMC Gold запущен!</b>\n"
        f"Тестовая аналитика XAUUSD сформирована.\n\n"
        f"<b>Тип:</b> {signal_type}\n"
        f"<b>Вход:</b> {current_price}\n"
        f"<b>SL:</b> {sl} | <b>TP:</b> {tp}\n"
        f"<i>Ожидание исполнения в cTrader...</i>"
    )
    send_telegram(msg)

# ------------------------------------------------------------------
# SCANNER TIMER LOOP (SYNCHRONIZED TO M15 CLOSES)
# ------------------------------------------------------------------
def market_scanner_loop():
    """Таймер сканирования рынка строго в :01, :16, :31, :46 минут"""
    print("[+] ИИ GOLD SMC Trend Sweep запущен в режиме автосканирования")
    
    # 1. Запуск тестового сигнала при старте
    try:
        send_startup_analytics()
    except Exception as e:
        print(f"[-] Ошибка стартовой аналитики: {e}")

    # 2. Основной цикл ожидания M15 свечей
    while True:
        try:
            now = datetime.now(timezone.utc)
            current_minute = now.minute
            target_minutes = [1, 16, 31, 46]
            
            next_min = next((m for m in target_minutes if m > current_minute), None)
            
            if next_min is not None:
                minutes_to_wait = next_min - current_minute
            else:
                minutes_to_wait = (60 - current_minute) + 1

            seconds_to_wait = (minutes_to_wait * 60) - now.second + 1
            
            print(f"⏳ Следующий анализ рынка XAUUSD через {int(seconds_to_wait)} сек...")
            time.sleep(seconds_to_wait)

            analyze_smart_money_trend_sweep()
                
        except Exception as fatal_error:
            print(f"[-] Ошибка в цикле таймера: {fatal_error}")
            time.sleep(10)

# ------------------------------------------------------------------
# REST API ENDPOINTS FOR CTRADER
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "active", "system": "XAUUSD SMC Bridge"}), 200

@app.route('/signal', methods=['GET'])
def get_signal():
    """cTrader опрашивает этот эндпоинт каждые N секунд"""
    return jsonify(latest_signal), 200

@app.route('/ack', methods=['POST'])
def acknowledge_signal():
    """cTrader подтверждает открытие сделки и сбрасывает статус"""
    global latest_signal
    latest_signal["status"] = "PROCESSED"
    print("[+] Сигнал обработан cTrader и переведен в статус PROCESSED")
    return jsonify({"status": "acknowledged"}), 200

# ------------------------------------------------------------------
# APPLICATION ENTRY POINT
# ------------------------------------------------------------------
if __name__ == '__main__':
    # Запуск фонового сканера рынка
    scanner_thread = threading.Thread(target=market_scanner_loop, daemon=True)
    scanner_thread.start()
    
    # Запуск Flask API
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
