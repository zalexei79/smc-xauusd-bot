import os
import time
import json
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request
import pandas as pd
import requests

app = Flask(__name__)

# ------------------------------------------------------------------
# НАСТРОЙКИ СИСТЕМЫ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "c997ad22987e477e83034ea132621542")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8726797778:AAFzn_Fo7vS4PXqg3XG7RJpEBZ-AMkIaYM4")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@Gbpusdbotanalitic")

SYMBOL_TWELVE = "GBP/USD"
SYMBOL_SIGNAL = "GBPUSD"
SIGNAL_FILE = "/tmp/gbp_analytics_signal.json"
SIGNAL_LIFETIME_SECONDS = 10800  # Сигнал активен 3 часа (10800 сек)

# Параметры Риск/Прибыль для GBPUSD (в пипсах)
RISK_REWARD_RATIO = 2.5
MIN_SL_PIPS = 0.0015   # 15 пипсов
MAX_SL_PIPS = 0.0040   # 40 пипсов
MIN_TP_PIPS = 0.0030   # 30 пипсов
MAX_TP_PIPS = 0.0120   # 120 пипсов

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

# ------------------------------------------------------------------
# РАБОТА С ФАЙЛОМ СИГНАЛА
# ------------------------------------------------------------------
def save_signal_to_file(signal_data):
    try:
        with open(SIGNAL_FILE, "w") as f:
            json.dump(signal_data, f)
    except Exception as e:
        print(f"[-] Ошибка записи сигнала GBPUSD: {e}")

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
    if not BOT_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False

# ------------------------------------------------------------------
# ЗАГРУЗКА ДАННЫХ ИЗ TWELVEDATA API
# ------------------------------------------------------------------
def fetch_tf_data(interval):
    """Загрузка конкретного таймфрейма через TwelveData"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.twelvedata.com/time_series?symbol={SYMBOL_TWELVE}&interval={interval}&outputsize=50&apikey={TWELVE_DATA_API_KEY}"
        
        res = requests.get(url, headers=headers, timeout=10).json()
        
        if "values" not in res:
            print(f"[-] Ошибка TwelveData ({interval}): {res.get('message', 'No values')}")
            return interval, None
        
        df = pd.DataFrame(res["values"])
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'}, inplace=True)
        # Разворачиваем порядок хронологически (от старых к новым)
        df.iloc[:] = df.iloc[::-1].values
        return interval, df
    except Exception as e:
        print(f"❌ Исключение при загрузке ({interval}): {e}")
        return interval, None

def get_multi_tf_market_data():
    """Параллельная загрузка H4, H1 и M15 для GBPUSD"""
    intervals = ["4h", "1h", "15min"]
    dfs = {}
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(fetch_tf_data, intervals)
        for interval, df in results:
            if df is None:
                return None, None, None
            dfs[interval] = df

    return dfs["4h"], dfs["1h"], dfs["15min"]

# ------------------------------------------------------------------
# SMC ИНДИКАТОРЫ И СТРАТЕГИЯ ДЛЯ GBPUSD
# ------------------------------------------------------------------
def run_gbp_analytics():
    """Основной 3-часовой анализ рынка GBP/USD"""
    now_utc = datetime.now(timezone.utc)
    print(f"🔍 [{now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}] Запуск 3H аналитики GBP/USD...")

    df_h4, df_h1, df_m15 = get_multi_tf_market_data()
    if df_h4 is None or df_h1 is None or df_m15 is None:
        print("[-] Ошибка: Не удалось загрузить свечи TwelveData. Пропуск.")
        return

    curr_price = round(float(df_m15['Close'].iloc[-1]), 5)

    # 1. Тренд H4 и H1
    h4_ema = df_h4['Close'].tail(20).mean()
    h1_ema = df_h1['Close'].tail(20).mean()
    
    is_bullish = curr_price >= h4_ema and curr_price >= h1_ema
    trend_str = "🟢 BULLISH (Бычий)" if is_bullish else "🔴 BEARISH (Медвежий)"

    # 2. Поиск свинг-уровней ликвидности на M15
    recent_m15 = df_m15.iloc[-24:-1]
    last_m15 = df_m15.iloc[-1]
    
    swing_high = float(recent_m15['High'].max())
    swing_low = float(recent_m15['Low'].min())

    action = "NONE"
    reason = ""
    sl, tp = 0.0, 0.0

    # 3. Логика SMC Liquidity Sweep & Trend Follow
    if is_bullish and float(last_m15['Low']) < swing_low and float(last_m15['Close']) > swing_low:
        action = "BUY"
        reason = "SMC Sweep Low (Снятие ликвидности продавцов на M15 по тренду H4/H1)"
        raw_sl = float(last_m15['Low']) - 0.0003
        sl_dist = max(MIN_SL_PIPS, min(curr_price - raw_sl, MAX_SL_PIPS))
        tp_dist = max(MIN_TP_PIPS, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_PIPS))
        sl = round(curr_price - sl_dist, 5)
        tp = round(curr_price + tp_dist, 5)

    elif not is_bullish and float(last_m15['High']) > swing_high and float(last_m15['Close']) < swing_high:
        action = "SELL"
        reason = "SMC Sweep High (Снятие ликвидности покупателей на M15 по тренду H4/H1)"
        raw_sl = float(last_m15['High']) + 0.0003
        sl_dist = max(MIN_SL_PIPS, min(raw_sl - curr_price, MAX_SL_PIPS))
        tp_dist = max(MIN_TP_PIPS, min(sl_dist * RISK_REWARD_RATIO, MAX_TP_PIPS))
        sl = round(curr_price + sl_dist, 5)
        tp = round(curr_price - tp_dist, 5)

    elif is_bullish:
        action = "BUY"
        reason = "Вход по трендовому импульсу H4/H1 EMA"
        sl = round(curr_price - 0.0020, 5)
        tp = round(curr_price + (0.0020 * RISK_REWARD_RATIO), 5)
    else:
        action = "SELL"
        reason = "Вход по трендовому импульсу H4/H1 EMA"
        sl = round(curr_price + 0.0020, 5)
        tp = round(curr_price - (0.0020 * RISK_REWARD_RATIO), 5)

    # Сохраняем актуальный сигнал
    signal = {
        "symbol": SYMBOL_SIGNAL,
        "action": action,
        "entry": curr_price,
        "sl": sl,
        "tp": tp,
        "status": "NEW",
        "timestamp": int(now_utc.timestamp())
    }

    with lock:
        save_signal_to_file(signal)

    # Перевод времени в ваше локальное время (MSK: UTC+3)
    user_tz = timezone(timedelta(hours=3))
    now_local = now_utc.astimezone(user_tz)

    # Отправка HTML-сообщения в Telegram
    msg = (
        f"⚡ <b>GBP/USD SMC ANALYTICS REPORT (3H)</b> ⚡\n\n"
        f"<b>Инструмент:</b> GBP/USD\n"
        f"<b>Макро-Тренд (H4/H1):</b> {trend_str}\n"
        f"<b>Рекомендация:</b> <b>{action}</b>\n\n"
        f"📍 <b>Вход:</b> <code>{curr_price}</code>\n"
        f"🛡️ <b>Stop Loss:</b> <code>{sl}</code>\n"
        f"🎯 <b>Take Profit:</b> <code>{tp}</code>\n\n"
        f"💡 <b>Обоснование:</b> {reason}\n"
        f"🕒 <i>Время анализа: {now_local.strftime('%H:%M:%S')} MSK</i>"
    )
    send_telegram(msg)
    print(f"✅ Анализ GBPUSD завершен. Сигнал: {action} @ {curr_price}")

# ------------------------------------------------------------------
# РАСПИСАНИЕ: 1:00, 4:00, 7:00, 10:00, 13:00, 16:00, 19:00, 22:00 (UTC)
# ИСКЛЮЧАЕТ ВЫХОДНЫЕ (СУББОТУ И ВОСКРЕСЕНЬЕ)
# ------------------------------------------------------------------
def get_seconds_until_next_3h_mark():
    """Вычисляет секунды до меток 1:00, 4:00, 7:00, 10:00, 13:00, 16:00, 19:00, 22:00 (UTC)"""
    schedule_hours = [1, 4, 7, 10, 13, 16, 19, 22]
    now = datetime.now(timezone.utc)
    
    # Ищем следующий час в текущих сутках
    next_hour = None
    for h in schedule_hours:
        if h > now.hour or (h == now.hour and now.minute == 0 and now.second < 10):
            next_hour = h
            break
            
    if next_hour is not None:
        target_time = now.replace(hour=next_hour, minute=0, second=10, microsecond=0)
    else:
        # Переход на следующий день на 01:00:10 UTC
        next_day = now + timedelta(days=1)
        target_time = next_day.replace(hour=1, minute=0, second=10, microsecond=0)
        
    sleep_seconds = (target_time - now).total_seconds()
    return max(sleep_seconds, 5)

def analytics_scheduler_loop():
    """Цикл планировщика анализа с учётом выходных"""
    time.sleep(3)
    
    # Запускаем первичный анализ только если сегодня не суббота (5) и не воскресенье (6)
    if datetime.now(timezone.utc).weekday() < 5:
        run_gbp_analytics()

    while True:
        sleep_time = get_seconds_until_next_3h_mark()
        minutes_wait = round(sleep_time / 60, 1)
        print(f"⏳ Ожидание {minutes_wait} мин. ({int(sleep_time)} сек.) до следующей метки (UTC)...")
        time.sleep(sleep_time)
        
        # Проверка на выходные дни перед исполнением аналитики
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() < 5:  # Пн-Пт
            run_gbp_analytics()
        else:
            print(f"⏸️ Выходной день (UTC {now_utc.strftime('%A')}). Анализ пропущен.")

threading.Thread(target=analytics_scheduler_loop, daemon=True).start()

# ------------------------------------------------------------------
# REST API ENDPOINTS ДЛЯ CTRADER И КЛИЕНТОВ
# ------------------------------------------------------------------
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "running", "symbol": SYMBOL_SIGNAL, "service": "GBPUSD 3H Analytics Engine"})

@app.route('/scalp_signal', methods=['GET'])
@app.route('/signal', methods=['GET'])
def get_signal():
    """Раздает актуальный сигнал cBot в течение 3 часов"""
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
    """Логирование подтверждения приема сигнала cBot"""
    data = request.get_json(silent=True) or {}
    client_id = data.get('client_id', request.remote_addr)
    print(f"👍 [ACK] GBPUSD сигнал подтвержден клиентом: {client_id}")
    return jsonify({"status": "acknowledged"}), 200

@app.route('/force_analytics', methods=['GET', 'POST'])
def force_analytics():
    """Принудительный запуск анализа по кнопке/запросу"""
    run_gbp_analytics()
    with lock:
        return jsonify({"message": "Принудительный анализ GBPUSD выполнен", "signal": load_signal_from_file()}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
