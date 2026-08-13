#!/usr/bin/env python3
"""List and action manual password reset requests.

There is no transactional email service by design, so resets are done by hand:
this sets a new random password and prints the message to paste into your own
mail client.

Run with the venv interpreter — the system python has no fastapi:

    cd ~/hsk-lyrics
    ./.venv/bin/python tools/reset_requests.py --list
    ./.venv/bin/python tools/reset_requests.py --reset user@example.com
    ./.venv/bin/python tools/reset_requests.py --dismiss user@example.com

SEND THE NEW PASSWORD ONLY TO THE ADDRESS PRINTED as "registered address".
It is read from the users table, never from the reset form, so that filling in
someone else's email cannot be used to take over their account.
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import auth, db  # noqa: E402

WORDS = ("amber", "birch", "cedar", "delta", "ember", "flint", "grove", "harbor",
         "indigo", "juniper", "kestrel", "lantern", "meadow", "north", "opal",
         "pine", "quarry", "river", "summit", "thistle", "umber", "violet",
         "willow", "yarrow")


def new_password():
    """Readable enough to retype from an email, random enough to be safe:
    ~2^47 from three words plus four digits."""
    return "-".join(secrets.choice(WORDS) for _ in range(3)) + \
        "-" + str(secrets.randbelow(9000) + 1000)


def cmd_list(conn, show_all):
    q = ("SELECT r.*, u.email AS registered FROM password_resets r "
         "LEFT JOIN users u ON u.id = r.user_id "
         + ("" if show_all else "WHERE r.handled IS NULL ") +
         "ORDER BY r.created DESC LIMIT 50")
    rows = conn.execute(q).fetchall()
    if not rows:
        print("no open reset requests")
        return
    for r in rows:
        state = "handled " + r["handled"] if r["handled"] else "OPEN"
        who = r["registered"] or "(no matching account)"
        print(f"[{r['id']:4d}] {r['created']}  {state}\n"
              f"        typed: {r['email']}\n"
              f"        account: {who}")
        if r["note"]:
            print(f"        note: {r['note'][:200]}")


def cmd_reset(conn, email):
    email = email.strip().lower()
    row = conn.execute(
        "SELECT r.id, u.id AS uid, u.email AS registered FROM password_resets r "
        "JOIN users u ON u.id = r.user_id "
        "WHERE r.email = ? AND r.handled IS NULL ORDER BY r.created LIMIT 1",
        (email,)).fetchone()
    if not row:
        print(f"no open request matching an account for {email!r}.")
        print("run --list to see what's pending (a request can exist with no account).")
        return 1

    pw = new_password()
    conn.execute("UPDATE users SET pwhash = ? WHERE id = ?",
                 (auth.hash_password(pw), row["uid"]))
    # Kill existing sessions: if the account was compromised, a reset that
    # leaves the intruder logged in achieves nothing.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["uid"],))
    conn.execute("DELETE FROM lockouts WHERE email = ?", (row["registered"],))
    conn.execute("UPDATE password_resets SET handled = datetime('now') WHERE id = ?",
                 (row["id"],))
    conn.commit()

    print(f"\npassword reset, sessions cleared, lockout cleared.")
    print(f"registered address: {row['registered']}   <-- send ONLY here\n")
    print("--- copy from here -------------------------------------------")
    print(f"""Subject: Your Mandoremi password

Hi — you asked to reset your Mandoremi password.

Your new temporary password is:

    {pw}

Sign in at https://mandoremi.com/ and change it straight away under
Settings -> Change password.

If you didn't request this, reply to this message and let me know.""")
    print("--- to here --------------------------------------------------")
    return 0


def cmd_dismiss(conn, email):
    n = conn.execute("UPDATE password_resets SET handled = datetime('now') "
                     "WHERE email = ? AND handled IS NULL",
                     (email.strip().lower(),)).rowcount
    conn.commit()
    print(f"dismissed {n} request(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --list, include handled")
    ap.add_argument("--reset", metavar="EMAIL")
    ap.add_argument("--dismiss", metavar="EMAIL")
    args = ap.parse_args()

    db.init()
    with db.connect() as conn:
        if args.reset:
            return cmd_reset(conn, args.reset)
        if args.dismiss:
            return cmd_dismiss(conn, args.dismiss)
        return cmd_list(conn, args.all)


if __name__ == "__main__":
    sys.exit(main() or 0)
