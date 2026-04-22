import sys
from pathlib import Path

# 測試時以專案根為 sys.path，讓 routes.api / shared 可直接 import。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
