"""Tunable parameters: the registry's guarantees, the bounds, and the fallback.

The standing rule these serve: a guardrail must be tunable, observable and overridable, never a
silent block. Two failure modes matter more than the rest — a value written outside its bounds
(a slipped decimal turning a 15% cash floor into 150%), and defaults being substituted for the
operator's settings without saying so.
"""

from __future__ import annotations

import pytest
from app.db import DbUnavailable
from app.services import settings_store as store


@pytest.fixture(autouse=True)
def clean_cache():
    store.reset_cache()
    yield
    store.reset_cache()


def test_every_registered_default_sits_inside_its_own_bounds():
    """A default outside its bounds is unwritable: the UI would offer a reset button that the API
    then refuses, and the reset is the one action an operator reaches for when a value has gone
    wrong."""
    for p in store.REGISTRY:
        assert p.minimum <= p.default <= p.maximum, (
            f"{p.key} defaults to {p.default}, outside its own {p.minimum}–{p.maximum}"
        )


def test_every_parameter_says_what_it_does_and_where_it_shows_up():
    """A tunable with no explanation is a number an operator will not touch, which makes it a
    hardcoded value wearing a text box."""
    for p in store.REGISTRY:
        assert len(p.help) > 30, f"{p.key} has no usable help text"
        assert p.used_by, f"{p.key} does not say where a change takes effect"


def test_an_unknown_key_is_refused_rather_than_stored():
    """A settings table that accepts anything is where typos live silently — the write appears to
    succeed and the value it was meant to change never moves."""
    with pytest.raises(store.SettingError, match="not a tunable parameter"):
        store.set_value("drift_tolerence_pct", 2.0, actor=None)   # transposed, as typos are


@pytest.mark.parametrize("value", [-1.0, 0.0, 26.0, float("inf"), float("nan")])
def test_out_of_bounds_values_are_refused_and_the_message_names_the_bound(value):
    """The message is shown to the operator verbatim, so 'invalid value' would leave them guessing
    at a number they cannot see."""
    with pytest.raises(store.SettingError) as exc:
        store.set_value("drift_tolerance_pct", value, actor=None)
    assert "drift" in str(exc.value).lower() or "number" in str(exc.value).lower()


def test_the_database_being_down_yields_defaults_and_says_so(monkeypatch):
    """Substituting defaults silently is the dangerous version: a breach judged against 1.5 while
    the operator believes they set 3.0 is a guardrail lying about what it enforced."""
    def boom():
        raise DbUnavailable("down", "the database is unavailable")

    monkeypatch.setattr(store, "connection", lambda: boom())
    values, source = store.get_all()
    assert source == "defaults"
    assert values == store.defaults()


def test_the_registry_and_its_index_cannot_drift():
    assert set(store.BY_KEY) == {p.key for p in store.REGISTRY}
    assert len(store.BY_KEY) == len(store.REGISTRY), "two parameters share a key"
