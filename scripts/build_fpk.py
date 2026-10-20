#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 kkzdzc-src + fnos/ 打成 fnOS 原生应用 .fpk，并做结构自检。

产物结构（fnpack 1.2.3 实测格式）：
  <name>.fpk = tar.gz {
      manifest, manifest.checksum,
      app.tgz,           # 应用本体
      cmd/, config/, wizard/, ui/,
      ICON.PNG, ICON_256.PNG
  }
其中 manifest.checksum = md5(app.tgz 的字节内容)。

用法：
  python3 scripts/build_fpk.py --src kkzdzc-src --pkg fnos --out dist \
      --version 1.0.0 --arch x86 --tag v1.0.0 --sha $(git rev-parse --short HEAD)
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time

SKIP_DIRS = {".git", ".github", ".update", "__pycache__", "venv", ".venv",
             "node_modules", "fnos", "scripts", "dist"}
SKIP_FILES = {"build.json", ".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log")


def parse_manifest(path):
    """manifest 是 `key = value`，保留原值边界。"""
    data = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise SystemExit(f"manifest 行非法：{line!r}")
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def write_manifest(path, data, order):
    with open(path, "w", encoding="utf-8") as f:
        for k in order:
            if k in data:
                f.write(f"{k:<27}= {data[k]}\n")


def copytree(src, dst, base=None):
    """复制并顺手规范化权限：目录 755，文件 644，脚本 755。"""
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            if name in SKIP_DIRS:
                continue
            copytree(s, d)
            continue
        if name in SKIP_FILES or name.endswith(SKIP_SUFFIX):
            continue
        shutil.copy2(s, d)
        mode = 0o755 if name.endswith(".sh") else 0o644
        os.chmod(d, mode)


def make_targz(src_dir, out_path, arc_prefix=""):
    """把目录打成确定性 tar.gz（mtime 归零，权限归一）。"""
    with tarfile.open(out_path, "w:gz", compresslevel=9) as tar:
        for root, dirs, files in os.walk(src_dir):
            dirs.sort()
            rel_root = os.path.relpath(root, src_dir)
            if rel_root == ".":
                rel_root = ""
            for name in sorted(dirs) + sorted(files):
                full = os.path.join(root, name)
                rel = os.path.join(rel_root, name) if rel_root else name
                arc = os.path.join(arc_prefix, rel) if arc_prefix else rel
                info = tar.gettarinfo(full, arcname=arc)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isdir():
                    info.mode = 0o755
                    tar.addfile(info)
                elif info.isfile():
                    info.mode = 0o755 if name.endswith(".sh") else 0o644
                    with open(full, "rb") as fh:
                        tar.addfile(info, fh)


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="应用源码目录")
    ap.add_argument("--pkg", required=True, help="fnOS 工程目录（含 manifest/cmd/...）")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--version", default=None, help="覆盖 manifest.version")
    ap.add_argument("--arch", default="x86", choices=["x86", "arm", "all"])
    ap.add_argument("--tag", default="", help="写入 app/build.json 的 BUILD_TAG")
    ap.add_argument("--sha", default="", help="写入 app/build.json 的 BUILD_SHA")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    pkg = os.path.abspath(args.pkg)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    mpath = os.path.join(pkg, "manifest")
    order = [ln.split("=")[0].strip() for ln in open(mpath, encoding="utf-8")
             if "=" in ln and not ln.lstrip().startswith("#")]
    man = parse_manifest(mpath)
    if args.version:
        man["version"] = args.version
    man["platform"] = args.arch

    appname = man["appname"]
    version = man["version"]
    required = ("cmd/main", "config/privilege", "config/resource",
                "ICON.PNG", "ICON_256.PNG", "ui/config")
    missing = [f for f in required if not os.path.exists(os.path.join(pkg, f))]
    if missing:
        raise SystemExit("工程缺少必需文件：" + "、".join(missing))

    stage = tempfile.mkdtemp(prefix="fpk-stage-")
    try:
        root = os.path.join(stage, appname)
        os.makedirs(root)

        for name in ("cmd", "config", "wizard"):
            s = os.path.join(pkg, name)
            if os.path.isdir(s):
                copytree(s, os.path.join(root, name))
        for name in ("ICON.PNG", "ICON_256.PNG"):
            shutil.copy2(os.path.join(pkg, name), os.path.join(root, name))
            os.chmod(os.path.join(root, name), 0o644)

        # 应用本体
        app_dir = os.path.join(root, "app")
        copytree(src, app_dir)
        # 桌面入口目录必须在 app/ 下（manifest 的 desktop_uidir 相对于 app/）
        ui_src = os.path.join(pkg, "ui")
        if os.path.isdir(ui_src):
            copytree(ui_src, os.path.join(app_dir, "ui"))
        build = {"BUILD_TAG": args.tag or f"v{version}", "BUILD_SHA": args.sha}
        with open(os.path.join(app_dir, "build.json"), "w", encoding="utf-8") as f:
            json.dump(build, f, ensure_ascii=False)

        # 内部归档 + 校验和（fnOS 要求 checksum 写在 manifest 里，等于 app.tgz 的 md5）
        app_tgz = os.path.join(root, "app.tgz")
        make_targz(app_dir, app_tgz, arc_prefix="")
        checksum = md5_file(app_tgz)
        # 外层只放 app.tgz，不要把暂存的 app/ 目录也塞进去（真实 fpk 里没有）
        shutil.rmtree(app_dir, ignore_errors=True)
        man["checksum"] = checksum

        fields = [k for k in order if k != "checksum"] + ["checksum"]
        write_manifest(os.path.join(root, "manifest"), man, fields + [k for k in man if k not in fields])

        # 外层 fpk
        fpk_name = f"{appname}_{version}_{args.arch}.fpk"
        fpk_path = os.path.join(out, fpk_name)
        make_targz(root, fpk_path)
        digest = sha256_file(fpk_path)
        with open(fpk_path + ".sha256", "w", encoding="utf-8") as f:
            f.write(f"{digest}  {fpk_name}\n")

        # ---- 自检 ----
        problems = []
        with tarfile.open(fpk_path, "r:gz") as tar:
            names = set(tar.getnames())
            for need in ("manifest", "app.tgz", "cmd/main", "ICON.PNG", "ICON_256.PNG"):
                if need not in names:
                    problems.append(f"缺少 {need}")
            if "manifest" in names:
                text = tar.extractfile("manifest").read().decode("utf-8")
                m = re.search(r"^\s*checksum\s*=\s*([0-9a-f]{32})\s*$", text, re.M)
                if not m:
                    problems.append("manifest 缺少 checksum 字段（fnOS 会报数据包不完整）")
                elif m.group(1) != checksum:
                    problems.append(f"checksum 不匹配（{m.group(1)} != {checksum}）")
            if "app.tgz" in names:
                blob = tar.extractfile("app.tgz").read()
                got = hashlib.md5(blob).hexdigest()
                if got != checksum:
                    problems.append(f"app.tgz md5 变化（{got} != {checksum}）")
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as inner:
                    inner_names = set(inner.getnames())
                    for need in ("app/run.py", "app/updater.py", "quark_auto_save.py",
                                 "notify.py", "service.sh", "requirements.txt", "ui/config"):
                        if need not in inner_names:
                            problems.append(f"app.tgz 缺少 {need}")
                    bad = [n for n in inner_names if n.startswith("/") or ".." in n.split("/")]
                    if bad:
                        problems.append("app.tgz 含越界路径")
        if problems:
            raise SystemExit("自检失败：\n - " + "\n - ".join(problems))

        size = os.path.getsize(fpk_path)
        print(json.dumps({
            "ok": True, "fpk": fpk_path, "size": size,
            "sha256": digest, "app_tgz_md5": checksum,
            "appname": appname, "version": version, "platform": args.arch,
        }, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()
