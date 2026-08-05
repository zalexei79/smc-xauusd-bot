import os
import time
import json
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
import pandas as pd
import requests

app = Flask(__name__)

# ------------------------------------------------------------------
# НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "c997ad22987e477e83034ea132621542")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8874872969:AAHtxvHw_mupom466pm3jh4BkZEjEAQ180A")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@XauProBot")

SYMBOL = "XAU/USD"
SIGNAL_FILE = "/tmp/gold_analytics_signal.json"
SIGNAL_LIFETIME_SECONDS = 10800  # Сигнал активен 3 часа (10800 сек)

# Параметры риск-менеджмента для Gold (XAUUSD)
RISK_REWARD_RATIO = 2.0
MIN_SL_DIST = 5.0    # Минимальный Стоп-Лосс ($5)
MAX_SL_DIST = 15.0   # Максимальный Стоп-Лосс ($15)
MIN_TP_DIST = 10.0   # Минимальный Тейк-Профит ($10)
MAX_TP_DIST = 45.0   # Максимальный Тейк-Профит ($45)

EMPTY_SIGNAL = {
    "symbol": "XAUUSD",
    "action": "NONE",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "status": "NONE",
    "timestamp": 0
}

lock = threading.Lock()

# ------------------------------------------------------------------
# РАБОТА С ФАЙЛОМ СИГНАЛА
# ------------------------------------------------------------------
def save_signal_to_file(signal_data):
    try:
        with open(SIGNAL_FILE, "w") as f:
            json.dump(signal_data, f)
    except Exception as e:
        print(f"[-] Ошибка записи сигнала в файл: {e}")

def load_signal_from_file():
    if not os.path.exists(SIGNAL_FILE):
        return EMPTY_SIGNAL
    try:
        with open(SIGNAL_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return EMPTY_SIGNAL

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
            print("[+] Сообщение успешно отправлено в Telegram!")
        else:
            print(f"[-] Ошибка отправки в Telegram: {res.text}")
    except Exception as e:
        print(f"[-] Исключение при отправке в Telegram: {e}")

# ------------------------------------------------------------------
# ЗАГРУЗКА ДАННЫХ ИЗ TWELVEDATA API (С ЗАЩИТОЙ ОТ ПРЕВЫШЕНИЯ ЛИМИТОВ)
# ------------------------------------------------------------------
def fetch_tf_data(interval):
    """Загрузка таймфрейма через TwelveData"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={interval}&outputsize=40&apikey={TWELVE_DATA_API_KEY}"
        
        res = requests.get(url, headers=headers, timeout=10).json()
        
        if "values" not in res:
            print(f"[-] Ошибка TwelveData на {interval}: {res.get('message', 'No values')}")
            return interval, None
        
        df = pd.DataFrame(res["values"])
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        # Разворачиваем порядок хронологически (от старых к новым)
        df.iloc[:] = df.iloc[::-1].values
        return interval, df
    except Exception as e:
        print(f"[-] Исключение при загрузке {interval}: {e}")
        return interval, None

def get_multi_tf_market_data():
    """Последовательная загрузка H4, H1 и M15 с паузй для соблюдения лимита 8 zapros/min"""
    intervals = ["4h", "1h", "15min"]
    dfs = {}
    
    for interval in intervals:
        _, df = fetch_tf_data(interval)
        if df is None:
            return None, None, None
        dfs[interval] = df
        time.sleep(1.2) # Пауза 1.2 секунды между запросами, чтобы не превысить лимит 8 зап/мин

    return dfs["4h"], dfs["1h"], dfs["15min"]

# ------------------------------------------------------------------
# SMC АНАЛИТИКА И ГЕНЕРАЦИЯ СИГНАЛА (3h CYCLE)
# ------------------------------------------------------------------
def run_gold_analytics():
    """Основной анализ рынка XAUUSD по SMC концепции"""
    now_utc = datetime.now(timezone.utc)
    print(f"🔍 [{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}] Старт 3-часового SMC анализа Gold (H4, H1, M15)...")

    df_h4, df_h1, df_m15 = get_multi_tf_market_data()
    if df_h4 is None or df_h1 is None or df_m15 is None:
        print("[-] Ошибка: Не удалось получить данные от TwelveData. Пропуск цикла.")
        return

    curr_price = round(float(df_m15['Close'].iloc[-1]), 2)

    # 1. Тренд H4 и H1 (по скользящим средним и структуре)
    h4_ema = df_h4['Close'].tail(20).mean()
    h1_ema = df_h1['Close'].tail(20).mean()
    
    is_bullish = curr_price >= h4_ema and curr_price >= h1_ema
    trend_str = "🟢 BULLISH (Бычий)" if is_bullish else "🔴 BEARISH (Медвежий)"

    # 2. Анализ ликвидности M15 / H1
    recent_m15 = df_m15.iloc[-20:-1]
    last_m15 = df_m15.iloc[-1]
    
    swing_high = float(recent_m15['High'].max())
    swing_low = float(recent_m15['Low'].min())

    action = "NONE"
    reason = ""
    sl, tp = 0.0, 0.0

    # 3. Логика сигналов SMC Sweep & Structure Breakout
    # Покупка: Бычий тренд + Забор ликвидности снизу (Liquidity Sweep Low)
    if is_bullish and float(last_m15['Low']) < swing_low and float(last_m15['Close']) > swing_low:
        action = "BUY"
        reason = "SMC Liquidity Sweep Low (снятие продавцов на M15 в направлении H4/H1 тренда)"
        raw_sl = float(last_m15['Low']) - 0.5
        sl_dist = max(MIN_SL_DIST, min(curr_price - raw_sl, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        sl = round(curr_price - sl_dist, 2)
        tp = round(curr_price + tp_dist, 2)

    # Продажа: Медвежий тренд + Забор ликвидности сверху (Liquidity Sweep High)
    elif not is_bullish and float(last_m15['High']) > swing_high and float(last_m15['Close']) < swing_high:
        action = "SELL"
        reason = "SMC Liquidity Sweep High (снятие покупателей на M15 в направлении H4/H1 тренда)"
        raw_sl = float(last_m15['High']) + 0.5
        sl_dist = max(MIN_SL_DIST, min(raw_sl - curr_price, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        sl = round(curr_price + sl_dist, 2)
        tp = round(curr_price - tp_dist, 2)

    # Базовый трендовый вход (если нет ясного Sweep, но есть сильный трендовый импульс)
    elif is_bullish:
        action = "BUY"
        reason = "Почасовой трендовый импульс H4/H1"
        sl = round(curr_price - 7.5, 2)
        tp = round(curr_price + 15.0, 2)
    else:
        action = "SELL"
        reason = "Почасовой трендовый импульс H4/H1"
        sl = round(curr_price + 7.5, 2)
        tp = round(curr_price - 15.0, 2)

    # Сохраняем сигнал для cTrader
    signal = {
        "symbol": "XAUUSD",
        "action": action,
        "entry": curr_price,
        "sl": sl,
        "tp": tp,
        "status": "NEW",
        "timestamp": int(now_utc.timestamp())
    }

    with lock:
        save_signal_to_file(signal)

    # Отправка подробного отчета в Telegram
    msg = (
        f"🏆 <b>GOLD SMC ANALYTICS REPORT (3H)</b> 🏆\n\n"
        f"<b>Инструмент:</b> XAU/USD (Gold)\n"
        f"<b>Макро-Тренд (H4/H1):</b> {trend_str}\n"
        f"<b>Рекомендация:</b> <b>{action}</b>\n\n"
        f"📍 <b>Вход:</b> <code>{curr_price}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit:</b> <code>{tp}</code>\n\n"
        f"💡 <b>Обоснование:</b> {reason}\n"
        f"🕒 <i>Время анализа: {now_utc.strftime('%H:%M:%S UTC')}</i>"
    )
    send_telegram(msg)
    print(f"[+] Аналитика завершена. Сигнал: {action} @ {curr_price}")

# ------------------------------------------------------------------
# СИНХРОНИЗИРОВАННЫЙ С АСТРОНОМИЧЕСКИМИ ЧАСАМИ ТАЙМЕР (СДВИГ +1 ЧАС ДЛЯ ЗОЛОТА)
# ------------------------------------------------------------------
def get_seconds_until_next_3h_mark():
    """
    Вычисляет оставшиеся секунды до 3-часовых меток с учетом сдвига на +1 час (UTC):
    Метки запуска: 01:00, 04:00, 07:00, 10:00, 13:00, 16:00, 19:00, 22:00 UTC
    """
    now = datetime.now(timezone.utc)
    
    # Сетка часов с учетом смещения рынка Золота (+1 час)
    gold_schedule_hours = [1, 4, 7, 10, 13, 16, 19, 22]
    
    target_hour = None
    for h in gold_schedule_hours:
        if now.hour < h:
            target_hour = h
            break
            
    if target_hour is not None:
        target_time = now.replace(hour=target_hour, minute=0, second=10, microsecond=0)
    else:
        # Если время больше 22:00 UTC, следующий запуск завтра в 01:00 UTC
        target_time = (now + timedelta(days=1)).replace(hour=1, minute=0, second=10, microsecond=0)
        
    sleep_seconds = (target_time - now).total_seconds()
    return max(sleep_seconds, 5)

def analytics_scheduler_loop():
    """Первичный запуск при старте, затем выравнивание по сетке Золота (01, 04, 07... UTC)"""
    time.sleep(3)
    run_gold_analytics()  # Стартовый анализ при запускe

    while True:
        sleep_time = get_seconds_until_next_3h_mark()
        minutes_wait = round(sleep_time / 60, 1)
        print(f"⏳ Ожидание {minutes_wait} мин. ({int(sleep_time)} сек.) до следующего 3H цикла Gold (UTC)...")
        time.sleep(sleep_time)
        run_gold_analytics()

threading.Thread(target=analytics_scheduler_loop, daemon=True).start()

# ------------------------------------------------------------------
# REST API ENDPOINTS FOR CTRADER / CLIENTS
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "active", "service": "Gold 3H SMC Analytics Engine"})

@app.route('/scalp_signal', methods=['GET'])
@app.route('/signal', methods=['GET'])
def get_signal():
    """Выдача текущего актуального сигнала"""
    with lock:
        signal = load_signal_from_file()
        current_time = int(time.time())
        signal_timestamp = signal.get("timestamp", 0)

        if signal.get("action") != "NONE" and (current_time - signal_timestamp) <= SIGNAL_LIFETIME_SECONDS:
            signal["status"] = "NEW"
        else:
            signal["status"] = "EXPIRED"
            signal["action"] = "NONE"

        return jsonify(signal), 200

@app.route('/scalp_ack', methods=['POST'])
@app.route('/ack', methods=['POST'])
def acknowledge_signal():
    """Логирование подтверждения приема сигнала клиентом"""
    data = request.get_json(silent=True) or {}
    print(f"👍 [ACK] Сигнал подтвержден клиентом: {data.get('client_id', request.remote_addr)}")
    return jsonify({"status": "acknowledged"}), 200

@app.route('/force_analytics', methods=['GET', 'POST'])
def force_analytics():
    """Ручной принудительный запуск анализа"""
    run_gold_analytics()
    with lock:
        return jsonify({"message": "Принудительный 3H анализ выполнен", "signal": load_signal_from_file()}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
