"""BIRD tables.json -> original MAC-SQL schema representation.

Reimplemented from the pinned behavior, with read-only and resource boundaries.
No questions, reference SQL, gold schema, retrieval, or local enum enrichment.
"""
from dataclasses import dataclass
import re

from sql_agent_lab.macsql.tools import EvaluationLimitExceeded, quote_identifier


def bounded_rows(cursor, limits):
    rows, size = [], 0
    for row in cursor:
        size += sum(len(v) if isinstance(v, bytes) else len(str(v).encode('utf8')) for v in row)
        if len(rows) >= limits.max_result_rows or size > limits.max_result_bytes:
            raise EvaluationLimitExceeded('Complete result exceeds reproduction resource limit')
        rows.append(row)
    return rows


def value_examples(values, column_type):
    if not values or (len(values) > 10 and column_type in {'INTEGER', 'REAL', 'NUMERIC', 'FLOAT', 'INT'}):
        return ''
    nonempty = [v for v in values if v is not None and str(v).strip()]
    if not nonempty:
        return ''
    if column_type in {'TEXT', 'VARCHAR'}:
        if any(isinstance(v, str) and (
            'http://' in v or 'https://' in v or re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', v)
        ) for v in nonempty) or max(len(str(v)) for v in nonempty) > 50:
            return ''
    chosen = nonempty[:6]

    def date_like(value):
        if not isinstance(value, str):
            return False
        first = value.split()[0]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', first):
            return False
        year, month, day = map(int, first.split('-'))
        return year >= 1 and 1 <= month <= 12 and 1 <= day <= 31

    if all(date_like(v) for v in chosen):
        chosen = chosen[:1]
    if None in values:
        chosen.insert(0, None)
    return str(chosen)


@dataclass(frozen=True)
class BirdSchema:
    db_id: str
    # Insertion order is the upstream tables.json order, not SQLite alphabetical order.
    tables: dict

    @property
    def needs_selector(self):
        total = sum(len(t['columns']) for t in self.tables.values())
        return total > 30 or total // len(self.tables) > 6

    def render(self, decisions=None):
        decisions = decisions or {}
        blocks, links, selected = [], [], {}
        for name, table in self.tables.items():
            columns = table['columns']
            decision = decisions.get(name, '')
            if decision == 'drop_all':
                chosen = columns[:6]
            elif decision in ('keep_all', ''):
                chosen = columns[:]
            else:
                keys = set(table['primary_keys']) | {f[0] for f in table['foreign_keys']}
                chosen = [c for c in columns if c['name'] in keys or c['name'] in decision]
                if len(columns) > 6:
                    for column in columns:
                        if len(chosen) >= 6:
                            break
                        if column not in chosen:
                            chosen.append(column)
            selected[name] = [c['name'] for c in chosen]
            lines = []
            for column in chosen:
                line = f"  ({column['name']},"
                if column['full_name']:
                    line += ' ' + column['full_name'].strip() + '.'
                if column['examples']:
                    line += ' Value examples: ' + column['examples'] + '.'
                lines.append(line + '),')
            blocks.append('# Table: ' + name + '\n[\n' + '\n'.join(lines).strip(',') + '\n]\n')
            for source, target_table, target_column in table['foreign_keys']:
                source = source if '`' in source else f'`{source}`'
                target_column = target_column if '`' in target_column else f'`{target_column}`'
                link = f'{name}.{source} = {target_table}.{target_column}'
                if link not in links:
                    links.append(link)
        return ''.join(blocks).strip(), '\n'.join(links).strip(), selected


def prepare_schema(tools, entry):
    """Read metadata and frequency-ranked values; refuse incomplete sampling.

    entry is one BIRD tables.json record. Do not synthesize it from a question
    or from reference SQL. Missing metadata is a preparation failure.
    """
    db_id = entry['db_id']
    names = entry['table_names_original']
    originals = entry['column_names_original']
    descriptions = entry['column_names']
    if not names or len(originals) != len(descriptions):
        raise ValueError('Invalid BIRD tables.json record')
    primary = []
    for key in entry['primary_keys']:
        primary.extend(key if isinstance(key, list) else [key])
    foreign = entry['foreign_keys']
    keys = set(primary) | {i for pair in foreign for i in pair}
    actual = tools.load_schema(db_id)
    if set(names) != set(actual):
        raise ValueError('BIRD metadata tables differ from database')
    tables = {}
    for table_index, name in enumerate(names):
        indexed = [(i, c) for i, (t, c) in enumerate(originals) if t == table_index]
        if {c for _, c in indexed} != set(actual[name]['columns']):
            raise ValueError('BIRD metadata columns differ from database')
        columns = []
        for index, column in indexed:
            examples = ''
            if index not in keys and not column.lower().endswith(('id', 'email', 'url')):
                identifier = quote_identifier(column)
                sql = (f'SELECT {identifier} FROM {quote_identifier(name)} '
                       f'GROUP BY {identifier} ORDER BY COUNT(*) DESC')
                with tools.connection(db_id, query=True) as conn:
                    conn.text_factory = lambda b: b.decode(errors='ignore')
                    values = [r[0] for r in bounded_rows(conn.execute(sql), tools.limits)]
                examples = value_examples(values, actual[name]['columns'][column]['type'])
            columns.append(dict(name=column,
                                full_name=descriptions[index][1].replace('_', ' '), examples=examples))
        tables[name] = dict(columns=columns,
                           primary_keys=[originals[i][1] for i in primary if originals[i][0] == table_index],
                           foreign_keys=[(originals[a][1], names[originals[b][0]], originals[b][1])
                                         for a, b in foreign if originals[a][0] == table_index])
    return BirdSchema(db_id, tables)
