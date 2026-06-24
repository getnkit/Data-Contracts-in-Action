import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


# Default paths used when the user does not pass arguments from CLI.
DEFAULT_CONTRACT = "contracts/customer_profile.contract.json"
DEFAULT_SCHEMA = "actual_schemas/customer_profile.actual.ok.json"
DEFAULT_INPUT = "samples/customer_profile_input.csv"
DEFAULT_OUTPUT_DIR = "output"


def load_json(path):
    # Load a JSON file such as contract spec or actual schema.
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def contract_columns(contract):
    # Convert contract columns from list to dictionary.
    # This lets us find a column directly by name, for example expected["customer_id"].
    return {column["name"]: column for column in contract.get("schema", {}).get("columns", [])}


def actual_columns(schema):
    # Convert actual schema columns from list to dictionary for the same reason.
    return {column["name"]: column for column in schema.get("columns", [])}


def check_schema(contract, actual_schema):
    # Compare expected schema from the contract with actual schema from the source.
    expected = contract_columns(contract)
    actual = actual_columns(actual_schema)

    errors = []
    warnings = []

    for column_name, expected_col in expected.items():
        actual_col = actual.get(column_name)

        # Required column missing = breaking issue.
        # Optional column missing = warning only.
        if actual_col is None:
            if expected_col.get("required", False):  # Check if this column is required; default to False if not specified.
                errors.append(f"missing required column: {column_name}")
            else:
                warnings.append(f"missing optional column: {column_name}")
            continue

        # Type mismatch can break downstream logic.
        if expected_col.get("type") != actual_col.get("type"):
            errors.append(
                f"type mismatch for {column_name}: "
                f"expected {expected_col.get('type')}, actual {actual_col.get('type')}"
            )

        # Required column should not become nullable in actual schema.
        if expected_col.get("required", False) and actual_col.get("nullable") is True:
            errors.append(f"required column became nullable: {column_name}")

    # Extra columns are warnings because adding columns is often non-breaking
    # when downstream consumers can safely ignore them.
    for column_name in actual:
        if column_name not in expected:
            warnings.append(f"extra column in actual schema: {column_name}")

    return errors, warnings


def validate_record(row, contract):  # row is a dictionary representing a single data record, contract is the contract specification.
    # Validate one data record against simple rules from the contract.
    # This demo checks:
    # - required fields
    # - allowed values
    violations = []

    for column in contract.get("schema", {}).get("columns", []):
        column_name = column["name"]
        value = (row.get(column_name) or "").strip()

        if column.get("required", False) and not value:  # Check if this column is required and the value is empty or missing.
            violations.append(f"{column_name} is required")
            continue  # Skip further checks for this column if it's required and missing.

        allowed_values = column.get("allowed_values")

        if allowed_values and value and value not in allowed_values:  # Check if allowed_values is defined and the value is not empty and not in the allowed values.
            violations.append(f"{column_name} must be one of {allowed_values}, got {value}")

    return violations


def write_csv(path, rows, fieldnames):
    # Create parent folder if it does not exist.
    # Example: output/published/ or output/quarantine/
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)  # Use the provided fieldnames to ensure consistent column order in the output CSV.
        writer.writeheader()  # Write the header row to the CSV file.
        writer.writerows(rows)  # Write all the rows to the CSV file.


def run_data_validate(contract_path, input_path, output_dir):
    # Runtime validation demo:
    # read input data -> validate -> publish / quarantine -> audit log
    contract = load_json(contract_path)

    with open(input_path, "r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        input_fields = reader.fieldnames or []

    valid_rows = []
    invalid_rows = []

    # Counter summarizes how many times each violation happens.
    # This is useful for audit log / monitoring.
    violation_counter = Counter()

    for row in rows:
        violations = validate_record(row, contract)

        if violations:
            invalid_row = dict(row)  # Create a copy of the row to avoid modifying the original data.
            invalid_row["violation_reason"] = "; ".join(violations)  # Concatenate all violation messages into a single string for easier logging and reporting.
            invalid_rows.append(invalid_row)
            violation_counter.update(violations)  # Update the counter with the list of violations for this row, which will increment the count for each violation type.
        else:
            valid_rows.append(row)

    output = Path(output_dir)
    asset_name = contract.get("name", "data_asset")  # Use the contract name as the asset name, defaulting to "data_asset" if not specified.

    # Publish valid records.
    write_csv(output / "published" / f"{asset_name}.csv", valid_rows, input_fields)

    # Send invalid records to quarantine with violation reason.
    write_csv(
        output / "quarantine" / f"{asset_name}_invalid.csv",
        invalid_rows,
        input_fields + ["violation_reason"],
    )

    # Write a small audit log for this validation run.
    audit_log = {
        "contract_id": contract.get("contract_id"),
        "contract_version": contract.get("version"),
        "asset_name": asset_name,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "input_record_count": len(rows),
        "valid_record_count": len(valid_rows),
        "invalid_record_count": len(invalid_rows),
        "violation_summary": dict(violation_counter),  # Convert Counter to a regular dictionary for JSON serialization.
    }

    audit_path = output / "audit" / "validation_log.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    with open(audit_path, "w", encoding="utf-8") as file:
        json.dump(audit_log, file, indent=2, ensure_ascii=False)

    print("Data validation completed")
    print(f"Published records: {len(valid_rows)}")
    print(f"Quarantined records: {len(invalid_rows)}")
    print(f"Audit log: {audit_path}")

    return 0


def run_schema_check(contract_path, schema_path):
    # Compare contract schema with actual source schema.
    contract = load_json(contract_path)
    actual_schema = load_json(schema_path)

    errors, warnings = check_schema(contract, actual_schema)

    for warning in warnings:
        print(f"WARNING: {warning}")

    if errors:
        print("Schema check failed")
        for error in errors:
            print(f"ERROR: {error}")

        # Return 1 to make the command fail.
        # GitHub Actions uses this exit code to fail the workflow.
        return 1

    print("Schema check passed")
    return 0


def build_parser():
    # argparse creates a small CLI with two commands:
    # - schema-check   : check contract against actual schema
    # - data-validate  : validate records and write publish/quarantine/audit output
    parser = argparse.ArgumentParser(description="Simple Data Contract demo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema_parser = subparsers.add_parser("schema-check")
    schema_parser.add_argument("--contract", default=DEFAULT_CONTRACT)
    schema_parser.add_argument("--schema", default=DEFAULT_SCHEMA)

    data_parser = subparsers.add_parser("data-validate")
    data_parser.add_argument("--contract", default=DEFAULT_CONTRACT)
    data_parser.add_argument("--input", default=DEFAULT_INPUT)
    data_parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)

    return parser


# Main function that parses command-line arguments and executes the appropriate function based on the command.
def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "schema-check":
        return run_schema_check(args.contract, args.schema)

    if args.command == "data-validate":
        return run_data_validate(args.contract, args.input, args.output_dir)

    parser.print_help()
    return 1


# Entry point for the script. When the script is run directly, it will execute the main() function and exit with the returned status code.
if __name__ == "__main__":
    sys.exit(main())
