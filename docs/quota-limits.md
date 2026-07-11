# Quota & Limits

The trial tier is deliberately simple. There are exactly four rules:

1. **100 reads per rolling 7-day window.** A "read" is one successful `POST /sdk-api/session/{id}/sensie`. Session creation and listing are free.
2. **The window rolls; it doesn't reset at midnight.** Your quota is the count of reads over the trailing 7 calendar days (UTC), including today.
3. **One trial per email, ever.** Enforced at the database constraint level, not just in application code.
4. **No credit card, no auto-upgrade, no surprise billing.** When you hit the cap, requests return a structured 429 — nothing else happens.

## How the rolling window behaves — a worked example

Say you make reads on these days:

| Day | Reads made | Reads in window (trailing 7 days) |
|-----|-----------|-----------------------------------|
| Mon Jul 6 | 50 | 50 |
| Thu Jul 9 | 49 | 99 |
| Fri Jul 10 | 1 | 100 — **quota full** |
| Sat Jul 11 | attempt → 429 | 100 |
| Mon Jul 13 (00:00 UTC) | — | 50 — Mon Jul 6's reads aged out |
| Mon Jul 13 | up to 50 more | ≤100 |

The counting is day-grained: a read made on day *D* leaves the window at *D + 7 days, 00:00 UTC*.

## window_reset_at

The 429 body tells you exactly when capacity returns:

```json
{"error": "quota_exceeded", "used": 100, "limit": 100, "window_reset_at": "2026-07-12T00:00:00+00:00"}
```

`window_reset_at` is the instant the **oldest counted read** ages out of the window — the earliest moment at least one read becomes available. It is not "when you get all 100 back"; capacity returns gradually as old reads age out day by day. A `Retry-After` header carries the equivalent seconds.

To wait it out in a script (exit code 75 means quota):

<!-- doctest: api -->
```bash
sensie-eval run --api --reads 1; rc=$?
if [ "$rc" -eq 75 ]; then
  echo "quota exhausted — retry after the window_reset_at timestamp"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
```

## Other limits

- **Key security:** keys are shown once at issuance; the server stores only a SHA-256 hash. A lost key cannot be recovered (see [troubleshooting](troubleshooting.md)).
- **Raw motion:** trial keys cannot submit raw accelerometer/gyroscope arrays — scalars only. This is an IP and privacy boundary, not a soft default.
- **Disposable email domains** are rejected at signup (deny-list of ~3,500 domains).

## When 100 reads isn't enough

That's the point of the trial: enough to validate the integration path end to end, not enough to run a study. The next step is the pre-registered pilot — see [the README](../README.md#the-pilot) or write to mike@joinsensie.com.
