"""
Мониторинг свободных билетов на Gaztelugatxe (public.tiketa.eus).

При обнаружении достаточного числа свободных мест в подходящем временном
окне на целевую дату — отправляет уведомление в Telegram. Уже отправленные
уведомления запоминаются в notify_state.json, чтобы не спамить одним и тем
же результатом.

Сайт использует WordPress-плагин Bookly. Форма в 2 шага:
  1. "Solicitud" — тип брони (select) + число персон (select) + кнопка
     ".bookly-js-next-step" ("SIGUIENTE").
  2. "Hora" — календарь (Svelte-виджет, класс ".bookly-js-slot-calendar")
     с помесячной навигацией; клик по дню либо показывает список слотов
     (кнопки ".bookly-hour", каждая содержит время и "N plazas"), либо
     всплывает попап "#custom-popup-box" с текстом "AFORO COMPLETADO"
     (мест нет вообще).

Конфиг — через переменные окружения (см. ниже) или CLI-флаги (перекрывают
переменные окружения, удобно для локальной отладки).
"""

import argparse
import os
import re
import json
import sys
from datetime import datetime, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

# ---------- НАСТРОЙКИ ПО УМОЛЧАНИЮ (переопределяются env / CLI) ----------

TARGET_URL = "https://public.tiketa.eus/gaztelugatxe/?tipore=2"

DEFAULT_TARGET_DATE = "2026-08-28"
DEFAULT_TIME_WINDOWS = "15:30-16:30,17:50-18:30"
DEFAULT_PEOPLE_NEEDED = 3

STATE_FILE = "notify_state.json"
DEBUG_DIR = Path("debug")

# Рабочие часы проверки (по Мадриду) — скрипт сам "выходит" за пределы, ничего не делая.
# Не применяется в --dry-run (там нужно проверить парсинг в любое время).
ACTIVE_FROM = dtime(8, 0)
ACTIVE_TO = dtime(20, 0)

MONTHS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

NAV_TIMEOUT_MS = 20000

# ---------- TELEGRAM ----------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы, пропускаю отправку")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    if resp.status_code != 200:
        print(f"[!] Ошибка отправки в Telegram: {resp.status_code} {resp.text}")


# ---------- STATE (чтобы не слать одно и то же повторно) ----------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- ВРЕМЕННОЕ ОКНО РАБОТЫ СКРИПТА ----------

def within_active_hours() -> bool:
    now = datetime.now(ZoneInfo("Europe/Madrid")).time()
    return ACTIVE_FROM <= now <= ACTIVE_TO


def in_target_window(slot_time: dtime, windows: list[tuple[dtime, dtime]]) -> bool:
    return any(start <= slot_time <= end for start, end in windows)


def parse_time_windows(raw: str) -> list[tuple[dtime, dtime]]:
    windows = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start_str, end_str = chunk.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        windows.append((dtime(sh, sm), dtime(eh, em)))
    return windows


# ---------- DEBUG DUMP ----------

def save_debug(page: Page, name: str):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = DEBUG_DIR / f"{name}_{stamp}.png"
        html_path = DEBUG_DIR / f"{name}_{stamp}.html"
        page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"[!] Debug-дамп сохранён: {screenshot_path}, {html_path}")
    except Exception as e:
        print(f"[!] Не удалось сохранить debug-дамп: {e}")


# ---------- ШАГ 1: ТИП БРОНИ + ЧИСЛО ПЕРСОН ----------

def fill_step1(page: Page, people_needed: int):
    # Куки-баннер — не всегда появляется, поэтому короткий таймаут и мягкий пропуск.
    try:
        page.get_by_role("button", name="Aceptar").click(timeout=4000)
    except PlaywrightTimeoutError:
        pass

    # Видимых select ровно два на шаге 1: тип брони, затем число персон.
    # (Есть ещё скрытые "теневые" select Bookly и select времени из другого
    # блока формы — они не видны и не должны использоваться.)
    visible_selects = page.locator("select:visible")
    visible_selects.first.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)

    type_select = visible_selects.nth(0)
    people_select = visible_selects.nth(1)

    # Оба option'а "Individual"/"Grupal" в этом select имеют одинаковый
    # value="1" — выбираем по индексу (0 = плейсхолдер, 1 = Individual),
    # а не по value/label, чтобы не зависеть от точного текста/дефисов.
    type_select.select_option(index=1)
    people_select.select_option(str(people_needed))

    page.locator(".bookly-js-next-step").click(timeout=NAV_TIMEOUT_MS)

    # Ждём реальной загрузки шага 2 (календаря), а не фиксированный timeout.
    page.locator(".bookly-js-slot-calendar").wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    page.locator(".bookly-calendar-middle-button-mark span").wait_for(state="visible", timeout=NAV_TIMEOUT_MS)


# ---------- ШАГ 2: КАЛЕНДАРЬ ----------

def current_month_label(page: Page) -> str:
    return page.locator(".bookly-calendar-middle-button-mark span").inner_text().strip()


def navigate_to_month(page: Page, target_label: str, max_clicks: int = 24) -> bool:
    left_btn = page.locator(".bookly-calendar-left-button-mark")
    right_btn = page.locator(".bookly-calendar-right-button-mark")

    for _ in range(max_clicks):
        current = current_month_label(page)
        if current == target_label:
            return True

        cur_idx = MONTHS_ES.index(current.split(" ")[0]) + int(current.split(" ")[1]) * 12
        tgt_idx = MONTHS_ES.index(target_label.split(" ")[0]) + int(target_label.split(" ")[1]) * 12
        btn = right_btn if tgt_idx > cur_idx else left_btn

        if btn.get_attribute("disabled") == "true":
            # Уткнулись в границу диапазона бронирования раньше цели.
            return False

        btn.click()
        # Ждём, пока подпись месяца реально сменится, а не фиксированную паузу.
        try:
            page.wait_for_function(
                """(prev) => {
                    const el = document.querySelector('.bookly-calendar-middle-button-mark span');
                    return el && el.textContent.trim() !== prev;
                }""",
                arg=current,
                timeout=5000,
            )
        except PlaywrightTimeoutError:
            pass

    return current_month_label(page) == target_label


def get_slots_for_day(page: Page, day: int) -> list[tuple[dtime, int]]:
    """
    Кликает по числу дня в календаре (только активная дата ТЕКУЩЕГО месяца —
    у ячеек соседних месяцев нет класса bookly-calendar-current-month-mark)
    и возвращает список (время, свободных мест) из правой панели.

    Если день полностью забронирован, сайт показывает попап "AFORO
    COMPLETADO" вместо списка слотов — в этом случае возвращается [].
    """
    # Вёрстка поменялась (2026-08-16): число дня теперь лежит не текстом
    # прямо в ячейке, а завёрнуто в два вложенных <span>, и рядом появился
    # пустой <span class="bookly-popup-catcher">. Из-за этого textContent
    # ячейки стал не "28", а " 28" с пробелами, и фильтр по регулярке
    # ^28$ перестал совпадать — скрипт 20 секунд ждал ячейку, которая была
    # прямо перед ним, и падал по таймауту. Поэтому сравниваем сами, по
    # обрезанному тексту, а не регуляркой по сырому содержимому.
    cells = page.locator('[class*="bookly-calendar-current-month-mark"]')
    cells.first.wait_for(state="attached", timeout=NAV_TIMEOUT_MS)

    day_cell = None
    for i in range(cells.count()):
        cell = cells.nth(i)
        if (cell.inner_text() or "").strip() == str(day):
            day_cell = cell
            break

    if day_cell is None:
        raise RuntimeError(
            f"В календаре {current_month_label(page)} нет ячейки дня {day} — "
            f"похоже, вёрстка страницы снова изменилась."
        )

    # День без свободных мест сайт отдаёт как обычную ячейку, но с
    # disabled="true" и pointer-events-none: кликнуть по нему нельзя.
    # Это НЕ поломка — это ответ «мест нет», и относиться к нему надо
    # так же, как к попапу AFORO COMPLETADO ниже. Раньше скрипт этого не
    # различал, честно кликал в никуда и падал с таймаутом.
    css_class = day_cell.get_attribute("class") or ""
    if day_cell.get_attribute("disabled") == "true" or "pointer-events-none" in css_class:
        print(f"День {day} в календаре неактивен — свободных мест на эту дату нет.")
        return []

    day_cell.click()

    popup = page.locator("#custom-popup-box")
    hour_btn = page.locator("button.bookly-hour")

    # Явно ждём ОДИН из двух возможных исходов, а не фиксированный timeout.
    try:
        page.wait_for_function(
            """() => {
                const popup = document.querySelector('#custom-popup-box');
                const popupVisible = popup && popup.getBoundingClientRect().width > 0;
                const hour = document.querySelector('button.bookly-hour');
                const hourVisible = hour && hour.getBoundingClientRect().width > 0;
                return !!(popupVisible || hourVisible);
            }""",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"После клика по дню {day} не появился ни список слотов, ни попап "
            f"«AFORO COMPLETADO» — похоже, вёрстка страницы изменилась."
        )

    if popup.count() > 0 and popup.bounding_box():
        text = popup.inner_text()
        # Закрываем попап, чтобы не мешал следующей проверке в этом же запуске.
        try:
            popup.get_by_text("×", exact=True).click(timeout=3000)
        except PlaywrightTimeoutError:
            pass
        if "AFORO COMPLETADO" in text.upper() or "COMPLETADO" in text.upper():
            return []
        # Неизвестный попап — не молчим, поднимаем наверх для диагностики.
        raise RuntimeError(f"Неожиданный попап при выборе дня {day}: {text.strip()!r}")

    # Есть слоты — собираем все, пролистывая карусель "MÁS TURNOS" вперёд,
    # пока кнопка не станет недоступна или не перестанут появляться новые часы.
    slots: dict[str, int] = {}
    next_btn = page.locator("button.bookly-time-next")

    for _ in range(20):
        for btn in page.locator("button.bookly-hour").all():
            text = btn.inner_text()
            m_time = re.search(r"(\d{1,2}:\d{2})", text)
            m_plazas = re.search(r"(\d+)\s*plazas", text)
            if m_time and m_plazas:
                slots[m_time.group(1)] = int(m_plazas.group(1))

        if next_btn.count() == 0:
            break
        if next_btn.get_attribute("disabled") is not None:
            break
        before = set(slots.keys())
        next_btn.click()
        page.wait_for_timeout(400)
        after_texts = [b.inner_text() for b in page.locator("button.bookly-hour").all()]
        new_times = {m.group(1) for t in after_texts if (m := re.search(r"(\d{1,2}:\d{2})", t))}
        if new_times <= before:
            # Карусель зациклилась / больше нового ничего не показывает.
            break

    return sorted(
        (dtime(*map(int, t.split(":"))), plazas) for t, plazas in slots.items()
    )


# ---------- ОСНОВНАЯ ЛОГИКА ----------

def check_tickets(target_date: date, windows: list[tuple[dtime, dtime]],
                   people_needed: int, dry_run: bool):
    if not dry_run and not within_active_hours():
        print("Вне рабочего окна 08:00-20:00 (Europe/Madrid), пропускаю проверку.")
        return

    state = load_state()
    found_any = False
    target_month_label = f"{MONTHS_ES[target_date.month - 1]} {target_date.year}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            fill_step1(page, people_needed)

            if not navigate_to_month(page, target_month_label):
                print(f"[!] Не удалось долистать календарь до {target_month_label} "
                      f"(упёрлись в границу доступных для брони месяцев).")
                save_debug(page, "calendar_month_not_reached")
                browser.close()
                return

            slots = get_slots_for_day(page, target_date.day)
        except Exception as e:
            print(f"[!] Ошибка во время работы со страницей: {e}")
            save_debug(page, "scrape_error")
            browser.close()
            raise

        browser.close()

    if dry_run:
        print(f"Слоты на {target_date.isoformat()} ({target_month_label}, {people_needed} чел.):")
        if not slots:
            print("  (свободных слотов нет / день полностью забронирован)")
        for slot_time, plazas in slots:
            marker = " <-- в целевом окне" if in_target_window(slot_time, windows) else ""
            print(f"  {slot_time.strftime('%H:%M')} — {plazas} plazas{marker}")
        return

    for slot_time, plazas in slots:
        if not in_target_window(slot_time, windows):
            continue
        print(f"Слот {slot_time} — {plazas} plazas")
        if plazas >= people_needed:
            key = f"{target_date.isoformat()}-{slot_time.strftime('%H:%M')}"
            already = state.get(key)
            if already == plazas:
                continue
            found_any = True
            state[key] = plazas
            send_telegram(
                "🎟 Gaztelugatxe: появились места!\n"
                f"{target_date.strftime('%d.%m.%Y')}, {slot_time.strftime('%H:%M')} — "
                f"{plazas} свободных мест (нужно минимум {people_needed}).\n"
                f"Бронировать: {TARGET_URL}"
            )

    if found_any:
        save_state(state)
    else:
        print("Подходящих слотов пока нет.")


def env_or(name: str, default):
    """
    Пустая строка — это «не задано» (2026-08-16).

    Workflow передаёт настройки как `TARGET_DATE: ${{ vars.TARGET_DATE }}`.
    Если переменной в репозитории нет, Actions подставляет не «ничего», а
    ПУСТУЮ СТРОКУ — переменная окружения существует и равна "". Поэтому
    os.environ.get(name, default) возвращал "" вместо значения по
    умолчанию, и скрипт падал на int("") ещё до первого обращения к сайту.

    Это же ломало и старый монитор: 10 августа после теста переменную
    TARGET_DATE удалили, рассчитывая, что скрипт вернётся к дате по
    умолчанию, — а он с тех пор падал на каждом запуске.
    """
    value = os.environ.get(name, "")
    return value.strip() or default


def parse_args():
    parser = argparse.ArgumentParser(description="Мониторинг билетов Gaztelugatxe")
    parser.add_argument("--target-date", default=env_or("TARGET_DATE", DEFAULT_TARGET_DATE),
                         help="Дата в формате YYYY-MM-DD")
    parser.add_argument("--time-windows", default=env_or("TIME_WINDOWS", DEFAULT_TIME_WINDOWS),
                         help='Например "15:30-16:30,17:50-18:30"')
    parser.add_argument("--people", type=int,
                         default=int(env_or("PEOPLE_NEEDED", DEFAULT_PEOPLE_NEEDED)),
                         help="Сколько мест нужно найти минимум")
    parser.add_argument("--dry-run", action="store_true",
                         help="Только печатает найденные слоты, без отправки в Telegram")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        target_date = date.fromisoformat(args.target_date)
        time_windows = parse_time_windows(args.time_windows)
        check_tickets(target_date, time_windows, args.people, args.dry_run)
    except Exception as e:
        print(f"[!] Ошибка выполнения: {e}")
        sys.exit(1)
