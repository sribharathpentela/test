from pydantic import BaseModel
from src.da.schemas.base_intake import GateAIntakeOutput


class SupervisorHandoffMessage(BaseModel):
    status: str = "OUT_OF_SCOPE"
    target_agent: str = "supervisor_agent"
    ticket_id: str
    rejection_reason: str
    message: str


class HITL1HandoffHandler:
    @classmethod
    def handle_non_da_task(cls, intake_data: GateAIntakeOutput) -> SupervisorHandoffMessage:
        ticket_id = intake_data.ticket_metadata.ticket_id if intake_data.ticket_metadata else "UNKNOWN"
        reason = intake_data.rejection_reason or "Request is out of scope for DA."
        
        return SupervisorHandoffMessage(
            status="OUT_OF_SCOPE",
            target_agent="supervisor_agent",
            ticket_id=ticket_id,
            rejection_reason=reason,
            message=f"Gate A Handoff: Task '{ticket_id}' is OUT OF SCOPE for Deployment Agent (DA). Rejection Reason: {reason}. Informing Supervisor Agent."
        )
