import json
import glob
import sys
import os
import sys

# Run from anywhere: this lives in scripts/, so the repo root is one level up.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da.intake.parser import JiraIntakeParser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def main():
    print("==================================================")
    print("[DA INTAKE & CLASSIFICATION PARSER TEST RUNNER]")
    print("==================================================\n")

    try:
        parser = JiraIntakeParser()
    except ValueError as err:
        print(f"[CONFIG ERROR] {err}")
        print("Please add your Groq API key to .env file: GROQ_API_KEY=gsk_...")
        return

    sample_files = glob.glob("src/da/samples/*.json") or glob.glob("src/da/intake/samples/*.json") or glob.glob("samples/raw_jira_payloads/*.json")

    if not sample_files:
        print("[ERROR] No sample JSON files found in samples/raw_jira_payloads/")
        return

    for filepath in sample_files:
        print(f"Processing: {filepath} ...")
        with open(filepath, "r", encoding="utf-8-sig") as f:
            raw_data = json.load(f)

        try:
            result = parser.parse_raw_jira_ticket(raw_data)
            print(f"  Gate A Status : {'PASS (DA Task)' if result.is_da_task else 'FAIL (Non-DA Task)'}")
            print(f"  Classification: {result.classification}")
            print(f"  Environment   : {result.environment}")
            print(f"  Target Repo   : {result.target_service_repo}")
            print(f"  Resource Type : {result.resource_type}")
            print(f"  Extracted Vars: {json.dumps(result.extracted_inputs, indent=2)}")
            print("-" * 50)
        except Exception as e:
            print(f"  Failed to parse {filepath}: {e}")
            print("-" * 50)

if __name__ == "__main__":
    main()
