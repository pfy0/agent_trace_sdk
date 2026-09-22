"""构建并发布到公司私有 PyPI（Nexus / Artifactory）。

设计目标：一次配置，之后业务方 `pip install agent-trace-sdk` 一行搞定。

用法：
    # 1) 只构建，不发布（本地校验）
    python tools/release.py --build-only

    # 2) 构建并上传（从环境变量读地址与凭证）
    set AGENT_TRACE_PYPI_URL=https://nexus.example.com/repository/pypi-hosted/
    set AGENT_TRACE_PYPI_USER=deploy
    set AGENT_TRACE_PYPI_PASSWORD=******
    python tools/release.py

    # 3) 上传到 TestPyPI 先验证一遍
    python tools/release.py --repository testpypi

凭证也可写在 ~/.pypirc 里（twine 原生支持），环境变量优先级更高。
详见 docs/publish_private_pypi.md。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# 私有仓库地址与凭证统一用这三个环境变量，避免把密码写进代码/仓库
ENV_URL = "AGENT_TRACE_PYPI_URL"
ENV_USER = "AGENT_TRACE_PYPI_USER"
ENV_PASSWORD = "AGENT_TRACE_PYPI_PASSWORD"

# twine 里已注册好的仓库别名（写在 ~/.pypirc 时用）
NAMED_REPOSITORIES = {"pypi": "pypi", "testpypi": "testpypi"}


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
    """twine check：校验元数据，防止上传后被仓库拒收。"""
    run([sys.executable, "-m", "twine", "check", *map(str, sorted(DIST.glob("*")))])


def upload(repository: str, skip_existing: bool) -> None:
    cmd = [sys.executable, "-m", "twine", "upload", "--non-interactive"]

    # 优先级：环境变量（私有仓库） > ~/.pypirc 里的别名
    url = os.environ.get(ENV_URL)
    if url:
        cmd += ["--repository-url", url]
        user = os.environ.get(ENV_USER)
        password = os.environ.get(ENV_PASSWORD)
        if user:
            cmd += ["--username", user]
        if password:
            cmd += ["--password", password]
    elif repository in NAMED_REPOSITORIES:
        cmd += ["--repository", NAMED_REPOSITORIES[repository]]
    else:
        cmd += ["--repository", repository]

    if skip_existing:
        cmd += ["--skip-existing"]

    cmd += [str(p) for p in sorted(DIST.glob("*"))]
    run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并发布 agent-trace-sdk")
    parser.add_argument("--build-only", action="store_true", help="只构建，不上传")
    parser.add_argument("--no-clean", action="store_true", help="不清理 dist/ 与 build/")
    parser.add_argument(
        "--repository",
        default="private",
        help="twine 仓库别名：private（默认，读环境变量）/ testpypi / pypi",
    )
    parser.add_argument("--skip-existing", action="store_true", help="版本已存在时跳过而非报错")
    args = parser.parse_args()

    version = read_version()
    print("agent-trace-sdk 版本: %s" % version)

    if not args.no_clean:
        clean()
    build()
    check()

    if args.build_only:
        print("\n构建完成（未上传）。产物在 %s" % DIST)
        return 0

    if not os.environ.get(ENV_URL) and args.repository not in NAMED_REPOSITORIES:
        print(
            "\n未设置 %s，且 --repository=%s 不是内置别名。\n"
            "请设置 %s / %s / %s，或配置 ~/.pypirc。\n"
            "详见 docs/publish_private_pypi.md" % (ENV_URL, args.repository, ENV_URL, ENV_USER, ENV_PASSWORD),
            file=sys.stderr,
        )
        return 2

    upload(args.repository, args.skip_existing)
    print("\n发布完成：agent-trace-sdk==%s" % version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
