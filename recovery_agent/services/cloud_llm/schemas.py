from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, AliasChoices


class CallStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    DECLINED = "declined"
    CALLBACK = "callback"
    WRONG_NUMBER = "wrong_number"
    ALREADY_SERVICED = "already_serviced"
    VEHICLE_SOLD = "vehicle_sold"
    DO_NOT_CALL = "do_not_call"
    CLOSED = "closed"
    ERROR = "error"


TERMINAL_STATUSES = {
    CallStatus.BOOKED,
    CallStatus.RESCHEDULED,
    CallStatus.DECLINED,
    CallStatus.WRONG_NUMBER,
    CallStatus.ALREADY_SERVICED,
    CallStatus.VEHICLE_SOLD,
    CallStatus.DO_NOT_CALL,
    CallStatus.CLOSED,
}


class CRMUpdateField(str, Enum):
    VEHICLE_MODEL = "vehicle_model"
    MOBILE_NUMBER = "mobile_number"
    VEHICLE_NAME = "vehicle_name"
    PURCHASE_DATE = "purchase_date"
    LAST_SERVICE_DATE = "last_service_date"
    NEXT_SERVICE_DATE = "next_service_date"
    NEXT_SERVICE_TIME = "next_service_time"


class CRMUpdate(BaseModel):
    field: CRMUpdateField
    new_value: str = Field(validation_alias=AliasChoices("new_value", "value"))


class CallSummaryData(BaseModel):
    booking_confirmed: bool = False
    appointment_datetime: Optional[str] = None
    next_action: Optional[str] = None
    nps_score: Optional[int] = None
    feedback_note: Optional[str] = None
    refusal_count: Optional[int] = None
    error: Optional[str] = None


class TurnResult(BaseModel):
    intent: str
    response_text: str
    call_status: CallStatus = CallStatus.IN_PROGRESS
    call_ended: bool = False          # <-- new: conversation is over, hang up now
    summary: CallSummaryData = Field(default_factory=CallSummaryData)
    crm_updates: List[CRMUpdate] = Field(default_factory=list)
    call_summary: Optional[str] = None

    # 🔥 NEW: whole-call quality/cost fields, written by
    # BharatRouterClient/KrutrimClient.generate_call_summary().
    # accuracy = average of filler_accuracy + llm_accuracy, computed in
    # Python from the two LLM-judged scores below -- never asked of the
    # LLM directly.
    accuracy: float | None = None
    filler_accuracy: float | None = None
    llm_accuracy: float | None = None
    # llm_pricing = computed in Python from total_prompt_tokens/
    # total_output_tokens passed into generate_call_summary(), at
    # ₹9/₹33 per Mtok (in/out) -- never asked of the LLM.
    llm_pricing: float | None = None

    @field_validator("call_status", mode="before")
    @classmethod
    def _default_call_status(cls, v):
        return v if v is not None else CallStatus.IN_PROGRESS

    @field_validator("call_ended", mode="before")
    @classmethod
    def _default_call_ended(cls, v):
        return v if v is not None else False

    @field_validator("summary", mode="before")
    @classmethod
    def _default_summary(cls, v):
        return v if v is not None else {}

    @field_validator("crm_updates", mode="before")
    @classmethod
    def _default_crm_updates(cls, v):
        return v if v is not None else []

class LiveTurnResult(BaseModel):
    """
    Used for every in-call turn. Deliberately minimal -- only what's needed
    to speak the response and track call state in real time. Full summary,
    crm_updates, and call_summary are generated once, after the call ends,
    via generate_call_summary() -- not on every turn -- since none of that
    blocks what the customer needs to hear.

    Per-turn accuracy/filler_accuracy/llm_pricing are NOT part of this --
    they're produced by a separate, dedicated call (score_and_price_turn())
    made in the background after the turn's audio has already played, so
    they never sit on this model or add latency here.
    """
    intent: str
    response_text: str
    call_status: CallStatus = CallStatus.IN_PROGRESS
    call_ended: bool = False

    @field_validator("call_status", mode="before")
    @classmethod
    def _default_call_status(cls, v):
        return v if v is not None else CallStatus.IN_PROGRESS

    @field_validator("call_ended", mode="before")
    @classmethod
    def _default_call_ended(cls, v):
        return v if v is not None else False


class CallContext(BaseModel):
    customer_name: str
    vehicle_model: str
    due_date: str
    module: str
    crm_notes: Optional[str] = None