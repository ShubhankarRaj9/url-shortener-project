# URL Shortener

FastAPI backend behind Traefik (HTTPS, rate limiting), with PostgreSQL, Redis, and a Prometheus /
Grafana / Alertmanager monitoring stack. Everything runs from one `docker-compose.yml`.

```
Internet ──► Traefik :80/:443 ──► api (×N replicas) ──► PostgreSQL   (users, links, tokens)
             │  TLS (Let's Encrypt)      │  └──────────► Redis        (link cache, click buffer, throttles)
             │  rate limits, headers     └─ /metrics (internal only) ◄── Prometheus ──► Alertmanager
             └► grafana. / prometheus. / alertmanager. / traefik.  (admin UIs)           └► Grafana
```

## Quick start (local trial)

```bash
make init          # writes .env + secrets/htpasswd with random secrets; prints the admin logins
make up            # docker compose up -d --build
```

With `DOMAIN=localhost` (the default) everything is served over HTTPS with Traefik's self-signed
certificate (use `curl -k`, or accept the browser warning), and a Mailpit container catches outgoing
mail at `https://mailpit.localhost` so you can read verification and reset emails.

Local mode publishes HTTPS on **port 8531** (HTTP on 8530, which only redirects). Change it with
`HTTPS_PORT=9000 ./scripts/init-env.sh`, or edit `HTTP_PORT`, `HTTPS_PORT` and `PUBLIC_PORT_SUFFIX` in `.env`.

* API docs (Swagger): `https://localhost:8531/docs`
* Health: `https://localhost:8531/health`
* Grafana: `https://grafana.localhost:8531` · Prometheus: `https://prometheus.localhost:8531` · Alertmanager: `https://alertmanager.localhost:8531` · Traefik: `https://traefik.localhost:8531` · Mailpit: `https://mailpit.localhost:8531`

Try it:

```bash
curl -ks https://localhost:8531/api/v1/auth/login -d 'username=<ADMIN_EMAIL>&password=<ADMIN_PASSWORD>'
curl -ks https://localhost:8531/api/v1/links -H "Authorization: Bearer <token>" \
     -H 'content-type: application/json' -d '{"url":"https://example.org/a/long/url","alias":"demo"}'
curl -ksI https://localhost:8531/demo      # 302 -> https://example.org/a/long/url
```

## Deploying for real

1. Point DNS `A`/`AAAA` records for `DOMAIN` **and** `traefik.`, `grafana.`, `prometheus.`,
   `alertmanager.` subdomains (or a wildcard) at the server. Open ports 80 and 443.
2. `DOMAIN=sho.example.com ACME_EMAIL=you@example.com ADMIN_EMAIL=you@example.com scripts/init-env.sh`
3. Edit `.env`: set the SMTP block (verification and reset emails need it), `ALERT_EMAIL_TO` and/or
   `ALERT_WEBHOOK_URL`, and ideally `ADMIN_IP_ALLOWLIST` for the admin UIs. Try
   `ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory` first to avoid rate limits.
4. `docker compose up -d --build` (the one-shot `migrate` service runs Alembic before the API starts).

Certificates are issued automatically (HTTP-01) and renewed by Traefik; the `TLSCertificateExpiringSoon`
alert fires if renewal breaks. Scale the API with `API_REPLICAS` (Prometheus scrapes every replica).

## API overview

| | |
|---|---|
| `POST /api/v1/auth/register` | create account (always answers 202, so addresses can't be enumerated) |
| `POST /api/v1/auth/verify-email` · `resend-verification` | confirm email |
| `POST /api/v1/auth/login` | OAuth2 password form (`username` = email) → JWT. Works with Swagger's *Authorize* button |
| `POST /api/v1/auth/forgot-password` · `reset-password` | emailed single-use token; reset revokes all existing JWTs |
| `POST /api/v1/auth/change-password`, `GET /api/v1/users/me` | logged-in user |
| `POST/GET/DELETE /api/v1/links` | create (optional `alias`), list *my* links, delete |
| `GET /{code}` | public 302 redirect |
| `GET /api/v1/admin/users`, `DELETE /api/v1/admin/users/{id}` | admin only (also `/admin/links`) |
| `GET /health` · `/health/live` | readiness (PostgreSQL + Redis, 503 if either is down) · liveness |

Emails link to `FRONTEND_URL` (`/verify-email?token=…`, `/reset-password?token=…`) and also contain the raw
token, so the flows work from Swagger without any front end.

## How it works

* **Auth**: bcrypt (12 rounds, run in a thread pool) and HS256 JWTs (30 min). Passwords are 10–72 bytes (bcrypt's
  limit). Login always runs bcrypt, even for unknown users, to equalise timing. Five failures per
  email+IP in 15 minutes lock that pair out (HTTP 429), on top of Traefik's per-IP limits.
* **First start**: if the users table is empty, `ADMIN_EMAIL` / `ADMIN_PASSWORD` become a verified admin
  (guarded by a Postgres advisory lock, so replicas starting together don't race). Change the password afterwards.
* **Redirects**: Redis cache (including short-lived negative caching for unknown codes), falling back to PostgreSQL
  if Redis is unavailable.
* **Click counting (exact)**: each redirect appends one entry to a Redis stream (a single O(1) command). Every few
  seconds one replica (Redis lock) adds the new entries to `links.click_count` **in one PostgreSQL transaction that also
  advances a watermark** (`click_flush_state`), and only then trims the stream. A crash before the commit means
  nothing was counted and the same entries are read again; a crash after it means the watermark already covers them.
  So each click is counted **exactly once**, however often the flusher is interrupted (covered by tests that simulate
  both crashes and race four flushers against each other). If Redis is down when someone clicks, the click is written
  straight to PostgreSQL instead, so it still counts. Counts shown by the API lag by up to
  `CLICK_FLUSH_INTERVAL_SECONDS` (5 s). A click can only be lost if Redis *and* PostgreSQL are both down at that
  moment (alert `ClicksLost`) or if the Redis host itself loses data (AOF syncs once per second, so a hard crash can lose
  up to ~1 s of not-yet-flushed clicks).
* **Validation**: http(s) URLs only, ≤ 2048 chars, no embedded credentials, no links back to this service;
  aliases are 3–32 chars of `[A-Za-z0-9_-]`, case-sensitive, with route names (`docs`, `health`, `api`…) reserved.
  `max_links_per_user` (1000) caps abuse.

## Security notes

* HTTPS everywhere: port 80 only redirects; TLS ≥ 1.2 with AEAD ciphers; HSTS and other security headers.
* Traefik reaches Docker only through a read-only socket proxy. The API container runs as a non-root user with a read-only
  filesystem, all capabilities dropped and `no-new-privileges`. PostgreSQL, Redis and the exporters sit on an
  internal-only network.
* **`/metrics` is not routed publicly** (the router rule excludes it) and the app also returns 404 to any request that
  carries `X-Forwarded-*`. Prometheus scrapes the containers directly.
* Prometheus, Alertmanager, Traefik dashboard: basic auth (`secrets/htpasswd`) plus optional IP allow-list.
  Grafana: its own login (no sign-up, no anonymous) plus the allow-list. Swagger can be turned off with `DOCS_ENABLED=false`.
* Rate limits (labels in `docker-compose.yml`, tune with `RL_*` in `.env`): 100 req/s per client IP overall, 20/min on
  `/api/v1/auth/*`. Behind a CDN or load balancer see the next section, otherwise every visitor looks like one IP.
* Logs are JSON with a request id (`X-Request-ID`), never contain passwords or tokens, and tokens are only
  printed by the `log` mail backend outside production.
* Secrets are in `.env` (mode 600, git-ignored). For stricter setups use Docker/Swarm secrets or an external secret store.

## Behind a CDN or load balancer

Leave the defaults when visitors connect straight to this server: Traefik then ignores any `X-Forwarded-For` the client
sends and rate limits / allow-lists the real TCP peer. If a CDN (Cloudflare, CloudFront...) or load balancer sits in
front, three settings in `.env` make the real visitor IP visible. Set them together:

| Setting | Meaning |
|---|---|
| `TRUSTED_PROXY_IPS` | CIDRs of the proxies in front of Traefik (use the provider's published ranges). Traefik only believes `X-Forwarded-For` from these. |
| `PROXY_DEPTH` | Which address of `X-Forwarded-For`, counted **from the right**, is the visitor. Traefik appends the connecting proxy's address, so one CDN hop means `2`. Used by the rate limits and `ADMIN_IP_ALLOWLIST`. |
| `API_FORWARDED_ALLOW_IPS` | Proxies the API trusts when logging the client IP and locking out failed logins. Keep the Docker ranges and add the same CIDRs. **Never `*`**: uvicorn would then trust the left-most, client-controlled entry (tested in `tests/test_client_ip.py`). |

Also firewall ports 80/443 to the CDN's ranges so nobody can bypass it. After changing the values, check the Traefik
access log (`ClientHost`) shows real visitor addresses, and that repeated bad logins from one address, not from the CDN's
address, trigger the 429. The proxy-header behaviour of the API is covered by automated tests; Traefik's side is validated
only as configuration, so do this check once in your environment.

## Backups and restore

PostgreSQL holds everything that matters (Redis only holds the link cache and the few seconds of clicks not yet flushed).

```bash
make backup                                              # ./scripts/backup.sh
make restore-check FILE=backups/shortener-<time>.dump    # SAFE drill: restore into a scratch DB, print counts
make restore       FILE=backups/shortener-<time>.dump    # DESTRUCTIVE: replace the live database
```

`backup.sh` writes a custom-format `pg_dump`, **verifies it can be listed**, records a SHA-256, keeps the last
`BACKUP_KEEP_DAYS` days locally, and (when `BACKUP_RCLONE_REMOTE` is set) copies it off the server with
[rclone](https://rclone.org) and prunes old remote copies. `restore.sh` checks the SHA-256 first, asks you to type
`RESTORE`, stops the API, rebuilds the database, runs any newer migrations and restarts the API.

Set up off-site storage once (an rclone *crypt* remote on top of S3/Backblaze/SFTP keeps dumps encrypted; they contain
password hashes), then schedule it:

```bash
rclone config                      # create e.g. "offsite" (and a crypt remote layered on it)
# in .env:  BACKUP_RCLONE_REMOTE=offsite-crypt:shortener
crontab -e                         # nightly at 02:30; failures print to cron's mail / log
30 2 * * *  cd /opt/url-shortener && ./scripts/backup.sh >> backups/backup.log 2>&1
```

**Run `make restore-check` on a recent dump at least monthly**, and after fetching an off-site copy
(`rclone copy offsite-crypt:shortener/<file> backups/`) to prove the whole chain works. These scripts are covered by
automated tests that take a real dump, corrupt one, prune old ones and restore into a live database.

## Operations

```bash
make logs       # api + traefik logs
make test       # pytest against a real PostgreSQL + Redis (see tests/conftest.py)
```

Alerts (`monitoring/prometheus/alerts.yml`): target/Postgres/Redis down, 5xx rate, p95 latency, login-failure
spikes, click-flush failures, mail failures, Postgres connections, Redis memory, TLS expiry. The Grafana
dashboard "URL Shortener" is provisioned automatically.

Not included (by design, to keep scope tight): refresh tokens (access tokens simply expire after 30 minutes),
link expiry, per-click analytics, and PostgreSQL point-in-time recovery (backups are nightly dumps).

Image tags in `docker-compose.yml` are pinned; review and bump them (Grafana, exporters, socket proxy, Mailpit)
before a production rollout.
