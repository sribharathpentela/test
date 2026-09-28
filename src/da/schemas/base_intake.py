from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, model_validator


class TicketMetadata(BaseModel):
    ticket_id: str = Field(..., description="Jira ticket key, e.g. DEVOPS-111")
    ticket_url: Optional[str] = Field(None, description="Jira ticket URL")
    reporter: Optional[str] = Field(None, description="Ticket reporter name")


class GateAIntakeOutput(BaseModel):
    is_da_task: bool = Field(
        ...,
        description="True if request is an infrastructure, onboarding, or config task for Deployment Agent; False otherwise.",
    )
    rejection_reason: Optional[str] = Field(
        None, description="Explanation if is_da_task is False"
    )

    classification: Optional[str] = Field(
        None,
        description="Must be one of: 'cloud_service_iac', 'microservice_onboarding', 'config_changes', or null if non-DA task",
    )

    ticket_metadata: Optional[TicketMetadata] = Field(
        None, description="Extracted ticket metadata"
    )
    environment: Optional[str] = Field(
        None, description="Target environment normalized to: dev, qa, staging, or prod"
    )
    target_service_repo: Optional[str] = Field(
        None, description="Target service repository name, e.g. pss_fabric_app_iac_sample"
    )
    resource_type: Optional[str] = Field(
        None, description="Extracted module/resource type, e.g. PostgreSQL-Flexible-Server, storage_account, load-balancer"
    )
    resource_prefix: Optional[str] = Field(
        None, description="Explicit or derived resource name prefix, e.g. analytics, core-pg, ingress-lb"
    )

    extracted_inputs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value dictionary of configuration parameters extracted from description",
    )

    @model_validator(mode="before")
    @classmethod
    def populate_ticket_metadata(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(values, dict) and not values.get("ticket_metadata"):
            ticket_id = values.get("ticket_id") or values.get("ticket_key") or "UNKNOWN"
            values["ticket_metadata"] = {
                "ticket_id": ticket_id,
                "ticket_url": values.get("ticket_url"),
                "reporter": values.get("reporter"),
            }
        return values
