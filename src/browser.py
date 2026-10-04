"""兼容旧导入路径；实现位于 :mod:`src.integrations.browser`。"""
import sys

from .integrations import browser as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "browser", _implementation)
