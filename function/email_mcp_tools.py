from __future__ import annotations

import base64
import os
import re
from typing import Any

EMAIL_TOOL_NAME = "send_email"
_EMAIL_ADDR_EXPR = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_EXTRACT_EXPR = re.compile(r"([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def email_tool_schema() -> dict[str, Any]:
    return {
        "name": EMAIL_TOOL_NAME,
        "description": (
            "Send an email using Azure Communication Services Email. "
            "Use this when a user asks to send email. "
            "Required fields are to, subject, and at least one of plain_text or html."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "description": "Required. Primary recipients. Each item must include address and can include display_name.",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "string",
                                "description": "Recipient email address, for example alice@example.com.",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Optional display name for this recipient.",
                            },
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                },
                "cc": {
                    "type": "array",
                    "description": "Optional. Carbon-copy recipients with the same object shape as to.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "string",
                                "description": "Recipient email address.",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Optional display name.",
                            },
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                },
                "bcc": {
                    "type": "array",
                    "description": "Optional. Blind carbon-copy recipients with the same object shape as to.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "string",
                                "description": "Recipient email address.",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Optional display name.",
                            },
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                },
                "subject": {
                    "type": "string",
                    "description": "Required. Email subject line.",
                    "minLength": 1,
                },
                "plain_text": {
                    "type": "string",
                    "description": "Optional plain text body. Provide this, html, or both.",
                },
                "html": {
                    "type": "string",
                    "description": "Optional HTML body. Provide this, plain_text, or both.",
                },
                "reply_to": {
                    "type": "array",
                    "description": "Optional. Reply-to addresses with the same object shape as to.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "string",
                                "description": "Reply-to email address.",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Optional display name.",
                            },
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                },
                "sender_address": {
                    "type": "string",
                    "description": (
                        "Optional sender address override. "
                        "If omitted, the server uses AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS from environment."
                    ),
                },
                "attachments": {
                    "type": "array",
                    "description": "Optional attachments. content_base64 must contain base64-encoded file bytes.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Required attachment file name, for example report.pdf.",
                            },
                            "content_type": {
                                "type": "string",
                                "description": "Required MIME type, for example application/pdf.",
                            },
                            "content_base64": {
                                "type": "string",
                                "description": "Required base64-encoded attachment data.",
                            },
                        },
                        "required": ["name", "content_type", "content_base64"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["to", "subject"],
            "anyOf": [
                {"required": ["plain_text"]},
                {"required": ["html"]},
            ],
            "additionalProperties": False,
        },
    }


def _is_valid_email(address: str) -> bool:
    return bool(_EMAIL_ADDR_EXPR.match(address.strip()))


def _coerce_email_address(value: str) -> str:
    text = (value or "").strip().strip("<>").strip().strip(",;:")
    match = _EMAIL_EXTRACT_EXPR.search(text)
    candidate = match.group(1) if match else text
    return candidate.strip().strip(",;:")


def _normalize_email_participants(value: Any, field_name: str, required: bool) -> list[dict[str, str]]:
    if isinstance(value, str):
        value = [{"address": part.strip()} for part in value.split(",") if part.strip()]

    if value is None:
        if required:
            raise ValueError(f"'{field_name}' is required.")
        return []

    if not isinstance(value, list):
        raise ValueError(f"'{field_name}' must be an array.")

    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(value):
        if isinstance(item, str):
            item = {"address": item}

        if not isinstance(item, dict):
            raise ValueError(f"'{field_name}[{idx}]' must be an object.")

        raw_address = _coerce_email_address(str(item.get("address", "")))
        if not raw_address:
            raise ValueError(f"'{field_name}[{idx}].address' is required.")
        if not _is_valid_email(raw_address):
            raise ValueError(f"'{field_name}[{idx}].address' is not a valid email address.")

        recipient: dict[str, str] = {"address": raw_address}
        display_name = str(item.get("display_name", "")).strip()
        if display_name:
            recipient["displayName"] = display_name

        normalized.append(recipient)

    if required and not normalized:
        raise ValueError(f"'{field_name}' must include at least one recipient.")

    return normalized


def _normalize_attachments(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []

    if not isinstance(value, list):
        raise ValueError("'attachments' must be an array.")

    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"'attachments[{idx}]' must be an object.")

        name = str(item.get("name", "")).strip()
        content_type = str(item.get("content_type", "")).strip()
        content_base64 = str(item.get("content_base64", "")).strip()

        if not name:
            raise ValueError(f"'attachments[{idx}].name' is required.")
        if not content_type:
            raise ValueError(f"'attachments[{idx}].content_type' is required.")
        if not content_base64:
            raise ValueError(f"'attachments[{idx}].content_base64' is required.")

        try:
            base64.b64decode(content_base64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"'attachments[{idx}].content_base64' is not valid base64.") from exc

        normalized.append(
            {
                "name": name,
                "contentType": content_type,
                "contentInBase64": content_base64,
            }
        )

    return normalized


def _build_email_message(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    connection_string = os.getenv("AZURE_COMMUNICATION_EMAIL_CONNECTION_STRING", "").strip()
    if not connection_string:
        raise ValueError("Missing required environment variable 'AZURE_COMMUNICATION_EMAIL_CONNECTION_STRING'.")

    default_sender_address = os.getenv("AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS", "").strip()
    sender_address = str(arguments.get("sender_address", "")).strip() or default_sender_address
    if not sender_address:
        raise ValueError(
            "Sender address is required. Set 'sender_address' or configure AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS."
        )
    if not _is_valid_email(sender_address):
        raise ValueError("Sender address must be a valid email address.")

    subject = str(arguments.get("subject", "")).strip()
    if not subject:
        raise ValueError("'subject' is required.")

    plain_text = str(
        arguments.get("plain_text")
        or arguments.get("text")
        or arguments.get("body")
        or arguments.get("message")
        or ""
    ).strip()
    html = str(arguments.get("html") or arguments.get("html_body") or "").strip()
    if not plain_text and not html:
        raise ValueError("At least one of 'plain_text' or 'html' is required.")

    recipients_to = _normalize_email_participants(
        arguments.get("to") or arguments.get("recipient") or arguments.get("email"),
        field_name="to",
        required=True,
    )
    recipients_cc = _normalize_email_participants(arguments.get("cc"), field_name="cc", required=False)
    recipients_bcc = _normalize_email_participants(arguments.get("bcc"), field_name="bcc", required=False)
    reply_to = _normalize_email_participants(arguments.get("reply_to"), field_name="reply_to", required=False)
    attachments = _normalize_attachments(arguments.get("attachments"))

    message: dict[str, Any] = {
        "senderAddress": sender_address,
        "recipients": {
            "to": recipients_to,
        },
        "content": {
            "subject": subject,
        },
    }

    if recipients_cc:
        message["recipients"]["cc"] = recipients_cc
    if recipients_bcc:
        message["recipients"]["bcc"] = recipients_bcc

    if plain_text:
        message["content"]["plainText"] = plain_text
    if html:
        message["content"]["html"] = html

    if reply_to:
        message["replyTo"] = reply_to

    if attachments:
        message["attachments"] = attachments

    return message, connection_string


def send_email(arguments: dict[str, Any], logger: Any) -> dict[str, Any]:
    try:
        from azure.communication.email import EmailClient
    except ModuleNotFoundError as exc:
        raise ValueError(
            "Email SDK dependency is missing: 'azure-communication-email'. "
            "Ensure requirements are built during deployment."
        ) from exc

    message, connection_string = _build_email_message(arguments)
    timeout_seconds_raw = os.getenv("AZURE_COMMUNICATION_EMAIL_SEND_TIMEOUT_SECONDS", "60").strip()

    try:
        timeout_seconds = int(timeout_seconds_raw)
    except ValueError:
        timeout_seconds = 60

    client = EmailClient.from_connection_string(connection_string)
    poller = client.begin_send(message)
    send_result = poller.result(timeout=timeout_seconds)

    operation_id = getattr(send_result, "id", None)
    status = getattr(send_result, "status", None)

    summary_lines = [
        "Email send request accepted.",
        f"sender: {message['senderAddress']}",
        f"to_count: {len(message['recipients']['to'])}",
        f"subject: {message['content']['subject']}",
    ]
    if operation_id:
        summary_lines.append(f"operation_id: {operation_id}")
    if status:
        summary_lines.append(f"status: {status}")

    logger.info(
        "email_send_success to=%s subject=%s operation_id=%s status=%s",
        [item["address"] for item in message["recipients"]["to"]],
        message["content"]["subject"],
        operation_id,
        status,
    )

    return {
        "content": [
            {
                "type": "text",
                "text": "\n".join(summary_lines),
            }
        ]
    }
