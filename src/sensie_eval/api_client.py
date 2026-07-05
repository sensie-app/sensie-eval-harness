"""
api_client.py — Minimal client for the Sensie live API (stdlib only).

Talks to the Sensie SDK API (Supabase Edge Functions). Uses urllib from
the standard library on purpose: the published package depends on
numpy/scipy only.

Endpoints (base URL = SENSIE_API_URL, default production):
    POST {base}/sdk-api/session                       — create a session
    POST {base}/sdk-api/session/{id}/sensie           — post one read (metered)
    GET  {base}/sdk-api/session/{id}/sensie           — list reads in a session

Every request carries the customer's trial key in an `x-api-key` header
(format: sk_sensie_<64 hex>).

IP / PRIVACY GUARDRAIL: This client never sends raw accelerometer or
gyroscope arrays. Trial-tier requests carry only scalar summary values
(whips, flowing, agreement) — the trial partner rejects raw motion.
"""

import json
import urllib.error
import urllib.request
from typing import Dict, List, Optional

DEFAULT_API_URL = "https://pqimowhxuxfcqqadlkdn.supabase.co/functions/v1"

# Contract-valid values for the sensie payload
VALID_FLOWING = (1, -1)
VALID_AGREEMENT = (-1, 1, 2)


class SensieApiError(Exception):
    """Base class for API errors."""

    def __init__(self, status: int, body: Dict, headers: Optional[Dict] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        super().__init__(f"HTTP {status}: {body}")


class SensieAuthError(SensieApiError):
    """401 — invalid or missing API key."""


class SensieQuotaError(SensieApiError):
    """429 — trial quota exhausted.

    Body schema (committed): {"error": "quota_exceeded", "used": <int>,
    "limit": <int>, "window_reset_at": "<ISO8601 UTC or null>"}
    plus a Retry-After header (seconds).
    """

    @property
    def used(self) -> Optional[int]:
        return self.body.get("used")

    @property
    def limit(self) -> Optional[int]:
        return self.body.get("limit")

    @property
    def window_reset_at(self) -> Optional[str]:
        return self.body.get("window_reset_at")

    @property
    def retry_after(self) -> Optional[str]:
        return self.headers.get("Retry-After")


class SensieApiClient:
    """Thin JSON-over-HTTP client for the Sensie SDK API."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_API_URL,
                 timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str,
                 payload: Optional[Dict] = None) -> Dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"x-api-key": self.api_key}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                body = {"raw": raw}
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            if exc.code == 401:
                raise SensieAuthError(exc.code, body, resp_headers) from None
            if exc.code == 429:
                raise SensieQuotaError(exc.code, body, resp_headers) from None
            raise SensieApiError(exc.code, body, resp_headers) from None

    def create_session(self, user_id: str, sdk_version: str) -> Dict:
        """POST /sdk-api/session — returns the session dict (with 'id')."""
        response = self._request("POST", "/sdk-api/session", {
            "userId": user_id,
            "type": "evaluation",
            "sdkVersion": sdk_version,
        })
        return response["data"]["session"]

    def post_sensie(self, session_id, whips: float,
                    flowing: int, agreement: int) -> Dict:
        """POST /sdk-api/session/{id}/sensie — the metered read.

        Sends scalar summary values only — never raw IMU arrays.
        """
        if flowing not in VALID_FLOWING:
            raise ValueError(f"flowing must be one of {VALID_FLOWING}")
        if agreement not in VALID_AGREEMENT:
            raise ValueError(f"agreement must be one of {VALID_AGREEMENT}")
        response = self._request(
            "POST", f"/sdk-api/session/{session_id}/sensie", {
                "whips": whips,
                "flowing": flowing,
                "agreement": agreement,
            }
        )
        return response["data"]["sensie"]

    def list_sensies(self, session_id) -> List[Dict]:
        """GET /sdk-api/session/{id}/sensie — list reads in the session."""
        response = self._request(
            "GET", f"/sdk-api/session/{session_id}/sensie"
        )
        return response["data"]["sensies"]
