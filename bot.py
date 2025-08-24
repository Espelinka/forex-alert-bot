import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ----------------------
# Load configuration
# ----------------------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=False)

API_TOKEN = os.getenv("API_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "America/New_York").strip()  # ForexFactory по умолчанию нередко ET
CURRENCIES = [c.strip().upper() for c in os.getenv("CURRENCIES", "USD,GBP,EUR").split(",") if c.strip()]
POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "10"))
LEAD_MINUTES = int(os.getenv("LEAD_MINUTES", "15"))

if not API_TOKEN or not CHAT_ID:
    raise SystemExit("API_TOKEN/CHAT_ID не заданы. Укажите их в .env или как переменные окружения.")

try:
    tz = ZoneInfo(TIMEZONE)
except Exception:
    tz = ZoneInfo("UTC")

URL = "https://www.forexfactory.com/calendar"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("forex-bot")

bot = Bot(token=API_TOKEN)
scheduler = AsyncIOScheduler(timezone=tz)

# Хранилище запланированных уведомлений (чтобы не дублировать)
SCHEDULED_IDS: set[str] = set()

TIME_PATTERNS = [
    "%I:%M%p",  # 8:30am
    "%I%p",     # 8am
]

def parse_forex_calendar():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'DNT': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'TE': 'trailers'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        # ... остальной код
    except Exception as e:
        print(f"Ошибка: {e}")
        return []

def _cell_text(el) -> str:
    if not el:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()

def _row_has_high_impact(row) -> bool:
    # Пытаемся детектить 'красные' новости по возможным классам/текстам.
    impact_el = row.select_one(
        ".calendar__impact-icon--high, .impact__icon--high, .impact.high, td.impact span.high, .ff-impact--high"
    )
    if impact_el:
        return True
    impact_cell = row.select_one(".calendar__impact, td.impact")
    if impact_cell and "high" in impact_cell.get_text(" ", strip=True).lower():
        return True
    return False

def fetch_events() -> list[dict]:
    # Парсит ForexFactory Calendar и возвращает список событий за сегодня:
    # [ {id, currency, title, event_dt, forecast, previous}, ... ]
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ForexAlertBot/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    resp = requests.get(URL, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    events: list[dict] = []
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    rows = soup.select("tr.calendar__row, tr.calendar_row, tr.calendar-row, tr")
    for row in rows:
        if not _row_has_high_impact(row):
            continue

        currency = _cell_text(row.select_one(".calendar__currency, td.currency, .currency"))
        if not currency or currency.upper() not in CURRENCIES:
            continue

        title = _cell_text(row.select_one(".calendar__event-title, td.event, .event"))
        if not title:
            continue

        time_str = _cell_text(row.select_one(".calendar__time, td.time, .time"))
        event_dt = _parse_time_to_dt(time_str, base_date=today)
        if event_dt is None:
            continue

        forecast = _cell_text(row.select_one(".calendar__forecast, td.forecast, .forecast")) or "—"
        previous = _cell_text(row.select_one(".calendar__previous, td.previous, .previous")) or "—"

        event_id = f"{event_dt.isoformat()}|{currency}|{title}".lower()

        events.append({
            "id": event_id,
            "currency": currency.upper(),
            "title": title,
            "event_dt": event_dt,
            "forecast": forecast,
            "previous": previous,
        })

    return events

async def notify(event: dict):
    text = (
        f"⚠️ Через {LEAD_MINUTES} мин выйдет новость по <b>{event['currency']}</b>\n\n"
        f"<b>{event['title']}</b>\n"
        f"⏰ Время выхода: <b>{event['event_dt'].strftime('%H:%M')}</b>\n"
        f"📊 Прогноз: <b>{event['forecast']}</b>\n"
        f"📉 Предыдущее: <b>{event['previous']}</b>"
    )
    try:
        await bot.send_message(int(CHAT_ID), text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.exception("Ошибка отправки в Telegram: %s", e)

def schedule_notifications(events: list[dict]):
    now = datetime.now(tz)
    for ev in events:
        notify_at = ev["event_dt"] - timedelta(minutes=LEAD_MINUTES)
        if notify_at <= now:
            continue
        if ev["id"] in SCHEDULED_IDS:
            continue
        scheduler.add_job(
            notify,
            "date",
            run_date=notify_at,
            args=[ev],
            id=ev["id"],
            misfire_grace_time=60,
            coalesce=True,
        )
        SCHEDULED_IDS.add(ev["id"])
        logger.info("Запланировано: %s @ %s", ev["title"], notify_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"))

async def poll_and_schedule():
    try:
        events = await asyncio.to_thread(fetch_events)
    except Exception as e:
        logger.exception("Ошибка загрузки календаря: %s", e)
        return
    schedule_notifications(events)

async def main():
    logger.info("Старт бота. TZ=%s; CURRENCIES=%s; POLL_INTERVAL_MIN=%s; LEAD_MINUTES=%s",
                TIMEZONE, ",".join(CURRENCIES), POLL_INTERVAL_MIN, LEAD_MINUTES)
    await poll_and_schedule()
    scheduler.add_job(poll_and_schedule, "interval", minutes=POLL_INTERVAL_MIN, next_run_time=None)
    scheduler.start()
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
