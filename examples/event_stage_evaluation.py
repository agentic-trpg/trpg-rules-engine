"""Evaluate one explicit /24 request once, or export the current local schemas.

uv run python examples/event_stage_evaluation.py examples/event_stage_request.json
uv run python examples/event_stage_evaluation.py --schema-dir <output-directory>

This is a local candidate demo, not SM Evidence, a commit adapter or a second
stage evaluator. The serialized request must match the bundled effective corpus.
"""

import argparse
import asyncio
import json
from pathlib import Path

from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?", type=Path)
    parser.add_argument("--schema-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.schema_dir is not None:
        args.schema_dir.mkdir(parents=True, exist_ok=True)
        for name, model in (("request", RuleEvaluationRequest), ("result", RuleEvaluationResult)):
            (args.schema_dir / f"{name}.schema.json").write_text(
                json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    if args.request is None:
        if args.schema_dir is None:
            parser.error("provide a request or --schema-dir")
        return
    request = RuleEvaluationRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    result = asyncio.run(evaluate(request))
    # A consumer must perform the same binding/Delta/stage verification after transport.
    RuleEvaluationResult.model_validate_json(result.model_dump_json()).verify_request(request)
    encoded = result.model_dump_json(indent=2)
    if args.output is None:
        print(encoded)
    else:
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
