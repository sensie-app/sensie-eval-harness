# Live mode (tier two)

**Status: preview, not yet released.** The backend routes are not deployed to production and no public SomaCheck build includes activation-code entry yet. The app side has been exercised in the iOS Simulator only; it has not been validated on a physical device.

You already ran the offline demo. This one uses a **real gesture**, done on a real phone in the SomaCheck app — so budget the time: about **15-20 minutes including calibration**. The activation code is valid for **30 minutes**.

Everything here is additive. `sensie-eval run` and `sensie-eval run --api` behave exactly as before.

## What you need

- A trial key in `SENSIE_API_KEY` (see the [quickstart](quickstart.md#live-api)).
- An iPhone that can install SomaCheck.
- About 20 uninterrupted minutes.

## What is shared

The researcher running this command never receives raw motion, the statement you check, or your account details. They receive two values the app derives from your gesture:

| Value | Type | Meaning |
|-------|------|---------|
| `whips` | integer, 0 or more | how many gesture movements were counted |
| `flowing` | integer, `1` or `-1` | `1` if your reading was Aligned, `-1` if it was Unaligned |

The report also has an optional `agreement` field (the [`sensie` endpoint](api-reference.md#post-sdk-apisessionsessionidsensie) accepts `-1`, `1` or `2`). The app does not fill it in, so the CLI prints `agreement: not provided` (never `0` or any default) and nothing it prints is derived from agreement.

The SomaCheck app itself handles your check under the SomaCheck privacy policy. That includes sending your motion data, the statement you check, and your reading to Sensie, and recording app usage events (for example that you linked, completed, or stopped sharing a code; never the code or the values). <!-- Approved by Mike on 2026-09-21, along with the retention terms below. -->

This is a reading of your own gesture. Do the gesture yourself, on your own phone. Do not give the code to anyone else or use it to collect another person's reading.

You can stop at any time.

Consent is collected **in the terminal, before a code exists**. The CLI prints the consent text and asks for a yes; only then does it record your consent with Sensie and request a code. If you say no, nothing is sent.

> **Draft consent version.** Consent version `live-gesture-v1-draft`. The consent wording and retention terms are approved (Mike, 2026-09-21): Sensie keeps the consent record and the values from your check for up to one year, then deletes them; email mike@joinsensie.com to ask for earlier deletion. The version stays `-draft` until the separate release gates (device test, secret provisioning, deploy) are cleared.

## Run it

```text
$ export SENSIE_API_KEY=sk_sensie_your_key_here
$ sensie-eval run --live
```

`run --live` currently exits 2 against production: the consent version
(`live-gesture-v1-draft`) is still a draft, and the CLI refuses to record a
draft consent version against the production API. It works against a
non-production `SENSIE_API_URL` (e.g. local/staging) in the meantime.

The CLI then:

1. prints the consent text and asks `Do you consent to this? [y/N]`,
2. records consent, then requests an activation code,
3. prints the install link, your 8-character code, and the `somacheck://activate/<CODE>` link,
4. checks the code every 10 seconds and prints a status line each time it changes: `pending` (waiting for the code to be entered), `claimed` (entered in the app; calibration and gesture in progress), then `completed`,
5. prints your values (`agreement` reads `not provided` when the app did not send one).

In the app, open SomaCheck (install link: https://go.somacheck.com/install), enter the code, and do the gesture. The result appears in your terminal when the app finishes.

A completed run ends like this:

```text
Your live read
----------------------------------------
Real gesture, done on your phone in the SomaCheck app — not synthetic data.
  whips:     3
  flowing:   1 (Aligned)
  agreement: not provided
The researcher-facing result is only the values above; raw motion is never shared with the researcher.
```

The `agreement` line reads `not provided` because the app does not fill it in.

The values are shown as the app derived them. The CLI does not score, rank, or route anyone from them.

### Flags

```bash
sensie-eval run --help
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--yes` | off | Confirm consent without the y/N prompt, only when you are the person doing the gesture. The consent text is still printed. Required when there is no interactive terminal; without it, the CLI refuses (exit 2) and makes no request. |
| `--poll-interval SECONDS` | 10 | Seconds between status checks. |
| `--timeout MINUTES` | 30 | Stop waiting after this long. |

`--live` and `--api` cannot be combined.

## Checking back later

You do not have to keep the terminal open. Press Ctrl-C at any time (exit code 130) and the CLI prints how to resume. The code keeps working until it expires.

```text
$ sensie-eval status ABCD2345
  status: claimed — code entered in the app — calibration and gesture in progress (14m 02s left on the code)
```

`status` makes one request and prints one of:

- **completed** — your values (`agreement` may read `not provided`), exit 0.
- **pending** or **claimed** — the status and the time left on the code, exit 0.
- **expired** — exit 76 (see below).

```bash
sensie-eval status --help
```

If `--timeout` passes before the gesture is finished, the CLI prints the same resume hint and exits 0: the code is still valid, so this is a pause, not a failure.

## If the code expires

A code that is not completed within 30 minutes expires. No result was produced. Sensie keeps the consent record and the expired code; no gesture values were stored. Run `sensie-eval run --live` again for a new code; you will be asked for consent again.

If the app cannot read the gesture, it asks you to try again. That is not a result and nothing is reported for it.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | completed, or `--timeout` reached with the code still valid, or `status` on an in-progress code |
| 1 | consent declined, other API error, or network failure |
| 2 | bad arguments, or consent needed but no interactive terminal and no `--yes` |
| 75 | quota exhausted (HTTP 429, `quota_exceeded`) — see [quota-limits.md](quota-limits.md) |
| 76 | activation code expired |
| 77 | authentication failed (HTTP 401) |
| 78 | `SENSIE_API_KEY` not set |
| 130 | interrupted with Ctrl-C |

Two different things return HTTP 429: `quota_exceeded` (exit 75, read quota) and `rate_limited` (exit 1, more than 3 unexpired, unfinished codes on the key). The CLI tells them apart by the `error` field in the body.

## Endpoints used

Live mode calls, and only calls:

- `POST /sdk-api/trial/consent`
- `POST /sdk-api/trial/activation-code`
- `GET /sdk-api/activation/{code}`

The phone talks to the API separately to claim the code and report the two required values (`whips`, `flowing`) plus the optional `agreement`. Set `SENSIE_API_URL` to point the CLI at another environment (see the [quickstart](quickstart.md#pointing-at-a-different-environment)).
