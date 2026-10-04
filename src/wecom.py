"""兼容旧导入路径；实现位于 :mod:`src.integrations.wecom`。"""
import sys

from .integrations import wecom as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "wecom", _implementation)
