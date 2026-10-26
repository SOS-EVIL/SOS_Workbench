# Release

SOS_Workbench 采用 **源码发布**：GitHub Release 附源码 zip，使用者 `pip install -r requirements.txt` 后运行。

## 发布前检查

1. 版本号只改一处：[`config/version.py`](../config/version.py)。
2. 更新 [`CHANGELOG.md`](../CHANGELOG.md)。
3. 全量测试：

   ```bash
   python test/conformance.py
   python test/integration_phase2.py
   python test/inheritance.py
   python test/cli_smoke.py
   python test/cli_advanced.py
   ```

4. GUI 启动冒烟：

   ```bash
   python main.py
   ```

5. 确认工作区不包含：
   - `.sos_api_key.enc` / `.sos_cipher_key`
   - `storage/` 里的真实项目、凭据、客户数据
   - `.idea/`、`__pycache__/`、本地临时脚本
   - 截图中的 API Key、本地绝对路径、真实 target

## 打 tag

```bash
git add -A
git commit -m "release: SOS_Workbench 0.1.0"
git tag -a v0.1.0 -m "SOS_Workbench 0.1.0"
git push origin main
git push origin v0.1.0
```

## 生成源码包

```bash
git archive --format=zip --prefix=SOS_Workbench-0.1.0/ -o SOS_Workbench-0.1.0.zip v0.1.0
```

不要直接 `Compress-Archive` 整个工作目录；用 `git archive` 才会排除 `.gitignore` 中的本地数据。

## GitHub Release

推送 `v*` tag 后，[`release.yml`](../.github/workflows/release.yml) 会：

1. 跑测试；
2. 用 `git archive` 生成源码 zip；
3. 创建 GitHub Release 并上传 zip。

也可以手工：

```bash
gh release create v0.1.0 SOS_Workbench-0.1.0.zip \
  --title "SOS_Workbench 0.1.0" \
  --notes-file CHANGELOG.md
```

## 截图

- `docs/screenshot-exec.png`
- `docs/screenshot-chain.png`

要求：窗口最大化、无 API Key、无本地绝对路径、无真实目标。
