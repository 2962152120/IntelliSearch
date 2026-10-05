"""Query 解析与面向 AI 的 Query 改写。

功能(需求二.1 / 二.2):
- 分词(中英混合)
- 布尔检索: AND / OR / NOT, -排除词, "精确短语", site: 限定
- 时间范围过滤: 近1天/1周/1月/自定义
- Query 改写: 把大模型的自然语言问题拆成适合搜索引擎的关键词

典型改写::

    铭凡UM880 Pro的NPU算力多少
    -> 铭凡 UM880 Pro NPU TOPS 参数 规格      (主语型)
    -> 铭凡 UM880 Pro NPU 算力 官方参数        (保留原意型)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from ..models import Freshness, ParsedQuery, QueryOptions, now_utc

# --------------------------------------------------------------------------
# 停用词 / 疑问词
# --------------------------------------------------------------------------
STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "或", "有", "我", "你", "他", "它", "这", "那",
    "吗", "呢", "吧", "啊", "请", "请问", "帮我", "我想", "知道", "一下", "什么",
    "怎么", "如何", "为什么", "多少", "几个", "哪些", "哪个", "是否", "可以",
    "能", "会", "要", "对于", "关于", "一个", "有没有", "求", "求推荐", "谢谢",
    "the", "a", "an", "is", "are", "of", "to", "in", "for", "on", "what",
    "how", "why", "when", "which", "who", "do", "does", "please", "tell", "me",
}

# 疑问句式(整句剥离)
QUESTION_PATTERNS = [
    r"^\s*(请问|帮我|我想知道|我想问一下|麻烦问下|谁能告诉我|求问)\s*",
    r"^\s*(what|how|why|when|which|who|is|are|does|do|can|could)\s+",
]

# 场景词扩展表: 命中关键词 -> 追加的检索词
INTENT_RULES: List[Tuple[str, List[str]]] = [
    (r"算力|TOPS|NPU|AI性能|神经网络|推理性能", ["TOPS", "参数", "规格"]),
    (r"参数|规格|配置|详细参数", ["参数", "规格", "详细"]),
    (r"价格|多少钱|售价|报价|贵不贵|性价比", ["价格", "报价"]),
    (r"评测|测评|实测|怎么样|好不好|值得买|翻车", ["评测", "实测", "体验"]),
    (r"对比|vs|还是|区别|哪个好", ["对比", "区别"]),
    (r"驱动|固件|bios|升级|更新", ["驱动", "固件", "更新"]),
    (r"功耗|温度|散热|噪音|风扇", ["功耗", "散热", "实测"]),
    (r"接口|拓展|硬盘|内存|插槽", ["接口", "规格", "扩展"]),
    (r"文档|教程|怎么用|使用方法|配置方法", ["文档", "教程"]),
    (r"发布|上市|什么时候出|新闻", ["发布", "上市"]),
    (r"论文|研究|学术", ["paper", "research"]),
]

# 时间范围
TIME_RULES: List[Tuple[re.Pattern, Freshness]] = [
    (re.compile(r"(近|最近|过去)?\s*(1|一|24)\s*(天|日|小时)", re.I), Freshness.DAY),
    (re.compile(r"(近|最近|过去)?\s*(1|一)\s*(周|星期|礼拜)", re.I), Freshness.WEEK),
    (re.compile(r"(近|最近|过去)?\s*(1|一|30)\s*(个?月)", re.I), Freshness.MONTH),
    (re.compile(r"(近|最近|过去)?\s*(1|一)\s*年", re.I), Freshness.YEAR),
    (re.compile(r"今天|今日|最新", re.I), Freshness.DAY),
    (re.compile(r"本周", re.I), Freshness.WEEK),
    (re.compile(r"本月", re.I), Freshness.MONTH),
]

_YEAR_RANGE = re.compile(r"(20\d{2})\s*年?(?:\s*(以来|之后|起))?")
_CUSTOM_RANGE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})\s*(?:到|至|-|~)\s*"
                           r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")

# 有价值的 token: 型号/数字/英文/技术词
MODEL_RE = re.compile(r"[A-Za-z]+[\w\-]*\d+[\w\-]*|\d+[A-Za-z]+[\w\-]*", re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
ENG_RE = re.compile(r"[A-Za-z][A-Za-z0-9\.\+#\-_]*")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def now() -> datetime:
    return now_utc()


def freshness_to_delta(f: Freshness) -> Optional[timedelta]:
    return {
        Freshness.DAY: timedelta(days=1),
        Freshness.WEEK: timedelta(days=7),
        Freshness.MONTH: timedelta(days=30),
        Freshness.YEAR: timedelta(days=365),
        Freshness.ANY: None,
    }.get(f)


# --------------------------------------------------------------------------
# 分词
# --------------------------------------------------------------------------
# 单字切分点: 只有"出现在词边界也不会把实词切碎"的虚词才允许切分。
#
# 之前这里既手写了一批字, 又把 STOPWORDS 里所有单字词的首字并进来
# (CJK_SPLIT_CHARS |= {w[0] ...}), 导致 能/会/要/最/为/什/可/一/些 这些
# **词内字**也成了切分点, 实词被切烂:
#     人工智能 -> 人工智   (能 被切)
#     最新     -> 新       (最 被切)
#     为啥     -> 啥       (为 被切)   <- 最严重
# 为啥被切成"啥"是**假成功的源头**: 查询 "为啥我的 fastapi 请求一并发就502"
# 的检索词里混进了 "啥", 于是搜"啥"的字典释义页也能命中相关性闸门,
# 一整批垃圾结果被当成正常结果返回(实测 6 条全是"啥"字词典页)。
#
# 修法两条:
# 1) 不再从 STOPWORDS 注入单字(停用词表是"整词"级别的, 不能拆成单字用);
# 2) 只保留真正的句法虚词, 且切分点只在"缓冲区已积累 >=2 字"时才生效,
#    这样单字尾字(如 多少/怎么样 的尾字)不会单独成词。
CJK_SPLIT_CHARS = set(
    "的了着是在和与或但而吧吗呢啊请呀嘛哦嗯"
    "把被让给从向往到等比及并却"
    "我你他她它咱这那有"
    "多少几个么怎如何为什哪些可对"
    "于关一下没从到过还很就更最都也就才")


# 说明: 这里**不能**有"段首虚词直接丢弃"的规则。中文虚词大量参与构词
# (并发/和平/在线/是非/给力/等于/及时/比较/被子/着急/并且/在家/请求...),
# 任何"丢掉段首字"的启发式都会把实词整词吃掉 —— 实测 16 个常用词里 15 个
# 被切成空或残缺, 查询 "FastAPI 并发 502" 的 "并发" 直接消失, "请求超时"
# 被切成 "求超时"。
#
# 正确做法: 切分点只在**已经切出完整实词之后**生效, 切分点自身永不单独
# 成词, 且切不出 >=2 字片段时回退整段。段首字一律保留。
_TAIL_DROP = set("的地得了着过")


# 多字疑问词/虚词: 切分时必须**整体**识别, 不能逐字切。
# 否则 "为什么" 被切成 "为什"+"么选择", "有没有" 被切成 "有没"+"有便宜",
# 这些 2 字碎片长度 >=2, 会被 fusion 的 _strong_terms 当作实词,
# 于是"为什的拼音"这类词典页又能通过相关性闸门 —— 假成功换了个词复发。
MULTI_CJK_STOP = (
    "为什么", "为啥", "什么", "怎么", "怎样", "怎麽", "如何", "哪些", "哪个",
    "哪种", "哪里", "多少", "几个", "是否", "可以", "能否", "有没有",
    "是不是", "会不会", "要不要", "请问", "一个", "一下", "一些", "一点",
)

# 结构助词: 只作分隔符, 不进结果 —— 否则 "有没有便宜的显卡" 会产出
# "的显卡" 这种伪词。
#
# 注意 "着" 只能放在**多字助词** 里(看着/接着) 才算助词; 段内的 "着"
# 是实词的一部分(着急/睡着/着重), 所以单字 "着" 不能无条件丢弃。
_TAIL_DROP = set("的地得了过")


def _split_cjk(seg: str) -> List[str]:
    """按虚词切分中文串, 产出 >=2 字的实词片段。

    规则(每条都有对应的真实缺陷):
    1. 多字疑问词整体跳过, 绝不逐字拆 —— 否则 "为什么" 变成
       "为什"+"么选择", 这些 2 字伪词会被 fusion 当作实词, 让
       "为什的拼音"词典页通过相关性闸门;
    2. 结构助词(的/地/得/了...)只作分隔, 不进结果 —— 否则产出
       "的显卡" 这种伪词;
    3. 切分点只在缓冲区已积累 >=2 字时生效, 且切分点自身永不单独成词
       —— 否则产出 "啥"(单字无检索价值, 却能匹配"啥字词典页");
    4. **段首字一律保留**, 不做"丢弃段首虚词"的优化 —— 中文虚词大量
       参与构词(并发/和平/在线/是非/给力/请求...), 丢段首会整词丢失;
    5. 若整段切不出任何 >=2 字片段, 回退为整段, 保证不丢输入。
    """
    parts: List[str] = []
    buf = ""
    i = 0
    while i < len(seg):
        matched = None
        for w in MULTI_CJK_STOP:
            if seg.startswith(w, i):
                matched = w
                break
        if matched:
            if len(buf) >= 2:
                parts.append(buf)
            buf = ""
            i += len(matched)          # 疑问词整体跳过, 不进 buf
            continue
        ch = seg[i]
        i += 1
        if ch in _TAIL_DROP:
            if len(buf) >= 2:
                parts.append(buf)
            buf = ""
            continue
        if ch in CJK_SPLIT_CHARS and len(buf) >= 2:
            parts.append(buf)           # 已有完整实词, 用虚词作分隔
            buf = ""
            continue
        buf += ch                      # 段首字/词内字: 必须留在缓冲区
    if len(buf) >= 2:
        parts.append(buf)
    # 注意: 这里**不做**"切不出就整段保留"的兜底。那会把
    # "为啥我的" 这种疑问短语整体变成检索词(既无检索价值, 又长到无法与
    # 真实结果匹配, 还会污染相关性闸门)。段首字保留已由"切分点只在
    # len(buf)>=2 时生效"保证: 并发/和平/在线/请求 等词不会丢。
    return parts


def tokenize(text: str) -> List[str]:
    """中英混合分词, 结果按在原文本中的出现顺序排列。

    中文: 先按虚词切分, 再对每段做 2-gram(对检索足够, 无需词典);
    英文/数字: 按连续串切分;
    型号(如 UM880 / R7-8845HS): 整体保留。
    """
    if not text:
        return []
    text = text.strip()
    found: List[Tuple[int, str]] = []

    # 型号整体保留
    for m in MODEL_RE.finditer(text):
        found.append((m.start(), m.group(0)))
    rest = MODEL_RE.sub(lambda m: " " * len(m.group(0)), text)

    for m in ENG_RE.finditer(rest):
        t = m.group(0)
        if len(t) >= 2:
            found.append((m.start(), t))
    rest2 = ENG_RE.sub(lambda m: " " * len(m.group(0)), rest)

    for m in CJK_RE.finditer(rest2):
        seg = m.group(0)
        base = m.start()
        offset = 0
        subs = _split_cjk(seg)
        if not subs and len(seg) == 1 and seg not in STOPWORDS:
            # 整段只有 1 个字且不是停用词(如 "钱"): 切分器会返回空,
            # 这里保留原字, 否则该字会被静默丢掉。
            subs = [seg]
        for sub in subs:
            idx = seg.find(sub, offset)
            pos = base + (idx if idx >= 0 else offset)
            offset = idx + len(sub) if idx >= 0 else offset + len(sub)
            if len(sub) == 1:
                found.append((pos, sub))
            elif len(sub) <= 8:
                # 连续实词串整体作为一个词(搜索引擎自带中文分词, 传整段更准)
                found.append((pos, sub))
            else:
                # 超长段才退化为 2-gram, 避免生成过长的无效词
                for i in range(0, len(sub) - 1, 2):
                    found.append((pos + i, sub[i:i + 2]))
                found.append((pos, sub[:6]))

    for m in NUM_RE.finditer(rest2):
        found.append((m.start(), m.group(0)))

    found.sort(key=lambda x: x[0])
    seen, out = set(), []
    for _, t in found:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def core_terms(text: str, max_terms: int = 12) -> List[str]:
    """抽取核心检索词: 分词后去停用词。"""
    toks = tokenize(text)
    out = [t for t in toks if t.lower() not in STOPWORDS and len(t) >= 1]
    # 中文 2-gram 中纯停用词组合较多, 再过滤一次
    out = [t for t in out if not all(c in STOPWORDS for c in t)]
    return out[:max_terms]


# --------------------------------------------------------------------------
# 语法解析
# --------------------------------------------------------------------------
def parse_boolean(query: str) -> Tuple[List[str], List[str], List[str], List[str], str]:
    """解析布尔语法, 返回 (must, should, not, phrases, 清洗后的 query)。

    支持: "精确短语" / -排除词 / site:xxx / AND / OR / NOT(大小写均可)
    """
    phrases = []
    for m in re.finditer(r'"([^"]+)"|\'([^\']+)\'', query):
        p = (m.group(1) or m.group(2) or "").strip()
        if p:
            phrases.append(p)

    rest = re.sub(r'"[^"]*"|\'[^\']*\'', " ", query)

    nots: List[str] = []
    must: List[str] = []
    should: List[str] = []

    # -排除词
    rest = re.sub(r"(?:^|\s)-(\S+)", lambda m: _collect(nots, m.group(1)), rest)
    # NOT 词
    rest = re.sub(r"(?i)\bNOT\s+(\S+)", lambda m: _collect(nots, m.group(1)), rest)

    # 显式 AND / OR 分段
    parts = re.split(r"(?i)\s+OR\s+", rest)
    if len(parts) > 1:
        should.extend(_strip_empty(parts))
    else:
        rest = re.sub(r"(?i)\s+AND\s+", " ", rest)
        must.extend(_strip_empty([rest]))

    # 清洗后的文本: 剔除已解析的排除词, 供后续分词使用
    cleaned = rest.strip()
    for w in nots:
        cleaned = re.sub(rf"(?i)\b{re.escape(w)}\b", " ", cleaned)
    return must, should, nots, phrases, re.sub(r"\s+", " ", cleaned).strip()


def _collect(bucket: List[str], value: str) -> str:
    v = value.strip()
    if v:
        bucket.append(v)
    return " "


def _strip_empty(parts: List[str]) -> List[str]:
    return [p.strip() for p in parts if p and p.strip()]


def extract_site(query: str) -> Tuple[Optional[str], str]:
    """抽出 site:xxx 并返回剩余 query。"""
    m = re.search(r"site:\s*([A-Za-z0-9\.\-]+)", query, re.I)
    if not m:
        return None, query
    site = m.group(1).lower()
    return site, re.sub(r"site:\s*[A-Za-z0-9\.\-]+", " ", query, flags=re.I).strip()


def extract_time(query: str) -> Tuple[Freshness, Optional[datetime], Optional[datetime], str]:
    """解析时间范围。返回 (freshness, time_from, time_to, 剩余 query)。"""
    freshness = Freshness.ANY
    t_from = t_to = None

    m = _CUSTOM_RANGE.search(query)
    if m:
        try:
            t_from = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                              tzinfo=timezone.utc)
            t_to = datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)),
                            tzinfo=timezone.utc)
            return freshness, t_from, t_to, _CUSTOM_RANGE.sub(" ", query).strip()
        except ValueError:
            pass

    for rx, f in TIME_RULES:
        m = rx.search(query)
        if m:
            freshness = f
            # 从 query 中剥离时间短语, 避免污染关键词(时间已由 freshness 承载)
            query = (query[: m.start()] + " " + query[m.end():]).strip()
            break
    return freshness, t_from, t_to, query


# --------------------------------------------------------------------------
# Query 改写
# --------------------------------------------------------------------------
def strip_question(text: str) -> str:
    s = text.strip()
    for pat in QUESTION_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    s = re.sub(r"[?？。！!，,、]+$", "", s).strip()
    return s or text.strip()


def intent_expansion(text: str) -> List[str]:
    """按意图追加检索词。"""
    extra: List[str] = []
    for pat, words in INTENT_RULES:
        if re.search(pat, text, re.I):
            for w in words:
                if w.lower() not in text.lower():
                    extra.append(w)
            break        # 只取首个命中的意图, 避免 query 膨胀
    return extra


def rewrite(query: str, options: QueryOptions = None,
            rewriter: Callable[[str], List[str]] = None) -> ParsedQuery:
    """把自然语言问题改写为适合搜索引擎的 query。

    rewriter: 可选的外部改写器(如接 LLM), 返回若干改写后的 query;
              提供时以其结果为主, 规则改写作为兜底。
    """
    options = options or QueryOptions()
    raw = query.strip()
    site, q = extract_site(raw)
    freshness, t_from, t_to, q = extract_time(q)
    must, should, nots, phrases, cleaned = parse_boolean(q)

    body = strip_question(" ".join(phrases) if phrases else (cleaned or q))
    terms = core_terms(body)
    extra = intent_expansion(body)

    # 主 query: 核心词 + 意图扩展(去重)
    seen, ordered = set(), []
    for t in terms + extra:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        ordered.append(t)
    effective = " ".join(ordered).strip() or body.strip() or raw

    if site:
        effective = f"{effective} site:{site}"

    expanded: List[str] = []
    if rewriter:
        try:
            for q2 in rewriter(raw) or []:
                q2 = (q2 or "").strip()
                if q2 and q2 not in expanded:
                    expanded.append(q2)
        except Exception:      # noqa: BLE001 - 外部改写器失败不影响主流程
            pass
    # 规则生成的备选 query
    alt = " ".join(dict.fromkeys(terms)).strip()
    if alt and alt != effective and alt not in expanded:
        expanded.append(alt)
    if phrases:
        p = " ".join(phrases)
        if p not in expanded and p != effective:
            expanded.insert(0, p)

    return ParsedQuery(
        raw=raw, effective=effective, terms=terms, must=must or terms,
        should=should, not_=nots, site=site, expanded=expanded[:3],
        rewritten=effective.strip().lower() != raw.strip().lower(),
    )


def apply_options(parsed: ParsedQuery, options: QueryOptions) -> ParsedQuery:
    """用显式 options 覆盖从 query 中推断出的参数。"""
    if options.site and not parsed.site:
        parsed.site = options.site
        parsed.effective = f"{parsed.effective} site:{options.site}"
    if options.freshness != Freshness.ANY:
        pass     # freshness 由 options 直接作用于过滤阶段
    return parsed


def merge_time_filter(options: QueryOptions, parsed: ParsedQuery
                      ) -> Tuple[Optional[datetime], Optional[datetime]]:
    """汇总最终时间过滤区间: options 优先, 其次 query 推断。"""
    t_from, t_to = options.time_from, options.time_to
    if not t_from:
        _, f_from, f_to, _ = extract_time(parsed.raw)
        t_from, t_to = f_from, (f_to or t_to)
    if not t_from and options.freshness != Freshness.ANY:
        delta = freshness_to_delta(options.freshness)
        if delta:
            t_from = now() - delta
    return t_from, t_to
