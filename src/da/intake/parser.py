from typing import Any, Dict, Optional

from dotenv import load_dotenv

from src.da.llm.client import Completer, complete_json, groq_completer
from src.da.llm.prompts import SYSTEM_INTAKE_PROMPT
from src.da.schemas.base_intake import GateAIntakeOutput, TicketMetadata

load_dotenv()


class JiraIntakeParser:
    """Gate A: a raw Jira payload in, a classified intake out.

    The completer is injected, like every other model call in the agent. It
    used to build its own `Groq` client in here, which made constructing the
    parser require a credential -- so a caller that only wanted to parse an
    already-extracted ticket still had to have a key.
    """

    def __init__(self, api_key: str = None, model_name: str = None,
                 completer: Optional[Completer] = None):
        self._completer = completer
        self._api_key = api_key
        self._model_name = model_name

    @property
    def completer(self) -> Completer:
        """Built on first use, so no key is needed to construct the parser."""
        if self._completer is None:
            self._completer = groq_completer(model=self._model_name,
                                             api_key=self._api_key)
        return self._completer

    def parse_raw_jira_ticket(self, raw_jira_data: Dict[str, Any]) -> GateAIntakeOutput:
        jira_info = raw_jira_data.get("jira", raw_jira_data)

        ticket_key = jira_info.get("key") or jira_info.get("ticket_id") or "UNKNOWN"
        reporter_name = jira_info.get("reporter") or "UNKNOWN"
        ticket_url = jira_info.get("url") or ""

        user_prompt = f"""Raw Jira Ticket Context:
Ticket Key: {ticket_key}
Summary: {jira_info.get('summary', '')}
Description: {jira_info.get('description', '')}
Reporter: {reporter_name}
URL: {ticket_url}
"""

        parsed_json = complete_json(SYSTEM_INTAKE_PROMPT, user_prompt,
                                    completer=self.completer)

        intake_obj = GateAIntakeOutput.model_validate(parsed_json)
        
        intake_obj.ticket_metadata = TicketMetadata(
            ticket_id=ticket_key,
            ticket_url=ticket_url,
            reporter=reporter_name
        )

        return intake_obj
