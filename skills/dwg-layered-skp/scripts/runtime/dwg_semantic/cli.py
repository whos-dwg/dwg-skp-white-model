from __future__ import annotations

import argparse
import json
from pathlib import Path

from .classifier import classify_document, load_rules
from .report import render_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify normalized DWG extraction JSON with reusable evidence rules.")
    parser.add_argument("input", type=Path, help="Normalized extraction JSON")
    parser.add_argument("--rules", type=Path, default=Path("config/default_rules.json"))
    parser.add_argument("--profile", type=Path, help="Explicit external per-project profile; never overrides the fixed modeling standard")
    parser.add_argument("--source-name", help="Human-readable source DWG name stored in the result")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    rules = load_rules(args.rules, profile=args.profile)
    result = classify_document(data, rules, source_name=args.source_name or args.input.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "validation": result["validation"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

