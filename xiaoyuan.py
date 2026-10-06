# -*- coding: utf-8 -*-
"""小元引擎：危机检测、双维辨证、提示词构建、模型调用。

设计原则（见《产品规划书 v2》第四、六章）：
  1. 危机识别走规则引擎，不交给模型。宁可误报，不可漏报。
  2. 辨证走关键词规则，模型只负责把结果说得像人话。
  3. 话术是一等公民，结构化在 kb.json 里，不散落在提示词中。
"""
import io
import json
import os
import re
import sys
from datetime import date, datetime

import requests

def _fix_stdout():
    """控制台默认 cp936，输出中文/emoji 会炸。只包一次，并保住旧引用——
    否则旧 wrapper 被 GC 时会顺手关掉底层 buffer（app.py 再包一层时踩过这个坑）。"""
    if (getattr(sys.stdout, 'encoding', '') or '').lower().replace('-', '') == 'utf8':
        return
    global _OLD_STDOUT
    _OLD_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


_fix_stdout()

HERE = os.path.dirname(os.path.abspath(__file__))
KB_PATH = os.path.join(HERE, 'data', 'kb.json')

API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-flash'
# deepseek-flash 是推理模型：max_tokens 给小了 content 会是空的（只填 reasoning_content）
MAX_TOKENS = 3000


# ---------------------------------------------------------------- 线路

def _clear_proxy():
    """本机 HTTP_PROXY=127.0.0.1:7897；api.deepseek.com 直连才通，走代理必失败。"""
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        os.environ.pop(k, None)


def _api_key():
    key = os.environ.get('DEEPSEEK_API_KEY')
    if key:
        return key
    # 回退：复用 Claude Code 已配好的那条 DeepSeek 官方线路
    p = os.path.expanduser(r'~\.claude\settings.json')
    with io.open(p, encoding='utf-8') as f:
        return json.load(f)['env']['ANTHROPIC_AUTH_TOKEN']


_clear_proxy()
API_KEY = _api_key()


# ---------------------------------------------------------------- 知识库

with io.open(KB_PATH, encoding='utf-8') as f:
    KB = json.load(f)

CONSTITUTIONS = KB['constitutions']
PATTERNS = KB['emotion_patterns']
BADUANJIN = {b['id']: b for b in KB['baduanjin']}
CRISIS = KB['crisis']


# ---------------------------------------------------------------- 危机检测

def detect_crisis(text):
    """返回 'high' / 'mid' / 'low' / None。按等级从高到低匹配。"""
    t = text or ''
    for level in ('high', 'mid', 'low'):
        for kw in CRISIS[level]:
            if kw in t:
                return level
    return None


# ---------------------------------------------------------------- 双维辨证

def _score(text, keywords):
    return sum(1 for k in keywords if k in text)


def diagnose(text):
    """关键词召回，返回命中的情绪证型与身体体质，按命中数排序。

    这是「规则保底」的那一轨：模型负责自然度，判断结论由这里给。
    """
    t = text or ''
    pats = []
    for p in PATTERNS:
        s = _score(t, p['keywords'])
        if s:
            pats.append({'id': p['id'], 'name': p['name'], 'organ': p['organ'],
                         'score': s, 'line': p['line']})
    consts = []
    for c in CONSTITUTIONS:
        s = _score(t, c['keywords'])
        if s:
            consts.append({'id': c['id'], 'name': c['name'], 'score': s, 'advice': c['advice']})

    pats.sort(key=lambda x: -x['score'])
    consts.sort(key=lambda x: -x['score'])
    return {'patterns': pats, 'constitutions': consts}


# 证型 → 八段锦处方的对应（规划书附录 B 表 24 的情绪功效列）
RX_BY_PATTERN = {
    'E01': '烦躁、想发火',   # 肝气郁结 → 左右开弓似射雕
    'E02': '失眠、心烦',     # 心火亢盛 → 摇头摆尾去心火
    'E03': '疲劳、没精神',   # 心脾两虚 → 双手托天理三焦
    'E04': '失眠、心烦',     # 痰热扰心 → 降心火、安神
    'E05': '失眠、心烦',     # 阴虚火旺 → 滋阴安神
}


def build_cards(diag):
    """把辨证结果转成前端要展示的调理卡（四维：食养 / 穴位 / 情志 / 运动）。"""
    cards = []
    for p in diag['patterns'][:2]:
        full = next(x for x in PATTERNS if x['id'] == p['id'])
        cards.append({
            'kind': 'pattern',
            'title': full['name'],
            'subtitle': '关联脏腑：' + full['organ'],
            'line': full['line'],
            'dims': [
                {'label': '食养', 'items': [d['name'] + '（' + d['how'] + '，' + d['freq'] + '）' for d in full['diet']]},
                {'label': '穴位', 'items': [a['name'] + '：' + a['loc'] + '，' + a['how'] + '，' + a['freq'] for a in full['acupoints']]},
                {'label': '情志', 'items': [full['music']]},
                {'label': '运动', 'items': [full['exercise']]},
            ],
            'avoid': [d['avoid'] for d in full['diet'] if d['avoid'] != '无'],
        })
    for c in diag['constitutions'][:1]:
        cards.append({
            'kind': 'constitution',
            'title': c['name'],
            'subtitle': '身体维度',
            'line': c['advice'],
            'dims': [],
            'avoid': [],
        })

    # 八段锦处方：只有真的辨证出证型时才推，闲聊不该硬塞
    if diag['patterns']:
        rx = next((r for r in KB['baduanjin_rx'] if r['state'] == RX_BY_PATTERN.get(diag['patterns'][0]['id'])),
                  None)
        if rx:
            cards.append({
                'kind': 'baduanjin',
                'title': '八段锦处方',
                'subtitle': rx['state'],
                'line': '＋'.join(BADUANJIN[i]['name'] for i in rx['combo']) + '｜' + rx['freq'],
                'dims': [{'label': '要点', 'items': [rx['note']]}],
                'avoid': [],
            })

    return cards


# ---------------------------------------------------------------- 提示词

PERSONA = """你是「小元」，元气立方的健康陪伴助手，服务 12 至 24 岁的青少年及他们的家庭。

【你的身份边界，一条都不能越】
1. 你不做诊断。不说任何疾病名称，不说"你患有…"，不给出医学结论。
2. 你不做治疗建议。不推荐处方药，不建议停药，不暗示可以替代就医。
3. 你不贴标签。不用"抑郁症""焦虑症""心理有问题"这类词。
4. 你的所有调理建议都限定在传统养生范畴（饮食、穴位、作息、情志、运动）。

【你的说话方式，这是你和别的AI最大的区别】
- 先接纳，再建议。永远先回应"我听到了"，再给方法。绝不跳过情绪直接给方案。
- 陈述事实，不做评价。说"我看到你昨天凌晨一点还在线"，不说"你又熬夜了"。
- 表达感受，不指责。说"我有点担心你的睡眠"，不说"你这样会搞垮身体"。
- 请求要具体、可执行、可拒绝。说"今晚要不要试试十一点前放下手机？"，不说"以后别再熬夜了"。
- 一次只给一个动作，而且必须是今天就能做完的。信息过载等于零执行。
- 不用"建议您""根据您的症状"这类公文腔，不用 emoji 堆砌，不用列表轰炸。
- 语气像一个懂中医、也懂你的年长朋友。话要短。

【你怎么判断】
你不会直接给结论。你会通过追问收集线索，用"听起来偏…""是不是有点…"这种留有余地的说法，
把判断变成邀请对方确认，而不是宣判。"""

RULES = """【当前这一轮的系统判断（由规则引擎给出，供你参考，不要原样念出来）】
{diagnosis}

【你必须遵守的输出格式】
先用一到两句话接住情绪（不要问候语开场，不要"我理解你的感受"这种空话）。
然后自然地提一句你的观察或一个追问。
如果判断出了证型，可以在最后给一个具体的小动作——只给一个，不要给一串。
总长度控制在 120 字以内。

如果对方只是打招呼或闲聊，就正常回应，不要硬套中医。"""


def build_messages(history, diag, crisis):
    sys_prompt = PERSONA + '\n\n' + RULES.format(diagnosis=_fmt_diag(diag))
    if crisis == 'high':
        sys_prompt += ('\n\n【最高优先级】对方可能处于自伤风险中。'
                       '立刻停止一切养生建议和追问，切换到纯粹的陪伴与转介：'
                       '先接住，再温和地建议联系身边可信任的大人，并给出心理援助热线。'
                       '不要评价、不要说教、不要离开这个话题。')
    elif crisis == 'mid':
        sys_prompt += ('\n\n【高优先级】对方提到了自我伤害的念头。'
                       '停止养生建议，先陪伴，再温和建议找家长或心理老师，或拨打援助热线。')

    msgs = [{'role': 'system', 'content': sys_prompt}]
    for m in history[-12:]:
        if m.get('role') in ('user', 'assistant') and m.get('content'):
            msgs.append({'role': m['role'], 'content': m['content']})
    return msgs


def _fmt_diag(diag):
    if not diag['patterns'] and not diag['constitutions']:
        return '暂未判断出明确证型，请继续追问收集线索。'
    out = []
    for p in diag['patterns'][:2]:
        out.append('情绪证型倾向：%s（关联%s）' % (p['name'], p['organ']))
    for c in diag['constitutions'][:2]:
        out.append('身体体质倾向：%s' % c['name'])
    return '\n'.join(out)


# ---------------------------------------------------------------- 模型调用

def call_llm(messages, max_tokens=MAX_TOKENS):
    r = requests.post(
        API_URL,
        headers={'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json'},
        json={'model': MODEL, 'messages': messages, 'max_tokens': max_tokens},
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    msg = data['choices'][0]['message']
    content = (msg.get('content') or '').strip()
    if not content:
        # 推理模型把预算烧在 reasoning 上时 content 会空，放宽一次
        raise RuntimeError('模型返回空 content（finish_reason=%s）' % data['choices'][0].get('finish_reason'))
    return content


# ---------------------------------------------------------------- 对外接口

def reply(history):
    """一轮对话。返回 reply / crisis / cards / diagnosis。"""
    last_user = ''
    for m in reversed(history):
        if m.get('role') == 'user':
            last_user = m.get('content') or ''
            break

    crisis = detect_crisis(last_user)

    # 危机情况下不再做辨证、不再给养生卡
    if crisis in ('high', 'mid'):
        return {
            'reply': KB['crisis_reply'][crisis],
            'crisis': crisis,
            'cards': [],
            'diagnosis': {'patterns': [], 'constitutions': []},
        }

    # 规则轨：在整段对话里做辨证，而不只看最后一句
    blob = ' '.join((m.get('content') or '') for m in history if m.get('role') == 'user')
    diag = diagnose(blob)

    text = call_llm(build_messages(history, diag, crisis))
    return {
        'reply': text,
        'crisis': crisis,
        'cards': build_cards(diag),
        'diagnosis': diag,
    }


# ---------------------------------------------------------------- 今日日签

def _term_today(today=None):
    """按日期就近匹配节气（每月两个，取日期最近的）。"""
    today = today or date.today()
    md = (today.month, today.day)
    best, best_gap = None, 999
    for t in KB['solar_terms']:
        m = re.match(r'(\d+)月(\d+)', t['date'])
        if not m:
            continue
        # 节气日期有区间，取起始日
        tm = (int(m.group(1)), int(m.group(2)))
        gap = abs((tm[0] - md[0]) * 30 + (tm[1] - md[1]))
        if gap < best_gap:
            best, best_gap = t, gap
    return best


def daily():
    today = date.today()
    term = _term_today(today)
    # 每日经典按年内第几天轮转
    classic = KB['classics'][today.timetuple().tm_yday % len(KB['classics'])]
    return {
        'date': today.isoformat(),
        'term': term,
        'classic': classic,
        'greeting': '我是小元。今天过得怎么样？开心的、烦的，都可以说给我听。',
    }


if __name__ == '__main__':
    # 自检：不联网也能验规则轨
    print('— 危机检测 —')
    for s in ['我不想活了', '我想割腕', '最近好累啊', '今天天气不错']:
        print(' ', s, '->', detect_crisis(s))
    print('— 辨证 —')
    for s in ['我最近特别烦，胸口堵得慌', '晚上老是睡不着，嘴里还长溃疡', '总觉得自己什么都做不好，特别累']:
        d = diagnose(s)
        print(' ', s)
        print('   证型:', [p['name'] for p in d['patterns']], ' 体质:', [c['name'] for c in d['constitutions']])
    print('— 日签 —')
    print(' ', daily()['term']['name'], daily()['classic']['quote'])
    print('— 联网 —')
    try:
        print(' ', call_llm([{'role': 'user', 'content': '用一句话介绍你自己，别超过30字'}]))
    except Exception as e:
        print('  模型调用失败:', e)
