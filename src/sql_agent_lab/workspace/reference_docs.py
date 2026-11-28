"""Local extractive answers with version-bound citations into UTF-8 reference files."""
import json
import re
from pathlib import PurePath

from .retrieval import _tokens

FORMATS = {'.md': 'markdown', '.txt': 'text', '.json': 'json'}
_DOC = re.compile(r'资料|文档|政策|条款|手册|字典|规定|口径|怎么定义|是什么意思|如何定义|什么是|由谁承担|谁承担|能否退|能退吗|可以退吗')
_FORMULA = re.compile(r'怎么算|如何计算|怎么计算|计算公式')
_DATA = re.compile(r'(?:统计|算出|计算|汇总|列出订单|排名|总共多少|查一下订单)')
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


def reference_answer(question, load_documents):
    if not (_DOC.search(question) or _FORMULA.search(question)) or _DATA.search(_FORMULA.sub('', question)):
        return None
    documents = load_documents()
    files = [d for d in documents if PurePath(d['title']).suffix.lower() in FORMATS]
    if not files and not re.search(r'资料|文档|政策|手册|条款|字典', question):
        return None
    terms = {t for t in _tokens(_FILLER.sub(' ', question)) if len(t) >= 2}
    labels = {code.lower(): code for code in re.findall(r"\b[a-z]+[0-9]+\b", question, re.ASCII | re.I)}
    codes = set(labels)
    hits = []
    code_hits = {}
    for doc in files:
        for index, (location, text) in enumerate(passages(doc['format'], doc['body'])):
            content = (location + ' ' + text).lower()
            matched_codes = codes.intersection(re.findall(r"[a-z0-9_]+", content))
            if codes and not matched_codes:
                continue
            score = sum(len(t) for t in terms if t in content)
            if score:
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
        if len(files) < len(documents) and not re.search(r'文档|资料|条款|政策|手册|字典', question):
            return None
        return {'answer': '当前数据源的参考资料没有找到可引用的条款。请补充资料或说明要查的术语。'}
    citations, lines = [], []
    for score, doc, index, location, text in hits[:3]:
        if not codes and score < hits[0][0] / 2:
            break
        number = len(lines) + 1
        lines.append(f'[{number}] {location}：{text[:800]}' + ('…' if len(text) > 800 else ''))
        citations.append({k: doc[k] for k in ('datasource_id', 'id', 'title', 'version')} |
                         {'passage': index, 'location': location})
    intro = '检索到以下相关原文。'
    if len({c['id'] for c in citations}) > 1:
        intro = '多份资料有相关条款，请核对适用版本和范围。'
    return {'answer': intro + '\n' + '\n'.join(lines), 'citations': citations}
