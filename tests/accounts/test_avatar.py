"""Profile pictures: a built-in preset or a small raster image, stored on the user's own profile row only."""

import base64

import pytest

from egoquant.accounts.routes import valid_avatar
from tests.accounts.test_accounts import A, B, client, store  # noqa: F401

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 200).decode()
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200).decode()
WEBP = "data:image/webp;base64," + base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 200).decode()


@pytest.fixture
def profiles(store):
    store.tables["profiles"] = [{"user_id": "user-a", "username": "alice", "avatar": None},
                                {"user_id": "user-b", "username": "bob", "avatar": None}]
    return store


@pytest.mark.parametrize("value", ["preset:p1", "preset:p5", JPEG, PNG, WEBP])
def test_presets_and_real_raster_images_are_accepted(value):
    assert valid_avatar(value)


@pytest.mark.parametrize("value", [
    "preset:p0", "preset:p6", "preset:p1 ", "https://evil.example/x.png",
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode(),   # SVG can carry script
    "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode(),  # not really a PNG
    "data:image/jpeg;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50).decode(),  # PNG labelled JPEG
    "data:image/webp;base64," + base64.b64encode(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 50).decode(),
    "data:image/png;base64,!!!!", "data:image/png;base64,",
    "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 64_000).decode(),  # over 64 kB
], ids=lambda v: f"{v[:24]}...len{len(v)}")
def test_everything_else_is_refused(value):
    assert not valid_avatar(value)


def test_set_and_read_own_avatar(client, profiles):
    assert client.put("/account/avatar", json={"avatar": "preset:p3"}, headers=A).json() == {"ok": True}
    assert client.get("/account/profile", headers=A).json() == {"username": "alice", "name": None, "avatar": "preset:p3"}
    assert client.get("/account/me", headers=A).json()["avatar"] == "preset:p3"
    assert client.put("/account/avatar", json={"avatar": JPEG}, headers=A).status_code == 200
    assert client.get("/account/profile", headers=A).json()["avatar"] == JPEG


def test_one_user_never_changes_another_users_picture(client, profiles):
    client.put("/account/avatar", json={"avatar": "preset:p2"}, headers=A)
    assert client.get("/account/profile", headers=B).json() == {"username": "bob", "name": None, "avatar": None}
    rows = {r["user_id"]: r["avatar"] for r in profiles.tables["profiles"]}
    assert rows == {"user-a": "preset:p2", "user-b": None}


def test_a_user_id_cannot_be_smuggled_in(client, profiles):
    r = client.put("/account/avatar", json={"avatar": "preset:p2", "user_id": "user-b"}, headers=A)
    assert r.status_code == 422
    assert all(row["avatar"] is None for row in profiles.tables["profiles"])


def test_a_bad_picture_is_refused_without_echoing_it(client, profiles):
    bad = "data:image/svg+xml;base64," + base64.b64encode(b"<svg onload=alert(1)>").decode()
    r = client.put("/account/avatar", json={"avatar": bad}, headers=A)
    assert r.status_code == 422 and "svg" not in r.text.lower()
    assert client.get("/account/profile", headers=A).json()["avatar"] is None


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer forged"}])
def test_profile_and_avatar_need_a_verified_token(client, profiles, headers):
    assert client.get("/account/profile", headers=headers).status_code == 401
    assert client.put("/account/avatar", json={"avatar": "preset:p1"}, headers=headers).status_code == 401
