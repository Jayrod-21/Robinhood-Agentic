"""§5.1/§5.2 — ONE Argon2id hasher, pinned parameters, shared by the CLI and the service.

Stored PHC strings are self-describing, so a hash minted with ANY parameters still verifies —
which is exactly why parameter drift is invisible to functional tests while being a live timing
oracle: real verifications run at the STORED hash's cost, the §5.2 dummy verification runs at
the service hasher's cost, and any difference between the two answers "does this email exist?"
per request. (Measured on this host before unification: bin/manage_operator.py hashed with
argon2-cffi's default parallelism=4 and verified in ~52 ms, while the pinned p=1 dummy took
~132 ms — unknown addresses were 2.5x SLOWER, the §5.2 defence inverted.)

These tests pin the seam: crypto.PASSWORD_HASHER is the one instance, the service verifies and
equalises with IT, and its output carries the exact pinned PHC parameter string. The CLI half —
that a hash actually written by ``seed``/``reset-password`` carries the same prefix — lives in
db/tests/test_manage_operator.py::test_hash_uses_argon2id_with_pinned_params.
"""

from __future__ import annotations

from app.services import auth, crypto

# The exact parameter segment every newly minted hash must carry (9b ADR-002, ported).
PINNED_PHC_PREFIX = "$argon2id$v=19$m=65536,t=3,p=1$"


def test_pinned_parameters_are_the_adr_002_set():
    assert (
        crypto.ARGON2_TIME_COST,
        crypto.ARGON2_MEMORY_COST,
        crypto.ARGON2_PARALLELISM,
    ) == (3, 65536, 1)
    hasher = crypto.PASSWORD_HASHER
    assert (hasher.time_cost, hasher.memory_cost, hasher.parallelism) == (3, 65536, 1)


def test_shared_hasher_output_carries_the_pinned_phc_prefix():
    assert crypto.PASSWORD_HASHER.hash("a perfectly serviceable pw").startswith(PINNED_PHC_PREFIX)


def test_service_verifier_is_the_shared_hasher_instance():
    """Identity, not equality: the service must verify (and dummy-verify) with THE instance the
    CLI stores with, so the two cannot drift independently ever again."""
    assert auth._hasher is crypto.PASSWORD_HASHER


def test_dummy_hash_carries_the_same_parameters_as_a_stored_hash():
    """The §5.2 timing-equalisation hash must do the SAME Argon2 work as verifying a real stored
    hash — i.e. identical PHC parameter segments ($argon2id$v=19$m=…,t=…,p=…)."""

    def params(phc: str) -> list[str]:
        return phc.split("$")[:4]

    assert params(auth._DUMMY_HASH) == params(crypto.PASSWORD_HASHER.hash("x" * 12))
    assert auth._DUMMY_HASH.startswith(PINNED_PHC_PREFIX)


def test_dummy_verify_never_raises():
    auth._dummy_verify()
