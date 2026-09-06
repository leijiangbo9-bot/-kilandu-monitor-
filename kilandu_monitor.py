#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kilandu 留言实时提醒监控核心脚本
===============================
监控 kilandu.com(英文站) 和 kilandu.cn(中文站) 智码CMS 后台的"在线留言"表单,
发现新留言时经垃圾过滤分级, 通过 notify 模块推送到 微信(Server酱)+邮件。

运行方式:
  前台单测(跑一轮就退出):  python kilandu_monitor.py --once
  常驻循环:               python kilandu_monitor.py [--interval 300]
首次运行会自动记录当前最新留言ID为基线, 不推送历史积压。
"""
import sys, os, json, time, subprocess, re, ssl, smtplib, logging, argparse, urllib.parse
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _env_get(*names):
    """依次取环境变量, 返回第一个存在的; 都没有返回 None"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None

def load_config():
    """读取 config.json, 并用环境变量覆盖敏感字段(供 GitHub Secrets 注入)。
    优先级: 环境变量 > config.json > 默认空。"""
    with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # 站点账号可用环境变量覆盖
    if _env_get("KILANDU_USER"):
        for s in cfg.get("sites", []):
            s["username"] = os.environ["KILANDU_USER"]
    if _env_get("KILANDU_PASS"):
        for s in cfg.get("sites", []):
            s["password"] = os.environ["KILANDU_PASS"]
    # 推送凭据覆盖
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

CONF = load_config()
LOG = logging.getLogger("kilandu_monitor")
STATE_FILE = os.path.join(BASE_DIR, CONF.get("state_file", "state.json"))

# ---------- Git 状态持久化 (供 GitHub Actions 跨运行保存基线) ----------
def git_pull_state():
    """运行前从远端拉取最新 state.json(避免覆盖他人/上次更新的基线)。
    非 git 环境或失败时静默跳过。"""
    try:
        r = subprocess.run(["git", "-C", BASE_DIR, "pull", "--rebase", "--quiet"],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            LOG.debug("git pull 失败(可能非git仓库或无需拉取): %s", r.stderr.decode()[:100])
    except Exception:
        pass

def git_push_state(commit_msg="update state"):
    """运行后把更新的 state.json 提交并推送回仓库。
    需要仓库已配置 GITHUB_TOKEN(actions) 或本地 git 凭据。非 git 环境静默跳过。"""
    # 只在本仓库目录存在 .git 且 state 有变化时才提交
    if not os.path.exists(os.path.join(BASE_DIR, ".git")):
        return
    try:
        subprocess.run(["git", "-C", BASE_DIR, "add", "-A"], capture_output=True, timeout=30)
        # 检查是否有变更
        chk = subprocess.run(["git", "-C", BASE_DIR, "status", "--porcelain"],
                             capture_output=True, timeout=30)
        if not chk.stdout.decode().strip():
            return
        subprocess.run(["git", "-C", BASE_DIR, "-c", "user.email=bot@kilandu.local",
                        "-c", "user.name=kilandu-bot",
                        "commit", "-m", commit_msg],
                       capture_output=True, timeout=30)
        subprocess.run(["git", "-C", BASE_DIR, "push", "--quiet"], capture_output=True, timeout=60)
        LOG.info("  [git] state.json 已提交推送")
    except Exception as e:
        LOG.warning("  [git] 提交推送失败(不影响本轮监控): %s", e)

# ---------- 工具 ----------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def http_json(method, url, cookie_file, data=None, timeout=25):
    """用外部 curl 发请求(规避 Python cookie jar 与引号问题), 返回 (ok, json_or_text, httpcode)"""
    cmd = ["curl", "-s", "-m", str(timeout), "-b", cookie_file, "-c", cookie_file,
           "-H", "X-Requested-With: XMLHttpRequest", "-X", method, url]
    if data:
        # 用临时文件传 data, 避免命令行引号问题
        tmp = os.path.join(BASE_DIR, "_post_tmp.txt")
        with open(tmp, "w", encoding="utf-8") as f:
            for k, v in data.items():
                f.write(urllib.parse.urlencode({k: v}) + "\n")
        # curl --data 会把每行当一个字段, 更稳妥用 --data-urlencode 逐项; 这里直接拼接URL编码体
        body = urllib.parse.urlencode(data)
        cmd += ["--data", body]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        txt = r.stdout.decode("utf-8", errors="ignore").strip()
        if not txt:
            txt = r.stderr.decode("utf-8", errors="ignore").strip()
        return True, txt, (r.returncode if r.returncode == 0 else 1)
    except Exception as e:
        return False, str(e), -1

def site_cookie_file(site):
    """每个站点独立 cookie 文件, 放在运行目录下"""
    safe = site["base"].replace("https://", "").replace(".", "_").replace("/", "_")
    return os.path.join(BASE_DIR, f"_ck_{safe}.txt")

# ---------- 登录保活 ----------
def ensure_login(site):
    """保证站点会话有效; 返回 True/False"""
    ck = site_cookie_file(site)
    url = site["base"] + site["admin_login"]
    ok, txt, code = http_json("POST", url, ck,
                              data={"username": site["username"], "password": site["password"]})
    if not ok:
        LOG.warning("  [%s] 登录请求失败: %s", site["name"], txt[:120])
        return False
    # 期望 JSON {"code":1,...}
    if '"code":1' in txt or '"code": 1' in txt:
        return True
    LOG.warning("  [%s] 登录返回异常: %s", site["name"], txt[:150])
    return False

# ---------- 拉取留言 ----------
def fetch_messages(site):
    """拉取指定站 fid 表单全部留言JSON, 返回 list[dict]; 失败返回 []"""
    ck = site_cookie_file(site)
    fid = site.get("fid", 1)
    url = f"{site['base']}/admin/diyform/formcon/fid/{fid}?page=1&key="
    ok, txt, _ = http_json("GET", url, ck)
    if not ok or not txt or not txt.startswith("["):
        LOG.warning("  [%s] 留言接口返回非JSON, 可能未登录或结构变化: %s", site["name"], txt[:100])
        return []
    try:
        arr = json.loads(txt)
        return arr if isinstance(arr, list) else []
    except Exception as e:
        LOG.warning("  [%s] JSON解析失败: %s", site["name"], e)
        return []

# ---------- 垃圾过滤与分级 ----------
_filters_cfg = CONF.get("filters", {})
SPAM_WORDS = [w.lower() for w in _filters_cfg.get("spam_keywords", [])]
INTENT_WORDS = [w.lower() for w in _filters_cfg.get("real_intent_keywords", [])]
FREE_DOMAIN_MARKERS = tuple(_filters_cfg.get("spam_domains",
    ("gmail.com","yahoo.com","hotmail.com","outlook.com","aol.com","msn.com",
     "live.com","icloud.com","googlemail.com","mail.ru","gmx.com")))

def classify(rec):
    """返回 (等级, 原因). A=重要客户询盘, B=普通留言, SPAM=垃圾广告, LOW=低质/系统/自测"""
    name = (rec.get("name") or "").strip()
    email = (rec.get("email") or "").strip().lower()
    tel = (rec.get("tel") or "").strip()
    raw_text = rec.get("neirong") or ""
    text = raw_text.strip().lower()
    blob = " ".join([name.lower(), email, tel, text])
    email_domain = email.split("@")[-1] if "@" in email else ""

    # 0) 系统/自站/管理端消息 —— 一律不打扰
    if email_domain in ("kilandu.com", "kilandu.cn"):
        return "LOW", "本站系统/自测消息"
    if email_domain in ("registry.godaddy", "godaddy.com", "abuse.") or "abuse@" in email:
        return "SPAM", "域名注册局系统消息"

    # 1) 垃圾词命中(内容或元信息)
    hit_spam = [w for w in SPAM_WORDS if w in text or w in blob]
    if hit_spam:
        return "SPAM", f"命中垃圾词: {hit_spam[0]}"

    # 2) 纯外链/链接墙广告(内容几乎全是URL)
    urls = re.findall(r"https?://\S+", raw_text)
    no_url_body = re.sub(r"https?://\S+", "", raw_text).strip()
    if urls and len(no_url_body) < 60 and "dear" not in text[:20]:
        return "SPAM", "纯外链广告"

    # 3) 促销/推销模板启发式: 先扬后抑推销
    praise = any(p in text for p in ("content is amazing", "your content", "your website is", "stumbled upon", "i visited your site"))
    promote = any(p in text for p in ("my name is", "i can show", "let me know if you're interested",
                                      "contact now", "click here", "visit ", "check out", "https://", "http://",
                                      "offer", "special price", "limited time"))
    if praise and promote:
        return "SPAM", "疑似SEO/外链推销模板"

    # 3.5) Web开发/营销推销开头话术
    if any(p in text for p in ("still looking at getting your website", "website done/ completed",
                               "web design", "web development services", "build your website",
                               "website development")):
        return "SPAM", "网站开发推销"

    # 3) 空内容无价值
    if not text and not email:
        return "LOW", "空留言无邮箱"

    # 4) 有真实采购意图词 => A级(最可能客户)
    hit_intent = [w for w in INTENT_WORDS if w in text or w in blob]
    if hit_intent:
        # 若仍带外链/促销语气, 降级避免误报(极少见真实询盘发外链)
        if any(u in text for u in ("http://", "https://")) and len(text) < 60:
            return "SPAM", "短内容+外链疑似广告"
        return "A", f"含采购意图词: {', '.join(hit_intent[:5])}"

    # 5) 公司域名邮箱 + 有实质内容 => A
    corp_domain = "@" in email and not email_domain.startswith(FREE_DOMAIN_MARKERS) and email_domain not in FREE_DOMAIN_MARKERS
    if text and corp_domain and len(text) >= 15:
        return "A", "公司域名邮箱发来实质内容"

    # 6) 其余有内容 => B
    if text:
        return "B", "普通留言"

    return "LOW", "信息不完整"

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ---------- 主流程(单轮) ----------
def run_once():
    st = load_state()
    results = []   # 每条新留言 push 元数据
    for site in CONF["sites"]:
        site_key = site["base"].replace("https://", "")
        LOG.info("[%s] 开始检查", site["name"])
        if not ensure_login(site):
            continue
        msgs = fetch_messages(site)
        if not msgs:
            continue
        # 站点历史最大ID(程序视角)
        prev_max = int(st.get("max_id", {}).get(site_key, 0))
        cur_max = max(r.get("id", 0) for r in msgs)
        if prev_max == 0:
            # 首次运行: 记录基线, 不推送历史
            st["max_id"] = st.get("max_id", {})
            st["max_id"][site_key] = cur_max
            LOG.info("  [%s] 首次运行, 记录基线 max_id=%s (不推送历史积压)", site["name"], cur_max)
            save_state(st)
            continue
        # 找出比 prev_max 大的新留言
        new_list = [r for r in msgs if r.get("id", 0) > prev_max]
        if new_list:
            st["max_id"][site_key] = max(cur_max, prev_max)
            save_state(st)
            # 按 id 升序推送(保持提交顺序)
            for rec in sorted(new_list, key=lambda x: x.get("id", 0)):
                results.append({"site": site, "site_key": site_key, "rec": rec})
            LOG.info("  [%s] 发现 %d 条新留言", site["name"], len(new_list))
        else:
            LOG.info("  [%s] 无新留言(当前max=%s)", site["name"], prev_max)
    return results

def main():
    global CONF
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮(用于测试)")
    ap.add_argument("--interval", type=int, default=CONF.get("poll_interval_sec", 300))
    ap.add_argument("--log", default=os.path.join(BASE_DIR, "monitor.log"))
    ap.add_argument("--rebase", action="store_true", help="重置基线(重新把当前最新留言记为已读基线)")
    ap.add_argument("--ci", action="store_true", help="CI模式:运行前后自动git拉取/提交state.json(供GitHub Actions跨运行持久化)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(args.log, encoding="utf-8")])

    # 延迟导入 notify, 避免无推送配置时报错
    try:
        from notify import notify_new_message, notify_startup
    except Exception as e:
        LOG.error("无法加载 notify 模块: %s", e)
        return

    if args.rebase:
        st = load_state()
        for site in CONF["sites"]:
            site_key = site["base"].replace("https://", "")
            if ensure_login(site):
                msgs = fetch_messages(site)
                if msgs:
                    cur_max = max(r.get("id", 0) for r in msgs)
                    st.setdefault("max_id", {})[site_key] = cur_max
                    LOG.info("重置 %s 基线 max_id=%s", site["name"], cur_max)
        save_state(st)
        return

    if args.once:
        # CI 模式(GitHub Actions): state 的拉取由 checkout 完成, 提交由 workflow 末尾 step 完成,
        # 脚本内部不做 git 操作(避免与 workflow 冲突)。
        if args.ci and os.environ.get("GITHUB_ACTIONS") == "true":
            notify_startup()
        run_once()
        return

    # 常驻循环
    LOG.info("启动留言监控, 每 %s 秒轮询一次", args.interval)
    notify_startup()
    while True:
        try:
            new_items = run_once()
            if new_items:
                LOG.info("本轮共 %d 条新留言待推送", len(new_items))
                for it in new_items:
                    notify_new_message(it["site"], it["site_key"], it["rec"])
            else:
                LOG.info("本轮无新留言")
        except Exception as e:
            LOG.exception("轮询出错: %s", e)
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
