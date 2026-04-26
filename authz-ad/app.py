import logging
import os
import ssl
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from typing import Optional
from urllib.parse import urlsplit

from ldap3 import SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

NESTED_GROUP_MATCHING_RULE_OID = "1.2.840.113556.1.4.1941"


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    enforce: bool
    header_email: str
    header_user: str
    cache_ttl_seconds: int
    negative_cache_ttl_seconds: int
    ldap_uri: str
    bind_dn: str
    bind_password: str
    base_dn: str
    allowed_group_dn: str
    identity_attrs: list[str]
    connect_timeout_seconds: float
    read_timeout_seconds: int
    tls_validate: bool
    tls_ca_cert_file: str


@dataclass
class CacheEntry:
    allow: bool
    reason: str
    expires_at: float


CACHE: dict[str, CacheEntry] = {}
CACHE_LOCK = RLock()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise RuntimeError("AUTHZ_AD_LISTEN must be in host:port format")
    host, port_str = value.rsplit(":", 1)
    return host, int(port_str)


def load_config() -> Config:
    listen = os.environ.get("AUTHZ_AD_LISTEN", "0.0.0.0:8081")
    listen_host, listen_port = _parse_listen(listen)

    identity_attrs_raw = os.environ.get(
        "AUTHZ_AD_IDENTITY_ATTRS", "mail,userPrincipalName,sAMAccountName"
    )
    identity_attrs = [item.strip() for item in identity_attrs_raw.split(",") if item.strip()]
    if not identity_attrs:
        raise RuntimeError("AUTHZ_AD_IDENTITY_ATTRS must include at least one attribute")

    return Config(
        listen_host=listen_host,
        listen_port=listen_port,
        enforce=_env_bool("AUTHZ_AD_ENFORCE", True),
        header_email=os.environ.get("AUTHZ_AD_HEADER_EMAIL", "X-Auth-Request-Email"),
        header_user=os.environ.get("AUTHZ_AD_HEADER_USER", "X-Auth-Request-User"),
        cache_ttl_seconds=_env_int("AUTHZ_AD_CACHE_TTL_SECONDS", 300),
        negative_cache_ttl_seconds=_env_int("AUTHZ_AD_NEGATIVE_CACHE_TTL_SECONDS", 60),
        ldap_uri=_env_required("AUTHZ_AD_LDAP_URI"),
        bind_dn=_env_required("AUTHZ_AD_BIND_DN"),
        bind_password=_env_required("AUTHZ_AD_BIND_PASSWORD"),
        base_dn=_env_required("AUTHZ_AD_BASE_DN"),
        allowed_group_dn=_env_required("AUTHZ_AD_ALLOWED_GROUP_DN"),
        identity_attrs=identity_attrs,
        connect_timeout_seconds=_env_float("AUTHZ_AD_CONNECT_TIMEOUT_SECONDS", 2.0),
        read_timeout_seconds=_env_int("AUTHZ_AD_READ_TIMEOUT_SECONDS", 2),
        tls_validate=_env_bool("AUTHZ_AD_TLS_VALIDATE", True),
        tls_ca_cert_file=os.environ.get("AUTHZ_AD_TLS_CA_CERT_FILE", "").strip(),
    )


def _normalize_identity(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().lower()


def _get_cached_decision(identity: str) -> Optional[CacheEntry]:
    now = time.time()
    with CACHE_LOCK:
        entry = CACHE.get(identity)
        if not entry:
            return None
        if entry.expires_at <= now:
            del CACHE[identity]
            return None
        return entry


def _set_cached_decision(identity: str, allow: bool, reason: str, ttl: int) -> None:
    expires_at = time.time() + max(ttl, 0)
    with CACHE_LOCK:
        CACHE[identity] = CacheEntry(allow=allow, reason=reason, expires_at=expires_at)


def _build_identity_filter(identity: str, attrs: list[str]) -> str:
    escaped_identity = escape_filter_chars(identity)
    terms = "".join(f"({attr}={escaped_identity})" for attr in attrs)
    return f"(&(objectCategory=person)(objectClass=user)(|{terms}))"


def _build_member_filter(user_dn: str, group_dn: str) -> str:
    escaped_user_dn = escape_filter_chars(user_dn)
    escaped_group_dn = escape_filter_chars(group_dn)
    return (
        "(&(objectCategory=person)(objectClass=user)"
        f"(distinguishedName={escaped_user_dn})"
        f"(memberOf:{NESTED_GROUP_MATCHING_RULE_OID}:={escaped_group_dn}))"
    )


def _build_tls_config(cfg: Config) -> Optional[Tls]:
    if not cfg.ldap_uri.lower().startswith("ldaps://"):
        return None

    validate_mode = ssl.CERT_REQUIRED if cfg.tls_validate else ssl.CERT_NONE
    kwargs = {"validate": validate_mode}
    if cfg.tls_ca_cert_file:
        kwargs["ca_certs_file"] = cfg.tls_ca_cert_file
    return Tls(**kwargs)


def _parse_ldap_endpoint(ldap_uri: str) -> tuple[str, int, bool]:
    if "://" in ldap_uri:
        parsed = urlsplit(ldap_uri)
        if parsed.scheme not in {"ldap", "ldaps"}:
            raise RuntimeError("AUTHZ_AD_LDAP_URI scheme must be ldap:// or ldaps://")
        if not parsed.hostname:
            raise RuntimeError("AUTHZ_AD_LDAP_URI must include a hostname")
        use_ssl = parsed.scheme == "ldaps"
        default_port = 636 if use_ssl else 389
        return parsed.hostname, parsed.port or default_port, use_ssl

    host = ldap_uri.strip()
    if not host:
        raise RuntimeError("AUTHZ_AD_LDAP_URI must not be empty")
    if ":" in host:
        host_name, port_value = host.rsplit(":", 1)
        return host_name, int(port_value), False
    return host, 389, False


def check_authorized(identity: str, cfg: Config) -> tuple[bool, str]:
    ldap_host, ldap_port, ldap_use_ssl = _parse_ldap_endpoint(cfg.ldap_uri)
    server = Server(
        host=ldap_host,
        port=ldap_port,
        use_ssl=ldap_use_ssl,
        connect_timeout=cfg.connect_timeout_seconds,
        tls=_build_tls_config(cfg),
    )

    with Connection(
        server,
        user=cfg.bind_dn,
        password=cfg.bind_password,
        auto_bind=True,
        receive_timeout=cfg.read_timeout_seconds,
    ) as conn:
        user_filter = _build_identity_filter(identity, cfg.identity_attrs)
        conn.search(
            search_base=cfg.base_dn,
            search_filter=user_filter,
            search_scope=SUBTREE,
            attributes=["distinguishedName"],
            size_limit=2,
        )
        if len(conn.entries) == 0:
            return False, "user_not_found"
        if len(conn.entries) > 1:
            return False, "identity_ambiguous"

        user_dn = str(conn.entries[0].entry_dn)

        member_filter = _build_member_filter(user_dn, cfg.allowed_group_dn)
        conn.search(
            search_base=cfg.base_dn,
            search_filter=member_filter,
            search_scope=SUBTREE,
            attributes=["distinguishedName"],
            size_limit=1,
        )
        if len(conn.entries) == 0:
            return False, "group_missing"

        return True, "group_match"


def authorize_request(headers, cfg: Config) -> tuple[bool, str]:
    email_identity = _normalize_identity(headers.get(cfg.header_email, ""))
    user_identity = _normalize_identity(headers.get(cfg.header_user, ""))
    identity = email_identity or user_identity

    if not identity:
        return False, "missing_identity"

    cached = _get_cached_decision(identity)
    if cached:
        return cached.allow, f"cache_{cached.reason}"

    try:
        allow, reason = check_authorized(identity, cfg)
    except LDAPException:
        logging.exception("LDAP query failed for identity=%s", identity)
        return False, "ldap_error"
    except Exception:
        logging.exception("Authorization failure for identity=%s", identity)
        return False, "internal_error"

    ttl = cfg.cache_ttl_seconds if allow else cfg.negative_cache_ttl_seconds
    _set_cached_decision(identity, allow, reason, ttl)
    return allow, reason


CFG = load_config()
logging.basicConfig(
    level=os.environ.get("AUTHZ_AD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)


class AuthzHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request(send_body=False)

    def do_POST(self):
        self._handle_request()

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _write_response(
        self,
        status: int,
        decision: str,
        reason: str,
        body: str,
        send_body: bool = True,
        content_type: str = "text/plain; charset=utf-8",
    ):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Authz-Decision", decision)
        self.send_header("X-Authz-Reason", reason)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if send_body:
            self.wfile.write(payload)
            self.wfile.flush()
        self.close_connection = True

    def _handle_request(self, send_body: bool = True):
        if self.path == "/healthz":
            self._write_response(200, "allow", "healthy", "ok\n", send_body=send_body)
            return

        if self.path != "/authz":
            self._write_response(404, "deny", "not_found", "not found\n", send_body=send_body)
            return

        allow, reason = authorize_request(self.headers, CFG)

        if allow:
            logging.info("AuthZ allow reason=%s", reason)
            self._write_response(200, "allow", reason, "authorized\n", send_body=send_body)
            return

        if not CFG.enforce:
            logging.info("AuthZ dry-run deny reason=%s", reason)
            self._write_response(200, "allow", f"dry_run_{reason}", "authorized (dry run)\n", send_body=send_body)
            return

        logging.info("AuthZ deny reason=%s", reason)
        self._write_response(
            403,
            "deny",
            reason,
            "<!doctype html>"
            "<html lang=\"en\">"
            "<head><meta charset=\"utf-8\"><title>Not Authorized</title></head>"
            "<body>"
            "<p>Sorry, you are not currently authorized to use the 3D printers.</p>"
            "<p>To become authorized, complete the Canvas course at "
            "<a href=\"https://psone.link/3dauth\">https://psone.link/3dauth</a></p>"
            "</body></html>\n",
            send_body=send_body,
            content_type="text/html; charset=utf-8",
        )


if __name__ == "__main__":
    logging.info(
        "Starting authz-ad on %s:%s enforce=%s group=%s",
        CFG.listen_host,
        CFG.listen_port,
        CFG.enforce,
        CFG.allowed_group_dn,
    )
    server = ThreadingHTTPServer((CFG.listen_host, CFG.listen_port), AuthzHandler)
    server.serve_forever()
