# quark-auto-save · kkzdzc 定制版（fnOS 原生应用）

在 [Cp0204/quark-auto-save](https://github.com/Cp0204/quark-auto-save)（AGPL-3.0）基础上做的定制分支，目标是把「夸克网盘自动转存」变成**飞牛 fnOS 的原生应用**，并让它**能自己更新**，不用每次重新打包镜像。

## 相比上游的改动

功能层：

- **链接失效自动换链**：任务链接失效时，按任务名走 PanSou / CloudSaver 聚合搜索，逐个验证 `get_stoken` + `get_detail`，取第一个有效链接替换并继续转存；支持历史失效任务的自愈，失败仍会标记。
- **WebUI 免登录开关**：`WEBUI_AUTH=off`（默认，与定制版行为一致）/ `on`（恢复账号密码校验）。
- **内部更新**：WebUI 侧边栏检查 / 下载 / 原子替换 / 失败回滚 / 自动重启。
- 一些容错：自定义通知的去反引号与 `\n` 处理、PanSou 超时重试、时间解析兼容、移动端布局。

交付层：

- `fnos/`：fnOS 应用工程（manifest、生命周期脚本、权限、安装向导、桌面入口）。
- `scripts/build_fpk.py`：打包 + 自检（`manifest.checksum = md5(app.tgz)`、路径越界、必需文件）。
- `.github/workflows/build-fpk.yml`：推 tag 自动构建 x86/arm 包并发布 Release。

## 安装（fnOS）

应用中心「手动安装」选择 `.fpk`，或：

```bash
appcenter-cli install-fpk quark-auto-save_1.0.0_x86.fpk
```

安装向导会问：WebUI 登录校验（on/off）、更新源 `owner/repo`、更新通道、pip 镜像。安装时创建 venv 并装依赖，约 1–2 分钟。

| 项 | 位置 |
|---|---|
| 端口 | 安装时可选（默认 `5005`），被占用会提示建议端口 |
| 配置 | `/var/apps/quark-auto-save/var/config/quark_config.json` |
| 日志 | `/var/apps/quark-auto-save/var/app.log`、`install.log` |
| 生命周期 | `cmd/main start\|stop\|status`（未运行返回 3） |

> 默认 `WEBUI_AUTH=off`：能访问 5005 就能改任务和 Cookie。只在内网暴露，或在安装向导里选 `on`。


## 端口占用怎么处理

安装向导第一步就问「服务端口」（默认 5005），`install_init` 会当场检查：

- 端口被占用 → 安装**直接失败**，提示占用进程与建议端口（向上找 50 个），用户回上一步改端口重装；
- 端口非法（非数字 / 不在 1024-65535）→ 同样失败并提示；
- 端口可用 → 写入 `var/env.sh`，同时改写 `ui/config` 的 `port`，让桌面图标指向实际端口；
- `manifest.checkport=false`：系统不再拿固定的 5005 去卡，改由我们自己的脚本判断。

装好后想换端口：应用设置里改（`config_callback` 同样校验），然后**停止再启动**生效。

启动时也会再查一次：端口被占则拒绝启动并把原因写进日志与界面，避免半启动状态。

## 内部更新

```
检查 → 下载 → 解压校验 → 备份 → 原子替换 → 写 build.json → 退出码 75 → 外壳重启
```

配置在 `quark_config.json` 的 `update` 段：

```json
"update": {
  "repo": "tianlmm/quark-auto-save",
  "mode": "auto",          // auto | release | tag | branch | url
  "branch": "main",
  "url": "",
  "install_deps": true,
  "preserve_local": true,
  "proxy": ""
}
```

- `mode=auto`：优先 Release，其次 Tag，最后分支。
- 替换范围是白名单（`app/`、`notify.py`、`quark_auto_save.py`、`plugins/`、`requirements.txt`、`skills/`），配置目录永不触碰。
- 远端缺少本地定制（`auto_repair_shareurl` 或 `app/updater.py`）时拒绝覆盖，需显式强制。
- 任何异常从 `.update/backup-<ts>/` 回滚，只保留最近 2 份备份。

## 构建

```bash
python3 scripts/build_fpk.py --src . --pkg fnos --out dist \
    --version 1.0.0 --arch x86 --tag v1.0.0 --sha $(git rev-parse --short HEAD)
```

## 发版流程

```bash
# 1. 改版本号（两处需一致）
echo 1.0.1 > VERSION
sed -i 's/^version .*/version                    = 1.0.1/' fnos/manifest

# 2. 提交并打 tag
git add -A && git commit -m "chore: bump 1.0.1"
git tag v1.0.1
git push origin main --tags
```

推送 tag 后，Actions 会：

1. 校验 `tag` 与 `VERSION` 一致（不一致直接失败，避免发错版本）；
2. 构建 `x86` / `arm` 两个 fpk，校验归档结构；
3. 发布 Release 并附 `.fpk`、`.sha256`；
4. 自检 Release 资产是否可下载（内部更新走的就是这条链路）。

之后应用内点「检查更新」即可拿到新版本。

## 已知限制

- `plugins/fnv_refresh_v2*.so` 是 alpine(musl) 编译产物，fnOS 是 glibc，加载会失败并打印 `libc.musl-...: cannot open shared object file`，属预期，不影响主功能。
- 更新依赖 GitHub 可达；网络受限时用 `update.proxy` 或 `mode=url` 指向自建地址。
- 端口固定 5005，与其他服务冲突需先改 `manifest.service_port` 与 `ui/config` 的 `port`。

## 许可

上游为 AGPL-3.0，本仓库同样以 AGPL-3.0 分发，保留原始 `LICENSE` 与作者信息；上游说明见 `UPSTREAM-README.md`。
