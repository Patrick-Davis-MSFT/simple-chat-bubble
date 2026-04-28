from __future__ import annotations

import re


_WEATHER_INTENT_EXPR = re.compile(
    r"\b(weather|forecast|temperature|rain|snow|wind|humidity|precipitation|storm|sunny|cloudy)\b",
    flags=re.IGNORECASE,
)
_WEATHER_CITY_STATE_EXPR = re.compile(r"\b(?:in|for)\s+([A-Za-z .'-]+?)(?:,\s*|\s+)([A-Za-z]{2})\b", flags=re.IGNORECASE)
_WEATHER_DAYS_EXPR = re.compile(r"\b(\d{1,2})\s*(?:day|days)\b", flags=re.IGNORECASE)


def latest_user_message(messages: list[dict[str, str]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def is_weather_intent(text: str) -> bool:
    return bool(_WEATHER_INTENT_EXPR.search(text or ""))


def extract_weather_args(text: str) -> dict[str, object] | None:
    if not text:
        return None

    city = ""
    state = ""
    match = _WEATHER_CITY_STATE_EXPR.search(text)
    if match:
        city = match.group(1).strip(" ,")
        state = match.group(2).strip().upper()

    if not city or not state:
        return None

    days = 3
    days_match = _WEATHER_DAYS_EXPR.search(text)
    if days_match:
        try:
            parsed_days = int(days_match.group(1))
            if 1 <= parsed_days <= 7:
                days = parsed_days
        except ValueError:
            days = 3

    return {
        "city": city,
        "state": state,
        "days": days,
    }