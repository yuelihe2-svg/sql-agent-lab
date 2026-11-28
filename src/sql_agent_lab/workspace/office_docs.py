"""Local extraction of versioned evidence from Word and text PDFs."""
import base64
import binascii
from html import escape
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_FILE = 5 * 1024 * 1024
EXTENSIONS = {'.pdf', '.docx'}
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _xml(package, name):
    item = package.getinfo(name)
    if item.file_size > 8 * 1024 * 1024:
        raise ValueError('Word 文档结构过大。')
    data = package.read(item)
    if b'<!DOCTYPE' in data.replace(b'\x00', b'').upper():
        raise ValueError('不支持带 DTD 的 Word 文档。')
    return ET.fromstring(data)


def _word_text(node):
    return ''.join(n.text or '' if n.tag == _W + 't' else ' '
                   for n in node.iter() if n.tag in {_W+'t', _W+'tab', _W+'br'}).strip()


def _word(data):
    try:
        with ZipFile(io.BytesIO(data)) as package:
            body = _xml(package, 'word/document.xml').find(_W + 'body')
            styles = {}
            if 'word/styles.xml' in package.namelist():
                for style in _xml(package, 'word/styles.xml').findall(_W + 'style'):
                    name = style.find(_W + 'name')
                    if name is not None:
                        styles[style.get(_W + 'styleId')] = name.get(_W + 'val', '')
    except (KeyError, BadZipFile, ET.ParseError, RuntimeError, NotImplementedError) as error:
        raise ValueError('无法读取 DOCX，请使用完整、未加密的 Word 文档。') from error
    if body is None:
        raise ValueError('DOCX 正文缺失。')
    headings = {}
    paragraph = table = 0
    for block in body:
        section = ' / '.join(headings.values()) or '正文'
        if block.tag == _W + 'p':
            text = _word_text(block)
            if not text:
                continue
            paragraph += 1
            style = block.find(f"{_W}pPr/{_W}pStyle")
            style_id = style.get(_W + 'val', '') if style is not None else ''
            heading = re.fullmatch(r'(?:heading|标题)\s*([1-9])', styles.get(style_id, style_id), re.I)
            outline = block.find(f"{_W}pPr/{_W}outlineLvl")
            level = int(heading[1]) if heading else 0
            if outline is not None and outline.get(_W+'val') in tuple('012345678'):
                level = int(outline.get(_W+'val')) + 1
            if level:
                headings = {k:v for k,v in headings.items() if k < level}
                headings[level] = text
            else:
                yield f'{section} · 段落 {paragraph}', text
        elif block.tag == _W + 'tbl':
            if block.find('.//' + _W + 'vMerge') is not None or block.find('.//' + _W + 'gridSpan') is not None or len(list(block.iter(_W + 'tbl'))) != 1:
                raise ValueError('暂不支持合并单元格或嵌套表格，请拆分为普通表格后导入。')
            table += 1
            header = []
            for row, tr in enumerate(block.findall(_W+'tr'), 1):
                cells = [' '.join(filter(None, (_word_text(p) for p in cell.findall(_W+'p'))))
                         for cell in tr.findall(_W+'tc')]
                if row == 1:
                    header = cells
                if any(cells):
                    text = ' | '.join(f'{h}：{v}' for h,v in zip(header, cells)) if row > 1 and len(header)==len(cells) else ' | '.join(cells)
                    yield f'{section} · 表 {table} 第 {row} 行', text


def _pdf(data):
    if not data.startswith(b'%PDF-'):
        raise ValueError('文件内容不是 PDF。')
    tool = shutil.which('pdftotext')
    if not tool:
        raise ValueError('导入 PDF 需要本机 Poppler pdftotext，请安装后重试。')
    with tempfile.TemporaryDirectory() as tmp:
        source, output = Path(tmp)/'input.pdf', Path(tmp)/'pages.txt'
        source.write_bytes(data)
        try:
            result = subprocess.run([tool, '-enc','UTF-8','-layout','-l','121',str(source),str(output)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except subprocess.TimeoutExpired as error:
            raise ValueError('PDF 解析超时，请拆分后导入。') from error
        if result.returncode or not output.exists():
            raise ValueError('PDF 无法读取，请检查文件是否损坏或加密。')
        if output.stat().st_size > 200_000:
            raise ValueError('PDF 文本过长，请拆分后导入。')
        pages = output.read_text(encoding='utf8').removesuffix('\f').split('\f')
    if len(pages) > 120:
        raise ValueError('PDF 超过 120 页，请拆分后导入。')
    for page, content in enumerate(pages, 1):
        lines = [s.strip() for s in content.splitlines() if s.strip()]
        if not lines:
            raise ValueError(f'PDF 第 {page} 页无可提取文本，请移除空白页或先对扫描页做 OCR。')
        for line, text in enumerate(lines, 1):
            yield f'第 {page} 页 · 行 {line}', text


def extract(title, encoded):
    extension = Path(title).suffix.lower()
    if extension not in EXTENSIONS or not isinstance(encoded, str) or len(encoded) > 4*((MAX_FILE+2)//3):
        raise ValueError('请上传不超过 5 MiB 的 DOCX 或 PDF。')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('无效的文件编码。') from error
    if len(data) > MAX_FILE:
        raise ValueError('文件超过 5 MiB。')
    parts = dict(_pdf(data) if extension == '.pdf' else _word(data))
    if not parts:
        raise ValueError('文件没有可检索正文。')
    return json.dumps(parts, ensure_ascii=False)

def word_preview(doc, parts, selected):
    """Render the full extracted text and give the cited block a stable fragment."""
    sections = []
    for index, (location, text) in enumerate(parts):
        body = escape(text)
        if index == selected:
            body = f'<mark>{body}</mark>'
        sections.append(f'<section id="p-{index}"><h2>{escape(location)}</h2><p>{body}</p></section>')
    title = escape(doc['title'])
    html = (f'<!doctype html><html lang="zh"><meta charset="utf-8"><title>{title}</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{max-width:50rem;margin:2rem auto;padding:0 1rem;font:1rem/1.6 system-ui}'
            'h2{font-size:1rem}section{border-top:1px solid #ccc}p{white-space:pre-wrap}</style>'
            f'<h1>{title}</h1><p>原文文本预览 · v{int(doc["version"])}</p>'
            + ''.join(sections) + '</html>')
    return html.encode('utf8')
