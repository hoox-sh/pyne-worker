"""Pynescript runtime glue vendored from pynescript/backend.

The installable ``pynescript`` wheel does not include ``backend/``; this
package holds the bar-loop runtime used by pyne-worker. Keep in sync with
``pynescript/backend/{runtime,evaluator,series}.py``.
"""

from .runtime import Runtime

__all__ = ["Runtime"]