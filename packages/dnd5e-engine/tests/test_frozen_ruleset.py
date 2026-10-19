"""Fixed corpus trust, isolation, lifecycle and actual entrypoint regressions."""

import asyncio
import gc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from weakref import ref

import pytest
from dnd5e_srd_data.loader import BundledAssetLoader, MemoryAssetLoader
from pydantic import BaseModel, ValidationError

from dnd5e_engine import evaluation_ruleset as rules
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    query_action_availability,
)
from dnd5e_engine.evaluation_initialization import evaluate_combat_initialization_candidate
from tests.evaluation_support import WEAPON, synthetic_loader
from tests.test_stateless_attack import request_and_live
from tests.test_stateless_initialization import init_case


def test_fixed_content_retains_canonical_binding_and_detaches_nested_fields():
    weapon = synthetic_loader().get_weapon(WEAPON)
    wizard = BundledAssetLoader().get_class("wizard")
    source = MemoryAssetLoader(items=[weapon], classes=[wizard])
    original = rules.ruleset_binding(source)
    fixed = rules.freeze_ruleset(source)
    assert rules.ruleset_binding(fixed) == original
    assert rules.freeze_ruleset(fixed) is fixed
    expected_weapon, expected_class = deepcopy(weapon), deepcopy(wizard)

    # Original input, mutable top-level model, nested model/list and free-form
    # advancement dictionaries all have independent ownership.
    weapon.magical_bonus = 5
    weapon.activities.clear()
    wizard.advancement[0].configuration["injected"] = {"nested": [1]}
    candidate = fixed.get_weapon(WEAPON)
    candidate.damage_parts.clear()
    with pytest.raises(ValidationError):
        candidate.range.value = 120
    candidate.activities[0].effects.clear()
    candidate_class = fixed.get_class("wizard")
    candidate_class.advancement[0].configuration["new"] = ["changed"]
    fixed.list_slugs("items").clear()
    assert fixed.get_item(WEAPON) == expected_weapon
    assert fixed.get_weapon(WEAPON) == expected_weapon
    assert fixed.get_class("wizard") == expected_class
    assert rules.ruleset_binding(fixed) == original
    with pytest.raises(rules.RulesetBindingError):
        rules.verify_ruleset(original, source)
    assert rules.freeze_ruleset(source).binding.data_revision != original.data_revision
    with pytest.raises(AttributeError):
        fixed.binding = original
    with pytest.raises(AttributeError):
        fixed.__init__(source)


def test_multiple_datasets_keep_distinct_content_and_release_sources():
    source = synthetic_loader()
    source_ref = ref(source)
    first = rules.freeze_ruleset(source)
    source.get_weapon(WEAPON).magical_bonus = 2
    second = rules.freeze_ruleset(source)
    assert first.binding != second.binding
    assert first.get_weapon(WEAPON).magical_bonus == 0
    assert second.get_weapon(WEAPON).magical_bonus == 2
    del source
    gc.collect()
    assert source_ref() is None
    dataset_ref = ref(second)
    del second
    gc.collect()
    assert dataset_ref() is None
    assert rules.get_default_ruleset().get_item(WEAPON) is None


def test_fixed_verify_never_enumerates_loads_or_hashes(monkeypatch):
    source = synthetic_loader()
    counts = Counter()
    original = source.list_slugs

    def enumerate_assets(category):
        counts["enumeration"] += 1
        return original(category)

    source.list_slugs = enumerate_assets
    binding = rules.ruleset_binding(source)
    counts.clear()
    for _ in range(5):
        rules.verify_ruleset(binding, source)
    assert counts["enumeration"] == 55
    fixed = rules.freeze_ruleset(source)
    counts.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("fixed verification revisited corpus content")

    monkeypatch.setattr(rules, "_compute_binding", forbidden)
    monkeypatch.setattr(type(fixed), "list_slugs", forbidden)
    monkeypatch.setattr(BundledAssetLoader, "_load_json", forbidden)
    monkeypatch.setattr(rules.hashlib, "sha256", forbidden)
    for _ in range(5):
        rules.verify_ruleset(binding, fixed)
    assert counts == {}
    for field, value in (
        ("ruleset_id", "untrusted"),
        ("evaluator_version", "untrusted"),
        ("data_revision", "sha256:" + "0" * 64),
    ):
        with pytest.raises(rules.RulesetBindingError):
            rules.verify_ruleset(binding.model_copy(update={field: value}), fixed)


def test_default_identity_retains_pre_snapshot_canonical_digest():
    fixed = rules.get_default_ruleset()
    assert fixed.binding == rules.ruleset_binding(BundledAssetLoader())
    assert fixed.binding.data_revision == (
        "sha256:f1f2b8d5e9bef154940cb14c615f2f4ac7301a4ab9d869841bc1b86e9664c44e"
    )


def test_live_loader_cannot_claim_a_trusted_binding():
    source = synthetic_loader()
    source.binding = rules.freeze_ruleset(source).binding
    source.get_weapon(WEAPON).magical_bonus = 2
    with pytest.raises(rules.RulesetBindingError):
        rules.verify_ruleset(source.binding, source)


def test_uuid_collision_keeps_binding_error_contract_and_never_computes_identity(monkeypatch):
    spell = rules.get_default_ruleset().get_spell("fireball")
    homebrew = spell.model_copy(deep=True, update={"slug": "hb-fireball"})
    source = MemoryAssetLoader(spells=[spell, homebrew])

    def forbidden(*args, **kwargs):
        pytest.fail("invalid UUID corpus must fail before computing a trusted binding")

    monkeypatch.setattr(rules, "_compute_binding", forbidden)
    with pytest.raises(rules.RulesetBindingError) as caught:
        rules.freeze_ruleset(source)
    for identity in (spell.foundry_uuid, spell.slug, homebrew.slug):
        assert identity in str(caught.value)


def test_default_uuid_collision_does_not_publish_and_retry_after_correction_succeeds(monkeypatch):
    existing = rules.get_default_ruleset()
    existing_binding = existing.binding
    spell = existing.get_spell("fireball")
    homebrew = spell.model_copy(deep=True, update={"slug": "hb-fireball"})
    source = MemoryAssetLoader(spells=[spell, homebrew])
    monkeypatch.setattr(rules, "_DEFAULT_RULESET", None)
    monkeypatch.setattr(rules, "BundledAssetLoader", lambda: source)
    original_compute = rules._compute_binding
    computed = []

    def compute(loader):
        computed.append(loader)
        return original_compute(loader)

    monkeypatch.setattr(rules, "_compute_binding", compute)
    for _ in range(2):
        with pytest.raises(rules.RulesetBindingError):
            rules.get_default_ruleset()
        assert rules._DEFAULT_RULESET is None
        assert computed == []
        assert existing.binding == existing_binding
        assert existing.get_spell_by_uuid(spell.foundry_uuid) == spell
    homebrew.foundry_uuid = "custom:corrected-fireball"
    fixed = rules.get_default_ruleset()
    assert rules.get_default_ruleset() is fixed
    assert computed == [fixed]
    assert fixed.get_spell_by_uuid(spell.foundry_uuid) == spell
    assert fixed.get_spell_by_uuid(homebrew.foundry_uuid) == homebrew
    assert existing.get_spell("hb-fireball") is None
    assert existing.binding == existing_binding


def test_bundled_materialization_reads_parses_once_and_uuid_uses_same_corpus(monkeypatch):
    # A small real JSON corpus gives exact counts independent of corpus growth.
    from pathlib import Path

    fixture = Path(__file__).parents[2] / "dnd5e-srd-data/tests/fixtures/canonical"
    source = BundledAssetLoader(root=fixture)
    reads, parses, constructions = Counter(), Counter(), Counter()
    original = BundledAssetLoader._load_json
    original_model = BundledAssetLoader._model
    validate = BaseModel.model_validate.__func__

    def construct(cls, obj, *args, **kwargs):
        constructions[cls.__name__, obj["slug"]] += 1
        return validate(cls, obj, *args, **kwargs)

    def read(loader, category, slug):
        reads[category, slug] += 1
        return original(loader, category, slug)

    def parse(loader, category, slug, model):
        if loader._models is None or (category, slug) not in loader._models:
            parses[category, slug] += 1
        return original_model(loader, category, slug, model)

    monkeypatch.setattr(BundledAssetLoader, "_load_json", read)
    monkeypatch.setattr(BundledAssetLoader, "_model", parse)
    binding = rules.ruleset_binding(source)
    reads.clear()
    for _ in range(5):
        rules.verify_ruleset(binding, source)
    assert all(n == (15 if category == "items" else 5) for (category, _), n in reads.items())
    reads.clear()
    parses.clear()
    monkeypatch.setattr(BaseModel, "model_validate", classmethod(construct))
    fixed = rules.freeze_ruleset(source)
    assert reads
    assert set(reads.values()) == {1}
    assert reads == parses
    assert set(constructions.values()) == {1}
    before = reads.copy()
    before_constructions = constructions.copy()
    for _ in range(5):
        assert fixed.get_item("test-longsword") == fixed.get_weapon("test-longsword")
        assert fixed.get_armor("test-longsword") is None
        rules.verify_ruleset(fixed.binding, fixed)
    assert reads == before
    assert constructions == before_constructions
    # Restore generic model validation before materializing another custom source.
    monkeypatch.setattr(BaseModel, "model_validate", classmethod(validate))
    spell = rules.get_default_ruleset().get_spell("fireball")
    expected_spell = deepcopy(spell)
    spell_source = MemoryAssetLoader(spells=[spell])
    indexed = rules.freeze_ruleset(spell_source)
    spell_source.get_spell("fireball").foundry_uuid = "changed"
    monkeypatch.setattr(type(indexed), "list_slugs", lambda *a: pytest.fail("UUID scan"))
    found = indexed.get_spell_by_uuid(expected_spell.foundry_uuid)
    assert found == expected_spell
    found.activities.clear()
    assert indexed.get_spell_by_uuid(expected_spell.foundry_uuid) == expected_spell
    assert indexed.get_spell_by_uuid("") is None
    assert indexed.get_spell_by_uuid("missing") is None


@pytest.mark.parametrize("problem", ["duplicate", "missing", "identity", "accessor", "invalid"])
def test_failed_sources_reject_before_publishing_and_can_be_retried(problem):
    source = synthetic_loader()
    if problem == "duplicate":
        source.list_slugs = lambda category: [WEAPON, WEAPON] if category == "items" else []
    elif problem == "missing":
        source.get_item = lambda slug: None
    elif problem == "identity":
        source.get_item(WEAPON).slug = "wrong"
    elif problem == "accessor":
        source.get_weapon = lambda slug: None
    else:
        source.get_weapon(WEAPON).magical_bonus = -1
    with pytest.raises(rules.RulesetBindingError):
        rules.freeze_ruleset(source)
    assert rules.freeze_ruleset(synthetic_loader()).get_weapon(WEAPON) is not None


def test_default_concurrent_first_load_is_atomic_and_failed_load_retries(monkeypatch):
    saved = rules.get_default_ruleset()
    entered, release = Event(), Event()
    calls = []

    def build(source):
        calls.append(source)
        entered.set()
        assert release.wait(10)
        return saved

    monkeypatch.setattr(rules, "_DEFAULT_RULESET", None)
    monkeypatch.setattr(rules, "freeze_ruleset", build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(rules.get_default_ruleset) for _ in range(8)]
        try:
            assert entered.wait(10)
            assert rules._DEFAULT_RULESET is None
            assert not any(future.done() for future in pending)
        finally:
            release.set()
        assert all(future.result() is saved for future in pending)
    assert len(calls) == 1
    monkeypatch.setattr(rules, "_DEFAULT_RULESET", None)

    def fail_once(source):
        assert rules._DEFAULT_RULESET is None
        raise ValueError("broken source")

    monkeypatch.setattr(rules, "freeze_ruleset", fail_once)
    with pytest.raises(ValueError, match="broken source"):
        rules.get_default_ruleset()
    assert rules._DEFAULT_RULESET is None
    monkeypatch.setattr(rules, "freeze_ruleset", lambda source: saved)
    assert rules.get_default_ruleset() is saved


def test_custom_override_execution_and_rng_match_live_source_after_detachment():
    request, _, _ = request_and_live(seed=5)
    source = synthetic_loader()
    source.get_weapon(WEAPON).magical_bonus = 2
    fixed = rules.freeze_ruleset(source)
    request = request.model_copy(update={"ruleset_binding": fixed.binding})
    expected = asyncio.run(evaluate(request, loader=source))
    source.get_weapon(WEAPON).magical_bonus = 5
    assert asyncio.run(evaluate(request, loader=fixed)) == expected
    assert expected.status == "accepted"
    assert expected.rng_transition.state_changed
    with pytest.raises(rules.RulesetBindingError):
        asyncio.run(evaluate(request, loader=source))


def test_evaluation_availability_and_initialization_share_fixed_loader(monkeypatch):
    request, _, _ = request_and_live(seed=4)
    fixed = rules.freeze_ruleset(synthetic_loader())
    query = ActionAvailabilityRequest(
        schema_version="engine-availability/13",
        **{
            name: getattr(request, name)
            for name in (
                "session_id",
                "operation_kind",
                "actor_id",
                "payload",
                "state_snapshot",
                "ruleset_binding",
            )
        },
    )
    init_request, _, _, source, _ = init_case()
    init_fixed = rules.freeze_ruleset(source)
    expected = asyncio.run(evaluate(request, loader=fixed))
    available = query_action_availability(query, loader=fixed)
    initialized = evaluate_combat_initialization_candidate(init_request, loader=init_fixed)
    monkeypatch.setattr(rules, "_DEFAULT_RULESET", fixed)

    def forbidden(*args, **kwargs):
        pytest.fail("entrypoint recreated or rescanned bundled data")

    monkeypatch.setattr(BundledAssetLoader, "__init__", forbidden)
    monkeypatch.setattr(BundledAssetLoader, "_load_json", forbidden)
    monkeypatch.setattr(rules, "_compute_binding", forbidden)
    for _ in range(3):
        assert asyncio.run(evaluate(request)) == expected
        assert query_action_availability(query) == available
        assert (
            evaluate_combat_initialization_candidate(init_request, loader=init_fixed) == initialized
        )
    assert rules.ruleset_binding() == fixed.binding
    bad = request.model_copy(
        update={"ruleset_binding": fixed.binding.model_copy(update={"evaluator_version": "wrong"})}
    )
    with pytest.raises(rules.RulesetBindingError):
        asyncio.run(evaluate(bad))
