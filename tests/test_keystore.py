import pytest

from ezgrader.keystore import KeyStore, ProfileError


def test_profiles_round_trip(memory_keyring):
    ks = KeyStore()
    assert ks.list_profiles() == []
    ks.create("Emmanuel")
    ks.set_secret("Emmanuel", "ed_token", "  ed-abcdef1234  ")
    ks.set_canvas_base_url("Emmanuel", "https://canvas.example.edu")

    info = ks.list_profiles()[0]
    assert info == {"name": "Emmanuel", "canvas_base_url": "https://canvas.example.edu",
                    "ed_token": "••••1234", "canvas_token": None}
    assert ks.get_secret("Emmanuel", "ed_token") == "ed-abcdef1234"
    # Stored in the keyring, not anywhere else.
    assert memory_keyring.items[("EzGrader", "Emmanuel::ed_token")] == "ed-abcdef1234"


def test_public_info_never_contains_full_tokens():
    ks = KeyStore()
    ks.create("A")
    ks.set_secret("A", "canvas_token", "1234~supersecretvalue9999")
    assert "supersecret" not in repr(ks.list_profiles())


def test_multiple_profiles_and_delete(memory_keyring):
    ks = KeyStore()
    ks.create("Bea")
    ks.create("alex")
    ks.set_secret("Bea", "ed_token", "tok-bea")
    assert [p["name"] for p in ks.list_profiles()] == ["alex", "Bea"]
    ks.delete("Bea")
    assert [p["name"] for p in ks.list_profiles()] == ["alex"]
    assert ("EzGrader", "Bea::ed_token") not in memory_keyring.items


@pytest.mark.parametrize("name", ["", " ", "a/b", "x" * 41, "-dash"])
def test_invalid_profile_names(name):
    with pytest.raises(ProfileError):
        KeyStore().create(name)


def test_duplicate_profile_names_case_insensitive():
    ks = KeyStore()
    ks.create("Sam")
    with pytest.raises(ProfileError):
        ks.create("sam")


def test_rejects_bad_tokens():
    ks = KeyStore()
    ks.create("A")
    with pytest.raises(ProfileError):
        ks.set_secret("A", "ed_token", "")
    with pytest.raises(ProfileError):
        ks.set_secret("A", "ed_token", "has space")
    with pytest.raises(ProfileError):
        ks.set_secret("A", "other", "tok")
    with pytest.raises(ProfileError):
        ks.set_secret("missing", "ed_token", "tok")
