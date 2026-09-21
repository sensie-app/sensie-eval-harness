"""
mock_activation_server.py — In-memory mock for the Sensie "live-gesture" SDK API.

Mirrors SHARED CONTRACT v1 (frozen in
https://github.com/sensie-app/sensie-eval-harness/issues/11,
the "Scoped build — fanned out to 4 parallel builders" comment) plus the
additive CONTRACT CLARIFICATIONS C1..C10 from briefs/COMMON.md:

  C1  Envelope: success {"status":"success","data":{...}}; error
      {"status":"error","error":"<code>","message":"<human readable>"}.
  C2  401 {"error":"unauthorized"} for bad x-api-key OR bad x-app-secret.
  C3  GET issued to a DIFFERENT key -> 404 code_not_found (never reveal
      existence). Unknown -> 404 code_not_found.
  C4  8 chars from ABCDEFGHJKMNPQRSTUVWXYZ23456789, CSPRNG; case-insensitive
      (server uppercases); TTL = 30 min default (override --ttl-seconds).
  C5  State machine pending -> claimed -> completed; non-completed flips to
      expired on read once now > expires_at. GET of lapsed code returns 200
      with status:"expired" (not HTTP error). sensie is null unless
      completed.
  C6  claim: unknown -> 404 code_not_found; expired -> 410 code_expired;
      already claimed/completed -> 410 code_already_claimed. Atomic.
  C7  complete (PATCH): pending -> 409 code_not_claimed; completed -> 410
      code_already_claimed; expired -> 410 code_expired; unknown -> 404.
      whips int >= 0; flowing in {1,-1}; agreement in {-1,1,2};
      violation -> 400 invalid_payload. Single-use, immutable.
  C8  consent: accepted literal true and scope == "live-gesture" else 400
      invalid_payload. activation-code with consent_id not belonging to
      the calling key, revoked, or unknown -> 403 consent_required.
  C9  >3 outstanding (non-expired, non-completed) codes on the key -> 429
      {"error":"rate_limited"} + Retry-After. (Existing 429
      error=="quota_exceeded" is a separate path; clients switch on the
      error field, not the status.)
  C10 install_url = https://go.somacheck.com/install.

And the additive DELTAS D1..D8 from briefs/CONTRACT-DELTA.md (Lane 1, the
real backend). D1/D2 are schema-only with no wire change; D3..D7 affect
this mock's behaviour:

  D3  consent_id missing / empty / non-string -> 400 invalid_payload;
      present but not a well-formed UUID -> 403 consent_required.
  D4  claim / complete 200 bodies carry an activation object that mirrors
      the GET shape: claim -> {status:"claimed", expires_at}; complete ->
      {status:"completed", completed_at}. ISO-8601 UTC.
  D5  Bounded free-text: consent_version 1-64 chars, device_id 1-200 chars;
      control characters (0x00-0x1F, 0x7F) forbidden. Violation -> 400.
  D6  A {code} path segment that is not exactly 8 chars of the Crockford
      alphabet (case-insensitive) -> 404 code_not_found on claim, complete
      AND GET. A code that cannot exist is a code that does not exist.
  D7  On complete, a value-domain violation returns 400 invalid_payload
      whether or not the code exists: payload is validated BEFORE the code
      is looked up, so a bad body never reveals whether a code is real.
  D8  Rollback SQL keeps the trial_consents and activation_codes tables.

Endpoints (all under /sdk-api/...):
    POST  /sdk-api/trial/consent              auth: x-api-key (trial key)
    POST  /sdk-api/trial/activation-code      auth: x-api-key
    POST  /sdk-api/activation/{code}/claim    auth: x-app-secret
    PATCH /sdk-api/activation/{code}/complete auth: x-app-secret
    GET   /sdk-api/activation/{code}          auth: x-api-key (must match
                                                   issuing key)
Plus one MOCK-ONLY test helper (clearly marked, never present in real
backend):
    POST  /__mock/advance?seconds=N           advance the injectable clock
                                              (used by expired-path tests)

This module is also importable: pytest can call start_server() / stop_server()
to spin the mock up in-process; the CLI form is for live integration runs.

Stdlib only (http.server + threading + secrets). No network. No persistence.
Never logs API keys, app secrets, codes, or consent ids.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "v1"
INSTALL_URL = "https://go.somacheck.com/install"
CONSENT_SCOPE = "live-gesture"

# C4 / D6: Crockford-style alphabet (no I/L/O/1/0). Case-insensitive on input;
# server stores uppercase. D6 tightens the path-segment matcher to exactly
# 8 chars from this alphabet; anything else is 404 code_not_found.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_RE = re.compile(f"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")

# D5: bounded free-text fields.
CONSENT_VERSION_MIN = 1
CONSENT_VERSION_MAX = 64
DEVICE_ID_MIN = 1
DEVICE_ID_MAX = 200
# ASCII control characters (0x00-0x1F, 0x7F) are forbidden in free-text
# fields per D5.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# D3: well-formed UUID match — anything that is not a UUID format is
# either a missing/empty/non-string (handled separately as 400) or a
# malformed UUID (handled here as 403 consent_required, since a string
# that cannot be an identifier is functionally unknown).
UUID_FORMAT_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# C2: trial key shape. The mock REJECTS anything else with 401.
TRIAL_KEY_PREFIX = "sk_sensie_"
TRIAL_KEY_RE = re.compile(r"^sk_sensie_[0-9a-fA-F]{64}$")

# C7: value domains.
VALID_FLOWING = (1, -1)
VALID_AGREEMENT = (-1, 1, 2)

# C9: max outstanding non-expired non-completed codes per key.
MAX_OUTSTANDING_CODES = 3
# Retry-After when the limit is hit (seconds). Real backend will set this
# to the earliest outstanding code's TTL; mock uses a static 60s.
RATE_LIMITED_RETRY_AFTER = "60"

# Default knobs; overridable via CLI flags or start_server() kwargs.
DEFAULT_PORT = 8787
DEFAULT_TTL_SECONDS = 1800
DEFAULT_APP_SECRET = "mock-app-secret"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class _ApiError(Exception):
    """Raised inside handlers to short-circuit with a structured error."""

    def __init__(self, status: int, code: str, message: str,
                 headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        super().__init__(f"{status} {code}: {message}")


# ---------------------------------------------------------------------------
# Store (in-memory, single-process; guarded by a lock)
# ---------------------------------------------------------------------------


class _Store:
    """Single-process, thread-safe in-memory store.

    Codes are keyed by their 8-char string. Each code carries the issuing
    trial-key id so a GET with a different key returns 404 (C3). The clock
    is injectable so tests can advance time without sleeping (C5).
    """

    def __init__(self, ttl_seconds: int, clock=None):
        self._ttl = ttl_seconds
        # Wall-clock epoch anchor captured at startup. `now()` returns
        # real Unix epoch seconds (so `_iso()` formats real UTC, e.g.
        # 2026-09-21T...Z, not 1970). `time.monotonic()` is still the
        # underlying tick source so the wall clock cannot run backwards
        # if the system clock jumps.
        self._monotonic_origin = time.monotonic()
        self._wall_origin = time.time()
        # MOCK-ONLY test hook: extra seconds added on top of the wall
        # anchor. `advance()` shifts this; everything else reads through
        # `now()`. Active even if the caller passed a custom `clock` —
        # we only use the injected clock as the monotonic tick source.
        self._mock_offset = 0.0
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        # consents: id -> dict
        self._consents: Dict[str, Dict[str, Any]] = {}
        # codes: code -> dict (status, key_id, consent_id, expires_at,
        # created_at, claimed_at, completed_at, device_id, sensie).
        self._codes: Dict[str, Dict[str, Any]] = {}

    # -- clock ------------------------------------------------------------

    def now(self) -> float:
        """Real Unix epoch seconds, with any test-hook advance applied."""
        return (self._wall_origin
                + (self._clock() - self._monotonic_origin)
                + self._mock_offset)

    def advance(self, seconds: int) -> None:
        """MOCK-ONLY test helper. Advances the wall clock by `seconds`.

        Expiry comparisons and `_iso()` formatting both go through
        `now()`, so shifting the wall clock by N seconds immediately
        makes any code whose `expires_at < now()` appear lapsed and
        formats a new `expires_at` accordingly on the next read.
        """
        self._mock_offset += seconds

    # -- consents ---------------------------------------------------------

    def create_consent(self, key_id: str, consent_version: str,
                       scope: str, accepted: bool) -> Dict[str, Any]:
        if not isinstance(accepted, bool) or accepted is not True:
            raise _ApiError(400, "invalid_payload",
                            "accepted must be literal true")
        if scope != CONSENT_SCOPE:
            raise _ApiError(400, "invalid_payload",
                            f"scope must be \"{CONSENT_SCOPE}\"")
        # D5: consent_version is 1-64 chars, no control characters.
        if not isinstance(consent_version, str) or not consent_version:
            raise _ApiError(400, "invalid_payload",
                            "consent_version is required")
        if (len(consent_version) < CONSENT_VERSION_MIN
                or len(consent_version) > CONSENT_VERSION_MAX):
            raise _ApiError(400, "invalid_payload",
                            f"consent_version must be "
                            f"{CONSENT_VERSION_MIN}-{CONSENT_VERSION_MAX} "
                            "characters")
        if _CONTROL_CHAR_RE.search(consent_version):
            raise _ApiError(400, "invalid_payload",
                            "consent_version must not contain control "
                            "characters")
        with self._lock:
            cid = str(uuid.uuid4())
            self._consents[cid] = {
                "id": cid,
                "key_id": key_id,
                "consent_version": consent_version,
                "scope": scope,
                "accepted": True,
                "consented_at": self._iso(self.now()),
                "revoked_at": None,
            }
            return self._consents[cid]

    def get_consent_for_key(self, key_id: str, consent_id: str) -> Dict[str, Any]:
        with self._lock:
            c = self._consents.get(consent_id)
        if c is None:
            # C8: unknown -> 403 consent_required (per the contract, do not
            # distinguish "doesn't exist" from "wrong key" or "revoked" —
            # all surface as consent_required).
            raise _ApiError(403, "consent_required",
                            "consent id is unknown, revoked, or does "
                            "not belong to this key")
        if c.get("revoked_at") is not None:
            raise _ApiError(403, "consent_required",
                            "consent id is unknown, revoked, or does "
                            "not belong to this key")
        if c.get("key_id") != key_id:
            raise _ApiError(403, "consent_required",
                            "consent id is unknown, revoked, or does "
                            "not belong to this key")
        return c

    # -- codes ------------------------------------------------------------

    def _generate_code(self) -> str:
        # CSPRNG via secrets; retry until unique.
        for _ in range(64):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._codes:
                return code
        raise RuntimeError("could not allocate a unique activation code")

    def _is_outstanding(self, code: Dict[str, Any]) -> bool:
        if code["status"] == "completed":
            return False
        # check-on-read expiry: a lapsed pending/claimed counts as expired.
        if code["status"] != "completed" and self.now() > code["expires_at"]:
            return False
        return True

    def create_code(self, key_id: str, consent_id: str) -> Dict[str, Any]:
        # C8: verify consent belongs to this key (raises 403 consent_required).
        self.get_consent_for_key(key_id, consent_id)
        with self._lock:
            # C9: count outstanding non-expired non-completed codes on this key.
            outstanding = sum(
                1 for c in self._codes.values()
                if c["key_id"] == key_id and self._is_outstanding(c)
            )
            if outstanding >= MAX_OUTSTANDING_CODES:
                raise _ApiError(
                    429, "rate_limited",
                    f"too many outstanding codes (>{MAX_OUTSTANDING_CODES})",
                    headers={"Retry-After": RATE_LIMITED_RETRY_AFTER},
                )
            code = self._generate_code()
            now = self.now()
            rec = {
                "code": code,
                "key_id": key_id,
                "consent_id": consent_id,
                "status": "pending",
                "created_at": now,
                "expires_at": now + self._ttl,
                "claimed_at": None,
                "completed_at": None,
                "device_id": None,
                "sensie": None,
            }
            self._codes[code] = rec
            return {
                "code": code,
                "expires_at": self._iso(rec["expires_at"]),
                "install_url": INSTALL_URL,
            }

    def _lock_code(self, code: str) -> Dict[str, Any]:
        """Return the code record, applying check-on-read expiry.

        Mutates state under the store lock to flip a lapsed non-completed
        record to 'expired'. Caller MUST hold no other locks.
        """
        with self._lock:
            rec = self._codes.get(code)
            if rec is None:
                return None  # type: ignore[return-value]
            if rec["status"] != "completed" and self.now() > rec["expires_at"]:
                rec["status"] = "expired"
            return rec

    def get_code_for_key(self, key_id: str, code: str) -> Dict[str, Any]:
        rec = self._lock_code(code)
        if rec is None:
            raise _ApiError(404, "code_not_found", "unknown activation code")
        # C3: never reveal codes issued to a different key.
        if rec["key_id"] != key_id:
            raise _ApiError(404, "code_not_found", "unknown activation code")
        return rec

    def claim(self, code: str, device_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._codes.get(code)
            if rec is None:
                raise _ApiError(404, "code_not_found", "unknown activation code")
            # C5: check-on-read expiry before claim evaluation.
            if rec["status"] != "completed" and self.now() > rec["expires_at"]:
                rec["status"] = "expired"
            if rec["status"] == "expired":
                raise _ApiError(410, "code_expired", "code has expired")
            # C6: pending -> claimed (atomic). Anything else -> 410.
            if rec["status"] != "pending":
                raise _ApiError(410, "code_already_claimed",
                                f"code is {rec['status']}")
            # D5: device_id is 1-200 chars, no control characters.
            if not isinstance(device_id, str) or not device_id:
                raise _ApiError(400, "invalid_payload",
                                "device_id is required")
            if (len(device_id) < DEVICE_ID_MIN
                    or len(device_id) > DEVICE_ID_MAX):
                raise _ApiError(400, "invalid_payload",
                                f"device_id must be "
                                f"{DEVICE_ID_MIN}-{DEVICE_ID_MAX} "
                                "characters")
            if _CONTROL_CHAR_RE.search(device_id):
                raise _ApiError(400, "invalid_payload",
                                "device_id must not contain control "
                                "characters")
            rec["status"] = "claimed"
            rec["claimed_at"] = self.now()
            rec["device_id"] = device_id
            # D4: success body shape matches the GET shape: an activation
            # object with status + ISO-8601 expires_at.
            return {"activation": {
                "status": "claimed",
                "expires_at": self._iso(rec["expires_at"]),
            }}

    def complete(self, code: str, whips: Any, flowing: Any,
                 agreement: Any) -> Dict[str, Any]:
        # C7 / D7: validate the value domain FIRST so the same shape of
        # error fires whether the code exists or not. Real backend rejects
        # payload BEFORE looking up the code; mirror that ordering so a
        # bad payload is never an oracle for which codes exist.
        if (not isinstance(whips, int) or isinstance(whips, bool)
                or whips < 0):
            raise _ApiError(400, "invalid_payload",
                            "whips must be a non-negative integer")
        if flowing not in VALID_FLOWING:
            raise _ApiError(400, "invalid_payload",
                            f"flowing must be one of {list(VALID_FLOWING)}")
        if agreement not in VALID_AGREEMENT:
            raise _ApiError(400, "invalid_payload",
                            f"agreement must be one of {list(VALID_AGREEMENT)}")
        with self._lock:
            rec = self._codes.get(code)
            if rec is None:
                raise _ApiError(404, "code_not_found", "unknown activation code")
            if rec["status"] != "completed" and self.now() > rec["expires_at"]:
                rec["status"] = "expired"
            # C7: state-aware errors.
            if rec["status"] == "pending":
                raise _ApiError(409, "code_not_claimed",
                                "code must be claimed before it can be completed")
            if rec["status"] == "completed":
                raise _ApiError(410, "code_already_claimed",
                                "code is already completed")
            if rec["status"] == "expired":
                raise _ApiError(410, "code_expired", "code has expired")
            # claimed -> completed, atomic single-use; values immutable.
            rec["status"] = "completed"
            rec["completed_at"] = self.now()
            rec["sensie"] = {
                "whips": whips,
                "flowing": flowing,
                "agreement": agreement,
            }
            # D4: success body shape matches the GET shape: an activation
            # object with status + ISO-8601 completed_at.
            return {"activation": {
                "status": "completed",
                "completed_at": self._iso(rec["completed_at"]),
            }}

    def public_view(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """The exact shape returned by GET /sdk-api/activation/{code}."""
        # C5: sensie is null unless status == completed.
        sensie = rec["sensie"] if rec["status"] == "completed" else None
        return {
            "status": rec["status"],
            "sensie": sensie,
            "expires_at": self._iso(rec["expires_at"]),
        }

    @staticmethod
    def _iso(epoch_seconds: float) -> str:
        # ISO-8601 UTC with 'Z'. time.gmtime returns a struct_time in UTC
        # because we feed it the epoch.
        import datetime as _dt
        return _dt.datetime.fromtimestamp(
            epoch_seconds, tz=_dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _normalize_activation_path(path: str) -> str:
    """C4: server uppercases the activation code on input.

    Returns a copy of `path` with the activation-code segment (the segment
    between "/sdk-api/activation/" and either end-of-string or "/<verb>")
    converted to uppercase. Other paths pass through untouched.
    """
    prefix = "/sdk-api/activation/"
    if not path.startswith(prefix):
        return path
    rest = path[len(prefix):]
    # `rest` is either "<CODE>" or "<CODE>/<verb>".
    if "/" in rest:
        code, _, tail = rest.partition("/")
        return f"{prefix}{code.upper()}/{tail}"
    return f"{prefix}{rest.upper()}"


class _Handler(BaseHTTPRequestHandler):
    """Routes one request, delegates to the shared _Store on `server.store`."""

    # Silence the default stderr access log — we never log keys/secrets,
    # and the per-request lines are noisy. Tests can set LOG=1 to opt in.
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("MOCK_ACTIVATION_LOG") == "1":
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------

    def _store(self) -> _Store:
        return self.server.store  # type: ignore[attr-defined]

    def _key_id(self) -> str:
        """Pull x-api-key, validate format, and return a stable id.

        The real backend hashes the key for lookup; for an in-memory mock
        we just hash to a short hex id so we can scope codes to keys (C3).
        """
        import hashlib
        key = self.headers.get("x-api-key")
        if not key or not TRIAL_KEY_RE.match(key):
            raise _ApiError(401, "unauthorized",
                            "missing or malformed x-api-key")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _require_app_secret(self) -> None:
        secret = self.headers.get("x-app-secret")
        expected = self.server.app_secret  # type: ignore[attr-defined]
        if not secret or secret != expected:
            raise _ApiError(401, "unauthorized",
                            "missing or invalid x-app-secret")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            raise _ApiError(400, "invalid_payload", "request body is not valid JSON")
        if not isinstance(data, dict):
            raise _ApiError(400, "invalid_payload", "request body must be a JSON object")
        return data

    def _write(self, status: int, body: Dict[str, Any],
               extra_headers: Optional[Dict[str, str]] = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _ok(self, data: Dict[str, Any], status: int = 200,
            extra_headers: Optional[Dict[str, str]] = None) -> None:
        # C1 success envelope.
        self._write(status, {"status": "success", "data": data}, extra_headers)

    def _err(self, err: _ApiError) -> None:
        # C1 error envelope; carry any extra headers (e.g. Retry-After).
        self._write(
            err.status,
            {"status": "error", "error": err.code, "message": err.message},
            err.headers,
        )

    # -- HTTP verb dispatch ----------------------------------------------

    def do_POST(self) -> None:
        try:
            self._route_post()
        except _ApiError as exc:
            self._err(exc)

    def do_PATCH(self) -> None:
        try:
            self._route_patch()
        except _ApiError as exc:
            self._err(exc)

    def do_GET(self) -> None:
        try:
            self._route_get()
        except _ApiError as exc:
            self._err(exc)

    # -- routing ----------------------------------------------------------

    def _route_post(self) -> None:
        path = urlparse(self.path).path
        path = _normalize_activation_path(path)
        qs = parse_qs(urlparse(self.path).query)

        # MOCK-ONLY test helper. Clearly marked; the real backend has no
        # such endpoint. Guarded to refuse anything that didn't come from
        # localhost, in case someone accidentally exposes this mock.
        if path == "/__mock/advance":
            if os.environ.get("MOCK_ACTIVATION_ALLOW_TEST_HOOKS") != "1":
                raise _ApiError(404, "code_not_found",
                                "no such endpoint")
            secs = int(qs.get("seconds", ["0"])[0])
            self._store().advance(secs)
            self._ok({"advanced": secs})
            return

        if path == "/sdk-api/trial/consent":
            key_id = self._key_id()
            body = self._read_json()
            c = self._store().create_consent(
                key_id=key_id,
                consent_version=body.get("consent_version", ""),
                scope=body.get("scope", ""),
                accepted=body.get("accepted"),
            )
            self._ok({"consent": {
                "id": c["id"],
                "consented_at": c["consented_at"],
            }}, status=201)
            return

        if path == "/sdk-api/trial/activation-code":
            key_id = self._key_id()
            body = self._read_json()
            # D3: split the failure modes for consent_id.
            #   - missing / empty / not a string -> 400 invalid_payload
            #     (the payload itself is malformed; nothing to look up).
            #   - present but not a well-formed UUID -> 403 consent_required
            #     (a string that cannot be an identifier is functionally
            #     unknown; 400 would disclose format vs existence).
            consent_id = body.get("consent_id")
            if not isinstance(consent_id, str) or not consent_id:
                raise _ApiError(400, "invalid_payload",
                                "consent_id is required")
            if not UUID_FORMAT_RE.match(consent_id):
                raise _ApiError(403, "consent_required",
                                "consent id is unknown, revoked, or does "
                                "not belong to this key")
            activation = self._store().create_code(
                key_id=key_id, consent_id=consent_id,
            )
            self._ok({"activation": activation}, status=201)
            return

        # claim: POST /sdk-api/activation/{code}/claim
        # D6: the {code} segment must be exactly 8 chars of the Crockford
        # alphabet; anything else -> 404 code_not_found.
        m = re.match(r"^/sdk-api/activation/([A-Z0-9]{1,16})/claim$", path)
        if m and not CODE_RE.match(m.group(1)):
            raise _ApiError(404, "code_not_found",
                            "no such endpoint")
        if m:
            self._require_app_secret()
            code = m.group(1).upper()
            body = self._read_json()
            data = self._store().claim(code, body.get("device_id", ""))
            self._ok(data)
            return

        raise _ApiError(404, "code_not_found", "no such endpoint")

    def _route_patch(self) -> None:
        path = urlparse(self.path).path
        path = _normalize_activation_path(path)
        # PATCH /sdk-api/activation/{code}/complete
        # D6: the {code} segment must be exactly 8 chars of the Crockford
        # alphabet; anything else -> 404 code_not_found.
        m = re.match(r"^/sdk-api/activation/([A-Z0-9]{1,16})/complete$", path)
        if m and not CODE_RE.match(m.group(1)):
            raise _ApiError(404, "code_not_found",
                            "no such endpoint")
        if not m:
            raise _ApiError(404, "code_not_found", "no such endpoint")
        self._require_app_secret()
        code = m.group(1).upper()
        body = self._read_json()
        data = self._store().complete(
            code,
            whips=body.get("whips"),
            flowing=body.get("flowing"),
            agreement=body.get("agreement"),
        )
        self._ok(data)

    def _route_get(self) -> None:
        path = urlparse(self.path).path
        path = _normalize_activation_path(path)
        # GET /sdk-api/activation/{code}
        # D6: the {code} segment must be exactly 8 chars of the Crockford
        # alphabet; anything else -> 404 code_not_found.
        m = re.match(r"^/sdk-api/activation/([A-Z0-9]{1,16})$", path)
        if m and not CODE_RE.match(m.group(1)):
            raise _ApiError(404, "code_not_found",
                            "no such endpoint")
        if not m:
            raise _ApiError(404, "code_not_found", "no such endpoint")
        key_id = self._key_id()
        code = m.group(1).upper()
        rec = self._store().get_code_for_key(key_id, code)
        # C5: GET of a lapsed code returns 200 with status:"expired"
        # (it is NOT an HTTP error — that's the whole point of the
        # check-on-read flip).
        self._ok({"activation": self._store().public_view(rec)})


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class _MockServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that carries the shared store and app secret."""

    # Bind to localhost only — never expose this mock to the network.
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr: Tuple[str, int], handler: type,
                 store: _Store, app_secret: str):
        super().__init__(addr, handler)
        self.store = store
        self.app_secret = app_secret


def start_server(port: int = 0, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 app_secret: str = DEFAULT_APP_SECRET
                 ) -> Tuple[_MockServer, str]:
    """Start the mock in a background thread.

    Returns (server, base_url) where base_url already includes the scheme
    and host:port (e.g. "http://127.0.0.1:54321"). The caller owns the
    server and must eventually call stop_server(server).
    """
    store = _Store(ttl_seconds=ttl_seconds)
    server = _MockServer(
        ("127.0.0.1", int(port)),
        _Handler,
        store=store,
        app_secret=app_secret,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Stash the thread so stop_server can verify shutdown intent.
    server._thread = thread  # type: ignore[attr-defined]
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


def stop_server(server: _MockServer) -> None:
    """Shut the mock down cleanly. Safe to call from a test teardown."""
    try:
        server.shutdown()
    finally:
        server.server_close()
        thread = getattr(server, "_thread", None)
        if thread is not None:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mock_activation_server",
        description=(
            "In-memory mock for the Sensie live-gesture SDK API "
            f"(contract {CONTRACT_VERSION}). Stdlib only."
        ),
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=(
            f"TCP port to bind (default {DEFAULT_PORT}). "
            "Use 0 to let the OS pick a free port."
        ),
    )
    p.add_argument(
        "--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS,
        help=(
            f"Activation-code TTL in seconds (default {DEFAULT_TTL_SECONDS})."
        ),
    )
    p.add_argument(
        "--app-secret", type=str, default=DEFAULT_APP_SECRET,
        help=(
            "Shared x-app-secret the mock will accept (default "
            f"{DEFAULT_APP_SECRET!r}). The trial key is REJECTED unless it "
            f"matches {TRIAL_KEY_PREFIX}<64 hex>."
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)
    server, base_url = start_server(
        port=args.port,
        ttl_seconds=args.ttl_seconds,
        app_secret=args.app_secret,
    )
    # Exactly one stdout line on start, so callers (CI, shell pipelines,
    # pytest fixtures that exec the script) can grep for readiness.
    print(
        f"mock activation server listening on {base_url}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
