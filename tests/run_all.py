"""跑全部测试：python tests/run_all.py [-q]

不依赖 pytest，只用标准库 unittest。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main() -> int:
    quiet = "-q" in sys.argv
    loader = unittest.TestLoader()
    suite = loader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    if loader.errors:
        for err in loader.errors:
            print(err, file=sys.stderr)
        return 1

    result = unittest.TextTestRunner(verbosity=1 if quiet else 2).run(suite)
    print()
    print("=" * 60)
    print(f"共 {result.testsRun} 个用例 | 失败 {len(result.failures)} | 错误 {len(result.errors)} "
          f"| 跳过 {len(result.skipped)}")
    if not result.wasSuccessful():
        for case, tb in result.failures + result.errors:
            print(f"\n--- {case}\n{tb}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())