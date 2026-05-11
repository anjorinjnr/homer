"""Tests for tools/short_links.py — generic portal URL shortener."""

import pytest

import tools.short_links as short_links


HID = "11111111-1111-1111-1111-111111111111"


class FakeSupabase:
    """Minimal in-memory stand-in for the Supabase REST client.

    Only implements the two methods short_links.py uses (select, insert)
    and enforces both the (household_id, target_url) uniqueness used for
    idempotency and the `code` PK uniqueness used for the retry path.
    """

    def __init__(self):
        self.rows: list[dict] = []
        self.insert_calls = 0

    def select(self, table, filters=None, order=None, limit=None, offset=None, columns="*"):
        assert table == short_links.TABLE
        filters = filters or {}
        # Filters arrive as {"household_id": "eq.<value>", ...}
        def matches(row):
            for col, expr in filters.items():
                op, _, val = expr.partition(".")
                assert op == "eq", f"only eq supported in fake, got {op}"
                if str(row.get(col)) != val:
                    return False
            return True
        out = [r for r in self.rows if matches(r)]
        if limit:
            out = out[:limit]
        return out

    def insert(self, table, data):
        assert table == short_links.TABLE
        self.insert_calls += 1
        if any(r["code"] == data["code"] for r in self.rows):
            raise RuntimeError("duplicate code (PK collision simulated)")
        if any(
            r["household_id"] == data["household_id"]
            and r["target_url"] == data["target_url"]
            for r in self.rows
        ):
            raise RuntimeError("duplicate (household_id, target_url)")
        self.rows.append(dict(data))
        return data


@pytest.fixture()
def fake_db(monkeypatch):
    """Patch the Supabase client with an in-memory fake."""
    db = FakeSupabase()
    monkeypatch.setattr(short_links, "_client", lambda: db)
    monkeypatch.setenv("PORTAL_BASE_URL", "https://example.com")
    return db


class TestShorten:
    def test_returns_short_url_with_s_prefix(self, fake_db):
        url = short_links.shorten(
            "https://example.com/rsvp/abc/event/token",
            household_id=HID,
        )
        assert url.startswith("https://example.com/s/")
        code = url.rsplit("/", 1)[-1]
        assert len(code) == short_links.CODE_LENGTH

    def test_idempotent_same_target(self, fake_db):
        target = "https://example.com/rsvp/abc/event/token"
        u1 = short_links.shorten(target, household_id=HID)
        u2 = short_links.shorten(target, household_id=HID)
        assert u1 == u2
        # Only one row should have been inserted.
        assert len(fake_db.rows) == 1

    def test_different_targets_get_different_codes(self, fake_db):
        u1 = short_links.shorten("https://example.com/a", household_id=HID)
        u2 = short_links.shorten("https://example.com/b", household_id=HID)
        assert u1 != u2

    def test_same_target_different_household_gets_different_code(self, fake_db):
        target = "https://example.com/x"
        u1 = short_links.shorten(target, household_id=HID)
        u2 = short_links.shorten(target, household_id="other-hh")
        assert u1 != u2

    def test_kind_is_persisted(self, fake_db):
        short_links.shorten(
            "https://example.com/rsvp/abc/event/token",
            household_id=HID,
            kind="rsvp",
        )
        assert fake_db.rows[0]["kind"] == "rsvp"

    def test_uses_portal_base_url_env(self, fake_db, monkeypatch):
        monkeypatch.setenv("PORTAL_BASE_URL", "https://staging.example.com")
        url = short_links.shorten("https://x/y", household_id=HID)
        assert url.startswith("https://staging.example.com/s/")

    def test_retries_on_pk_collision(self, fake_db, monkeypatch):
        """If the first random code collides, retry until we get a fresh one."""
        codes = iter(["XXXXYYYY", "XXXXYYYY", "ZZZZWWWW"])
        monkeypatch.setattr(short_links, "_random_code", lambda: next(codes))
        # Pre-seed a row with the colliding code under a different household,
        # so the (hh, target) uniqueness check doesn't short-circuit.
        fake_db.rows.append({
            "code": "XXXXYYYY",
            "household_id": "other-hh",
            "target_url": "https://other/url",
        })
        url = short_links.shorten("https://example.com/x", household_id=HID)
        assert url.endswith("/ZZZZWWWW")

    def test_raises_on_missing_household_id(self, fake_db):
        with pytest.raises(ValueError):
            short_links.shorten("https://x", household_id="")

    def test_raises_on_missing_target_url(self, fake_db):
        with pytest.raises(ValueError):
            short_links.shorten("", household_id=HID)


class TestShortenOrNone:
    def test_returns_url_on_success(self, fake_db):
        url = short_links.shorten_or_none("https://x/y", household_id=HID)
        assert url and url.startswith("https://example.com/s/")

    def test_returns_none_on_failure(self, monkeypatch, capsys):
        def boom():
            raise RuntimeError("supabase down")
        monkeypatch.setattr(short_links, "_client", boom)
        result = short_links.shorten_or_none("https://x/y", household_id=HID)
        assert result is None
        # Logs to stderr so the failure is observable in container logs.
        assert "short_links" in capsys.readouterr().err
