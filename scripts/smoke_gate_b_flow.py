import json
import os
import sys
import os
import sys

# Run from anywhere: this lives in scripts/, so the repo root is one level up.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.da.intake.parser import JiraIntakeParser
from src.da.classify.router import ClassificationRouter
from src.da.section_a_iac.gate_b import GateBValidator
from src.da.section_a_iac.hitl.hitl1_intake_handoff import HITL1HandoffHandler
from src.da.section_a_iac.hitl.hitl2_basic_iac import HITL2BasicIaCHandler
from src.da.section_a_iac.hitl.confirm_provisioning import ProvisioningConfirmationHandler
from src.da.section_a_iac.module_discovery import ModuleDiscoveryEngine
from src.da.section_a_iac.usage_discovery import UsageDiscoveryEngine

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def resolve_filepath(input_str: str) -> str:
    cleaned = input_str.strip().strip("'").strip('"')
    if os.path.exists(cleaned):
        return cleaned

    if not cleaned.endswith(".json"):
        cleaned += ".json"

    search_paths = [
        os.path.join("src", "da", "samples", cleaned),
        os.path.join("src", "da", "intake", "samples", cleaned),
        os.path.join("samples", "raw_jira_payloads", cleaned),
    ]
    for p in search_paths:
        if os.path.exists(p):
            return p

    return cleaned

def main():
    print("\n" + "="*60)
    print("🧠 [CHAIN OF THOUGHT] DA INTAKE & GATE B PIPELINE RUNNER")
    print("="*60 + "\n")

    user_file_input = input("👉 Enter Jira payload file name or path (e.g. DEVOPS-111.json or DEVOPS-444.json): ").strip()

    if not user_file_input:
        print("[ERROR] No file provided. Exiting.")
        return

    filepath = resolve_filepath(user_file_input)

    if not os.path.exists(filepath):
        print(f"\n❌ [FILE NOT FOUND] Could not locate file: '{filepath}'")
        print("Available samples in src/da/samples/:")
        sample_dir = os.path.join("src", "da", "samples")
        if os.path.exists(sample_dir):
            for f in os.listdir(sample_dir):
                if f.endswith(".json"):
                    print(f"    {f}")
        return

    print(f"\n📂 Loading Payload: {filepath}")
    with open(filepath, "r", encoding="utf-8-sig") as f:
        raw_data = json.load(f)

    # ----------------------------------------------------
    # STAGE 1: INTAKE & GATE A CHECK
    # ----------------------------------------------------
    print("\n" + "-"*60)
    print("1️⃣ [STAGE 1: INTAKE & GATE A CHECK]")
    print("-" * 60)

    try:
        parser = JiraIntakeParser()
        intake_output = parser.parse_raw_jira_ticket(raw_data)
    except Exception as e:
        print(f"❌ [PARSER ERROR] Failed to parse ticket: {e}")
        return

    ticket_id = intake_output.ticket_metadata.ticket_id if intake_output.ticket_metadata else "UNKNOWN"
    print(f"    Raw Ticket Key  : {ticket_id}")
    print(f"    Gate A Intent   : {'PASS (DA Task)' if intake_output.is_da_task else 'FAIL (Non-DA Task)'}")

    if not intake_output.is_da_task:
        print("\n" + "-"*60)
        print("🚨 [HITL 1: GATE A NON-DA TASK HANDOFF TO SUPERVISOR AGENT]")
        print("-" * 60)
        handoff_msg = HITL1HandoffHandler.handle_non_da_task(intake_output)
        print(f"    Status       : {handoff_msg.status}")
        print(f"    Target Agent : {handoff_msg.target_agent}")
        print(f"    Ticket Key   : {handoff_msg.ticket_id}")
        print(f"    Reason       : {handoff_msg.rejection_reason}")
        print(f"    Notification : {handoff_msg.message}")
        print("\n==================================================")
        print("🛑 [EXECUTION PAUSED / HANDED OFF TO SUPERVISOR AGENT]")
        print("This ticket is Out-of-Scope for Deployment Agent (DA).")
        print("Supervisor Agent has been notified of the out-of-scope status.")
        print("==================================================\n")
        return

    # ----------------------------------------------------
    # STAGE 2: CLASSIFY ROUTER
    # ----------------------------------------------------
    print("\n" + "-"*60)
    print("2️⃣ [STAGE 2: CLASSIFICATION ROUTER]")
    print("-" * 60)
    
    try:
        classification = ClassificationRouter.route_request(intake_output)
        print(f"    Routed to Classification : '{classification}'")
    except ValueError as val_err:
        print(f"❌ [ROUTER ERROR] {val_err}")
        return

    # ----------------------------------------------------
    # STAGE 3: SECTION A (IaC) GATE B BASELINE CHECK
    # ----------------------------------------------------
    print("\n" + "-"*60)
    print("3️⃣ [SECTION A (IaC): GATE B BASELINE CHECK]")
    print("-" * 60)

    initial_validation = GateBValidator.validate_iac_request(intake_output)

    print("    Inspecting 3 Mandatory IaC Baseline Fields:")

    env_valid = intake_output.environment and intake_output.environment.lower() in GateBValidator.ALLOWED_ENVIRONMENTS
    print(f"    1. Environment          : '{intake_output.environment}' [{'VALID' if env_valid else 'INVALID/MISSING'}]")

    repo_valid = bool(intake_output.target_service_repo and intake_output.target_service_repo.strip())
    print(f"    2. Target Service Repo  : '{intake_output.target_service_repo}' [{'VALID' if repo_valid else 'MISSING'}]")

    type_valid = bool(intake_output.resource_type and intake_output.resource_type.strip())
    print(f"    3. Resource Type        : '{intake_output.resource_type}' [{'VALID' if type_valid else 'MISSING'}]")

    # resource_prefix is reported for information only -- it is not a Gate B
    # requirement (M8); the name-carrying variable is discovered at Step 4a.
    print(f"    -- Resource Name Prefix : '{intake_output.resource_prefix}' [informational, not required]")

    if initial_validation.is_valid:
        print("\n  ✅ [GATE B STATUS: PASSED] All 3 IaC baseline fields are present and valid.")
    else:
        print(f"\n  ⚠️ [GATE B STATUS: FAILED] Missing fields: {', '.join(initial_validation.missing_fields)}")
        print("    Triggering HITL 2 Loop to collect missing baseline fields...")

    # ----------------------------------------------------
    # STAGE 4: HITL 2 LOOP (If Gate B failed)
    # ----------------------------------------------------
    validation_result = HITL2BasicIaCHandler.resolve_missing_fields(intake_output)

    if not validation_result.is_valid:
        print(f"\n🛑 [HALTED AT HITL 2] {validation_result.message}")
        return

    # ----------------------------------------------------
    # STAGE 5: STEP 2b SINGLE RESOURCE PROVISIONING CONFIRMATION
    # ----------------------------------------------------
    print("\n" + "-"*60)
    print("4️⃣ [STEP 2b SINGLE RESOURCE PROVISIONING CONFIRMATION]")
    print("-" * 60)

    confirmed = ProvisioningConfirmationHandler.confirm_single_resource_request(validation_result.validated_payload)

    if confirmed:
        print("\n==================================================")
        print("✅ [SCOPE CONFIRMED BY USER]")
        print("Proceeding to Step 3a: Central Module Registry Discovery & Pinned Ref Verification...")
        print("==================================================\n")

        # ----------------------------------------------------
        # STAGE 6: STEP 3a CENTRAL MODULE REGISTRY DISCOVERY
        # ----------------------------------------------------
        print("\n" + "-"*60)
        print("5️⃣ [STEP 3a: CENTRAL MODULE REGISTRY DISCOVERY & PINNED REF VERIFICATION]")
        print("-" * 60)

        discovery_engine = ModuleDiscoveryEngine()
        discovery_result = discovery_engine.discover_module_in_central_registry(
            validation_result.validated_payload
        )

        if not discovery_result.found:
            print(f"\n{discovery_result.message}")
            if discovery_result.case_mismatches:
                print(f"    Case-only near-misses : {', '.join(discovery_result.case_mismatches)}")
            if discovery_result.gap_entry:
                print(f"    Gap Entry Logged : ticket_id={discovery_result.gap_entry.ticket_id}, resource_type={discovery_result.gap_entry.requested_resource_type}")
                print(f"    Timestamp        : {discovery_result.gap_entry.timestamp}")
            print("\n==================================================")
            print("🛑 [EXECUTION PAUSED / MODULE NOT FOUND IN CENTRAL REGISTRY]")
            print("==================================================\n")
            return

        print(f"\n    Status          : FOUND in Central Module Registry")
        print(f"    Central Repo    : {discovery_result.central_repo_name}")
        print(f"    Module Rel Path : {discovery_result.module_rel_path}")
        print(f"    Pinned Ref      : {discovery_result.module_ref} (from common.hcl)")
        print(f"    Git Source URL  : {discovery_result.git_source_url}")
        if discovery_result.temp_var_file_path:
            rel_temp_path = os.path.relpath(discovery_result.temp_var_file_path, os.getcwd())
            print(f"    Temp Var Storage: {rel_temp_path}")

        print("\n==================================================")
        print("✅ [STEP 3a COMPLETED SUCCESSFULLY]")
        print("Proceeding to Step 3b: Target Service Repo & Usage Discovery...")
        print("==================================================\n")

        # ----------------------------------------------------
        # STAGE 7: STEP 3b TARGET SERVICE REPO USAGE DISCOVERY
        # ----------------------------------------------------
        print("\n" + "-"*60)
        print("6️⃣ [STEP 3b: TARGET SERVICE REPO & USAGE DISCOVERY]")
        print("-" * 60)

        usage_engine = UsageDiscoveryEngine()
        usage_result = usage_engine.discover_usage_in_target_service_repo(
            validation_result.validated_payload
        )

        if not usage_result.target_repo_exists:
            print(f"\n{usage_result.message}")
            print("\n==================================================")
            print("🛑 [EXECUTION PAUSED / TARGET REPO NOT FOUND]")
            print("==================================================\n")
            return

        if not usage_result.target_branch_exists:
            print(f"\n{usage_result.message}")
            print("\n==================================================")
            print("🛑 [EXECUTION PAUSED / TARGET BRANCH NOT FOUND]")
            print("==================================================\n")
            return

        if usage_result.is_duplicate_conflict:
            print(f"\n{usage_result.message}")
            print("    Triggering HITL Duplicate Conflict Alert...")
            print("    [HITL PROMPT]: A resource with the same prefix already exists on this target branch.")
            print("    [1] Stop Execution (Prevent overwrite)")
            print("    [2] Provide a unique new resource_prefix & folder name")
            print("\n==================================================")
            print("🚨 [EXECUTION PAUSED AT HITL DUPLICATE CONFLICT]")
            print("==================================================\n")
            return

        print(f"\n    Target Repo Check : VALIDATED ({validation_result.validated_payload.target_service_repo})")
        print(f"    Target Branch     : VALIDATED ({usage_result.target_branch_name})")
        print(f"    Resource Conflict : NONE (Unique Prefix '{validation_result.validated_payload.resource_prefix}')")
        print(f"    Proposed Folder   : {usage_result.proposed_folder_name}")

        print("\n==================================================")
        print("🎉 [STEP 3b COMPLETED SUCCESSFULLY]")
        print("Target service repo & branch validated. No duplicate conflicts found.")
        print("Execution paused before Step 4 (Target Repo Configuration Inheritance).")
        print("==================================================\n")
    else:
        print("\n==================================================")
        print("🛑 [EXECUTION CANCELED]")
        print("User requested adjustment or canceled the request.")
        print("==================================================\n")

if __name__ == "__main__":
    main()
