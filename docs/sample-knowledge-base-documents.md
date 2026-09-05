# Sample Knowledge Base Documents

Paste each of these into the Knowledge Base page ("Add document"). Two or
three sentences each is intentional — enough for RAG to retrieve something
concrete and grounded, not a legal manual. Using these six covers every
intent your persona's system prompt already knows how to route to
(payment questions, hardship, dispute, callback policy).

---

### 1. Category: `payment_methods`
**Title:** Accepted payment methods

**Content:**
Customers can pay via UPI, net banking, debit/credit card, or a payment
link sent by SMS/WhatsApp. Cash payments are not accepted over the phone
under any circumstances. UPI and payment link are the fastest options and
should be offered first unless the customer asks for something else.

---

### 2. Category: `late_payment`
**Title:** Late payment grace period and fees

**Content:**
A payment is considered late after 3 days past the due date. A grace
period of 7 days from the due date applies before any late fee is added.
After the grace period, a flat late fee of 2% of the outstanding amount
applies. Agents should mention the grace period calmly and factually, never
as a threat.

---

### 3. Category: `promise_to_pay`
**Title:** Recording and following up on a payment promise

**Content:**
When a customer commits to a specific payment date, that date is recorded
as their promise date and a follow-up callback is scheduled for the day
after. If a promised payment is missed, the next contact should reference
the missed promise factually and ask for a new date — it should not
escalate tone or imply consequences beyond the standard late fee policy.

---

### 4. Category: `hardship`
**Title:** Options for genuine financial hardship

**Content:**
If a customer describes genuine financial hardship, offer either a
partial payment now with the remainder on an extended date, or a full
restructured payment plan over 2-3 installments. Do not pressure a
customer who has stated hardship to commit to a full payment immediately
— the goal is a realistic plan they'll actually keep, not the fastest
promise.

---

### 5. Category: `dispute`
**Title:** Handling a disputed charge

**Content:**
If a customer says a charge is incorrect or they don't recognize it, the
case should be marked as disputed and escalated to the billing team for
review within 2 business days. The agent should acknowledge the dispute
respectfully, confirm no further payment reminders will go out for that
amount until it's resolved, and should never argue the charge is correct.

---

### 6. Category: `communication_policy`
**Title:** Tone and conduct rules for all calls

**Content:**
Agents must never threaten legal action, never raise their tone, and must
respect a customer's request not to be called again (do-not-call) by
ending the call politely and logging the request. At most one question
should be asked per turn, and financial information must only come from
verified system data — never invented or assumed.
