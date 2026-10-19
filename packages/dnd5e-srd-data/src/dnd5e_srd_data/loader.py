"""AssetLoader Protocol + in-memory implementation.

Production `BundledAssetLoader` lives below; tests use `MemoryAssetLoader`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import copy
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel

from dnd5e_srd_data.schema.background import Background
from dnd5e_srd_data.schema.class_ import Class, Subclass
from dnd5e_srd_data.schema.condition import Condition
from dnd5e_srd_data.schema.feat import Feat
from dnd5e_srd_data.schema.feature import Feature
from dnd5e_srd_data.schema.item import Armor, Item, MagicItem, Weapon
from dnd5e_srd_data.schema.monster import Monster, MonsterTrait
from dnd5e_srd_data.schema.species import Species
from dnd5e_srd_data.schema.spell import Spell

Category = Literal[
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
]

_CATEGORIES: tuple[str, ...] = (
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


@runtime_checkable
class AssetLoader(Protocol):
    def get_item(self, slug: str) -> Item | Weapon | Armor | MagicItem | None: ...
    def get_weapon(self, slug: str) -> Weapon | None: ...
    def get_armor(self, slug: str) -> Armor | None: ...
    def get_monster(self, slug: str) -> Monster | None: ...
    def get_spell(self, slug: str) -> Spell | None: ...
    def get_spell_by_uuid(self, uuid: str) -> Spell | None: ...
    def get_species(self, slug: str) -> Species | None: ...
    def get_class(self, slug: str) -> Class | None: ...
    def get_subclass(self, slug: str) -> Subclass | None: ...
    def get_background(self, slug: str) -> Background | None: ...
    def get_feat(self, slug: str) -> Feat | None: ...
    def get_feature(self, slug: str) -> Feature | None: ...
    def get_condition(self, slug: str) -> Condition | None: ...
    def list_conditions(self) -> list[str]: ...
    def get_trait(self, slug: str) -> MonsterTrait | None: ...
    def list_slugs(self, category: Category) -> list[str]: ...
    def __contains__(self, key: tuple[Category, str]) -> bool: ...


class MemoryAssetLoader:
    """In-memory loader for tests. Pass entities at construction."""

    def __init__(
        self,
        *,
        items: Sequence[Item | Weapon | Armor | MagicItem] = (),
        monsters: Sequence[Monster] = (),
        spells: Sequence[Spell] = (),
        species: Sequence[Species] = (),
        classes: Sequence[Class] = (),
        subclasses: Sequence[Subclass] = (),
        backgrounds: Sequence[Background] = (),
        feats: Sequence[Feat] = (),
        features: Sequence[Feature] = (),
        conditions: Sequence[Condition] = (),
        traits: Sequence[MonsterTrait] = (),
    ) -> None:
        self._items: dict[str, Item | Weapon | Armor | MagicItem] = {i.slug: i for i in items}
        self._monsters: dict[str, Monster] = {m.slug: m for m in monsters}
        self._spells: dict[str, Spell] = {s.slug: s for s in spells}
        self._species: dict[str, Species] = {sp.slug: sp for sp in species}
        self._classes: dict[str, Class] = {c.slug: c for c in classes}
        self._subclasses: dict[str, Subclass] = {s.slug: s for s in subclasses}
        self._backgrounds: dict[str, Background] = {b.slug: b for b in backgrounds}
        self._feats: dict[str, Feat] = {f.slug: f for f in feats}
        self._features: dict[str, Feature] = {ft.slug: ft for ft in features}
        self._conditions: dict[str, Condition] = {c.slug: c for c in conditions}
        self._traits: dict[str, MonsterTrait] = {t.slug: t for t in traits}

    def get_item(self, slug: str) -> Item | Weapon | Armor | MagicItem | None:
        return self._items.get(slug)

    def get_weapon(self, slug: str) -> Weapon | None:
        candidate = self._items.get(slug)
        return candidate if isinstance(candidate, Weapon) else None

    def get_armor(self, slug: str) -> Armor | None:
        candidate = self._items.get(slug)
        return candidate if isinstance(candidate, Armor) else None

    def get_monster(self, slug: str) -> Monster | None:
        return self._monsters.get(slug)

    def get_spell(self, slug: str) -> Spell | None:
        return self._spells.get(slug)

    def get_spell_by_uuid(self, uuid: str) -> Spell | None:
        if not uuid:
            return None
        for slug in self.list_slugs("spells"):
            spell = self.get_spell(slug)
            if spell is not None and spell.foundry_uuid == uuid:
                return spell
        return None

    def get_species(self, slug: str) -> Species | None:
        return self._species.get(slug)

    def get_class(self, slug: str) -> Class | None:
        return self._classes.get(slug)

    def get_subclass(self, slug: str) -> Subclass | None:
        return self._subclasses.get(slug)

    def get_background(self, slug: str) -> Background | None:
        return self._backgrounds.get(slug)

    def get_feat(self, slug: str) -> Feat | None:
        return self._feats.get(slug)

    def get_feature(self, slug: str) -> Feature | None:
        return self._features.get(slug)

    def get_condition(self, slug: str) -> Condition | None:
        return self._conditions.get(slug)

    def list_conditions(self) -> list[str]:
        return sorted(self._conditions)

    def get_trait(self, slug: str) -> MonsterTrait | None:
        return self._traits.get(slug)

    def list_slugs(self, category: Category) -> list[str]:
        by_category: dict[str, Mapping[str, object]] = {
            "items": self._items,
            "monsters": self._monsters,
            "spells": self._spells,
            "species": self._species,
            "classes": self._classes,
            "subclasses": self._subclasses,
            "backgrounds": self._backgrounds,
            "feats": self._feats,
            "features": self._features,
            "conditions": self._conditions,
            "traits": self._traits,
        }
        return sorted(by_category.get(category, {}))

    def __contains__(self, key: object) -> bool:
        if not (isinstance(key, tuple) and len(key) == 2):
            return False
        category, slug = key
        if category not in _CATEGORIES:
            return False
        return slug in self.list_slugs(category)


_Model = TypeVar("_Model", bound=BaseModel)


class BundledAssetLoader:
    """Reads canonical/<category>/<slug>.json on demand. Lazy per-slug.

    Production: constructed with no args; reads packaged ``canonical/``.
    Tests: pass an explicit ``root`` pointing at a fixture tree.
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._root: Path | None = root
        self._spell_uuid_index: dict[str, str] | None = None
        self._models: dict[tuple[Category, str], BaseModel | None] | None = None

    def _for_materialization(self) -> BundledAssetLoader:
        """Private short-lived parse cache; the public live loader stays uncached."""
        result = copy(self)
        result._models = {}
        result._spell_uuid_index = None
        return result

    def _model(self, category: Category, slug: str, model: type[_Model]) -> _Model | None:
        key = (category, slug)
        if self._models is not None and key in self._models:
            return cast("_Model | None", self._models[key])
        raw = self._load_json(category, slug)
        result = None
        if raw is not None:
            if category == "items":
                item_models: dict[str, type[Item]] = {
                    "weapon": Weapon,
                    "armor": Armor,
                    "magic_item": MagicItem,
                }
                model = cast("type[_Model]", item_models.get(raw.get("item_kind", "item"), Item))
            result = model.model_validate(raw)
        if self._models is not None:
            self._models[key] = result
        return result

    def _category_dir(self, category: Category) -> Path:
        if self._root is not None:
            return self._root / category
        # importlib.resources path for packaged canonical/
        return Path(str(resources.files("dnd5e_srd_data") / "canonical" / category))

    def _load_json(self, category: Category, slug: str) -> dict[str, Any] | None:
        path = self._category_dir(category) / f"{slug}.json"
        if not path.is_file():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return None
        return loaded

    def get_item(self, slug: str) -> Item | Weapon | Armor | MagicItem | None:
        return self._model("items", slug, Item)

    def get_weapon(self, slug: str) -> Weapon | None:
        candidate = self.get_item(slug)
        return candidate if isinstance(candidate, Weapon) else None

    def get_armor(self, slug: str) -> Armor | None:
        candidate = self.get_item(slug)
        return candidate if isinstance(candidate, Armor) else None

    def get_monster(self, slug: str) -> Monster | None:
        return self._model("monsters", slug, Monster)

    def get_spell(self, slug: str) -> Spell | None:
        return self._model("spells", slug, Spell)

    def get_spell_by_uuid(self, uuid: str) -> Spell | None:
        if not uuid:
            return None
        if self._spell_uuid_index is None:
            index: dict[str, str] = {}
            for candidate_slug in self.list_slugs("spells"):
                spell = self.get_spell(candidate_slug)
                if spell is not None and spell.foundry_uuid:
                    index[spell.foundry_uuid] = candidate_slug
            self._spell_uuid_index = index
        resolved_slug = self._spell_uuid_index.get(uuid)
        return self.get_spell(resolved_slug) if resolved_slug is not None else None

    def get_species(self, slug: str) -> Species | None:
        return self._model("species", slug, Species)

    def get_class(self, slug: str) -> Class | None:
        return self._model("classes", slug, Class)

    def get_subclass(self, slug: str) -> Subclass | None:
        return self._model("subclasses", slug, Subclass)

    def get_background(self, slug: str) -> Background | None:
        return self._model("backgrounds", slug, Background)

    def get_feat(self, slug: str) -> Feat | None:
        return self._model("feats", slug, Feat)

    def get_feature(self, slug: str) -> Feature | None:
        return self._model("features", slug, Feature)

    def get_condition(self, slug: str) -> Condition | None:
        return self._model("conditions", slug, Condition)

    def list_conditions(self) -> list[str]:
        return self.list_slugs("conditions")

    def get_trait(self, slug: str) -> MonsterTrait | None:
        return self._model("traits", slug, MonsterTrait)

    def list_slugs(self, category: Category) -> list[str]:
        d = self._category_dir(category)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def __contains__(self, key: object) -> bool:
        if not (isinstance(key, tuple) and len(key) == 2):
            return False
        category, slug = key
        if category not in _CATEGORIES:
            return False
        return slug in self.list_slugs(category)
