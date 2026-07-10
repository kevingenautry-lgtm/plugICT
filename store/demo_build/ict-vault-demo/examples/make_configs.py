"""Write the Claude Desktop config with THIS folder's real path.

Run once after creating the .venv (see README). Safe to re-run if you move
the folder.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ('.venv/Scripts/python.exe' if sys.platform == 'win32' else '.venv/bin/python')
SERVER = ROOT / 'mcp_server.py'
EXAMPLES = Path(__file__).resolve().parent

cfg = {"mcpServers": {"ict-vault-demo": {"command": str(VENV_PY), "args": [str(SERVER)]}}}
(EXAMPLES / 'claude_desktop_config.json').write_text(json.dumps(cfg, indent=2) + "\n")
print(f"Demo config written for: {ROOT}")
