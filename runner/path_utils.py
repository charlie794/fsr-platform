from __future__ import annotations
"""
Shared path utilities used by both TestRunnerWorker and GridRunner.

Keeping these here means a bug fix or path-search change only needs to
happen in one place rather than being duplicated across both runner classes.
"""

import os
from typing import Optional

try:
    from Sensor_Testor.domain.models import store
except Exception:
    try:
        from domain.models import store  # type: ignore
    except Exception:
        store = None  # type: ignore


def resolve_criteria_path(filename: Optional[str]) -> Optional[str]:
    """
    Resolve a criteria filename to an absolute path by searching in order:
      1. Alongside the plan file
      2. In a pass_fail_criteria_files/ subdirectory next to the plan
      3. In the output folder
      4. In a pass_fail_criteria_files/ subdirectory in the output folder
      5. As a straight os.path.abspath() of the filename itself

    Returns the first existing path found, or None.
    """
    if not filename:
        return None
    name = str(filename).strip().strip('"').strip("'")
    if not name:
        return None

    candidates: list[str] = []

    plan_path = getattr(store, "plan_path", None) if store else None
    if plan_path:
        d = os.path.dirname(plan_path)
        candidates += [
            os.path.join(d, name),
            os.path.join(d, "pass_fail_criteria_files", name),
            os.path.join(d, "pass_fail_criteria_files", os.path.basename(name)),
        ]

    out_dir = getattr(store, "output_folder", None) if store else None
    if out_dir:
        candidates += [
            os.path.join(out_dir, name),
            os.path.join(out_dir, "pass_fail_criteria_files", name),
            os.path.join(out_dir, "pass_fail_criteria_files", os.path.basename(name)),
        ]

    candidates.append(os.path.abspath(name))

    for p in candidates:
        try:
            if p and os.path.isfile(p):
                return p
        except Exception:
            continue
    return None
