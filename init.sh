#!/bin/bash

set -e

# Check required environment variables
if [ -z "$PRUSALINK_USERNAME" ] || [ -z "$PRUSALINK_PASSWORD" ] || [ -z "$PRUSALINK_URL" ]; then
    echo "Error: Missing required environment variables"
    echo "   Required: PRUSALINK_USERNAME, PRUSALINK_PASSWORD, PRUSALINK_URL"
    exit 1
fi

# Generate Base64-encoded credentials
CREDENTIALS="$PRUSALINK_USERNAME:$PRUSALINK_PASSWORD"
BASE64_CREDS=$(printf '%s' "$CREDENTIALS" | base64 | tr -d '\n')
PRUSALINK_UPSTREAM="${PRUSALINK_URL%/}"

# Create Caddyfile from template
cp /etc/caddy/Caddyfile.template /etc/caddy/Caddyfile

DOMAIN_VALUE="${DOMAIN:-printers.example.com}"
CADDY_DEV_MODE_VALUE=$(echo "${CADDY_DEV_MODE:-false}" | tr '[:upper:]' '[:lower:]')
IS_DEV_MODE=false

case "$CADDY_DEV_MODE_VALUE" in
    true|1|yes)
        IS_DEV_MODE=true
        ;;
esac

if [ "$IS_DEV_MODE" != "true" ]; then
    if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
        echo "Error: CLOUDFLARE_API_TOKEN is required when CADDY_DEV_MODE is false"
        exit 1
    fi

    if [ -z "$ACME_EMAIL" ]; then
        echo "Error: ACME_EMAIL is required when CADDY_DEV_MODE is false"
        exit 1
    fi
fi

GLOBAL_OPTIONS_FILE=$(mktemp)

{
    echo "{"
    if [ -n "$ACME_EMAIL" ]; then
        echo "  email ${ACME_EMAIL}"
    fi

    if [ "$IS_DEV_MODE" = "true" ]; then
        echo "  auto_https off"
    else
        # Fronting reverse proxies sometimes connect to this container by IP
        # and omit SNI; make Caddy fall back to the public hostname certificate.
        echo "  default_sni ${DOMAIN_VALUE}"
        echo "  acme_dns cloudflare {env.CLOUDFLARE_API_TOKEN}"
    fi
    echo "}"
    echo
} > "$GLOBAL_OPTIONS_FILE"

cat "$GLOBAL_OPTIONS_FILE" /etc/caddy/Caddyfile > /tmp/Caddyfile && mv /tmp/Caddyfile /etc/caddy/Caddyfile
rm -f "$GLOBAL_OPTIONS_FILE"

if [ "$IS_DEV_MODE" = "true" ]; then
    SITE_ADDRESS="http://${DOMAIN_VALUE}"
    sed -i '/# DEV_REDIRECT_START/,/# DEV_REDIRECT_END/d' /etc/caddy/Caddyfile
else
    SITE_ADDRESS="${DOMAIN_VALUE}"
    sed -i '/# DEV_REDIRECT_START/d; /# DEV_REDIRECT_END/d' /etc/caddy/Caddyfile
fi

# Replace placeholders
sed -i "s|{{SITE_ADDRESS}}|${SITE_ADDRESS}|g" /etc/caddy/Caddyfile
sed -i "s|{{PRUSALINK_URL}}|${PRUSALINK_UPSTREAM}|g" /etc/caddy/Caddyfile
sed -i "s|{{BASE64_CREDENTIALS}}|${BASE64_CREDS}|g" /etc/caddy/Caddyfile

# Start Caddy with provided arguments
exec caddy run --config /etc/caddy/Caddyfile "$@"
