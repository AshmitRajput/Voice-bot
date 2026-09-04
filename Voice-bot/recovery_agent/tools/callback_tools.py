"""
callback_tools.py

Compatibility shim for older code that imports `schedule_callback` from
recovery_agent.tools.callback_tools. Delegates to CallbackService, which is
the single owner of Callback writes.

FIX vs. previous version: the old lookup was
    Customer.objects.filter(phone_number=phone_number, flag="c")
`flag` is not a field on the real Customer model -- this would raise
FieldError on first use. Filtering on phone_number alone is enough here.

FIX vs. this file's earlier version: callback_service.schedule_callback()
no longer takes callback_date/callback_time at all. The real signature is:

    schedule_callback(customer_id, requested_window=None,
                       reason="customer_requested", session_id=None)

The service does its own heuristic day/time resolution against
requested_window internally (see callback_service.py's _heuristic_resolve)
and writes the required scheduled_for DateTimeField itself. This file must
not attempt to parse or split the customer's phrase -- it just passes it
through whole as requested_window.
"""


def schedule_callback(
    session_id,
    phone_number=None,
    customer_name="Customer",
    reason=None,
    requested_for=None,
):
    """Legacy function -- delegates to the new callback service."""
    from recovery_agent.services.callback_service import callback_service
    from recovery_agent.models import Customer

    customer = (
        Customer.objects.filter(phone_number=phone_number).first()
        if phone_number
        else None
    )
    if not customer:
        return {"scheduled": False, "error": "customer not found for this phone number"}

    return callback_service.schedule_callback(
        customer_id=customer.id,
        requested_window=requested_for,
        reason=reason or "customer_requested",
        session_id=session_id,
    )