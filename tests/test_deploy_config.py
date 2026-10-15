"""Static checks on the deployment files (needs the `docker` CLI for `docker compose config`; no daemon)."""

import json
import os
import re
import shutil
import subprocess

import pytest
import yaml
from conftest import ROOT

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI not available")

REQUIRED = {
    "DOMAIN": "sho.example.com",
    "POSTGRES_PASSWORD": "pw",
    "REDIS_PASSWORD": "pw",
    "SECRET_KEY": "k" * 40,
    "GRAFANA_ADMIN_PASSWORD": "pw",
}


def compose(extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
    env.update(REQUIRED)
    env.update(extra_env or {})
    out = subprocess.run(
        ["docker", "compose", "--profile", "dev", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def labels(cfg):
    for name, svc in cfg["services"].items():
        for k, v in (svc.get("labels") or {}).items():
            yield name, k, str(v)


def test_every_referenced_traefik_middleware_exists():
    cfg = compose()
    defined_docker = {m.group(1) for _, k, _ in labels(cfg) if (m := re.match(r"traefik\.http\.middlewares\.([^.]+)\.", k))}
    file_cfg = yaml.safe_load((ROOT / "traefik/dynamic/dynamic.yml").read_text())
    defined_file = set(file_cfg["http"]["middlewares"])
    for svc, k, v in labels(cfg):
        if re.fullmatch(r"traefik\.http\.routers\.[^.]+\.middlewares", k):
            for ref in v.split(","):
                name, _, provider = ref.strip().partition("@")
                assert name in (defined_file if provider == "file" else defined_docker), f"{svc}: {ref} is not defined"


def test_only_traefik_is_published_and_datastores_are_internal():
    cfg = compose()
    published = {n for n, s in cfg["services"].items() if s.get("ports")}
    assert published == {"traefik"}
    assert cfg["networks"]["backend"]["internal"] is True
    for name in ("postgres", "redis", "postgres-exporter", "redis-exporter"):
        assert set(cfg["services"][name]["networks"]) == {"backend"}, name


def test_metrics_are_not_routed_publicly():
    cfg = compose()
    rules = {k: v for _, k, v in labels(cfg) if k.endswith(".rule")}
    assert "!PathPrefix(`/metrics`)" in rules["traefik.http.routers.api.rule"]
    assert not any("/metrics" in v and "!" not in v for v in rules.values())


def test_trusted_proxy_settings_default_to_trusting_nobody():
    cfg = compose()
    cmd = cfg["services"]["traefik"]["command"]
    assert "--entrypoints.websecure.forwardedheaders.trustedips=127.0.0.1/32" in cmd
    assert "--entrypoints.web.forwardedheaders.trustedips=127.0.0.1/32" in cmd
    env = cfg["services"]["api"]["environment"]
    assert "*" not in env["FORWARDED_ALLOW_IPS"]
    assert "*" not in re.search(r"FORWARDED_ALLOW_IPS=(\S+)", (ROOT / "Dockerfile").read_text()).group(1)
    lbl = dict((k, v) for n, k, v in labels(cfg) if n == "api")
    assert lbl["traefik.http.middlewares.rl-global.ratelimit.sourcecriterion.ipstrategy.depth"] == "0"


def test_cdn_settings_are_wired_through():
    cfg = compose({
        "TRUSTED_PROXY_IPS": "203.0.113.0/24,198.51.100.0/24",
        "PROXY_DEPTH": "2",
        "API_FORWARDED_ALLOW_IPS": "172.16.0.0/12,203.0.113.0/24",
        "RL_AUTH_PER_MINUTE": "5",
        "HTTPS_PORT": "8531",
    })
    cmd = cfg["services"]["traefik"]["command"]
    assert "--entrypoints.websecure.forwardedheaders.trustedips=203.0.113.0/24,198.51.100.0/24" in cmd
    assert "--entrypoints.web.http.redirections.entrypoint.to=:8531" in cmd
    lbl = {k: v for _, k, v in labels(cfg)}
    for mw in ("rl-global", "rl-auth"):
        assert lbl[f"traefik.http.middlewares.{mw}.ratelimit.sourcecriterion.ipstrategy.depth"] == "2"
    assert lbl["traefik.http.middlewares.admin-ips.ipallowlist.ipstrategy.depth"] == "2"
    assert lbl["traefik.http.middlewares.rl-auth.ratelimit.average"] == "5"
    assert cfg["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"] == "172.16.0.0/12,203.0.113.0/24"
