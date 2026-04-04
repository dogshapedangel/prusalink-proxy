#!/bin/bash

set -e

# Create Caddyfile from template
cp /etc/caddy/Caddyfile.template /etc/caddy/Caddyfile

DOMAIN_VALUE="${DOMAIN:-printers.example.com}"

if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
    echo "Error: CLOUDFLARE_API_TOKEN is required"
    exit 1
fi

if [ -z "$ACME_EMAIL" ]; then
    echo "Error: ACME_EMAIL is required"
    exit 1
fi

GLOBAL_OPTIONS_FILE=$(mktemp)

{
    echo "{"
    echo "  email ${ACME_EMAIL}"
    # Fronting reverse proxies sometimes connect to this container by IP
    # and omit SNI; make Caddy fall back to the public hostname certificate.
    echo "  default_sni ${DOMAIN_VALUE}"
    echo "  acme_dns cloudflare {env.CLOUDFLARE_API_TOKEN}"
    echo "}"
    echo
} > "$GLOBAL_OPTIONS_FILE"

cat "$GLOBAL_OPTIONS_FILE" /etc/caddy/Caddyfile > /tmp/Caddyfile && mv /tmp/Caddyfile /etc/caddy/Caddyfile
rm -f "$GLOBAL_OPTIONS_FILE"

SITE_ADDRESS="${DOMAIN_VALUE}"

# Replace placeholders
sed -i "s|{{SITE_ADDRESS}}|${SITE_ADDRESS}|g" /etc/caddy/Caddyfile

# Start Caddy with provided arguments
exec caddy run --config /etc/caddy/Caddyfile "$@"
