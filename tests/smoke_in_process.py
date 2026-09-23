"""
smoke_in_process.py — Confirm the importable API (start_server / stop_server)
spins the mock up in-process and accepts a real request.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = _ilu.spec_from_file_location(
    "mock_activation_server",
    os.path.join(ROOT, "tests", "fixtures", "mock_activation_server.py"),
)
mock = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(mock)

# N1: configure the in-process mock's allowlist so the consent
# endpoint accepts the version we send below. Empty (the default) ->
# reject every consent. The constructor option is the cleanest path
# here (no env var to clean up between this script and the wider
# test suite).
server, base_url = mock.start_server(
    port=0, ttl_seconds=1800, app_secret="importable-test",
    accepted_consent_versions=["v1"],
)
try:
    # No announcement line when imported — the (server, base_url) return
    # value is the contract. Confirm that.
    assert base_url.startswith("http://127.0.0.1:"), base_url

    # Hit consent with a valid key.
    key = "sk_sensie_" + "f" * 64
    req = urllib.request.Request(
        f"{base_url}/sdk-api/trial/consent",
        data=json.dumps({
            "consent_version": "v1", "scope": "live-gesture", "accepted": True,
        }).encode("utf-8"),
        headers={"x-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        assert resp.status == 201, resp.status
        body = json.loads(resp.read().decode("utf-8"))
        assert body["status"] == "success", body
        assert "id" in body["data"]["consent"], body

    print("[PASS] start_server in-process: consent returned 201")
    print(f"  base_url = {base_url}")
finally:
    mock.stop_server(server)

# After stop_server, the socket should be closed. Trying to reconnect must
# fail (not hang).
import socket
host, port = base_url.rsplit(":", 1)[1:], None
port = int(base_url.rsplit(":", 1)[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        print("[PASS] stop_server: socket is closed")
    else:
        print("[FAIL] stop_server: socket still open after stop")
        sys.exit(1)
