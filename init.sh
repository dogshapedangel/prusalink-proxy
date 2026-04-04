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

# Start Caddy with provided arguments
exec caddy run --config /etc/caddy/Caddyfile "$@"
