# Live mode (tier two)

You already ran the offline demo. This one uses a **real gesture**, done on a real phone in the SomaCheck app — so budget the time: about **15-20 minutes including calibration**. The activation code is valid for **30 minutes**.

Everything here is additive. `sensie-eval run` and `sensie-eval run --api` behave exactly as before.

## What you need

- A trial key in `SENSIE_API_KEY` (see the [quickstart](quickstart.md#live-api)).
- An iPhone that can install SomaCheck.
- About 20 uninterrupted minutes.

## What is shared

Raw motion stays on the phone and is never sent anywhere. The only things that come back are the three values the app derives from the gesture:

| Value | Type | Range |
|-------|------|-------|
| `whips` | integer | 0 or more |
| `flowing` | integer | `1` or `-1` |
| `agreement` | integer, optional | `-1`, `1`, or `2` — or not provided |

Those are the same three fields as the [`sensie` endpoint](api-reference.md#post-sdk-apisessionsessionidsensie). Nothing else is captured or shared. You can stop at any time.

`agreement` may be **not provided**. In the app it is optional feedback collected *after* the reveal, so it does not exist yet when the gesture completes, and the app never fills it in or guesses it. When it is missing, the CLI prints `agreement: not provided` (never `0` or any default) and nothing it prints is derived from agreement.

Consent is collected **in the terminal, before a code exists**. The CLI prints the consent text and asks for a yes; only then does it record your consent with Sensie and request a code. If you say no, nothing is sent.

> **Draft consent text.** Consent version `live-gesture-v1-draft`. The retention period and deletion route are not final: the text currently reads `[retention/deletion terms pending Sensie policy approval]` until Sensie's policy is approved.

## Run it

```text
$ export SENSIE_API_KEY=sk_sensie_your_key_here
$ sensie-eval run --live
```

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
  flowing:   1
  agreement: 2
Only these values left your phone; raw motion did not.
```

When the app did not send an agreement value, that line reads `  agreement: not provided` and the rest is the same.

The values are shown as the app derived them. The CLI does not score, rank, or route anyone from them.

### Flags

```bash
sensie-eval run --help
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--yes` | off | Confirm consent without the y/N prompt. The consent text is still printed. Required when there is no interactive terminal; without it, the CLI refuses (exit 2) and makes no request. |
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

A code that is not completed within 30 minutes expires. No result was produced, and nothing is stored beyond the expired code. Run `sensie-eval run --live` again for a new code; you will be asked for consent again.

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

The phone talks to the API separately to claim the code and report the three values. Set `SENSIE_API_URL` to point the CLI at another environment (see the [quickstart](quickstart.md#pointing-at-a-different-environment)).
