from src.da.schemas.base_intake import GateAIntakeOutput


class ClassificationRouter:
    VALID_CLASSIFICATIONS = [
        "cloud_service_iac",
        "microservice_onboarding",
        "config_changes"
    ]

    @classmethod
    def route_request(cls, intake_data: GateAIntakeOutput) -> str:
        classification = intake_data.classification.lower() if intake_data.classification else ""
        if classification not in cls.VALID_CLASSIFICATIONS:
            raise ValueError(f"Unknown classification: '{classification}'. Expected one of {cls.VALID_CLASSIFICATIONS}")
        return classification
