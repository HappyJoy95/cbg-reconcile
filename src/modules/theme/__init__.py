"""**主题和壁纸模块** —— 「有哪些主题、前端那几个文件自不自洽」。

## ⚠ 这个模块**不是**主题引擎（别照着别处那套建 Registry）

`.dsh/docs/2026-09-19-主题架构-执行规范.md` 第七节把机制定死了：

> 本项目要的等价物就三样：`theme.css` + 一个 `body[data-theme]` + 一行 `localStorage`。
> **不建 Registry / Manager / Storage / Events / Adapter 六件套。**

⭐ **一主题一文件**（用户 2026-09-22）：配色住在 `web/themes/<名>.css`，
加载时整目录引入。加主题 = 丢一个文件 + `index.html` 链一行。

| 出口 | 干什么 | 为什么值得有 |
|---|---|---|
| `names()` / `themes()` | 从 `web/themes/*.css` **扫**出有哪些主题 | 加主题只加文件，这里自动跟上 —— **不产生第二份真相** |
| `files()` / `check()` | 前端文件在不在、配色有没有排在 `style.css` 前、每个主题文件有没有链上 | 启动自检要能一眼看出"页面怎么白板了 / 少链一个主题" |
| `wallpapers()` | 壁纸目录里有哪些图（口径 + 扩展名白名单） | 壁纸是**门店自己的文件**，不能进 `NEVER_TOUCH` 那套覆盖逻辑 |

⚠ **切换的 UI 和状态是前端的事**（`localStorage`，见规范 7.2）——
本模块**不提供 `/api/theme`**：多一条后端接口就多一处要和 `localStorage` 对账的状态。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

#: 前端就这四个文件（无构建步骤 ⇒ 浏览器直接加载，少一个页面就白板）
FILES = ("index.html", "theme.css", "style.css", "app.js")

#: 主题配色目录 —— **一主题一文件**（文件名 = 主题名）
THEMES_DIR = "themes"

#: ⚠ 顺序是**硬要求**：主题块靠层叠顺序压过组件规则，
#:   配色排到 `style.css` 后面，深色主题会被组件的 background 盖回去。
#:   加载顺序：`theme.css`（共享非配色）→ `themes/*.css`（配色）→ `style.css`。
ORDER = ("theme.css", "style.css")

#: 壁纸放这儿（在 `web/` 下 ⇒ 后端 `_static()` 原样发，**一行都不用改**）
WALLPAPER_DIR = "web/wallpaper"
WALLPAPER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
#: 单张上限 —— 背景图不是相册，5MB 足够手机原图裁过的；再大是拖首屏
MAX_WALLPAPER = 5 * 1024 * 1024
#: 照片主题的 key（`body[data-theme="photo"]` + `themes/photo.css`）
PHOTO_THEME = "photo"

#: 中文名兜底（文件里写了 `/* label: … */` 就用文件里的）
LABELS = {
    "default": "默认（浅色）",
    "dark": "深色",
    "celadon": "青瓷绿",
    "dusk": "暮山蓝 × 晚桃粉（深色）",
    "pome": "石榴红",
    "pine": "松针绿",
    "mist": "雾霭紫",
    "bean": "豆沙粉",
    "butter": "酪黄",
    "shiqing": "石青",
    "daiqiu": "黛秋",
    "zhusha": "朱砂",
    "zhuqing": "竹青",
    "taotu": "陶土",
    "dianye": "靛夜（深色）",
    "photo": "自定义照片主题",
}

#: 暗色主题名单 —— 其余一律 light
#: dusk 是暮山蓝×晚桃粉、dianye 是靛蓝×夜航灰（用户 2026-09-22 点名）
DARK = frozenset({"dark", "dusk", "dianye"})


def web_dir(root=None) -> Path:
    from ...paths import ROOT
    return (Path(root) if root else ROOT) / "web"


def themes_dir(root=None) -> Path:
    return web_dir(root) / THEMES_DIR


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        return ""


def _strip_css(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def theme_files(root=None) -> List[Path]:
    """`web/themes/` 下的配色文件（一主题一文件）。"""
    d = themes_dir(root)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".css")


def label_of(path: Path) -> str:
    """文件里的 `/* label: 中文名 */`，没写就用文件名的兜底表。"""
    m = re.search(r"/\*\s*label:\s*([^*]+?)\s*\*/", _read(path))
    if m:
        return m.group(1).strip()
    return LABELS.get(path.stem, path.stem)


def names(root=None) -> List[str]:
    """这台程序**认识**哪些主题 —— 文件名就是主题名。

    从 `web/themes/*.css` 扫而不是在 Python 里列一份：**加主题只加一个文件**，
    两份名单迟早对不上（而"界面上能选、切了没反应"最难查）。
    ⚠ `default` 永远在第一位（`:root` 基线，对应"不写 data-theme"）。
    """
    out = ["default"]
    for p in theme_files(root):
        n = p.stem
        if n not in out:
            out.append(n)
    return out


def themes(root=None) -> List[dict]:
    """主题清单（名字 + 中文名 + 明暗）—— 界面拿它渲染选择器。"""
    out = []
    by_stem = {p.stem: p for p in theme_files(root)}
    for n in names(root):
        p = by_stem.get(n)
        label = label_of(p) if p else LABELS.get(n, n)
        out.append({"name": n, "label": label,
                    "kind": "dark" if n in DARK else "light"})
    return out


def files(root=None) -> List[dict]:
    d = web_dir(root)
    out = []
    for f in FILES:
        p = d / f
        out.append({"name": f, "exists": p.is_file(),
                    "bytes": p.stat().st_size if p.is_file() else 0})
    for p in theme_files(root):
        out.append({"name": "%s/%s" % (THEMES_DIR, p.name), "exists": True,
                    "bytes": p.stat().st_size})
    return out


def order_ok(root=None) -> tuple:
    """`theme.css` / `themes/*.css` 有没有排在 `style.css` 前面 —— `(过没过, 为什么)`。

    ⚠ 只认 **`href` 里的路径** —— 注释里也会写 `style.css`，
      拿裸文件名 `find` 会匹到说明文字上，把真实 link 判成"排在后面"。
    """
    html = _read(web_dir(root) / "index.html")
    if not html:
        return False, "index.html 读不出来"

    def href_pos(name):
        """`href="…name…"` 的位置；认相对/绝对两种写法。"""
        for key in ("/" + name, 'href="%s"' % name, "href='%s'" % name,
                    'href="/%s"' % name):
            i = html.find(key)
            if i >= 0:
                return i
        # themes/ 下的文件
        return -1

    pos = {}
    for f in ORDER:
        i = href_pos(f)
        if i < 0:
            return False, "index.html 里根本没引 %s" % f
        pos[f] = i
    if pos[ORDER[0]] > pos[ORDER[1]]:
        return False, "%s 排在了 %s 后面 —— 换主题会被组件规则盖回去" % ORDER
    style_at = pos["style.css"]
    late = []
    for p in theme_files(root):
        rel = "%s/%s" % (THEMES_DIR, p.name)
        i = href_pos(rel)
        if i < 0:
            late.append("%s（没链）" % p.name)
        elif i > style_at:
            late.append("%s（排在 style.css 后）" % p.name)
    if late:
        return False, "主题配色没排在 style.css 前面：" + "、".join(late)
    return True, ""


def _unlinked_themes(root=None) -> List[str]:
    """`themes/` 里有文件、但 `index.html` 没链的那些。"""
    html = _read(web_dir(root) / "index.html")
    miss = []
    for p in theme_files(root):
        rel = "%s/%s" % (THEMES_DIR, p.name)
        if ("/" + rel) in html or rel in html:
            continue
        miss.append(p.name)
    return miss


def wallpaper_dir(root=None) -> Path:
    return (Path(root) if root else _root()) / WALLPAPER_DIR


def safe_wallpaper_name(name) -> str:
    """只认**纯文件名** + 白名单扩展名 —— 穿越 / 脚本 / 怪后缀一律拒。

    `_static()` 按文件原样发：`.svg` 能带脚本，路径穿越能写到 `web/` 外。
    ⚠ **不做 basename 洗白**：名字里带 `/` `\\` `..` 直接拒 ——
      洗成"看起来安全"的名字会让人以为接口收了那条路径（测过 `../x.png`）。
    """
    raw = str(name or "").strip()
    if not raw or raw in (".", ".."):
        raise ValueError("文件名不对")
    if any(c in raw for c in '/\\') or ".." in raw:
        raise ValueError("文件名不能带路径")
    if any(c in raw for c in '<>:"|?*') or "\x00" in raw:
        raise ValueError("文件名含非法字符")
    ext = Path(raw).suffix.lower()
    if ext not in WALLPAPER_EXTS:
        raise ValueError("只认 %s（收到 %s）" % ("、".join(WALLPAPER_EXTS), ext or "无扩展名"))
    return raw


def save_wallpaper(root, name, data: bytes) -> dict:
    """把一张图写进 `web/wallpaper/`（**只管文件**，不碰主题选中态）。"""
    safe = safe_wallpaper_name(name)
    if not data:
        raise ValueError("图片内容是空的")
    if len(data) > MAX_WALLPAPER:
        raise ValueError("图片 %d 字节，超过上限 %d 字节" % (len(data), MAX_WALLPAPER))
    d = wallpaper_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    target = (d / safe).resolve()
    # resolve 后必须仍在壁纸目录里（safe 名几乎挡死了，这是第二道）
    if target.parent != d.resolve():
        raise ValueError("路径不对")
    target.write_bytes(data)
    return {"name": safe, "bytes": len(data), "ok": True,
            "url": "/wallpaper/" + safe}


def delete_wallpaper(root, name) -> dict:
    """删一张壁纸。**选中哪张只在前端**（浏览器本地存储），后端不管。"""
    safe = safe_wallpaper_name(name)
    p = wallpaper_dir(root) / safe
    if not p.is_file():
        return {"ok": False, "error": "没有这张图：%s" % safe, "name": safe}
    try:
        p.unlink()
    except OSError as e:
        return {"ok": False, "error": "删不掉 %s：%s" % (safe, e), "name": safe}
    return {"ok": True, "name": safe}


def wallpapers(root=None) -> List[dict]:
    """壁纸目录里的图（**门店自己放的**，随包不发）。

    只认白名单里的扩展名：`_static()` 是按文件原样发的，
    丢个 `.svg` 进去等于把一份能带脚本的文件挂到站点上 —— 不认。
    """
    d = wallpaper_dir(root)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name != ".gitkeep":
            out.append({"name": p.name, "bytes": p.stat().st_size,
                        "ok": p.suffix.lower() in WALLPAPER_EXTS,
                        "url": "/wallpaper/" + p.name})
    return out


def _root():
    from ...paths import ROOT
    return ROOT


def check(root=None) -> dict:
    """启动自检那一项：**前端文件自不自洽**（不联网、不渲染）。

    ⚠ 只静态看：文件在不在、配色顺序对不对、每个主题文件有没有链上、壁纸扩展名认不认。
      "页面长什么样"是浏览器的事，这里判不了也不该判。
    """
    items = []
    miss = [f["name"] for f in files(root) if not f["exists"]]
    items.append({"key": "web-files", "name": "前端文件", "ok": not miss,
                  "why": "" if not miss else "缺文件：%s（页面会白板）" % "、".join(miss),
                  "need": "控制台打不开 / 样式错乱"})
    tfs = theme_files(root)
    items.append({"key": "theme-files", "name": "主题配色文件",
                  "ok": bool(tfs) and (themes_dir(root) / "default.css").is_file(),
                  "why": "" if tfs and (themes_dir(root) / "default.css").is_file()
                  else "web/themes/ 下要有 default.css 和各主题文件",
                  "need": "换主题没颜色 / 只剩组件默认样式"})
    unlinked = _unlinked_themes(root)
    items.append({"key": "theme-linked", "name": "主题已链入页面",
                  "ok": not unlinked,
                  "why": "" if not unlinked else "index.html 没链：%s" % "、".join(unlinked),
                  "need": "那个主题在清单里但切了没反应"})
    ok_order, why_order = order_ok(root)
    items.append({"key": "theme-order", "name": "主题样式顺序", "ok": ok_order,
                  "why": why_order, "need": "换主题不生效（深色主题被浅色盖回去）"})
    bad_wp = [w["name"] for w in wallpapers(root) if not w["ok"]]
    items.append({"key": "wallpaper", "name": "壁纸", "ok": not bad_wp,
                  "why": "" if not bad_wp else "不认的格式：%s" % "、".join(bad_wp),
                  "need": "那张图不会被当成壁纸（只认 %s）"
                          % "、".join(WALLPAPER_EXTS)})
    bad = [i for i in items if not i["ok"]]
    return {"items": items, "ok": not bad,
            "themes": themes(root),
            "why": "；".join(i["why"] for i in bad if i["why"])}
