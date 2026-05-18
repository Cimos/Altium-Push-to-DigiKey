"""Tests for digikey_push.

These tests cover BOM parsing, aggregation, payload shaping, and the push() function
with a mocked requests layer. Hitting the live DigiKey endpoint is intentionally avoided
so the suite is hermetic and CI-friendly.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

# Ensure the project root is on sys.path when tests are invoked outside an install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import digikey_push as dp  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _coerce_qty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3),
        ("3", 3),
        ("3.0", 3),
        ("  3 ", 3),
        ("3 pcs", 3),
        ("", 0),
        (None, 0),
        ("abc", 0),
        ("-1", -1),  # caller filters non-positives; coerce returns the number as parsed
        (3.7, 3),
    ],
)
def test_coerce_qty(value, expected):
    assert dp._coerce_qty(value) == expected


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


CSV_BASIC = textwrap.dedent("""\
    Designator,Name,Manufacturer 1,Manufacturer Part Number 1,Supplier 1,Supplier Part Number 1,Quantity
    "R1, R2, R3","10k 0402 1%","Yageo","RC0402FR-0710KL","Digikey","311-10.0KLRCT-ND","3"
    "C1, C2","100nF 0402","Murata","GRM155R71C104KA88D","Digikey","490-1320-1-ND","2"
    "U1","STM32H743VIT6","ST","STM32H743VIT6","Digikey","497-STM32H743VIT6-ND","1"
""")


def test_load_bom_csv_basic(tmp_path):
    p = write(tmp_path, "bom.csv", CSV_BASIC)
    items = dp.load_bom_csv(str(p))
    assert len(items) == 3
    assert items[0]["mpn"] == "RC0402FR-0710KL"
    assert items[0]["qty"] == 3
    assert items[0]["refs"] == "R1, R2, R3"
    assert items[0]["dkpn"] == "311-10.0KLRCT-ND"


def test_load_bom_csv_skips_empty_mpn(tmp_path):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity
        "R1","",3
        "R2","ACTUAL-PN",1
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    items = dp.load_bom_csv(str(p))
    assert len(items) == 1
    assert items[0]["mpn"] == "ACTUAL-PN"


def test_load_bom_csv_skips_zero_qty(tmp_path):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity
        "R1","PN-A",0
        "R2","PN-B",2
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    items = dp.load_bom_csv(str(p))
    assert len(items) == 1
    assert items[0]["mpn"] == "PN-B"


def test_load_bom_csv_alternate_headers(tmp_path):
    csv_text = textwrap.dedent("""\
        Ref,MPN,Qty
        R1,PN-A,5
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    items = dp.load_bom_csv(str(p))
    assert items == [{"mpn": "PN-A", "qty": 5, "refs": "R1", "dkpn": ""}]


def test_load_bom_csv_missing_required_columns(tmp_path):
    csv_text = "Designator,Description\nR1,resistor\n"
    p = write(tmp_path, "bom.csv", csv_text)
    with pytest.raises(SystemExit) as ei:
        dp.load_bom_csv(str(p))
    assert "MPN" in str(ei.value)


def test_load_bom_csv_dkpn_only_when_supplier_is_digikey(tmp_path):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity,Supplier 1,Supplier Part Number 1
        R1,PN-A,1,Mouser,MOUSER-12345
        R2,PN-B,1,Digikey,DK-67890
        R3,PN-C,1,Element14,E14-XYZ
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    items = dp.load_bom_csv(str(p))
    assert {it["mpn"]: it["dkpn"] for it in items} == {
        "PN-A": "",
        "PN-B": "DK-67890",
        "PN-C": "",
    }


def test_load_bom_csv_explicit_dkpn_header_trusts_value(tmp_path):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity,DigiKey Part Number
        R1,PN-A,1,DK-67890
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    items = dp.load_bom_csv(str(p))
    assert items[0]["dkpn"] == "DK-67890"


def test_load_bom_csv_empty(tmp_path):
    p = write(tmp_path, "bom.csv", "")
    items = dp.load_bom_csv(str(p))
    assert items == []


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------


def test_load_bom_json_basic(tmp_path):
    data = {
        "rows": [
            {"mpn": "PN-A", "quantity": 3, "ref_des": ["R1", "R2", "R3"]},
            {"mpn": "PN-B", "quantity": 2, "ref_des": ["C1", "C2"], "dkpn": "DK-B"},
            {"mpn": "PN-DNP", "quantity": 1, "ref_des": ["X1"], "dnp": True},
            {"mpn": "", "quantity": 1},
            {"mpn": "PN-ZERO", "quantity": 0, "ref_des": ["X2"]},
        ]
    }
    p = write(tmp_path, "bom.json", json.dumps(data))
    items = dp.load_bom_json(str(p))
    assert [it["mpn"] for it in items] == ["PN-A", "PN-B"]
    assert items[1]["dkpn"] == "DK-B"
    assert items[0]["refs"] == "R1, R2, R3"


# ---------------------------------------------------------------------------
# load_bom dispatcher
# ---------------------------------------------------------------------------


def test_load_bom_dispatch_by_extension(tmp_path):
    j = write(tmp_path, "x.json", json.dumps({"rows": [{"mpn": "PN", "quantity": 1}]}))
    c = write(tmp_path, "x.csv", "MPN,Qty\nPN,1\n")
    assert dp.load_bom(str(j))[0]["mpn"] == "PN"
    assert dp.load_bom(str(c))[0]["mpn"] == "PN"
    with pytest.raises(SystemExit):
        dp.load_bom(str(tmp_path / "nope.txt"))


# ---------------------------------------------------------------------------
# aggregate_by_mpn
# ---------------------------------------------------------------------------


def test_aggregate_by_mpn_merges_duplicate_rows():
    items = [
        {"mpn": "PN-A", "qty": 2, "refs": "R1, R2", "dkpn": ""},
        {"mpn": "PN-B", "qty": 1, "refs": "C1", "dkpn": "DK-B"},
        {"mpn": "PN-A", "qty": 3, "refs": "R3", "dkpn": "DK-A"},
        {"mpn": "pn-a", "qty": 1, "refs": "R4", "dkpn": ""},  # case-insensitive merge
    ]
    out = dp.aggregate_by_mpn(items)
    assert [it["mpn"] for it in out] == ["PN-A", "PN-B"]
    a = out[0]
    assert a["qty"] == 6
    assert sorted(r.strip() for r in a["refs"].split(",")) == ["R1", "R2", "R3", "R4"]
    assert a["dkpn"] == "DK-A"  # first non-empty wins


def test_aggregate_by_mpn_dedup_refs():
    items = [
        {"mpn": "PN-A", "qty": 1, "refs": "R1, R2", "dkpn": ""},
        {"mpn": "PN-A", "qty": 1, "refs": "R2, R3", "dkpn": ""},
    ]
    out = dp.aggregate_by_mpn(items)
    refs = sorted(r.strip() for r in out[0]["refs"].split(","))
    assert refs == ["R1", "R2", "R3"]
    assert out[0]["qty"] == 2


def test_aggregate_by_mpn_drops_empty_mpn():
    items = [{"mpn": "", "qty": 5, "refs": "", "dkpn": ""}]
    assert dp.aggregate_by_mpn(items) == []


# ---------------------------------------------------------------------------
# scale_quantities
# ---------------------------------------------------------------------------


def test_scale_quantities():
    items = [{"mpn": "PN-A", "qty": 3, "refs": "R1", "dkpn": ""}]
    out = dp.scale_quantities(items, 10)
    assert out[0]["qty"] == 30
    assert out[0]["mpn"] == "PN-A"


def test_scale_quantities_rejects_nonpositive():
    with pytest.raises(ValueError):
        dp.scale_quantities([], 0)
    with pytest.raises(ValueError):
        dp.scale_quantities([], -1)


# ---------------------------------------------------------------------------
# pick_part_number / build_payload
# ---------------------------------------------------------------------------


def test_pick_part_number_default_mpn():
    item = {"mpn": "PN-A", "dkpn": "DK-A"}
    assert dp.pick_part_number(item, "mpn") == "PN-A"


def test_pick_part_number_prefer_dkpn_with_fallback():
    assert dp.pick_part_number({"mpn": "PN-A", "dkpn": "DK-A"}, "dkpn") == "DK-A"
    assert dp.pick_part_number({"mpn": "PN-A", "dkpn": ""}, "dkpn") == "PN-A"


def test_build_payload_shape():
    items = [{"mpn": "PN-A", "qty": 3, "refs": "R1, R2", "dkpn": "DK-A"}]
    payload = dp.build_payload(items, prefer="mpn")
    assert payload == [
        {
            "requestedPartNumber": "PN-A",
            "quantities": [{"quantity": 3}],
            "customerReference": "R1, R2",
            "notes": "",
        }
    ]


def test_build_payload_prefer_dkpn():
    items = [{"mpn": "PN-A", "qty": 3, "refs": "", "dkpn": "DK-A"}]
    payload = dp.build_payload(items, prefer="dkpn")
    assert payload[0]["requestedPartNumber"] == "DK-A"


# ---------------------------------------------------------------------------
# push() with mocked requests
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def test_push_success():
    short = "https://www.digikey.com/short/abc123"
    items = [{"mpn": "PN-A", "qty": 1, "refs": "R1", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, json.dumps(short))
    ) as m:
        url, err = dp.push(items, "list1")
    assert err is None
    assert url == short
    args, kwargs = m.call_args
    assert args[0] == dp.API_URL
    assert kwargs["params"] == {"listName": "list1"}
    assert kwargs["json"][0]["requestedPartNumber"] == "PN-A"


def test_push_includes_tags_when_provided():
    short = "https://www.digikey.com/short/abc"
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, json.dumps(short))
    ) as m:
        dp.push(items, "list1", tags="a,b,c")
    assert m.call_args.kwargs["params"]["tags"] == "a,b,c"


def test_push_http_error():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(dp.requests, "post", return_value=FakeResponse(500, "boom")):
        url, err = dp.push(items, "list1")
    assert url is None
    assert "HTTP 500" in err


def test_push_non_json_response():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, "<html>nope</html>")
    ):
        url, err = dp.push(items, "list1")
    assert url is None
    assert "non-JSON" in err


def test_push_unexpected_response_shape():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, json.dumps({"foo": "bar"}))
    ):
        url, err = dp.push(items, "list1")
    assert url is None
    assert "unexpected response shape" in err


def test_push_network_error():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", side_effect=dp.requests.exceptions.ConnectionError("down")
    ):
        url, err = dp.push(items, "list1")
    assert url is None
    assert "network error" in err


# ---------------------------------------------------------------------------
# derive_list_name
# ---------------------------------------------------------------------------


def test_derive_list_name():
    name = dp.derive_list_name("/some/dir/CubeRacer-Rev-B.csv")
    assert name.startswith("CubeRacer-Rev-B-")
    assert re.match(r".+-\d{8}-\d{4}$", name)


# ---------------------------------------------------------------------------
# CLI integration (dry-run path, no network)
# ---------------------------------------------------------------------------


def test_cli_dry_run(tmp_path, capsys):
    p = write(tmp_path, "bom.csv", CSV_BASIC)
    rc = dp.main([str(p), "--dry-run", "--list-name", "test-list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "RC0402FR-0710KL" in out
    assert "STM32H743VIT6" in out
    assert "test-list" in out


def test_cli_dry_run_with_scale_and_aggregate(tmp_path, capsys):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity
        R1,PN-A,2
        R2,PN-A,3
        C1,PN-B,1
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    rc = dp.main([str(p), "--dry-run", "--scale", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    # PN-A should be aggregated to qty 5, then scaled to 50.
    payload_start = out.index("[")
    payload = json.loads(out[payload_start:])
    by_mpn = {row["requestedPartNumber"]: row["quantities"][0]["quantity"] for row in payload}
    assert by_mpn == {"PN-A": 50, "PN-B": 10}


def test_cli_dry_run_no_aggregate(tmp_path, capsys):
    csv_text = textwrap.dedent("""\
        Designator,Manufacturer Part Number 1,Quantity
        R1,PN-A,2
        R2,PN-A,3
    """)
    p = write(tmp_path, "bom.csv", csv_text)
    rc = dp.main([str(p), "--dry-run", "--no-aggregate"])
    assert rc == 0
    out = capsys.readouterr().out
    payload_start = out.index("[")
    payload = json.loads(out[payload_start:])
    qtys = [row["quantities"][0]["quantity"] for row in payload]
    assert qtys == [2, 3]


def test_cli_writes_out_file(tmp_path, capsys):
    p = write(tmp_path, "bom.csv", CSV_BASIC)
    out_path = tmp_path / "url.txt"
    short = "https://www.digikey.com/short/aaa"
    with mock.patch.object(dp.requests, "post", return_value=FakeResponse(200, json.dumps(short))):
        rc = dp.main(
            [
                str(p),
                "--out",
                str(out_path),
                "--no-warn-shareable",
                "--list-name",
                "test",
            ]
        )
    assert rc == 0
    assert out_path.read_text(encoding="utf-8").strip() == short


def test_cli_no_parseable_rows(tmp_path):
    p = write(tmp_path, "bom.csv", "Designator,Manufacturer Part Number 1,Quantity\nR1,,0\n")
    with pytest.raises(SystemExit):
        dp.main([str(p), "--dry-run"])


# ---------------------------------------------------------------------------
# Authenticated MyLists v1 path: build_auth_part_payload + push_authenticated
# ---------------------------------------------------------------------------


def test_build_auth_part_payload_shape():
    items = [
        {"mpn": "PN-A", "qty": 3, "refs": "R1, R2", "dkpn": ""},
        {"mpn": "PN-B", "qty": 1, "refs": "C1", "dkpn": "DK-B"},
    ]
    payload = dp.build_auth_part_payload(items, prefer="mpn")
    assert payload == [
        {
            "RequestedPartNumber": "PN-A",
            "OriginalPartNumber": "PN-A",
            "Quantities": [{"Quantity": 3}],
            "CustomerReference": "R1, R2",
            "Notes": "",
        },
        {
            "RequestedPartNumber": "PN-B",
            "OriginalPartNumber": "PN-B",
            "Quantities": [{"Quantity": 1}],
            "CustomerReference": "C1",
            "Notes": "",
        },
    ]


def test_build_auth_part_payload_prefer_dkpn_falls_back_to_mpn():
    items = [
        {"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": "DK-A"},
        {"mpn": "PN-B", "qty": 1, "refs": "", "dkpn": ""},
    ]
    payload = dp.build_auth_part_payload(items, prefer="dkpn")
    assert payload[0]["RequestedPartNumber"] == "DK-A"
    assert payload[1]["RequestedPartNumber"] == "PN-B"


def test_push_authenticated_two_step_success():
    items = [{"mpn": "PN-A", "qty": 3, "refs": "R1", "dkpn": ""}]
    posts = [
        FakeResponse(200, json.dumps("LISTID-123")),
        FakeResponse(200, json.dumps({"PartsAdded": 1})),
    ]
    with mock.patch.object(dp.requests, "post", side_effect=lambda *a, **kw: posts.pop(0)):
        list_id, list_url, err = dp.push_authenticated(
            items,
            "my-list",
            access_token="ACCESS",
            client_id="cid",
            environment="production",
            tags=["alpha"],
            prefer="mpn",
        )
    assert err is None
    assert list_id == "LISTID-123"
    assert list_url == "https://www.digikey.com/en/mylists/list/LISTID-123"


def test_push_authenticated_accepts_dict_list_id_response():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [
        FakeResponse(200, json.dumps({"ListId": "DICT-456"})),
        FakeResponse(204, ""),
    ]
    with mock.patch.object(dp.requests, "post", side_effect=lambda *a, **kw: posts.pop(0)):
        list_id, _list_url, err = dp.push_authenticated(
            items,
            "ln",
            access_token="A",
            client_id="cid",
        )
    assert err is None
    assert list_id == "DICT-456"


def test_push_authenticated_create_list_http_error():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(dp.requests, "post", return_value=FakeResponse(401, "unauthorized")):
        list_id, list_url, err = dp.push_authenticated(
            items,
            "ln",
            access_token="A",
            client_id="cid",
        )
    assert list_id is None
    assert list_url is None
    assert "CreateList HTTP 401" in err


def test_push_authenticated_create_list_no_id_in_response():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    with mock.patch.object(
        dp.requests, "post", return_value=FakeResponse(200, json.dumps({"foo": "bar"}))
    ):
        list_id, _list_url, err = dp.push_authenticated(
            items,
            "ln",
            access_token="A",
            client_id="cid",
        )
    assert list_id is None
    assert "no list id" in err.lower()


def test_push_authenticated_add_parts_fails_returns_partial():
    """CreateList ok, AddParts fails — return list_id so user can salvage the empty list."""
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [
        FakeResponse(200, json.dumps("PARTIAL-789")),
        FakeResponse(500, "boom"),
    ]
    with mock.patch.object(dp.requests, "post", side_effect=lambda *a, **kw: posts.pop(0)):
        list_id, list_url, err = dp.push_authenticated(
            items,
            "ln",
            access_token="A",
            client_id="cid",
        )
    assert list_id == "PARTIAL-789"
    assert list_url == "https://www.digikey.com/en/mylists/list/PARTIAL-789"
    assert "AddPartsToListId HTTP 500" in err


def test_push_authenticated_sandbox_routes_to_sandbox():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [FakeResponse(200, json.dumps("SBX-1")), FakeResponse(204, "")]
    seen_urls = []

    def fake_post(url, **kwargs):
        seen_urls.append(url)
        return posts.pop(0)

    with mock.patch.object(dp.requests, "post", side_effect=fake_post):
        _, _, err = dp.push_authenticated(
            items,
            "ln",
            access_token="A",
            client_id="cid",
            environment="sandbox",
        )
    assert err is None
    assert all(u.startswith("https://sandbox-api.digikey.com") for u in seen_urls)


def test_push_authenticated_sends_required_auth_headers():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [FakeResponse(200, json.dumps("X")), FakeResponse(204, "")]
    seen_headers = []

    def fake_post(url, **kwargs):
        seen_headers.append(kwargs.get("headers", {}))
        return posts.pop(0)

    with mock.patch.object(dp.requests, "post", side_effect=fake_post):
        dp.push_authenticated(items, "ln", access_token="THE-TOKEN", client_id="THE-CID")
    assert len(seen_headers) == 2
    for h in seen_headers:
        assert h["Authorization"] == "Bearer THE-TOKEN"
        assert h["X-DIGIKEY-Client-Id"] == "THE-CID"
        assert h["Content-Type"] == "application/json"


def test_push_authenticated_create_list_body_includes_tags():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [FakeResponse(200, json.dumps("X")), FakeResponse(204, "")]
    seen_bodies = []

    def fake_post(url, **kwargs):
        seen_bodies.append(kwargs.get("json"))
        return posts.pop(0)

    with mock.patch.object(dp.requests, "post", side_effect=fake_post):
        dp.push_authenticated(
            items,
            "my-list",
            access_token="A",
            client_id="cid",
            tags=["alpha", "beta"],
        )
    # First call is CreateList — should carry ListName, Source, Tags.
    create_body = seen_bodies[0]
    assert create_body["ListName"] == "my-list"
    assert create_body["Source"] == "b2b"
    assert create_body["Tags"] == ["alpha", "beta"]
    # Second call is AddPartsToListId — should carry the parts array.
    parts_body = seen_bodies[1]
    assert isinstance(parts_body, list)
    assert parts_body[0]["RequestedPartNumber"] == "PN-A"


def test_push_authenticated_add_parts_url_includes_index_param():
    items = [{"mpn": "PN-A", "qty": 1, "refs": "", "dkpn": ""}]
    posts = [FakeResponse(200, json.dumps("LID")), FakeResponse(204, "")]
    seen = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs.get("params")))
        return posts.pop(0)

    with mock.patch.object(dp.requests, "post", side_effect=fake_post):
        dp.push_authenticated(items, "ln", access_token="A", client_id="cid")
    # AddPartsToListId is the second POST.
    parts_url, parts_params = seen[1]
    assert parts_url.endswith("/mylists/v1/lists/LID/parts")
    assert parts_params == {"index": "0"}


# ---------------------------------------------------------------------------
# CLI integration: --auth --dry-run prints the auth-shape payload
# ---------------------------------------------------------------------------


def test_cli_auth_dry_run_prints_auth_payload(tmp_path, capsys):
    p = write(tmp_path, "bom.csv", "Designator,Manufacturer Part Number 1,Quantity\nR1,PN-A,3\n")
    rc = dp.main([str(p), "--auth", "--dry-run", "--list-name", "ln", "--tags", "a,b"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CreateList" in out
    assert "AddPartsToListId" in out
    assert "ListName" in out
    assert "RequestedPartNumber" in out
    # b2b source marker confirms the auth body shape, not the anonymous shape.
    assert '"Source": "b2b"' in out
    # Tags split into a list.
    assert '"a"' in out and '"b"' in out


def test_cli_auth_mode_no_credentials_exits_with_setup_hint(tmp_path, monkeypatch):
    """Without client_id/secret available, --auth (non-dry-run) exits and
    points the user at `altium-digikey-auth`."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for k in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)

    p = write(tmp_path, "bom.csv", "Designator,Manufacturer Part Number 1,Quantity\nR1,PN-A,3\n")
    with pytest.raises(SystemExit) as ei:
        dp.main([str(p), "--auth", "--list-name", "ln"])
    msg = str(ei.value)
    assert "altium-digikey-auth" in msg or "DIGIKEY_CLIENT_ID" in msg
