# kilandu 留言监控 · GitHub Actions 免费部署指南（无需云服务器）

用你自己的 GitHub 私有仓库做"免费云端服务器"，GitHub 每 30 分钟自动帮你跑一次留言检查，
有新客户留言就推送到你的微信 + 邮箱。**零成本、无需任何服务器。**

> 频率说明：GitHub 免费版 Actions 额度约 2000 分钟/月。每 30 分钟跑一次 ≈ 960 分钟/月，
> 在免费额度内安全运行。不建议低于 20 分钟（会超额）。

---

## 第一步：创建私有仓库

1. 打开 **github.com** → 右上角 `+` → **New repository**
2. 仓库名随意（如 `kilandu-monitor`），**务必选 Private（私有）**——里面有你的业务逻辑
3. 不要勾选 "Add a README"（空仓库即可，方便等下推送），点 **Create repository**

## 第二步：把文件推上去

本目录下的文件就是要部署的全部内容。在本目录打开终端，执行：

```bash
# 在 github_deploy 目录下
git init
git add -A
git commit -m "init kilandu monitor"
git branch -M main
git remote add origin https://github.com/你的用户名/kilandu-monitor.git
git push -u origin main
```

> 如果 push 要登录，用你的 GitHub 用户名 + Personal Access Token（Settings → Developer settings → Tokens）。

## 第三步：配置 6 个 Secrets（关键！凭据都存这里，不落仓库）

仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**，
逐个添加下面 6 个（Name 和 Value 严格按下面填）：

| Secret 名称 | Value（填你自己的） |
|---|---|
| `KILANDU_USER` | `kilandu` |
| `KILANDU_PASS` | `kilandu2024?` |
| `SCT_KEY` | `SCT412837THzX34Kx7tGBQ2tUmpdUfzLFn` |
| `SMTP_USER` | `1542333614@qq.com` |
| `SMTP_PASS` | `uckpayyqacxyfjfi` |
| `SMTP_TO` | `1542333614@qq.com` |
| `SMTP_ENABLE` | `true` |

## 第四步：手动触发一次，验证

仓库页面 → **Actions** 标签 → 左侧 `kilandu message monitor` → 右侧 **Run workflow** → 点绿色按钮。
等它跑完（约 1 分钟），你的微信/邮箱会收到一条"监控已启动"的推送 = 成功。

## 第五步：确认定时生效

Actions 会自动按 `*/30 * * * *`（每 30 分钟）触发。第一次 schedule 触发可能延迟几分钟，
之后会规律运行。可随时在 Actions 页看运行历史。

---

## 以后想测试/看是否工作

- 在 kilandu 网站前台随便提交一条留言，等下一个 30 分钟窗口内，微信/邮箱就会收到提醒（A级会带🔴）。
- 想立刻验证就进 Actions 手动 **Run workflow**。
- 查看每次运行日志：Actions → 点某次运行 → 展开 `Run kilandu monitor`。

## 注意事项

- **不要**把真实密钥写进 config.json 提交。密钥只放 GitHub Secrets。
- state.json 会被每次运行自动更新并提交回仓库（GitHub 已配置自动 commit+push 权限）。
- 如果某次运行失败，先看 Actions 日志；常见原因是后台登录被限制或网络波动，脚本会自动重登录。
