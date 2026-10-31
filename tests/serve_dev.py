"""Run the API and UI on their own, with a separate data folder, for testing in a browser."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("AEGIS_HOME", str(Path(tempfile.gettempdir()) / "aegis_dev_home"))
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.argv = ["aegis", "--serve"]

from aegis.__main__ import main  # noqa: E402

main()
