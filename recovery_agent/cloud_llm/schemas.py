from pydantic import BaseModel
from typing import Optional


class TurnResult(BaseModel):
    """The single structured JSON output per turn: intent + response + running summary."""
    intent: str
    response_text: str
    call_status: str = "in_progress"
    summary: dict = {}


class CallContext(BaseModel):
    """Structured facts injected per call — never invented by the model."""
    customer_name: str
    vehicle_model: str
    due_date: str
    module: str
    crm_notes: Optional[str] = None