import pytest

from igris.people import PeopleError, PeopleStore


@pytest.fixture
def store(tmp_path):
    return PeopleStore(tmp_path / "people.sqlite3")


def test_find_ignores_case_and_diacritics(store):
    person = store.add("alice", "Student Deniz \u00d6zt\u00fcrk")

    assert store.find("alice", "ozturk") == [person]
    assert store.find("alice", "\u00d6ZT\u00dcRK") == [person]
    assert store.find("alice", "student deniz") == [person]


def test_find_matches_alias_and_first_name(store):
    person = store.add("alice", "Ece Inceo\u011flu", aliases=("Ece Club",))

    assert store.find("alice", "ece") == [person]
    assert store.find("alice", "\u0130nceoglu") == [person]
    assert store.find("alice", "inceoglu") == [person]
    assert store.find("alice", "club") == [person]


def test_find_returns_all_namesakes(store):
    store.add("alice", "Can A")
    store.add("alice", "Can B")

    assert len(store.find("alice", "can")) == 2


def test_find_unknown_is_empty(store):
    store.add("alice", "Ece Inceoglu")

    assert store.find("alice", "nobody") == []


@pytest.mark.parametrize("address", [
    "a b@x.lt",
    "x@y",
    "x@y.lt\nBcc: z@z.lt",
    "<x@y.lt>",
])
def test_add_rejects_bad_address(store, address):
    with pytest.raises(PeopleError):
        store.add("alice", "Ece", emails=(address,))


@pytest.mark.parametrize(
    "field,value",
    [("importance", "urgent"), ("tone", "rude"), ("identity", "work")],
)
def test_add_rejects_bad_enum(store, field, value):
    with pytest.raises(PeopleError):
        store.add("alice", "Ece", **{field: value})


def test_owner_isolation(store):
    person = store.add("a", "Ece")

    assert store.find("b", "Ece") == []
    assert store.list("b") == []
    assert store.get("b", person.id) is None
    assert store.remove("b", person.id) is False
    assert store.get("a", person.id) == person


def test_update_changes_preferred_address_and_keeps_id(store):
    person = store.add("alice", "Ece", emails=("first@example.lt", "second@example.lt"))

    updated = store.update("alice", person.id, emails=("new@example.lt", "first@example.lt"))

    assert updated.id == person.id
    assert updated.emails == ("new@example.lt", "first@example.lt")
    assert store.by_address("alice", "new@example.lt") == updated


def test_by_address_is_case_insensitive(store):
    person = store.add("alice", "Ece", emails=("Ece@Example.LT",))

    assert store.by_address("alice", "ece@example.lt") == person


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "people.sqlite3"
    first_store = PeopleStore(path)
    person = first_store.add("alice", "Ece", aliases=("ESN Ece",), emails=("ece@example.lt",))

    reopened = PeopleStore(path)

    assert reopened.get("alice", person.id) == person
    assert reopened.find("alice", "esn") == [person]
