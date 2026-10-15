from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "Requests currently being handled")

REDIRECTS = Counter("shortener_redirects_total", "Short link lookups", ["result"])  # hit|miss|not_found
LINKS_CREATED = Counter("shortener_links_created_total", "Links created", ["kind"])  # custom|generated
USERS_REGISTERED = Counter("shortener_users_registered_total", "Accounts created")
LOGINS = Counter("shortener_logins_total", "Login attempts", ["result"])  # success|failure|locked|unverified
CLICKS_FLUSHED = Counter("shortener_clicks_flushed_total", "Clicks written from Redis to Postgres")
CLICK_FLUSH_FAILURES = Counter("shortener_click_flush_failures_total", "Failed click flush runs")
EMAILS = Counter("shortener_emails_total", "Outbound e-mails", ["result"])  # sent|failed
CLICKS_PENDING = Gauge("shortener_clicks_pending", "Clicks waiting in Redis to be written to PostgreSQL")
CLICKS_FALLBACK = Counter("shortener_clicks_fallback_total", "Clicks written straight to PostgreSQL because Redis was unavailable")
CLICKS_LOST = Counter("shortener_clicks_lost_total", "Clicks that could be recorded in neither Redis nor PostgreSQL")
