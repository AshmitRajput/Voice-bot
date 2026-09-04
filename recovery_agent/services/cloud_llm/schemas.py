from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class CallStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DECLINED = "declined"
    CALLBACK = "callback"
    WRONG_NUMBER = "wrong_number"
    DO_NOT_CALL = "do_not_call"
    CLOSED = "closed"
    ERROR = "error"


TERMINAL_STATUSES = {
    CallStatus.COMPLETED,
    CallStatus.DECLINED,
    CallStatus.WRONG_NUMBER,
    CallStatus.DO_NOT_CALL,
    CallStatus.CLOSED,
}


class RecoveryOutcome(str, Enum):
    PAYMENT_VERIFIED = "payment_verified"
    PROMISE_RECORDED = "promise_recorded"
    PAYMENT_LINK_SENT = "payment_link_sent"
    REFUSED = "refused"
    DISPUTED = "disputed"
    CALLBACK_SCHEDULED = "callback_scheduled"
    COMPLAINT_ESCALATED = "complaint_escalated"
    WRONG_NUMBER = "wrong_number"
    ACCOUNT_NOT_OWNED = "account_not_owned"
    NONE = "none"


class RecoverySummaryData(BaseModel):
    """Whole-call recovery summary -- replaces the old booking-shaped
    CallSummaryData. No vehicle/appointment/nps fields; those belonged to
    the Honda service-booking domain."""
    recovery_outcome: RecoveryOutcome = RecoveryOutcome.NONE
    promise_date: Optional[str] = None
    next_action: Optional[str] = None
    refusal_count: Optional[int] = None
    error: Optional[str] = None


class TurnResult(BaseModel):
    intent: str
    response_text: str
    call_status: CallStatus = CallStatus.IN_PROGRESS
    call_ended: bool = False
    summary: RecoverySummaryData = Field(default_factory=RecoverySummaryData)
    call_summary: Optional[str] = None

    accuracy: float | None = None
    filler_accuracy: float | None = None
    llm_accuracy: float | None = None
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


class LiveTurnResult(BaseModel):
    """Per-turn, minimal -- only what's needed to speak the response and
    track call state live. Full summary/scoring happens after the call
    via a separate call, same as before."""
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
    outstanding_amount: str
    due_date: str
    workflow: str = "revenue_recovery"
    recovery_notes: Optional[str] = None