"""Generate the runtime error catalog from cross-SDK policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "spec/shared/error-codes.json"
    target = root / "src/torn_sdk/_error_codes.py"
    data = json.loads(source.read_text(encoding="utf-8"))
    lines = [
        '"""Generated from spec/shared/error-codes.json. Do not edit."""',
        "",
        "from ._error_code_behavior import _ErrorCodeBehavior",
        "",
        "",
        "class TornErrorCode(_ErrorCodeBehavior):",
    ]
    lines.extend(f"    {entry['snake'].upper()} = {entry['code']}" for entry in data["codes"])
    lines.extend(("", "", "ERROR_POLICY = {"))
    for entry in data["codes"]:
        values = tuple(
            entry[key]
            for key in (
                "name",
                "snake",
                "message",
                "category",
                "action",
                "disable_key",
                "retryable",
            )
        )
        lines.append(f"    {entry['code']}: {values!r},")
    lines.extend(("}", ""))
    settings = data["retry"]
    for constant, field in (
        ("DEFAULT_MAX_ATTEMPTS", "max_attempts"),
        ("DEFAULT_BASE_DELAY_MS", "base_delay_ms"),
        ("DEFAULT_MAX_DELAY_MS", "max_delay_ms"),
        ("DEFAULT_RETRY_AFTER_CAP_MS", "retry_after_cap_ms"),
        ("HTTP_RETRY_STATUSES", "http_retry_statuses"),
    ):
        value = settings[field]
        formatted = f"frozenset({tuple(value)!r})" if isinstance(value, list) else repr(value)
        lines.append(f"{constant} = {formatted}")
    expected = ("\n".join(lines) + "\n").encode()
    if args.check:
        if not target.exists() or target.read_bytes() != expected:
            parser.error(f"generated file is out of date: {target.relative_to(root)}")
    else:
        target.write_bytes(expected)


if __name__ == "__main__":
    main()
