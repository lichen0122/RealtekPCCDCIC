import os
import sys

# 讓 tests/ 內可以 `import agent`(agent.py 在上一層 WebUtility/)。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
