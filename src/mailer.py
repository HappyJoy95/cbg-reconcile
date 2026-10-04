"""兼容旧导入路径；实现位于 :mod:`src.integrations.mailer`。"""
import sys

from .integrations import mailer as _implementation

sys.modules[__name__] = _implementation
setattr(sys.modules[__package__], "mailer", _implementation)
