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
SYMBOL_SIGNAL = "XAUUSD"
SIGNAL_FILE = "/tmp/gold_analytics_signal.json"
SIGNAL_LIFETIME_SECONDS = 10800  # Сигнал активен 3 часа (10800 сек)

# Параметры риск-менеджмента для Gold (XAUUSD)
RISK_REWARD_RATIO = 2.0
MIN_SL_DIST = 5.0    # Минимальный Стоп-Лосс ($5)
MAX_SL_DIST = 15.0   # Максимальный Стоп-Лосс ($15)
MIN_TP_DIST = 10.0   # Минимальный Тейк-Профит ($10)
MAX_TP_DIST = 45.0   # Максимальный Тейк-Профит ($45)

EMPTY_SIGNAL = {
    "symbol": SYMBOL_SIGNAL,
    "action": "NONE",
    "entry": 0.0,
    "sl": 0.0,
    "tp": 0.0,
    "status": "NONE",
    "timestamp": 0
}

lock = threading.Lock()
MSK_TZ = timezone(timedelta(hours=3))

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
# ЗАГРУЗКА ДАННЫХ ИЗ TWELVEDATA API (С РЕТРАЯМИ И УВЕЛИЧЕННЫМ ТАЙМАУТОМ)
# ------------------------------------------------------------------
def fetch_tf_data(interval, retries=3):
    """Загрузка таймфрейма через TwelveData с повторными попытками при таймауте"""
    url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL}&interval={interval}&outputsize=40&apikey={TWELVE_DATA_API_KEY}"
    headers = {'User-Agent': 'Mozilla/5.0'}

    for attempt in range(1, retries + 1):
        try:
            # Увеличен таймаут: 5 сек подключение, 15 сек чтение
            res = requests.get(url, headers=headers, timeout=(5, 15)).json()
            
            if "values" not in res:
                print(f"[-] Ошибка TwelveData на {interval} (попытка {attempt}/{retries}): {res.get('message', 'No values')}")
                if attempt < retries:
                    time.sleep(2)
                    continue
                return interval, None
            
            df = pd.DataFrame(res["values"])
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
            df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
            # Разворачиваем порядок хронологически (от старых к новым)
            df.iloc[:] = df.iloc[::-1].values
            return interval, df

        except Exception as e:
            print(f"[-] Исключение при загрузке {interval} (попытка {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2)

    return interval, None

def get_multi_tf_market_data():
    """Последовательная загрузка H4, H1 и M15"""
    intervals = ["4h", "1h", "15min"]
    dfs = {}
    
    for interval in intervals:
        _, df = fetch_tf_data(interval)
        if df is None:
            return None, None, None
        dfs[interval] = df
        time.sleep(1.2) # Пауза 1.2 сек для защиты лимитов API

    return dfs["4h"], dfs["1h"], dfs["15min"]

# ------------------------------------------------------------------
# SMC АНАЛИТИКА И ГЕНЕРАЦИЯ СИГНАЛА (3h CYCLE)
# ------------------------------------------------------------------
def run_gold_analytics():
    """Основной анализ рынка XAUUSD по SMC концепции"""
    now_msk = datetime.now(MSK_TZ)
    print(f"🔍 [{now_msk.strftime('%Y-%m-%d %H:%M:%S MSK')}] Старт 3-часового SMC анализа Gold (H4, H1, M15)...")

    df_h4, df_h1, df_m15 = get_multi_tf_market_data()
    if df_h4 is None or df_h1 is None or df_m15 is None:
        print("[-] Ошибка: Не удалось получить данные от TwelveData. Пропуск цикла.")
        return

    curr_price = round(float(df_m15['Close'].iloc[-1]), 2)

    # 1. Тренд H4 и H1
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
    if is_bullish and float(last_m15['Low']) < swing_low and float(last_m15['Close']) > swing_low:
        action = "BUY"
        reason = "SMC Liquidity Sweep Low (снятие продавцов на M15 в направлении H4/H1 тренда)"
        raw_sl = float(last_m15['Low']) - 0.5
        sl_dist = max(MIN_SL_DIST, min(curr_price - raw_sl, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        sl = round(curr_price - sl_dist, 2)
        tp = round(curr_price + tp_dist, 2)

    elif not is_bullish and float(last_m15['High']) > swing_high and float(last_m15['Close']) < swing_high:
        action = "SELL"
        reason = "SMC Liquidity Sweep High (снятие покупателей на M15 в направлении H4/H1 тренда)"
        raw_sl = float(last_m15['High']) + 0.5
        sl_dist = max(MIN_SL_DIST, min(raw_sl - curr_price, MAX_SL_DIST))
        tp_dist = max(MIN_TP_DIST, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_DIST))
        sl = round(curr_price + sl_dist, 2)
        tp = round(curr_price - tp_dist, 2)

    elif is_bullish:
        action = "BUY"
        reason = "3-Часовой трендовый импульс H4/H1"
        sl = round(curr_price - 7.5, 2)
        tp = round(curr_price + 15.0, 2)
    else:
        action = "SELL"
        reason = "3-Часовой трендовый импульс H4/H1"
        sl = round(curr_price + 7.5, 2)
        tp = round(curr_price - 15.0, 2)

    # Сохраняем сигнал для cTrader (timestamp UTC)
    signal = {
        "symbol": SYMBOL_SIGNAL,
        "action": action,
        "entry": curr_price,
        "sl": sl,
        "tp": tp,
        "status": "NEW",
        "timestamp": int(datetime.now(timezone.utc).timestamp())
    }

    with lock:
        save_signal_to_file(signal)

    # Отправка отчета в Telegram со временем MSK
    msg = (
        f"🏆 <b>GOLD SMC ANALYTICS REPORT (3H)</b> 🏆\n\n"
        f"<b>Инструмент:</b> XAU/USD (Gold)\n"
        f"<b>Макро-Тренд (H4/H1):</b> {trend_str}\n"
        f"<b>Рекомендация:</b> <b>{action}</b>\n\n"
        f"📍 <b>Вход:</b> <code>{curr_price}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit:</b> <code>{tp}</code>\n\n"
        f"💡 <b>Обоснование:</b> {reason}\n"
        f"🕒 <i>Время анализа: {now_msk.strftime('%H:%M:%S MSK')}</i>"
    )
    send_telegram(msg)
    print(f"[+] Аналитика завершена. Сигнал: {action} @ {curr_price}")

# ------------------------------------------------------------------
# РАСПИСАНИЕ XAUUSD (СТРОГО ПО МЕТКАМ ЧАСОВ В UTC)
# UTC: 01:00, 04:00, 07:00, 10:00, 13:00, 16:00, 19:00, 22:00
# MSK: 04:00, 07:00, 10:00, 13:00, 16:00, 19:00, 22:00, 01:00
# ------------------------------------------------------------------
def get_seconds_until_next_3h_mark():
    schedule_hours_utc = [1, 4, 7, 10, 13, 16, 19, 22]
    now_utc = datetime.now(timezone.utc)

    for h in schedule_hours_utc:
        target_time = now_utc.replace(hour=h, minute=0, second=10, microsecond=0)
        if target_time > now_utc:
            return (target_time - now_utc).total_seconds()

    # Следующая метка — 01:00:10 UTC завтра
    target_time = (now_utc + timedelta(days=1)).replace(hour=1, minute=0, second=10, microsecond=0)
    return (target_time - now_utc).total_seconds()

def analytics_scheduler_loop():
    """Синхронизированный цикл анализа без старта при запуске"""
    while True:
        sleep_time = get_seconds_until_next_3h_mark()
        hours_wait = round(sleep_time / 3600, 2)
        print(f"⏳ Ждём {hours_wait} ч. ({int(sleep_time)} сек.) до следующего планового анализа по расписанию...")
        
        time.sleep(sleep_time)
        
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() < 5:  # Пн-Пт
            run_gold_analytics()
        else:
            print(f"⏸️ Выходной день (UTC {now_utc.strftime('%A')}). Анализ XAUUSD пропущен.")

threading.Thread(target=analytics_scheduler_loop, daemon=True).start()

# ------------------------------------------------------------------
# REST API ENDPOINTS FOR CTRADER / CLIENTS
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "active", "symbol": SYMBOL_SIGNAL, "service": "Gold 3H SMC Analytics Engine"})

@app.route('/scalp_signal', methods=['GET'])
@app.route('/signal', methods=['GET'])
def get_signal():
    """Выдача текущего актуального сигнала cBot"""
    with lock:
        signal = load_signal_from_file().copy()
        current_time = int(datetime.now(timezone.utc).timestamp())
        signal_timestamp = signal.get("timestamp", 0)
        age = current_time - signal_timestamp

        if signal.get("action") != "NONE" and age <= SIGNAL_LIFETIME_SECONDS:
            signal["status"] = "NEW"
            signal["age_seconds"] = age
        else:
            signal["status"] = "EXPIRED"
            signal["action"] = "NONE"
            signal["age_seconds"] = age if signal_timestamp > 0 else 0

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
