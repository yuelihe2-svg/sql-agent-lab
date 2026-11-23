"""Narrow, deterministic metadata intents; query requests retain SQL routing."""
import re


_GENERAL = re.compile(r'(?:请)?(?:介绍|查看|显示|看看|解释)?(?:一下)?(?:这个|当前)?(?:数据库的?)?'
                      r'(?:业务说明|统计口径)(?:是什么|有哪些)?')
_DEFINITION = re.compile(r'(?:请问|请解释一下)?(?:什么是(.+)|(.+?)(?:怎么定义的?|如何定义的?|'
                         r'是什么意思|的定义是什么|的(?:业务说明|统计口径)(?:是什么)?))')


def _definition_answer(topic, documents):
    if not documents:
        return '当前数据库还没有业务说明，无法确定这个定义。请在数据库介绍中补充口径后再问。'
    if topic:
        needle = topic.casefold()
        # Short contiguous Chinese terms also match inside unsegmented definitions.
        terms = set(re.findall(r'[a-z0-9_]+', needle))
        for run in re.findall(r'[\u4e00-\u9fff]+', needle):
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
        def relevance(doc):
            title = doc['title'].casefold()
            body = doc.get('body', '').casefold()
            return (needle in title, needle in body, sum(t in title or t in body for t in terms))
        documents = sorted(documents, key=relevance, reverse=True)
        documents = [d for d in documents if any(relevance(d))]
        if not documents:
            return f'当前数据库的业务说明没有找到与「{topic}」相关的定义。可以在数据库介绍中补充口径。'
    from .retrieval import matching_excerpt
    shown = []
    for doc in documents[:3]:
        excerpt, clipped = matching_excerpt(doc.get('body', ''), [topic] if topic else [], limit=3000)
        shown.append('【' + doc['title'] + '】\n' + excerpt + ('（节选）' if clipped else ''))
    return '以下是当前数据库已保存的业务说明，供核对：\n' + '\n'.join(shown)


def direct_answer(question, profile, knowledge=None):
    text = re.sub(r'[\s，。！？?！]', '', question)
    if _GENERAL.fullmatch(text):
        return _definition_answer(None, knowledge() if knowledge else [])
    definition = _DEFINITION.fullmatch(text)
    if definition and not re.match(r'^(?:请|帮我)?(?:按|根据|计算|列出|查询|统计)', text):
        return _definition_answer(definition[1] or definition[2], knowledge() if knowledge else [])
    if not re.fullmatch(r'(?:请)?(?:(?:描述|介绍)(?:一下)?(?:这个|当前)?数据库|(?:这个|当前)?数据库(?:里)?有哪些表)', text):
        return None
    data = profile()
    tables = data.get('tables', [])
    lines = [f"数据库 {data.get('name', '')} 包含 {len(tables)} 张表。"]
    for table in tables[:30]:
        fields = '、'.join(c['name'] for c in table.get('columns', [])[:20])
        lines.append(f"{table['name']}：{fields}")
    if len(tables) > 30:
        lines.append('这里只展示前30张表，完整结构可在数据库介绍中查看。')
    lines.append('以上来自数据库结构；字段的业务口径需要参考业务说明，不能仅凭名称确定。')
    return '\n'.join(lines)
