"""Bind the effective typed corpus, including overlays, before evaluation."""

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from threading import Lock
from types import MappingProxyType
from typing import Annotated, Any, Literal

from dnd5e_srd_data.frozen import FrozenAssetLoader
from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader, Category
from dnd5e_srd_data.schema.item import Armor, Weapon
from pydantic import BaseModel, Field, model_validator

from dnd5e_engine.evaluation_base import EvaluationModel
from dnd5e_engine.turn_rules import ROUND_DURATION_SECONDS

EVALUATOR_VERSION = "dnd5e-evaluation/75"
RULESET_ID = "dnd-2024-srd-5.2.1"


class FixedSpellDuration(EvaluationModel):
    """Replace a reviewed spell duration with this many complete ruleset rounds."""

    rounds: int = Field(gt=0)


SpellDurationPolicy = Literal["canonical", "until_long_rest"] | FixedSpellDuration
DurationSpell = Literal["aid", "longstrider"]


class SpellDurationOverlay(EvaluationModel):
    """Set the default for covered reviewed spells, with per-spell precedence."""

    applies_to: tuple[DurationSpell, ...] = Field(min_length=1)
    minimum_seconds: int = Field(default=3600, ge=1)
    default: Literal["canonical", "until_long_rest"]
    overrides: dict[DurationSpell, SpellDurationPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope(self) -> "SpellDurationOverlay":
        """Reject ambiguous coverage and overrides outside that coverage."""
        if len(set(self.applies_to)) != len(self.applies_to):
            raise ValueError("spell duration scope contains duplicate spells")
        if not self.overrides.keys() <= set(self.applies_to):
            raise ValueError("spell duration override is outside its declared scope")
        return self


class RulesetOverlay(EvaluationModel):
    """Declare a versioned configuration and the base ruleset it supports."""

    schema_version: Literal["ruleset-overlay/1"]
    overlay_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    base_ruleset_id: Literal["dnd-2024-srd-5.2.1"]
    spell_durations: SpellDurationOverlay


class RulesetBinding(EvaluationModel):
    """Pin rules identity, effective content revision and evaluator semantics."""

    ruleset_id: Annotated[str, Field(min_length=1)]
    data_revision: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    evaluator_version: Annotated[str, Field(min_length=1)]


class RulesetBindingError(ValueError):
    """Configuration/input failure outside the four rule statuses."""


def _canonical(value: Any) -> Any:
    """Convert bound rules values to the canonical content representation."""
    if isinstance(value, BaseModel):
        return {key: _canonical(getattr(value, key)) for key in type(value).model_fields}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=_encode)
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _encode(value: Any) -> str:
    """Encode canonical rules content deterministically for hashing."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_bytes(value: Any) -> bytes:
    """Encode typed values for hashing, ordering maps and sets while preserving sequences."""
    return _encode(_canonical(value)).encode("utf-8")


def _compute_binding(loader: AssetLoader, overlay: RulesetOverlay | None = None) -> RulesetBinding:
    """Hash every effective category/slug and its actual typed content.

    Loaders must enumerate their entire effective corpus, including homebrew and
    overrides. Missing listed assets or duplicate identities are binding errors.
    This uncached compatibility implementation also computes each new snapshot's
    identity once. Mutable sources cannot retain an old valid binding.
    """
    categories: tuple[Category, ...] = (
        "items",
        "monsters",
        "spells",
        "species",
        "classes",
        "subclasses",
        "backgrounds",
        "feats",
        "features",
        "conditions",
        "traits",
    )
    methods = (
        loader.get_item,
        loader.get_monster,
        loader.get_spell,
        loader.get_species,
        loader.get_class,
        loader.get_subclass,
        loader.get_background,
        loader.get_feat,
        loader.get_feature,
        loader.get_condition,
        loader.get_trait,
    )
    corpus = []
    for category, getter in zip(categories, methods, strict=True):
        slugs = loader.list_slugs(category)
        if len(slugs) != len(set(slugs)):
            raise RulesetBindingError("duplicate effective asset identity")
        for slug in sorted(slugs):
            asset = getter(slug)
            if asset is None or asset.slug != slug:
                raise RulesetBindingError("effective loader enumeration is inconsistent")
            if category == "items":
                weapon = loader.get_weapon(slug)
                armor = loader.get_armor(slug)
                if (weapon != asset if isinstance(asset, Weapon) else weapon is not None) or (
                    armor != asset if isinstance(asset, Armor) else armor is not None
                ):
                    raise RulesetBindingError("effective typed item accessors disagree")
            corpus.append([category, slug, _canonical(asset)])
    bound_content = (
        corpus if overlay is None else {"assets": corpus, "overlay": _canonical(overlay)}
    )
    digest = hashlib.sha256(_encode(bound_content).encode("utf-8")).hexdigest()
    return RulesetBinding(
        ruleset_id=RULESET_ID, data_revision=f"sha256:{digest}", evaluator_version=EVALUATOR_VERSION
    )


class FrozenRuleset(FrozenAssetLoader):
    """A detached effective corpus and its locally computed, verified identity."""

    __slots__ = ("__binding", "__spell_durations")
    __binding: RulesetBinding
    __spell_durations: Mapping[str, SpellDurationPolicy]

    def __init__(self, source: AssetLoader, *, overlay: RulesetOverlay | None = None) -> None:
        """Capture a detached effective dataset and compute its trusted rules binding.

        Invalid corpus/identity input becomes RulesetBindingError before the binding is
        published. The base loader retains private parsed assets and returns defensive
        copies; freezing does not create a cache of action evaluation results.
        """
        try:
            super().__init__(source)
            if overlay is not None:
                overlay = RulesetOverlay.model_validate(overlay)
            durations = _admit_spell_durations(self, overlay)
            binding = _compute_binding(self) if overlay is None else _compute_binding(self, overlay)
        except ValueError as exc:
            raise RulesetBindingError(str(exc)) from exc
        object.__setattr__(self, "_FrozenRuleset__binding", binding)
        object.__setattr__(self, "_FrozenRuleset__spell_durations", durations)

    def spell_duration_policy(self, spell_id: str) -> SpellDurationPolicy:
        """Read a covered spell's effective policy from the detached snapshot."""
        return self.__spell_durations.get(spell_id, "canonical")

    def covers_spell_duration(self, spell_id: str) -> bool:
        """Identify the explicit duration scope even when a spell keeps its base clock."""
        return spell_id in self.__spell_durations

    @property
    def binding(self) -> RulesetBinding:
        """Return the fixed dataset's validated three-field rules binding."""
        return self.__binding

    @property
    def round_duration_seconds(self) -> int:
        """Return the D&D combat Round duration in game seconds for this evaluator."""
        return ROUND_DURATION_SECONDS


def _admit_spell_durations(
    loader: AssetLoader, overlay: RulesetOverlay | None
) -> Mapping[str, SpellDurationPolicy]:
    """Validate that every explicitly covered duration has a reviewed base spell."""
    if overlay is None:
        return MappingProxyType({})

    from dnd5e_engine.evaluation_world_effect import reviewed_duration_seconds

    policies: dict[str, SpellDurationPolicy] = {}
    config = overlay.spell_durations
    canonical = BundledAssetLoader()
    for spell_id in config.applies_to:
        spell = loader.get_spell(spell_id)
        if spell is None or spell != canonical.get_spell(spell_id) or spell.concentration:
            raise RulesetBindingError(f"duration overlay requires reviewed {spell_id}")
        seconds = reviewed_duration_seconds(spell)
        default = config.default if seconds >= config.minimum_seconds else "canonical"
        policies[spell_id] = config.overrides.get(spell_id, default)
    return MappingProxyType(policies)


def spell_duration_policy(loader: AssetLoader, spell_id: str) -> SpellDurationPolicy:
    """Read the policy only from a validated effective ruleset."""
    return loader.spell_duration_policy(spell_id) if type(loader) is FrozenRuleset else "canonical"


def freeze_ruleset(loader: AssetLoader, *, overlay: RulesetOverlay | None = None) -> FrozenRuleset:
    """Validate/copy a stable source once; retain and reuse the returned loader.

    Caller owns synchronization of mutable sources during construction. No
    per-source/session global cache is retained. Editing requires a new dataset.
    """
    if type(loader) is FrozenRuleset and overlay is None:
        return loader
    return FrozenRuleset(loader, overlay=overlay)


_DEFAULT_LOCK = Lock()
_DEFAULT_RULESET: FrozenRuleset | None = None


def get_default_ruleset() -> FrozenRuleset:
    """One process-wide bundled snapshot, published only after complete success.

    Failed construction leaves the slot empty and the next caller retries.
    Custom datasets are reference-owned and never occupy this slot.
    """
    global _DEFAULT_RULESET
    with _DEFAULT_LOCK:
        if _DEFAULT_RULESET is None:
            _DEFAULT_RULESET = freeze_ruleset(BundledAssetLoader())
        return _DEFAULT_RULESET


def ruleset_binding(loader: AssetLoader | None = None) -> RulesetBinding:
    """Compare fixed identities cheaply; live/custom loaders are fully rehashed.

    Only this module's exact concrete snapshot type is trusted, never a loader's
    self-declared revision or an object-address cache.
    """
    if loader is None:
        loader = get_default_ruleset()
    if type(loader) is FrozenRuleset:
        return loader.binding
    return _compute_binding(loader)


def verify_ruleset(binding: RulesetBinding, loader: AssetLoader) -> None:
    """Verify all binding fields, using trusted frozen identity or the mutable-loader
    safeguards.
    """
    if binding != ruleset_binding(loader):
        raise RulesetBindingError("request binding does not match the effective rules/evaluator")
