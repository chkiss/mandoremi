import pytest
from fastapi import HTTPException

from app import auth


def test_password_roundtrip():
    stored = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("anything", "garbage-no-dollar")


def test_lockout_escalation(conn):
    email = "victim@example.com"
    # first two failures just count
    for _ in range(auth.LOCKOUT_THRESHOLD - 1):
        auth.record_login_failure(conn, email)
        auth.check_lockout(conn, email)  # not locked yet
    # third failure locks for period 1 and raises
    with pytest.raises(HTTPException) as e:
        auth.record_login_failure(conn, email)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        auth.check_lockout(conn, email)
    row = conn.execute("SELECT * FROM lockouts WHERE email = ?", (email,)).fetchone()
    assert row["lockout_count"] == 1 and row["fails"] == 0 and not row["permanent"]


def test_lockout_caps_instead_of_going_permanent(conn):
    """The ladder tops out at the longest period and never becomes permanent.

    There is no self-service password reset, so a permanent lock is a state a
    real user can walk into by mistake with no way out but the owner running
    SQL. A 7-day lock deters credential stuffing just as effectively.
    """
    email = "victim@example.com"
    for _round in range(len(auth.LOCKOUT_PERIODS) + 3):   # well past the ladder
        conn.execute("UPDATE lockouts SET locked_until = NULL WHERE email = ?", (email,))
        for _ in range(auth.LOCKOUT_THRESHOLD - 1):
            auth.record_login_failure(conn, email)
        with pytest.raises(HTTPException) as e:
            auth.record_login_failure(conn, email)
        assert "permanently" not in e.value.detail

    row = conn.execute("SELECT * FROM lockouts WHERE email = ?", (email,)).fetchone()
    assert not row["permanent"]
    assert row["lockout_count"] > len(auth.LOCKOUT_PERIODS)
    # still locked, just for the capped period rather than forever
    conn.execute("UPDATE lockouts SET locked_until = NULL WHERE email = ?", (email,))
    with pytest.raises(HTTPException) as e:
        auth.record_login_failure(conn, email)
        auth.record_login_failure(conn, email)
        auth.record_login_failure(conn, email)
    assert "7 days" in e.value.detail or "day" in e.value.detail


def test_admin_can_still_hard_ban(conn):
    """The permanent flag remains honoured when set deliberately by an admin."""
    email = "abuser@example.com"
    conn.execute("INSERT INTO lockouts(email, fails, lockout_count, permanent) "
                 "VALUES (?,0,0,1)", (email,))
    with pytest.raises(HTTPException) as e:
        auth.check_lockout(conn, email)
    assert "permanently" in e.value.detail


def test_failure_counter_survives_rollback(conn):
    """The regression that motivated explicit commits: a `with conn` block
    rolls back on the raised HTTPException."""
    email = "victim@example.com"
    try:
        with conn:
            auth.record_login_failure(conn, email)
            raise RuntimeError("simulated request failure")
    except RuntimeError:
        pass
    row = conn.execute("SELECT fails FROM lockouts WHERE email = ?", (email,)).fetchone()
    assert row["fails"] == 1


def test_clear_login_failures(conn):
    auth.record_login_failure(conn, "a@b.co")
    auth.clear_login_failures(conn, "a@b.co")
    assert conn.execute("SELECT * FROM lockouts").fetchone() is None
