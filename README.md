# URL Shortener

This is a FastAPI URL-shortening service. Users register and authenticate, create links, and request public redirects; admins can manage users and links. Redirects resolve through a Redis cache with PostgreSQL fallback, while clicks are buffered and flushed exactly once. The repository also documents Traefik ingress and a Prometheus/Grafana/Alertmanager stack. Some infrastructure and route implementations were not sampled.

[![Architecture diagram of shubhankarraj9/url-shortener-project](https://gitdiagram.com/shubhankarraj9/url-shortener-project/diagram.png)](https://gitdiagram.com/shubhankarraj9/url-shortener-project?utm_source=readme&utm_medium=picture)

**Choose where to start:** [Local setup](#local-setup) · [Production](#production) · [API](#api) · [Operations](#operations)

## Local setup

<details open>
<summary><strong>Windows, Git Bash, or Linux</strong></summary>

### 1. Generate app secrets

Run once from the project directory:

```bash
bash ./scripts/init-env.sh
```

Keep the generated admin credentials. The script creates `.env` and `secrets/htpasswd`; it won't overwrite an existing `.env`.

### 

### 2. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

Open **[https://localhost:8531/docs](https://localhost:8531/docs)**. The API docs and health check are available at `/docs` and `/health`. Local email is captured by Mailpit at [https://mailpit.localhost:8531](https://mailpit.localhost:8531).

</details>

<details>
<summary><strong>What runs in the stack?</strong></summary>


| Service                             | Purpose                                       |
| ------------------------------------- | ----------------------------------------------- |
| Traefik                             | HTTPS, routing, security headers, rate limits |
| FastAPI                             | Authentication, link management, redirects    |
| PostgreSQL                          | Users, links, authentication tokens           |
| Redis                               | Link cache, click buffer, throttling          |
| Prometheus + Grafana + Alertmanager | Metrics, dashboards, alerts                   |
| Mailpit                             | Captures local development email              |

Traefik is the public ingress. PostgreSQL and Redis are also published to loopback-only host ports for local development.

</details>

<details>
## API

<summary><strong>Endpoints at a glance</strong></summary>


| Endpoint                         | What it does                       |
| ---------------------------------- | ------------------------------------ |
| `POST /api/v1/auth/register`     | Register an account                |
| `POST /api/v1/auth/verify-email` | Verify an email address            |
| `POST /api/v1/auth/login`        | Sign in and receive a JWT          |
| `POST /api/v1/links`             | Create a short link                |
| `GET /api/v1/links`              | List your links                    |
| `DELETE /api/v1/links/{id}`      | Delete one of your links           |
| `GET /{code}`                    | Redirect to the original URL       |
| `GET /health`                    | Check database and Redis readiness |

For interactive requests and schemas, open `/docs` while the app is running. Verification and password-reset emails are shown in Mailpit during local development.

</details>

<details>
## Production

<summary><strong>Deployment steps</strong></summary>

1. Use a domain you control and point its DNS records (including the admin subdomains) at your server.
2. Allow inbound ports 80 and 443.
3. Set `DOMAIN`, `ACME_EMAIL`, and `ADMIN_EMAIL` when generating `.env`:

   ```bash
   DOMAIN=sho.example.com ACME_EMAIL=ops@example.com ADMIN_EMAIL=admin@example.com bash ./scripts/init-env.sh
   ```
4. Configure working SMTP settings and alert destinations in `.env`. Consider setting `ADMIN_IP_ALLOWLIST`.
5. Start the services:

   ```bash
   docker compose up -d --build
   ```

Traefik obtains and renews public certificates with Let's Encrypt. **Do not use the local mkcert certificate for a public deployment.** Test with Let's Encrypt staging first by setting `ACME_CA_SERVER` to `https://acme-staging-v02.api.letsencrypt.org/directory`.

</details>

<details>
## Operations

<summary><strong>Useful commands</strong></summary>

```bash
docker compose ps                    # service status
docker compose logs -f --tail=100 api traefik
docker compose up -d --build          # build and start
docker compose down                   # stop services; keep named volumes
make backup                           # create and verify a database backup
make restore-check FILE=backups/FILE  # test a backup without replacing live data
```

`make restore` replaces the live database. Read `scripts/restore.sh` and confirm the backup before using it.

For tests, see `tests/conftest.py` for the required PostgreSQL and Redis test URLs. Never point tests at production data.

</details>

<details>
<summary><strong>Security and configuration</strong></summary>

- `.env` and `secrets/` are git-ignored. Never commit passwords, certificates, or private keys.
- `.env.example` contains configuration placeholders; review it before copying values.
- Admin monitoring pages use authentication. Configure `ADMIN_IP_ALLOWLIST` to restrict access further.
- Swagger can be disabled with `DOCS_ENABLED=false`.
- If using a CDN or load balancer, configure `TRUSTED_PROXY_IPS`, `PROXY_DEPTH`, and `API_FORWARDED_ALLOW_IPS` together. Do not set `API_FORWARDED_ALLOW_IPS=*`.
- Review `README.md`'s deployment configuration and the comments in `.env.example` before exposing the service publicly.

</details>
