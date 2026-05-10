#!/bin/sh

set -eu

# Create Caddyfile from template
cp /etc/caddy/Caddyfile.template /etc/caddy/Caddyfile

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "Error: CLOUDFLARE_API_TOKEN is required"
    exit 1
fi

if [ -z "${ACME_EMAIL:-}" ]; then
    echo "Error: ACME_EMAIL is required"
    exit 1
fi

normalize_printer_id() {
    printf '%s' "$1" | tr '[:lower:]-.' '[:upper:]__'
}

append_domain() {
    domain_value="$1"
    if [ -z "$SITE_ADDRESSES" ]; then
        SITE_ADDRESSES="$domain_value"
        PRIMARY_DOMAIN="$domain_value"
    else
        SITE_ADDRESSES="$SITE_ADDRESSES, $domain_value"
    fi
}

SITE_ADDRESSES=""
PRIMARY_DOMAIN=""

if [ -n "${PRINTER_IDS:-}" ]; then
    OLD_IFS=$IFS
    IFS=', '
    set -- ${PRINTER_IDS}
    IFS=$OLD_IFS

    for raw_id in "$@"; do
        [ -n "$raw_id" ] || continue
        printer_key=$(normalize_printer_id "$raw_id")
        domain_var="PRINTER_${printer_key}_DOMAIN"
        domain_value=$(eval "printf '%s' \"\${$domain_var:-}\"")

        if [ -z "$domain_value" ]; then
            echo "Error: $domain_var is required when PRINTER_IDS is set"
            exit 1
        fi

        append_domain "$domain_value"
    done
elif [ -n "${DOMAIN:-}" ]; then
    append_domain "$DOMAIN"
else
    echo "Error: set DOMAIN for a single printer or PRINTER_IDS with PRINTER_<ID>_DOMAIN entries"
    exit 1
fi

# Always include the printer index host.
append_domain "printers.psone.space"

to_lower() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

CAMERA_FEEDS_REQUIRE_OAUTH_RAW=$(to_lower "${CAMERA_FEEDS_REQUIRE_OAUTH:-true}")
case "$CAMERA_FEEDS_REQUIRE_OAUTH_RAW" in
    1|true|yes|on)
        CAMERA_FEEDS_REQUIRE_OAUTH_BOOL=true
        ;;
    0|false|no|off)
        CAMERA_FEEDS_REQUIRE_OAUTH_BOOL=false
        ;;
    *)
        echo "Error: CAMERA_FEEDS_REQUIRE_OAUTH must be true/false (or 1/0, yes/no, on/off)"
        exit 1
        ;;
esac

CAMERA_ROUTES_BLOCK=$(cat <<'EOF'
    # Serve go2rtc's browser player and assets same-origin so WebRTC signaling,
    # JS modules, and media endpoints stay on the authenticated host.
    handle_path /camera/* {
      reverse_proxy go2rtc:1984
    }

    # Legacy fallback: camera MJPEG requested directly on a printer subdomain.
    handle /camera-stream {
      rewrite * /api/stream.mjpeg?src={labels.0}
      reverse_proxy go2rtc:1984
    }
EOF
)

if [ "$CAMERA_FEEDS_REQUIRE_OAUTH_BOOL" = true ]; then
    CAMERA_ROUTES_PREAUTH="# camera routes are OAuth-protected (enabled via CAMERA_FEEDS_REQUIRE_OAUTH=true)"
    CAMERA_ROUTES_POSTAUTH="$CAMERA_ROUTES_BLOCK"
else
    CAMERA_ROUTES_PREAUTH="$CAMERA_ROUTES_BLOCK"
    CAMERA_ROUTES_POSTAUTH="# camera routes bypass OAuth (enabled via CAMERA_FEEDS_REQUIRE_OAUTH=false)"
fi

GLOBAL_OPTIONS_FILE=$(mktemp)

{
    echo "{"
    echo "  email ${ACME_EMAIL}"
    echo "  default_sni ${PRIMARY_DOMAIN}"
    echo "  acme_dns cloudflare {env.CLOUDFLARE_API_TOKEN}"
    echo "}"
    echo
} > "$GLOBAL_OPTIONS_FILE"

cat "$GLOBAL_OPTIONS_FILE" /etc/caddy/Caddyfile > /tmp/Caddyfile && mv /tmp/Caddyfile /etc/caddy/Caddyfile
rm -f "$GLOBAL_OPTIONS_FILE"

ESCAPED_SITE_ADDRESSES=$(printf '%s' "$SITE_ADDRESSES" | sed 's/[&/]/\\&/g')
sed -i "s|{{SITE_ADDRESSES}}|${ESCAPED_SITE_ADDRESSES}|g" /etc/caddy/Caddyfile

TMP_CADDYFILE=$(mktemp)
awk -v preauth="$CAMERA_ROUTES_PREAUTH" -v postauth="$CAMERA_ROUTES_POSTAUTH" '
{
    if ($0 ~ /\{\{CAMERA_ROUTES_PREAUTH\}\}/) {
        print preauth
    } else if ($0 ~ /\{\{CAMERA_ROUTES_POSTAUTH\}\}/) {
        print postauth
    } else {
        print
    }
}
' /etc/caddy/Caddyfile > "$TMP_CADDYFILE"
mv "$TMP_CADDYFILE" /etc/caddy/Caddyfile

# Start Caddy with provided arguments
exec caddy run --config /etc/caddy/Caddyfile "$@"
