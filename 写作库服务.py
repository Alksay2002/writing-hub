#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写作库 · 本地写作内容管理服务
把 WorkBuddy / DSH / Trae 交付里的写作文件聚合成一个可检索、可编辑的页面。

用法:
    python 写作库服务.py                    # 监听 0.0.0.0:8765，手机同 WiFi 可访问
    python 写作库服务.py --port 9000
    python 写作库服务.py --host 127.0.0.1    # 只允许本机
    python 写作库服务.py --list              # 只打印索引，不起服务

零依赖，只用 Python 标准库。迁移 NAS 时把本文件和 index.html 放同一目录即可。
"""

import os
import sys
import json
import re
import time
import html
import socket
import zipfile
import mimetypes
import subprocess
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

def _crash_log_path():
    """启动崩溃时的兜底日志路径（无控制台模式也能留下 traceback）"""
    for d in (os.path.join(os.environ.get("APPDATA", ""), "WritingHub"), os.environ.get("TEMP", "")):
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "writinghub_crash.log")
        except Exception:
            continue
    return ""

def _log_crash(exc_text):
    p = _crash_log_path()
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("=== %s ===\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), exc_text))
    except Exception:
        pass

# 禁用 SO_REUSEADDR：确保端口冲突时会抛 OSError（自适应端口扫描的前提）
class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

# ─────────────────────────────────────────────────────────────
# 路径定位：同时兼容源码运行和 PyInstaller 打包运行
#   开发时: BASE_DIR = 写作库服务.py 所在目录
#   打包后(onedir): BASE_DIR = EXE 所在目录（数据+资源都放旁边）
#   打包后(onefile): BASE_DIR = %APPDATA%/WritingHub（数据持久化）
#   静态资源(INDEX/BENCH): EXE 同目录优先 → fallback sys._MEIPASS
# ─────────────────────────────────────────────────────────────
def _is_frozen():
    """PyInstaller 打包后 sys.frozen 为 True"""
    import sys
    return getattr(sys, 'frozen', False)

def _exe_dir():
    """打包后返回 EXE 所在目录（onedir 和 onefile 通用）"""
    import sys
    return os.path.dirname(sys.executable) if _is_frozen() else os.path.dirname(os.path.abspath(__file__))

def _meipass():
    """PyInstaller 临时解压目录（只读静态资源兜底）"""
    import sys
    return getattr(sys, '_MEIPASS', '')

def _data_dir():
    """可写数据目录：打包后 onefile 放 %APPDATA%，其他情况和资源目录一致"""
    if not _is_frozen():
        return os.path.dirname(os.path.abspath(__file__))
    import sys
    if getattr(sys, '_MEIPASS', ''):
        # onefile 模式 → APPDATA 下持久化
        d = os.path.join(os.environ.get('APPDATA', _exe_dir()), 'WritingHub')
        os.makedirs(d, exist_ok=True)
        return d
    # onedir 模式 → exe 同目录
    return _exe_dir()

def _resource_path(*parts):
    """静态资源查找顺序：exe 同目录 → _MEIPASS → __file__ 所在目录 → __file__ 上级目录 → exe 上级目录"""
    exe_side = os.path.join(_exe_dir(), *parts)
    if os.path.exists(exe_side):
        return exe_side
    mp = _meipass()
    if mp:
        mp_side = os.path.join(mp, *parts)
        if os.path.exists(mp_side):
            return mp_side
    # 开发时：__file__ 同级（写作库/ 目录里有 index.html）
    here_side = os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
    if os.path.exists(here_side):
        return here_side
    # 开发时：__file__ 上级目录（写作工作台.html 在项目根）
    parent_side = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)
    if os.path.exists(parent_side):
        return parent_side
    # 最后兜底：exe 上级目录
    fallback = os.path.join(os.path.dirname(_exe_dir()), *parts)
    if os.path.exists(fallback):
        return fallback
    return exe_side

BASE_DIR = _data_dir()
INDEX_HTML = _resource_path("index.html")
BENCH_HTML = _resource_path("写作工作台.html")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

AUTHOR = "凌空"

APP_PREFS_PATH = os.path.join(BASE_DIR, "prefs.json")

# 全局应用偏好（持久化到 prefs.json）。后端只存这些值本身，前端决定如何渲染。
DEFAULT_APP_PREFS = {
    "lang": "zh",                   # zh / en / ja
    "theme": "auto",                # dark / light / auto
    "autoOpenBrowser": True,
    "refreshOnStart": True,         # 每次启动自动刷新索引
    "peekWidth": 440,               # 预览面板默认宽度（px）
    "peekCollapsed": False,
    "maxItems": 2000,
    "fileExts": [".md", ".markdown", ".txt", ".html", ".htm", ".tex"],
    "ignoreDirs": ["node_modules", ".git", ".venv", "__pycache__", ".trae"],
}

def load_app_prefs():
    """读 prefs.json（不存在返回默认值）"""
    data = dict(DEFAULT_APP_PREFS)
    if os.path.exists(APP_PREFS_PATH):
        try:
            with open(APP_PREFS_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                data.update(user)
        except Exception:
            pass
    # 类型/取值兜底
    if data.get("lang") not in ("zh", "en", "ja"):
        data["lang"] = "zh"
    if data.get("theme") not in ("dark", "light", "auto"):
        data["theme"] = "auto"
    data["peekWidth"] = max(240, min(900, int(data.get("peekWidth", 440))))
    data["maxItems"] = max(100, min(10000, int(data.get("maxItems", 2000))))
    if not isinstance(data.get("fileExts"), list):
        data["fileExts"] = list(DEFAULT_APP_PREFS["fileExts"])
    if not isinstance(data.get("ignoreDirs"), list):
        data["ignoreDirs"] = list(DEFAULT_APP_PREFS["ignoreDirs"])
    for k in ("autoOpenBrowser", "refreshOnStart", "peekCollapsed"):
        data[k] = bool(data.get(k, DEFAULT_APP_PREFS.get(k, False)))
    return data

def save_app_prefs(data):
    """合并更新 prefs.json，只覆盖允许字段"""
    cur = load_app_prefs()
    if isinstance(data, dict):
        for k in DEFAULT_APP_PREFS:
            if k in data:
                cur[k] = data[k]
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(APP_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cur


def _read_version():
    """从 version.json 读版本号（exe 同目录 / 打包内嵌），读不到就 dev"""
    cands = [os.path.join(_exe_dir(), "version.json")]
    if _meipass():
        cands.append(os.path.join(_meipass(), "version.json"))
    for p in cands:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    v = str(json.load(f).get("version") or "").strip()
                if v:
                    return v
            except Exception:
                pass
    return "dev"


APP_VERSION = _read_version()

# 本机技能目录：只取「名字 + 一行描述」，不读正文，避免把别人的作品带上
SKILL_SOURCES = [
    ("Trae 插件", os.path.join(os.environ.get("USERPROFILE", ""), ".trae-cn", "plugins"), "plugin"),
    ("Claude Code", os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "skills"), "flat"),
    ("Codex", os.path.join(os.environ.get("USERPROFILE", ""), ".codex", "skills"), "flat"),
    ("Cursor", os.path.join(os.environ.get("USERPROFILE", ""), ".cursor", "skills"), "flat"),
    ("通用 Agent", os.path.join(os.environ.get("USERPROFILE", ""), ".agents", "skills"), "flat"),
]

# 偏好文件找不到时的兜底约定（可在 preferences.md 里整份替换）
DEFAULT_PREFS = [
    "开场三问。动手前先对齐标杆、范围、合格线；有外部参照就先找参照，再做。",
    "HTML 优先。分析、报告、面板一律单文件 HTML：暗色默认、可切亮色、离线可用、可打印。",
    "链接必须可点击直达。资源清单里不留裸地址，不让用户自己去搜。",
    "效率铁律。工具的意义是减少工作量、帮助决策，不是产出更多需要阅读的东西。",
    "文风硬约束。成稿要像人写的：少用破折号、不写排比套话、改具体场景与真实细节。",
    "新长期偏好要确认。可以写进偏好文件，但不擅自改工作方式规则。",
    "复盘落盘。每轮结束记清验收合同、取舍、可迁移原则与未解决项。",
]


def _skill_desc(md_path):
    """取 SKILL.md 的 description 首行（最多 80 字）"""
    try:
        with open(md_path, encoding="utf-8", errors="ignore") as f:
            lines = [next(f, "") for _ in range(40)]
    except Exception:
        return ""
    in_fm = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s == "---":
            in_fm = not in_fm
            continue
        if in_fm and s.lower().startswith("description:"):
            d = s[12:].strip().strip("\"'")
            if d in ("|", ">", "|-", ">-", ""):
                # YAML 多行写法：描述在下一行
                for nxt in lines[i + 1:]:
                    t = nxt.strip()
                    if t == "---":
                        break
                    if t:
                        d = t
                        break
            return d[:80]
    return ""


def scan_skills(limit=80):
    """扫描本机已安装技能，只返回名字/来源/一行描述"""
    import glob
    out, seen = [], set()
    for label, base, kind in SKILL_SOURCES:
        if not os.path.isdir(base):
            continue
        if kind == "plugin":
            # plugins/<registry>/<plugin>/<version>/skills/<skill>/SKILL.md
            paths = glob.glob(os.path.join(base, "*", "*", "*", "skills", "*", "SKILL.md"))
            paths += glob.glob(os.path.join(base, "*", "*", "skills", "*", "SKILL.md"))
        else:
            paths = glob.glob(os.path.join(base, "*", "SKILL.md"))
        for md in paths:
            nm = os.path.basename(os.path.dirname(md))
            if not nm or nm in seen:
                continue
            seen.add(nm)
            out.append({"name": nm, "desc": _skill_desc(md), "source": label})
    out.sort(key=lambda x: (x["source"], x["name"].lower()))
    return out[:limit]


def load_prefs():
    """优先读用户自己的 preferences.md，没有就用内置兜底"""
    for p in (os.path.join(BASE_DIR, "preferences.md"), os.path.join(_exe_dir(), "preferences.md")):
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        rules = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            s = re.sub(r'^(?:[-*+]|\d+[.、)])\s*', '', s)
            if s:
                rules.append(s)
        if rules:
            return {"source": "file", "path": p, "rules": rules}
    return {"source": "default", "path": "", "rules": DEFAULT_PREFS}

# ─────────────────────────────────────────────────────────────
# 内容根目录：下面的 DEFAULT_ROOTS 是自动探测模板，
# 到任何电脑上只保留真实存在的条目。想手动加/改？在 config.json 里写：
#   {"roots": [{"id":"wb","name":"WorkBuddy","path":"C:\\Users\\you\\WorkBuddy"}]}
# 有 config.json 就完全覆盖自动探测结果。
# ─────────────────────────────────────────────────────────────
# 辅助：构造多个候选路径
def _user(*parts):
    return os.path.normpath(os.path.join(os.environ.get("USERPROFILE", ""), *parts))
def _appdata(*parts):
    return os.path.normpath(os.path.join(os.environ.get("APPDATA", ""), *parts))
def _local_appdata(*parts):
    return os.path.normpath(os.path.join(os.environ.get("LOCALAPPDATA", ""), *parts))

DEFAULT_ROOTS = [
    # ═══════════════════════════════════════════════════════════
    # Agent 工具专用数据目录（不扫桌面、不扫整个文档）
    # 任何电脑上这里面哪些存在就自动加哪些。用户想加别的？
    # 左侧「⚙ 管理」里手动选。
    # ═══════════════════════════════════════════════════════════
    {"id": "wb",   "name": "WorkBuddy",
     "probe": [_user("WorkBuddy")]},
    {"id": "dsh",  "name": "DSH 工作区",
     "probe": [
        _user("Documents","deepseek-harness","default-workspace"),
        _user("deepseek-harness","default-workspace"),
        _user(".deepseek-harness","default-workspace"),
    ]},
    {"id": "trae", "name": "Trae 交付",
     "probe": [_user("Documents","Trae-CN"), _user("Documents","Trae")]},
    {"id": "codex", "name": "Codex CLI",
     "probe": [_user(".codex")]},
    {"id": "claude", "name": "Claude Code",
     "probe": [_user(".claude"), _user(".claude-code")]},
    {"id": "cd", "name": "Claude Desktop",
     "probe": [_appdata("Claude"), _user(".claude-desktop")]},
    {"id": "cursor", "name": "Cursor",
     "probe": [_appdata("Cursor","User","workspaceStorage"), _appdata("Cursor")]},
    {"id": "windsurf", "name": "Windsurf",
     "probe": [_appdata("Windsurf","User","workspaceStorage"), _appdata("Windsurf")]},
    {"id": "cline", "name": "Cline",
     "probe": [_appdata("Code","User","globalStorage","saoudrizwan.claude-dev")]},
    {"id": "aider", "name": "Aider",
     "probe": [_user(".aider")]},
    {"id": "copilot", "name": "GitHub Copilot",
     "probe": [_appdata("Code","User","globalStorage","github.copilot")]},
    # ── 可选候选（不默认启用，只出现在「⚙ 管理候选列表」里）──
    # 注意：桌面和文档故意不默认启用，避免把整盘内容扫进来
]

# 可选候选：前端「⚙ 管理」里能看到但不默认加入的
OPTIONAL_ROOTS = [
    {"id": "desk", "name": "桌面（可选）",
     "probe": [_user("Desktop")]},
    {"id": "docs", "name": "我的文档（可选）",
     "probe": [_user("Documents")]},
    {"id": "notes", "name": "Obsidian 笔记（可选）",
     "probe": [_user("Documents","Obsidian")]},
]

def load_roots():
    """先看 config.json，没找到就自动探测 DEFAULT_ROOTS 中存在的路径"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and isinstance(cfg.get("roots"), list):
                out = []
                for r in cfg["roots"]:
                    p = r.get("path", "")
                    if p and os.path.exists(os.path.normpath(p)):
                        out.append({"id": r["id"], "name": r.get("name", r["id"]), "path": os.path.normpath(p)})
                if out: return out
        except Exception as e:
            print("[WARN] config.json 读失败，走自动探测：", e)
    # 自动探测（只扫 Agent 工具目录，不兜底到文档）
    out = []
    for t in DEFAULT_ROOTS:
        for p in t.get("probe", []):
            if p and os.path.exists(p):
                out.append({"id": t["id"], "name": t["name"], "path": os.path.normpath(p)})
                break
    return out

def roots_probe_list():
    """返回 DEFAULT_ROOTS + OPTIONAL_ROOTS 中实际存在的 —— 给前端展示「可开启的候选」"""
    found = []
    for t in DEFAULT_ROOTS + OPTIONAL_ROOTS:
        for p in t.get("probe", []):
            if p and os.path.exists(p):
                found.append({"id": t["id"], "name": t["name"], "path": os.path.normpath(p)})
                break
    return found

ROOTS = load_roots()

TEXT_EXT = {".md", ".markdown", ".txt", ".tex", ".html", ".htm"}
DOC_EXT = {".docx", ".doc", ".pdf"}
ALL_EXT = TEXT_EXT | DOC_EXT

SKIP_DIRS = {
    "node_modules", "__pycache__", "frames", "vo", "video-check",
    "resume_pages", "clean-keyframes", "_assets", "写作库", "dist", "build",
    # 下面的都不是写作内容，是代码仓库或第三方解压包
    "Worktrees", "crosspet-extract", "vendor", "third_party", "cnocr_models",
}

GENRE = {
    ".md": "稿件", ".markdown": "稿件", ".txt": "稿件", ".tex": "稿件",
    ".html": "网页", ".htm": "网页",
    ".docx": "文档", ".doc": "文档",
    ".pdf": "PDF",
    ".csv": "数据", ".json": "数据", ".yml": "数据", ".yaml": "数据",
}

MAX_DEPTH = 6
MAX_READ = 2 * 1024 * 1024      # 超过 2MB 不读正文，只登记元信息
CACHE_TTL = 8                    # 秒

DIGEST_DIR = os.path.join(BASE_DIR, "日报")
STATE_FILE = os.path.join(BASE_DIR, ".state.json")
DIGEST_AT = "22:00"              # 每天自动归集的时间，改这里即可
_digest_lock = threading.Lock()

_cache = {"t": 0.0, "items": []}
_meta_cache = {}                 # abs_path -> (mtime, size, title, excerpt, words)
_lock = threading.Lock()


# ────────────────────────── 文本工具 ──────────────────────────
def read_text(ap):
    try:
        with open(ap, "rb") as f:
            b = f.read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def strip_markup(s):
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"[*_`>|]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return html.unescape(s).strip()


def pick_title(text, ext, fallback):
    if ext in (".md", ".markdown"):
        m = re.search(r"^\s{0,3}#\s+(.+)$", text, flags=re.M)
        if m:
            return m.group(1).strip()[:120]
    if ext in (".html", ".htm"):
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", text, flags=re.I)
        if m:
            t = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
            if t:
                return t[:120]
    if ext == ".tex":
        m = re.search(r"\\title\{([^}]*)\}", text)
        if m:
            return m.group(1).strip()[:120]
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("---", "+++", "<!--")):
            return strip_markup(line)[:120] or fallback
    return fallback


def docx_text(ap):
    try:
        with zipfile.ZipFile(ap) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    txt = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(txt)


def count_words(s):
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", s))
    en = len(re.findall(r"[A-Za-z]+", s))
    return cjk + en


def build_meta(ap, ext, size, mtime, name):
    """返回 (title, excerpt, words)。带 mtime+size 缓存，避免每次全量解析。"""
    key = ap
    hit = _meta_cache.get(key)
    if hit and hit[0] == mtime and hit[1] == size:
        return hit[2], hit[3], hit[4]

    base = os.path.splitext(name)[0]
    if ext in DOC_EXT and ext != ".pdf":
        if size <= 20 * 1024 * 1024:
            txt = docx_text(ap)
            plain = strip_markup(txt)
            title = base
            for line in txt.splitlines():
                if line.strip():
                    title = line.strip()[:120]
                    break
            words = count_words(plain)
            excerpt = plain[:220]
        else:
            title, excerpt, words = base, "", 0
    elif ext == ".pdf":
        title, excerpt, words = base, "PDF 文档，可在右侧直接打开预览", 0
    elif size > MAX_READ:
        title, excerpt, words = base, "文件较大，已跳过正文摘要", 0
    else:
        text = read_text(ap)[:200000]
        plain = strip_markup(text)
        title = pick_title(text, ext, base)
        excerpt = plain[:220]
        words = count_words(plain)

    _meta_cache[key] = (mtime, size, title, excerpt, words)
    return title, excerpt, words


# ────────────────────────── 扫描索引 ──────────────────────────
def scan(force=False):
    with _lock:
        now = time.time()
        if not force and now - _cache["t"] < CACHE_TTL and _cache["items"]:
            return _cache["items"]

        items = []
        for root in ROOTS:
            rp = root["path"]
            if not os.path.isdir(rp):
                continue
            for cur, dirs, files in os.walk(rp):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
                rel_dir = os.path.relpath(cur, rp)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth >= MAX_DEPTH:
                    dirs[:] = []
                for fn in files:
                    if fn.startswith(("~$", ".")):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in ALL_EXT:
                        continue
                    ap = os.path.join(cur, fn)
                    try:
                        st = os.stat(ap)
                    except OSError:
                        continue
                    rel = os.path.relpath(ap, rp).replace("\\", "/")
                    try:
                        title, excerpt, words = build_meta(ap, ext, st.st_size, st.st_mtime, fn)
                    except Exception:
                        title, excerpt, words = os.path.splitext(fn)[0], "", 0
                    items.append({
                        "id": root["id"] + "/" + rel,
                        "root": root["id"],
                        "rootName": root["name"],
                        "dir": os.path.dirname(rel).replace("\\", "/"),
                        "name": fn,
                        "ext": ext,
                        "genre": GENRE.get(ext, "其他"),
                        "editable": ext in TEXT_EXT,
                        "title": title,
                        "excerpt": excerpt,
                        "words": words,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })

        items.sort(key=lambda x: -x["mtime"])
        _cache["t"] = now
        _cache["items"] = items
        return items


def roots_status():
    out = []
    for r in ROOTS:
        p = r["path"]
        out.append({
            "id": r["id"], "name": r["name"], "path": p,
            "exists": os.path.isdir(p),
            "writable": os.access(p, os.W_OK) if os.path.isdir(p) else False,
        })
    return out


# ────────────────────────── 增量归集 ──────────────────────────
def snapshot(items):
    return {it["id"]: {"mtime": round(it["mtime"], 3),
                       "size": it["size"],
                       "words": it["words"]} for it in items}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            if isinstance(s, dict):
                s.setdefault("files", {})
                return s
        except Exception:
            pass
    return {"files": {}, "initialized": False, "lastRun": 0, "lastDigestDate": ""}


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def compute_diff(prev, cur):
    added = [k for k in cur if k not in prev]
    changed = [k for k in cur if k in prev and (
        prev[k]["mtime"] != cur[k]["mtime"] or prev[k]["size"] != cur[k]["size"])]
    removed = [k for k in prev if k not in cur]
    return added, changed, removed


def brief(it):
    return {
        "id": it["id"], "title": it["title"], "root": it["root"],
        "rootName": it["rootName"], "dir": it["dir"], "name": it["name"],
        "ext": it["ext"], "genre": it["genre"], "words": it["words"],
        "size": it["size"], "mtime": it["mtime"], "excerpt": it["excerpt"],
    }


def make_highlights(added, changed, removed):
    out = []
    if added:
        group = {}
        for it in added:
            group.setdefault(it["rootName"], []).append(it)
        out.append("新增 %d 个文件，分布在 %s" % (
            len(added), "、".join("%s %d 篇" % (k, len(v)) for k, v in group.items())))
        top = sorted(added, key=lambda x: -x["words"])[:3]
        if top and top[0]["words"] > 0:
            out.append("篇幅最大：" + "、".join("《%s》%d 字" % (t["title"], t["words"]) for t in top))
    if changed:
        up = [c for c in changed if (c.get("wordsDelta") or 0) > 0]
        out.append("%d 个文件有改动%s" % (len(changed), "，其中 %d 个在扩写" % len(up) if up else ""))
    if removed:
        out.append("%d 个文件已不在索引里（移动或删除）" % len(removed))
    if not (added or changed or removed):
        out.append("今天没有新增或改动")
    return out


def run_digest(reason="auto"):
    """对比快照，产生当日增量。没有变化就只更新状态，不写日报。"""
    with _digest_lock:
        items = scan(force=True)
        cur = snapshot(items)
        by_id = {it["id"]: it for it in items}
        state = load_state()
        prev = state.get("files") or {}
        now = time.time()
        today = time.strftime("%Y-%m-%d")

        if not state.get("initialized"):
            state["files"] = cur
            state["initialized"] = True
            state["lastRun"] = now
            state["lastDigestDate"] = today
            save_state(state)
            return {"ok": True, "baseline": True, "message": "首次运行，已建立基线，本次不出日报"}

        added_ids, changed_ids, removed_ids = compute_diff(prev, cur)

        if not (added_ids or changed_ids or removed_ids):
            state["files"] = cur
            state["lastRun"] = now
            state["lastDigestDate"] = today
            save_state(state)
            return {"ok": True, "empty": True, "message": "没有新增或改动，已跳过，未产生日报"}

        added = sorted((brief(by_id[k]) for k in added_ids), key=lambda x: -x["mtime"])
        changed = []
        for k in changed_ids:
            it = brief(by_id[k])
            it["wordsDelta"] = cur[k]["words"] - prev[k].get("words", 0)
            it["oldWords"] = prev[k].get("words", 0)
            changed.append(it)
        changed.sort(key=lambda x: -x["mtime"])
        removed = [{"id": k} for k in removed_ids]

        doc = {
            "date": today,
            "generatedAt": now,
            "reason": reason,
            "added": added,
            "changed": changed,
            "removed": removed,
            "stats": {},
        }

        # 同一天多次归集时合并，不丢上午的增量
        path = os.path.join(DIGEST_DIR, today + ".json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
                merged_add = {a["id"]: a for a in old.get("added", [])}
                for a in doc["added"]:
                    merged_add[a["id"]] = a
                merged_chg = {c["id"]: c for c in old.get("changed", [])}
                for c in doc["changed"]:
                    merged_chg[c["id"]] = c
                doc["added"] = list(merged_add.values())
                doc["changed"] = list(merged_chg.values())
            except Exception:
                pass

        by_root, by_genre = {}, {}
        for a in doc["added"]:
            by_root[a["rootName"]] = by_root.get(a["rootName"], 0) + 1
            by_genre[a["genre"]] = by_genre.get(a["genre"], 0) + 1
        doc["stats"] = {
            "addedCount": len(doc["added"]),
            "changedCount": len(doc["changed"]),
            "removedCount": len(doc["removed"]),
            "wordsAdded": sum(a["words"] for a in doc["added"]),
            "byRoot": by_root,
            "byGenre": by_genre,
        }
        doc["highlights"] = make_highlights(doc["added"], doc["changed"], doc["removed"])

        os.makedirs(DIGEST_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)

        state["files"] = cur
        state["lastRun"] = now
        state["lastDigestDate"] = today
        save_state(state)
        return {"ok": True, "empty": False, "date": today,
                "added": len(doc["added"]), "changed": len(doc["changed"]),
                "message": "已生成 %s 日报：新增 %d 个，改动 %d 个" % (
                    today, len(doc["added"]), len(doc["changed"]))}


def list_digests():
    out = []
    if not os.path.isdir(DIGEST_DIR):
        return out
    for fn in sorted(os.listdir(DIGEST_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(DIGEST_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            st = d.get("stats", {})
            out.append({
                "date": d.get("date", fn[:-5]),
                "generatedAt": d.get("generatedAt", 0),
                "addedCount": st.get("addedCount", 0),
                "changedCount": st.get("changedCount", 0),
                "wordsAdded": st.get("wordsAdded", 0),
                "highlights": d.get("highlights", [])[:2],
            })
        except Exception:
            continue
    return out


def pending_diff():
    items = scan()
    cur = snapshot(items)
    state = load_state()
    if not state.get("initialized"):
        return {"ok": True, "initialized": False, "added": 0, "changed": 0, "removed": 0, "items": []}
    prev = state.get("files") or {}
    a, c, r = compute_diff(prev, cur)
    aset = set(a)
    return {
        "ok": True, "initialized": True,
        "added": len(a), "changed": len(c), "removed": len(r),
        "items": [brief(it) for it in items if it["id"] in aset][:8],
        "lastRun": state.get("lastRun", 0),
        "schedule": DIGEST_AT,
    }


def scheduler_loop():
    while True:
        try:
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            hhmm = time.strftime("%H:%M", now)
            st = load_state()
            if st.get("initialized") and st.get("lastDigestDate") != today and hhmm >= DIGEST_AT:
                r = run_digest("auto")
                print("[%s] 定时归集：%s" % (hhmm, r.get("message", "")))
        except Exception as e:
            print("调度异常：%s" % e)
        time.sleep(45)


# ────────────────────────── 路径安全 ──────────────────────────
def root_by_id(pid):
    for r in ROOTS:
        if r["id"] == pid:
            return r
    return None


def safe_abs(pid, rel):
    r = root_by_id(pid)
    if not r:
        raise ValueError("未知内容根：" + str(pid))
    rel = (rel or "").replace("\\", "/").strip("/")
    if any(seg in ("..", "") for seg in rel.split("/") if seg):
        raise ValueError("非法路径")
    base = os.path.realpath(r["path"])
    ap = os.path.realpath(os.path.join(base, rel))
    if ap != base and not ap.startswith(base + os.sep):
        raise ValueError("路径越界")
    return ap


def split_id(doc_id):
    if "/" not in doc_id:
        raise ValueError("缺少内容根前缀")
    pid, rel = doc_id.split("/", 1)
    return pid, rel


# ────────────────────────── HTTP ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "WritingHub/1.0"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 输出助手 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, ap):
        ctype, _ = mimetypes.guess_type(ap)
        ctype = ctype or "application/octet-stream"
        if ap.lower().endswith((".md", ".markdown", ".txt", ".tex")):
            ctype = "text/plain; charset=utf-8"
        try:
            with open(ap, "rb") as f:
                data = f.read()
        except OSError as e:
            self.send_json({"ok": False, "error": "读取失败：" + str(e)}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        if path in ("/", "/index.html"):
            if os.path.exists(INDEX_HTML):
                self.send_file_bytes(INDEX_HTML)
            else:
                self.send_json({"ok": False, "error": "index.html 不在同目录"}, 500)
            return

        if path == "/workbench":
            if os.path.exists(BENCH_HTML):
                self.send_file_bytes(BENCH_HTML)
            else:
                self.send_json({"ok": False, "error": "写作工作台.html 不在上级目录"}, 404)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path == "/api/meta":
            items = scan()
            roots = roots_status()
            genres = {}
            for it in items:
                genres[it["genre"]] = genres.get(it["genre"], 0) + 1
            self.send_json({
                "ok": True,
                "version": APP_VERSION,
                "author": AUTHOR,
                "roots": roots,
                "genres": genres,
                "count": len(items),
                "totalWords": sum(i["words"] for i in items),
                "totalSize": sum(i["size"] for i in items),
                "skills": scan_skills(),
                "prefs": load_prefs(),
                "appPrefs": load_app_prefs(),
                "dataDir": BASE_DIR,
                "serverTime": time.time(),
            })
            return

        if path == "/api/prefs":
            self.send_json({
                "ok": True,
                "prefs": load_app_prefs(),
                "defaults": DEFAULT_APP_PREFS,
                "dataDir": BASE_DIR,
            })
            return

        if path == "/api/roots":
            global ROOTS
            # 返回当前 roots + 自动探测到的候选 + 是否用户手动覆盖
            manual = os.path.exists(CONFIG_PATH)
            self.send_json({
                "ok": True,
                "roots": ROOTS,
                "probed": roots_probe_list(),
                "manual": manual,
                "configPath": CONFIG_PATH,
            })
            return

        if path == "/api/index":
            self.send_json({"ok": True, "items": scan(force="1" in q.get("force", []))})
            return

        if path == "/api/digest/list":
            self.send_json({"ok": True, "list": list_digests(), "schedule": DIGEST_AT})
            return

        if path == "/api/digest/pending":
            self.send_json(pending_diff())
            return

        if path == "/api/digest/get":
            lst = list_digests()
            date = (q.get("date") or [""])[0] or (lst[0]["date"] if lst else "")
            p = os.path.join(DIGEST_DIR, (date or "_") + ".json")
            if date and os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    self.send_json({"ok": True, "digest": json.load(f)})
            else:
                self.send_json({"ok": False, "error": "没有这一天的日报"}, 404)
            return

        if path == "/api/file":
            try:
                doc_id = (q.get("p") or [""])[0]
                pid, rel = split_id(doc_id)
                ap = safe_abs(pid, rel)
                if not os.path.isfile(ap):
                    self.send_json({"ok": False, "error": "文件不存在"}, 404)
                    return
                ext = os.path.splitext(ap)[1].lower()
                text = read_text(ap) if ext in TEXT_EXT else ""
                st = os.stat(ap)
                self.send_json({
                    "ok": True, "id": doc_id, "text": text,
                    "editable": ext in TEXT_EXT, "ext": ext,
                    "mtime": st.st_mtime, "size": st.st_size,
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/raw":
            try:
                doc_id = (q.get("p") or [""])[0]
                pid, rel = split_id(doc_id)
                ap = safe_abs(pid, rel)
                if not os.path.isfile(ap):
                    self.send_json({"ok": False, "error": "文件不存在"}, 404)
                    return
                self.send_file_bytes(ap)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        self.send_json({"ok": False, "error": "未知接口"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        global ROOTS
        u = urlparse(self.path)

        if u.path == "/api/roots/save":
            """接收前端传来的 roots 列表，写 config.json，立即生效"""
            data = self.read_body()
            try:
                raw = data.get("roots")
                if not isinstance(raw, list) or not raw:
                    raise ValueError("roots 不能为空数组；想回到自动探测，调 /api/roots/reset")
                cleaned = []
                seen = set()
                for r in raw:
                    p = os.path.normpath(r.get("path", "").strip())
                    if not p:
                        continue
                    if not os.path.isdir(p):
                        raise ValueError("目录不存在：" + p)
                    rid = (r.get("id") or "r").strip().replace(" ", "_")[:32]
                    if rid in seen:
                        rid += "_" + str(len(seen))
                    seen.add(rid)
                    name = (r.get("name") or os.path.basename(p) or rid).strip()[:48]
                    cleaned.append({"id": rid, "name": name, "path": p})
                save_config_roots(cleaned)
                ROOTS = load_roots()
                _cache["t"] = 0
                _meta_cache.clear()
                self.send_json({"ok": True, "roots": ROOTS, "manual": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/roots/reset":
            """删除 config.json，回到自动探测"""
            try:
                if os.path.exists(CONFIG_PATH):
                    os.remove(CONFIG_PATH)
                ROOTS = load_roots()
                _cache["t"] = 0
                _meta_cache.clear()
                self.send_json({"ok": True, "roots": ROOTS, "manual": False,
                                "probed": roots_probe_list()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/roots/add":
            """前端输入任意路径 → 后端验证存在性 → 返回可放入 roots 的条目"""
            data = self.read_body()
            try:
                raw = data.get("path", "").strip().strip('"').strip("'")
                if not raw:
                    raise ValueError("路径不能为空")
                # 支持 ~ 展开
                expanded = os.path.expanduser(raw)
                p = os.path.normpath(expanded)
                if not os.path.isdir(p):
                    raise ValueError("目录不存在：" + p)
                rid = "u_" + os.path.basename(p)[:24].replace(" ", "_")
                name = os.path.basename(p) or p
                self.send_json({"ok": True,
                                "root": {"id": rid, "name": name, "path": p}})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/open/explorer":
            data = self.read_body()
            try:
                pid, rel = split_id(data.get("p", ""))
                ap = safe_abs(pid, rel)
                if not os.path.exists(ap):
                    raise FileNotFoundError("文件不存在：" + rel)
                target = ap.replace("/", "\\")
                subprocess.Popen(["explorer", "/select,", target])
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/digest/run":
            self.read_body()
            try:
                self.send_json(run_digest("manual"))
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            return

        if u.path == "/api/save":
            data = self.read_body()
            try:
                pid, rel = split_id(data.get("p", ""))
                ap = safe_abs(pid, rel)
                if not ap.lower().endswith(tuple(TEXT_EXT)):
                    raise ValueError("该类型不支持在线编辑")
                if not os.path.isfile(ap):
                    raise FileNotFoundError("文件不存在：" + rel)
                text = data.get("text", "")
                if os.path.exists(ap):
                    bak_dir = os.path.join(BASE_DIR, ".backup")
                    os.makedirs(bak_dir, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    with open(ap, "rb") as f:
                        old = f.read()
                    with open(os.path.join(bak_dir, stamp + "-" + os.path.basename(ap)), "wb") as f:
                        f.write(old)
                with open(ap, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                _cache["t"] = 0
                _meta_cache.pop(ap, None)
                self.send_json({"ok": True, "mtime": os.stat(ap).st_mtime, "bytes": len(text.encode("utf-8"))})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/new":
            data = self.read_body()
            try:
                pid = data.get("root", "")
                sub = (data.get("dir") or "").replace("\\", "/").strip("/")
                name = (data.get("name") or "").strip()
                if not name:
                    raise ValueError("文件名不能为空")
                if not os.path.splitext(name)[1]:
                    name += ".md"
                if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                    raise ValueError("只能新建文本类文件")
                rel = (sub + "/" + name).strip("/") if sub else name
                ap = safe_abs(pid, rel)
                if os.path.exists(ap):
                    raise ValueError("同名文件已存在")
                os.makedirs(os.path.dirname(ap), exist_ok=True)
                with open(ap, "w", encoding="utf-8", newline="\n") as f:
                    f.write(data.get("text", ""))
                _cache["t"] = 0
                self.send_json({"ok": True, "id": pid + "/" + rel})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        if u.path == "/api/prefs/save":
            try:
                data = self.read_body()
                cur = save_app_prefs(data)
                self.send_json({"ok": True, "prefs": cur})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        self.send_json({"ok": False, "error": "未知接口"}, 404)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    # ① 日志文件（打包后 exe 不会有控制台，错误落盘到 BASE_DIR/writinghub.log）
    LOG_FILE = os.path.join(BASE_DIR, "writinghub.log")
    os.makedirs(BASE_DIR, exist_ok=True)
    import traceback

    # 打包后（尤其 --noconsole）控制台句柄不存在或已失效，
    # 一律把 stdout/stderr 指向日志文件，避免 print 抛 [Errno 9] Bad file descriptor
    if _is_frozen():
        try:
            _sink = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
            sys.stdout = _sink
            sys.stderr = _sink
        except Exception:
            pass

    def _log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    # ② 启动提示（打包后 onefile 模式下给用户友好提示）
    frozen = _is_frozen()
    onefile_mode = frozen and _meipass()  # onefile 会解压到 _MEIPASS
    if onefile_mode:
        _log("onefile 模式首次启动，解压到临时目录可能需要几秒…")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    host, port, verbose, list_only = "0.0.0.0", 8765, False, False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1]); i += 2; continue
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]; i += 2; continue
        if a in ("--verbose", "-v"):
            verbose = True
        if a == "--list":
            list_only = True
        i += 1

    if list_only:
        items = scan(force=True)
        for it in items:
            print("%-10s %-8s %6d字  %s" % (it["genre"], it["root"], it["words"], it["id"]))
        print("共 %d 个文件" % len(items))
        return

    # ③ 端口自适应：8765 被占了自动试 8766、8767…直到找到空的（上限 8799）
    MAX_PORT = 8799
    used_port = None
    srv = None
    for try_port in range(port, MAX_PORT + 1):
        try:
            srv = ExclusiveHTTPServer((host, try_port), Handler)
            used_port = try_port
            if try_port != port:
                _log(f"端口 {port} 被占，自动切换到 {try_port}")
            break
        except OSError as e:
            _log(f"端口 {try_port} 也被占：{e}")
            continue

    if srv is None:
        msg = f"8765–{MAX_PORT} 全部被占用，无法启动"
        _log(msg)
        if frozen:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "写作库 · 启动失败", 0x10)
        else:
            print(f"\n  ✗ {msg}")
            print(f"    关闭占用端口的程序，或用 --port 9000 指定范围外的端口")
        return

    port = used_port  # 用实际拿到的端口覆盖原默认值

    # 把实际端口写到 runtime.json（前端 bootCheck 读这个来知道访问哪个端口）
    try:
        runtime_path = os.path.join(BASE_DIR, "runtime.json")
        with open(runtime_path, "w", encoding="utf-8") as f:
            json.dump({"port": port, "host": host, "startedAt": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass

    srv.verbose = verbose

    app_prefs = load_app_prefs()

    try:
        items = scan(force=bool(app_prefs.get("refreshOnStart", True)))
    except Exception as e:
        _log(f"首次扫描异常：{e}\n{traceback.format_exc()}")
        items = []

    st = load_state()
    if not st.get("initialized"):
        try:
            r = run_digest("init")
            _log("基线建立：" + r.get("message", ""))
        except Exception as e:
            _log(f"基线建立异常：{e}")

    threading.Thread(target=scheduler_loop, daemon=True).start()

    _log(f"=== 写作库已启动 · 索引 {len(items)} 文件 ===")
    _log(f"本机 http://127.0.0.1:{port}/")
    if host == "0.0.0.0":
        _log(f"局域网 http://{lan_ip()}:{port}/")
    for r in ROOTS:
        _log(f"内容根 {r['name']} → {r['path']}")

    # ④ frozen 模式下按偏好决定是否自动打开浏览器
    if frozen and app_prefs.get("autoOpenBrowser", True):
        try:
            time.sleep(0.8)  # 等服务真的起来
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("Ctrl+C 停止")
    except Exception as e:
        _log(f"服务异常：{e}\n{traceback.format_exc()}")
    finally:
        srv.server_close()
        _log("已停止")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        _log_crash(traceback.format_exc())
        raise
