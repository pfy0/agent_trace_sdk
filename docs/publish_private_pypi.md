# 私有 PyPI 发布与接入（C 方案）

目标：把 `agent-trace-sdk` 发到公司内网的 Nexus / Artifactory，业务方**一次配好源**，
之后 `pip install agent-trace-sdk` 一行搞定，版本也由仓库统一管理。

```
开发机 / CI                    私有 PyPI                    业务 Agent 环境
────────────                   ─────────                    ───────────────
python tools/release.py  ──►   pypi-hosted 仓库      ◄──     pip install agent-trace-sdk
（build + twine upload）        （存 tar.gz / whl）           （读 ~/.pip/pip.conf）
```

---

## 一、服务端准备（运维/管理员做一次）

### 1. Nexus 3

1. 建 **hosted** 类型的 pypi 仓库，名字如 `pypi-hosted`。
   - 路径一般自动是 `https://nexus.公司域名/repository/pypi-hosted/`。
2. 建一个部署用的账号（或复用 CI 账号），授予该仓库的 `nx-repository-view-pypi-*-add/edit`。
3. 业务方读取权限授予 `nx-repository-view-pypi-*-read` 即可。

### 2. Artifactory

1. 建 local 类型 pypi 仓库，如 `pypi-local`。
2. 地址形如 `https://artifactory.公司域名/artifactory/api/pypi/pypi-local`。
3. 生成一个 deployment token / 专用账号密码。

> 关键点：**部署地址**和**安装地址**可能不一样。
> - 上传（twine）用仓库根地址，Nexus 要带结尾斜杠；
> - 安装（pip）用 index 地址，Artifactory 常需 `/simple` 后缀。
> 两边以实际仓库页面「Set Me Up」给出的为准。

---

## 二、发布端配置（SDK 维护者）

### 方式 A：环境变量（推荐，适合 CI，密码不落盘）

```powershell
$env:AGENT_TRACE_PYPI_URL      = "https://nexus.公司域名/repository/pypi-hosted/"
$env:AGENT_TRACE_PYPI_USER     = "deploy"
$env:AGENT_TRACE_PYPI_PASSWORD = "******"
python tools/release.py
```

`tools/release.py` 会：清目录 → `python -m build` → `twine check` → `twine upload`。

只想本地出包不上传：

```powershell
python tools/release.py --build-only
```

### 方式 B：`~/.pypirc`（适合个人开发机）

Windows 路径是 `C:\Users\<你>\.pypirc`，Linux/macOS 是 `~/.pypirc`：

```ini
[distutils]
index-servers =
    private

[private]
repository = https://nexus.公司域名/repository/pypi-hosted/
username = deploy
password = ******
```

然后：

```powershell
python tools/release.py --repository private
```

> `.pypirc` 里是明文密码，注意文件权限，别提交到仓库（本仓库 `.gitignore` 已忽略相关本地文件）。

### 先在 TestPyPI 演练

正式发私有仓库前，可以先发公网 TestPyPI 验证包结构：

```powershell
$env:AGENT_TRACE_PYPI_URL      = "https://test.pypi.org/legacy/"
$env:AGENT_TRACE_PYPI_USER     = "__token__"
$env:AGENT_TRACE_PYPI_PASSWORD = "pypi-******"
python tools/release.py
```

---

## 三、业务方安装（一行搞定）

### 1. 配置 pip 源（一次性）

**全局配置**（Windows：`%APPDATA%\pip\pip.ini`；Linux/macOS：`~/.pip/pip.conf` 或 `~/.config/pip/pip.conf`）：

```ini
[global]
index-url = https://nexus.公司域名/repository/pypi-hosted/simple
trusted-host = nexus.公司域名
extra-index-url = https://pypi.org/simple
```

- `index-url` 指向私有源的 `/simple` 页面（Nexus 的 hosted pypi 会暴露 `.../pypi-hosted/simple`）。
- `extra-index-url` 保留公网源，方便拉 `pydantic` 等第三方依赖。
  若走内网代理仓库（group），可以把它设成 index-url，去掉 extra。
- 内网自签证书时才需要 `trusted-host`；正式的 https 证书建议不要加，避免降级风险。

**临时用一次**：

```bash
pip install agent-trace-sdk ^
  --index-url https://nexus.公司域名/repository/pypi-hosted/simple ^
  --trusted-host nexus.公司域名
```

### 2. 安装

```bash
pip install agent-trace-sdk              # 核心（pydantic）
pip install "agent-trace-sdk[xlsx]"      # 需要 Excel 导出
pip install "agent-trace-sdk[all]"       # 全部可选依赖
```

### 3. 验证

```bash
python -c "import agent_trace; print(agent_trace.__version__, agent_trace.__file__)"
```

---

## 四、版本与记录

- 版本号在 `pyproject.toml` 的 `[project] version`，`tools/release.py` 会自动读取，不要在两处维护。
- 建议语义化：`0.1.0` → `0.1.1`（修 bug）→ `0.2.0`（加能力）→ `1.0.0`（契约稳定）。
- 同一个版本号不能重复上传，仓库会拒绝；改完版本再发，或用 `--skip-existing` 跳过。
- `schema/trace.schema.json` 与 `schema/enums.json` 是跨语言契约，随包一起发（打包时已包含）。
  业务方或 Java 版实现以这两个文件为准。

## 五、离线/无网环境

内网机器完全不通公网时，用离线安装包：

```bash
# 有网机器上，把 SDK 和依赖一起下载
pip download agent-trace-sdk --dest ./wheels ^
  --index-url https://nexus.公司域名/repository/pypi-hosted/simple

# 内网机器上离线安装
pip install --no-index --find-links=./wheels agent-trace-sdk
```

或者最简单：直接把 `tools/release.py --build-only` 产出的
`dist/agent_trace_sdk-<版本>-py3-none-any.whl` 拷进内网，
`pip install ./agent_trace_sdk-<版本>-py3-none-any.whl` 即可（核心依赖仍需 pydantic）。
