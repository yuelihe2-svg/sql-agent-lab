"""Exercise a new workspace in a real browser using the offline fixture."""
import argparse
from contextlib import closing
import csv
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


def exercise(url, database, channel):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url + '/')
                      else route.abort())
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/workspace/')
        expect(page.get_by_text('点击左侧「添加数据库」', exact=False)).to_be_visible()
        page.get_by_role('button', name='+ 添加数据库').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('数据库文件').set_input_files(database)
        dialog.get_by_label('名称', exact=True).fill('验收门店')
        dialog.get_by_label('业务说明', exact=False).fill('金额以元为单位')
        dialog.get_by_role('button', name='添加并开始提问').click()
        expect(dialog).not_to_be_visible()
        question = page.get_by_role('textbox', name='输入问题')
        question.fill('那杭州呢？')
        question.press('Control+Enter')
        expect(page.locator('.message').last).to_contain_text('补充完整条件')
        question.fill('列出所有门店记录')
        page.get_by_role('button', name='发送', exact=True).click()
        table = page.get_by_role('table')
        expect(table.locator('tbody tr')).to_have_count(5)
        page.get_by_role('button', name='显示这条 SQL').click()
        page.get_by_role('button', name='复制 SQL').click()
        expect(page.get_by_text('已复制', exact=True)).to_be_visible()
        assert page.evaluate('navigator.clipboard.readText()') == 'SELECT n FROM numbers'
        with page.expect_download() as download:
            page.get_by_role('button', name='下载预览 CSV').click()
        with Path(download.value.path()).open(encoding='utf-8-sig', newline='') as handle:
            assert list(csv.reader(handle)) == [['n'], ['1'], ['2'], ['3'], ['4'], ['5']]
        page.reload()
        expect(page.get_by_role('table').locator('tbody tr')).to_have_count(5)
        page.goto(url + '/')
        page.locator('.history-item').filter(has_text='列出所有门店记录').click()
        expect(page.locator('#tableWrap tbody tr')).to_have_count(5)
        page.locator('#detailView').click()
        expect(page.locator('#modelCalls')).to_contain_text('1 次')
        assert not errors, errors
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Interpreter with sql-agent-lab installed')
    parser.add_argument('--channel', default=None, help='Installed browser channel, e.g. msedge')
    args = parser.parse_args()
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        database = root / 'sample.sqlite'
        with closing(sqlite3.connect(database)) as conn:
            conn.execute('CREATE TABLE numbers(n INTEGER)')
            conn.executemany('INSERT INTO numbers VALUES (?)', [(i,) for i in range(1, 7)])
            conn.commit()
        fixture = root / 'fixture'
        process = subprocess.Popen([args.python, str(Path(__file__).with_name('browser_lifecycle_fixture.py')),
                                    '--empty-workspace', '--root', str(fixture)], stdout=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        try:
            url = json.loads(process.stdout.readline())['url']
            exercise(url, database, args.channel)
        finally:
            if fixture.is_dir():
                (fixture / 'shutdown').touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        metrics = json.loads((fixture / 'fixture-metrics.json').read_text(encoding='utf8'))
        assert process.returncode == 0
        assert metrics['real_model_calls'] == metrics['blocked_outbound_connections'] == 0
    print('Browser smoke passed: upload, clarify, query, copy, CSV, reload, single-question history.')


if __name__ == '__main__':
    main()
