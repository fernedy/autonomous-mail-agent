"""Pytest configuration — sets TESTING=1 before any test code runs.

This ensures that ``src.web.limiter`` replaces the rate limiter's
``.limit()`` with a no-op before any route modules are imported,
preventing slowapi decorators from wrapping route handlers.
"""

import os

os.environ["TESTING"] = "1"
