from src.da.schemas.base_intake import GateAIntakeOutput
from src.da.section_a_iac.gate_b import GateBValidator, GateBValidationResult


class HITL2BasicIaCHandler:
    MAX_RETRIES = 3

    @classmethod
    def resolve_missing_fields(cls, intake_data: GateAIntakeOutput, retry_count: int = 0) -> GateBValidationResult:
        validation_result = GateBValidator.validate_iac_request(intake_data)

        if validation_result.is_valid:
            return validation_result

        ticket_id = intake_data.ticket_metadata.ticket_id if intake_data.ticket_metadata else "UNKNOWN"

        if retry_count >= cls.MAX_RETRIES:
            validation_result.message = f"HITL 2 Exceeded Max Retries ({cls.MAX_RETRIES}). Escalating ticket {ticket_id} to human DevOps."
            return validation_result

        print(f"\n⚠️  [HITL 2 - Missing Basic IaC Fields] (Attempt {retry_count + 1}/{cls.MAX_RETRIES})")
        print(f"   Ticket: {ticket_id}")
        print(f"   Missing/Invalid Fields: {', '.join(validation_result.missing_fields)}")

        for field in validation_result.missing_fields:
            if "environment" in field:
                val = input("   👉 Enter Environment (dev | qa | staging | prod): ").strip().lower()
                if val:
                    intake_data.environment = val

            elif "target_service_repo" in field:
                val = input("   👉 Enter Target Service Repository (e.g. pss_fabric_core_app_iac_sample): ").strip()
                if val:
                    intake_data.target_service_repo = val

            elif "resource_type" in field:
                val = input("   👉 Enter Resource Module Type (e.g. PostgreSQL-Flexible-Server, storage_account): ").strip()
                if val:
                    intake_data.resource_type = val

        return cls.resolve_missing_fields(intake_data, retry_count + 1)
