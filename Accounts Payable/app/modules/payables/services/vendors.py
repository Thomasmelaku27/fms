"""Vendor master data: vendors, supplier status changes, discount policies."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.payables.integration import Integrations
from app.modules.payables.models import DiscountPolicy, Vendor, VendorStatus
from app.modules.payables.schemas import (
    DiscountPolicyIn,
    DiscountPolicyUpdate,
    VendorIn,
    VendorUpdate,
)
from app.modules.payables.services.common import (
    Conflict,
    NotFound,
    ValidationFailed,
    get_vendor,
    require_discount_account,
    require_payable_account,
    utcnow,
)

# --- discount policies ----------------------------------------------------------------------


def get_discount_policy(db: Session, policy_id: int) -> DiscountPolicy:
    policy = db.get(DiscountPolicy, policy_id)
    if policy is None:
        raise NotFound("Discount policy not found")
    return policy


def _referenced_policy(db: Session, policy_id: int) -> DiscountPolicy:
    policy = db.get(DiscountPolicy, policy_id)
    if policy is None:
        raise ValidationFailed(f"Discount policy {policy_id} does not exist")
    if not policy.is_active:
        raise ValidationFailed(f"Discount policy {policy.code} is inactive")
    return policy


def create_discount_policy(db: Session, data: DiscountPolicyIn) -> DiscountPolicy:
    if db.scalar(select(DiscountPolicy.id).where(DiscountPolicy.code == data.code)):
        raise Conflict("Discount policy code exists")
    require_discount_account(db, data.discount_account_id)
    policy = DiscountPolicy(**data.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def update_discount_policy(
    db: Session, policy_id: int, data: DiscountPolicyUpdate
) -> DiscountPolicy:
    """Policies are snapshotted onto bills at approval, so editing never changes approved bills."""
    policy = get_discount_policy(db, policy_id)
    fields = data.model_dump(exclude_unset=True)
    if fields.get("discount_account_id") is not None:
        require_discount_account(db, fields["discount_account_id"])
    for name, value in fields.items():
        setattr(policy, name, value)
    db.commit()
    db.refresh(policy)
    return policy


def list_discount_policies(db: Session) -> list[DiscountPolicy]:
    return list(db.scalars(select(DiscountPolicy).order_by(DiscountPolicy.code)))


# --- vendors -------------------------------------------------------------------------------------


def create_vendor(db: Session, data: VendorIn) -> Vendor:
    if db.scalar(select(Vendor.id).where(Vendor.code == data.code)):
        raise Conflict("Vendor code exists")
    if data.payable_account_id is not None:
        require_payable_account(db, data.payable_account_id)
    if data.discount_policy_id is not None:
        _referenced_policy(db, data.discount_policy_id)
    vendor = Vendor(**data.model_dump(), status=VendorStatus.ACTIVE)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def update_vendor(db: Session, vendor_id: int, data: VendorUpdate) -> Vendor:
    vendor = get_vendor(db, vendor_id)
    fields = data.model_dump(exclude_unset=True)
    if fields.get("payable_account_id") is not None:
        require_payable_account(db, fields["payable_account_id"])
    if fields.get("discount_policy_id") is not None:
        _referenced_policy(db, fields["discount_policy_id"])
    is_active = fields.pop("is_active", None)
    for name, value in fields.items():
        setattr(vendor, name, value)
    if is_active is not None:
        # Legacy flag: map onto the status model without losing an explicit on-hold.
        if is_active and vendor.status in (VendorStatus.SUSPENDED, VendorStatus.INACTIVE):
            _set_status(vendor, VendorStatus.ACTIVE, "reactivated", "api")
        elif not is_active and vendor.status in (VendorStatus.ACTIVE, VendorStatus.ON_HOLD):
            _set_status(vendor, VendorStatus.INACTIVE, "deactivated", "api")
    db.commit()
    db.refresh(vendor)
    return vendor


def _set_status(vendor: Vendor, status: VendorStatus, reason: str, actor: str) -> None:
    vendor.status = status
    vendor.is_active = status in (VendorStatus.ACTIVE, VendorStatus.ON_HOLD)
    vendor.status_reason = reason
    vendor.status_changed_by = actor
    vendor.status_changed_at = utcnow()


def change_vendor_status(
    db: Session,
    vendor_id: int,
    *,
    status: VendorStatus,
    reason: str,
    actor: str,
    integrations: Integrations,
) -> Vendor:
    """Explicit, audited supplier status change (active / on_hold / suspended / inactive)."""
    vendor = get_vendor(db, vendor_id)
    if vendor.status == status:
        raise Conflict(f"Vendor {vendor.code} is already {status.value}")
    _set_status(vendor, status, reason, actor)
    integrations.audit.record(
        db, actor=actor, action="payables.vendor.status_changed", resource=f"vendor:{vendor.id}"
    )
    db.commit()
    db.refresh(vendor)
    return vendor


def list_vendors(db: Session, *, include_inactive: bool = True) -> list[Vendor]:
    stmt = select(Vendor).order_by(Vendor.code)
    if not include_inactive:
        stmt = stmt.where(Vendor.is_active.is_(True))
    return list(db.scalars(stmt))
