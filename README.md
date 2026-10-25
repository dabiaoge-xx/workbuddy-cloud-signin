# WorkBuddy 云端自动签到（GitHub Actions）

关机也能每天自动领积分。本地电脑不开机时，由 GitHub Actions 云端执行签到。

## 架构

```
本机 WorkBuddy 客户端（天天开机，token 自动刷新）
   │  每小时 sync_secret.py 解密最新 token，加密推送到 GitHub Secret
   ▼
GitHub Secret: WORKBUDDY_AUTH
   │  每天 00:05 / 6 次轮询（北京时间）
   ▼
GitHub Actions 云端跑 signin.py 签到领积分
```

- 本地计划任务照旧在跑（开机时领，token 最新）；
- 云端 Actions 兜底（关机时领，靠同步过去的 Secret）；
- 两端都幂等：先查状态再领取，重复跑不会多领、不会报错。

## 一次性配置（3 步，约 5 分钟）

### 1. 创建 GitHub 仓库并推送代码

在 GitHub 网页创建**私有**仓库（例如 `workbuddy-cloud-signin`），然后：

```bash
cd D:/WorkBuddy项目存档/workbuddy-cloud-signin
git init
git add .
git commit -m "WorkBuddy cloud checkin"
git branch -M main
git remote add origin https://github.com/<你的用户名>/workbuddy-cloud-signin.git
git push -u origin main
```

### 2. 创建 PAT（fine-grained token）

GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token：

- **Repository access**: Only select repositories → 选中刚建的仓库
- **Permissions**: Actions Secrets → **Read and write**（其余全只读/无）

生成后复制（`github_pat_` 开头）。

### 3. 配置两个 Secret

仓库页面 → Settings → Secrets and variables → Actions → New repository secret：

| Name | Value |
|---|---|
| `WORKBUDDY_AUTH` | 先随便填个占位符（如 `{}`），本地同步脚本会自动覆盖 |
| `GH_PAT` | 第 2 步的 PAT |

然后在本目录创建 `config.json`（不会被提交，已在 .gitignore）：

```json
{
  "repo": "<你的用户名>/workbuddy-cloud-signin",
  "pat": "github_pat_第2步的令牌"
}
```

手动跑一次同步，验证整条链路：

```bash
python sync_secret.py
```

看到 `Secret 更新成功` 即配置完成。之后每小时一次的计划任务会自动保持 Secret 最新。

## 日常使用

- **手动跑一次本地签到**：`python signin.py`
- **手动同步 Secret**：`python sync_secret.py`
- **查看云端运行记录**：仓库 → Actions → Daily Checkin / Growth Poll
- **手动触发一次云端签到**：Actions → Daily Checkin → Run workflow

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| Actions 报 `Secret WORKBUDDY_AUTH 未配置` | Secret 没建 | 回第 3 步 |
| Actions 日志 `AUTH_REJECTED` | Secret 里 token 过期 | 本机跑一次 `python sync_secret.py`（本机开过机就会自动更新） |
| sync_secret.py 报 `获取仓库公钥失败` | PAT 权限不对或 repo 填错 | 检查 config.json 与 PAT 权限 |
| 本机长期不开机（>28 天） | token 彻底过期 | 开机后 WorkBuddy 客户端自动刷新，同步脚本自动推送恢复 |

## 安全说明

- `config.json`（含 PAT）、`cloud-auth.json`（含明文 token）均被 `.gitignore` 忽略，不会提交。
- GitHub Secret 为加密存储，仅 Actions 运行时解密。
- 如需彻底移除：删除仓库即可同时清除 Secret 与历史。
