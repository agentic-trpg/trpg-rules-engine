from dnd5e_srd_data.loader import MemoryAssetLoader

from dnd5e_engine import lib_loader
from dnd5e_engine.evaluation_ruleset import get_default_ruleset


def test_default_is_bundled_loader():
    lib_loader.set_lib_loader_for_tests(None)
    loader = lib_loader.get_lib_loader()
    assert loader is get_default_ruleset()
    assert loader.get_spell("magic-missile") is not None


def test_set_for_tests_overrides_singleton():
    mem = MemoryAssetLoader()
    lib_loader.set_lib_loader_for_tests(mem)
    assert lib_loader.get_lib_loader() is mem
    lib_loader.set_lib_loader_for_tests(None)
    assert lib_loader.get_lib_loader() is get_default_ruleset()
