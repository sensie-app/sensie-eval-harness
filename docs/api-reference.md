# API Reference — Sensie SDK API (trial surface)

Base URL: `https://pqimowhxuxfcqqadlkdn.supabase.co/functions/v1` (override with `SENSIE_API_URL`).

Authentication: every request carries your trial key in the `x-api-key` header.
Key format: `sk_sensie_` + 64 hex characters. Keys are shown once at issuance and stored server-side only as a SHA-256 hash.

## Response conventions

- **Success** and **validation failures** both return HTTP 200 with an envelope:
  `{"status": "success" | "fail", "message": "...", "data": ...}`.
  A `status: "fail"` inside an HTTP 200 is a request-shape problem (missing/invalid field). Only two conditions use distinct HTTP status codes:
- **401** — missing/invalid API key: `{"status":"fail","message":"Invalid API key","data":[]}`
- **429** — trial quota exhausted (see below; different body shape, committed schema).

## Getting a key (issuance flow)

Key issuance happens at **https://somabets.com/trial**, not through this API surface: email → 6-digit verification code → key shown once. Behind the page, an OTP-authenticated call issues the key; the relevant error states you can see there:

| HTTP | body.error | Meaning |
|------|-----------|---------|
| 201 | — | key issued: `{apiKey, keyPrefix, shownOnce: true, quota: {limit: 100, window_days: 7, used: 0, window_reset_at: null}}` |
| 403 | `disposable_email` | disposable email domains can't start a trial |
| 409 | `trial_already_used` | that email already claimed its one trial |
| 401 | `auth_required` | email not verified (no valid session) |

## POST /sdk-api/session

Create an evaluation session. Not metered.

Request:

```json
{"userId": "eval-3fa1b2c4d5e6", "type": "evaluation", "sdkVersion": "0.1.0"}
```

`userId` (required) is any stable string identifying the end user in *your* system — the CLI auto-generates a non-identifying hash. `type` is `"evaluation"` (default) or `"calibration"`. Device/app fields (`deviceId`, `deviceOs`, `deviceModel`, `deviceVersion`, `appVersion`, `sdkVersion`) are optional.

Response (200):

```json
{"status": "success", "message": "Session created.", "data": {"session": {"id": "<uuid>", "...": "..."}}}
```

## POST /sdk-api/session/{sessionId}/sensie

Post one read. **This is the metered call** — each successful request counts against the trial quota (100 per rolling 7-day window).

Request:

```json
{"whips": 3, "flowing": 1, "agreement": 2}
```

| Field | Type | Constraint |
|-------|------|-----------|
| `whips` | integer | required; non-integers are rejected (`"Invalid typeof for: whips"`) |
| `flowing` | integer | required; `1` or `-1` |
| `agreement` | integer | required; `-1`, `1`, or `2` |

Raw motion fields (`accelerometerX/Y/Z`, `gyroscopeX/Y/Z`) exist in the API for licensed partners but are **rejected for trial keys** — the trial partner does not accept raw motion, by design. Send scalars only.

Response (200):

```json
{"status": "success", "message": "Sensie created.", "data": {"sensie": {"id": "<uuid>", "whips": 3, "flowing": 1, "agreement": 2, "...": "..."}}}
```

## GET /sdk-api/session/{sessionId}/sensie

List the reads in a session. Not metered.

Response (200):

```json
{"status": "success", "message": "List of session-sensie.", "data": {"sensies": [{"id": "<uuid>", "...": "..."}]}}
```

A session id that doesn't belong to your key returns an empty list, not an error.

## HTTP 429 — quota exhausted

When a trial key's rolling window is full, the metered call returns HTTP 429 with **exactly** this body (committed JSON schema; verified live):

```json
{"error": "quota_exceeded", "used": 100, "limit": 100, "window_reset_at": "2026-07-12T00:00:00+00:00"}
```

- `used` — reads counted inside the current rolling 7-day window.
- `limit` — 100 for the trial tier.
- `window_reset_at` — the UTC instant the **oldest counted read ages out** of the window, i.e. the earliest moment at least one read becomes available again. Null only if the window is empty (can't happen on an exhausted quota).
- A `Retry-After` header carries the same wait in seconds.

Quota exhaustion is a soft cap: always this 429, never a 5xx. There is no server-side queue — clients self-schedule using `window_reset_at`. The CLI surfaces all of this and exits with code 75.
