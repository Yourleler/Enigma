import io
import json
import os
import shlex
import sys
import threading
import time

import pytest

from enigma import Enigma, FakeModelClient, SessionStore, WorkspaceContext, cli
from enigma.evaluator import BenchmarkEvaluator
from enigma.memory import LayeredMemory
from enigma.models import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from enigma.orchestration import parse_review_findings
from enigma.static_review import scan_python_file


def agent_at(root, outputs, **options):
    return Enigma(FakeModelClient(outputs), WorkspaceContext.build(root),
                  SessionStore(root / ".enigma" / "sessions"), approval_policy=options.pop("approval_policy", "auto"),
                  feature_flags={"reflection": False}, **options)


@pytest.mark.parametrize("content", ['marker = "</tool>"\n', '</content>\n<tool>literal</tool>\n中文'])
def test_xml_delimiters_survive_actual_write(tmp_path, content):
    request = f'<tool name="write_file" path="literal.txt"><content>{content}</content></tool>'
    agent = agent_at(tmp_path, [request, '<final>Written.</final>'])
    agent.ask("Write the exact literal")
    assert (tmp_path / 'literal.txt').read_text(encoding='utf-8') == content


def test_plain_progress_cannot_finish_a_task(tmp_path):
    agent = agent_at(tmp_path, ['I will inspect and fix this file.'] * 3, max_steps=1)
    assert agent.ask('Create ready.txt').startswith('Stopped')
    assert agent.current_task_state.status == 'stopped'


def test_last_tool_gets_a_final_round_without_an_extra_tool(tmp_path):
    agent = agent_at(tmp_path, ['<tool>{"name":"list_files","args":{}}</tool>', '<final>Listed.</final>'], max_steps=1)
    assert agent.ask('List files') == 'Listed.'
    assert agent.current_task_state.tool_steps == 1
    blocked = agent_at(tmp_path, ['<tool>{"name":"list_files","args":{}}</tool>'] * 2, max_steps=1)
    assert blocked.ask('Keep listing').startswith('Stopped')
    assert blocked.current_task_state.tool_steps == 1


def test_model_failure_persists_terminal_state_and_report(tmp_path):
    agent = agent_at(tmp_path, [])
    with pytest.raises(RuntimeError):
        agent.ask('Fail the provider')
    state = json.loads(agent.run_store.task_state_path(agent.current_task_state).read_text(encoding='utf-8'))
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding='utf-8'))
    assert state['status'] == report['status'] == 'failed'
    assert state['stop_reason'] == 'model_error'


def test_completion_check_is_independent_of_final_claim(tmp_path):
    agent = agent_at(tmp_path, ['<final>Done.</final>'], completion_check=lambda a: ((a.root / 'ready.txt').exists(), 'ready.txt required'))
    assert 'verification failed' in agent.ask('Create ready.txt')
    assert agent.current_task_state.verification_status == 'failed'
    assert agent.current_task_state.status == 'stopped'


def test_known_secret_absent_from_all_runtime_artifacts(tmp_path, monkeypatch):
    secret = 'known-private-fixture-value'
    monkeypatch.setenv('ENIGMA_FIXTURE_KEY', secret)
    agent = agent_at(tmp_path, [f'<final>{secret}</final>'], secret_env_names=['ENIGMA_FIXTURE_KEY'])
    agent.session['title'] = secret
    agent.session['opaque'] = {secret: 'fixture'}
    agent._register_session_in_db()
    agent.memory.promote_durable([('user-preferences', secret)])
    agent.ask(secret)
    for path in (tmp_path / '.enigma').rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_session_atomic_write_preserves_old_file_on_error(tmp_path):
    store = SessionStore(tmp_path)
    store.save({'id': 'session', 'history': ['old']})
    with pytest.raises(TypeError):
        store.save({'id': 'session', 'history': [object()]})
    assert store.load('session')['history'] == ['old']
    assert not list(tmp_path.glob('*.tmp'))
    with pytest.raises(ValueError):
        store.load('../escape')


@pytest.mark.parametrize('text', ['No findings.\ncritical: authentication bypass', '严重：鉴权逻辑错误', '{bad json', ''])
def test_review_never_turns_invalid_or_mixed_output_into_clean(text):
    assert any(item.is_blocking() for item in parse_review_findings(text))


def test_syntax_error_blocks_review(tmp_path):
    path = tmp_path / 'broken.py'
    path.write_text('def broken(:')
    assert scan_python_file(path, tmp_path)[0].severity == 'error'


class ByteStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 1) if size >= 0 else size)


def test_openai_stream_decodes_unicode_and_last_line():
    payload = ('data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': '中文'}, ensure_ascii=False)
               + '\ndata: {"type":"response.completed","response":{}}')
    client = OpenAICompatibleModelClient('fixture', 'https://example.test/v1', '', None, 1)
    tokens = []
    assert client._stream_openai_response(ByteStream(payload.encode()), tokens.append, None, None) == '中文'
    assert ''.join(tokens) == '中文'


@pytest.mark.parametrize('end', ['\ndata: {"type":"response.failed","response":{"error":"fixture"}}\n', ''])
def test_partial_openai_stream_is_not_success(end):
    payload = 'data: {"type":"response.output_text.delta","delta":"partial"}\n' + end
    client = OpenAICompatibleModelClient('fixture', 'https://example.test/v1', '', None, 1)
    with pytest.raises(RuntimeError):
        client._stream_openai_response(ByteStream(payload.encode()), lambda _: None, None, None)


def test_anthropic_stream_unicode_error_and_completion():
    client = AnthropicCompatibleModelClient('fixture', 'https://example.test/v1', '', None, 1)
    payload = 'event: content_block_delta\ndata: ' + json.dumps({'delta': {'text': '中文'}}, ensure_ascii=False)
    assert client._stream_anthropic_response(ByteStream((payload + '\nevent: message_stop\ndata: {}').encode()), lambda _: None) == '中文'
    with pytest.raises(RuntimeError):
        client._stream_anthropic_response(ByteStream((payload + '\nevent: error\ndata: {"error":"failed"}').encode()), lambda _: None)


def test_repl_keeps_ask_and_main_thread_owns_approval(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, ['<tool>{"name":"write_file","args":{"path":"denied.txt","content":"x"}}</tool>', '<final>Declined.</final>'], approval_policy='ask')
    answers = iter(['Write denied.txt', 'n', '/exit'])
    owner = threading.get_ident()
    def answer(prompt):
        assert threading.get_ident() == owner
        assert agent.approval_policy == 'ask'
        return next(answers)
    monkeypatch.setattr('builtins.input', answer)
    monkeypatch.setattr(cli, 'build_agent', lambda _: agent)
    monkeypatch.setattr(cli.repl, 'read_key_nonblocking', lambda: None)
    assert cli.main([]) == 0
    assert not (tmp_path / 'denied.txt').exists()
    assert agent.approval_callback is None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group check')
def test_cancel_shell_kills_waiting_child_before_effect(tmp_path):
    agent = agent_at(tmp_path, [])
    script = "import time; from pathlib import Path; time.sleep(2); Path('late.txt').write_text('bad')"
    command = f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
    thread = threading.Thread(target=lambda: agent.run_tool('run_shell', {'command': command, 'timeout': 10}))
    thread.start()
    time.sleep(0.2)
    agent.request_cancel()
    thread.join(2)
    assert not thread.is_alive()
    assert agent._last_tool_result_metadata['aborted'] is True
    time.sleep(2)
    assert not (tmp_path / 'late.txt').exists()


@pytest.mark.parametrize('prefix', ['', 'Python'])
def test_chinese_memory_recalls_relevant_note(prefix):
    memory = LayeredMemory()
    memory.append_note(prefix + '修复登录鉴权时保留用户权限校验')
    memory.append_note('报表数据每周更新')
    assert '权限校验' in memory.retrieval_view('登录鉴权修复')
    assert '报表' not in memory.retrieval_view('登录鉴权修复')


def test_reserved_xml_tags_inside_content_are_not_arguments(tmp_path):
    content = '<path>../escape</path>\n<command>bad</command>'
    agent = agent_at(tmp_path, [f'<tool name="write_file" path="literal.txt"><content>{content}</content></tool>', '<final>Written.</final>'])
    agent.ask('Write exact content')
    assert (tmp_path / 'literal.txt').read_text(encoding='utf-8') == content
    assert Enigma.parse('<tool name="write_file" path="a"><content>a</content></tool><tool name="write_file" path="b"><content>b</content></tool>')[0] == 'retry'


def plan_step(goal, path, value):
    return {'goal': goal, 'allowed_tools': ['write_file'], 'verification': {'files': {path: value}}}


def xml_tool(name, args):
    return '<tool>' + json.dumps({'name': name, 'args': args}) + '</tool>'


def test_orchestration_requires_real_artifact_and_declared_checks(tmp_path):
    plan = '<plan>' + json.dumps({'steps': [plan_step('Write ready', 'ready.txt', 'ready')]}) + '</plan>'
    agent = agent_at(tmp_path, [plan, xml_tool('write_file', {'path': 'ready.txt', 'content': 'ready'}), '<final>Ready.</final>', '<final>No findings.</final>'])
    assert agent.orchestrate('Write ready') .startswith('Orchestration complete.')
    assert (tmp_path / 'ready.txt').read_text() == 'ready'
    assert agent.current_task_state.verification_status == 'verified'
    unverified = agent_at(tmp_path, ['<plan>## Steps\n1. Create absent.txt</plan>', '<final>Done.</final>', '<final>No findings.</final>'])
    assert unverified.orchestrate('Create absent.txt').startswith('Orchestration unverified:')
    assert not (tmp_path / 'absent.txt').exists()


def test_repair_preserves_original_remaining_steps(tmp_path):
    first = plan_step('Write first', 'a.txt', 'a')
    second = plan_step('Write second', 'b.txt', 'b')
    initial = '<plan>' + json.dumps({'steps': [first, second]}) + '</plan>'
    repair = '<plan>' + json.dumps({'steps': [first]}) + '</plan>'
    agent = agent_at(tmp_path, [initial, '<final>Premature.</final>', repair,
        xml_tool('write_file', {'path': 'a.txt', 'content': 'a'}), '<final>Repaired.</final>',
        '<final>First now verified.</final>', xml_tool('write_file', {'path': 'b.txt', 'content': 'b'}),
        '<final>Second verified.</final>', '<final>No findings.</final>'])
    assert agent.orchestrate('Write both files').startswith('Orchestration complete.')
    assert (tmp_path / 'a.txt').read_text() == 'a'
    assert (tmp_path / 'b.txt').read_text() == 'b'


def test_benchmark_tool_policy_is_enforced_even_if_artifact_preexists(tmp_path):
    evaluator = BenchmarkEvaluator(workspace_root=tmp_path / 'bench', artifact_path=tmp_path / 'result.json',
        model_client_factory=lambda **_: FakeModelClient([
            xml_tool('run_shell', {'command': 'echo forbidden > README.md'}), '<final>Done.</final>']))
    task = evaluator.load()['tasks'][0]
    task['allowed_tools'] = ['read_file']
    row = evaluator.run_task(task)
    assert row['passed'] is False
    assert row['allowed_tools_compliant'] is False
    assert row['failure_category'] == 'tool_policy_violation'


def test_shell_timeout_is_structured_and_bounded(tmp_path):
    agent = agent_at(tmp_path, [])
    command = subprocess_command('import time; time.sleep(10)')
    started = time.monotonic()
    agent.run_tool('run_shell', {'command': command, 'timeout': 1})
    assert time.monotonic() - started < 5
    assert agent._last_tool_result_metadata['timed_out'] is True
    assert agent._last_tool_result_metadata['tool_error_code'] == 'tool_timeout'


def subprocess_command(script):
    if os.name == 'nt':
        import subprocess
        return subprocess.list2cmdline([sys.executable, '-c', script])
    return f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'


def test_final_answer_can_explain_protocol_tags():
    text = 'Use <plan>steps</plan> and literal </final> in examples.'
    assert Enigma.parse('<final>' + text + '</final>') == ('final', text)


def test_truncated_nonstream_provider_response_is_not_success(monkeypatch):
    class Response(io.BytesIO):
        headers = {'Content-Type': 'application/json'}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    client = OpenAICompatibleModelClient('fixture', 'https://example.test/v1', '', None, 1)
    data = {'status': 'incomplete', 'output_text': '<final>Partial</final>'}
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: Response(json.dumps(data).encode()))
    with pytest.raises(RuntimeError):
        client.complete('fixture', 1)


def test_shell_command_column_in_session_db_is_redacted(tmp_path, monkeypatch):
    secret = 'private-command-fixture'
    monkeypatch.setenv('ENIGMA_FIXTURE_KEY', secret)
    agent = agent_at(tmp_path, [xml_tool('run_shell', {'command': 'echo ' + secret}), '<final>Checked.</final>'], secret_env_names=['ENIGMA_FIXTURE_KEY'])
    agent.ask('Check output')
    for path in (tmp_path / '.enigma').rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_one_shot_failed_verification_exits_nonzero(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, ['<final>Done.</final>'], completion_check=lambda _: (False, 'missing'))
    monkeypatch.setattr(cli, 'build_agent', lambda _: agent)
    assert cli.main(['Create ready.txt']) == 1


def test_runtime_rejects_invalid_budgets(tmp_path):
    with pytest.raises(ValueError):
        agent_at(tmp_path, [], max_steps=-1)
    with pytest.raises(ValueError):
        agent_at(tmp_path, [], max_new_tokens=0)


def test_real_loopback_http_tool_and_verified_final(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []
    outputs = [xml_tool('write_file', {'path': 'ready.txt', 'content': 'ready'}), '<final>Ready.</final>']

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            payload = json.dumps({'status': 'completed', 'output_text': outputs.pop(0)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        agent = agent_at(tmp_path, [], max_steps=1,
            completion_check=lambda a: ((a.root / 'ready.txt').read_text() == 'ready', {'file': 'ready.txt'}))
        agent.model_client = OpenAICompatibleModelClient('fixture', f'http://127.0.0.1:{server.server_port}/v1', '', None, 2)
        assert agent.ask('Write ready.txt') == 'Ready.'
        assert agent.current_task_state.verification_status == 'verified'
        assert len(calls) == 2
        assert all(path == '/v1/responses' for path, _ in calls)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('global_check', [None, lambda _: (True, {'global': 'passed'})])
def test_later_step_cannot_invalidate_earlier_acceptance(tmp_path, global_check):
    plan = '<plan>' + json.dumps({'steps': [plan_step('Write a', 'a.txt', 'a'), plan_step('Write b', 'b.txt', 'b')]}) + '</plan>'
    agent = agent_at(tmp_path, [plan, xml_tool('write_file', {'path': 'a.txt', 'content': 'a'}), '<final>A.</final>',
        xml_tool('write_file', {'path': 'a.txt', 'content': 'bad'}), xml_tool('write_file', {'path': 'b.txt', 'content': 'b'}),
        '<final>B.</final>', '<final>No findings.</final>'], completion_check=global_check)
    assert agent.orchestrate('Write a and b', retry_limit=0).startswith('Orchestration aborted:')
    assert agent.current_task_state.verification_status != 'verified'


def test_verifier_timeout_kills_child_before_late_effect(tmp_path):
    evaluator = BenchmarkEvaluator(workspace_root=tmp_path / 'bench', artifact_path=tmp_path / 'result.json', verifier_timeout=0.3,
        model_client_factory=lambda **_: FakeModelClient(['<final>Done.</final>']))
    task = evaluator.load()['tasks'][0]
    late = tmp_path / 'late.txt'
    script = f"import time; from pathlib import Path; time.sleep(1); Path({str(late)!r}).write_text('bad')"
    task['verifier'] = subprocess_command(script)
    row = evaluator.run_task(task)
    assert row['verifier_timed_out'] is True
    assert row['verifier_exit_code'] == 124
    time.sleep(1.1)
    assert not late.exists()


def test_cancellation_during_model_call_blocks_returned_write(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, [])
    def complete(*args, **kwargs):
        agent.request_cancel()
        return xml_tool('write_file', {'path': 'cancelled.txt', 'content': 'bad'})
    monkeypatch.setattr(agent.model_client, 'complete', complete)
    assert agent.ask('Create file') == 'Stopped by user.'
    assert not (tmp_path / 'cancelled.txt').exists()
    assert agent.current_task_state.stop_reason == 'user_cancel'
    assert agent.current_task_state.tool_steps == 0


def test_cancelled_planner_never_starts_execution(tmp_path):
    agent = agent_at(tmp_path, ['<plan>## Steps\n1. Write file</plan>'])
    agent.request_cancel()
    assert agent.orchestrate('Write file').startswith('Orchestration aborted:')
    assert agent.current_task_state.stop_reason == 'user_cancel'
    assert not agent.model_client.prompts


def test_executor_cancellation_does_not_start_repair(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, [])
    count = 0
    def complete(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return '<plan>## Steps\n1. Write file</plan>'
        agent.request_cancel()
        return xml_tool('write_file', {'path': 'cancelled.txt', 'content': 'bad'})
    monkeypatch.setattr(agent.model_client, 'complete', complete)
    assert agent.orchestrate('Write file').startswith('Orchestration aborted:')
    assert count == 2
    assert not (tmp_path / 'cancelled.txt').exists()
    assert agent.current_task_state.stop_reason == 'user_cancel'


def test_cancelled_final_verifier_does_not_start_repair(tmp_path):
    def check(agent):
        agent.request_cancel()
        return False, {'cancelled': True}
    agent = agent_at(tmp_path, ['<plan>## Steps\n1. Inspect</plan>', '<final>Inspected.</final>', '<final>No findings.</final>'], completion_check=check)
    assert agent.orchestrate('Inspect').startswith('Orchestration aborted:')
    assert agent.current_task_state.stop_reason == 'user_cancel'
    assert agent.current_task_state.verification_status == 'unverified'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group interruption')
def test_keyboard_interrupt_cleans_shell_process_group(tmp_path, monkeypatch):
    import subprocess
    agent = agent_at(tmp_path, [])
    original = subprocess.Popen.communicate
    first = True
    def communicate(process, *args, **kwargs):
        nonlocal first
        if first:
            first = False
            time.sleep(0.1)
            raise KeyboardInterrupt
        return original(process, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'communicate', communicate)
    command = subprocess_command("import time; from pathlib import Path; time.sleep(1); Path('late.txt').write_text('bad')")
    with pytest.raises(KeyboardInterrupt):
        agent.run_tool('run_shell', {'command': command, 'timeout': 10})
    time.sleep(1.1)
    assert not (tmp_path / 'late.txt').exists()


def test_orchestration_keyboard_interrupt_has_cancel_reason(tmp_path, monkeypatch):
    agent = agent_at(tmp_path, [])
    def complete(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(agent.model_client, 'complete', complete)
    assert agent.orchestrate('Inspect').startswith('Orchestration aborted:')
    assert agent.current_task_state.stop_reason == 'user_cancel'


@pytest.mark.parametrize('decision', [True, False, None, 'no'])
def test_approval_callback_requires_explicit_boolean_consent(tmp_path, decision):
    agent = agent_at(tmp_path, [], approval_policy='ask', approval_callback=lambda *args: decision)
    agent.run_tool('write_file', {'path': 'consent.txt', 'content': 'x'})
    assert (tmp_path / 'consent.txt').exists() is (decision is True)
