"""`schtasks` 的小封装 —— 坑集中在一个地方。

**为什么要有这个模块**：定时执行（`schedule.py`）和开机自启（`autostart.py`）
都要调 `schtasks`，也都得处理同样两个坑。抄两份的话，修好一处另一处还留着。

两个坑：

1. **输出编码不稳**。中文 Windows 上可能是 UTF-16（带 BOM、隔一个字节一个 `\\x00`），
   也可能是 GBK。无脑按 UTF-8 解就是乱码，而且不报错 —— 静默解析失败。
2. **字段名本地化**。`/fo LIST` 输出的是「下次运行时间」还是 `Next Run Time`
   取决于系统语言。**永远不要解析它** —— 只用：
   - `/fo CSV /nh`：第一列固定是任务名，跟语言无关；
   - `/xml`：标签名是固定英文（`StartBoundary` / `Command` / `RunLevel`）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

# Windows 任务计划 XML 的命名空间 —— 标签名固定英文，不受系统语言影响
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


# 起**控制台程序**时带上它 —— 不要控制台窗口。
#
# ⚠ 为什么必须有：服务是 `pythonw.exe` 跑的（本身**没有**控制台）。这时再起一个
#   控制台子进程（schtasks / tasklist / python …），Windows 会**给它新开一个控制台
#   窗口** —— 用户看到的就是"操作的时候弹出来的黑窗口"。
#   任务列表每 30 秒刷一次、每次好几个 schtasks，不处理的话会一直闪。
CREATE_NO_WINDOW = 0x08000000


def quiet_kwargs() -> dict:
    """`subprocess` 的"别弹窗"参数（非 Windows 上是空的）。

    ⚠ 判断用 `os.name == "nt"` 而不是 `platform.system()`：后者是个**模块属性**，
      测试里 `mock.patch.object(某个模块.platform, "system", ...)` 会**全局生效** ——
      结果在 macOS 上也塞了 creationflags，subprocess 直接 ValueError。
      `os.name` 既便宜又不会被这种 patch 误伤。
    """
    if os.name != "nt":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


def decode(raw) -> str:
    """把 `schtasks` 的输出解成字符串。

    按**字节内容**判断编码，不猜：UTF-16 的文本前 200 字节里一定有 `\\x00`。
    依次退到 GBK —— 中文任务名在 GBK 控制台上就是这么出来的。
    """
    if not raw:
        return ""
    if isinstance(raw, str):          # 测试里直接塞字符串
        return raw
    if b"\x00" in raw[:200]:
        for enc in ("utf-16", "utf-16-le"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def schtasks(args: list, timeout: int = 20):
    """跑一条 schtasks 命令。**返回 bytes**，交给 `decode()` 处理。

    找不到 schtasks、或者超时被杀 → 返回 None（调用方按"失败"处理，
    不要抛出去 —— 门店电脑上宁可降级也不要崩）。
    """
    exe = shutil.which("schtasks") or "schtasks"
    try:
        return subprocess.run([exe, *args], capture_output=True, timeout=timeout,
                              **quiet_kwargs())
    except (OSError, ValueError, subprocess.SubprocessError):
        # ValueError：平台/参数不对时 subprocess 会抛这个。这个包装函数的约定是
        # **永不抛异常**，失败就返回 None 让调用方降级。
        return None


def strip_bom(text: str) -> str:
    return text.lstrip("\ufeff")


def parse_xml(raw) -> ET.Element | None:
    """解析 `schtasks /query /xml` 的输出，**去掉 XML 声明**。

    ⚠ 声明的 encoding 可能是 `UTF-16`，而我们已经把字节解成 str 了 ——
    带着声明去 `fromstring` 会报 "encoding declaration in Unicode string"。
    """
    text = strip_bom(decode(raw))
    if not text.strip():
        return None
    text = re.sub(r"<\?xml[^>]*\?>", "", text, count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def xml_text(root: ET.Element | None, path: str) -> str:
    """取第一个匹配元素的文本；没有就返回空串。

    `path` 可以是一段路径，比如 `"Settings/Enabled"` —— 任务 XML 里有**两个**
    `<Enabled>`（触发器里一个、设置里一个），只按标签名找会拿到错的那个。
    """
    if root is None:
        return ""
    steps = "/".join(f"{NS}{s}" for s in path.split("/"))
    el = root.find(f".//{steps}")
    return (el.text or "").strip() if el is not None else ""
