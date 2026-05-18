"""Tests for the authenticated-push surface in digikey_push.

Covers _auth_api_base, _auth_headers, build_auth_part_payload, _extract_list_id
(every response shape), push_authenticated (2-step happy path, every error
arm), and the CLI --auth path's dry-run + delegation behavior. All HTTP is
mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import digikey_push as dp  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload, raw_text=None):
        self.status_code = status_code
        if raw_text is not None:
            self.text = raw_text
        else:
            self.text = json.dumps(payload)


# ---------------------------------------------------------------------------
# Endpoint + header helpers
# ---------------------------------------------------------------------------


def test_auth_api_base_prod_vs_sandbox():
    assert dp._auth_api_base("production") == dp.AUTH_API_BASE_PROD
    assert dp._auth_api_base("sandbox") == dp.AUTH_API_BASE_SANDBOX
    # Anything not "sandbox" falls back to prod (defensive).
    assert dp._auth_api_base("") == dp.AUTH_API_BASE_PROD


def test_auth_headers_shape():
    h = dp._auth_headers("CID", "AT-bearer")
    assert h["Authorization"] == "Bearer AT-bearer"
    assert h["X-DIGIKEY-Client-Id"] == "CID"
    assert h["Content-Type"] == "application/json"
    assert h["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# build_auth_part_payload
# ---------------------------------------------------------------------------


def test_build_auth_part_payload_uses_pascal_case():
    items = [{"mpn": "PN-A", "qty": 3, "refs": "R1, R2", "dkpn": ""}]
    payload = dp.build_auth_part_payload(items)
    assert payload == [
        {
            "RequestedPartNumber": "PN-A",
            "OriginalPartNumber": "PN-A",
            "Quantities": [{"Quantity": 3}],
            "CustomerReference": "R1, R2",
            "Notes": "",
        }
    ]


def test_build_auth_part_payload_prefer_dkpn():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": "DK-A"}]
    payload = dp.build_auth_part_payload(items, prefer="dkpn")
    assert payload[0]["RequestedPartNumber"] == "DK-A"
    assert payload[0]["OriginalPartNumber"] == "DK-A"


# ---------------------------------------------------------------------------
# _extract_list_id — every observed response shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ('"abc-123"', "abc-123"),
        ('{"ListId": "abc-123"}', "abc-123"),
        ('{"Id": "abc-123"}', "abc-123"),
        ('{"listId": "abc-123"}', "abc-123"),
        ('{"id": "abc-123"}', "abc-123"),
        ('{"ListName": "x"}', None),  # no id field
        ("not-json", None),
        ('""', None),
        ('{"ListId": ""}', None),
    ],
)
def test_extract_list_id_variants(body, expected):
    assert dp._extract_list_id(body) == expected


# ---------------------------------------------------------------------------
# push_authenticated — 2-step flow
# ---------------------------------------------------------------------------


def _items():
    return [{"mpn": "PN-A", "qty": 1, "refs": "R1", "dkpn": ""}]


def test_push_authenticated_happy_path():
    create_resp = FakeResponse(200, "abc-list-123")
    parts_resp = FakeResponse(204, "", raw_text="")
    with mock.patch.object(dp.requests, "post", side_effect=[create_resp, parts_resp]) as m:
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert err is None
    assert list_id == "abc-list-123"
    assert list_url == "https://www.digikey.com/en/mylists/list/abc-list-123"
    # Verify CreateList call
    create_call = m.call_args_list[0]
    assert create_call.args[0].endswith(dp.AUTH_LISTS_PATH)
    assert create_call.kwargs["json"]["ListName"] == "list1"
    assert create_call.kwargs["json"]["Source"] == "b2b"
    assert create_call.kwargs["headers"]["Authorization"] == "Bearer AT"
    # Verify AddPartsToListId call
    parts_call = m.call_args_list[1]
    assert parts_call.args[0].endswith("/abc-list-123/parts")
    assert parts_call.kwargs["params"] == {"index": "0"}
    assert parts_call.kwargs["json"][0]["RequestedPartNumber"] == "PN-A"


def test_push_authenticated_sandbox_uses_sandbox_host():
    create_resp = FakeResponse(200, "id")
    parts_resp = FakeResponse(204, "", raw_text="")
    with mock.patch.object(dp.requests, "post", side_effect=[create_resp, parts_resp]) as m:
        dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
            environment="sandbox",
        )
    assert "sandbox-api.digikey.com" in m.call_args_list[0].args[0]


def test_push_authenticated_includes_tags_when_provided():
    create_resp = FakeResponse(200, "id")
    parts_resp = FakeResponse(204, "", raw_text="")
    with mock.patch.object(dp.requests, "post", side_effect=[create_resp, parts_resp]) as m:
        dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
            tags=("a", "b", "c"),
        )
    assert m.call_args_list[0].kwargs["json"]["Tags"] == ["a", "b", "c"]


def test_push_authenticated_create_list_http_error():
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(500, None, raw_text="boom")
    ):
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert list_id is None
    assert list_url is None
    assert "CreateList HTTP 500" in err


def test_push_authenticated_create_list_no_id_in_response():
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, {"ListName": "nope"})
    ):
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert list_id is None
    assert "no list id was found" in err


def test_push_authenticated_create_list_network_error():
    with mock.patch.object(
        dp.requests, "post", side_effect=dp.requests.exceptions.ConnectionError("down")
    ):
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert list_id is None
    assert "network error on CreateList" in err


def test_push_authenticated_parts_error_returns_partial_success():
    create_resp = FakeResponse(200, "list-id-1")
    parts_resp = FakeResponse(400, None, raw_text="bad part")
    with mock.patch.object(dp.requests, "post", side_effect=[create_resp, parts_resp]):
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert list_id == "list-id-1"
    assert list_url == "https://www.digikey.com/en/mylists/list/list-id-1"
    assert "AddPartsToListId HTTP 400" in err


def test_push_authenticated_parts_network_error():
    create_resp = FakeResponse(200, "list-id-1")
    with mock.patch.object(
        dp.requests,
        "post",
        side_effect=[create_resp, dp.requests.exceptions.ConnectionError("down")],
    ):
        list_id, list_url, err = dp.push_authenticated(
            _items(),
            "list1",
            access_token="AT",
            client_id="CID",
        )
    assert list_id == "list-id-1"
    assert "network error on AddPartsToListId" in err


# ---------------------------------------------------------------------------
# CLI --auth path
# ---------------------------------------------------------------------------


def test_cli_dry_run_with_auth_emits_both_payloads(tmp_path, capsys):
    csv_text = "Designator,Manufacturer Part Number 1,Quantity\nR1,PN-A,1\n"
    p = tmp_path / "bom.csv"
    p.write_text(csv_text, encoding="utf-8")
    rc = dp.main([str(p), "--auth", "--dry-run", "--list-name", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CreateList body" in out
    assert "AddPartsToListId body" in out
    assert "ListName" in out
    assert "RequestedPartNumber" in out


def test_cli_auth_mode_invokes_get_valid_access_token_and_push(tmp_path, capsys):
    csv_text = "Designator,Manufacturer Part Number 1,Quantity\nR1,PN-A,1\n"
    p = tmp_path / "bom.csv"
    p.write_text(csv_text, encoding="utf-8")

    import digikey_oauth as do_mod

    class _Cfg:
        environment = "production"
        client_id = "CID"

    cfg = do_mod.OAuthConfig(client_id="CID", client_secret="SEC")
    tokens = do_mod.TokenSet(access_token="AT-live", refresh_token="RT", expires_at=99999999999.0)

    with mock.patch.object(
        do_mod, "get_valid_access_token", return_value=("AT-live", cfg, tokens)
    ) as gvat, mock.patch.object(
        dp,
        "push_authenticated",
        return_value=("list-1", "https://www.digikey.com/en/mylists/list/list-1", None),
    ) as pa:
        rc = dp.main([str(p), "--auth", "--list-name", "test-list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Success" in out
    assert "list-1" in out
    gvat.assert_called_once()
    pa.assert_called_once()
    call_kwargs = pa.call_args.kwargs
    assert call_kwargs["access_token"] == "AT-live"
    assert call_kwargs["client_id"] == "CID"


def test_cli_auth_mode_exits_on_oauth_error(tmp_path):
    csv_text = "Designator,Manufacturer Part Number 1,Quantity\nR1,PN-A,1\n"
    p = tmp_path / "bom.csv"
    p.write_text(csv_text, encoding="utf-8")

    import digikey_oauth as do_mod

    with mock.patch.object(
        do_mod, "get_valid_access_token", side_effect=do_mod.DigiKeyOAuthError("no cached tokens")
    ):
        with pytest.raises(SystemExit) as ei:
            dp.main([str(p), "--auth"])
    assert "no cached tokens" in str(ei.value)
