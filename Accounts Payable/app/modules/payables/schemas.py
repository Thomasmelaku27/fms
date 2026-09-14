"""Request / response models of the payables API.

Amount rules applied here (mirrored by CHECK constraints in the database):

* money is ``Decimal`` with at most 2 decimals; quantities and unit prices
  allow 4 decimals;
* a line total is ``quantity x unit_price`` rounded half-up to the cent; a
  client may send ``line_total`` as a cross-check, and it must match;
* bill lines must sum exactly to the bill ``total`` (checked in the service so
  partial updates can compare against the stored total);
* payment allocations may not exceed the payment ``amount``; any remainder is
  recorded as an unapplied (advance) balance on the vendor's payable account.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.payables.models import (
    AllocationKind,
    BillStatus,
    CreditKind,
    CreditStatus,
    DuplicateFlagStatus,
    HoldKind,
    OperationState,
    PaymentMethod,
    PaymentRunStatus,
    PaymentStatus,
    RunItemStatus,
    StatementLineKind,
    StatementMatchStatus,
    VendorStatus,
)

CENT = Decimal("0.01")
Money = Decimal


def to_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def compute_line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return to_cents(quantity * unit_price)


class Actor(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


# --- discount policies ----------------------------------------------------------------------


class DiscountPolicyIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    description: str = Field(min_length=1, max_length=200)
    discount_percent: Decimal = Field(gt=0, lt=100, max_digits=5, decimal_places=2)
    discount_days: int = Field(ge=0, le=365)
    discount_account_id: int
    is_active: bool = True


class DiscountPolicyUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=200)
    discount_percent: Decimal | None = Field(
        default=None, gt=0, lt=100, max_digits=5, decimal_places=2
    )
    discount_days: int | None = Field(default=None, ge=0, le=365)
    discount_account_id: int | None = None
    is_active: bool | None = None


class DiscountPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str
    discount_percent: Decimal
    discount_days: int
    discount_account_id: int
    is_active: bool


# --- vendors -----------------------------------------------------------------------------------


class VendorIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    payable_account_id: int | None = None
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    preferred_method: PaymentMethod | None = None
    bank_reference: str | None = Field(default=None, max_length=100)
    discount_policy_id: int | None = None


class VendorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    payable_account_id: int | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_active: bool | None = None  # legacy: false -> inactive, true -> active
    preferred_method: PaymentMethod | None = None
    bank_reference: str | None = Field(default=None, max_length=100)
    discount_policy_id: int | None = None


class VendorStatusChangeIn(Actor):
    status: VendorStatus
    reason: str = Field(min_length=1, max_length=500)


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    is_active: bool
    status: VendorStatus
    status_reason: str | None
    status_changed_by: str | None
    status_changed_at: datetime | None
    payable_account_id: int | None
    payment_terms_days: int
    preferred_method: PaymentMethod | None
    bank_reference: str | None
    discount_policy_id: int | None


class VendorBalance(BaseModel):
    """Individual bill balances versus the net amount owed to the vendor."""

    vendor_id: int
    vendor_code: str
    vendor_name: str
    as_of: date
    bills_outstanding: Decimal  # sum of open bill balances
    unapplied_credits: Decimal  # approved credit value not yet applied to bills
    unapplied_advances: Decimal  # recorded payments not yet applied to bills
    net_owed: Decimal  # bills_outstanding - unapplied_credits - unapplied_advances
    open_bill_count: int


# --- bills ------------------------------------------------------------------------------------


class BillLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    account_id: int
    quantity: Decimal = Field(default=Decimal("1"), gt=0, max_digits=18, decimal_places=4)
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    line_total: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)

    @model_validator(mode="after")
    def line_total_matches(self) -> "BillLineIn":
        computed = compute_line_total(self.quantity, self.unit_price)
        if computed <= 0:
            raise ValueError("line total must be at least 0.01")
        if self.line_total is None:
            self.line_total = computed
        elif self.line_total != computed:
            raise ValueError(
                f"line_total {self.line_total} does not equal quantity x unit_price ({computed})"
            )
        return self


class BillLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_no: int
    description: str
    account_id: int
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


class BillIn(BaseModel):
    vendor_id: int
    invoice_number: str = Field(min_length=1, max_length=80)
    invoice_date: date
    due_date: date | None = None  # defaults to invoice_date + vendor payment terms
    total: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    payable_account_id: int | None = None  # defaults to the vendor's AP account
    description: str | None = Field(default=None, max_length=500)
    # A draft may be saved before its lines are entered; lines are required to approve.
    lines: list[BillLineIn] = Field(default_factory=list)


class BillUpdate(BaseModel):
    """Partial update of a draft; ``lines`` (when given) replaces all existing lines."""

    vendor_id: int | None = None
    invoice_number: str | None = Field(default=None, min_length=1, max_length=80)
    invoice_date: date | None = None
    due_date: date | None = None
    total: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    payable_account_id: int | None = None
    description: str | None = Field(default=None, max_length=500)
    lines: list[BillLineIn] | None = None


class HoldIn(Actor):
    kind: HoldKind = HoldKind.HOLD
    reason: str = Field(min_length=1, max_length=500)


class HoldReleaseIn(Actor):
    resolution: str = Field(min_length=1, max_length=500)


class HoldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bill_id: int
    kind: HoldKind
    reason: str
    placed_by: str
    placed_at: datetime
    released_by: str | None
    released_at: datetime | None
    resolution: str | None
    is_active: bool


class DuplicateFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bill_id: int
    suspected_bill_id: int
    reasons: str
    score: int
    blocks_approval: bool
    status: DuplicateFlagStatus
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None


class FlagReviewIn(Actor):
    decision: DuplicateFlagStatus  # cleared or confirmed
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def decision_is_final(self) -> "FlagReviewIn":
        if self.decision == DuplicateFlagStatus.PENDING:
            raise ValueError("decision must be 'cleared' or 'confirmed'")
        return self


class DiscountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bill_id: int
    payment_id: int | None
    amount: Decimal
    taken_on: date
    taken_by: str
    discount_account_id: int
    journal_entry_id: int | None
    reversal_date: date | None
    reversed_by: str | None
    reversal_journal_entry_id: int | None
    is_active: bool


class BillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor_id: int
    invoice_number: str
    invoice_date: date
    due_date: date
    total: Decimal
    status: BillStatus
    description: str | None
    payable_account_id: int | None
    journal_entry_id: int | None
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    discount_date: date | None
    discount_amount: Decimal
    discount_account_id: int | None
    created_at: datetime
    updated_at: datetime
    lines_total: Decimal
    amount_paid: Decimal
    credits_applied: Decimal
    discounts_taken: Decimal
    amount_settled: Decimal
    outstanding: Decimal
    on_hold: bool
    lines: list[BillLineOut]
    holds: list[HoldOut]
    duplicate_flags: list[DuplicateFlagOut]
    discounts: list[DiscountOut]


class ApprovalIn(Actor):
    pass


class RejectionIn(Actor):
    reason: str = Field(min_length=1, max_length=500)


class ReopenIn(Actor):
    pass


# --- payment schedules --------------------------------------------------------------------------


class InstallmentIn(BaseModel):
    due_date: date
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class ScheduleIn(BaseModel):
    installments: list[InstallmentIn] = Field(min_length=1)


class InstallmentOut(BaseModel):
    installment_no: int
    due_date: date
    amount: Decimal
    amount_remaining: Decimal


class ScheduleOut(BaseModel):
    bill_id: int
    status: BillStatus
    total: Decimal
    amount_paid: Decimal
    installments: list[InstallmentOut]


class ScheduleReportRow(BaseModel):
    bill_id: int
    vendor_id: int
    vendor_code: str
    invoice_number: str
    installment_no: int
    due_date: date
    amount: Decimal
    amount_remaining: Decimal


class ScheduleReport(BaseModel):
    date_from: date | None
    date_to: date | None
    rows: list[ScheduleReportRow]
    total_remaining: Decimal


# --- payments -------------------------------------------------------------------------------------


class AllocationIn(BaseModel):
    bill_id: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    # Settle the bill's remaining early-payment discount together with this allocation.
    take_discount: bool = False


class PaymentIn(BaseModel):
    vendor_id: int
    payment_date: date
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    reference: str | None = Field(default=None, max_length=100)
    cash_account_id: int
    recorded_by: str = Field(min_length=1, max_length=100)
    # Optional client token: repeating a request with the same key returns the
    # payment recorded the first time instead of paying twice.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)
    # Allocations may total less than the amount: the remainder is an advance
    # (unapplied) balance carried on ``payable_account_id`` (vendor default if omitted).
    allocations: list[AllocationIn] = Field(default_factory=list)
    payable_account_id: int | None = None

    @model_validator(mode="after")
    def allocations_within_amount(self) -> "PaymentIn":
        allocated = sum((a.amount for a in self.allocations), Decimal("0"))
        if allocated > self.amount:
            raise ValueError(f"allocations ({allocated}) exceed the payment amount ({self.amount})")
        bill_ids = [a.bill_id for a in self.allocations]
        if len(bill_ids) != len(set(bill_ids)):
            raise ValueError("a bill can only be allocated once per payment")
        return self


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    bill_id: int
    amount: Decimal
    kind: AllocationKind
    allocation_date: date
    allocated_by: str
    unallocation_date: date | None
    unallocated_by: str | None
    unallocation_reason: str | None
    is_active: bool


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor_id: int
    payment_date: date
    amount: Decimal
    method: PaymentMethod
    reference: str | None
    cash_account_id: int
    payable_account_id: int | None
    journal_entry_id: int | None
    idempotency_key: str | None
    recorded_by: str
    created_at: datetime
    status: PaymentStatus
    reversed_by: str | None
    reversed_at: datetime | None
    reversal_date: date | None
    reversal_reason: str | None
    reversal_journal_entry_id: int | None
    corrects_payment_id: int | None
    allocated_total: Decimal
    unapplied_amount: Decimal
    allocations: list[AllocationOut]
    discounts: list[DiscountOut]


class ReversalIn(Actor):
    reason: str = Field(min_length=1, max_length=500)
    reversal_date: date | None = None  # defaults to today; never before the original date


class PaymentCorrectionIn(ReversalIn):
    """Reverse a payment and record its replacement in one transaction."""

    replacement: PaymentIn


class CorrectionOut(BaseModel):
    reversed: PaymentOut
    replacement: PaymentOut


class AdvanceApplyIn(Actor):
    """Apply part of a payment's unapplied remainder to a bill."""

    bill_id: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    allocation_date: date | None = None  # defaults to today


class AdvanceUnallocateIn(Actor):
    reason: str | None = Field(default=None, max_length=500)
    unallocation_date: date | None = None  # defaults to today


# --- supplier credits and adjustments ----------------------------------------------------------


class CreditLineIn(BillLineIn):
    pass


class CreditLineOut(BillLineOut):
    pass


class CreditIn(BaseModel):
    vendor_id: int
    kind: CreditKind = CreditKind.CREDIT_NOTE
    credit_number: str = Field(min_length=1, max_length=80)
    credit_date: date
    total: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    payable_account_id: int | None = None  # defaults to the vendor's AP account
    related_bill_id: int | None = None
    description: str | None = Field(default=None, max_length=500)
    lines: list[CreditLineIn] = Field(default_factory=list)


class CreditUpdate(BaseModel):
    kind: CreditKind | None = None
    credit_number: str | None = Field(default=None, min_length=1, max_length=80)
    credit_date: date | None = None
    total: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    payable_account_id: int | None = None
    related_bill_id: int | None = None
    description: str | None = Field(default=None, max_length=500)
    lines: list[CreditLineIn] | None = None


class CreditApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    credit_id: int
    bill_id: int
    amount: Decimal
    application_date: date
    applied_by: str
    applied_at: datetime
    unapplication_date: date | None
    unapplied_by: str | None
    unapplied_at: datetime | None
    unapplication_reason: str | None
    is_active: bool


class CreditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor_id: int
    kind: CreditKind
    credit_number: str
    credit_date: date
    total: Decimal
    status: CreditStatus
    description: str | None
    related_bill_id: int | None
    payable_account_id: int | None
    journal_entry_id: int | None
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    reversed_by: str | None
    reversed_at: datetime | None
    reversal_date: date | None
    reversal_reason: str | None
    reversal_journal_entry_id: int | None
    created_at: datetime
    updated_at: datetime
    lines_total: Decimal
    amount_applied: Decimal
    remaining: Decimal
    lines: list[CreditLineOut]
    applications: list[CreditApplicationOut]


class ApplyCreditIn(Actor):
    bill_id: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    application_date: date | None = None  # defaults to today


class UnapplyCreditIn(Actor):
    reason: str | None = Field(default=None, max_length=500)
    unapplication_date: date | None = None  # defaults to today; never before the application


# --- payment runs -------------------------------------------------------------------------------


class PaymentRunCreateIn(Actor):
    reference: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=500)
    pay_date: date
    due_through: date | None = None  # defaults to pay_date
    vendor_ids: list[int] | None = None
    take_discounts: bool = True
    use_credits: bool = True
    use_advances: bool = True
    cash_account_id: int
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER


class RunItemUpdateIn(Actor):
    """Reviewer adjusts how much of the bill this run settles (partial settlement)."""

    settle_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class RunItemExcludeIn(Actor):
    reason: str = Field(min_length=1, max_length=500)


class RunActionIn(Actor):
    note: str | None = Field(default=None, max_length=500)


class RunItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    vendor_id: int
    bill_id: int
    installment_no: int | None
    due_date: date
    outstanding_snapshot: Decimal
    settle_amount: Decimal
    discount_amount: Decimal
    credit_amount: Decimal
    advance_amount: Decimal
    pay_amount: Decimal
    status: RunItemStatus
    explanation: str
    payment_id: int | None
    execution_reference: str | None
    failure_reason: str | None
    excluded_by: str | None
    exclusion_reason: str | None
    destination_reference: str | None
    # Actual settlement next to the approved plan (fmspayables06).
    settlement_status: str = "pending"
    actual_pay_amount: Decimal | None = None
    actual_discount_amount: Decimal | None = None
    actual_credit_amount: Decimal | None = None
    actual_advance_amount: Decimal | None = None
    settlement_note: str | None = None


class PaymentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    description: str | None
    status: PaymentRunStatus
    pay_date: date
    due_through: date
    vendor_filter: str | None
    take_discounts: bool
    use_credits: bool
    use_advances: bool
    cash_account_id: int
    method: PaymentMethod
    created_by: str
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    approved_by: str | None
    approved_at: datetime | None
    executed_by: str | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    attempts: int
    cancelled_by: str | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    version: int
    proposal_fingerprint: str | None
    approved_fingerprint: str | None
    approval_invalidated_reason: str | None
    finalization_status: str | None = None
    settlement_summary_json: str | None = None
    finalized_at: datetime | None = None
    total_pay_amount: Decimal
    items: list[RunItemOut]


class ExecutionOutcome(BaseModel):
    vendor_id: int
    vendor_code: str
    amount: Decimal
    success: bool
    payment_id: int | None
    reference: str | None
    error: str | None
    skipped: bool = False  # already paid in an earlier attempt
    operation_uid: str | None = None
    state: str | None = None


class RunExecutionOut(BaseModel):
    run: PaymentRunOut
    outcomes: list[ExecutionOutcome]


# --- outstanding and aging ------------------------------------------------------------------------


class OutstandingRow(BaseModel):
    bill_id: int
    vendor_id: int
    vendor_code: str
    invoice_number: str
    invoice_date: date
    due_date: date
    status: BillStatus
    total: Decimal
    amount_paid: Decimal
    outstanding: Decimal
    days_overdue: int
    on_hold: bool = False


class OutstandingReport(BaseModel):
    as_of: date
    rows: list[OutstandingRow]
    total_outstanding: Decimal


class AgingBuckets(BaseModel):
    current: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    over_90: Decimal
    total: Decimal


class AgingRow(AgingBuckets):
    vendor_id: int
    vendor_code: str
    vendor_name: str


class AgingReport(BaseModel):
    as_of: date
    rows: list[AgingRow]
    totals: AgingBuckets


# --- supplier statements and reconciliation -------------------------------------------------------


class StatementLine(BaseModel):
    date: date
    type: str  # bill | credit | payment | advance_application | discount | *_reversal ...
    document_id: int
    reference: str
    description: str
    journal_entry_id: int | None
    debit: Decimal  # reduces what is owed (payments, credits, discounts)
    credit: Decimal  # increases what is owed (bills, reversals of payments / credits / discounts)
    balance: Decimal


class StatementTotals(BaseModel):
    bills: Decimal
    credits: Decimal
    payments: Decimal
    discounts: Decimal
    payment_reversals: Decimal
    credit_reversals: Decimal
    discount_reversals: Decimal


class VendorStatement(BaseModel):
    vendor_id: int
    vendor_code: str
    vendor_name: str
    date_from: date
    date_to: date
    opening_balance: Decimal
    lines: list[StatementLine]
    totals: StatementTotals
    closing_balance: Decimal
    # Reconciliation of the statement against the open-item view as of date_to.
    outstanding_bills: Decimal
    unapplied_credits: Decimal
    unapplied_advances: Decimal
    difference: Decimal  # closing - (outstanding - unapplied credits - unapplied advances)


class ReconciliationException(BaseModel):
    account_id: int | None
    account_code: str | None
    vendor_id: int | None
    vendor_code: str | None
    document_type: str  # bill | credit | payment | discount | *_reversal | journal_entry
    document_id: int | None
    journal_entry_id: int | None
    expected: Decimal  # effect the AP document should have on the control account
    actual: Decimal  # effect the posted ledger lines actually have
    difference: Decimal
    cause: str


class ReconciliationRow(BaseModel):
    account_id: int
    account_code: str
    account_name: str
    ledger_balance: Decimal  # credit - debit of posted lines on the AP control account
    subledger_balance: Decimal  # sum of vendor balances that post to this account
    difference: Decimal


class PendingObligation(BaseModel):
    """A gateway-confirmed transfer that is not yet in the books (cash out, nothing posted)."""

    operation_uid: str
    vendor_id: int
    vendor_code: str | None
    amount: Decimal
    payment_date: date
    state: str
    gateway_confirmation_ref: str | None
    cash_account_id: int
    accounting_status: str


class ReconciliationReport(BaseModel):
    as_of: date
    rows: list[ReconciliationRow]
    exceptions: list[ReconciliationException]
    total_ledger: Decimal
    total_subledger: Decimal
    total_difference: Decimal
    # Confirmed-but-unrecorded transfers are obligations the reconciliation cannot explain away.
    pending_obligations: list[PendingObligation] = []
    pending_obligations_total: Decimal = Decimal("0.00")


# --- period end -----------------------------------------------------------------------------------


class PeriodCheck(BaseModel):
    code: str
    severity: str  # blocker | warning | info
    title: str
    count: int
    items: list[dict]


class PeriodValidationResult(BaseModel):
    period_end: date
    status: str  # ok | warnings | blocked
    can_close: bool
    checks: list[PeriodCheck]
    already_closed: bool


class PeriodCloseIn(Actor):
    period_end: date
    note: str | None = Field(default=None, max_length=500)
    force: bool = False  # override blockers (authorised actors only; exceptions are preserved)
    force_reason: str | None = Field(default=None, max_length=500)
    gl_period_reference: str | None = Field(default=None, max_length=100)


class AttestationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    period_end: date
    outcome: str
    attempted_by: str
    forced: bool
    force_reason: str | None
    liability_balance: Decimal
    open_bill_count: int
    vendor_count: int
    checks_json: str
    exceptions_json: str
    reconciliation_json: str
    source_fingerprint: str
    gl_period_reference: str | None
    lock_id: int | None
    created_at: datetime
    content_hash: str | None = None
    obligations_json: str | None = None


# --- payment operations, recovery, diagnostics ---------------------------------------------------


class OperationEventOut(BaseModel):
    seq: int
    at: datetime
    from_state: str | None
    to_state: str
    category: str
    detail: str | None
    actor: str


class OperationChecklist(BaseModel):
    prepared: bool
    submitted: bool
    gateway_confirmed: bool
    ap_payment_recorded: bool
    gl_entry_posted: bool
    allocations_committed: bool
    reversed: bool
    reconciles: bool
    notes: list[str]


class VerificationFindingOut(BaseModel):
    code: str
    severity: str  # critical | error | warning | info
    operation_uid: str
    vendor_id: int | None
    amount: str | None
    expected: str | None
    actual: str | None
    remediation: str
    detail: str = ""


class OperationVerificationOut(BaseModel):
    operation_uid: str
    state: str
    vendor_id: int
    amount: Decimal
    currency: str
    run_id: int | None
    delivery_status: str
    accounting_status: str
    settlement_status: str
    investigation_status: str
    recorded_accounting_status: str
    completion_policy_satisfied: bool
    worst_severity: str | None
    required_evidence: dict[str, bool]
    evidence: dict
    findings: list[VerificationFindingOut]


class RunVerificationOut(BaseModel):
    run_id: int
    status: str
    categories: dict[str, list[str]]
    approved_total_cash: Decimal
    actual_total_cash: Decimal
    can_complete: bool
    findings: list[VerificationFindingOut]
    operations: list[OperationVerificationOut]


class OperationStatusOut(BaseModel):
    operation_uid: str
    idempotency_key: str
    correlation_id: str
    state: OperationState
    delivery_status: str
    accounting_status: str
    settlement_status: str
    investigation_status: str
    recorded_accounting_status: str
    completion_kind: str | None
    completion_policy_satisfied: bool
    verification: OperationVerificationOut
    run_id: int | None
    vendor_id: int
    amount: Decimal
    currency: str
    payment_date: date
    method: str
    destination_reference_masked: str | None
    gateway_status: str | None
    gateway_confirmation_ref: str | None
    payment_id: int | None
    checklist: OperationChecklist
    attempts: int
    max_attempts: int
    failure_category: str | None
    failure_detail: str | None
    recovery_status: str
    recovery_action: str
    recovery_reason: str
    recovery_safe: bool
    manual_investigation_required: bool
    next_step: str
    timestamps: dict[str, datetime | None]
    events: list[OperationEventOut]


class OperationSummaryOut(BaseModel):
    operation_uid: str
    state: OperationState
    run_id: int | None
    vendor_id: int
    amount: Decimal
    payment_date: date
    gateway_status: str | None
    payment_id: int | None
    attempts: int
    failure_category: str | None
    recovery_status: str
    recovery_action: str
    created_at: datetime
    updated_at: datetime


class OperationPageOut(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[OperationSummaryOut]


class RecoveryDecisionOut(BaseModel):
    operation_uid: str
    state: str
    action: str
    reason: str
    attempts: int
    max_attempts: int
    run_id: int | None
    vendor_id: int


class RecoveryResultOut(BaseModel):
    operation_uid: str
    action: str
    before: str
    after: str
    note: str


class RecoveryRunIn(Actor):
    worker_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=500)


class OperationResolveIn(Actor):
    resolution: str = Field(min_length=1, max_length=1000)
    mark_completed: bool = False
    # Completion needs the accounting record; a note alone is refused (422).
    payment_id: int | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class InvestigationIn(Actor):
    note: str = Field(min_length=1, max_length=1000)


class OperationCompleteIn(Actor):
    payment_id: int
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class OperationCorrectionIn(Actor):
    payment_date: date
    allocations: list[AllocationIn] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)


class CloseExceptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    attestation_id: int
    period_end: date
    code: str
    severity: str
    item_json: str
    accepted_by: str
    accept_reason: str
    accepted_at: datetime
    status: str
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    resolution_reference: str | None


class CloseExceptionResolveIn(Actor):
    note: str = Field(min_length=1, max_length=1000)
    reference: str | None = Field(default=None, max_length=200)


class CloseFindingOut(BaseModel):
    code: str
    severity: str
    attestation_id: int
    period_end: date
    expected: str | None
    actual: str | None
    remediation: str
    detail: str = ""


class CloseVerificationOut(BaseModel):
    attestation_id: int
    period_end: date
    outcome: str
    verified: bool
    content_hash_verified: bool
    source_fingerprint_verified: bool
    liability_verified: bool
    obligations: list[dict]
    exceptions: list[dict]
    findings: list[CloseFindingOut]


class DuplicateConfirmationIn(Actor):
    reference: str = Field(min_length=1, max_length=100)


# --- historical reconstruction -----------------------------------------------------------------------


class ReconstructionFindingOut(BaseModel):
    check: str
    vendor_id: int | None
    bill_id: int | None
    transaction: str | None
    as_of: date
    expected: Decimal
    actual: Decimal
    difference: Decimal
    cause: str


class ReconstructionOut(BaseModel):
    as_of: date
    view: str
    bill_outstanding: dict[int, Decimal]
    vendor_bills: dict[int, Decimal]
    vendor_credits: dict[int, Decimal]
    vendor_advances: dict[int, Decimal]
    control_balance: dict[int, Decimal]
    ledger_balance: dict[int, Decimal]
    findings: list[ReconstructionFindingOut]
    limitations: list[str]
    # Payment operations as recorded at the cutoff (fmspayables06).
    operation_positions: dict[str, dict] = {}
    obligations_total: Decimal = Decimal("0.00")
    statement_imports: dict[str, int] = {}


# --- supplier statement imports ------------------------------------------------------------------------


class StatementImportIn(Actor):
    file_name: str = Field(min_length=1, max_length=200)
    statement_date: date
    content: str | None = None  # CSV text (UTF-8)
    content_base64: str | None = None  # alternative for raw bytes (encoding tests, uploads)

    @model_validator(mode="after")
    def one_content(self) -> "StatementImportIn":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one of content or content_base64")
        return self


class StatementPreviewLineOut(BaseModel):
    line_no: int
    kind: StatementLineKind
    reference: str
    normalized_reference: str
    date: date
    amount: Decimal
    match_status: StatementMatchStatus
    matched_type: str | None
    matched_id: int | None
    internal_amount: Decimal | None
    internal_date: date | None
    explanation: str


class StatementPreviewOut(BaseModel):
    vendor_id: int
    file_name: str
    content_hash: str
    already_imported_as: int | None
    summary: dict
    lines: list[StatementPreviewLineOut]


class StatementLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_no: int
    kind: StatementLineKind
    reference: str
    normalized_reference: str
    line_date: date
    amount: Decimal
    match_status: StatementMatchStatus
    matched_type: str | None
    matched_id: int | None
    internal_amount: Decimal | None
    internal_date: date | None
    explanation: str
    review_status: str
    resolution_note: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None


class StatementImportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor_id: int
    file_name: str
    content_hash: str
    statement_date: date
    imported_by: str
    imported_at: datetime
    line_count: int
    summary_json: str
    lines: list[StatementLineOut]


class StatementLineResolveIn(Actor):
    note: str = Field(min_length=1, max_length=500)


class PeriodLockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    closed_through: date
    closed_by: str
    closed_at: datetime
    note: str | None
