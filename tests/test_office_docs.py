"""Offline Office import round trips including authorization and versioned originals."""
import base64
import hashlib
import io
import json
import shutil
from uuid import uuid4
from zipfile import ZipFile

import pytest

from sql_agent_lab.macsql.live_ui import Observatory
from sql_agent_lab.workspace.office_docs import extract
from sql_agent_lab.workspace.reference_docs import document_passages
from test_conversations_api import build, client_for


def encoded(data):
    return base64.b64encode(data).decode('ascii')


def word_file():
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    xml = f"""<w:document xmlns:w="{ns}"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>领取条件</w:t></w:r></w:p>
    <w:p><w:r><w:t>外部讲师凭邀请函领用。</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>器材</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>同传耳机</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>"""
    data = io.BytesIO()
    with ZipFile(data, 'w') as package:
        package.writestr('word/document.xml', xml)
    return data.getvalue()


def pdf_file(last_text):
    """Build two physical PDF pages using a standard Type 1 font."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>']
    for index, text in enumerate((b'First page', last_text)):
        objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] '
                       b'/Resources << /Font << /F1 7 0 R >> >> /Contents %d 0 R >>' % (4 + index * 2))
        stream = b'BT /F1 12 Tf 30 200 Td (' + text + b') Tj ET'
        objects.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    data = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(b'%d 0 obj\n' % i + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(b'xref\n0 8\n0000000000 65535 f \n')
    for offset in offsets:
        data.extend(b'%010d 00000 n \n' % offset)
    data.extend(b'trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF' % xref)
    return bytes(data)


def test_word_import_and_invalid_replacement(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    with client_for(app) as c:
        route = '/api/sources/shop/knowledge'
        payload = dict(title='手册.docx', file_data=encoded(word_file()))
        doc = c.post(route, json=payload).json()
        assert list(document_passages(doc)) == [
            ('领取条件 · 段落 2', '外部讲师凭邀请函领用。'),
            ('领取条件 · 表 1 第 1 行', '器材'), ('领取条件 · 表 1 第 2 行', '器材：同传耳机')]
        item = route + '/' + doc['id']
        bad = c.put(item, json={**payload, 'file_data': encoded(b'invalid')})
        assert bad.status_code == 400
        preview = c.get(item+'?version=1&passage=2')
        assert '领取条件 · 表 1 第 2 行' in preview.text and '器材：同传耳机' in preview.text
        assert c.get(item).json()['version'] == 1


@pytest.mark.skipif(not shutil.which('pdftotext'), reason='PDF import requires local Poppler')
def test_pdf_page_original_authorization_and_version(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    data = pdf_file(b'Final settlement')
    payload = dict(title='finance.pdf', file_data=encoded(data))
    doc = app.add_source_knowledge('shop', payload)
    assert list(document_passages(doc))[-1] == ('第 2 页 · 行 1', 'Final settlement')
    config = tmp_path/'accounts.json'
    accounts = [dict(id=str(uuid4()), name=s, sources=[s],
                     token_sha256=hashlib.sha256((s*20).encode()).hexdigest()) for s in ('shop','archive')]
    config.write_text(json.dumps(accounts))
    team = Observatory(None, app.output, sources=tmp_path/'sources.json', workspace_db=app.workspace_db,
                       accounts_config=config)
    link = '/api/sources/shop/knowledge/'+doc['id']+'?version=1&passage=1&original=pdf'
    with client_for(team) as c:
        assert c.get(link).status_code == 401
        c.post('/api/session',json={'token':'archive'*20}).raise_for_status()
        assert c.get(link).status_code == 404
        c.post('/api/session',json={'token':'shop'*20}).raise_for_status()
        assert c.get(link).content == data
        app.update_source_knowledge('shop',doc['id'],{**payload, 'expected_version':1})
        assert c.get(link).status_code == 409
        app.delete_source_knowledge('shop',doc['id'])
        assert c.get(link.replace('version=1','version=2')).is_error
    with pytest.raises(ValueError, match='第 2 页'):
        extract('scan.pdf', encoded(pdf_file(b'')))
