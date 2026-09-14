# -*- coding: utf-8 -*-
"""NCM 本地转换器（纯 Python，无第三方依赖）。

从 www.ncm2mp3.com 的网页端算法（/js/decrypt.js）还原，逐字节对拍验证通过。
"""

__version__ = "1.0.0"

from .ncm_core import NcmError, NcmMeta, NcmResult, decrypt_file, decrypt_stream, sniff_format

__all__ = [
    "__version__",
    "NcmError",
    "NcmMeta",
    "NcmResult",
    "decrypt_file",
    "decrypt_stream",
    "sniff_format",
]
