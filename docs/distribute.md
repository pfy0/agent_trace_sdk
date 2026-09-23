# 包的获取与发布

包通过 **GitHub 私有仓库**分发，业务方用 `pip install git+...` 安装。

```
SDK 维护者                          GitHub 私有仓库                业务方环境
──────────                          ──────────────                ─────────
python tools/release.py --tag  ──►  tag v0.4.2 + main      ◄──     pip install "git+...@v0.4.2"
（打 tag 并 push）                  （存放源码）                   （需要读权限）
```

好处：不用额外基础设施；发一个版本就是推一个 tag。
代价：业务方机器要能访问 GitHub，且需要仓库读权限（或 Token）。

---

## 一、发布新版本（SDK 维护者）

### 1. 改版本号

`pyproject.toml` 里的 `[project] version`，例如 `0.1.0` → `0.1.1`。
语义化建议：修 bug → 第三位；加能力 → 第二位；契约（字段结构）变更 → 第一位。

### 2. 提交改动

```powershell
git add -A
git commit -m "修复 xxx"
git push origin main
```

### 3. 打 tag 并推送（一条命令）

```powershell
python tools/release.py --tag
```

脚本会：清理构建目录 → `python -m build` → `twine check` → **推送当前分支** →
打 `v<version>` 注释 tag → `git push origin v<version>`。

> 先推分支再推 tag：保证 tag 指向的提交在远端分支上可达，避免出现
> 「tag 存在但 main 落后」的错位（业务方按 tag 装到的版本与分支不一致，排查困难）。
> 分支已同步时会自动跳过推送。

只想本地演练：`python tools/release.py --tag --no-push`。
只出包不上传：`python tools/release.py --build-only`（产物在 `dist/`）。

> 也可手动：`git tag -a v0.1.1 -m "..."` + `git push origin v0.1.1`。
> **tag 名必须和 `pyproject.toml` 的版本号对应**，否则业务方装到的版本号和 tag 对不上，排查困难。

### 4. 需要连续发版本时

同一个 tag 不能覆盖。改完版本号重新 `--tag` 即可；若确实要重打，先
`git tag -d v0.1.1`（本地）和 `git push origin :refs/tags/v0.1.1`（远端）。

---

## 二、业务方安装

### 前置条件

1. 机器能访问 GitHub（公司网络若有代理，先配好 `git config --global http.proxy` 之类）。
2. 机器上装了 `git`（pip 从 git 安装需要 git 命令行）。
3. 对你的仓库有**读权限**。

### 方式 1：SSH（有配好 SSH key 时最省心）

```bash
pip install "git+ssh://git@github.com/pfy0/agent_trace_sdk.git@v0.4.1"
```

### 方式 2：HTTPS + Personal Access Token

去 GitHub → Settings → Developer settings → Personal access tokens 生成一个
带 `repo` 读权限的 Token，然后：

```bash
pip install "git+https://<TOKEN>@github.com/pfy0/agent_trace_sdk.git@v0.4.1"
```

> Token 会出现在命令历史/pip 日志里，注意别外泄；用环境变量或凭据管理器更安全。

### 方式 3：配好 git 凭据后直接用短地址

```bash
git config --global credential.helper manager     # 首次会弹窗登录
pip install "git+https://github.com/pfy0/agent_trace_sdk.git@v0.4.1"
```

### 装可选依赖

`git+...` 的地址后面可以带 extras：

```bash
pip install "agent-trace-sdk[xlsx] @ git+https://github.com/pfy0/agent_trace_sdk.git@v0.4.1"
pip install "agent-trace-sdk[all] @ git+ssh://git@github.com/pfy0/agent_trace_sdk.git@v0.4.1"
```

### 升级 / 换版本

`@v0.4.1` 换成目标 tag 重新装即可（pip 会重装）：

```bash
pip install --force-reinstall "git+https://github.com/pfy0/agent_trace_sdk.git@v0.1.1"
```

想看最新未发版：把 `@v0.1.1` 换成 `@main`（不推荐用于生产，版本不确定）。

### 验证

```bash
python -c "import agent_trace; print(agent_trace.__version__, agent_trace.__file__)"
```

---

## 三、DeerFlow 这类项目怎么装

DeerFlow 后端用的是 `uv`，语法一致：

```bash
uv pip install --python .venv/Scripts/python.exe "git+https://github.com/pfy0/agent_trace_sdk.git@v0.4.1"
```

或写进它的 `pyproject.toml` 依赖里，长期固定版本：

```toml
dependencies = [
    "agent-trace-sdk @ git+https://github.com/pfy0/agent_trace_sdk.git@v0.4.1",
]
```

---

## 四、离线 / 内网不通 GitHub 时

让维护者出一份 wheel，拷过去直接装（不依赖网络和 git）：

```powershell
# 维护者
python tools/release.py --build-only
# 产物：dist/agent_trace_sdk-0.1.0-py3-none-any.whl
```

```bash
# 业务方
pip install ./agent_trace_sdk-0.1.0-py3-none-any.whl
```

若目标机器连 PyPI 也不通，还需要把依赖一起带过去：

```bash
pip download agent-trace-sdk --no-deps --dest ./wheels   # 有网机器上，用于取依赖
pip install --no-index --find-links=./wheels ./agent_trace_sdk-0.1.0-py3-none-any.whl
```

---
