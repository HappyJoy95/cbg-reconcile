"""兼容旧 Web 导入路径；HTTP 控制台实现位于 :mod:`src.http`。"""
import sys

from . import http as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "web", _implementation)
