"""Local extractive answers with version-bound citations into UTF-8 reference files."""
from collections import Counter
import json
import math
import re
from pathlib import PurePath

from .retrieval import _tokens

FORMATS = {'.md': 'markdown', '.txt': 'text', '.json': 'json'}
_DOC = re.compile(r'资料|文档|政策|条款|手册|字典|规定|规则|口径|报销.{0,12}(?:标准|上限|每天)|(?:能否|能不能|能|可以|允许).{0,12}退货(?:吗|[？?])|怎么定义|是什么意思|如何定义|什么是|由谁承担|谁承担|能否退|能退吗|可以退吗')
_FORMULA = re.compile(r'怎么算|如何计算|怎么计算|计算公式')
_DATA = re.compile(r'(?:统计|算出|计算|汇总|列出订单|排名|总共多少|(?:多少|几)(?:笔|单|条订单)|(?:查询|列出|[12][0-9]{3}年|上月|本月).{0,24}(?:总金额|订单数|实收额)|查一下订单)')
_FILLER = re.compile(r'请问|根据|参考|资料|文档|政策|手册|条款|规定|字典|是否|可以|怎么|如何|定义|什么|意思|请查|里面|有没有|目前|哪些|关于|说明|提到|按照|一下|告诉我|口径')


def validate_reference(title, fmt, body):
    extension = PurePath(title).suffix.lower()
    if extension not in FORMATS:
        return
    if FORMATS[extension] != fmt or '\x00' in body:
        raise ValueError('文件扩展名、格式或文本编码不正确。')
    if fmt == 'json':
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not value:
                raise ValueError('知识 JSON 需要非空的键值对象。')
            list(passages(fmt, body))
        except RecursionError:
            raise ValueError('JSON 嵌套过深。') from None


def passages(fmt, body):
    if fmt == 'json':
        def walk(value, path, depth):
            if depth > 16:
                raise ValueError('JSON 嵌套过深。')
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from walk(child, path + '/' + key.replace('~', '~0').replace('/', '~1'), depth + 1)
            elif isinstance(value, list):
                if any(isinstance(child, (list, dict)) for child in value):
                    raise ValueError('记录数组请先导入数据表。')
                yield path, json.dumps(value, ensure_ascii=False)
            else:
                yield path, json.dumps(value, ensure_ascii=False)
        yield from walk(json.loads(body), '', 0)
        return
    heading = ''
    for line, text in enumerate(body.splitlines(), 1):
        if fmt == 'markdown' and re.match(r'^#{1,6}\s', text):
            heading = text.lstrip('# ').strip()
        elif text.strip():
            yield f'{heading} · 第 {line} 行' if heading else f'第 {line} 行', text


# Restrict implicit routing to permission questions about a business action.
# A polite request to fetch data ("能帮我查…") must continue to the SQL flow.
_PERMISSION = re.compile(
    r'^(?:请问[，, ]*)?(?P<condition>.*?)(?:还能不能|能不能|可不可以|是否可以|能否|还能|还可以|可以|允许|能)'
    r'(?P<action>拿到|获得|领取|申领|申请|办理|借用|使用|报销|改期|换证|补办|退货|入场|参加)'
    r'(?P<object>[^，,。？?]{0,40}?)(?:吗|么|呢)?[？?。]*$')
_RULE_LANGUAGE = re.compile(r'可以|允许|应当|必须|须|需|不得|不能|不可|不予|不受理|不发|禁止|仅限|凭|方可')


_ACTION_TERMS = {
    '拿到': ('拿到', '领', '发', '获得'), '获得': ('获得', '领', '发'),
    '领取': ('领', '发'), '申领': ('领', '发', '申请'),
}


def _permission_scope(question):
    match = _PERMISSION.fullmatch(question.strip())
    if not match:
        return None
    condition = match['condition'].strip(' ，,')
    # Completed actions can be written with or without 后. Preserve other
    # time qualifiers: 入场后 and 入场前 refer to different conditions.
    if condition.startswith(('完成', '已完成')):
        condition = re.sub(r'(?:之后|以后|后)$', '', condition)
    subject = match['object'] or match['action']
    return condition, subject, _ACTION_TERMS.get(match['action'], (match['action'],))


def reference_answer(question, load_documents):
    if _DATA.search(_FORMULA.sub('', question)):
        return None
    explicit = _DOC.search(question) or _FORMULA.search(question)
    scope = None if explicit else _permission_scope(question)
    if not explicit and scope is None:
        return None
    documents = load_documents()
    files = [d for d in documents if PurePath(d['title']).suffix.lower() in FORMATS]
    if not files and scope is None and not re.search(r'资料|文档|政策|手册|条款|字典', question):
        return None
    search_text = re.sub(r'(?:从)?(?:哪天|何时|什么时候)开始(?:执行|实施)', '生效日期', question)
    terms = {t for t in _tokens(_FILLER.sub(' ', search_text)) if len(t) >= 2}
    labels = {code.lower(): code for code in re.findall(r"\b[a-z]+[0-9]+\b", question, re.ASCII | re.I)}
    codes = set(labels)
    candidates = []
    frequencies = Counter()
    passage_count = 0
    for doc in files:
        for index, (location, text) in enumerate(passages(doc['format'], doc['body'])):
            content = (location + ' ' + text).lower()
            if scope is not None:
                condition, subject, actions = scope
                if (subject.lower() not in content
                        or (condition and not re.search(r'(?<![未非不])' + re.escape(condition.lower()), content))
                        or not any(action in content for action in actions) or not _RULE_LANGUAGE.search(text)):
                    continue
            matched_codes = codes.intersection(re.findall(r"[a-z0-9_]+", content))
            if codes and not matched_codes:
                continue
            passage_count += 1
            matched = {term for term in terms if term in content}
            if matched:
                frequencies.update(matched)
                candidates.append((doc, index, location, text, matched, matched_codes))
    hits = []
    code_hits = {}
    for doc, index, location, text, matched, matched_codes in candidates:
        # Weight distinctive substrings above words shared by several clauses.
        score = sum(len(t) * math.log1p((passage_count + .5) / (frequencies[t] + .5))
                    for t in sorted(matched))
        hit = (score, doc, index, location, text)
        hits.append(hit)
        for code in matched_codes:
            if code not in code_hits or score > code_hits[code][0]:
                code_hits[code] = hit
    hits.sort(key=lambda hit: -hit[0])
    if codes and hits:
        if codes != code_hits.keys():
            return {'answer': '当前参考资料缺少这些代码的依据：' + '、'.join(labels[code] for code in sorted(codes - code_hits.keys()))}
        # Reserve a passage for each requested code before filling remaining slots.
        required = []
        for code in sorted(codes):
            if code_hits[code] not in required:
                required.append(code_hits[code])
        if len(required) > 3:
            return {'answer': '要核对的代码涉及三处以上不同原文，请将问题拆开。'}
        hits = required + [hit for hit in hits if hit not in required]
    if not hits:
        if scope is not None:
            return {'clarification': '当前资料不足以确认“' + question.strip()[:120]
                    + '”。请说明适用对象、办理条件，或补充对应条款。'}
        if len(files) < len(documents) and not re.search(r'文档|资料|条款|政策|手册|字典', question):
            return None
        return {'answer': '当前数据源的参考资料没有找到可引用的条款。请补充资料或说明要查的术语。'}
    citations, lines = [], []
    cutoff = 0.5 if re.search(r'与|以及|分别|区别|比较', question) else 0.75
    for score, doc, index, location, text in hits[:3]:
        if not codes and score < hits[0][0] * cutoff:
            break
        number = len(lines) + 1
        lines.append(f'[{number}] {location}：{text[:800]}' + ('…' if len(text) > 800 else ''))
        citations.append({k: doc[k] for k in ('datasource_id', 'id', 'title', 'version')} |
                         {'passage': index, 'location': location})
    intro = '检索到以下相关原文。'
    if len({c['id'] for c in citations}) > 1:
        intro = '多份资料有相关条款，请核对适用版本和范围。'
    return {'answer': intro + '\n' + '\n'.join(lines), 'citations': citations}
