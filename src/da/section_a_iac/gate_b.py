from typing import List
from pydantic import BaseModel
from src.da.schemas.base_intake import GateAIntakeOutput


class GateBValidationResult(BaseModel):
    is_valid: bool
    missing_fields: List[str]
    validated_payload: GateAIntakeOutput
    message: str


class GateBValidator:
    ALLOWED_ENVIRONMENTS = ["dev", "qa", "staging", "prod"]

    @classmethod
    def validate_iac_request(cls, intake_data: GateAIntakeOutput) -> GateBValidationResult:
        missing = []

        if not intake_data.environment or intake_data.environment.lower() not in cls.ALLOWED_ENVIRONMENTS:
            missing.append("environment (must be dev, qa, staging, or prod)")

        if not intake_data.target_service_repo or not intake_data.target_service_repo.strip():
            missing.append("target_service_repo")

        if not intake_data.resource_type or not intake_data.resource_type.strip():
            missing.append("resource_type")

        # resource_prefix is deliberately NOT required here (M8). Name-ness is not
        # universal -- ACR uses `acr_name`, storage uses `storage_account_name`. Which
        # variable carries the name belongs to the module contract and is discovered at
        # Step 4a, not demanded of the ticket at Gate B.

        if missing:
            return GateBValidationResult(
                is_valid=False,
                missing_fields=missing,
                validated_payload=intake_data,
                message=f"Gate B Check Failed. Missing required IaC baseline fields: {', '.join(missing)}"
            )

        return GateBValidationResult(
            is_valid=True,
            missing_fields=[],
            validated_payload=intake_data,
            message="Gate B Check Passed. All required IaC baseline fields are valid."
        )
