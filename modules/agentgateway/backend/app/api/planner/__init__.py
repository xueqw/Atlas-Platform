"""Planner API package.

Re-exports the router (consumed by main.py) plus the symbols the test-suite
reads directly as ``app.api.planner.<name>`` (including ``engine``, which
conftest reassigns to the test engine). Keeping these names importable from the
package root means main.py and the tests need no changes after the split.
"""

from app.core.database import engine  # noqa: F401  (tests reassign planner.engine)

from .routes import router  # noqa: F401
from .state import _new_memory, _PROPOSAL_FILE_MARKER  # noqa: F401
from .proposal import (  # noqa: F401
    _strip_proposal_text,
    _try_extract_proposal,
    _ProposalJsonStripper,
    _MemoryTagStripper,
    _A2UITagStripper,
    _extract_memory_update,
    _extract_a2ui_request,
    _build_short_summary,
)
from .prompts import _resolve_model, _build_system_prompt_with_memory, resolve_effective_skills  # noqa: F401
