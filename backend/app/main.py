from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.prompty_loader import PromptProfile, load_prompty
from app.weather_mcp_tools import extract_weather_args


logger = logging.getLogger("chat_backend")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger.setLevel(logging.INFO)


_shared_credential = None
_foundry_chat_clients: dict[tuple[str, str], object] = {}
_CITY_STATE_EXPR = re.compile(r"^\s*([^,]+?)\s*,\s*([A-Za-z]{2})\s*$")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    assistant_message: str
    model: str


def _resolve_chat_provider() -> str:
    provider = os.getenv("CHAT_PROVIDER", "foundry_chat").strip().lower()
    if provider in {"foundry", "foundry_agent", "foundry-agent"}:
        return "foundry_agent"
    return "foundry_chat"


def _get_managed_identity_credential():
    global _shared_credential
    if _shared_credential is not None:
        return _shared_credential

    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    except Exception as ex:  # pragma: no cover
        raise RuntimeError(
            "Managed identity authentication requires 'azure-identity'. Install backend requirements first."
        ) from ex

    # Prefer managed identity in Azure-hosted environments, but allow local dev login chains.
    is_azure_hosted = any(
        os.getenv(name)
        for name in (
            "WEBSITE_INSTANCE_ID",
            "WEBSITE_SITE_NAME",
            "IDENTITY_ENDPOINT",
            "MSI_ENDPOINT",
            "CONTAINER_APP_NAME",
        )
    )

    if is_azure_hosted:
        _shared_credential = ManagedIdentityCredential()
        logger.info("auth_credential_selected credential=ManagedIdentityCredential")
        return _shared_credential

    _shared_credential = DefaultAzureCredential()
    logger.info("auth_credential_selected credential=DefaultAzureCredential")
    return _shared_credential


def _resolve_prompty_path() -> Path:
    app_root = Path(__file__).resolve().parents[1]
    default_path = app_root / "prompts" / "agent-plane-talk.prompty"
    configured = os.getenv("PROMPTY_PATH", "").strip()

    if not configured:
        if not default_path.exists():
            raise RuntimeError(f"Prompty file not found: {default_path}")
        return default_path

    configured_path = Path(configured)
    candidates = [configured_path]
    if not configured_path.is_absolute():
        candidates.append(app_root / configured_path)
        candidates.append(Path.cwd() / configured_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    checked = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Prompty file not found. Checked: {checked}")


def _load_agent_profile() -> PromptProfile:
    return load_prompty(_resolve_prompty_path())


def _resolve_foundry_project_endpoint(profile: PromptProfile) -> str:
    connection = profile.model.get("connection") if isinstance(profile.model, dict) else None
    endpoint = ""
    if isinstance(connection, dict):
        endpoint = str(connection.get("endpoint") or "").strip()

    endpoint = (
        endpoint
        or os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        or os.getenv("AZURE_FOUNDRY_PROJECT_ENDPOINT", "").strip()
    )
    if not endpoint:
        raise RuntimeError(
            "Missing Foundry project endpoint. Set model.connection.endpoint in .prompty or FOUNDRY_PROJECT_ENDPOINT."
        )
    return endpoint


def _resolve_foundry_chat_model(profile: PromptProfile) -> str:
    model_id = str(profile.model.get("id") or "").strip() if isinstance(profile.model, dict) else ""
    model = model_id or os.getenv("FOUNDRY_MODEL", "").strip() or os.getenv("FOUNDRY_MODEL_DEPLOYMENT_NAME", "").strip()
    if not model:
        raise RuntimeError(
            "Missing Foundry model deployment. Set model.id in .prompty or FOUNDRY_MODEL(_DEPLOYMENT_NAME)."
        )
    return model


def _resolve_foundry_agent_settings(profile: PromptProfile) -> dict[str, str]:
    endpoint = _resolve_foundry_project_endpoint(profile)
    agent_name = os.getenv("FOUNDRY_AGENT_NAME", "").strip()
    agent_version = os.getenv("FOUNDRY_AGENT_VERSION", "").strip()
    if not agent_name:
        raise RuntimeError("FOUNDRY_AGENT_NAME is required when CHAT_PROVIDER=foundry_agent.")

    settings = {
        "project_endpoint": endpoint,
        "agent_name": agent_name,
    }
    if agent_version:
        settings["agent_version"] = agent_version
    return settings


def _build_model_label(profile: PromptProfile, provider: str) -> str:
    if provider == "foundry_agent":
        agent_name = os.getenv("FOUNDRY_AGENT_NAME", "").strip() or "unknown"
        agent_version = os.getenv("FOUNDRY_AGENT_VERSION", "").strip()
        if agent_version:
            return f"foundry-agent:{agent_name}@{agent_version}"
        return f"foundry-agent:{agent_name}"

    return f"foundry-chat:{_resolve_foundry_chat_model(profile)}"


def _tool_get_current_utc_time() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _tool_lookup_aviation_term(term: str) -> str:
    glossary = {
        "runway": "A defined rectangular area prepared for aircraft takeoff and landing.",
        "crosswind": "Wind blowing across the runway direction, which pilots must correct for during takeoff/landing.",
        "holding pattern": "A racetrack-shaped flight path flown while awaiting further clearance.",
        "final approach": "The last segment of an instrument approach before landing.",
        "taxi": "Aircraft movement on the ground under its own power, excluding takeoff and landing.",
        "vr": "Rotation speed; the speed at which the pilot initiates nose-up pitch for liftoff.",
        "v1": "Decision speed; on takeoff, the speed beyond which rejecting is no longer recommended except for serious emergencies.",
        "v2": "Takeoff safety speed; target climb speed after liftoff with one engine inoperative in multi-engine aircraft.",
        "vy": "Best rate-of-climb speed; yields the greatest altitude gain per unit time.",
        "vx": "Best angle-of-climb speed; yields the greatest altitude gain over distance.",
        "mda": "Minimum descent altitude; the lowest altitude permitted on a non-precision approach until required visual references are acquired.",
        "da": "Decision altitude; altitude on precision approaches where a go-around is initiated if visual references are insufficient.",
        "dh": "Decision height; similar to DA, but referenced to touchdown zone elevation.",
        "ils": "Instrument Landing System; precision guidance using localizer lateral guidance and glideslope vertical guidance.",
        "localizer": "Lateral navigation component of an ILS that aligns the aircraft with runway centerline.",
        "glideslope": "Vertical guidance component of an ILS that provides descent path information.",
        "gs": "Ground speed; aircraft speed over the ground, affected by wind.",
        "tas": "True airspeed; actual speed through the airmass, corrected for altitude and temperature.",
        "ias": "Indicated airspeed; speed read directly from the airspeed indicator.",
        "mach tuck": "Nose-down pitching tendency near transonic speeds caused by aft movement of the aerodynamic center.",
        "coffin corner": "High-altitude region where low-speed stall margin and high-speed buffet margin converge.",
        "beta": "Propeller blade pitch range near flat or reverse used for ground handling and deceleration in turboprops.",
        "feather": "Propeller blade position aligned with airflow to reduce drag after engine failure.",
        "clean configuration": "Aircraft setup with flaps and slats retracted and landing gear up.",
        "stabilized approach": "Approach flown within defined speed, descent rate, power, and configuration limits by a specified altitude.",
        "go-around": "Discontinued landing procedure transitioning back to climb for another approach.",
        "missed approach": "Published IFR procedure followed when an approach cannot be completed to landing.",
        "sid": "Standard Instrument Departure; published IFR departure route from an airport.",
        "star": "Standard Terminal Arrival Route; published IFR arrival procedure into terminal airspace.",
        "fms": "Flight Management System; avionics computer that manages navigation, performance, and guidance.",
        "fpa": "Flight path angle; commanded or flown vertical path angle, often used in non-precision approaches.",
        "papi": "Precision Approach Path Indicator; visual light system indicating whether the approach is high, low, or on-slope.",
        "lead radial": "A radial selected before station passage to smoothly roll out on a desired outbound radial.",
        "dme arc": "Procedure segment maintaining a constant DME distance from a navigation aid while maneuvering.",
        "crab angle": "Wind correction angle where the nose points into the wind to maintain desired track.",
        "slip": "Cross-controlled maneuver used to increase descent rate or align longitudinal axis with runway in crosswind landing.",
        "ball centered": "Condition of coordinated flight indicated by the inclinometer being centered.",
        "unicom": "Non-control advisory frequency at uncontrolled airports used for pilot position reports.",
        "ctaf": "Common Traffic Advisory Frequency used for self-announced operations at non-towered airports.",
    }
    return glossary.get(term.strip().lower(), f"No glossary entry found for '{term}'.")


def _base_tool_functions() -> dict[str, object]:
    return {
        "get_current_utc_time": _tool_get_current_utc_time,
        "lookup_aviation_term": _tool_lookup_aviation_term,
    }


def _mcp_headers_for_endpoint(endpoint: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    host = (urlparse(endpoint).hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return headers

    scope = os.getenv("WEATHER_MCP_TOKEN_SCOPE", "").strip()
    if not scope:
        return headers
    if not scope.endswith("/.default"):
        scope = f"{scope}/.default"

    try:
        token = _get_managed_identity_credential().get_token(scope)
        headers["Authorization"] = f"Bearer {token.token}"
    except Exception as ex:
        logger.warning("mcp_token_unavailable endpoint=%s scope=%s error=%s", endpoint, scope, ex)

    return headers


def _mcp_json_rpc_call(endpoint: str, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    timeout_seconds = int(os.getenv("MCP_HTTP_TIMEOUT_SECONDS", "20"))
    request = UrlRequest(
        url=endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=_mcp_headers_for_endpoint(endpoint),
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as ex:
        body_text = ex.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MCP HTTP error {ex.code} for {method}: {body_text[:300]}") from ex
    except URLError as ex:
        raise RuntimeError(f"MCP network error for {method}: {ex}") from ex

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"MCP returned non-JSON payload for {method}: {body_text[:300]}") from ex

    if not isinstance(body, dict):
        raise RuntimeError(f"MCP returned invalid payload type for {method}")

    error = body.get("error")
    if isinstance(error, dict):
        raise RuntimeError(f"MCP error for {method}: {error.get('message') or 'unknown'}")

    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"MCP returned invalid result for {method}")

    return result


def _stringify_mcp_result(result: dict[str, object]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        lines: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "text":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(text)
        if lines:
            return "\n".join(lines)
    return json.dumps(result, ensure_ascii=True)


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

    parts = text.rsplit(" ", 1)
    if len(parts) == 2:
        city = parts[0].strip(" ,")
        state = parts[1].strip().upper()
        if city and len(state) == 2 and state.isalpha():
            return city, state

    return None


def _normalize_weather_tool_kwargs(kwargs: dict[str, object]) -> dict[str, object]:
    normalized = dict(kwargs)
    city = str(normalized.get("city") or "").strip()
    state = str(normalized.get("state") or "").strip().upper()

    if not city or not state:
        for key in ("location", "query", "place", "city_state"):
            parsed = _parse_city_state_text(str(normalized.get(key) or ""))
            if not parsed:
                continue
            city, state = parsed
            normalized["city"] = city
            normalized["state"] = state
            break

    return normalized


def _extract_weather_fallback_args(messages: list[ChatMessage]) -> dict[str, object] | None:
    latest_user_text = ""
    for message in reversed(messages):
        if message.role == "user":
            latest_user_text = message.content
            break

    fallback = extract_weather_args(latest_user_text)
    if fallback:
        logger.info("weather_args_extracted_from_user_message args=%s", fallback)
    return fallback


def _make_mcp_tool(
    endpoint: str,
    tool_name: str,
    description: str,
    weather_fallback_args: dict[str, object] | None = None,
):
    def _invoke(tool_kwargs: dict[str, object]) -> str:
        logger.info("mcp_tool_call_start endpoint=%s tool=%s args=%s", endpoint, tool_name, tool_kwargs)
        try:
            result = _mcp_json_rpc_call(
                endpoint=endpoint,
                method="tools/call",
                params={"name": tool_name, "arguments": tool_kwargs},
            )
            text_result = _stringify_mcp_result(result)
            preview = text_result[:200].replace("\n", "\\n")
            logger.info("mcp_tool_call_success endpoint=%s tool=%s result_preview=%s", endpoint, tool_name, preview)
            return text_result
        except Exception as ex:
            logger.warning("mcp_tool_call_failed endpoint=%s tool=%s args=%s error=%s", endpoint, tool_name, tool_kwargs, ex)
            raise

    if tool_name == "send_email":
        def _call_send_email(
            to: list[dict[str, object]] | None = None,
            subject: str | None = None,
            plain_text: str | None = None,
            html: str | None = None,
            cc: list[dict[str, object]] | None = None,
            bcc: list[dict[str, object]] | None = None,
            reply_to: list[dict[str, object]] | None = None,
            sender_address: str | None = None,
            attachments: list[dict[str, object]] | None = None,
            **kwargs,
        ) -> str:
            tool_kwargs: dict[str, object] = {}
            if to is not None:
                tool_kwargs["to"] = to
            if subject is not None:
                tool_kwargs["subject"] = subject
            if plain_text is not None:
                tool_kwargs["plain_text"] = plain_text
            if html is not None:
                tool_kwargs["html"] = html
            if cc is not None:
                tool_kwargs["cc"] = cc
            if bcc is not None:
                tool_kwargs["bcc"] = bcc
            if reply_to is not None:
                tool_kwargs["reply_to"] = reply_to
            if sender_address is not None:
                tool_kwargs["sender_address"] = sender_address
            if attachments is not None:
                tool_kwargs["attachments"] = attachments
            tool_kwargs.update(kwargs)
            return _invoke(tool_kwargs)

        _call_send_email.__name__ = tool_name
        _call_send_email.__doc__ = (
            f"{description}\n"
            "Required arguments: to (list of recipient objects), subject (string), and plain_text or html body."
        )
        return _call_send_email

    def _call_tool(**kwargs) -> str:
        tool_kwargs = (
            _normalize_weather_tool_kwargs(kwargs)
            if tool_name == "get_weather_forecast"
            else kwargs
        )
        if tool_name == "get_weather_forecast" and weather_fallback_args:
            city = str(tool_kwargs.get("city") or "").strip()
            state = str(tool_kwargs.get("state") or "").strip().upper()
            if not city or not state:
                merged = dict(weather_fallback_args)
                merged.update(tool_kwargs)
                tool_kwargs = _normalize_weather_tool_kwargs(merged)
                logger.info("mcp_tool_call_weather_fallback_applied endpoint=%s args=%s", endpoint, tool_kwargs)

        return _invoke(tool_kwargs)

    _call_tool.__name__ = tool_name
    if description:
        _call_tool.__doc__ = description
    return _call_tool


def _discover_mcp_tools(
    profile: PromptProfile,
    weather_fallback_args: dict[str, object] | None = None,
) -> dict[str, object]:
    mcp_functions: dict[str, object] = {}
    for server in profile.mcp_servers:
        endpoint = str(server.get("endpoint") or "").strip()
        if not endpoint:
            continue

        allowed = server.get("allowed_tools")
        allowed_names = {str(name).strip() for name in allowed if str(name).strip()} if isinstance(allowed, list) else set()

        try:
            listed = _mcp_json_rpc_call(endpoint=endpoint, method="tools/list")
        except Exception as ex:
            logger.warning("mcp_list_failed endpoint=%s error=%s", endpoint, ex)
            continue

        tools = listed.get("tools")
        if not isinstance(tools, list):
            continue

        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name or name in mcp_functions:
                continue
            if allowed_names and name not in allowed_names:
                continue

            description = str(tool.get("description") or "").strip()
            mcp_functions[name] = _make_mcp_tool(
                endpoint,
                name,
                description,
                weather_fallback_args,
            )
            logger.info("mcp_tool_registered endpoint=%s tool=%s", endpoint, name)

    return mcp_functions


def _build_toolset_from_prompty(
    profile: PromptProfile,
    weather_fallback_args: dict[str, object] | None = None,
) -> tuple[list[object], set[str]]:
    base_functions = _base_tool_functions()
    all_functions = dict(base_functions)
    all_functions.update(_discover_mcp_tools(profile, weather_fallback_args))

    declared_names = [
        str(tool.get("function", {}).get("name") or "")
        for tool in profile.tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    ]

    selected: list[object] = []
    selected_names: set[str] = set()

    for name in declared_names:
        if not name:
            continue
        fn = all_functions.get(name)
        if fn is None:
            logger.warning("tool_declared_but_unavailable tool=%s", name)
            continue
        selected.append(fn)
        selected_names.add(name)

    for name, fn in all_functions.items():
        if name in selected_names:
            continue
        if name in base_functions:
            continue
        selected.append(fn)
        selected_names.add(name)

    return selected, selected_names


def _build_instructions(profile: PromptProfile, selected_tool_names: set[str]) -> str:
    instructions = profile.system_prompt
    if "get_weather_forecast" in selected_tool_names:
        instructions = (
            f"{instructions}\n\n"
            "Tool policy: for weather requests, call get_weather_forecast before answering. "
            "Do not invent weather values. If location is missing, ask a short follow-up question."
        )
    if "send_email" in selected_tool_names:
        instructions = (
            f"{instructions}\n\n"
            "Tool policy: when the user asks you to send an email and provides recipient intent, "
            "you must call send_email before responding. "
            "Only ask follow-up questions when required data is missing. "
            "Do not claim email capability is unavailable. "
            "When calling send_email, always provide: to (recipient list), subject, and plain_text or html. "
            "Preferred shape: to=[{address: 'user@example.com'}], subject='...', plain_text='...'."
        )
    return instructions


def _build_chat_input(profile: PromptProfile, messages: list[ChatMessage], include_few_shots: bool) -> str:
    lines: list[str] = []

    if include_few_shots:
        for msg in profile.few_shot_messages:
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            prefix = "User" if role == "user" else "Assistant"
            lines.append(f"{prefix}: {content}")

    for message in messages:
        if message.role == "system":
            continue
        prefix = "User" if message.role == "user" else "Assistant"
        lines.append(f"{prefix}: {message.content}")

    if not lines:
        return "Hello"

    lines.append("Assistant:")
    return "\n".join(lines)


def _get_or_create_foundry_chat_client(project_endpoint: str, model: str):
    cache_key = (project_endpoint, model)
    cached = _foundry_chat_clients.get(cache_key)
    if cached is not None:
        return cached

    try:
        from agent_framework.foundry import FoundryChatClient
    except Exception as ex:  # pragma: no cover
        raise RuntimeError(
            "Foundry chat mode requires 'agent-framework-core' and 'agent-framework-foundry'."
        ) from ex

    client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=_get_managed_identity_credential(),
    )
    _foundry_chat_clients[cache_key] = client
    return client


def _resolve_request_context(request: ChatRequest) -> dict[str, Any]:
    profile = _load_agent_profile()
    provider = _resolve_chat_provider()
    model_label = _build_model_label(profile, provider)

    if provider == "foundry_agent":
        return {
            "provider": provider,
            "profile": profile,
            "model_label": model_label,
            "agent_settings": _resolve_foundry_agent_settings(profile),
            "agent_input": _build_chat_input(profile, request.messages, include_few_shots=False),
        }

    weather_fallback_args = _extract_weather_fallback_args(request.messages)
    tools, selected_tool_names = _build_toolset_from_prompty(
        profile,
        weather_fallback_args,
    )
    context = {
        "provider": provider,
        "profile": profile,
        "model_label": model_label,
        "project_endpoint": _resolve_foundry_project_endpoint(profile),
        "deployment_name": _resolve_foundry_chat_model(profile),
        "agent_name": profile.name,
        "instructions": _build_instructions(profile, selected_tool_names),
        "tools": tools,
        "agent_input": _build_chat_input(profile, request.messages, include_few_shots=True),
    }
    logger.info(
        "chat_context provider=foundry_chat endpoint=%s model=%s tools=%s",
        context["project_endpoint"],
        context["deployment_name"],
        sorted(selected_tool_names),
    )
    return context


def _format_permission_error(ex: Exception, context: dict[str, Any] | None = None) -> str | None:
    text = str(ex)
    markers = ["PermissionDenied", "does not have access", "401", "Unauthorized"]
    if not any(marker in text for marker in markers):
        return None

    endpoint = ""
    deployment = ""
    if isinstance(context, dict):
        endpoint = str(context.get("project_endpoint") or "").strip()
        deployment = str(context.get("deployment_name") or "").strip()
    target = endpoint or "the configured Foundry project"
    model_part = f", deployment '{deployment}'" if deployment else ""

    return (
        "Managed identity authorization failed for Foundry. "
        f"Grant this app identity access to {target}{model_part}. "
        "Required roles: Azure AI Developer (or Azure AI User) on the Foundry project/account, "
        "and Cognitive Services OpenAI User on the Azure AI resource hosting the model deployment."
    )


def _run_async_blocking(coro_fn):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(coro_fn())).result()


async def _run_foundry_chat_async(context: dict[str, Any], stream: bool):
    try:
        from agent_framework import Agent
    except Exception as ex:  # pragma: no cover
        raise RuntimeError(
            "Foundry chat mode requires 'agent-framework-core' and 'agent-framework-foundry'."
        ) from ex

    client = _get_or_create_foundry_chat_client(
        project_endpoint=str(context["project_endpoint"]),
        model=str(context["deployment_name"]),
    )

    async with Agent(
        client=client,
        name=str(context.get("agent_name") or "Agent Plane Talk"),
        instructions=str(context.get("instructions") or "You are a helpful assistant."),
        tools=list(context.get("tools") or []),
    ) as agent:
        return await agent.run(str(context.get("agent_input") or "Hello"), stream=stream)


async def _run_foundry_agent_async(context: dict[str, Any], stream: bool):
    try:
        from agent_framework.foundry import FoundryAgent
    except Exception as ex:  # pragma: no cover
        raise RuntimeError("Foundry agent mode requires 'agent-framework-foundry'.") from ex

    settings = dict(context["agent_settings"])
    settings["credential"] = _get_managed_identity_credential()

    agent = FoundryAgent(**settings)
    return await agent.run(str(context.get("agent_input") or "Hello"), stream=stream)


def _run_non_stream(context: dict[str, Any]) -> str:
    provider = str(context.get("provider") or "foundry_chat")

    async def _runner() -> str:
        response = (
            await _run_foundry_agent_async(context, stream=False)
            if provider == "foundry_agent"
            else await _run_foundry_chat_async(context, stream=False)
        )
        text = str(getattr(response, "text", "") or "").strip()
        return text or str(response).strip()

    return str(_run_async_blocking(_runner)).strip()


def _sse_event(event_name: str, payload: dict[str, object]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"


app = FastAPI(title="Simple Chat Bubble API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def warmup() -> None:
    try:
        profile = _load_agent_profile()
        provider = _resolve_chat_provider()
        _get_managed_identity_credential()

        if provider == "foundry_agent":
            _resolve_foundry_agent_settings(profile)
            logger.info("startup_ready provider=foundry_agent")
            return

        endpoint = _resolve_foundry_project_endpoint(profile)
        model = _resolve_foundry_chat_model(profile)
        _get_or_create_foundry_chat_client(endpoint, model)
        logger.info("startup_ready provider=foundry_chat model=%s", model)
    except Exception as ex:
        logger.warning("startup_warmup_skipped error=%s", ex)


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/static/test-host.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: FastAPIRequest, exc: RequestValidationError):
    logger.warning("request_validation_failed path=%s errors=%s", request.url.path, exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    context: dict[str, Any] | None = None
    try:
        logger.info("chat_request_start endpoint=/api/chat messages=%s", len(request.messages))
        context = _resolve_request_context(request)
        answer = _run_non_stream(context)
        if not answer:
            raise HTTPException(status_code=502, detail="Model response was empty.")

        model = str(context["model_label"])
        logger.info("chat_request_success endpoint=/api/chat model=%s", model)
        return ChatResponse(assistant_message=answer, model=model)
    except HTTPException:
        raise
    except Exception as ex:  # pragma: no cover
        permission_message = _format_permission_error(ex, context=context)
        if permission_message:
            raise HTTPException(status_code=403, detail=permission_message) from ex
        raise HTTPException(status_code=500, detail=f"Chat request failed: {ex}") from ex


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    async def event_generator():
        context: dict[str, Any] | None = None
        try:
            logger.info("chat_stream_start endpoint=/api/chat/stream messages=%s", len(request.messages))
            context = _resolve_request_context(request)
            model = str(context["model_label"])

            yield _sse_event("start", {"model": model})

            provider = str(context.get("provider") or "foundry_chat")
            stream = (
                await _run_foundry_agent_async(context, stream=True)
                if provider == "foundry_agent"
                else await _run_foundry_chat_async(context, stream=True)
            )

            chunks: list[str] = []
            async for chunk in stream:
                text = str(getattr(chunk, "text", "") or "")
                if text:
                    chunks.append(text)
                    yield _sse_event("delta", {"content": text})

            final_response = await stream.get_final_response()
            final_text = str(getattr(final_response, "text", "") or "").strip() or "".join(chunks).strip()
            if not final_text:
                raise RuntimeError("Model response was empty.")

            logger.info("chat_stream_success endpoint=/api/chat/stream model=%s", model)
            yield _sse_event("done", {"assistant_message": final_text, "model": model})
        except Exception as ex:
            logger.exception("chat_stream_failure endpoint=/api/chat/stream")
            permission_message = _format_permission_error(ex, context=context)
            message = permission_message or f"Chat stream failed: {ex}"
            yield _sse_event("error", {"message": message})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
