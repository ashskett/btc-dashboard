#!/bin/bash
# =============================================================================
# AI OS — nginx reverse proxy + Let's Encrypt SSL
# Run on droplet AFTER the DNS A record for api.uncrewedmaritime.com
# has propagated to 165.232.101.253.
#
# Usage:
#   bash <(curl -s https://raw.githubusercontent.com/ashskett/btc-dashboard/main/scripts/ai-os-ssl.sh)
# =============================================================================
set -e

DOMAIN="api.uncrewedmaritime.com"
EMAIL="ashskett@gmail.com"
API_PORT=8080

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║     AI OS — nginx + SSL Setup                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# 1. Install nginx and certbot
echo "▶ Installing nginx and certbot..."
apt-get update -q
apt-get install -y nginx certbot python3-certbot-nginx 2>/dev/null | grep -E "^(Setting up|Already)" || true

# 2. Write nginx config
echo "▶ Configuring nginx for $DOMAIN..."
cat > /etc/nginx/sites-available/ai-os << NGINX
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$API_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/ai-os /etc/nginx/sites-enabled/ai-os
nginx -t && systemctl reload nginx
echo "  nginx configured."

# 3. Verify DNS resolves before requesting cert
echo "▶ Checking DNS for $DOMAIN..."
RESOLVED=$(dig +short $DOMAIN 2>/dev/null | tail -1)
if [ "$RESOLVED" != "165.232.101.253" ]; then
    echo "  WARNING: $DOMAIN resolves to '$RESOLVED', expected 165.232.101.253"
    echo "  DNS may not have propagated yet. Wait a few minutes and re-run."
    exit 1
fi
echo "  DNS OK ($RESOLVED)"

# 4. Get SSL certificate
echo "▶ Requesting Let's Encrypt certificate..."
certbot --nginx -d $DOMAIN --email $EMAIL --agree-tos --non-interactive --redirect
echo "  SSL certificate installed."

# 5. Open port 443
echo "▶ Opening port 443..."
ufw allow 443/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || true

# 6. Test
sleep 2
RESP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 https://$DOMAIN/health)
if [ "$RESP" = "200" ]; then
    echo ""
    echo "╔══════════════════════════════════════════════════╗"
    echo "║  HTTPS is live!                                  ║"
    echo "╠══════════════════════════════════════════════════╣"
    echo "║  API:  https://$DOMAIN  ║"
    echo "║  Docs: https://$DOMAIN/docs    ║"
    echo "╚══════════════════════════════════════════════════╝"
else
    echo "  Warning: HTTPS returned HTTP $RESP — check: nginx -t && journalctl -u nginx"
fi
