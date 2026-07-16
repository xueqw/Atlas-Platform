"""Dimension scorers package.

Importing this package registers every built-in scorer into the registry in
``base``. ``ragas_scorer`` only does its heavy import lazily inside the scorer,
so importing the package here is cheap and safe even without ragas installed.
"""

from __future__ import annotations

from app.core.scorers.base import (  # noqa: F401
    DimensionResult,
    ScoreContext,
    Scorer,
    get_scorer,
    register,
    registered_types,
    skipped_result,
)

# Side-effect imports: each module calls register(...) at import time.
from app.core.scorers import deterministic  # noqa: F401,E402
from app.core.scorers import judge  # noqa: F401,E402
from app.core.scorers import ragas_scorer  # noqa: F401,E402
from app.core.scorers import trace_scorer  # noqa: F401,E402
from app.core.scorers import templates  # noqa: F401,E402

__all__ = [
    "DimensionResult",
    "ScoreContext",
    "Scorer",
    "get_scorer",
    "register",
    "registered_types",
    "skipped_result",
    "deterministic",
    "judge",
    "ragas_scorer",
    "trace_scorer",
    "templates",
]
