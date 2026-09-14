# -*- coding: utf-8 -*-
"""NCM 本地转换器（纯 Python，无第三方依赖）。

从 www.ncm2mp3.com 的网页端算法（/js/decrypt.js）还原，逐字节对拍验证通过。
"""

from .metadata import APP_NAME, DEVELOPER, RELEASES_URL, REPO_URL, VERSION

__version__ = VERSION

from .ncm_core import (  # noqa: E402  (after __version__ so metadata stays dependency-free)
    NcmError,
    NcmMeta,
    NcmResult,
    decrypt_file,
    decrypt_stream,
    sniff_format,
)

__all__ = [
    "__version__",
    "APP_NAME",
    "DEVELOPER",
    "REPO_URL",
    "RELEASES_URL",
    "NcmError",
    "NcmMeta",
    "NcmResult",
    "decrypt_file",
    "decrypt_stream",
    "sniff_format",
]
