import sys
from pathlib import Path

# Add repo root to sys.path so tests can import both "tools.foo" (package-style)
# and "import foo" (direct-style, used by test_budget_check.py et al.)
sys.path.insert(0, str(Path(__file__).parent))
