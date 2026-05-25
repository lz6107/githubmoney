# -*- coding: utf-8 -*-
"""
GitHub 项目赚钱分享频道运营机器人 - 第一版

能力：
1. 自动从 GitHub 搜索项目
2. 自动读取 README 和项目信息
3. 调用 OpenAI 生成 TG 频道长文 + 图片提示词
4. 发到你的 Telegram 私聊里审核
5. 只有你手动 /publish <草稿ID> 才会发到频道

运行：
    pip install -r requirements.txt
    cp .env.example .env
    修改 .env 后执行：python bot.py
"""

from __future__ import annotations

import base64
import html
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DRAFTS_FILE = DATA_DIR / "drafts.json"
SEEN_FILE = DATA_DIR / "seen_repos.json"

load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
DEFAULT_SEARCH_KEYWORD = os.getenv(
    "DEFAULT_SEARCH_KEYWORD", "automation self-hosted ai data dashboard telegram bot"
).strip()
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "开源项目变现局").strip()
AUTO_DRAFT_ENABLED = os.getenv("AUTO_DRAFT_ENABLED", "false").lower().strip() == "true"
AUTO_DRAFT_HOUR = int(os.getenv("AUTO_DRAFT_HOUR", "9") or "9")
AUTO_DRAFT_MINUTE = int(os.getenv("AUTO_DRAFT_MINUTE", "0") or "0")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Shanghai").strip()

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
GITHUB_API = "https://api.github.com"

# 明确排除高风险/违法/灰产方向，避免频道变成违规内容
BANNED_KEYWORDS = [
    "porn", "adult", "sex", "nsfw", "nude", "onlyfans", "xxx",
    "malware", "ransomware", "phishing", "stealer", "rat", "botnet",
    "crack", "cracked", "piracy", "carding", "spam", "ddos",
    "casino", "betting", "gambling", "drug", "weapon",
]

GOOD_TOPICS = [
    "automation", "self-hosted", "dashboard", "data", "analytics",
    "monitoring", "telegram", "bot", "ai", "llm", "crm", "workflow",
    "no-code", "low-code", "scraper", "rss", "notification", "business",
]


@dataclass
class RepoInfo:
    full_name: str
    name: str
    owner: str
    html_url: str
    description: str
    stars: int
    forks: int
    open_issues: int
    language: str
    topics: List[str]
    pushed_at: str
    created_at: str
    readme: str = ""
    score: float = 0.0


# ---------------------------
# 基础文件读写
# ---------------------------

def ensure_files() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if not DRAFTS_FILE.exists():
        DRAFTS_FILE.write_text("[]", encoding="utf-8")
    if not SEEN_FILE.exists():
        SEEN_FILE.write_text("[]", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return default
        return json.loads(text)
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(BOT_TIMEZONE))
    except Exception:
        return datetime.now()


def make_draft_id() -> str:
    return now_local().strftime("%Y%m%d%H%M%S") + str(random.randint(100, 999))


# ---------------------------
# Telegram
# ---------------------------

def tg_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TG_API}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            print(f"[Telegram error] {method}: {data}", file=sys.stderr)
        return data
    except Exception as e:
        print(f"[Telegram exception] {method}: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def split_text(text: str, limit: int = 3800) -> List[str]:
    """Telegram 单条文本上限约 4096，这里保守切 3800。"""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.6:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return parts


def send_text(chat_id: str | int, text: str) -> None:
    for part in split_text(text):
        tg_request("sendMessage", {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
        time.sleep(0.4)


def get_updates(offset: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    try:
        resp = requests.get(f"{TG_API}/getUpdates", params=payload, timeout=35)
        return resp.json()
    except Exception as e:
        print(f"[getUpdates exception] {e}", file=sys.stderr)
        return {"ok": False, "result": []}


def is_owner(message: Dict[str, Any]) -> bool:
    if not TELEGRAM_OWNER_ID:
        return True
    user_id = str(message.get("from", {}).get("id", ""))
    return user_id == TELEGRAM_OWNER_ID


# ---------------------------
# GitHub 搜索与读取
# ---------------------------

def github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-money-channel-bot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def github_get(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(url, headers=github_headers(), params=params, timeout=30)
        if resp.status_code == 403:
            print(f"[GitHub rate/permission error] {resp.text[:500]}", file=sys.stderr)
        if resp.status_code >= 400:
            print(f"[GitHub error {resp.status_code}] {url}: {resp.text[:500]}", file=sys.stderr)
            return None
        return resp.json()
    except Exception as e:
        print(f"[GitHub exception] {url}: {e}", file=sys.stderr)
        return None


def parse_repo(item: Dict[str, Any]) -> RepoInfo:
    owner = item.get("owner", {}).get("login", "")
    return RepoInfo(
        full_name=item.get("full_name", ""),
        name=item.get("name", ""),
        owner=owner,
        html_url=item.get("html_url", ""),
        description=item.get("description") or "",
        stars=int(item.get("stargazers_count") or 0),
        forks=int(item.get("forks_count") or 0),
        open_issues=int(item.get("open_issues_count") or 0),
        language=item.get("language") or "",
        topics=item.get("topics") or [],
        pushed_at=item.get("pushed_at") or "",
        created_at=item.get("created_at") or "",
    )


def contains_banned(repo: RepoInfo) -> bool:
    text = " ".join([
        repo.full_name, repo.description, repo.language, " ".join(repo.topics)
    ]).lower()
    return any(k in text for k in BANNED_KEYWORDS)


def recency_score(pushed_at: str) -> float:
    try:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - dt).days
        if days <= 7:
            return 40
        if days <= 30:
            return 30
        if days <= 90:
            return 20
        if days <= 180:
            return 10
        return 0
    except Exception:
        return 0


def repo_score(repo: RepoInfo) -> float:
    topic_text = " ".join(repo.topics).lower()
    good_topic_bonus = sum(8 for t in GOOD_TOPICS if t in topic_text)
    language_bonus = 8 if repo.language in {"Python", "TypeScript", "JavaScript", "Go", "Shell", "Vue", "PHP"} else 0
    awesome_penalty = -80 if "awesome" in repo.name.lower() else 0
    archived_penalty = 0
    return (
        min(repo.stars, 20000) / 200
        + min(repo.forks, 5000) / 250
        + recency_score(repo.pushed_at)
        + good_topic_bonus
        + language_bonus
        + awesome_penalty
        + archived_penalty
    )


def build_search_queries(keyword: str) -> List[str]:
    # GitHub Search API 的 q 支持 qualifiers；这里用多个查询提高召回率。
    since = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
    keyword = keyword.strip() or DEFAULT_SEARCH_KEYWORD
    words = [w for w in re.split(r"\s+", keyword) if w]
    base_words = words[:5]
    queries = []

    # 组合关键词 + 最近更新 + star 门槛
    if base_words:
        queries.append(" ".join(base_words) + f" stars:>200 pushed:>{since}")

    # 常用高价值 topic
    for topic in ["automation", "self-hosted", "dashboard", "monitoring", "ai", "telegram-bot", "workflow", "analytics"]:
        queries.append(f"topic:{topic} stars:>200 pushed:>{since}")

    return queries[:8]


def search_github_projects(keyword: str = "", per_query: int = 8) -> List[RepoInfo]:
    seen_repos = set(load_json(SEEN_FILE, []))
    all_repos: Dict[str, RepoInfo] = {}

    for q in build_search_queries(keyword):
        params = {
            "q": q,
            "sort": "updated",
            "order": "desc",
            "per_page": per_query,
        }
        data = github_get(f"{GITHUB_API}/search/repositories", params=params)
        if not data:
            continue
        for item in data.get("items", []):
            repo = parse_repo(item)
            if not repo.full_name:
                continue
            if repo.full_name in seen_repos:
                continue
            if contains_banned(repo):
                continue
            repo.score = repo_score(repo)
            all_repos[repo.full_name] = repo
        time.sleep(0.4)

    repos = sorted(all_repos.values(), key=lambda r: r.score, reverse=True)
    return repos[:12]


def fetch_readme(full_name: str) -> str:
    url = f"{GITHUB_API}/repos/{full_name}/readme"
    data = github_get(url)
    if not data:
        return ""
    content = data.get("content", "")
    encoding = data.get("encoding", "")
    if encoding == "base64" and content:
        try:
            raw = base64.b64decode(content).decode("utf-8", errors="ignore")
            # 去掉过长内容，避免 token 浪费
            raw = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", raw)
            raw = re.sub(r"<img[^>]*>", "", raw, flags=re.I)
            return raw[:7000]
        except Exception:
            return ""
    return ""


def get_repo_by_full_name_or_url(text: str) -> Optional[RepoInfo]:
    text = text.strip()
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", text)
    if m:
        full_name = f"{m.group(1)}/{m.group(2).replace('.git', '')}"
    else:
        full_name = text.strip().replace("https://github.com/", "").strip("/")
    if "/" not in full_name:
        return None
    data = github_get(f"{GITHUB_API}/repos/{full_name}")
    if not data:
        return None
    repo = parse_repo(data)
    repo.score = repo_score(repo)
    repo.readme = fetch_readme(repo.full_name)
    return repo


# ---------------------------
# OpenAI 调用
# ---------------------------

def openai_call(system_prompt: str, user_prompt: str, temperature: float = 0.4) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 未配置")

    # 优先使用 Responses API；失败后自动尝试 Chat Completions，增强兼容性。
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    responses_payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=responses_payload,
            timeout=90,
        )
        data = r.json()
        if r.status_code < 400:
            if data.get("output_text"):
                return str(data["output_text"]).strip()
            # 兼容不同响应结构
            output = data.get("output", [])
            chunks = []
            for item in output:
                for c in item.get("content", []) if isinstance(item, dict) else []:
                    if isinstance(c, dict):
                        if c.get("type") in {"output_text", "text"} and c.get("text"):
                            chunks.append(c["text"])
            if chunks:
                return "\n".join(chunks).strip()
        else:
            print(f"[OpenAI Responses error] {data}", file=sys.stderr)
    except Exception as e:
        print(f"[OpenAI Responses exception] {e}", file=sys.stderr)

    chat_payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=chat_payload,
        timeout=90,
    )
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI 调用失败：{data}")
    return data["choices"][0]["message"]["content"].strip()


def extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.I).strip()
    text = re.sub(r"^```\s*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    # 尝试从大段文字里抠 JSON
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def ai_choose_project(candidates: List[RepoInfo]) -> RepoInfo:
    if not candidates:
        raise RuntimeError("没有找到候选项目")

    for repo in candidates[:6]:
        if not repo.readme:
            repo.readme = fetch_readme(repo.full_name)

    candidate_text = []
    for i, r in enumerate(candidates[:6], 1):
        candidate_text.append(
            f"""
{i}. {r.full_name}
URL: {r.html_url}
Description: {r.description}
Stars: {r.stars}, Forks: {r.forks}, Language: {r.language}, Topics: {', '.join(r.topics[:8])}
README 摘要: {r.readme[:900] if r.readme else '无'}
""".strip()
        )

    system = """你是一个中文 Telegram 频道的选题编辑，频道定位是“GitHub 开源项目赚钱分享”。
你的任务是从候选 GitHub 项目中挑一个最适合写成“副业/接单/工具服务变现拆解”的项目。
必须避开色情、盗版、赌博、黑客攻击、钓鱼、恶意软件、诈骗、侵权和灰产方向。
不要选择纯 awesome 列表、纯论文、纯玩具 demo、长期不维护项目。
只输出 JSON，不要输出多余解释。"""
    user = f"""
请从下面候选项目中选择 1 个最适合今天频道发布的项目。
选择标准：
1. 普通人或轻技术人员有机会包装成服务
2. 有明确使用场景和付费对象
3. 项目维护相对活跃
4. 文案能讲清楚上手门槛、变现路径、风险

输出 JSON 格式：
{{
  "selected_full_name": "owner/repo",
  "reason": "为什么选它，50字内",
  "risk_note": "需要提醒的风险，50字内"
}}

候选项目：
{chr(10).join(candidate_text)}
"""
    try:
        result = openai_call(system, user, temperature=0.2)
        data = extract_json(result)
        selected = data.get("selected_full_name", "").strip()
        for repo in candidates:
            if repo.full_name.lower() == selected.lower():
                return repo
    except Exception as e:
        print(f"[AI choose failed] {e}", file=sys.stderr)

    return candidates[0]


def safe_filename(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\-_.]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:50] or "project"


def build_image_prompt(repo: RepoInfo, draft: Dict[str, Any]) -> str:
    image_title = draft.get("image_title") or repo.name
    subtitle = draft.get("image_subtitle") or draft.get("one_sentence") or repo.description or "开源项目变现拆解"
    difficulty = draft.get("difficulty") or "中等"
    tech = draft.get("tech_threshold") or "Docker / 服务器 / 配置"
    audience = draft.get("audience") or "副业接单 / 小团队服务"
    monetization = draft.get("monetization") or "部署 / 定制 / 维护"
    visual = draft.get("visual") or "代码界面、数据面板、工作流节点、商业信息卡片"

    return (
        f"16:9 横版科技风 Telegram 频道封面，主题为“GitHub 开源项目变现”，"
        f"左上角放频道名“{CHANNEL_NAME}”，顶部小字放“开源项目拆解”，"
        f"中间大字写“{image_title}”，副标题写“{subtitle}”，"
        f"底部用信息标签展示：“上手难度：{difficulty}”“技术门槛：{tech}”“适合人群：{audience}”“变现方向：{monetization}”。"
        f"背景加入{visual}等元素，深蓝灰科技色调，信息卡片式排版，简洁高级，文字清晰，强识别度，适合 Telegram 频道配图。"
    )


def ai_generate_draft(repo: RepoInfo) -> Dict[str, Any]:
    if not repo.readme:
        repo.readme = fetch_readme(repo.full_name)

    system = """你是一个中文 Telegram 频道的内容主编，频道定位是“GitHub 开源项目赚钱分享”。
你的风格：实用、清醒、有吸引力，但不夸大，不承诺收益，不鼓励违法违规。
你要把 GitHub 项目拆解成：项目能做什么、上手难度、需要门槛、适合谁、怎么包装成服务赚钱、风险和避坑。
必须遵守：
1. 不写色情、赌博、诈骗、黑产、盗版、恶意软件、攻击入侵相关变现。
2. 不承诺“稳赚”“月入多少必成”。
3. 要写清楚技术门槛、资金门槛、时间门槛、运营门槛。
4. 文字适合 Telegram 频道，一篇发完，尽量控制在 2500-3800 个中文字符。
5. 只输出 JSON，不要输出 Markdown 代码块，不要输出多余解释。"""

    user = f"""
请基于下面 GitHub 项目资料，生成一篇可以直接发到 Telegram 频道的中文长文，并生成图片提示词所需字段。

项目资料：
项目名：{repo.name}
完整名：{repo.full_name}
地址：{repo.html_url}
简介：{repo.description}
Stars：{repo.stars}
Forks：{repo.forks}
语言：{repo.language}
Topics：{', '.join(repo.topics[:12])}
最近更新：{repo.pushed_at}
README：
{repo.readme[:6500]}

输出严格 JSON，字段如下：
{{
  "title": "文章标题",
  "one_sentence": "一句话用途，适合放在图片副标题",
  "difficulty": "极低/低/中等/较高/高",
  "tech_threshold": "技术门槛，短句",
  "money_threshold": "资金门槛，短句",
  "time_threshold": "时间门槛，短句",
  "operation_threshold": "运营门槛，短句",
  "audience": "适合人群，短句",
  "not_for": "不适合人群，短句",
  "monetization": "变现方向，短句，适合放在图片上",
  "visual": "图片背景应该出现什么元素，短句",
  "image_title": "图片主标题，一般用项目名",
  "image_subtitle": "图片副标题",
  "post": "完整 Telegram 频道正文，必须包含项目简介、能做什么、上手难度、需要门槛、适合人群、不适合人群、变现路径、收费参考、避坑提醒、结论、标签"
}}
"""
    result = openai_call(system, user, temperature=0.45)
    data = extract_json(result)
    if not data or "post" not in data:
        # 兜底，避免一次失败完全不可用
        post = fallback_post(repo)
        data = {
            "title": f"【开源项目变现】{repo.name}：一个值得研究的开源工具",
            "one_sentence": repo.description or "开源工具 / 服务包装 / 副业接单",
            "difficulty": "中等",
            "tech_threshold": "服务器 / 部署 / 基础配置",
            "money_threshold": "服务器成本约20-50元/月",
            "time_threshold": "首次部署约1-2小时",
            "operation_threshold": "需要找到真实付费场景",
            "audience": "副业接单 / 小团队服务",
            "not_for": "完全不想折腾工具的人",
            "monetization": "部署 / 定制 / 维护",
            "visual": "代码界面、数据面板、工作流节点",
            "image_title": repo.name,
            "image_subtitle": repo.description or "开源项目变现拆解",
            "post": post,
        }

    image_filename = f"{safe_filename(repo.name)}-1.png"
    image_prompt = build_image_prompt(repo, data)

    draft_id = make_draft_id()
    draft = {
        "id": draft_id,
        "created_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft",
        "repo": {
            "full_name": repo.full_name,
            "name": repo.name,
            "url": repo.html_url,
            "description": repo.description,
            "stars": repo.stars,
            "forks": repo.forks,
            "language": repo.language,
            "topics": repo.topics,
            "pushed_at": repo.pushed_at,
        },
        "title": data.get("title") or f"【开源项目变现】{repo.name}",
        "image": image_filename,
        "image_prompt": image_prompt,
        "post": data.get("post", "").strip(),
        "meta": data,
    }
    return draft


def fallback_post(repo: RepoInfo) -> str:
    return f"""【开源项目变现】{repo.name}：一个值得研究的开源工具

项目地址：{repo.html_url}

项目简介：
{repo.description or '这个项目在 GitHub 上有一定关注度，适合进一步研究它能不能包装成服务。'}

基础信息：
Stars：{repo.stars}
Forks：{repo.forks}
主要语言：{repo.language or '未知'}
最近更新：{repo.pushed_at}

上手难度：中等

需要门槛：
技术门槛：需要会看 README、基础部署、配置环境变量，最好懂一点服务器和命令行。
资金门槛：通常需要一台服务器，前期成本可以控制在几十元/月。
时间门槛：第一次部署和测试可能需要 1-2 小时，真正难点是后期维护和交付。
运营门槛：需要找到具体付费场景，不能只停留在“这个项目很酷”。

变现路径：
1. 帮别人部署
2. 做模板化交付
3. 给小团队做定制配置
4. 提供长期维护
5. 写教程或做小型付费社群

避坑提醒：
不要承诺一定赚钱，不要把开源项目包装成暴富项目。客户买的不是 GitHub 项目本身，而是你帮他省时间、省试错、解决具体问题。

结论：
这个项目可以作为候选选题继续拆解。真正能不能变现，要看你能不能找到具体场景、目标客户和交付方式。

#GitHub项目 #开源项目 #副业项目 #自动化工具""".strip()


# ---------------------------
# 草稿管理
# ---------------------------

def save_draft(draft: Dict[str, Any]) -> None:
    drafts = load_json(DRAFTS_FILE, [])
    drafts.append(draft)
    save_json(DRAFTS_FILE, drafts)

    seen = set(load_json(SEEN_FILE, []))
    full_name = draft.get("repo", {}).get("full_name")
    if full_name:
        seen.add(full_name)
    save_json(SEEN_FILE, sorted(seen))


def get_draft(draft_id: str) -> Optional[Dict[str, Any]]:
    drafts = load_json(DRAFTS_FILE, [])
    for d in drafts:
        if str(d.get("id")) == str(draft_id):
            return d
    return None


def get_last_draft() -> Optional[Dict[str, Any]]:
    drafts = load_json(DRAFTS_FILE, [])
    if not drafts:
        return None
    return drafts[-1]


def update_draft_status(draft_id: str, status: str) -> bool:
    drafts = load_json(DRAFTS_FILE, [])
    ok = False
    for d in drafts:
        if str(d.get("id")) == str(draft_id):
            d["status"] = status
            if status == "published":
                d["published_at"] = now_local().strftime("%Y-%m-%d %H:%M:%S")
            ok = True
            break
    if ok:
        save_json(DRAFTS_FILE, drafts)
    return ok


def render_review_package(draft: Dict[str, Any]) -> str:
    repo = draft.get("repo", {})
    return f"""草稿ID：{draft.get('id')}
状态：{draft.get('status')}
项目：{repo.get('full_name')}
地址：{repo.get('url')}
Stars：{repo.get('stars')}｜Forks：{repo.get('forks')}｜语言：{repo.get('language')}
图片：{draft.get('image')}

图片提示词：
{draft.get('image_prompt')}

TG正文：
{draft.get('post')}

发送到频道：/publish {draft.get('id')}
丢弃草稿：/discard {draft.get('id')}""".strip()


def render_image_only(draft: Dict[str, Any]) -> str:
    repo = draft.get("repo", {})
    return f"""项目：{repo.get('full_name')}
图片：{draft.get('image')}
提示词：{draft.get('image_prompt')}""".strip()


def render_post_only(draft: Dict[str, Any]) -> str:
    return draft.get("post", "").strip()


# ---------------------------
# 业务动作
# ---------------------------

def generate_new_draft(keyword: str = "") -> Dict[str, Any]:
    repos = search_github_projects(keyword)
    if not repos:
        raise RuntimeError("没有搜到合适项目。可以换关键词，例如：/find automation 或 /find dashboard")
    repo = ai_choose_project(repos)
    repo.readme = repo.readme or fetch_readme(repo.full_name)
    draft = ai_generate_draft(repo)
    save_draft(draft)
    return draft


def generate_draft_for_repo(repo_text: str) -> Dict[str, Any]:
    repo = get_repo_by_full_name_or_url(repo_text)
    if not repo:
        raise RuntimeError("没识别到仓库。格式示例：/draft https://github.com/n8n-io/n8n 或 /draft n8n-io/n8n")
    if contains_banned(repo):
        raise RuntimeError("这个项目命中高风险关键词，第一版不建议做成频道内容。")
    draft = ai_generate_draft(repo)
    save_draft(draft)
    return draft


def list_candidates(keyword: str = "") -> str:
    repos = search_github_projects(keyword)
    if not repos:
        return "没有搜到合适候选项目。"
    lines = ["候选项目："]
    for i, r in enumerate(repos[:8], 1):
        lines.append(
            f"{i}. {r.full_name}\n"
            f"   Stars: {r.stars}｜Forks: {r.forks}｜Lang: {r.language}\n"
            f"   {r.description}\n"
            f"   {r.html_url}"
        )
    return "\n\n".join(lines)


def publish_draft(draft_id: str) -> str:
    if not TELEGRAM_CHANNEL_ID:
        return "TELEGRAM_CHANNEL_ID 没配置，不能发频道。"
    draft = get_draft(draft_id)
    if not draft:
        return "没找到这个草稿ID。"
    if draft.get("status") == "published":
        return "这个草稿已经发过了。"
    post = draft.get("post", "").strip()
    if not post:
        return "这个草稿没有正文。"
    send_text(TELEGRAM_CHANNEL_ID, post)
    update_draft_status(draft_id, "published")
    return f"已发送到频道：{draft_id}"


# ---------------------------
# 命令处理
# ---------------------------

def help_text() -> str:
    return f"""GitHub 项目赚钱分享频道机器人

核心命令：
/find [关键词]
自动找 GitHub 项目 + AI 生成频道稿 + 图片提示词
例：/find automation

/candidates [关键词]
只查看候选项目，不生成草稿
例：/candidates dashboard

/draft <GitHub地址或owner/repo>
指定某个项目生成频道稿
例：/draft https://github.com/n8n-io/n8n

/last
查看最新草稿

/image <草稿ID>
只看某个草稿的图片提示词

/post <草稿ID>
只看某个草稿的 TG 正文

/publish <草稿ID>
手动确认发送到频道

/discard <草稿ID>
丢弃草稿

注意：机器人不会自动发频道，只有你手动 /publish 才会发。""".strip()


def handle_command(chat_id: int, text: str) -> None:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    try:
        if cmd in {"/start", "/help"}:
            send_text(chat_id, help_text())

        elif cmd in {"/find", "/today", "/run"}:
            send_text(chat_id, "开始自动找项目并生成草稿，完成后会发给你审核。")
            draft = generate_new_draft(arg)
            send_text(chat_id, render_review_package(draft))

        elif cmd == "/candidates":
            send_text(chat_id, "正在搜索候选项目……")
            send_text(chat_id, list_candidates(arg))

        elif cmd == "/draft":
            if not arg:
                send_text(chat_id, "用法：/draft https://github.com/owner/repo")
                return
            send_text(chat_id, "正在读取指定 GitHub 项目并生成草稿……")
            draft = generate_draft_for_repo(arg)
            send_text(chat_id, render_review_package(draft))

        elif cmd == "/last":
            draft = get_last_draft()
            if not draft:
                send_text(chat_id, "还没有草稿。先用 /find 生成一个。")
            else:
                send_text(chat_id, render_review_package(draft))

        elif cmd == "/image":
            draft_id = arg or (get_last_draft() or {}).get("id", "")
            draft = get_draft(str(draft_id)) if draft_id else None
            if not draft:
                send_text(chat_id, "没找到草稿。用法：/image 草稿ID")
            else:
                send_text(chat_id, render_image_only(draft))

        elif cmd == "/post":
            draft_id = arg or (get_last_draft() or {}).get("id", "")
            draft = get_draft(str(draft_id)) if draft_id else None
            if not draft:
                send_text(chat_id, "没找到草稿。用法：/post 草稿ID")
            else:
                send_text(chat_id, render_post_only(draft))

        elif cmd == "/publish":
            if not arg:
                send_text(chat_id, "用法：/publish 草稿ID")
                return
            send_text(chat_id, publish_draft(arg))

        elif cmd == "/discard":
            if not arg:
                send_text(chat_id, "用法：/discard 草稿ID")
                return
            ok = update_draft_status(arg, "discarded")
            send_text(chat_id, "已丢弃。" if ok else "没找到这个草稿ID。")

        else:
            send_text(chat_id, "未知命令。发送 /help 查看用法。")

    except Exception as e:
        send_text(chat_id, f"出错了：{e}")
        print(f"[handle_command error] {e}", file=sys.stderr)


# ---------------------------
# 可选：每日自动生成草稿到私聊
# ---------------------------

def auto_draft_tick(state: Dict[str, Any]) -> None:
    if not AUTO_DRAFT_ENABLED or not TELEGRAM_OWNER_ID:
        return
    n = now_local()
    today_key = n.strftime("%Y-%m-%d")
    if state.get("last_auto_draft_date") == today_key:
        return
    if n.hour == AUTO_DRAFT_HOUR and n.minute >= AUTO_DRAFT_MINUTE:
        try:
            send_text(TELEGRAM_OWNER_ID, "每日自动草稿开始生成：只发给你审核，不会自动发频道。")
            draft = generate_new_draft("")
            send_text(TELEGRAM_OWNER_ID, render_review_package(draft))
            state["last_auto_draft_date"] = today_key
        except Exception as e:
            send_text(TELEGRAM_OWNER_ID, f"每日自动草稿生成失败：{e}")
            state["last_auto_draft_date"] = today_key


# ---------------------------
# 主循环
# ---------------------------

def check_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if missing:
        raise RuntimeError("缺少配置：" + ", ".join(missing) + "。请复制 .env.example 为 .env 并填写。")


def main() -> None:
    ensure_files()
    check_config()
    print("GitHub 项目赚钱分享频道机器人已启动。")
    print("发送 /help 给机器人查看命令。")

    offset: Optional[int] = None
    state: Dict[str, Any] = {}

    while True:
        auto_draft_tick(state)
        updates = get_updates(offset)
        if not updates.get("ok"):
            time.sleep(3)
            continue
        for update in updates.get("result", []):
            offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text") or ""
            if not chat_id or not text:
                continue
            if not is_owner(message):
                send_text(chat_id, "这个机器人是私用的。")
                continue
            if text.startswith("/"):
                handle_command(chat_id, text)
            else:
                send_text(chat_id, "发送 /help 查看命令。")
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已退出。")
    except Exception as e:
        print(f"启动失败：{e}", file=sys.stderr)
        sys.exit(1)
