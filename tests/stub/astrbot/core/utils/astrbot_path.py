from __future__ import annotations

import os
import tempfile


def get_astrbot_data_path() -> str:
    return os.environ.get("STUB_ASTRBOT_DATA", tempfile.gettempdir())