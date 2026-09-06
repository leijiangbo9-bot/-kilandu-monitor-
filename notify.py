#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py — 微信(Server酱 SCT)+邮件(SMTP) 双通道推送模块
由 kilandu_monitor.py 调用。
"""
import os, json, logging, smtplib, ssl
from email.mime.text import MIMEText
from email.header import Header
import urllib.request, urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger("kilandu_monitor")

def _env_get(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None

def _conf():
    """读取 config.json 并用环境变量覆盖敏感字段(与主脚本一致,支持 GitHub Secrets)。
    优先级: 环境变量 > config.json。"""
    with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    push = cfg.setdefault("push", {})
    sct = _env_get("SCT_KEY")
    if sct:
        push["sct_key"] = sct
    smtp = push.setdefault("smtp", {})
    if _env_get("SMTP_USER"):
        smtp["user"] = os.environ["SMTP_USER"]
    if _env_get("SMTP_PASS"):
        smtp["pass"] = os.environ["SMTP_PASS"]
    if _env_get("SMTP_TO"):
        smtp["to"] = os.environ["SMTP_TO"]
    if _env_get("SMTP_ENABLE"):
        smtp["enable"] = os.environ["SMTP_ENABLE"].lower() in ("1", "true", "yes")
    return cfg

def _push_cfg():
    return _conf().get("push", {})

# ---------- 组装消息 ----------
def _level_tag(level):
    return {"A": "🔴 重要询盘", "B": "🟡 普通留言", "SPAM": "⚪ 疑似垃圾",
            "LOW": "⚪ 低质留言"}.get(level, level)

def _short(s, n=200):
    s = (s or "").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"

def _build_msg(site, rec, level, reason):
    """构造 (微信title, 微信desp, 邮件subject, 邮件html)"""
    site_name = site["name"]
    name = (rec.get("name") or "—").strip()
    email = (rec.get("email") or "—").strip()
    tel = (rec.get("tel") or "—").strip()
    content = (rec.get("neirong") or "（无内容）").strip()
    ctime = rec.get("create_time", "")
    rid = rec.get("id", "")

    title = f"{_level_tag(level)} | {site_name} 新留言"
    # 微信 desp 用 markdown
    desp = (
        f"### 🆕 {site_name} 收到新留言\n\n"
        f"**姓名：** {name}\n\n"
        f"**邮箱：** {email}\n\n"
        f"**电话：** {tel}\n\n"
        f"**留言内容：**\n> {_short(content, 400)}\n\n"
        f"**提交时间：** {ctime}  **记录ID：** {rid}\n"
        f"**判定：** {reason}"
    )
    subject = f"[{site_name}] 新留言 - {name} / {email}"
    html = (
        f"<h2>{_level_tag(level)} · {site_name} 收到新留言</h2>"
        f"<table border=1 cellspacing=0 cellpadding=6 style='border-collapse:collapse'>"
        f"<tr><td><b>姓名</b></td><td>{_esc(name)}</td></tr>"
        f"<tr><td><b>邮箱</b></td><td>{_esc(email)}</td></tr>"
        f"<tr><td><b>电话</b></td><td>{_esc(tel)}</td></tr>"
        f"<tr><td><b>提交时间</b></td><td>{_esc(ctime)}</td></tr>"
        f"<tr><td><b>记录ID</b></td><td>{_esc(str(rid))}</td></tr>"
        f"<tr><td valign=top><b>留言内容</b></td><td>{_esc(content)}</td></tr>"
        f"</table>"
        f"<p style='color:#888'>判定：{_esc(reason)}</p>"
    )
    return title, desp, subject, html

def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ---------- 微信 (Server酱 Turbo) ----------
def push_wechat(title, desp):
    cfg = _push_cfg()
    key = (cfg.get("sct_key") or "").strip()
    if not key:
        LOG.info("  微信未配置 sct_key, 跳过微信推送")
        return False
    url = cfg.get("sct_api", "https://sctapi.ftqq.com/{key}.send").replace("{key}", key)
    data = urllib.parse.urlencode({"title": title, "desp": desp, "tags": "网站留言"}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "ignore")
        if '"code":0' in body or "success" in body.lower():
            LOG.info("  微信推送成功")
            return True
        LOG.warning("  微信推送返回异常: %s", body[:200])
        return False
    except Exception as e:
        LOG.warning("  微信推送失败: %s", e)
        return False

# ---------- 邮件 ----------
def push_mail(subject, html):
    cfg = _push_cfg().get("smtp") or {}
    if not cfg.get("enable") or not cfg.get("host"):
        LOG.info("  邮件未配置启用, 跳过邮件推送")
        return False
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    try:
        if cfg.get("ssl", True):
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), context=ctx, timeout=20) as s:
                s.login(cfg["user"], cfg["pass"])
                s.sendmail(cfg["user"], [cfg["to"]], msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], int(cfg.get("port", 25)), timeout=20) as s:
                s.ehlo(); s.starttls(context=ssl.create_default_context()); s.login(cfg["user"], cfg["pass"])
                s.sendmail(cfg["user"], [cfg["to"]], msg.as_string())
        LOG.info("  邮件推送成功 → %s", cfg["to"])
        return True
    except Exception as e:
        LOG.warning("  邮件推送失败: %s", e)
        return False

# ---------- 对外接口 ----------
def notify_new_message(site, site_key, rec):
    """单条新留言: 分级后推送。返回 (level, reason, 是否推送)"""
    # 复用监控脚本的分类逻辑
    from kilandu_monitor import classify
    level, reason = classify(rec)
    push_spam = (_conf().get("filters", {}).get("push_spam", False))

    LOG.info("  新留言 id=%s 等级=%s 原因=%s", rec.get("id"), level, reason)

    # 垃圾/低质默认不推送
    if level == "SPAM":
        if not push_spam:
            LOG.info("  判定垃圾(spam), 已静默忽略不推送: %s", reason)
            return level, reason, False
    if level == "LOW":
        LOG.info("  低质留言, 不推送: %s", reason)
        return level, reason, False

    title, desp, subject, html = _build_msg(site, rec, level, reason)
    # 双通道
    push_wechat(title, desp)
    push_mail(subject, html)
    return level, reason, True

def notify_startup():
    """服务启动通知(可选)"""
    title = "✅ 网站留言监控已启动"
    desp = "kilandu 留言实时提醒已开始运行，有新留言会自动推送到这里。"
    push_wechat(title, desp)
