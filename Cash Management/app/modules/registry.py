"""Shared module wiring, frozen for parallel task branches."""
from app.modules.payables import models as payables_models  # noqa: F401
from app.modules.receivables import models as receivables_models  # noqa: F401
from app.modules.budgeting import models as budgeting_models  # noqa: F401
from app.modules.cash import models as cash_models  # noqa: F401
from app.modules.tax import models as tax_models  # noqa: F401
from app.modules.ai_review import models as ai_models  # noqa: F401
from app.modules.audit import models as audit_models  # noqa: F401
from app.modules.payables.router import router as payables_router
from app.modules.receivables.router import router as receivables_router
from app.modules.budgeting.router import router as budgeting_router
from app.modules.cash.router import router as cash_router
from app.modules.reporting.router import router as reporting_router
from app.modules.tax.router import router as tax_router
from app.modules.ai_review.router import router as ai_router
from app.modules.audit.router import router as audit_router

ROUTERS=(payables_router,receivables_router,budgeting_router,cash_router,reporting_router,tax_router,ai_router,audit_router)
