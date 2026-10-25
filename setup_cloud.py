#!/usr/bin/env python3
"""WorkBuddy 云端签到一键配置脚本。

用法：python setup_cloud.py <PAT> [仓库名]

给定一个有权限的 fine-grained PAT，自动完成：
  1. 创建私有仓库（已存在则复用）
  2. 写入 config.json
  3. git push 推送代码
  4. 创建 WORKBUDDY_AUTH Secret（占位）
  5. 运行 sync_secret.py 推送真实 token
  6. 触发一次 workflow_dispatch 验证云端链路

PAT 最小权限：目标仓库 Actions Secrets: Read and write + Contents: Read and write。
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"


def log(msg):
    print("[setup] %s" % msg, flush=True)


def gh(method, path, pat, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer %s" % pat,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "workbuddy-cloud-setup",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        log("API %s %s -> HTTP %s: %s" % (method, path, e.code, body[:300]))
        return e.code, None


def run(cmd, cwd=HERE, check=True, extra_env=None, sanitize_pat=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    shown = " ".join(cmd)
    if sanitize_pat and sanitize_pat in shown:
        shown = shown.replace(sanitize_pat, "github_pat_***")
    log("$ %s" % shown)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        log("命令失败(%d)：%s %s" % (r.returncode, r.stdout[-500:], r.stderr[-500:]))
        sys.exit(1)
    return r


def main():
    if len(sys.argv) < 2:
        log("用法：python setup_cloud.py <PAT> [仓库名]")
        return 1
    pat = sys.argv[1].strip()
    repo_name = sys.argv[2].strip() if len(sys.argv) > 2 else "workbuddy-cloud-signin"

    # ---- 0. 校验 PAT 身份 ----
    status, me = gh("GET", "/user", pat)
    if status != 200 or not me:
        log("PAT 无效或无权限。")
        return 1
    owner = me["login"]
    repo = "%s/%s" % (owner, repo_name)
    log("PAT 身份：%s（%s）" % (owner, me.get("name") or ""))

    # ---- 1. 建仓（存在则复用）----
    status, repo_info = gh("GET", "/repos/%s" % repo, pat)
    if status == 200:
        log("仓库已存在，复用：%s" % repo)
    else:
        status, created = gh("POST", "/user/repos", pat, {
            "name": repo_name, "private": True,
            "description": "WorkBuddy 每日自动领积分（GitHub Actions 云端签到）",
            "has_issues": False, "has_projects": False, "has_wiki": False,
        })
        if status != 201:
            log("建仓失败。")
            return 1
        log("仓库已创建（私有）：%s" % repo)

    # ---- 2. config.json ----
    cfg_path = os.path.join(HERE, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"repo": repo, "pat": pat}, f, ensure_ascii=False)
    log("config.json 已写入（已被 .gitignore 忽略，不会提交）")

    # ---- 3. git push ----
    remote = "https://x-access-token:%s@github.com/%s.git" % (pat, repo)
    run(["git", "remote", "remove", "origin"], check=False)
    run(["git", "remote", "add", "origin", remote], sanitize_pat=pat)
    r = run(["git", "ls-remote", "--heads", remote], check=False, sanitize_pat=pat)
    if r.returncode == 0 and r.stdout.strip():
        run(["git", "push", "-f", "origin", "HEAD:main"], sanitize_pat=pat)
    else:
        run(["git", "push", "-u", "origin", "HEAD:main"], sanitize_pat=pat)
    # 推送完成后从 remote URL 中移除令牌，避免长期驻留
    run(["git", "remote", "set-url", "origin", "https://github.com/%s.git" % repo])
    log("代码已推送")

    # ---- 4. Secret 占位 ----
    from nacl.public import PublicKey, SealedBox
    status, key_info = gh("GET", "/repos/%s/actions/secrets/public-key" % repo, pat)
    if status != 200:
        log("获取公钥失败，PAT 可能缺 Secrets 权限。")
        return 1
    sealed = SealedBox(PublicKey(base64.b64decode(key_info["key"]))).encrypt(b"{}")
    status, _ = gh("PUT", "/repos/%s/actions/secrets/WORKBUDDY_AUTH" % repo, pat, {
        "encrypted_value": base64.b64encode(sealed).decode("ascii"),
        "key_id": key_info["key_id"]})
    if status not in (201, 204):
        log("Secret 创建失败。")
        return 1
    log("WORKBUDDY_AUTH Secret 已创建（占位）")

    # ---- 5. 同步真实 token ----
    py = sys.executable
    r = run([py, "sync_secret.py"], check=False)
    if r.returncode != 0:
        log("sync_secret.py 失败，请检查本机 WorkBuddy 登录态。")
        return 1

    # ---- 6. 触发一次 Actions 验证 ----
    status, _ = gh("POST", "/repos/%s/actions/workflows/daily-checkin.yml/dispatches" % repo, pat,
                   {"ref": "main"})
    if status == 204:
        log("已触发 daily-checkin workflow（云端链路验证中，约 1 分钟后可在 Actions 页查看）")
    else:
        log("手动触发 workflow 失败（不影响定时任务），可稍后在网页手动 Run workflow")

    log("")
    log("全部完成！仓库：https://github.com/%s" % repo)
    log("- 每天北京 00:05 云端自动签到，6 次轮询照常")
    log("- 本机每小时自动同步最新 token 到 Secret，云端永不过期")
    log("- 从此关机也能领积分")
    return 0


if __name__ == "__main__":
    sys.exit(main())
