"""Compare keyword, bm25, and hybrid retrieval without calling a model.

The 24-question development set keeps its schema labels. Knowledge precision
is scored only where a case names the relevant documents. Keyword output is
also checked against keyword_baseline.json, which freezes the current
algorithm. Changing that file requires a separate experiment record.
"""
import json
from pathlib import Path
import time

from sql_agent_lab.evals.grounding_ab import CASES, KNOWLEDGE
from sql_agent_lab.workspace.grounding_context import render_grounding
from sql_agent_lab.workspace.retrieval import ground, retrieval_scores


BASELINE_PATH = Path(__file__).with_name('keyword_baseline.json')
STRATEGIES = ('keyword', 'bm25', 'hybrid')


def _doc(document_id, title, body, datasource_id='shop', version=1):
    return {
        'id': document_id,
        'datasource_id': datasource_id,
        'title': title,
        'version': version,
        'content_hash': document_id + '-v' + str(version),
        'provenance': 'user',
        'body': body,
    }


# These questions exist to score knowledge retrieval. They are not the 24-case
# schema set and they are not used to edit role prompts.
KNOWLEDGE_PROBES = [
    {
        'id': 'schema-word-only',
        'question': 'How many orders?',
        'relevant_knowledge': ['order-def'],
        'forbidden_knowledge': ['weather'],
        'documents': [
            _doc('order-def', 'Order', 'These orders are customer purchases.'),
            _doc('weather', 'Weather', 'Tomorrow will be sunny at the warehouse.'),
        ],
    },
    {
        'id': 'synonym-gap',
        'question': 'How many settled invoices?',
        'relevant_knowledge': ['settled-def'],
        'forbidden_knowledge': ['weather'],
        'lexical_overlap': False,
        'documents': [
            _doc('settled-def', 'Completed payment', 'A completed payment is an order with status paid.'),
            _doc('weather', 'Weather', 'Tomorrow will be sunny at the warehouse.'),
        ],
    },
    {
        'id': 'cross-source',
        'question': 'What is net revenue?',
        'relevant_knowledge': ['shop-revenue'],
        'forbidden_knowledge': ['archive-revenue'],
        'documents': [
            _doc('shop-revenue', 'Net revenue', 'Net collected revenue is a user definition.'),
            _doc('archive-revenue', 'Net revenue', 'Archive net revenue.', datasource_id='archive'),
        ],
    },
    {
        'id': 'title-rank',
        'question': 'paid',
        'relevant_knowledge': ['title-paid'],
        'forbidden_knowledge': [],
        'documents': [
            _doc('body-paid', 'Warehouse note', 'paid paid paid in a stored note'),
            _doc('title-paid', 'Paid status', 'see the definition'),
        ],
    },
    {
        'id': 'chinese-unsegmented',
        'question': '有多少活跃订单',
        'relevant_knowledge': ['active-zh'],
        'forbidden_knowledge': [],
        'chinese_segmentation': 'unicode61_keeps_contiguous_runs',
        'documents': [
            _doc('active-zh', '业务定义', '活跃订单是指已支付的订单'),
        ],
    },
    {
        'id': 'chinese-exact',
        'question': '活跃订单',
        'relevant_knowledge': ['exact-zh'],
        'forbidden_knowledge': [],
        'chinese_segmentation': 'exact_contiguous_run',
        'documents': [
            _doc('exact-zh', '定义', '活跃订单'),
        ],
    },
]


def keyword_snapshot(profile, documents=None, cases=None):
    """Per-question keyword view. This is the frozen baseline shape."""
    documents = KNOWLEDGE if documents is None else documents
    cases = CASES if cases is None else cases
    rows = []
    for case in cases:
        bundle = ground('shop', profile, documents, case['question'], cache={}, strategy='keyword')
        rows.append(_view(case, bundle))
    return rows


def strategy_report(profile, documents=None, cases=None):
    """Schema metrics for every strategy, plus knowledge metrics on labeled cases."""
    documents = KNOWLEDGE if documents is None else documents
    cases = CASES if cases is None else cases
    reports = {}
    views = {}
    for strategy in STRATEGIES:
        rows = []
        schema_scores = []
        knowledge_scores = []
        extra_schema = 0
        extra_knowledge = 0
        labeled = 0
        latency = []
        context = []
        for case in cases:
            started = time.perf_counter()
            bundle = ground('shop', profile, documents, case['question'], cache={}, strategy=strategy)
            latency.append((time.perf_counter() - started) * 1000)
            text, _summary = render_grounding(bundle)
            context.append(len(text))
            view = _view(case, bundle)
            rows.append(view)
            schema_scores.append(retrieval_scores(view['tables'], case['answer_tables']))
            extra_schema += len(set(view['tables']) - set(case['answer_tables']))
            if 'required_knowledge' in case:
                labeled += 1
                found = view['knowledge_ids']
                wanted = case['required_knowledge']
                knowledge_scores.append(retrieval_scores(found, wanted))
                extra_knowledge += len(set(found) - set(wanted))
        count = len(schema_scores)
        reports[strategy] = {
            'schema_precision': _mean(schema_scores, 'precision'),
            'schema_recall': _mean(schema_scores, 'recall'),
            'schema_hit_rate': _mean(schema_scores, 'hit'),
            'knowledge_precision': _mean(knowledge_scores, 'precision'),
            'knowledge_recall': _mean(knowledge_scores, 'recall'),
            'knowledge_hit_rate': _mean(knowledge_scores, 'hit'),
            'knowledge_labeled_cases': labeled,
            'irrelevant_schema_hits': extra_schema,
            'irrelevant_knowledge_hits': extra_knowledge,
            'mean_latency_ms': sum(latency) / count,
            'mean_context_chars': sum(context) / count,
            'cases': count,
        }
        views[strategy] = rows
    schema_same = views['keyword']
    for strategy in ('bm25', 'hybrid'):
        for left, right in zip(schema_same, views[strategy]):
            if left['tables'] != right['tables'] or left['columns'] != right['columns']:
                raise AssertionError('schema channel changed between strategies')
    return {'strategies': reports, 'questions': {
        'keyword': views['keyword'], 'bm25': views['bm25'], 'hybrid': views['hybrid']}}


def probe_report(profile):
    """Knowledge probes, including a synonym that lexical search is not expected to solve."""
    rows = []
    for probe in KNOWLEDGE_PROBES:
        item = {'id': probe['id'], 'strategies': {}}
        if probe.get('lexical_overlap') is False:
            item['embedding_candidate'] = True
            item['note'] = 'No shared content token. BM25 and keyword are both lexical.'
        if probe.get('chinese_segmentation'):
            item['chinese_segmentation'] = probe['chinese_segmentation']
            item['chinese_semantic_search'] = False
        for strategy in STRATEGIES:
            bundle = ground('shop', profile, probe['documents'], probe['question'], cache={}, strategy=strategy)
            found = [hit['id'] for hit in bundle['knowledge']]
            item['strategies'][strategy] = {
                'knowledge_ids': found,
                'precision': retrieval_scores(found, probe['relevant_knowledge'])['precision'],
                'recall': retrieval_scores(found, probe['relevant_knowledge'])['recall'],
                'hit': retrieval_scores(found, probe['relevant_knowledge'])['hit'],
                'forbidden_returned': sorted(set(found) & set(probe['forbidden_knowledge'])),
                'bm25_scores': [hit.get('bm25_score') for hit in bundle['knowledge']],
                'context_chars': len(render_grounding(bundle)[0]),
                'one_sided_relationships': sum(
                    1 for link in bundle['relationships'] if link.get('attachment') == 'one_matched_table'),
            }
        rows.append(item)
    return rows


def _view(case, bundle):
    return {
        'id': case['id'],
        'tables': [table['name'] for table in bundle['tables']],
        'columns': [table['name'] + '.' + column['name']
                    for table in bundle['tables'] for column in table['columns']],
        'knowledge_ids': [item['id'] for item in bundle['knowledge']],
        'knowledge_versions': [item['version'] for item in bundle['knowledge']],
        'knowledge_scores': [item.get('score') for item in bundle['knowledge']],
        'bm25_scores': [item.get('bm25_score') for item in bundle['knowledge']],
        'reasons': [item.get('reason') for item in bundle['knowledge']],
    }


def _mean(scores, key):
    if not scores:
        return None
    return sum(item[key] for item in scores) / len(scores)


def load_keyword_baseline():
    return json.loads(BASELINE_PATH.read_text(encoding='utf-8'))
