"""Validated corpus snapshots with defensive copies at every public read seam."""

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import TypeVar, cast

from pydantic import BaseModel

from dnd5e_srd_data.loader import AssetLoader, BundledAssetLoader, Category
from dnd5e_srd_data.schema.background import Background
from dnd5e_srd_data.schema.class_ import Class, Subclass
from dnd5e_srd_data.schema.condition import Condition
from dnd5e_srd_data.schema.feat import Feat
from dnd5e_srd_data.schema.feature import Feature
from dnd5e_srd_data.schema.item import Armor, Item, MagicItem, Weapon
from dnd5e_srd_data.schema.monster import Monster, MonsterTrait
from dnd5e_srd_data.schema.species import Species
from dnd5e_srd_data.schema.spell import Spell

_ASSETS: tuple[tuple[Category, str, type[BaseModel]], ...] = (
    ("items", "get_item", Item),
    ("monsters", "get_monster", Monster),
    ("spells", "get_spell", Spell),
    ("species", "get_species", Species),
    ("classes", "get_class", Class),
    ("subclasses", "get_subclass", Subclass),
    ("backgrounds", "get_background", Background),
    ("feats", "get_feat", Feat),
    ("features", "get_feature", Feature),
    ("conditions", "get_condition", Condition),
    ("traits", "get_trait", MonsterTrait),
)
_T = TypeVar("_T", bound=BaseModel)


def _capture(source: AssetLoader) -> Mapping[Category, Mapping[str, BaseModel]]:
    # Only the known bundled implementation gets a temporary parse cache.
    # Custom implementations/subclasses must run their own accessor checks.
    bundled = type(source) is BundledAssetLoader
    if type(source) is BundledAssetLoader:
        source = source._for_materialization()
    corpus: dict[Category, Mapping[str, BaseModel]] = {}
    for category, method, model in _ASSETS:
        slugs = source.list_slugs(category)
        if len(slugs) != len(set(slugs)):
            raise ValueError("duplicate effective asset identity")
        assets: dict[str, BaseModel] = {}
        for slug in sorted(slugs):
            asset = deepcopy(getattr(source, method)(slug))
            if not isinstance(asset, model) or getattr(asset, "slug", None) != slug:
                raise ValueError("effective loader enumeration is inconsistent")
            # A mutable model can have invalid fields assigned after creation.
            # Revalidate detached custom inputs without silently normalizing them.
            if (
                not bundled
                and type(asset).model_validate(asset.model_dump(mode="python", round_trip=True))
                != asset
            ):
                raise ValueError("effective asset contains invalid mutated fields")
            if category == "items":
                weapon, armor = source.get_weapon(slug), source.get_armor(slug)
                if (weapon != asset if isinstance(asset, Weapon) else weapon is not None) or (
                    armor != asset if isinstance(asset, Armor) else armor is not None
                ):
                    raise ValueError("effective typed item accessors disagree")
            assets[slug] = asset
        corpus[category] = MappingProxyType(assets)
    return MappingProxyType(corpus)


def _spell_uuid_index(spells: Mapping[str, BaseModel]) -> dict[str, str]:
    """Reject ambiguous identities before publishing any captured data or index."""
    index: dict[str, str] = {}
    for slug, spell in spells.items():
        if not isinstance(spell, Spell) or not spell.foundry_uuid:
            continue
        uuid = spell.foundry_uuid
        previous = index.get(uuid)
        if previous is not None:
            raise ValueError(
                f"duplicate effective spell UUID {uuid!r}: "
                f"conflicting slugs {previous!r} and {slug!r}"
            )
        index[uuid] = slug
    return index


class FrozenAssetLoader:
    """Materialize one effective corpus, retaining no reference to its source.

    Getters return deep copies of private parsed models, including nested model,
    list and dictionary fields. Consumers may edit these independent candidates.
    The source must be stable for the duration of construction. Construction
    failure publishes nothing; retry by constructing again after fixing it.
    """

    __slots__ = ("__corpus", "__spell_uuids", "__weakref__")
    __corpus: Mapping[Category, Mapping[str, BaseModel]]
    __spell_uuids: Mapping[str, str]

    def __init__(self, source: AssetLoader) -> None:
        if hasattr(self, "_FrozenAssetLoader__corpus"):
            raise AttributeError("fixed datasets cannot be initialized again")
        corpus = _capture(source)
        uuids = _spell_uuid_index(corpus["spells"])
        object.__setattr__(self, "_FrozenAssetLoader__corpus", corpus)
        object.__setattr__(self, "_FrozenAssetLoader__spell_uuids", MappingProxyType(uuids))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("fixed assets cannot be replaced; construct a new dataset")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("fixed assets cannot be removed")

    def _get(self, category: Category, slug: str, model: type[_T]) -> _T | None:
        asset = self.__corpus[category].get(slug)
        return deepcopy(asset) if isinstance(asset, model) else None

    def get_item(self, slug: str) -> Item | Weapon | Armor | MagicItem | None:
        return self._get("items", slug, Item)

    def get_weapon(self, slug: str) -> Weapon | None:
        return self._get("items", slug, Weapon)

    def get_armor(self, slug: str) -> Armor | None:
        return self._get("items", slug, Armor)

    def get_monster(self, slug: str) -> Monster | None:
        return self._get("monsters", slug, Monster)

    def get_spell(self, slug: str) -> Spell | None:
        return self._get("spells", slug, Spell)

    def get_spell_by_uuid(self, uuid: str) -> Spell | None:
        slug = self.__spell_uuids.get(uuid)
        return self.get_spell(slug) if slug is not None else None

    def get_species(self, slug: str) -> Species | None:
        return self._get("species", slug, Species)

    def get_class(self, slug: str) -> Class | None:
        return self._get("classes", slug, Class)

    def get_subclass(self, slug: str) -> Subclass | None:
        return self._get("subclasses", slug, Subclass)

    def get_background(self, slug: str) -> Background | None:
        return self._get("backgrounds", slug, Background)

    def get_feat(self, slug: str) -> Feat | None:
        return self._get("feats", slug, Feat)

    def get_feature(self, slug: str) -> Feature | None:
        return self._get("features", slug, Feature)

    def get_condition(self, slug: str) -> Condition | None:
        return self._get("conditions", slug, Condition)

    def get_trait(self, slug: str) -> MonsterTrait | None:
        return self._get("traits", slug, MonsterTrait)

    def list_conditions(self) -> list[str]:
        return self.list_slugs("conditions")

    def list_slugs(self, category: Category) -> list[str]:
        return sorted(self.__corpus.get(category, {}))

    def __contains__(self, key: object) -> bool:
        if not (isinstance(key, tuple) and len(key) == 2):
            return False
        category, slug = key
        return slug in self.__corpus.get(cast("Category", category), {})
