import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime, timedelta
import pytz
from telegram import Bot
import asyncio
import re
import os

# =============== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===============
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
CHECK_INTERVAL = 900  # Проверять каждые 15 минут (900 секунд)
ALERT_BEFORE = 900   # Уведомлять за 15 минут
FOREX_TZ = pytz.timezone('Etc/GMT-3')
LOCAL_TZ = pytz.timezone('Europe/Moscow')
# =============================================================

bot = Bot(token=TELEGRAM_TOKEN)
url = "https://www.forexfactory.com/calendar"
notified_events = set()

def parse_forex_calendar():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        events = []
        rows = soup.select('tr.calendar__row.calendar__row-event')

        for row in rows:
            date_cell = row.find('td', class_='calendar__cell calendar__cell--date')
            if not date_cell:
                continue
            date_str = date_cell.get_text(strip=True)

            time_cell = row.find('td', class_='calendar__cell calendar__cell--time')
            time_str = time_cell.get_text(strip=True) if time_cell else None
            if not time_str or time_str.lower() == 'all day':
                continue

            currency_cell = row.find('td', class_='calendar__cell calendar__cell--currency')
            currency = currency_cell.get_text(strip=True) if currency_cell else 'N/A'

            event_cell = row.find('td', class_='calendar__cell calendar__cell--event')
            event_name = event_cell.get_text(strip=True) if event_cell else 'N/A'

            impact_cell = row.find('td', class_=re.compile(r'calendar__cell--impact'))
            if not impact_cell or 'impact--high' not in impact_cell.get('class', ''):
                continue

            try:
                dt_str = f"{date_str} {time_str}"
                naive_dt = datetime.strptime(dt_str, '%a %b %d %I:%M%p')
                naive_dt = naive_dt.replace(year=datetime.now().year)
                aware_dt = FOREX_TZ.localize(naive_dt)
                event_time_utc = aware_dt.astimezone(pytz.utc)
                events.append({
                    'name': event_name,
                    'currency': currency,
                    'time': event_time_utc,
                    'local_time': event_time_utc.astimezone(LOCAL_TZ),
                    'id': f"{date_str}_{time_str}_{event_name[:30]}"
                })
            except Exception as e:
                continue

        return events
    except Exception as e:
        print(f"Ошибка: {e}")
        return []

async def send_telegram_alert(event):
    local_time = event['local_time'].strftime('%d %b %H:%M')
    message = (
        f"🔴 <b>High Impact News Soon!</b>\n"
        f"📅 <b>{local_time}</b>\n"
        f"💱 <b>{event['currency']}</b>\n"
        f"🗞️ <b>{event['name']}</b>\n"
        f"⏰ Через 15 минут!"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='HTML')
    except Exception as e:
        print(f"Ошибка отправки: {e}")

async def main():
    print("Бот запущен и начинает проверку...")
    while True:
        try:
            events = parse_forex_calendar()
            now_utc = datetime.now(pytz.utc)
            alert_time = now_utc + timedelta(seconds=ALERT_BEFORE)

            for event in events:
                if now_utc <= event['time'] <= alert_time:
                    if event['id'] not in notified_events:
                        await send_telegram_alert(event)
                        notified_events.add(event['id'])

        except Exception as e:
            print(f"Ошибка: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    asyncio.run(main())