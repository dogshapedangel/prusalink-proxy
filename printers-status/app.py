import logging
import os
import re
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth
from flask import Flask, render_template_string

logging.basicConfig(
    level=os.environ.get("STATUS_APP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

PORT = int(os.environ.get("STATUS_APP_PORT", "8888"))
TIMEOUT = float(os.environ.get("STATUS_APP_TIMEOUT", "10"))

session = requests.Session()

app = Flask(__name__)


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    domain: str
    upstream: str
    username: str
    password: str
    upstream_host: str


@dataclass
class PrinterStatus:
    name: str
    domain: str
    status: str
    time_remaining: Optional[int] = None
    progress: Optional[float] = None
    error: Optional[str] = None
    text_color: str = "white"
    bg_color: str = "black"
    model_name: str = ""


def _normalize_printer_id(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", value.upper())


def _split_printer_ids(raw: str) -> list[str]:
    if not raw:
        return []
    return [item for item in re.split(r"[\s,]+", raw.strip()) if item]


def _build_printer_config(name: str, domain: str, upstream: str, username: str, password: str) -> PrinterConfig:
    cleaned_upstream = upstream.rstrip("/")
    parsed = urlsplit(cleaned_upstream)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Printer '{name}' has an invalid PRUSALINK URL: {upstream}")

    return PrinterConfig(
        name=name,
        domain=domain,
        upstream=cleaned_upstream,
        username=username,
        password=password,
        upstream_host=parsed.netloc,
    )


def load_printer_configs() -> dict[str, PrinterConfig]:
    printers: dict[str, PrinterConfig] = {}
    printer_ids = _split_printer_ids(os.environ.get("PRINTER_IDS", ""))

    if printer_ids:
        for printer_id in printer_ids:
            env_key = _normalize_printer_id(printer_id)
            prefix = f"PRINTER_{env_key}_"
            values = {
                "DOMAIN": os.environ.get(f"{prefix}DOMAIN", "").strip(),
                "URL": os.environ.get(f"{prefix}URL", "").strip(),
                "USERNAME": os.environ.get(f"{prefix}USERNAME", "").strip(),
                "PASSWORD": os.environ.get(f"{prefix}PASSWORD", "").strip(),
            }
            missing = [field for field, value in values.items() if not value]
            if missing:
                missing_list = ", ".join(f"{prefix}{field}" for field in missing)
                raise RuntimeError(f"Printer '{printer_id}' is missing required settings: {missing_list}")

            domain = values["DOMAIN"].split(":", 1)[0].lower()
            if domain in printers:
                raise RuntimeError(f"Duplicate printer domain configured: {domain}")

            printers[domain] = _build_printer_config(
                name=printer_id,
                domain=domain,
                upstream=values["URL"],
                username=values["USERNAME"],
                password=values["PASSWORD"],
            )

        return printers

    legacy_upstream = os.environ.get("PRUSALINK_URL", "").strip()
    legacy_username = os.environ.get("PRUSALINK_USERNAME", "").strip()
    legacy_password = os.environ.get("PRUSALINK_PASSWORD", "").strip()
    if legacy_upstream and legacy_username and legacy_password:
        fallback_domain = os.environ.get("DOMAIN", "default").strip().lower() or "default"
        return {
            fallback_domain: _build_printer_config(
                name="default",
                domain=fallback_domain,
                upstream=legacy_upstream,
                username=legacy_username,
                password=legacy_password,
            )
        }

    raise RuntimeError(
        "No printer configuration found. Set PRINTER_IDS with PRINTER_<ID>_DOMAIN/URL/USERNAME/PASSWORD "
        "or provide the legacy PRUSALINK_URL/PRUSALINK_USERNAME/PRUSALINK_PASSWORD values."
    )


PRINTERS = load_printer_configs()

# Color scheme and order for each printer - matching the original design
PRINTER_ORDER = [
    "purple.psone.space",
    "orange.psone.space",
    "pink.psone.space",
    "blue.psone.space",
    "white.psone.space",
]

PRINTER_COLORS = {
    "purple.psone.space": {"text": "rgb(245, 111, 245)", "bg": "black", "name": "Core One+ L"},
    "orange.psone.space": {"text": "orange", "bg": "black", "name": "Core One+ L"},
    "pink.psone.space": {"text": "black", "bg": "pink", "name": "Core One+"},
    "blue.psone.space": {"text": "black", "bg": "lightblue", "name": "Core One+"},
    "white.psone.space": {"text": "black", "bg": "white", "name": "Core One+"},
}


def format_time(seconds: Optional[int]) -> str:
    """Format seconds to human readable time."""
    if seconds is None or seconds < 0:
        return "N/A"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def fetch_printer_status(printer: PrinterConfig) -> PrinterStatus:
    """Fetch status from a printer via the Prusa API."""
    # Get colors for this printer
    colors = PRINTER_COLORS.get(printer.domain, {"text": "white", "bg": "black"})
    
    try:
        url = f"{printer.upstream}/api/v1/status"
        response = session.get(
            url,
            auth=HTTPDigestAuth(printer.username, printer.password),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        
        printer_state = data.get("printer", {}).get("state", "UNKNOWN")
        job = data.get("job", {})
        time_remaining = job.get("time_remaining")
        progress = job.get("progress")
        
        return PrinterStatus(
            name=printer.name,
            domain=printer.domain,
            status=printer_state,
            time_remaining=time_remaining,
            progress=progress,
            text_color=colors["text"],
            bg_color=colors["bg"],
                    model_name=colors.get("name", ""),
        )
    except requests.RequestException as e:
        logging.warning(f"Failed to fetch status for {printer.name}: {e}")
        return PrinterStatus(
            name=printer.name,
            domain=printer.domain,
            status="OFFLINE",
            error=str(e),
                        model_name=colors.get("name", ""),
            text_color=colors["text"],
            bg_color=colors["bg"],
        )


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PS:One 3D Printers</title>
  <style>
    body {
      background-color: #fae6e6;
      text-align: center;
      font-family: 'Courier New', Courier, monospace;
    }
    ul {
      list-style: none;
      padding: 0;
    }
    li {
      display: inline-block;
      margin: 6px;
      font-size: 1.2em;
    }
    p {
      font-size: 1.5em;
    }
        .printer-row {
            display: flex;
            justify-content: center;
            flex-wrap: wrap;
        }
    .printer-item {
      display: inline-block;
      margin: 12px;
    }
    .status-bar {
      padding: 6px 8px;
      font-size: 0.85em;
      text-align: center;
      margin-bottom: 2px;
      border-radius: 4px;
      min-width: 200px;
            min-height: 72px;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
        }
        .status-bar.idle-status {
            justify-content: center;
    }
        .status-line {
            margin: 2px 0;
        }
        .status-bar span {
            padding: 2px 6px;
            border-radius: 4px;
        }
    .printer-link {
      display: block;
      text-decoration: none;
      color: inherit;
        margin-top: 8px;
    }
    .printer-link span {
        text-decoration: underline;
      padding: 2px 6px;
      border-radius: 4px;
    }
    .printer-link:hover span {
      opacity: 0.8;
    }
  </style>
</head>
<body>
  <h1>PS:One 3D Printer Dashboard</h1>
  <p>Select a printer:</p>
    <div class="printer-row">
    {% for printer_status in top_printer_statuses %}
  <div class="printer-item">
        <div class="status-bar{% if printer_status.status == 'IDLE' %} idle-status{% endif %}" style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">
      <div class="status-line">Status: {{ printer_status.status }}</div>
      {% if printer_status.progress is not none and printer_status.status == 'PRINTING' %}
      <div class="status-line">Progress: {{ "%.1f"|format(printer_status.progress) }}%</div>
      {% endif %}
      {% if printer_status.time_remaining is not none and printer_status.status == 'PRINTING' %}
      <div class="status-line">Time: {{ time_format(printer_status.time_remaining) }}</div>
      {% endif %}
    </div>
        <a href="https://{{ printer_status.domain }}" class="printer-link"><span style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">{{ printer_status.domain }} ({{ printer_status.model_name }})</span></a>
  </div>
  {% endfor %}
    </div>
    <div class="printer-row">
    {% for printer_status in bottom_printer_statuses %}
    <div class="printer-item">
        <div class="status-bar{% if printer_status.status == 'IDLE' %} idle-status{% endif %}" style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">
            <div class="status-line">Status: {{ printer_status.status }}</div>
            {% if printer_status.progress is not none and printer_status.status == 'PRINTING' %}
            <div class="status-line">Progress: {{ "%.1f"|format(printer_status.progress) }}%</div>
            {% endif %}
            {% if printer_status.time_remaining is not none and printer_status.status == 'PRINTING' %}
            <div class="status-line">Time: {{ time_format(printer_status.time_remaining) }}</div>
            {% endif %}
        </div>
                <a href="https://{{ printer_status.domain }}" class="printer-link"><span style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">{{ printer_status.domain }} ({{ printer_status.model_name }})</span></a>
    </div>
    {% endfor %}
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    statuses = []
    for domain in PRINTER_ORDER:
        printer_config = PRINTERS.get(domain)
        if printer_config:
            status = fetch_printer_status(printer_config)
            statuses.append(status)

    top_printer_statuses = statuses[:2]
    bottom_printer_statuses = statuses[2:]

    return render_template_string(
        HTML_TEMPLATE,
        top_printer_statuses=top_printer_statuses,
        bottom_printer_statuses=bottom_printer_statuses,
        time_format=format_time,
    )


@app.errorhandler(500)
def handle_error(error):
    logging.exception("Unhandled error")
    return "Internal Server Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
