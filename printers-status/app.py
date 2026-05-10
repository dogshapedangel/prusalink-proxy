import logging
import os
import re
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth
import yaml
from flask import Flask, Response, abort, render_template_string, stream_with_context

logging.basicConfig(
    level=os.environ.get("STATUS_APP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

PORT = int(os.environ.get("STATUS_APP_PORT", "8888"))
TIMEOUT = float(os.environ.get("STATUS_APP_TIMEOUT", "10"))
CAMERA_FEEDS_ENABLED = os.environ.get("STATUS_APP_CAMERA_FEEDS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
REFRESH_INTERVAL = int(os.environ.get("STATUS_APP_REFRESH_INTERVAL", "30"))

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
    camera_stream: Optional[str] = None


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
GO2RTC_BASE_URL = os.environ.get("GO2RTC_BASE_URL", "http://go2rtc:1984").rstrip("/")
GO2RTC_CONFIG_PATH = os.environ.get("GO2RTC_CONFIG_PATH", "/config/go2rtc.yaml")

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


def load_go2rtc_stream_names(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logging.warning("go2rtc config file not found at %s", path)
        return set()
    except OSError as exc:
        logging.warning("Failed to read go2rtc config %s: %s", path, exc)
        return set()
    except yaml.YAMLError as exc:
        logging.warning("Invalid go2rtc YAML in %s: %s", path, exc)
        return set()

    streams = config.get("streams", {})
    if not isinstance(streams, dict):
        return set()

    return {str(name).strip() for name in streams.keys() if str(name).strip()}


def build_domain_to_camera_map(printers: dict[str, PrinterConfig], stream_names: set[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    stream_lookup = {name.lower(): name for name in stream_names}

    for domain, printer in printers.items():
        candidates = [
            printer.name.strip().lower(),
            domain.split(".", 1)[0].strip().lower(),
        ]
        for candidate in candidates:
            if candidate in stream_lookup:
                mapping[domain] = stream_lookup[candidate]
                break

    return mapping


GO2RTC_STREAMS = load_go2rtc_stream_names(GO2RTC_CONFIG_PATH)
DOMAIN_TO_CAMERA_STREAM = build_domain_to_camera_map(PRINTERS, GO2RTC_STREAMS)


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
            camera_stream=DOMAIN_TO_CAMERA_STREAM.get(printer.domain),
        )
    except requests.RequestException as e:
        logging.warning(f"Failed to fetch status for {printer.name}: {e}")
        return PrinterStatus(
            name=printer.name,
            domain=printer.domain,
            status="OFFLINE",
            error=str(e),
            model_name=colors.get("name", ""),
            camera_stream=DOMAIN_TO_CAMERA_STREAM.get(printer.domain),
            text_color=colors["text"],
            bg_color=colors["bg"],
        )


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PS:One 3D Printers</title>
  {% if refresh_interval > 0 %}<meta http-equiv="refresh" content="{{ refresh_interval }}">{% endif %}
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
        .camera-button {
            display: inline-block;
            margin-top: 8px;
            padding: 6px 10px;
            font-size: 0.8em;
            font-weight: 700;
            font-family: inherit;
            letter-spacing: 0.04em;
            text-decoration: none;
            border-radius: 4px;
            border: 1px solid rgba(0, 0, 0, 0.4);
            color: #111;
            background: #f7f7f7;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
        }
        .camera-button:hover {
            filter: brightness(0.95);
        }
        .camera-button.disabled {
            opacity: 0.55;
            cursor: not-allowed;
            pointer-events: none;
        }
        .camera-actions {
            display: flex;
            justify-content: center;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 8px;
        }
        .camera-actions .camera-button {
            margin-top: 0;
        }
        .inline-camera {
            margin: 22px auto 10px;
            width: min(96vw, 1200px);
            background: rgba(0, 0, 0, 0.07);
            border: 1px solid rgba(0, 0, 0, 0.2);
            border-radius: 6px;
            padding: 12px;
            box-sizing: border-box;
        }
        .inline-camera.hidden {
            display: none;
        }
        .inline-camera-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 10px;
            flex-wrap: wrap;
        }
        .inline-camera-title {
            margin: 0;
            font-size: 1rem;
            text-align: left;
        }
        .inline-camera-controls {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .inline-camera-frame {
            display: block;
            width: 100%;
            border: 2px solid #444;
            border-radius: 6px;
            background: #000;
            min-height: 280px;
            aspect-ratio: 16 / 9;
        }
  </style>
</head>
<body>
  <h1>PS:One 3D Printer Dashboard</h1>
  <p>Select a printer:</p>
    <div class="printer-row">
    {% for printer_status in top_printer_statuses %}
  <div class="printer-item">
        <div class="status-bar" style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">
      <div class="status-line">Status: {{ printer_status.status }}</div>
      {% if printer_status.progress is not none and printer_status.status == 'PRINTING' %}
      <div class="status-line">Progress: {{ "%.1f"|format(printer_status.progress) }}%</div>
      {% endif %}
      {% if printer_status.time_remaining is not none and printer_status.status == 'PRINTING' %}
      <div class="status-line">Time: {{ time_format(printer_status.time_remaining) }}</div>
      {% endif %}
    </div>
        <a href="https://{{ printer_status.domain }}" class="printer-link"><span style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">{{ printer_status.domain }} ({{ printer_status.model_name }})</span></a>
            {% if camera_feeds_enabled and printer_status.camera_stream %}
                <div class="camera-actions">
                  <a href="/camera/stream.html?src={{ printer_status.camera_stream }}&mode=webrtc" class="camera-button" data-camera-inline data-stream-name="{{ printer_status.camera_stream }}">CAMERA FEED</a>
                </div>
            {% elif camera_feeds_enabled %}
                <span class="camera-button disabled">CAMERA FEED</span>
                {% endif %}
  </div>
  {% endfor %}
    </div>
    <div class="printer-row">
    {% for printer_status in bottom_printer_statuses %}
    <div class="printer-item">
        <div class="status-bar" style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">
            <div class="status-line">Status: {{ printer_status.status }}</div>
            {% if printer_status.progress is not none and printer_status.status == 'PRINTING' %}
            <div class="status-line">Progress: {{ "%.1f"|format(printer_status.progress) }}%</div>
            {% endif %}
            {% if printer_status.time_remaining is not none and printer_status.status == 'PRINTING' %}
            <div class="status-line">Time: {{ time_format(printer_status.time_remaining) }}</div>
            {% endif %}
        </div>
                <a href="https://{{ printer_status.domain }}" class="printer-link"><span style="color: {{ printer_status.text_color }}; background: {{ printer_status.bg_color }};">{{ printer_status.domain }} ({{ printer_status.model_name }})</span></a>
                                {% if camera_feeds_enabled and printer_status.camera_stream %}
                                                                <div class="camera-actions">
                                                                    <a href="/camera/stream.html?src={{ printer_status.camera_stream }}&mode=webrtc" class="camera-button" data-camera-inline data-stream-name="{{ printer_status.camera_stream }}">CAMERA FEED</a>
                                                                </div>
                                {% elif camera_feeds_enabled %}
                                <span class="camera-button disabled">CAMERA FEED</span>
                                {% endif %}
    </div>
    {% endfor %}
    </div>
        <section id="inline-camera" class="inline-camera hidden" aria-live="polite">
            <div class="inline-camera-header">
                <h2 id="inline-camera-title" class="inline-camera-title">Camera feed</h2>
                <div class="inline-camera-controls">
                    <a id="inline-camera-popout" class="camera-button" href="#" target="_blank" rel="noopener noreferrer">POP OUT</a>
                    <button id="inline-camera-close" type="button" class="camera-button">CLOSE</button>
                </div>
            </div>
            <iframe
                id="inline-camera-frame"
                class="inline-camera-frame"
                src="about:blank"
                title="Camera feed viewer"
                loading="lazy"
                allow="autoplay; camera; microphone; fullscreen"
            ></iframe>
        </section>
        <script>
            const inlineCameraContainer = document.getElementById('inline-camera');
            const inlineCameraFrame = document.getElementById('inline-camera-frame');
            const inlineCameraTitle = document.getElementById('inline-camera-title');
            const inlineCameraPopout = document.getElementById('inline-camera-popout');
            const inlineCameraClose = document.getElementById('inline-camera-close');

            document.addEventListener('click', (event) => {
                const cameraLink = event.target.closest('[data-camera-inline]');
                if (!cameraLink) {
                    return;
                }

                event.preventDefault();
                const href = cameraLink.getAttribute('href');
                if (!href) {
                    return;
                }

                const streamName = cameraLink.getAttribute('data-stream-name') || 'camera';
                inlineCameraTitle.textContent = `${streamName} camera`;
                inlineCameraFrame.src = href;
                inlineCameraPopout.href = href;
                inlineCameraContainer.classList.remove('hidden');
                inlineCameraContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
            });

            inlineCameraClose.addEventListener('click', () => {
                inlineCameraContainer.classList.add('hidden');
                inlineCameraFrame.src = 'about:blank';
                inlineCameraPopout.href = '#';
            });
        </script>
</body>
</html>
"""


CAMERA_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Camera Feed - {{ stream_name }}</title>
    <style>
        body {
            margin: 0;
            padding: 0;
            background: #111;
            color: #eee;
            font-family: 'Courier New', Courier, monospace;
            text-align: center;
        }
        h1 {
            font-size: 1.2rem;
            margin: 16px 8px;
        }
        .frame {
            width: min(96vw, 1200px);
            max-height: calc(100vh - 70px);
            border: 2px solid #444;
            border-radius: 6px;
            object-fit: contain;
            background: #000;
        }
        .status {
            margin: 8px 0 16px;
            color: #bbb;
            font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <h1>{{ stream_name }} camera</h1>
    <div class="status">Refreshing live frame every second</div>
    <img id="camera-frame" class="frame" src="/camera/{{ stream_name }}/frame.jpeg" alt="Camera feed for {{ stream_name }}">
    <script>
        const frame = document.getElementById('camera-frame');
        const baseUrl = '/camera/{{ stream_name }}/frame.jpeg';

        function refreshFrame() {
            frame.src = `${baseUrl}?t=${Date.now()}`;
        }

        setInterval(refreshFrame, 1000);
    </script>
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
        camera_feeds_enabled=CAMERA_FEEDS_ENABLED,
        refresh_interval=REFRESH_INTERVAL,
        time_format=format_time,
    )


@app.route("/camera/<stream_name>")
def camera_view(stream_name: str):
    if stream_name not in GO2RTC_STREAMS:
        abort(404)
    return render_template_string(CAMERA_TEMPLATE, stream_name=stream_name)


@app.route("/camera/<stream_name>/stream.mjpeg")
def camera_stream(stream_name: str):
    if stream_name not in GO2RTC_STREAMS:
        abort(404)

    upstream_url = f"{GO2RTC_BASE_URL}/api/stream.mjpeg?src={stream_name}"

    try:
        upstream = session.get(upstream_url, timeout=TIMEOUT, stream=True)
        upstream.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Camera stream proxy failed for %s: %s", stream_name, exc)
        return "Camera feed unavailable", 502

    content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=16 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), content_type=content_type)


@app.route("/camera/<stream_name>/frame.jpeg")
def camera_frame(stream_name: str):
    if stream_name not in GO2RTC_STREAMS:
        abort(404)

    upstream_url = f"{GO2RTC_BASE_URL}/api/frame.jpeg?src={stream_name}"

    try:
        upstream = session.get(upstream_url, timeout=TIMEOUT)
        upstream.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("Camera frame proxy failed for %s: %s", stream_name, exc)
        return "Camera feed unavailable", 502

    return Response(upstream.content, content_type=upstream.headers.get("Content-Type", "image/jpeg"))


@app.errorhandler(500)
def handle_error(error):
    logging.exception("Unhandled error")
    return "Internal Server Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
