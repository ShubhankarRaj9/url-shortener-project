#!/bin/sh
# Alertmanager can't read environment variables in its config, so render the config at start-up.
# The rendered file lives on a tmpfs and never touches the image or a volume.
set -eu

q() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }   # escape for a YAML double-quoted string
CFG=/tmp/alertmanager.yml

cat > "$CFG" <<'YAML'
global:
  resolve_timeout: 5m
route:
  receiver: ops
  group_by: [alertname, severity]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - receiver: ops
      matchers:
        - severity="critical"
      repeat_interval: 1h
inhibit_rules:
  - source_matchers:
      - severity="critical"
    target_matchers:
      - severity="warning"
    equal: [alertname]
receivers:
  - name: ops
YAML

configured=0
if [ -n "${ALERT_EMAIL_TO:-}" ] && [ -n "${SMTP_HOST:-}" ]; then
  configured=1
  case "${SMTP_SECURITY:-starttls}" in none) tls=false ;; *) tls=true ;; esac
  {
    printf '%s\n' "    email_configs:"
    printf '%s\n' "      - to: \"$(q "$ALERT_EMAIL_TO")\""
    printf '%s\n' "        from: \"$(q "${EMAIL_FROM:-alertmanager@localhost}")\""
    printf '%s\n' "        smarthost: \"$(q "$SMTP_HOST"):${SMTP_PORT:-587}\""
    if [ -n "${SMTP_USERNAME:-}" ]; then
      printf '%s\n' "        auth_username: \"$(q "$SMTP_USERNAME")\""
      printf '%s\n' "        auth_password: \"$(q "${SMTP_PASSWORD:-}")\""
    fi
    printf '%s\n' "        require_tls: $tls"
    printf '%s\n' "        send_resolved: true"
  } >> "$CFG"
fi
if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
  configured=1
  {
    printf '%s\n' "    webhook_configs:"
    printf '%s\n' "      - url: \"$(q "$ALERT_WEBHOOK_URL")\""
    printf '%s\n' "        send_resolved: true"
  } >> "$CFG"
fi
[ "$configured" = 1 ] || printf '%s\n' "WARNING: no ALERT_EMAIL_TO / ALERT_WEBHOOK_URL set; alerts are only visible in the UI" >&2

exec /bin/alertmanager --config.file="$CFG" --storage.path=/alertmanager "$@"
