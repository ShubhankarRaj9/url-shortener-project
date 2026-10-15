#!/usr/bin/env bash
# Generates .env (random secrets) and secrets/htpasswd (basic-auth for the admin UIs).
#
#   scripts/init-env.sh                                  # local trial on https://localhost
#   DOMAIN=sho.example.com ACME_EMAIL=ops@example.com ADMIN_EMAIL=me@example.com scripts/init-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -e .env ] && { echo ".env already exists; refusing to overwrite it." >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

rand() { openssl rand -hex "$1"; }
DOMAIN="${DOMAIN:-localhost}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ACME_EMAIL="${ACME_EMAIL:-$ADMIN_EMAIL}"
ADMIN_PASSWORD="$(rand 12)"
OPS_USER="${OPS_USER:-ops}"
OPS_PASSWORD="$(rand 12)"

sed \
  -e "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" \
  -e "s|^ACME_EMAIL=.*|ACME_EMAIL=$ACME_EMAIL|" \
  -e "s|^ADMIN_EMAIL=.*|ADMIN_EMAIL=$ADMIN_EMAIL|" \
  -e "s|CHANGE_ME_secret_key|$(rand 32)|" \
  -e "s|CHANGE_ME_admin_password|$ADMIN_PASSWORD|" \
  -e "s|CHANGE_ME_postgres|$(rand 24)|" \
  -e "s|CHANGE_ME_redis|$(rand 24)|" \
  -e "s|CHANGE_ME_grafana|$(rand 16)|" \
  .env.example > .env

if [ "$DOMAIN" = "localhost" ]; then
  # Local trial: catch e-mails in Mailpit instead of a real SMTP server.
  sed -i \
    -e "s|^EMAIL_BACKEND=.*|EMAIL_BACKEND=smtp|" \
    -e "s|^SMTP_HOST=.*|SMTP_HOST=mailpit|" \
    -e "s|^SMTP_PORT=.*|SMTP_PORT=1025|" \
    -e "s|^SMTP_SECURITY=.*|SMTP_SECURITY=none|" \
    -e "s|^API_REPLICAS=.*|API_REPLICAS=1|" .env
  echo "COMPOSE_PROFILES=dev" >> .env
  # Local ports (override with HTTPS_PORT=... HTTP_PORT=... scripts/init-env.sh). Default: HTTPS on 8531.
  LOCAL_HTTPS="${HTTPS_PORT:-8531}"; LOCAL_HTTP="${HTTP_PORT:-8530}"
  sed -i \
    -e "s|^HTTP_PORT=.*|HTTP_PORT=$LOCAL_HTTP|" \
    -e "s|^HTTPS_PORT=.*|HTTPS_PORT=$LOCAL_HTTPS|" \
    -e "s|^PUBLIC_PORT_SUFFIX=.*|PUBLIC_PORT_SUFFIX=:$LOCAL_HTTPS|" .env
fi
chmod 600 .env

mkdir -p secrets
# apr1 (MD5-crypt) hashes are understood by Traefik's basicAuth; fine behind TLS + rate limiting.
printf '%s:%s\n' "$OPS_USER" "$(openssl passwd -apr1 "$OPS_PASSWORD")" > secrets/htpasswd
chmod 600 secrets/htpasswd

cat <<EOF
Created .env and secrets/htpasswd

  API admin login      $ADMIN_EMAIL / $ADMIN_PASSWORD
  Prometheus, Alertmanager, Traefik dashboard (basic auth)   $OPS_USER / $OPS_PASSWORD
  Grafana              admin / (GRAFANA_ADMIN_PASSWORD in .env)

Write these down now: the basic-auth password is not stored anywhere in plain text.
Next: review .env, then run: docker compose up -d --build
$( [ "$DOMAIN" = "localhost" ] && echo "Then open https://localhost:${HTTPS_PORT:-8531}/docs  (accept the self-signed certificate warning)" )
EOF
