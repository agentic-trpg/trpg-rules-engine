"""The published matrix must account for every current Engine input."""

import re
from pathlib import Path
from typing import get_args

from dnd5e_engine import PlayerIntent
from dnd5e_engine.events import IntentType

from nat20_bridge.app import create_app
from nat20_bridge.state import BridgeState


def test_matrix_and_openapi_account_for_all_fields_and_intents(tmp_path):
    text = (Path(__file__).resolve().parents[3] / "docs/dev/bridge-intent-parity.md").read_text(
        encoding="utf-8"
    )
    intents, fields = text.split("## Every input field")
    assert set(re.findall(r"\| `([^`]+)` \|", intents)) == set(get_args(IntentType))
    field_table = fields.split("## Discovery")[0]
    assert set(re.findall(r"\| `([^`]+)` \|", field_table)) == set(PlayerIntent.model_fields)
    schema = create_app(BridgeState(homebrew_path=tmp_path / "homebrew.json")).openapi()
    intent = schema["components"]["schemas"]["IntentRequest"]
    assert set(intent["properties"]) == set(PlayerIntent.model_fields) | {"actor_id", "request_id"}
    assert intent["additionalProperties"] is False
