"""Isolated browser acceptance; all content is synthetic, all requests stay on loopback."""
# CONFIG
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audits' / 'input-page-checks-2'
PREFIX = '2026-11-02-input-page-checks-2'
VIEWPORT = {'width': 390, 'height': 844}
TIMEOUT_S = 0.6

import asyncio
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile

sys.path.insert(0, str(ROOT))
from aiohttp import web
from playwright.async_api import async_playwright, expect
from input import llm_postprocess, server

TAIL = '这是合成记录的唯一结尾：最后一段必须完整保留。'
COVERAGE_TAIL = '本次合成专属尾句暗号：青瓷白鹭入夜。'
TEXT = ''.join(f'第{i:03d}段：今天只用合成文字检查润色流程，英文术语Playwright保持原样，标点和段落顺序都应完整保留。' for i in range(75)) + TAIL
LONG_SENTENCE = '合成超长单句用来触发长度上限' * 285 + '唯一结尾必须保留'
state = {'mode': 'ok', 'requests': [], 'call': 0, 'last_call': None,
         'last_call_started': None, 'release_last_call': None}
result = {'viewport': VIEWPORT, 'app_js_sha256': hashlib.sha256((ROOT / 'input/static/app.js').read_bytes()).hexdigest(),
          'server_file': str(Path(server.__file__).resolve()), 'pid': os.getpid(), 'checks': [], 'cases': []}


def check(name, condition):
    result['checks'].append({'name': name, 'pass': bool(condition)})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)
    assert condition, name


def listen_socket():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    assert sock.getsockname()[1] not in (8765, 8086)
    return sock


async def fake(request):
    body = await request.json()
    text = body['messages'][-1]['content']
    state['call'] += 1
    mode, call = state['mode'], state['call']
    reason = 'length' if (mode == 'threshold' and len(text) > 1200) or (mode == 'middle-length' and call == 2) else 'stop'
    state['requests'].append({'mode': mode, 'call': call, 'chars': len(text),
                              'text': text, 'finish_reason': reason})
    if mode == 'missing-tail' and call == state['last_call']:
        state['last_call_started'].set()
        await state['release_last_call'].wait()
    content = text[:1200] if reason == 'length' else text
    if mode == 'middle-length':
        content = '截断的输出不能写入' if reason == 'length' else text.replace('原始', '整理')
    if mode == 'missing-tail' and call == state['last_call']:
        content = content.removesuffix(COVERAGE_TAIL)
    await asyncio.sleep(1.5 if mode == 'timeout' and call == 2 else 0.08)
    return web.json_response({'choices': [{'message': {'content': content},
                                          'finish_reason': reason}]})


async def main():
    OUT.mkdir(exist_ok=True)
    app_sock, fake_sock = listen_socket(), listen_socket()
    port, fake_port = app_sock.getsockname()[1], fake_sock.getsockname()[1]
    result.update(app_port=port, fake_port=fake_port)
    fake_app = web.Application()
    fake_app.router.add_post('/v1/chat/completions', fake)
    fake_runner = web.AppRunner(fake_app, access_log=None)
    app_runner = None
    with tempfile.TemporaryDirectory(prefix='input-polish-page-') as tmp:
        seed, data = Path(tmp) / 'seed', Path(tmp) / 'copy'
        server.InputStore(seed).create_text_session('合成历史记录', '合成原始文字，仅用于隔离验收。')
        shutil.copytree(seed, data)
        result['copied_synthetic_data'] = True
        result['data_root'] = str(data)
        prompt = data / 'prompt.txt'
        prompt.write_text('Echo this synthetic text exactly for local acceptance.', encoding='utf-8')
        (data / 'llm-postprocess.json').write_text(json.dumps({
            'enabled': True, 'auto': False, 'endpoint': f'http://127.0.0.1:{fake_port}/v1/chat/completions',
            'model': 'local-fake', 'prompt_path': str(prompt), 'timeout_s': TIMEOUT_S,
            'server_exe': '', 'model_path': ''}), encoding='utf-8')
        app = server.create_app(data, expected_port=port, dashboard_root=data / 'organization',
                                pending_dir=data / 'pending', wt_root=data / 'wt')
        app_runner = web.AppRunner(app, access_log=None)
        try:
            await fake_runner.setup()
            await web.SockSite(fake_runner, fake_sock).start()
            await app_runner.setup()
            await web.SockSite(app_runner, app_sock).start()
            store = app[server.STORE_KEY]
            check('isolated store and no ASR preload', store.root == data.resolve() and server.ASR_PRELOAD_KEY not in app)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
                page = await context.new_page()
                page_errors = []
                page.on('pageerror', lambda error: page_errors.append(str(error)))
                async def local_only(route):
                    if route.request.url.startswith(f'http://127.0.0.1:{port}/'):
                        await route.continue_()
                    else:
                        raise AssertionError('Unexpected browser URL: ' + route.request.url)
                await context.route('**/*', local_only)

                async def create(text):
                    await page.goto(f'http://127.0.0.1:{port}/')
                    await page.click('#text-open')
                    await page.fill('#text-input', text)
                    await page.click('#save-text')
                    await expect(page.locator('#result-editor')).to_have_value(text)
                    await expect(page.locator('#llm-run')).to_be_enabled()
                    return store.list_sessions()[0]['id']

                async def click_run():
                    async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/llm-postprocess')) as pending:
                        await page.click('#llm-run')
                    response = await pending.value
                    await expect(page.locator('#llm-run')).to_be_enabled()
                    return response.status

                async def photo(name):
                    await page.locator('#result-editor').evaluate('(e) => { e.scrollTop = e.scrollHeight; }')
                    await page.locator('#save-status').scroll_into_view_if_needed()
                    await page.screenshot(path=str(OUT / f'{PREFIX}-{name}.png'))

                for name, mode, text, failure in [
                    ('normal', 'ok', TEXT, None),
                    ('threshold-segmented', 'threshold', TEXT, None),
                    ('truncated', 'threshold', LONG_SENTENCE, '截断'),
                    ('timeout', 'timeout', TEXT, '超时')]:
                    print(f'CASE {name}: {len(text)} chars', flush=True)
                    sid = await create(text)
                    # Check a saved human edit too; failure must preserve the current draft.
                    if name == 'timeout':
                        text = text.replace('今天只用', '手改后只用', 1)
                        await page.fill('#result-editor', text)
                        await page.wait_for_function('() => selectedDetail === null && !editorDirty')
                        check('autosave: enabled button with cleared detail cache', await page.is_enabled('#llm-run'))
                    before = store.session_detail(sid)['revisions']
                    state.update(mode=mode, requests=[], call=0)
                    if name == 'timeout':
                        await photo('autosave-after')
                    status = await click_run()
                    after = store.session_detail(sid)['revisions']
                    calls = list(state['requests'])
                    sent = ''.join(c['text'] for c in calls)
                    check(name + ': input pieces preserve order', bool(sent) and text.startswith(sent))
                    if not failure or name == 'truncated':
                        check(name + ': all characters sent', sent == text)
                    if name == 'timeout':
                        check(name + ': timeout after one successful piece', len(calls) == 2 and len(sent) < len(text) and all(c['chars'] <= 1200 for c in calls))
                    check(name + ': editor text preserved', await page.input_value('#result-editor') == text)
                    if failure:
                        await expect(page.locator('#save-status')).to_contain_text(failure)
                        await page.wait_for_timeout(3300)  # include one production polling cycle
                        await expect(page.locator('#save-status')).to_contain_text(failure)
                        await expect(page.locator('#save-status')).to_be_in_viewport()
                        check(name + ': Chinese error and no revision written', status == 502 and after == before)
                        check(name + ': error styling visible', 'error' in (await page.get_attribute('#save-status', 'class')))
                    else:
                        check(name + ': one complete polished revision', status == 201 and len(after) == len(before) + 1 and after[-1]['content'] == text and after[-1]['content'].endswith(TAIL))
                        check(name + ': pieces <= 1200 chars', len(calls) > 1 and all(c['chars'] <= 1200 for c in calls))
                    check(name + ': previous revisions unchanged', after[:len(before)] == before)
                    check(name + ': retry button enabled', await page.is_enabled('#llm-run'))
                    await photo(name)
                    case = {'name': name, 'chars': len(text), 'status': status,
                            'before_versions': len(before), 'after_versions': len(after),
                            'pieces': [{k: v for k, v in c.items() if k != 'text'} for c in calls],
                            'status_text': await page.text_content('#save-status'),
                            'editor_sha256': hashlib.sha256((await page.input_value('#result-editor')).encode('utf-8')).hexdigest()}
                    if failure:
                        state.update(mode='ok', requests=[], call=0)
                        retry = await click_run()
                        retried = store.session_detail(sid)['revisions']
                        check(name + ': second click succeeds with same text', retry == 201 and len(retried) == len(before) + 1 and retried[-1]['content'] == text and await page.input_value('#result-editor') == text)
                        await photo(name + '-retry')
                        case.update(retry_status=retry, retry_versions=len(retried))
                    result['cases'].append(case)

                # A successful run keeps only the length-limited middle piece verbatim.
                pieces = [f'第{i}段原始文字：' + '样' * 1184 + '收尾保留。' for i in range(1, 4)]
                pieces[1] = '\n\n' + pieces[1]
                fallback_text = ''.join(pieces)
                check('fallback scenario: exactly three pieces', llm_postprocess.split_text(fallback_text) == pieces)
                fallback_sid = await create(fallback_text)
                fallback_before = store.session_detail(fallback_sid)['revisions']
                state.update(mode='middle-length', requests=[], call=0)
                fallback_status = await click_run()
                fallback_after = store.session_detail(fallback_sid)['revisions']
                expected = pieces[0].replace('原始', '整理') + pieces[1] + pieces[2].replace('原始', '整理')
                check('fallback scenario: one saved version, middle kept verbatim',
                      fallback_status == 201 and len(fallback_after) == len(fallback_before) + 1
                      and fallback_after[:-1] == fallback_before and fallback_after[-1]['content'] == expected
                      and fallback_after[-1]['data']['polish_fallback_pieces'] == [2]
                      and fallback_after[-1]['data']['polish_piece_count'] == 3)
                check('fallback scenario: all pieces sent in order',
                      [call['text'] for call in state['requests']] == pieces)
                await expect(page.locator('#result-editor')).to_have_value(expected)
                warning = page.locator('#llm-coverage-warning')
                await expect(warning).to_have_text('第 2/3 段没润色成，保留原文')
                await page.wait_for_timeout(3300)  # include one polling cycle
                await expect(warning).to_be_visible()
                await page.goto(f'http://127.0.0.1:{port}/?session={fallback_sid}')
                await expect(page.locator('#result-editor')).to_have_value(expected)
                await expect(warning).to_have_text('第 2/3 段没润色成，保留原文')
                await warning.scroll_into_view_if_needed()
                await page.screenshot(path=str(OUT / f'{PREFIX}-piece-fallback.png'))
                result['fallback_scenario'] = {'status': fallback_status, 'pieces': 3,
                    'fallback_pieces': [2], 'warning_text': await warning.text_content(),
                    'reopened_saved_version': True, 'screenshot': f'{PREFIX}-piece-fallback.png'}
                await page.click('#llm-original')
                await expect(page.locator('#result-editor')).to_have_value(fallback_text)
                await expect(warning).to_be_hidden()
                state.update(mode='ok', requests=[], call=0)
                normal_status = await click_run()
                await expect(warning).to_be_hidden()
                check('fallback scenario: original clears hint; normal retry has no fallback hint',
                      normal_status == 201 and store.session_detail(fallback_sid)['revisions'][-1]['data']['polish_fallback_pieces'] == [])

                # Hold the final piece in flight so the screenshot proves the whole long draft reached it.
                coverage_text = TEXT.removesuffix(TAIL) + COVERAGE_TAIL
                coverage_sid = await create(coverage_text)
                coverage_before = store.session_detail(coverage_sid)['revisions']
                segment_count = len(llm_postprocess.split_text(coverage_text))
                check('missing-tail scenario: long draft is segmented', segment_count > 1)
                state.update(mode='missing-tail', requests=[], call=0, last_call=segment_count,
                             last_call_started=asyncio.Event(), release_last_call=asyncio.Event())
                async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/llm-postprocess')) as pending:
                    await page.click('#llm-run')
                    await asyncio.wait_for(state['last_call_started'].wait(), timeout=10)
                    check('missing-tail scenario: final segment is in progress',
                          len(state['requests']) == segment_count
                          and await page.text_content('#llm-run') == '润色中…'
                          and '润色中' in (await page.text_content('#save-status')))
                    await photo('coverage-last-segment-progress')
                    state['release_last_call'].set()
                coverage_response = await pending.value
                await expect(page.locator('#llm-run')).to_be_enabled()
                coverage_after = store.session_detail(coverage_sid)['revisions']
                coverage_calls = list(state['requests'])
                coverage_output = coverage_after[-1]['content']
                coverage_data = coverage_after[-1]['data']
                check('missing-tail scenario: final response omits original tail',
                      coverage_response.status == 201 and len(coverage_calls) == segment_count
                      and ''.join(call['text'] for call in coverage_calls) == coverage_text
                      and not coverage_output.endswith(COVERAGE_TAIL)
                      and coverage_data.get('polish_tail_found') is False)
                warning = page.locator('#llm-coverage-warning')
                await expect(warning).to_contain_text('没找到原稿最后一句')
                await expect(warning).to_be_visible()
                coverage_warning = await warning.text_content()
                await warning.scroll_into_view_if_needed()
                await page.screenshot(path=str(OUT / f'{PREFIX}-coverage-missing-tail-warning.png'))
                check('missing-tail scenario: warning shown after completed segmented run',
                      len(coverage_after) == len(coverage_before) + 1 and '润色稿可能漏了内容' in coverage_warning)

                await page.click('#llm-original')
                await expect(page.locator('#result-editor')).to_have_value(coverage_text)
                await expect(warning).to_be_hidden()
                await photo('coverage-return-original-clears-warning')
                check('missing-tail scenario: returning to original clears warning',
                      await page.input_value('#result-editor') == coverage_text and await warning.is_hidden())

                short_text = '短稿合成内容，末句完整保留。'
                short_sid = await create(short_text)
                state.update(mode='ok', requests=[], call=0, last_call=None,
                             last_call_started=None, release_last_call=None)
                short_status = await click_run()
                short_revisions = store.session_detail(short_sid)['revisions']
                await expect(warning).to_be_hidden()
                await photo('coverage-short-no-warning')
                check('short draft: complete output without coverage warning',
                      short_status == 201 and short_revisions[-1]['content'] == short_text and await warning.is_hidden())
                result['coverage_scenario'] = {
                    'segments': segment_count,
                    'status': coverage_response.status,
                    'warning_text': coverage_warning,
                    'return_original_cleared_warning': True,
                    'short_status': short_status,
                    'short_warning_hidden': await warning.is_hidden(),
                    'screenshots': [
                        f'{PREFIX}-coverage-last-segment-progress.png',
                        f'{PREFIX}-coverage-missing-tail-warning.png',
                        f'{PREFIX}-coverage-return-original-clears-warning.png',
                        f'{PREFIX}-coverage-short-no-warning.png',
                    ],
                }
                check('no browser JavaScript errors', not page_errors)
                result['page_errors'] = page_errors
                result['sessions'] = len(store.list_sessions())
                await browser.close()
        finally:
            if app_runner:
                await app_runner.cleanup()
            await fake_runner.cleanup()
    check('temporary data cleaned', not Path(result['data_root']).exists())
    for p in (port, fake_port):
        with socket.socket() as sock:
            check(f'own port {p} released', sock.connect_ex(('127.0.0.1', p)) != 0)
    result['pass'] = True


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        OUT.mkdir(exist_ok=True)
        (OUT / f'{PREFIX}-evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
