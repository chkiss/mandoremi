#!/usr/bin/env python3
"""Nightly off-site backup of hsklyrics.db, encrypted at rest.

Writes a consistent snapshot into the Syncthing folder, so the database leaves
this machine. Everything else lives on one Hetzner volume: the .bak files sat
beside the live DB, which protects against a bad migration but not against
losing the disk. The DB holds user accounts and hours of corpus seeding.

Encrypted because these copies *travel*: Syncthing replicates them to other
machines, so unlike the live database they end up on disks with a different
threat model. GPG symmetric AES-256; the passphrase lives in a 0600 file
outside the repo (cron has no systemd environment), and MUST also live in the
password manager — an encrypted backup whose passphrase is lost is not a
backup.

Uses sqlite3's backup API rather than copying the file, so it is safe to run
against a live database (WAL mode, readers and writers active). Every run
decrypts what it just wrote and opens it, because an encrypted backup that
cannot be restored is worse than none: it fails silently and looks fine.

Cron (daily 04:17):
  17 4 * * * /usr/bin/python3 "$HOME/hsk-lyrics/tools/backup_db.py" >> "$HOME/backup_db.log" 2>&1
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

SRC = os.environ.get("HSKLYRICS_DB", os.path.expanduser("~/hsk-lyrics/hsklyrics.db"))
DEST_DIR = os.environ.get("MANDOREMI_BACKUP_DIR", os.path.expanduser("~/Sync/mandoremi-backups"))
PASSFILE = os.environ.get("MANDOREMI_BACKUP_KEYFILE",
                          os.path.expanduser("~/.config/mandoremi/backup.key"))
KEEP = 14
SUFFIX = ".db.gpg"


def gpg(args, passfile, **kw):
    """Run gpg with the passphrase read from a file descriptor, never argv."""
    return subprocess.run(
        ["gpg", "--batch", "--quiet", "--yes",
         "--passphrase-file", passfile, "--pinentry-mode", "loopback"] + args,
        check=True, **kw)


def main():
    if not os.path.exists(PASSFILE):
        print(f"ERROR: no backup passphrase at {PASSFILE}. Backups are encrypted; "
              f"create it (mode 600) and store the same passphrase in your password "
              f"manager.", file=sys.stderr)
        return 1

    os.makedirs(DEST_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    final = os.path.join(DEST_DIR, f"hsklyrics-{stamp}{SUFFIX}")

    with tempfile.TemporaryDirectory(prefix="mandoremi-bak-") as work:
        snap = os.path.join(work, "snapshot.db")
        src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)      # consistent snapshot of a live, WAL-mode DB
        dst.close()
        src.close()
        rows = sqlite3.connect(snap).execute(
            "SELECT COUNT(*) FROM users").fetchone()[0]

        # symmetric AES-256, compressed by gpg itself
        gpg(["--symmetric", "--cipher-algo", "AES256", "--compress-algo", "zlib",
             "--output", final, snap], PASSFILE)
        os.chmod(final, 0o600)

        # Restore drill, every run: decrypt and open what we just wrote.
        check = os.path.join(work, "verify.db")
        gpg(["--decrypt", "--output", check, final], PASSFILE)
        got = sqlite3.connect(check).execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if got != rows:
            print(f"ERROR: restore check failed ({got} users, expected {rows})",
                  file=sys.stderr)
            os.remove(final)
            return 1

    snaps = sorted(f for f in os.listdir(DEST_DIR)
                   if f.startswith("hsklyrics-") and f.endswith(SUFFIX))
    for old in snaps[:-KEEP]:
        os.remove(os.path.join(DEST_DIR, old))
    # drop any leftovers from the pre-encryption era
    for stale in os.listdir(DEST_DIR):
        if stale.endswith(".db.gz"):
            os.remove(os.path.join(DEST_DIR, stale))
            print(f"removed unencrypted legacy backup {stale}")

    size = os.path.getsize(final)
    print(f"{time.strftime('%F %T')} backed up {SRC} -> {final} "
          f"({size/1e6:.1f} MB, {rows} users, restore-checked, "
          f"keeping {min(len(snaps), KEEP)})")
    if size < 10_000:
        print("WARNING: snapshot suspiciously small", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
