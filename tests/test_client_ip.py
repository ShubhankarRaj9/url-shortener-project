"""The client IP the API logs / uses for login lockout must not be spoofable through X-Forwarded-For."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

from conftest import ROOT


def client_ip_seen(allow_ips: str, xff: str) -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "FORWARDED_ALLOW_IPS": allow_ips, "LOG_JSON": "true", "LOG_LEVEL": "INFO", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(port),
         "--no-access-log", "--proxy-headers"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/no-such-code", headers={"X-Forwarded-For": xff})
        try:
            urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError:
            pass
    finally:
        proc.terminate()
        out = proc.communicate(timeout=15)[0]
    lines = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]
    return next(ln["client_ip"] for ln in lines if ln.get("event") == "http.request" and ln.get("path") == "/no-such-code")


def test_trusted_proxy_chain_yields_the_real_client_not_the_spoofed_entry():
    # attacker sends "9.9.9.9"; our proxy (127.0.0.1) appended the address it really saw
    assert client_ip_seen("127.0.0.1", "9.9.9.9, 203.0.113.7") == "203.0.113.7"
    assert client_ip_seen("127.0.0.0/8", "9.9.9.9, 203.0.113.7") == "203.0.113.7"


def test_forwarded_headers_from_untrusted_peers_are_ignored():
    assert client_ip_seen("10.0.0.0/8,172.16.0.0/12", "9.9.9.9") == "127.0.0.1"
