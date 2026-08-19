# Handling password resets (manual, by design)

There is no transactional email provider. Users request a reset through a
captcha-gated form; you reset it and reply from your own mailbox.

## The loop

1. User submits the form at `/` → "Forgot your password?".
2. A row lands in `password_resets`, and if SMTP is configured you get a mail.
3. On the server:

   ```bash
   cd ~/hsk-lyrics
   ./.venv/bin/python tools/reset_requests.py --list
   ./.venv/bin/python tools/reset_requests.py --reset user@example.com
   ```

   That sets a new random password, kills the account's sessions, clears any
   lockout, marks the request handled, and prints a ready-to-send message.
4. Paste it into your own mail client and send.

## The rule that matters

**Send only to the "registered address" the tool prints.** It comes from the
`users` table, never from the form. Someone can type *your user's* email into
the form; if you replied to an address the form supplied, that form would be an
account-takeover tool. The form deliberately has no reply-to field, and a test
asserts the table has no such column.

Other properties worth knowing:

- The endpoint answers identically whether or not the account exists, so it
  can't be used to enumerate registered emails.
- Repeat requests for the same address collapse into one open row, so a user
  clicking twice doesn't spam your queue.
- Rate limited to 3 per 15 min per IP, and captcha-gated.
- A request with no matching account is still recorded — it's usually a typo by
  a real user, and seeing it is how you can help them.

## Owner notifications (optional, free)

`app/notify.py` mails **you**, authenticated as your own Gmail. Nothing
user-facing is ever sent by the server, so deliverability and reputation are
non-issues. Unset means log-only; the request is still queued and listed.

```
SMTP_USER=you@gmail.com
SMTP_PASS=<Gmail App Password — needs 2FA on the account>
OWNER_EMAIL=you@gmail.com     # defaults to SMTP_USER
```

Put them in the systemd user unit's environment
(`~/.config/systemd/user/mandoremi.service.d/env.conf`) as
`Environment="SMTP_USER=..."`, then `systemctl --user daemon-reload &&
systemctl --user restart mandoremi`.

Note the domain currently publishes `MX 0 .`, `v=spf1 -all` and DMARC
`p=reject` — a deliberate "this domain sends no mail" lockdown. That's fine:
this path sends *as your Gmail address*, not as mandoremi.com, so SPF/DKIM are
Google's and alignment is theirs. If you ever want mail *from* mandoremi.com,
all three records have to change.

## Captcha

Cloudflare Turnstile, on registration and reset requests only — never on the
anonymous analyze box, which is the funnel and persists nothing.

```
TURNSTILE_SITE_KEY=0x...
TURNSTILE_SECRET=0x...
```

Unset means no captcha and no Cloudflare script is loaded at all. It fails
**open** if Cloudflare is unreachable — an outage there shouldn't lock people
out of signing up, and the rate limits still apply.
