from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

import azure.functions as func
from email_mcp_tools import EMAIL_TOOL_NAME, email_tool_schema, send_email

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("weather-mcp-function")
logger.setLevel(logging.INFO)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MCP_PROTOCOL_VERSION = "2024-11-05"
WEATHER_TOOL_NAME = "get_weather_forecast"
HEARTBEAT_TOOL_NAME = "get_heartbeat"

_CITY_STATE_EXPR = re.compile(r"^\s*([^,]+?)\s*,\s*([A-Za-z]{2})\s*$")


def _parse_city_state_text(value: str) -> tuple[str, str] | None:
    text = (value or "").strip()
    if not text:
        return None

    match = _CITY_STATE_EXPR.match(text)
    if match:
        city = match.group(1).strip()
        state = match.group(2).strip().upper()
        if city and state:
            return city, state

    # Fallback: "City ST" where ST is 2-letter code.
    parts = text.rsplit(" ", 1)
    if len(parts) == 2:
        city = parts[0].strip(" ,")
        state = parts[1].strip().upper()
        if city and len(state) == 2 and state.isalpha():
            return city, state

    return None


def _normalize_weather_arguments(arguments: dict[str, Any]) -> tuple[str, str, int]:
    city = str(arguments.get("city", "")).strip()
    state = str(arguments.get("state", "")).strip().upper()

    if not city or not state:
        # Some models send a single location-like field instead of city/state.
        for key in ("location", "query", "place", "city_state"):
            parsed = _parse_city_state_text(str(arguments.get(key, "")))
            if parsed:
                city, state = parsed
                break

    days_raw = arguments.get("days", 3)
    if isinstance(days_raw, str):
        days_raw = days_raw.strip()

    if not city:
        raise ValueError("'city' is required.")
    if not state:
        raise ValueError("'state' is required.")

    try:
        days = int(days_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("'days' must be an integer.") from exc

    if days < 1 or days > 7:
        raise ValueError("'days' must be between 1 and 7.")

    return city, state, days


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _weather_tool_schema() -> dict[str, Any]:
    return {
        "name": WEATHER_TOOL_NAME,
        "description": (
            "Get an NWS forecast for a US location. Prefer explicit city/state arguments "
            "(for example city='Chicago', state='IL'). If only one location field is available, "
            "use location like 'Chicago, IL'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "minLength": 1,
                    "description": "US city name only, for example 'Chicago' (without state).",
                },
                "state": {
                    "type": "string",
                    "pattern": "^[A-Za-z]{2}$",
                    "description": "Two-letter US state abbreviation, for example 'IL' or 'WA'.",
                },
                "location": {
                    "type": "string",
                    "minLength": 4,
                    "description": "Fallback location string in the format 'City, ST', for example 'Chicago, IL'.",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of forecast periods to return (1-7). Default is 3.",
                    "minimum": 1,
                    "maximum": 7,
                    "default": 3,
                },
            },
            "anyOf": [
                {"required": ["city", "state"]},
                {"required": ["location"]},
            ],
            "additionalProperties": False,
        },
    }


def _heartbeat_tool_schema() -> dict[str, Any]:
    return {
        "name": HEARTBEAT_TOOL_NAME,
        "description": "Return a service heartbeat with current UTC timestamp.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _heartbeat_response() -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    return (
        "Heartbeat OK\n"
        f"timestamp_utc: {timestamp}\n"
        "service: weather-mcp-function\n"
        "status: healthy"
    )


def _http_get_json(
    base_url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_seconds: int = 20,
) -> tuple[int, Any, str]:
    query = url_parse.urlencode(params or {})
    url = f"{base_url}?{query}" if query else base_url

    req = url_request.Request(url=url, headers=headers, method="GET")

    try:
        with url_request.urlopen(req, timeout=timeout_seconds) as response:
            status_code = response.getcode()
            body_bytes = response.read()
            body_text = body_bytes.decode("utf-8", errors="replace")
            payload = json.loads(body_text) if body_text else None
            return status_code, payload, body_text
    except url_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, None, body


def _geocode_city_state(city: str, state: str) -> tuple[float, float, str]:
    base_url = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search").strip()
    geocoder_user_agent = os.getenv(
        "NOMINATIM_USER_AGENT",
        os.getenv("NWS_USER_AGENT", "simple-chat-bubble-weather-mcp/1.0"),
    ).strip()

    status_code, payload, raw = _http_get_json(
        base_url=base_url,
        params={
            "city": city,
            "state": state,
            "country": "USA",
            "format": "jsonv2",
            "limit": 1,
        },
        headers={"User-Agent": geocoder_user_agent, "Accept": "application/json"},
    )
    if status_code != 200:
        raise ValueError(f"Geocoding failed ({status_code}): {raw[:250]}")

    if not isinstance(payload, list) or not payload:
        raise ValueError(f"No geocoding results for '{city}, {state}'.")

    first = payload[0]
    if not isinstance(first, dict):
        raise ValueError("Invalid geocoding response: missing coordinates.")

    lat_raw = first.get("lat")
    lon_raw = first.get("lon")
    if lat_raw is None or lon_raw is None:
        raise ValueError("Invalid geocoding response: missing coordinates.")

    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid geocoding response: non-numeric coordinates.") from exc

    display_name = first.get("display_name")
    location_label = display_name if isinstance(display_name, str) and display_name.strip() else f"{city}, {state}"

    return lat, lon, location_label


def _get_forecast(lat: float, lon: float) -> dict[str, Any]:
    user_agent = os.getenv(
        "NWS_USER_AGENT",
        "simple-chat-bubble-weather-mcp/1.0 (contact: admin@example.com)",
    ).strip()

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/geo+json",
    }

    points_status, points_payload, points_raw = _http_get_json(
        base_url=f"https://api.weather.gov/points/{lat},{lon}",
        params=None,
        headers=headers,
    )
    if points_status != 200:
        raise ValueError(f"Weather points lookup failed ({points_status}): {points_raw[:250]}")

    points = points_payload
    properties = points.get("properties", {}) if isinstance(points, dict) else {}
    forecast_url = properties.get("forecast") if isinstance(properties, dict) else None
    if not forecast_url:
        raise ValueError("Weather.gov points response missing forecast URL.")

    forecast_status, forecast_payload, forecast_raw = _http_get_json(
        base_url=str(forecast_url),
        params=None,
        headers=headers,
    )
    if forecast_status != 200:
        raise ValueError(f"Forecast lookup failed ({forecast_status}): {forecast_raw[:250]}")

    forecast = forecast_payload
    forecast_props = forecast.get("properties", {}) if isinstance(forecast, dict) else {}
    periods = forecast_props.get("periods") if isinstance(forecast_props, dict) else None
    if not isinstance(periods, list) or not periods:
        raise ValueError("Weather.gov forecast response contains no periods.")

    return {
        "forecast": forecast,
        "periods": periods,
    }


def _build_forecast_text(city: str, state: str, days: int) -> str:
    lat, lon, location_label = _geocode_city_state(city, state)
    weather = _get_forecast(lat, lon)
    periods = weather["periods"][:days]

    lines = [
        f"Forecast for {location_label} ({lat:.4f}, {lon:.4f})",
        "",
    ]

    for period in periods:
        if not isinstance(period, dict):
            continue

        name = str(period.get("name", "Period"))
        temp = period.get("temperature")
        temp_unit = str(period.get("temperatureUnit", "F"))
        short_forecast = str(period.get("shortForecast", ""))
        wind = f"{period.get('windSpeed', '')} {period.get('windDirection', '')}".strip()

        lines.append(f"- {name}: {temp}{temp_unit}, {short_forecast}")
        if wind:
            lines.append(f"  Wind: {wind}")

    return "\n".join(lines)


def _run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    logger.info("tool_call_start tool=%s args=%s", name, arguments)

    if name == HEARTBEAT_TOOL_NAME:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": _heartbeat_response(),
                }
            ]
        }
        logger.info("tool_call_success tool=%s", name)
        return result

    if name == WEATHER_TOOL_NAME:
        city, state, days = _normalize_weather_arguments(arguments)

        forecast_text = _build_forecast_text(city=city, state=state, days=days)

        result = {
            "content": [
                {
                    "type": "text",
                    "text": forecast_text,
                }
            ]
        }
        logger.info("tool_call_success tool=%s city=%s state=%s days=%s", name, city, state, days)
        return result

    if name == EMAIL_TOOL_NAME:
        try:
            return send_email(arguments=arguments, logger=logger)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool_call_failure tool=%s", name)
            raise ValueError(f"Failed to send email: {exc}") from exc

    logger.warning("tool_call_unknown tool=%s", name)
    raise ValueError(f"Unknown tool: {name}")


def _json_response(payload: dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


@app.route(route="api/heartbeat", methods=["GET"])
def heartbeat_http(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("http_request_start endpoint=/api/heartbeat method=GET")
    try:
        result = _run_tool(HEARTBEAT_TOOL_NAME, {})
        logger.info("http_request_success endpoint=/api/heartbeat method=GET")
        return _json_response(result)
    except ValueError as exc:
        logger.warning("http_request_failure endpoint=/api/heartbeat method=GET status=400 error=%s", exc)
        return _json_response({"error": str(exc)}, status_code=400)
    except url_error.URLError as exc:
        logger.warning("http_request_failure endpoint=/api/heartbeat method=GET status=502 error=%s", exc)
        return _json_response({"error": f"Upstream request failed: {exc}"}, status_code=502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("http_request_failure endpoint=/api/heartbeat method=GET status=500")
        return _json_response({"error": f"Internal error: {exc}"}, status_code=500)


@app.route(route="api/weather", methods=["GET"])
def weather_http(req: func.HttpRequest) -> func.HttpResponse:
    arguments = {
        "city": req.params.get("city", ""),
        "state": req.params.get("state", ""),
        "days": req.params.get("days", "3"),
    }

    logger.info(
        "http_request_start endpoint=/api/weather method=GET city=%s state=%s days=%s",
        arguments["city"],
        arguments["state"],
        arguments["days"],
    )

    try:
        result = _run_tool(WEATHER_TOOL_NAME, arguments)
        logger.info("http_request_success endpoint=/api/weather method=GET")
        return _json_response(result)
    except ValueError as exc:
        logger.warning("http_request_failure endpoint=/api/weather method=GET status=400 error=%s", exc)
        return _json_response({"error": str(exc)}, status_code=400)
    except url_error.URLError as exc:
        logger.warning("http_request_failure endpoint=/api/weather method=GET status=502 error=%s", exc)
        return _json_response({"error": f"Upstream request failed: {exc}"}, status_code=502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("http_request_failure endpoint=/api/weather method=GET status=500")
        return _json_response({"error": f"Internal error: {exc}"}, status_code=500)


@app.route(route="api/mcp", methods=["POST"])
def mcp_http(req: func.HttpRequest) -> func.HttpResponse:
    request_id: Any = None

    logger.info("http_request_start endpoint=/api/mcp method=POST")
    try:
        body = req.get_json()
    except ValueError:
        logger.warning("http_request_failure endpoint=/api/mcp method=POST status=400 reason=invalid_json")
        error = _jsonrpc_error(None, -32700, "Parse error: invalid JSON body.")
        return _json_response(error)

    if not isinstance(body, dict):
        logger.warning("http_request_failure endpoint=/api/mcp method=POST status=400 reason=invalid_request_type")
        error = _jsonrpc_error(None, -32600, "Invalid Request: expected a JSON object.")
        return _json_response(error)

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})
    logger.info("mcp_method_received method=%s request_id=%s", method, request_id)

    if not isinstance(method, str):
        error = _jsonrpc_error(request_id, -32600, "Invalid Request: missing method.")
        logger.warning("mcp_method_failure method=unknown request_id=%s error=missing_method", request_id)
        return _json_response(error)

    try:
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "weather-mcp-function",
                    "version": "1.0.0",
                },
            }
            response = _jsonrpc_result(request_id, result)
            logger.info("mcp_method_success method=initialize request_id=%s", request_id)

        elif method == "tools/list":
            response = _jsonrpc_result(
                request_id,
                {"tools": [_weather_tool_schema(), _heartbeat_tool_schema(), email_tool_schema()]},
            )
            logger.info("mcp_method_success method=tools/list request_id=%s", request_id)

        elif method == "tools/call":
            if not isinstance(params, dict):
                raise ValueError("Invalid params for tools/call.")

            name = params.get("name")
            arguments = params.get("arguments", {})

            if not isinstance(name, str):
                raise ValueError("tools/call requires a string 'name'.")
            if not isinstance(arguments, dict):
                raise ValueError("tools/call requires an object 'arguments'.")

            tool_result = _run_tool(name, arguments)
            response = _jsonrpc_result(request_id, tool_result)
            logger.info("mcp_method_success method=tools/call request_id=%s tool=%s", request_id, name)

        else:
            response = _jsonrpc_error(request_id, -32601, f"Method not found: {method}")
            logger.warning("mcp_method_failure method=%s request_id=%s error=method_not_found", method, request_id)

    except ValueError as exc:
        logger.warning("mcp_method_failure method=%s request_id=%s error=%s", method, request_id, exc)
        response = _jsonrpc_error(request_id, -32000, str(exc))
    except url_error.URLError as exc:
        logger.warning("mcp_method_failure method=%s request_id=%s upstream_error=%s", method, request_id, exc)
        response = _jsonrpc_error(request_id, -32001, f"Upstream request failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("mcp_method_failure method=%s request_id=%s internal_error", method, request_id)
        response = _jsonrpc_error(request_id, -32603, f"Internal error: {exc}")

    logger.info("http_request_end endpoint=/api/mcp method=POST")
    return _json_response(response)


@app.route(route="", methods=["GET"])
def root(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response({"status": "ok", "service": "weather-mcp-function"})
