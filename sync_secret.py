#!/usr/bin/env python3
"""WorkBuddy 云端签到 Secret 同步器（本地运行）。

职责：把本机 WorkBuddy 客户端的最新登录态（明文 accessToken/refreshToken）
加密后推送到 GitHub 仓库 Secret `WORKBUDDY_AUTH`，供 Actions 云端签到使用。

为什么需要它：accessToken 有效期 28 天，GitHub Secret 里的副本会过期。
本机客户端每次开机都会自动刷新 token，所以只要本机开过机，就跑一次本脚本，
云端拿到的就永远是最新凭据。配合每小时一次的计划任务形成闭环。

前置条件：
  1. 同目录 config.json：{"repo": "owner/repo", "pat": "github_pat_xxx"}
     （PAT 需对目标仓库有 Secrets:Read and write 权限；config.json 已被 .gitignore 忽略）
  2. 本机已登录 WorkBuddy 桌面端（凭据文件可解密）。

退出码：0 成功（含"无需更新"）；1 失败。
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signin  # noqa: E402  复用凭据定位与信封解密能力

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LAST_SYNC_PATH = os.path.join(HERE, "last-sync.json")
SECRET_NAME = "WORKBUDDY_AUTH"
# token 剩余不足该天数时，每次运行都推送（确保云端拿到的尽量新）
FRESH_DAYS = 7
# 距上次推送超过该天数，强制刷新一次（防止 Secret 被外部改动）
MAX_SYNC_AGE_DAYS = 20


def log(msg):
    print("[sync-secret] %s" % msg, flush=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log("缺少 config.json（需包含 repo 与 pat）。")
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("repo") or not cfg.get("pat"):
        log("config.json 需包含 repo(owner/repo) 与 pat。")
        return None
    return cfg


def build_cloud_auth():
    """解密本机凭据，构造 Actions 云端模式所需的 JSON。"""
    auth_file, _ = signin.find_auth_file()
    if not auth_file:
        log("未找到本机 WorkBuddy 登录凭据。")
        return None
    session = signin.load_session_retry(auth_file)
    resolved = signin.resolve_session(session)
    auth = resolved["auth"]
    uid = resolved["account"]["uid"]
    domain = auth.get("domain") or "www.workbuddy.cn"

    refresh = auth.get("refreshToken")
    if isinstance(refresh, dict):  # 仍是信封，解密之
        refresh = signin._run_auth_helper(
            signin.find_workbuddy_runtime(),
            {"operation": "decrypt", "value": refresh})["accessToken"]

    return {
        "auth": {
            "accessToken": auth["accessToken"],
            "refreshToken": refresh,
            "domain": domain,
            "endpoint": "https://%s" % domain,
        },
        "account": {"uid": uid},
    }


def token_remaining_days(token):
    """解析 JWT exp，返回剩余天数；无法解析返回 None。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8"))
        exp = payload.get("exp")
        if not exp:
            return None
        return (exp - time.time()) / 86400.0
    except Exception:
        return None


def gh_request(method, url, pat, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer %s" % pat,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "workbuddy-secret-sync",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        log("GitHub API %s %s -> HTTP %s: %s" % (method, url, e.code, body[:300]))
        return e.code, None


def main():
    cfg = load_config()
    if not cfg:
        # 未配置时静默跳过（exit 0）：计划任务每小时跑，不能让"没配"变成"每小时报错"
        return 0
    repo = cfg["repo"].strip().strip("/")
    pat = cfg["pat"].strip()
    if "/" not in repo:
        log("repo 需为 owner/repo 形式，当前：%s" % repo)
        return 1

    cloud_auth = build_cloud_auth()
    if not cloud_auth:
        return 1
    token = cloud_auth["auth"]["accessToken"]
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    remaining = token_remaining_days(token)

    # ---- 判断是否需要推送 ----
    last = {}
    if os.path.exists(LAST_SYNC_PATH):
        try:
            with open(LAST_SYNC_PATH, "r", encoding="utf-8") as f:
                last = json.load(f)
        except Exception:
            last = {}
    age_days = (time.time() - last.get("synced_at", 0)) / 86400.0
    stale = age_days > MAX_SYNC_AGE_DAYS
    changed = last.get("fingerprint") != fingerprint
    expiring = remaining is not None and remaining < FRESH_DAYS
    if not (changed or stale or expiring):
        log("Secret 已是最新（指纹 %s，%s 天前同步，token 剩余 %.1f 天），跳过。"
            % (fingerprint, "%.1f" % age_days, remaining or -1))
        return 0
    log("需要推送：changed=%s stale=%s expiring=%s（token 剩余 %s 天）"
        % (changed, stale, expiring, "%.1f" % remaining if remaining is not None else "?"))

    # ---- 获取仓库公钥 ----
    status, key_info = gh_request("GET", "https://api.github.com/repos/%s/actions/secrets/public-key" % repo, pat)
    if status != 200 or not key_info:
        log("获取仓库公钥失败（检查 PAT 权限与 repo 配置）。")
        return 1
    public_key = base64.b64decode(key_info["key"])
    key_id = key_info["key_id"]

    # ---- libsodium sealed box 加密 ----
    try:
        from nacl.public import PublicKey, SealedBox
    except ImportError:
        log("缺少 pynacl，请先执行：python -m pip install pynacl")
        return 1
    sealed = SealedBox(PublicKey(public_key)).encrypt(
        json.dumps(cloud_auth, ensure_ascii=False).encode("utf-8"))
    encrypted_value = base64.b64encode(sealed).decode("ascii")

    # ---- 推送 ----
    status, _ = gh_request(
        "PUT",
        "https://api.github.com/repos/%s/actions/secrets/%s" % (repo, SECRET_NAME),
        pat,
        {"encrypted_value": encrypted_value, "key_id": key_id})
    if status not in (201, 204):
        log("Secret 更新失败。")
        return 1

    with open(LAST_SYNC_PATH, "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint, "synced_at": time.time(),
                   "remaining_days": remaining}, f)
    log("Secret 更新成功（指纹 %s，token 剩余 %s 天）。"
        % (fingerprint, "%.1f" % remaining if remaining is not None else "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
