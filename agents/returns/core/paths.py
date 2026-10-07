"""Where the agent's own data lives (reference documents, prompts)."""
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = AGENT_DIR / "reference"
PROMPTS_DIR = AGENT_DIR / "prompts"
