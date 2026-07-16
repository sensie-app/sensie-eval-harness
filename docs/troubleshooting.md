# Troubleshooting / FAQ

Every error you can hit, in the order you're likely to hit it.

## Install

**"zsh: command not found: pip"**
On macOS, Homebrew Python blocks bare system installs and some shells do not expose `pip`. Use pipx instead, then run the first command:

<!-- doctest: macos -->
```bash
brew install pipx
pipx install sensie-eval
sensie-eval run
```

## Signup (somabets.com/trial)

**"Disposable email domains can't start a trial." (HTTP 403, `disposable_email`)**
The trial requires a work, school, or personal address. Aliases of well-known disposable providers are all on the deny-list. Use a different address — but note each email gets exactly one trial.

**"This email has already claimed its free trial." (HTTP 409, `trial_already_used`)**
One trial per email, ever — including trials whose keys were lost or whose gateways were deleted. If you believe this is wrong (e.g. a teammate used the shared address), write to mike@joinsensie.com.

**The 6-digit code never arrives.**
Check spam for "Your Sensie trial verification code". Codes expire quickly; request a fresh one by re-submitting your email.

## Key handling

**I lost my key.**
Keys are shown exactly once and stored server-side only as a SHA-256 hash — there is no recovery path, and the one-trial-per-email rule means the same email can't get a new one. Contact mike@joinsensie.com.

**What does a valid key look like?**
`sk_sensie_` followed by exactly 64 hex characters. Anything else fails format validation before touching the database.

## Running `sensie-eval run --api`

**"Error: SENSIE_API_KEY is not set." (exit code 78)**

```bash
export SENSIE_API_KEY=sk_sensie_your_key_here
```

Put it in your shell profile or a `.env` you source — don't commit it anywhere.

**"Authentication failed (HTTP 401)." (exit code 77)**
The key is malformed, disabled, or wrong. Check for copy-paste truncation (it's 74 characters total) and stray whitespace/quotes in the env var.

**"Trial quota exhausted (HTTP 429)." (exit code 75)**
Not a bug — you've used your 100 reads in the current rolling 7-day window. The message includes `window_reset_at`, the moment the oldest read ages out and capacity starts returning. See [quota-limits.md](quota-limits.md).

**"Invalid typeof for: whips" (status "fail" inside an HTTP 200)**
`whips` must be an **integer** — the API rejects floats. Same for `flowing` (1 or -1) and `agreement` (-1, 1, or 2). Note the API's convention: request-shape errors come back as `{"status":"fail", ...}` with HTTP 200; only auth (401) and quota (429) use distinct HTTP codes.

**"Raw motion egress forbidden for this partner."**
You sent `accelerometerX/Y/Z` or `gyroscopeX/Y/Z` fields. Trial keys can't submit raw motion — send only the three scalar summary values. (The CLI never does this; you'll only see it from hand-rolled requests.)

**Connection errors / timeouts.**
Check `SENSIE_API_URL` if you've overridden it (default is production). Corporate proxies that strip the `x-api-key` header will produce 401s.

## Python environment

**`pip install sensie-eval` fails or `sensie-eval: command not found`.**
Requires Python ≥ 3.10. If pip installed into a Python you're not running, use `python3 -m pip install sensie-eval` and `python3 -m sensie_eval.cli run`. In virtualenvs, re-activate after install.

**NumPy/SciPy build errors on install.**
Upgrade pip first (`python3 -m pip install --upgrade pip`) so it pulls prebuilt wheels instead of compiling from source.

## Honest questions

**Can I fake the motion signal to game the pre-screen?**
The trial surface accepts only scalar summaries, so there's nothing to fake here. In production, classification runs server-side on involuntary micro-dynamics of gesture motion — the signal degrades when engagement degrades, which is precisely what it's for.

**Does the harness phone home?**
No. Offline mode (`sensie-eval run`) makes zero network calls. Live mode calls exactly the three documented endpoints, only when you pass `--api`, and sends only the documented scalar fields.

Still stuck? mike@joinsensie.com — include the command you ran and the full output.
