# Quickstart

Two paths: offline (no account, ~2 minutes) and live API (trial key, ~5 minutes).

## Offline (no account needed)

Requires Python ≥ 3.10.

**macOS (recommended):** Homebrew's Python blocks bare `pip install` into the system environment, so use pipx — it gives the CLI its own isolated env:

<!-- doctest: macos -->
```bash
brew install pipx
pipx install sensie-eval
sensie-eval run
```

**Anywhere with a virtualenv (Linux, CI, or if you prefer pip):**

```bash
pip install sensie-eval
sensie-eval run
```

That's it. The harness generates 50 synthetic subjects, calibrates on 70% of them, evaluates the held-out 30% under the subject-disjoint protocol, and prints a report ending in a PASS/FAIL banner.

**The banner is a methodology demo, not a claim.** It applies pre-registered thresholds to synthetic data whose parameters you control, and it reports honestly — at the default noise level (0.15) the accuracy threshold typically **fails**. Turn the base noise up and it passes:

```bash
sensie-eval run --noise 0.5
```

Counterintuitive on purpose: `--noise` scales the *separation* between high-signal and low-signal synthetic subjects, so more noise makes the routing decision easier. That inversion is exactly the kind of thing a subject-disjoint evaluation is supposed to surface — the banner tracks the data you feed it, not a marketing number.

Useful knobs:

```bash
sensie-eval run --n-subjects 100 --seed 7      # bigger cohort, different seed
sensie-eval run --train-frac 0.8               # different calibration split
sensie-eval version                            # print the version
```

## Live API

1. **Get a key:** https://somabets.com/trial — verify your email with a 6-digit code, and the key (`sk_sensie_<64 hex>`) is shown **exactly once**. Save it immediately.
   Trial terms, in plain words: 100 reads per rolling 7-day window · one trial per email, ever · disposable email domains rejected · no credit card.

2. **Run:**

<!-- doctest: api -->
```bash
export SENSIE_API_KEY=sk_sensie_your_key_here
sensie-eval run --api
```

The CLI:
- verifies your key by creating a session up front (`POST /sdk-api/session`, unmetered) — a bad key fails here in seconds,
- runs the offline evaluation,
- posts 5 summary reads derived from the held-out subjects (`POST /sdk-api/session/{id}/sensie`) — each one counts against your quota,
- lists them back (`GET /sdk-api/session/{id}/sensie`),
- prints the API summary followed by a clearly labeled synthetic routing report
  derived locally from the reads just posted.

Post more or fewer reads with `--reads N`:

<!-- doctest: api -->
```bash
sensie-eval run --api --reads 10
```

3. **Exit codes** (for scripting):

| Code | Meaning |
|------|---------|
| 0 | success |
| 75 | quota exhausted (HTTP 429) — see [quota-limits.md](quota-limits.md) |
| 77 | authentication failed (HTTP 401) — check `SENSIE_API_KEY` |
| 78 | `SENSIE_API_KEY` not set |

## Pointing at a different environment

`SENSIE_API_URL` overrides the API base URL (default is production):

```bash
export SENSIE_API_URL=http://127.0.0.1:54321/functions/v1   # local Supabase stack
```

## Next steps

- [API reference](api-reference.md) — exact request/response shapes
- [Quota & limits](quota-limits.md) — how the rolling window works
- [Troubleshooting](troubleshooting.md) — every error you can hit, with fixes
