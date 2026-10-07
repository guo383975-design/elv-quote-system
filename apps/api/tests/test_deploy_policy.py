"""部署策略的防回归检查（2026-10-05）。

## 起因（真实事故）

用户执行「恢复出厂设置」清空了数据；十几分钟后我为修前端跑了次 `deploy.sh`，
它的第 5 步**无条件**执行 `seed.py`，而 seed 的"幂等"是"已存在则跳过" ——
于是演示数据在空库上被全量重建。用户看到的就是"我清掉的数据自己回来了"。

这里把结论钉死：**部署脚本不允许无条件写入演示数据**。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy/deploy.sh"


def test_deploy_does_not_seed_demo_data_unconditionally() -> None:
    src = DEPLOY.read_text(encoding="utf-8")
    assert "scripts/seed.py" in src, "部署脚本里找不到种子步骤（结构变了？）"

    lines = src.splitlines()
    seed_lines = [i for i, ln in enumerate(lines) if "scripts/seed.py" in ln]
    assert seed_lines, "找不到调用 seed.py 的那一行"

    for idx in seed_lines:
        # 往前找 12 行，必须能看到显式开关
        window = "\n".join(lines[max(0, idx - 12) : idx])
        assert "SEED_DEMO" in window, (
            "deploy.sh 里的 seed.py 调用没有被 SEED_DEMO 开关保护 —— "
            "这会让「恢复出厂设置」清掉的数据在下次部署时自己长回来"
        )


def test_deploy_keeps_existing_env() -> None:
    """部署不能覆盖已存在的 apps/api/.env（否则 JWT_SECRET 被重置，所有人掉线）。"""
    src = DEPLOY.read_text(encoding="utf-8")
    assert re.search(r'if \[ -f "\$API/\.env" \]', src), (
        "找不到「.env 已存在则保留」的判断；.env 被覆盖会重置 JWT_SECRET"
    )


def test_docs_explain_demo_data_is_opt_in() -> None:
    """文档要写清"演示数据默认不种"，否则运维会以为装完就该有数据。"""
    ai = (ROOT / "AI_INSTALL.md").read_text(encoding="utf-8")
    assert "SEED_DEMO" in ai, "AI_INSTALL 未说明演示数据需要 SEED_DEMO=1 才写入"


def test_deploy_scripts_use_lf_line_endings() -> None:
    """部署脚本/配置必须是 LF 换行。

    在 Windows 上用 Python 的 write_text() 改这些文件时，它会默认把 \n 翻译成
    \r\n —— 服务器上 bash 直接拒绝执行（`set: pipefail: invalid option name`、
    `syntax error near unexpected token`），而且**部署看起来"跑了"其实没跑**。
    这条断言把这类问题挡在提交前。
    """
    d = ROOT / "deploy"
    bad = [
        p.name
        for p in sorted(d.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in {".sh", ".conf", ".service", ".timer"}
        and b"\r\n" in p.read_bytes()
    ]
    assert not bad, f"这些部署文件是 CRLF 换行，服务器上 bash/systemd 会出问题：{bad}"


def test_deploy_reinstalls_when_dependencies_change() -> None:
    """新增依赖时，部署必须重装 —— 否则服务器构建报 "Cannot find module"。

    真实情况：这次给 AI 回答加 markdown 渲染引入了 markdown-it，
    而 deploy.sh 原来只在 node_modules **不存在**时才 npm install，
    于是新依赖永远装不上（部署者只会看到一句莫名其妙的构建错误）。
    """
    src = (ROOT / "deploy/deploy.sh").read_text(encoding="utf-8")
    assert ".package-lock.json" in src, (
        "deploy.sh 没有比对 package.json / node_modules/.package-lock.json，"
        "新增依赖后不会重装"
    )
    assert "package.json -nt" in src and "package-lock.json -nt" in src, (
        "依赖变化的判据不完整（应以 package.json/lock 的时间戳为准）"
    )


def test_deploy_files_are_lf_only() -> None:
    """deploy/ 下交给 bash / systemd 的文件必须是纯 LF（2026-10-05 第二次踩坑）。

    在 Windows 上用 Python 重写文件时，`write_text()` 默认会把 `\n` 翻成 `\r\n`，
    bash 见到 `\r` 直接语法错（exit=2）—— deploy.sh 与 backup.sh 都这么坏过一次。
    这条测试让"第三次"不可能悄悄发生。
    """
    import pathlib

    deploy = pathlib.Path(__file__).resolve().parents[3] / "deploy"
    offenders = []
    for p in sorted(deploy.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".sh", ".service") or p.name in ("deploy.sh", "backup.sh"):
            if b"\r\n" in p.read_bytes():
                offenders.append(p.name)
    assert not offenders, f"这些文件是 CRLF，bash/systemd 会出错：{offenders}"


def test_pack_excludes_dumps_and_backups() -> None:
    """交付包不许含数据库转储/备份目录（2026-10-05 真实事故）。

    事故：默认备份目录曾在仓库里（`<repo>/backups-offsite`），于是 3 份
    **带业务数据**的 .dump 被打进了交付 zip ✗。修了两处：默认目录挪到用户主目录、
    打包规则改成按**危险类别**排除。

    这里做**行为断言**（直接加载打包模块看它最终的集合），而不是断言源码里
    出现某个字符串 —— 后者被格式化换行搞挂过一次。
    """
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "_pack_release_for_test", root / "deploy" / "pack_release.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 有 __main__ 守卫，导入不会真的打包

    for suffix in (".dump", ".sql", ".bak", ".sqlite", ".db"):
        assert suffix in mod.EXCLUDE_SUFFIX, f"打包规则里少了 {suffix}"
    for name in ("backups", "backups-offsite", "quote-backups"):
        assert name in mod.EXCLUDE_DIRS, f"打包规则里少了目录 {name}"
    # 白名单里的设计文档不能被误伤
    assert "数据库设计.sql" in mod.EXCLUDE_SUFFIX_ALLOW
