from pathlib import Path
import sys


ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = ROOT / "skills" / "jnby-news-watch" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
