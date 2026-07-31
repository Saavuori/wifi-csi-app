import sys
from pathlib import Path

# csi_node.py is a script that gets copied onto a Pi, not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
