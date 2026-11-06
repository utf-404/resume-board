import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from server import ProjectStore, create_server, git_snapshot, parse_handoff, project_snapshot


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / '한글 project'
        self.project.mkdir()
        self.store = ProjectStore(self.root / 'state/projects.json', initial=None)
        self.static = self.root / 'static'
        self.static.mkdir()
        (self.static / 'index.html').write_text('hello')
        self.server = create_server(port=0, store=self.store, static_dir=self.static)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, method, url, data=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        body = json.dumps(data).encode() if data is not None else None
        client.request(method, url, body, headers or {'Content-Type': 'application/json'})
        response = client.getresponse()
        status, raw = response.status, response.read()
        client.close()
        return status, raw

    def test_parser_preserves_multiline_commands_and_code_headings(self):
        text = '# 인계\n## 목표와 완료 조건\n- 로컬 실행\n## 완료한 작업\n- [x] 서버\n## 남은 작업 순서\n1. 화면 확인\n## 다음 실행 명령\n```bash\ncd "한글 project"\n# comment\npython3 server.py\n```\n## 막힌 사항\n- 없음\n'
        sections = parse_handoff(text)
        self.assertEqual(sections['goal'], ['로컬 실행'])
        self.assertEqual(sections['completed'], ['서버'])
        self.assertEqual(sections['remaining'], ['화면 확인'])
        self.assertEqual(sections['commands'], ['cd "한글 project"\n# comment\npython3 server.py'])
        self.assertEqual(sections['blocked'], ['없음'])

    def test_standard_agents_handoff_sections_stay_distinct(self):
        text = """# 인계
## 작업 목표와 완료 조건
- 결과 확인
## 완료한 작업과 변경 파일
- 구현 완료
## 주요 결정과 이유
- 원본 보존
## 실행한 검증과 결과
- 단위 검사 통과
## 진행 중인 부분
- 통합 확인 중
## 남은 작업 순서
- [x] 이전 작업
- [ ] 남은 작업
### 세부 단계
1. 화면 확인
## 다음 실행 명령
```bash
cd /tmp
python3 server.py
```
## 막힌 사항
- 없음
"""
        result = parse_handoff(text)
        self.assertEqual(result['in_progress'], ['통합 확인 중'])
        self.assertEqual(result['remaining'], ['남은 작업', '화면 확인'])
        self.assertEqual(result['completed'], ['구현 완료', '이전 작업'])
        self.assertEqual(result['decisions'], ['원본 보존'])
        self.assertEqual(result['validation'], ['단위 검사 통과'])
        self.assertEqual(result['commands'], ['cd /tmp\npython3 server.py'])

    def test_latest_goal_excludes_historical_tasks_and_commands(self):
        text = """# 누적 인계
## 목표와 완료 조건
- 과거 목표
## 남은 순서
- 과거 작업
## 다음 명령
```bash
old-command
```
## 새 목표: Atype
- 목표/완료 조건: 새 제품 검증
- 완료: 새 제품 복사
- 남은 순서/다음 명령: 최초 시험
### 중간 진단
- 다음시험: 이미 끝난 시험
### 최신 진행 기록
- 새 제품 시험 중. 성공은 미확정.
- 다음: 결과 확인
"""
        result = parse_handoff(text)
        self.assertEqual(result['goal'], ['새 제품 검증'])
        self.assertEqual(result['completed'], ['새 제품 복사'])
        self.assertEqual(result['remaining'], ['결과 확인'])
        self.assertEqual(result['commands'], [])
        self.assertEqual(result['in_progress'], ['새 제품 시험 중. 성공은 미확정.'])

    def test_registration_persistence_delete_and_missing_folder(self):
        status, raw = self.request('POST', '/api/projects', {'path': str(self.project)})
        self.assertEqual(status, 201)
        project = json.loads(raw)['project']
        self.assertFalse(project['handoff_exists'])
        self.assertEqual(ProjectStore(self.store.file).list(), self.store.list())
        self.assertEqual(self.request('POST', '/api/projects', {'path': str(self.project)})[0], 200)
        self.project.rmdir()
        response = json.loads(self.request('GET', '/api/projects')[1])
        self.assertIsNotNone(response['projects'][0]['error'])
        self.assertEqual(self.request('DELETE', '/api/projects/' + project['id'])[0], 200)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.request('DELETE', '/api/projects/' + project['id'])[0], 404)

    def test_invalid_inputs_and_origins(self):
        self.assertEqual(self.request('POST', '/api/projects', {'path': 'relative'})[0], 400)
        self.assertEqual(self.request('POST', '/api/projects', {'path': str(self.root / 'missing')})[0], 400)
        self.assertEqual(self.request('POST', '/api/projects', {}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/projects', {}, {'Content-Type': 'application/json', 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/projects', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/projects', ['not object'])[0], 400)

    def test_static_traversal(self):
        self.assertEqual(self.request('GET', '/')[0], 200)
        (self.root / 'secret.txt').write_text('secret')
        self.assertEqual(self.request('GET', '/%2e%2e/secret.txt')[0], 404)
        (self.static / 'link.txt').symlink_to(self.root / 'secret.txt')
        self.assertEqual(self.request('GET', '/link.txt')[0], 404)

    def test_bad_store_is_not_overwritten_and_failed_save_rolls_back(self):
        malformed = self.root / 'bad.json'
        malformed.write_text('{bad')
        with self.assertRaises(RuntimeError):
            ProjectStore(malformed)
        self.assertEqual(malformed.read_text(), '{bad')
        with patch.object(self.store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.store.add(str(self.project))
        self.assertEqual(self.store.list(), [])

    def test_git_handles_unborn_branch_rename_and_parent_repo(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.project), *args], check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.invalid')
        source = self.project / '파일 original.txt'
        source.write_text('one')
        unborn = git_snapshot(self.project)
        self.assertTrue(unborn['available'])
        self.assertIsNone(unborn['last_commit'])
        git('add', '.')
        git('commit', '-qm', 'First commit')
        git('mv', source.name, '파일 renamed.txt')
        (self.project / 'untracked file.txt').write_text('two')
        snapshot = git_snapshot(self.project)
        self.assertEqual(snapshot['changed_count'], 2)
        self.assertEqual({x['path'] for x in snapshot['files']}, {'파일 renamed.txt', 'untracked file.txt'})
        self.assertEqual(snapshot['last_commit']['message'], 'First commit')
        child = self.project / 'nested'
        child.mkdir()
        self.assertFalse(git_snapshot(child)['available'])

    def test_handoff_snapshot(self):
        (self.project / 'HANDOFF.md').write_text('## 목표\n- 화면 구현\n', encoding='utf-8')
        entry, _ = self.store.add(str(self.project))
        snapshot = project_snapshot(entry)
        self.assertTrue(snapshot['handoff_exists'])
        self.assertEqual(snapshot['sections']['goal'], ['화면 구현'])
        self.assertIsNotNone(snapshot['updated_at'])


if __name__ == '__main__':
    unittest.main()
