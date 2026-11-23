"""Narrow, deterministic metadata intents; query requests retain SQL routing."""
import re


def direct_answer(question, profile, knowledge=None):
    text = re.sub(r'[\s，。！？?！]', '', question)
    if re.search(r'怎么定义|如何定义|是什么意思|业务说明|统计口径', text) and not re.search(r'统计.*多少|计算|列出', text):
        documents = knowledge() if knowledge else []
        if not documents:
            return '当前数据库还没有业务说明，无法确定这个定义。请在数据库介绍中补充口径后再问。'
        return '以下是当前数据库已保存的业务说明，供核对；没有写明的定义不能据此推断：\n' + '\n'.join('【' + d['title'] + '】\n' + d.get('body', '')[:3000] for d in documents[:3])
    if not re.fullmatch(r'(?:请)?(?:描述|介绍)(?:一下)?(?:这个|当前)?数据库|(?:这个|当前)?数据库(?:里)?有哪些表', text):
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
