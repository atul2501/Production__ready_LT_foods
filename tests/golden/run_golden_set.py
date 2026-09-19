"""Runs the full extraction pipeline against a labeled set of sample invoices and reports
per-field / per-status accuracy. This is what catches silent hallucination or prompt
regressions over time - run before releases, not on every commit (OCR+LLM inference cost).

Setup: populate tests/golden/samples/<vendor>_<n>.pdf with real sample invoices, and
tests/golden/expected/<same-stem>.json with the manually-verified expected extraction
(the exact invoice_header/line_items/additional_fields JSON, per the spec).

Usage: python -m tests.golden.run_golden_set
"""
import json
from pathlib import Path

from app.pipeline.run import run_pipeline

SAMPLES_DIR = Path(__file__).parent / "samples"
EXPECTED_DIR = Path(__file__).parent / "expected"


def _flatten(data, prefix: str = "") -> dict:
    flat: dict = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    flat.update(_flatten(item, f"{path}[{idx}]"))
                else:
                    flat[f"{path}[{idx}]"] = item
        else:
            flat[path] = value
    return flat


def main() -> None:
    samples = sorted(SAMPLES_DIR.glob("*.pdf"))
    if not samples:
        print(f"No sample PDFs found in {SAMPLES_DIR}. Populate this directory to run the golden set.")
        return

    total_fields = 0
    matched_fields = 0
    status_counts: dict[str, int] = {}

    for sample_path in samples:
        expected_path = EXPECTED_DIR / f"{sample_path.stem}.json"
        if not expected_path.exists():
            print(f"SKIP {sample_path.name}: no expected JSON at {expected_path}")
            continue

        expected = json.loads(expected_path.read_text())
        pdf_bytes = sample_path.read_bytes()

        try:
            result = run_pipeline(pdf_bytes, job_id=f"golden-{sample_path.stem}")
        except Exception as exc:  # noqa: BLE001 - a pipeline crash on a golden sample is itself a result to report
            print(f"FAIL {sample_path.name}: pipeline error: {exc}")
            status_counts["pipeline_error"] = status_counts.get("pipeline_error", 0) + 1
            continue

        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1

        actual_flat = _flatten(result["extraction"].model_dump())
        expected_flat = _flatten(expected)

        for field, expected_value in expected_flat.items():
            total_fields += 1
            actual_value = actual_flat.get(field)
            if actual_value == expected_value:
                matched_fields += 1
            else:
                print(f"  MISMATCH {sample_path.name} :: {field} expected={expected_value!r} actual={actual_value!r}")

    accuracy = (matched_fields / total_fields * 100) if total_fields else 0.0
    print("\n--- Golden Set Summary ---")
    print(f"Samples evaluated: {len(samples)}")
    print(f"Field accuracy: {matched_fields}/{total_fields} ({accuracy:.1f}%)")
    print(f"Status distribution: {status_counts}")


if __name__ == "__main__":
    main()
