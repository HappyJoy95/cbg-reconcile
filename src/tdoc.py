"""兼容旧导入路径；实现位于 :mod:`src.integrations.tdoc`。"""
import sys

from .integrations import tdoc as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "tdoc", _implementation)
