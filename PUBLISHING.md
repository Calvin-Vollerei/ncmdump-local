# 发布到 GitHub 的步骤

仓库已初始化并完成首次提交，**本地已就绪**。以下三步把它推到 GitHub。

## 1. 在 GitHub 建一个空仓库

网页上 New repository → 填名字（例如 `ncmdump-local`）→ **不要**勾选 "Add a README / .gitignore / license"
（本仓库已经有）→ Create。

## 2. 关联远程并推送

```bash
cd ncm2mp3
git remote add origin https://github.com/<你的用户名>/ncmdump-local.git
git branch -M main
git push -u origin main
```

## 3. 推送后要改的几处占位符

`pyproject.toml` 里的 `OWNER` 需要换成你的用户名（否则 PyPI 的 Homepage/Issues 链接是错的）：

```toml
[project.urls]
Homepage = "https://github.com/OWNER/ncmdump-local"
Repository = "https://github.com/OWNER/ncmdump-local"
Issues = "https://github.com/OWNER/ncmdump-local/issues"
```

README 里如果写了具体用户名也一并替换。

---

## 推送前自查（都可直接运行）

```bash
# 1) 测试与 lint
python -m pytest -q
python -m ruff check src tests

# 2) 卫生守卫：媒体 / 产物 / 凭据 / 机器路径
python scripts/check_hygiene.py

# 3) 确认真正会被提交的文件
git ls-files

# 4) 确认没有敏感内容混入提交历史
git log --stat -1
```

`git ls-files` 应当只列出 21 个文件（源码、测试、文档、CI 配置），
**不应**出现 `.ncm` / `.flac` / `.lrc` / 图片 / `.exe` / `.dll` / `.log` / `__pycache__`。

## 常见坑

| 现象 | 原因 |
| --- | --- |
| 推送后 CI 立刻红 | 多半是 `ruff check` 有新增告警，或卫生守卫扫到了新加的文件 |
| 检出后行尾变成 CRLF | 已由 `.gitattributes`（`eol=lf`）固定；若仓库是旧的，重新 `git add --renormalize .` |
| `pip install .` 失败 | 检查 `README.md` 与 `src/ncmdump/py.typed` 是否还在（`pyproject.toml` 引用了它们） |
| EXE 构建报 `script not found` | PyInstaller 会 chdir 到 spec 所在目录，必须先 `cd src` 再执行 spec |

## 关于两份内部审查报告

开发过程中的泄密审查与依赖审查报告（含本机路径与开发过程细节）**不在仓库内**，
保存在仓库外的 `ncm-work/audit-reports/`。它们的结论已经落到仓库里：
`.gitignore`、`LICENSE`、`NOTICE.md`、`SECURITY.md`、CI 守卫，以及 GUI 的日志脱敏与
`NCM_OUT_DIR` 改动。不要把那两份原始报告提交上去。
