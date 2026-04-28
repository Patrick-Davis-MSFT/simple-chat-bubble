# Weather MCP Azure Function App (Python 3.13)

This service exposes an MCP-compatible HTTP endpoint with weather and email tools. The runtime is hosted in Azure as a Function App.

- `get_weather_forecast`
- `send_email`

It is designed for city/state weather lookups.

## Endpoint

- `POST /api/mcp`

## Tool

- Name: `get_weather_forecast`
- Arguments:
  - `city` (string, required)
  - `state` (string, required, e.g. `WA`)
  - `days` (integer, optional, default `3`, range `1-7`)

- Name: `send_email`
- Arguments:
  - `to` (array, required): recipient list of `{ "address": "user@example.com", "display_name": "Optional Name" }`
  - `subject` (string, required): email subject
  - `plain_text` (string, optional): plain text body
  - `html` (string, optional): HTML body
  - `cc` (array, optional): same shape as `to`
  - `bcc` (array, optional): same shape as `to`
  - `reply_to` (array, optional): same shape as `to`
  - `sender_address` (string, optional): override sender; default comes from environment
  - `attachments` (array, optional):
    - each item: `{ "name": "file.pdf", "content_type": "application/pdf", "content_base64": "..." }`
  - At least one body field is required: `plain_text` or `html`

## Environment Variables

- `NOMINATIM_BASE_URL` (optional): defaults to `https://nominatim.openstreetmap.org/search`
- `NOMINATIM_USER_AGENT` (optional but recommended): user agent for Nominatim calls
- `NWS_USER_AGENT` (optional but recommended): user agent for weather.gov requests
- `AZURE_COMMUNICATION_EMAIL_CONNECTION_STRING` (required for `send_email`): ACS Email connection string
- `AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS` (required unless tool argument `sender_address` is provided): default sender
- `AZURE_COMMUNICATION_EMAIL_DOMAIN` (optional): deployed sender domain output
- `AZURE_COMMUNICATION_EMAIL_SERVICE_NAME` (optional): deployed email service name output

## Local Run

From the repo root, use the local runner script:

```bash
./scripts/run_weather_webapp_local.sh .azure/bubble-chat/.env
```

Default local endpoint:

- `http://localhost:7071`

## MCP Request Examples

### List tools

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

### Call tool

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "get_weather_forecast",
    "arguments": {
      "city": "Seattle",
      "state": "WA",
      "days": 3
    }
  }
}
```

### Call email tool

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "send_email",
    "arguments": {
      "to": [
        {
          "address": "alex@example.com",
          "display_name": "Alex"
        }
      ],
      "subject": "Status update",
      "plain_text": "Your request has been processed.",
      "html": "<p>Your request has been <strong>processed</strong>.</p>"
    }
  }
}
```

## Prompty Integration (MCP Tool)

Example `.prompty` frontmatter snippet:

```yaml
tools:
  - name: weather
    kind: mcp
    serverName: weather-mcp
    connection:
      kind: reference
      name: weather-mcp-connection
    approvalMode:
      kind: never
    allowedTools:
      - get_weather_forecast
      - send_email
```

Configure your Prompty runtime connection so `weather-mcp-connection` points to this function endpoint.
