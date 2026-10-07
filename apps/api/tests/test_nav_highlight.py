"""导航高亮的防回归检查（2026-10-05）。

起因：`activeNav` 原来是一串写死的 `if` + `return 'projects'` 兜底，
新加的「客户档案」(`/customers`) 没被列进去 → **点它亮的却是「项目与报价」**。

这里做两层检查：
  ① **覆盖性**：路由里每个页面都必须能被某个导航项（或别名）匹配到
     —— 以后再加页面忘了登记，这条会失败；
  ② **映射正确性**：把期望的高亮关系钉死（含两个别名页面）。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
LAYOUT = ROOT / "apps/web/src/layouts/AppLayout.vue"
ROUTER = ROOT / "apps/web/src/router/index.ts"


def _nav_paths() -> set[str]:
    src = LAYOUT.read_text(encoding="utf-8")
    return set(re.findall(r"path: '(/[^']*)'", src))


def _nav_aliases() -> dict[str, str]:
    src = LAYOUT.read_text(encoding="utf-8")
    block = re.search(r"const NAV_ALIASES[^=]*=\s*\{(.*?)\}", src, re.S)
    assert block, "找不到 NAV_ALIASES（别把它删了：别名页面靠它高亮）"
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"'(/[^']*)':\s*'([^']+)'", block.group(1))
    }


def _child_routes() -> list[str]:
    """layout 下的子路由（相对路径），取每条的**首段**。"""
    src = ROUTER.read_text(encoding="utf-8")
    rel = list(re.findall(r"path: '([^'/][^']*)'", src))
    segs = []
    for p in rel:
        seg = p.split("/")[0]
        if seg and seg not in segs:
            segs.append(seg)
    return segs


def test_every_page_maps_to_a_nav_item() -> None:
    """① 覆盖性：每个页面都要能高亮到某个导航项。"""
    paths = _nav_paths()
    aliases = _nav_aliases()
    missing = []
    for seg in _child_routes():
        url = f"/{seg}"
        if url in paths or url in aliases:
            continue
        # 嵌套路由（如 projects/xxx/yyy）按前缀归到父导航项
        if any(url.startswith(f"{p}/") for p in paths):
            continue
        missing.append(url)
    assert not missing, (
        f"这些页面没有对应的导航项/别名，点进去会亮错或全不亮：{missing}。"
        "要么在 AppLayout 的 navs 里加一项，要么在 NAV_ALIASES 里登记它属于哪个入口。"
    )


# 路径 → 导航 key（与 AppLayout 里的定义一致；改了导航项要同步这里）
NAV_KEY_BY_PATH = {
    "/customers": "customers",
    "/projects": "projects",
    "/devices": "devices",
    "/labor": "labor",
    "/point-templates": "points",
    "/reports": "reports",
    "/settings": "settings",
}


def test_active_nav_mapping_is_pinned() -> None:
    """② 映射正确性：用与前端同口径的算法，把期望结果钉死。"""
    paths = sorted(_nav_paths(), key=len, reverse=True)
    aliases = _nav_aliases()

    def active(path: str) -> str:
        for alias, key in aliases.items():
            if path == alias or path.startswith(f"{alias}/"):
                return key
        for p in paths:
            if path == p or path.startswith(f"{p}/"):
                # 需要反查 key：这里用路径到 key 的映射表
                return NAV_KEY_BY_PATH[p]
        return ""

    cases = {
        "/customers": "customers",                      # ← 这次的 bug
        "/customers/CUS-0001": "customers",
        "/projects": "projects",
        "/projects/abc/workbench/xyz": "projects",      # 嵌套页面归到项目
        "/devices": "devices",
        "/suppliers": "devices",                        # 别名：设备库工具栏入口
        "/labor": "labor",
        "/point-templates": "points",
        "/reports": "reports",
        "/settings": "settings",
        "/users": "settings",                           # 别名：用户管理并入系统设置
        "/not-a-page": "",                              # 认不出来就不高亮（不要乱亮）
    }
    wrong = {k: (active(k), v) for k, v in cases.items() if active(k) != v}
    assert not wrong, f"导航高亮映射不符（得到, 期望）：{wrong}"
