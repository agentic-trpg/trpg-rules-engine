"""Bind the effective typed corpus, including overlays, before evaluation."""

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from threading import Lock
from typing import Annotated, Any

from dnd5e_srd_data.frozen import FrozenAssetLoader
from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader, Category
from dnd5e_srd_data.schema.item import Armor, Weapon
from pydantic import BaseModel, Field

from dnd5e_engine.evaluation_base import EvaluationModel

EVALUATOR_VERSION = "dnd5e-evaluation/37"
RULESET_ID = "dnd-2024-srd-5.2.1"


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


def _compute_binding(loader: AssetLoader) -> RulesetBinding:
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
    digest = hashlib.sha256(_encode(corpus).encode("utf-8")).hexdigest()
    return RulesetBinding(
        ruleset_id=RULESET_ID, data_revision=f"sha256:{digest}", evaluator_version=EVALUATOR_VERSION
    )


class FrozenRuleset(FrozenAssetLoader):
    """A detached effective corpus and its locally computed, verified identity."""

    __slots__ = ("__binding",)
    __binding: RulesetBinding

    def __init__(self, source: AssetLoader) -> None:
        """Capture a detached effective dataset and compute its trusted rules binding.

        Invalid corpus/identity input becomes RulesetBindingError before the binding is
        published. The base loader retains private parsed assets and returns defensive
        copies; freezing does not create a cache of action evaluation results.
        """
        try:
            super().__init__(source)
            binding = _compute_binding(self)
        except ValueError as exc:
            raise RulesetBindingError(str(exc)) from exc
        object.__setattr__(self, "_FrozenRuleset__binding", binding)

    @property
    def binding(self) -> RulesetBinding:
        """Return the fixed dataset's validated three-field rules binding."""
        return self.__binding


def freeze_ruleset(loader: AssetLoader) -> FrozenRuleset:
    """Validate/copy a stable source once; retain and reuse the returned loader.

    Caller owns synchronization of mutable sources during construction. No
    per-source/session global cache is retained. Editing requires a new dataset.
    """
    return loader if type(loader) is FrozenRuleset else FrozenRuleset(loader)


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
