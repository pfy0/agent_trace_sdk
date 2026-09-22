"""发布 agent-trace-sdk：git tag 方式。

业务方直接从 GitHub 私有仓库安装：

    pip install "git+https://github.com/pfy0/agent_trace_sdk.git@v0.1.0"

所以「发布」= 打一个版本 tag 并推上去。用法：

    # 1) 本地出包自检（dist/ 下得到 whl 与 sdist）
    python tools/release.py --build-only

    # 2) 打 tag 并推送（版本号取自 pyproject.toml，默认 v<version>）
    python tools/release.py --tag

    # 3) 只打 tag 不推送（本地演练）
    python tools/release.py --tag --no-push

详见 docs/distribute.md。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def read_version() -> str:
    """从 pyproject.toml 读版本号，避免两处维护。

    优先用标准库 tomllib（3.11+）；3.9/3.10 退回正则，避免额外依赖。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10
        tomllib = None  # type: ignore

    if tomllib is not None:
        return tomllib.loads(text)["project"]["version"]

    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml 里找不到 version")
    return match.group(1)


def run(cmd: list, **kwargs) -> None:
    print("$ " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, cwd=str(ROOT), **kwargs)
    except subprocess.CalledProcessError as exc:
        # 不打印整段 traceback，只保留可读的失败信息
        raise SystemExit("命令失败（退出码 %d）：%s" % (exc.returncode, cmd[0]))


def capture(cmd: list) -> str:
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("命令失败：%s\n%s" % (" ".join(cmd), result.stderr.strip()))
    return result.stdout.strip()


def clean() -> None:
    for name in ("dist", "build"):
        path = ROOT / name
        if path.is_dir():
            shutil.rmtree(path)
    for egg in ROOT.glob("src/*.egg-info"):
        shutil.rmtree(egg, ignore_errors=True)


def build() -> None:
    run([sys.executable, "-m", "build"])


def check() -> None:
    """twine check：校验元数据，保证别人装得上。"""
    run([sys.executable, "-m", "twine", "check", *map(str, sorted(DIST.glob("*")))])


def make_tag(tag: str, push: bool) -> None:
    """打带注释的版本 tag（git 安装的版本锚点），可选推送。"""
    existing = capture(["git", "tag", "--list", tag])
    if existing:
        raise SystemExit("tag %s 已存在。改 pyproject.toml 的版本号，或先删除该 tag。" % tag)

    dirty = capture(["git", "status", "--porcelain"])
    if dirty:
        print("提示：工作区有未提交改动，tag 指向的是最后一次提交：\n%s\n" % dirty)

    head = capture(["git", "rev-parse", "--short", "HEAD"])
    run(["git", "tag", "-a", tag, "-m", "agent-trace-sdk %s" % tag])
    print("已打 tag %s（指向 %s）" % (tag, head))

    if not push:
        print("未推送（--no-push）。推送后业务方才能安装该版本。")
        return
    run(["git", "push", "origin", tag])
    print("已推送 tag %s" % tag)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 / 打 tag 发布 agent-trace-sdk")
    parser.add_argument("--build-only", action="store_true", help="只构建本地包，不打 tag")
    parser.add_argument("--tag", action="store_true", help="打版本 tag 并推送（正式发布）")
    parser.add_argument("--tag-name", default=None, help="自定义 tag 名，默认 v<version>")
    parser.add_argument("--no-push", action="store_true", help="打 tag 但不推送")
    parser.add_argument("--no-clean", action="store_true", help="不清理 dist/ 与 build/")
    args = parser.parse_args()

    version = read_version()
    tag = args.tag_name or ("v%s" % version)
    print("agent-trace-sdk 版本: %s" % version)

    if not args.no_clean:
        clean()
    build()
    check()

    if args.build_only:
        print("\n构建完成。产物在 %s" % DIST)
        return 0

    if args.tag:
        make_tag(tag, push=not args.no_push)
        print("\n完成。业务方安装：")
        print('  pip install "git+https://github.com/pfy0/agent_trace_sdk.git@%s"' % tag)
        return 0

    print("\n已构建，但未指定动作。请加 --tag 打版本 tag。详见 docs/distribute.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
