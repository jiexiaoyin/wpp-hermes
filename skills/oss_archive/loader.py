"""OSS Archive skill (loader)."""
import sys
from pathlib import Path

_SKILLS_DIR = Path("/root/.hermes/skills")
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

import oss_archive
from oss_archive import (
    upload_bytes,
    upload_file,
    upload_media_to_oss,
    build_oss_key,
    build_key,
    register_accounts,
    AccountNotAllowedError,
)

__all__ = [
    "upload_bytes", "upload_file", "upload_media_to_oss",
    "build_oss_key", "build_key",
    "register_accounts", "AccountNotAllowedError",
]