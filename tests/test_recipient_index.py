"""recipient_index — email -> prospect/opportunity store + DDB provisioner."""
from __future__ import annotations

import pytest

from backend.common import recipient_index
from backend.common.dynamodb import ensure_table, ClientError


class FakeTable:
    """Minimal DynamoDB Table double (PK=email)."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, *, Item):  # noqa: N803 — boto3 API name
        self.items[Item["email"]] = Item

    def get_item(self, *, Key):  # noqa: N803
        item = self.items.get(Key["email"])
        return {"Item": item} if item is not None else {}


# --------------------------------------------------------------------------
# record / lookup
# --------------------------------------------------------------------------

def test_record_then_lookup_round_trip():
    tbl = FakeTable()
    assert recipient_index.record_recipient(
        email="Owner@Acme.test", prospect_id="pr-1", opportunity_id="op-1", tbl=tbl,
    ) is True
    # Normalised (lower/strip) on both write and read.
    rec = recipient_index.lookup_recipient("  owner@acme.test ", tbl=tbl)
    assert rec == {"prospect_id": "pr-1", "opportunity_id": "op-1"}


def test_lookup_unknown_is_none():
    assert recipient_index.lookup_recipient("nobody@nowhere.test", tbl=FakeTable()) is None


def test_record_requires_email_and_prospect():
    tbl = FakeTable()
    assert recipient_index.record_recipient(email="", prospect_id="pr-1", tbl=tbl) is False
    assert recipient_index.record_recipient(email="a@b.test", prospect_id="", tbl=tbl) is False
    assert tbl.items == {}


def test_disabled_table_is_noop(monkeypatch):
    # _index_table() returning None -> the index disables itself (no AWS, clean no-op).
    monkeypatch.setattr(recipient_index, "_index_table", lambda: None)
    assert recipient_index.record_recipient(email="a@b.test", prospect_id="pr-1") is False
    assert recipient_index.lookup_recipient("a@b.test") is None


def test_write_error_is_swallowed():
    class _Boom(FakeTable):
        def put_item(self, *, Item):  # noqa: N803
            raise ClientError({"Error": {"Code": "X"}}, "PutItem")

    assert recipient_index.record_recipient(
        email="a@b.test", prospect_id="pr-1", tbl=_Boom(),
    ) is False


# --------------------------------------------------------------------------
# ensure_table provisioner
# --------------------------------------------------------------------------

def test_ensure_table_creates_when_absent():
    created = {}

    class _Tbl:
        def wait_until_exists(self):
            created["waited"] = True

    class _Res:
        def create_table(self, **kw):
            created["kw"] = kw
            return _Tbl()

    out = ensure_table("samus_recipient_index", partition_key="email", resource=_Res())
    assert out == {"table": "samus_recipient_index", "created": True,
                   "status": "created", "partition_key": "email"}
    assert created["kw"]["KeySchema"][0]["AttributeName"] == "email"
    assert created["kw"]["BillingMode"] == "PAY_PER_REQUEST"
    assert created["waited"] is True


def test_ensure_table_idempotent_when_exists():
    class _Res:
        def create_table(self, **kw):
            raise ClientError(
                {"Error": {"Code": "ResourceInUseException"}}, "CreateTable",
            )

    out = ensure_table("samus_recipient_index", resource=_Res())
    assert out["created"] is False
    assert out["status"] == "exists"
