from src.da.schemas.base_intake import GateAIntakeOutput


class ProvisioningConfirmationHandler:
    @classmethod
    def confirm_single_resource_request(cls, intake_data: GateAIntakeOutput) -> bool:
        ticket_id = intake_data.ticket_metadata.ticket_id if intake_data.ticket_metadata else "UNKNOWN"
        print("\n" + "="*50)
        print("📌 [STEP 2b - SINGLE RESOURCE PROVISIONING CONFIRMATION]")
        print("="*50)
        print(f"  Ticket ID           : {ticket_id}")
        print(f"  Classification      : {intake_data.classification}")
        print(f"  Target Environment  : {intake_data.environment}")
        print(f"  Target Service Repo : {intake_data.target_service_repo}")
        print(f"  Resource Type       : {intake_data.resource_type}")
        print(f"  Resource Name Prefix: {intake_data.resource_prefix}")
        print(f"  Extracted Inputs    : {intake_data.extracted_inputs}")
        print("="*50)

        response = input("\n👉 Do you confirm proceeding with this single resource provisioning request? (yes/no): ").strip().lower()
        if response in ["yes", "y", "proceed"]:
            print("\n==================================================")
            print("✅ Thank you! Request confirmed by user.")
            print("==================================================\n")
            return True
        else:
            print("\n==================================================")
            print("🛑 Request canceled or needs adjustment by user.")
            print("==================================================\n")
            return False
