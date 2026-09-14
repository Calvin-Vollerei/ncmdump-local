# 发布到 GitHub 的步骤

仓库已初始化并完成提交，**本地已就绪**。以下三步把它推到 GitHub。

代码里登记的位置（`src/ncmdump/metadata.py`，也是界面"检查更新"要用的地址）：

```
仓库  https://github.com/Calvin-Vollerei/ncmdump-local
发布  https://github.com/Calvin-Vollerei/ncmdump-local/releases
```

如果实际用户名/仓库名不是这个，**改 `metadata.py` 顶部那两行**即可，
`REPO_URL` / `RELEASES_URL` / `API_LATEST_RELEASE` 都是由它们推导出来的；
同时把 `pyproject.toml` 的 `[project.urls]` 一起改掉。

## 1. 在 GitHub 建一个空仓库

网页上 New repository → 名字填 `ncmdump-local` → **不要**勾选 "Add a README / .gitignore / license"
（本仓库已经有）→ Create。

## 2. 关联远程并推送

```bash
cd ncm2mp3
git remote add origin https://github.com/Calvin-Vollerei/ncmdump-local.git
git branch -M main
git push -u origin main
```

## 3. 发第一个 Release（"检查更新"按钮才有东西可查）

在仓库页面 → Releases → Draft a new release：

1. **Tag** 填 `v1.0.0`（必须与 `metadata.py` 里的 `VERSION` 同源；前缀 `v` 可有可无）；
2. 标题随意，说明里写更新日志；
3. 附件可上传打包好的 `NCMConverter` 压缩包；
4. Publish。

之后客户端点「检查更新」会请求
`https://api.github.com/repos/Calvin-Vollerei/ncmdump-local/releases/latest`，
对比 `tag_name` 与本地 `VERSION`：

* 更新版本 → 显示「有新版本 vX.Y.Z」并给出链接；
* 相同或更低 → 「已是最新版本」；
* 仓库不存在 / 没网 / 被限流 → 「检查失败」并给 Releases 页面链接（**不会报错崩溃**）。

> 公开仓库的该接口无需 token，但有速率限制（未认证约 60 次/小时/IP）。
> 私有仓库需要 token，本项目的检查不支持，请改用「手动查看 Releases」。

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
