# -*- coding: utf-8 -*-
"""内部更新：从 GitHub 检查并就地更新代码，无需重建镜像 / 重装 fpk。

设计要点
- 只替换代码路径（白名单），永远不动 config/ 数据目录。
- 切换前先备份到 .update/backup-<ts>/，失败自动回滚。
- 更新成功后以退出码 75 结束进程，由 cmd/main（fnOS）或 restart 策略（Docker）拉起。
- 默认开启“定制保护”：如果远端代码里没有本地新增的 auto_repair 功能，必须 force 才覆盖，
  避免把本地定制版一键刷成上游原版。
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse

import requests

# 更新成功后用于通知外壳重启的退出码
RESTART_EXIT_CODE = 75

# 允许被远端覆盖的代码路径（相对项目根目录）
CODE_PATHS = [
    "app",
    "notify.py",
    "quark_auto_save.py",
    "plugins",
    "requirements.txt",
    "skills",
]

# 这些本地定制特征若在远端缺失，视为“会丢失定制”
SIGNATURES = [
    ("auto_repair_shareurl", "链接失效自动换链"),
]

# 更新时必须保留的本地路径（远端包通常没有：fnOS 桌面入口等）
PRESERVE_PATHS = ["app/ui", "service.sh"]

DEFAULT_CONFIG = {
    "repo": "tianlmm/quark-auto-save",
    "mode": "auto",  # auto | release | tag | branch | url
    "branch": "main",
    "url": "",  # mode=url 时直接指定 tar.gz 地址
    "api_base": "https://api.github.com",
    "download_base": "https://codeload.github.com",
    "proxy": "",
    "install_deps": True,
    "preserve_local": True,
    "timeout": 30,
}


class UpdateError(Exception):
    pass


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _ver_tuple(text):
    """把 v1.2.3 / 1.2.3-beta 解析成可比较的元组。"""
    if not text:
        return ()
    m = re.search(r"(\d+(?:\.\d+)*)", str(text))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _is_newer(latest, current):
    lt, ct = _ver_tuple(latest), _ver_tuple(current)
    if not lt or not ct:
        return True  # 判定不了就交给用户决定
    return lt > ct


class Updater:
    def __init__(self, root, config_getter=None, logger=None):
        self.root = os.path.abspath(root)
        self.config_getter = config_getter or (lambda: {})
        self.log = logger or (lambda msg: print(f"[updater] {msg}"))
        self._lock = threading.Lock()
        self._busy = False

    # ------------------------------------------------------------------ 配置
    def config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            remote = (self.config_getter() or {}).get("update") or {}
        except Exception:
            remote = {}
        user = {}
        if isinstance(remote, dict):
            user = {k: v for k, v in remote.items() if k in cfg and v not in (None, "")}
        cfg.update(user)
        # 环境变量只在配置文件未显式设置该项时生效，避免向导写入的 env 反过来压掉 WebUI 配置
        for key, env in (
            ("repo", "UPDATE_REPO"),
            ("mode", "UPDATE_MODE"),
            ("branch", "UPDATE_BRANCH"),
            ("url", "UPDATE_URL"),
            ("proxy", "UPDATE_PROXY"),
        ):
            if key not in user and os.environ.get(env):
                cfg[key] = os.environ[env]
        cfg["repo"] = str(cfg["repo"]).strip().strip("/")
        return cfg

    # ------------------------------------------------------------------ 状态
    def current_version(self):
        build = _read_json(os.path.join(self.root, "build.json"))
        tag = (build.get("BUILD_TAG") or os.environ.get("BUILD_TAG") or "").strip()
        sha = (build.get("BUILD_SHA") or os.environ.get("BUILD_SHA") or "").strip()
        if tag in ("dev", "main"):  # 源码形态的占位值，交给 VERSION 判定
            tag = ""
        if tag.startswith("v"):
            version = tag
        elif tag:
            version = f"{tag}({sha[:7]})" if sha else tag
        else:
            # 源码形态（无 build.json）时退回 VERSION 文件，便于与 release tag 比较
            ver_file = os.path.join(self.root, "VERSION")
            try:
                with open(ver_file, encoding="utf-8") as f:
                    v = f.read().strip()
            except Exception:
                v = ""
            version = f"v{v}" if v else "dev"
        return {"version": version, "build_tag": tag or version, "build_sha": sha}

    def status(self):
        cur = self.current_version()
        cfg = self.config()
        return {
            "version": cur["version"],
            "build_tag": cur["build_tag"],
            "build_sha": cur["build_sha"],
            "repo": cfg["repo"],
            "mode": cfg["mode"],
            "branch": cfg["branch"],
            "busy": self._busy,
            "last_backup": self._last_backup(),
        }

    # ------------------------------------------------------------------ 检查
    def _http_json(self, url, cfg):
        proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg.get("proxy") else None
        kw = {"timeout": cfg["timeout"], "proxies": proxies}
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "quark-auto-save-updater"}
        token = os.environ.get("UPDATE_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.get(url, headers=headers, **kw)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise UpdateError(f"{url} 返回 HTTP {r.status_code}")
        return r.json()

    def _resolve_latest(self, cfg):
        """按 mode 解析最新版本；auto 时依次尝试 release → tag → branch。"""
        api = cfg["api_base"].rstrip("/")
        repo = cfg["repo"]
        modes = [cfg["mode"]] if cfg["mode"] != "auto" else ["release", "tag", "branch"]

        if cfg["mode"] == "url":
            if not cfg["url"]:
                raise UpdateError("mode=url 但没有配置 update.url")
            return {"tag": os.path.basename(cfg["url"]).replace(".tar.gz", ""), "version": "",
                    "notes": "自定义更新源", "source": "url", "url": cfg["url"], "sha": ""}

        for mode in modes:
            try:
                if mode == "release":
                    data = self._http_json(f"{api}/repos/{repo}/releases/latest", cfg)
                    if not data:
                        continue
                    tag = data.get("tag_name") or ""
                    return {
                        "tag": tag,
                        "version": tag,
                        "notes": (data.get("body") or "").strip(),
                        "source": "release",
                        "url": f"{cfg['download_base'].rstrip('/')}/{repo}/tar.gz/refs/tags/{tag}",
                        "sha": "",
                    }
                if mode == "tag":
                    data = self._http_json(f"{api}/repos/{repo}/tags?per_page=1", cfg)
                    if not data:
                        continue
                    tag = (data[0].get("name") or "")
                    return {
                        "tag": tag,
                        "version": tag,
                        "notes": "",
                        "source": "tag",
                        "url": f"{cfg['download_base'].rstrip('/')}/{repo}/tar.gz/refs/tags/{tag}",
                        "sha": (data[0].get("commit") or {}).get("sha", ""),
                    }
                if mode == "branch":
                    data = self._http_json(f"{api}/repos/{repo}/commits/{cfg['branch']}", cfg)
                    if not data:
                        continue
                    sha = data.get("sha") or ""
                    return {
                        "tag": f"{cfg['branch']}@{sha[:7]}",
                        "version": "",
                        "notes": (data.get("commit", {}).get("message") or "").strip(),
                        "source": "branch",
                        "url": f"{cfg['download_base'].rstrip('/')}/{repo}/tar.gz/refs/heads/{cfg['branch']}",
                        "sha": sha,
                    }
            except UpdateError:
                raise
            except requests.RequestException as e:
                raise UpdateError(f"无法访问 GitHub：{e}")
        raise UpdateError(f"仓库 {repo} 没有可用的 release/tag/branch")

    def check(self):
        cfg = self.config()
        cur = self.current_version()
        latest = self._resolve_latest(cfg)
        target_ver = latest.get("version") or latest.get("tag")
        can_update = _is_newer(target_ver, cur["build_tag"] or cur["version"])
        if latest.get("source") == "branch" and latest.get("sha") == cur["build_sha"]:
            can_update = False
        return {
            "current": cur["version"],
            "latest": target_ver,
            "can_update": can_update,
            "source": latest.get("source"),
            "notes": (latest.get("notes") or "")[:4000],
            "url": latest.get("url"),
            "repo": cfg["repo"],
            "restart_exit_code": RESTART_EXIT_CODE,
        }

    # ------------------------------------------------------------------ 应用
    def apply(self, force=False):
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "已有更新任务在执行"}
        self._busy = True
        try:
            return self._apply(force)
        except UpdateError as e:
            self.log(f"更新失败：{e}")
            return {"success": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            self.log(f"更新异常：{e}")
            return {"success": False, "message": f"更新异常：{e}"}
        finally:
            self._busy = False
            self._lock.release()

    def _apply(self, force):
        cfg = self.config()
        cur = self.current_version()
        latest = self._resolve_latest(cfg)
        target = latest.get("version") or latest.get("tag")
        if not force and not _is_newer(target, cur["build_tag"] or cur["version"]):
            raise UpdateError(
                f"当前版本 {cur['version']} 已不低于远端 {target}，无需更新（强制重装请勾选强制更新）"
            )
        self.log(f"准备从 {cur['version']} 更新到 {target}（{latest.get('source')}）")

        blob = self._download(latest["url"], cfg)
        stage = self._extract(blob)
        try:
            self._validate(stage)
            lost = self._lost_signatures(stage)
            if lost and cfg["preserve_local"] and not force:
                raise UpdateError(
                    "远端版本缺少本地定制：" + "、".join(lost) + "。如确认要覆盖，请勾选强制更新。"
                )

            backup = self._backup()
            try:
                self._swap(stage)
                self._restore_preserved(backup)
                self._write_build(latest)
                deps_ok, deps_msg = (True, "")
                if cfg["install_deps"]:
                    deps_ok, deps_msg = self._pip_install()
                self.log(f"更新完成：{target}")
                return {
                    "success": True,
                    "message": f"已更新到 {target}" + ("" if deps_ok else f"（依赖安装失败：{deps_msg}）"),
                    "from": cur["version"],
                    "to": target,
                    "backup": backup,
                    "restart_required": True,
                    "lost": lost,
                }
            except Exception:
                self._rollback(backup)
                raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _download(self, url, cfg):
        proxies = {"http": cfg["proxy"], "https": cfg["proxy"]} if cfg.get("proxy") else None
        self.log(f"下载 {url}")
        try:
            r = requests.get(url, timeout=max(cfg["timeout"], 60), proxies=proxies, stream=True)
        except requests.RequestException as e:
            raise UpdateError(f"下载失败：{e}")
        if r.status_code != 200:
            raise UpdateError(f"下载失败：HTTP {r.status_code}")
        buf = bytearray()
        for chunk in r.iter_content(1024 * 256):
            if chunk:
                buf.extend(chunk)
        if not buf:
            raise UpdateError("下载内容为空")
        return bytes(buf)

    def _extract(self, blob):
        """解压到 .update/stage-<ts>/，拒绝越界路径，并把顶层目录剥掉。"""
        upd_dir = os.path.join(self.root, ".update")
        os.makedirs(upd_dir, exist_ok=True)
        stage = os.path.join(upd_dir, f"stage-{int(time.time())}")
        os.makedirs(stage, exist_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
                members = tar.getmembers()
                for m in members:
                    name = m.name
                    if name.startswith("/") or ".." in name.split("/"):
                        raise UpdateError(f"压缩包含非法路径：{name}")
                    if m.issym() or m.islnk():
                        raise UpdateError(f"压缩包含链接，已拒绝：{name}")
                    if not (m.isfile() or m.isdir()):
                        raise UpdateError(f"压缩包含特殊文件，已拒绝：{name}")
                tar.extractall(stage, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(stage)
        except UpdateError:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(stage, ignore_errors=True)
            raise UpdateError(f"解压失败：{e}")

        # 剥掉 GitHub 的 repo-tag/ 顶层目录
        entries = [e for e in os.listdir(stage) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(stage, entries[0])):
            inner = os.path.join(stage, entries[0])
            for name in os.listdir(inner):
                shutil.move(os.path.join(inner, name), os.path.join(stage, name))
            os.rmdir(inner)
        return stage

    def _validate(self, stage):
        need = ["quark_auto_save.py", os.path.join("app", "run.py")]
        missing = [n for n in need if not os.path.exists(os.path.join(stage, n))]
        if missing:
            raise UpdateError("更新包结构不完整，缺少：" + "、".join(missing))

    def _lost_signatures(self, stage):
        lost = []
        local_src = os.path.join(self.root, "quark_auto_save.py")
        remote_src = os.path.join(stage, "quark_auto_save.py")
        try:
            local_text = open(local_src, encoding="utf-8", errors="ignore").read()
            remote_text = open(remote_src, encoding="utf-8", errors="ignore").read()
        except Exception:
            return lost
        for needle, label in SIGNATURES:
            if needle in local_text and needle not in remote_text:
                lost.append(label)
        # 内部更新模块本身缺失（例如直接更新到上游原版）
        if os.path.exists(os.path.join(self.root, "app", "updater.py")) and not os.path.exists(
            os.path.join(stage, "app", "updater.py")
        ):
            lost.append("内部更新功能（更新后将失去更新入口）")
        return lost

    def _backup(self):
        backup = os.path.join(self.root, ".update", f"backup-{int(time.time())}")
        os.makedirs(backup, exist_ok=True)
        for rel in CODE_PATHS:
            src = os.path.join(self.root, rel)
            if not os.path.exists(src):
                continue
            dst = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst)
        self.log(f"已备份到 {backup}")
        return backup

    def _swap(self, stage):
        for rel in CODE_PATHS:
            src = os.path.join(stage, rel)
            dst = os.path.join(self.root, rel)
            if not os.path.exists(src):
                continue  # 远端没有的路径保留本地
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            elif os.path.exists(dst):
                os.remove(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, dst)

    def _restore_preserved(self, backup):
        """远端包里没有的本地文件（桌面入口等）从备份恢复，否则 fnOS 图标会消失。"""
        for rel in PRESERVE_PATHS:
            live = os.path.join(self.root, rel)
            saved = os.path.join(backup, rel)
            if os.path.exists(live) or not os.path.exists(saved):
                continue
            os.makedirs(os.path.dirname(live), exist_ok=True)
            if os.path.isdir(saved):
                shutil.copytree(saved, live, symlinks=False)
            else:
                shutil.copy2(saved, live)
            self.log(f"保留本地路径：{rel}")

    def _write_build(self, latest):
        data = {
            "BUILD_TAG": latest.get("version") or latest.get("tag") or "",
            "BUILD_SHA": latest.get("sha") or "",
            "UPDATED_AT": time.strftime("%Y-%m-%d %H:%M:%S"),
            "UPDATED_FROM": latest.get("source") or "",
        }
        with open(os.path.join(self.root, "build.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _pip_install(self):
        req = os.path.join(self.root, "requirements.txt")
        if not os.path.exists(req):
            return True, ""
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", req],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0:
                return True, ""
            self.log(f"pip 安装失败：{r.stderr[-500:]}")
            return False, r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else "未知错误"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]

    def _rollback(self, backup):
        self.log(f"回滚自 {backup}")
        for rel in CODE_PATHS:
            src = os.path.join(backup, rel)
            dst = os.path.join(self.root, rel)
            if not os.path.exists(src):
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            elif os.path.exists(dst):
                os.remove(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False)
            else:
                shutil.copy2(src, dst)

    def _last_backup(self):
        upd_dir = os.path.join(self.root, ".update")
        if not os.path.isdir(upd_dir):
            return ""
        names = sorted(n for n in os.listdir(upd_dir) if n.startswith("backup-"))
        return names[-1] if names else ""

    # ------------------------------------------------------------------ 维护
    def cleanup(self, keep=2):
        """只保留最近 keep 个备份，避免占满空间。"""
        upd_dir = os.path.join(self.root, ".update")
        if not os.path.isdir(upd_dir):
            return
        backups = sorted(n for n in os.listdir(upd_dir) if n.startswith("backup-"))
        for name in backups[:-keep] if len(backups) > keep else []:
            shutil.rmtree(os.path.join(upd_dir, name), ignore_errors=True)
