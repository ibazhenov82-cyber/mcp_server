"""
mcp_server.cron
==================

Разбор и расчёт расписания БЕЗ внешних зависимостей (в песочнице разработки
недоступны ни `croniter`, ни `apscheduler` — см. README, раздел
"Ограничения окружения разработки"), чистый stdlib (`datetime`).

Формат `schedule` — простой DSL из трёх видов строк (ровно то, что
Android-пресеты "каждые N минут / каждый час / ежедневно HH:MM / свой cron"
формируют и передают как есть):

    "every:<N><unit>"   -- интервал, unit ∈ {s, m, h}, например "every:5m",
                            "every:1h" ("каждые N минут"/"каждый час").
    "daily:HH:MM"       -- ежедневно в это время (24-часовой формат, UTC).
    "<m> <h> <dom> <mon> <dow>" -- обычный 5-полевой cron (стандартная
                            семантика: dow 0 и 7 — воскресенье), любая
                            другая строка. "Свой cron" в UI передаёт это
                            без изменений.

В проде (`scheduler.py`, при установленном `apscheduler`) эти же строки
переводятся в `IntervalTrigger`/`CronTrigger` — см. `to_apscheduler_trigger`.
Здесь же реализован независимый от APScheduler расчёт `next_run_at` (нужен
и для REST-ответов, и для тестов, которые не могут полагаться на
`apscheduler`, недоступный в песочнице разработки)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

_EVERY_RE = re.compile(r"^every:(\d+)(s|m|h)$")
_DAILY_RE = re.compile(r"^daily:([0-2]?\d):([0-5]?\d)$")

_CRON_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),  # 0 и 7 оба означают воскресенье (обычная cron-семантика)
}
_CRON_FIELD_ORDER = ["minute", "hour", "dom", "month", "dow"]

# Ищем следующее срабатывание перебором минут, но не бесконечно — если за
# два года ничего не нашлось, расписание точно некорректно (например,
# "31 2 30 2 *" — 30 февраля не существует).
_MAX_LOOKAHEAD_MINUTES = 366 * 2 * 24 * 60


class ScheduleError(ValueError):
    """Расписание не разобрано / синтаксически некорректно."""


@dataclass
class EverySchedule:
    seconds: int


@dataclass
class DailySchedule:
    hour: int
    minute: int


@dataclass
class CronSchedule:
    # Каждое поле — множество допустимых значений (уже нормализованное:
    # для dow 7 приведено к 0).
    minute: set
    hour: set
    dom: set
    month: set
    dow: set
    raw: str


ParsedSchedule = object  # EverySchedule | DailySchedule | CronSchedule


def _parse_cron_field(raw: str, field: str) -> set:
    lo, hi = _CRON_FIELD_RANGES[field]
    values: set = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"Пустая часть в поле '{field}' cron-выражения")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ScheduleError(f"Некорректный шаг '{step_s}' в поле '{field}'")
            if step <= 0:
                raise ScheduleError(f"Шаг в поле '{field}' должен быть положительным")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ScheduleError(f"Некорректный диапазон '{part}' в поле '{field}'")
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ScheduleError(f"Некорректное значение '{part}' в поле '{field}'")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ScheduleError(f"Значение вне диапазона {lo}-{hi} в поле '{field}': '{part}'")
        for v in range(start, end + 1, step):
            values.add(v)
    if field == "dow":
        # 7 означает то же воскресенье, что и 0 — нормализуем сразу, чтобы
        # дальше сравнивать только с 0..6 (см. compute_next_run).
        values = {0 if v == 7 else v for v in values}
    return values


def parse_schedule(schedule: str) -> ParsedSchedule:
    """Разбирает строку расписания в одно из трёх представлений выше.
    Поднимает `ScheduleError` с понятным сообщением на любой некорректной
    строке — вызывающий код (`store.py`, при регистрации/патче задачи)
    обязан вызывать это ПЕРЕД сохранением, чтобы в БД не попало
    неисполнимое расписание."""
    schedule = (schedule or "").strip()
    if not schedule:
        raise ScheduleError("Расписание не задано")

    m = _EVERY_RE.match(schedule)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise ScheduleError("Интервал должен быть положительным числом")
        seconds = {"s": 1, "m": 60, "h": 3600}[unit] * n
        return EverySchedule(seconds=seconds)

    m = _DAILY_RE.match(schedule)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23):
            raise ScheduleError("Час в 'daily:HH:MM' должен быть от 0 до 23")
        return DailySchedule(hour=hour, minute=minute)

    fields = schedule.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"Не удалось разобрать расписание '{schedule}': ожидается 'every:<N><s|m|h>', "
            "'daily:HH:MM' или 5-полевой cron 'm h dom mon dow'"
        )
    minute, hour, dom, month, dow = (
        _parse_cron_field(fields[0], "minute"),
        _parse_cron_field(fields[1], "hour"),
        _parse_cron_field(fields[2], "dom"),
        _parse_cron_field(fields[3], "month"),
        _parse_cron_field(fields[4], "dow"),
    )
    return CronSchedule(minute=minute, hour=hour, dom=dom, month=month, dow=dow, raw=schedule)


def validate_schedule(schedule: str) -> None:
    """Поднимает `ScheduleError`, если расписание некорректно; ничего не
    возвращает (удобно вызывать просто ради проверки)."""
    parse_schedule(schedule)


def compute_next_run(schedule: str, after: datetime) -> datetime:
    """Следующее срабатывание СТРОГО ПОСЛЕ `after` (`after` предполагается
    в UTC, наивным либо с tzinfo — на выходе всегда наивный UTC, как и все
    временные метки в этом сервисе, см. `db.py`). Поднимает `ScheduleError`
    на некорректном расписании и `ScheduleError` же, если расписание
    синтаксически валидно, но недостижимо (например, 30 февраля)."""
    if after.tzinfo is not None:
        after = after.astimezone(timezone.utc).replace(tzinfo=None)
    parsed = parse_schedule(schedule)

    if isinstance(parsed, EverySchedule):
        return after + timedelta(seconds=parsed.seconds)

    if isinstance(parsed, DailySchedule):
        candidate = after.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    # CronSchedule — перебор минута за минутой (простая и предсказуемая
    # реализация; при обычных интервалах между срабатываниями это доли
    # миллисекунды, худший случай — редкие даты вроде "29 февраля").
    assert isinstance(parsed, CronSchedule)
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_LOOKAHEAD_MINUTES):
        dow = candidate.isoweekday() % 7  # Python: Mon=1..Sun=7 -> cron: Sun=0..Sat=6
        if (
            candidate.minute in parsed.minute
            and candidate.hour in parsed.hour
            and candidate.day in parsed.dom
            and candidate.month in parsed.month
            and dow in parsed.dow
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ScheduleError(f"Не удалось найти следующее срабатывание для '{schedule}' (некорректное расписание?)")


def describe_schedule(schedule: str) -> str:
    """Человекочитаемое описание — для REST-ответов/логов, если понадобится
    показать расписание без дополнительной интерпретации на клиенте."""
    parsed = parse_schedule(schedule)
    if isinstance(parsed, EverySchedule):
        return f"каждые {parsed.seconds} с"
    if isinstance(parsed, DailySchedule):
        return f"ежедневно в {parsed.hour:02d}:{parsed.minute:02d}"
    assert isinstance(parsed, CronSchedule)
    return f"cron: {parsed.raw}"
