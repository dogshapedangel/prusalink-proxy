import logging
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

logging.basicConfig(
    level=os.environ.get("DIGEST_PROXY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

PORT = int(os.environ.get("DIGEST_PROXY_PORT", "8080"))
TIMEOUT = float(os.environ.get("DIGEST_PROXY_TIMEOUT", "120"))

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
RESPONSE_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {"www-authenticate"}
REQUEST_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {"authorization", "host"}

session = requests.Session()


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    domain: str
    upstream: str
    username: str
    password: str
    upstream_host: str


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
DEFAULT_PRINTER = next(iter(PRINTERS.values())) if len(PRINTERS) == 1 else None


class DigestProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._proxy_request()

    def do_HEAD(self):
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def do_PUT(self):
        self._proxy_request()

    def do_PATCH(self):
        self._proxy_request()

    def do_DELETE(self):
        self._proxy_request()

    def do_OPTIONS(self):
        self._proxy_request()

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _read_body(self):
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return None
        try:
            length = int(content_length)
        except ValueError:
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _resolve_printer(self) -> PrinterConfig:
        requested_host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        host_key = requested_host.split(",", 1)[0].strip().split(":", 1)[0].lower()

        if host_key in PRINTERS:
            return PRINTERS[host_key]
        if DEFAULT_PRINTER is not None:
            return DEFAULT_PRINTER

        raise KeyError(host_key or "<missing host>")

    def _proxy_request(self):
        try:
            printer = self._resolve_printer()
        except KeyError as exc:
            host_name = exc.args[0]
            logging.warning("Rejecting request for unconfigured host %s", host_name)
            message = f"No printer is configured for host: {host_name}\n".encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            return

        url = f"{printer.upstream}{self.path}"
        body = self._read_body()

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in REQUEST_HEADERS_TO_STRIP
        }
        headers["Host"] = printer.upstream_host

        try:
            response = session.request(
                method=self.command,
                url=url,
                headers=headers,
                data=body,
                allow_redirects=False,
                stream=True,
                timeout=TIMEOUT,
                auth=HTTPDigestAuth(printer.username, printer.password),
            )
        except requests.RequestException as exc:
            logging.exception(
                "Upstream request failed for printer=%s host=%s %s %s",
                printer.name,
                printer.domain,
                self.command,
                self.path,
            )
            message = f"PrusaLink upstream request failed: {exc}\n".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            return

        if response.status_code == 401:
            logging.error(
                "PrusaLink rejected the configured credentials for printer=%s host=%s %s %s",
                printer.name,
                printer.domain,
                self.command,
                self.path,
            )
            message = f"PrusaLink rejected the configured proxy credentials for {printer.domain}.\n".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            response.close()
            return

        self.send_response(response.status_code)
        self.send_header("Connection", "close")
        for key, value in response.headers.items():
            if key.lower() not in RESPONSE_HEADERS_TO_STRIP:
                self.send_header(key, value)
        self.end_headers()

        if self.command != "HEAD":
            response.raw.decode_content = False
            for chunk in response.raw.stream(64 * 1024, decode_content=False):
                if chunk:
                    self.wfile.write(chunk)
            self.wfile.flush()

        self.close_connection = True
        response.close()


if __name__ == "__main__":
    loaded_printers = ", ".join(
        f"{printer.name}:{printer.domain}->{printer.upstream}" for printer in PRINTERS.values()
    )
    logging.info("Starting digest proxy on 0.0.0.0:%s for %s", PORT, loaded_printers)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DigestProxyHandler)
    server.serve_forever()
