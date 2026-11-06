#!/usr/bin/env python3
"""Resume Board: dependency-free, loopback-only project dashboard."""
import argparse
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 16384
MAX_HANDOFF = 1024 * 1024


def classify_heading(value):
    value = re.sub(r'\s+', ' ', value.lower())
    if any(s in value for s in ('명령', 'command')) and not any(s in value for s in ('남은', '다음 할', '다음 작업')):
        return 'commands'
    if any(s in value for s in ('막힌', '차단', 'block', '장애')):
        return 'blocked'
    if any(s in value for s in ('남은', '다음 작업', '다음 할', '할 일', '할일', 'remaining', 'todo', 'next step')):
        return 'remaining'
    if any(s in value for s in ('진행 중', '진행중', 'in progress', 'in-progress')):
        return 'in_progress'
    if any(s in value for s in ('완료한', '완료 작업', '완료 구현', 'completed', 'done', '변경 파일')):
        return 'completed'
    if any(s in value for s in ('목표', '완료 조건', 'goal', 'objective')):
        return 'goal'
    if any(s in value for s in ('결정', '이유', '근거', 'decision')):
        return 'decisions'
    if any(s in value for s in ('검증', 'validation', 'verification')):
        return 'validation'
    return None


def parse_handoff(source):
    """Read headings, keeping historical goals out of the current summary.

    Explicit new-goal headings define a current scope. When that scope contains
    dated/narrative updates, only its latest update supplies live progress/tasks.
    The complete original document remains available in the handoff tab.
    """
    keys = ('goal', 'completed', 'remaining', 'commands', 'blocked',
            'in_progress', 'decisions', 'validation')
    sections = {key: [] for key in keys}
    # Ignore headings inside fenced commands when selecting the goal boundary.
    lines = source.splitlines()
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if not fence:
            match = re.match(r'^(#{1,6})\s+(.+)', line)
            if match:
                headings.append((index, len(match.group(1)), match.group(2)))
    boundaries = [h for h in headings if re.search(r'새\s*목표|현재\s*목표|new goal|current goal', h[2], re.I)]
    start = boundaries[-1][0] if boundaries else 0
    # Narrative updates are not task headings. For an explicitly scoped goal,
    # retain only the latest update as live progress, rather than stale history.
    updates = [h for h in headings if boundaries and h[0] > start
               and h[1] > boundaries[-1][1] and classify_heading(h[2]) is None]
    latest_update = updates[-1][0] if updates else None
    stack = []
    fence, block, block_active = None, [], None
    inline_keys = {'완료': 'completed', '검증': 'validation', '차단': 'blocked',
                   '다음': 'remaining', '다음시험': 'remaining', '다음 시험': 'remaining',
                   '주요 결정/진행': 'decisions'}
    for index, line in enumerate(lines[start:], start):
        stripped = line.strip()
        marker = re.match(r'^(`{3,}|~{3,})', stripped)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                if block_active:
                    sections[block_active].append('\n'.join(block))
                fence, block = None, []
            else:
                block.append(line)
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)', stripped)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            category = classify_heading(title)
            if latest_update is not None and index >= latest_update and category is None:
                category = 'in_progress'
            stack.append((level, category))
            continue
        active = next((category for _, category in reversed(stack) if category), None)
        if marker:
            fence, block, block_active = marker.group(1), [], active
            if latest_update is not None and index < latest_update:
                block_active = None
            continue
        if not stripped:
            continue
        text = re.sub(r'^(?:[-*+]\s+|\d+[.)]\s+)', '', stripped)
        checked = re.match(r'^\[([ xX])\]\s*(.*)', text)
        if checked:
            text = checked.group(2)
        # Recognize explicitly labeled bullets used in isaac_usd's new goal.
        label = re.match(r'^([^:：]{1,30})[:：]\s*(.+)$', text)
        if label:
            category = inline_keys.get(label.group(1)) or classify_heading(label.group(1))
            if category:
                active = category
                text = label.group(2)
            elif active == 'goal':
                # Role/metadata bullets under a goal are not goal statements.
                continue
        if latest_update is not None:
            if index < latest_update and index > start and active in ('remaining', 'commands', 'blocked', 'in_progress'):
                continue
            if index < latest_update and any(h[0] <= index for h in updates):
                continue
        if active:
            if checked and checked.group(1).lower() == 'x' and active == 'remaining':
                active = 'completed'
            if active == 'commands' and text.startswith('`') and text.endswith('`'):
                text = text[1:-1]
            sections[active].append(text)
    if fence and block_active and block:
        sections[block_active].append('\n'.join(block))
    return sections


def git_snapshot(path):
    result = dict(available=False, branch=None, changed_count=0, files=[], last_commit=None, error=None)
    if not shutil.which('git'):
        result['error'] = 'Git이 설치되어 있지 않습니다.'
        return result
    def git(*args):
        process = subprocess.run(['git', '-C', str(path), *args], capture_output=True, timeout=8)
        if process.returncode:
            raise ValueError(process.stderr.decode('utf-8', 'replace').strip())
        return process.stdout
    try:
        root = git('rev-parse', '--show-toplevel').decode('utf-8', 'replace').strip()
        if Path(root).resolve() != path.resolve():
            return result
        result['available'] = True
        result['branch'] = git('rev-parse', '--abbrev-ref', 'HEAD').decode().strip()
    except ValueError:
        # An unborn branch still has useful status.
        if not result['available']:
            return result
        try:
            result['branch'] = git('symbolic-ref', '--short', 'HEAD').decode().strip()
        except ValueError:
            result['branch'] = 'HEAD'
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
        return result
    try:
        entries = git('status', '--porcelain=v1', '-z', '--untracked-files=all').split(b'\0')
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            status = entry[:2].decode('ascii', 'replace')
            filename = entry[3:].decode('utf-8', 'replace')
            if 'R' in status or 'C' in status:
                index += 1  # porcelain -z reports destination, then original path.
            result['files'].append({'status': status, 'path': filename})
        result['changed_count'] = len(result['files'])
        try:
            commit = git('log', '-1', '--format=%h%x00%s%x00%cI').decode('utf-8', 'replace').strip().split('\0', 2)
            result['last_commit'] = dict(zip(('hash', 'message', 'date'), commit))
        except ValueError:
            pass
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)
    return result


def project_snapshot(entry):
    path = Path(entry['path'])
    handoff_path = path / 'HANDOFF.md'
    data = dict(entry, name=path.name or str(path), updated_at=None, handoff_exists=False, handoff='', error=None)
    if not path.is_dir():
        data['error'] = '프로젝트 폴더를 찾을 수 없습니다.'
    else:
        try:
            if handoff_path.is_file():
                data['handoff_exists'] = True
                if handoff_path.stat().st_size > MAX_HANDOFF:
                    raise ValueError('HANDOFF.md가 1MB를 초과합니다.')
                data['handoff'] = handoff_path.read_text(encoding='utf-8-sig')
                data['updated_at'] = datetime.fromtimestamp(handoff_path.stat().st_mtime, timezone.utc).isoformat()
        except (OSError, UnicodeError, ValueError) as exc:
            data['error'] = str(exc)
    data['sections'] = parse_handoff(data['handoff'])
    data['summary_note'] = ('새 목표 이후의 내용과 최신 진행 기록을 기준으로 요약합니다. '
                            '명시된 다음 할 일이 없더라도 작업 완료를 뜻하지 않습니다. '
                            '상세 조건 변경은 인계 원문을 함께 확인하세요.'
                            if re.search(r'^#{1,6}\s+.*(?:새\s*목표|현재\s*목표|new goal|current goal)',
                                         data['handoff'], re.M | re.I) else '')
    data['git'] = git_snapshot(path) if path.is_dir() else dict(available=False, branch=None, changed_count=0, files=[], last_commit=None, error=None)
    return data


class ProjectStore:
    def __init__(self, file, initial=ROOT):
        self.file = Path(file)
        self.lock = threading.Lock()
        if self.file.exists():
            try:
                self.entries = json.loads(self.file.read_text())
                if not isinstance(self.entries, list) or any(not isinstance(x, dict) or not isinstance(x.get('path'), str) or not isinstance(x.get('id'), str) for x in self.entries):
                    raise ValueError('Invalid project list')
            except (ValueError, OSError) as exc:
                raise RuntimeError(f'프로젝트 등록 파일을 읽을 수 없습니다: {self.file}: {exc}') from exc
        else:
            self.entries = [self.entry(Path(initial).resolve())] if initial else []
            self.save()

    @staticmethod
    def entry(path):
        return {'id': hashlib.sha256(str(path).encode()).hexdigest()[:16], 'path': str(path)}

    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.file)

    def list(self):
        with self.lock:
            return [dict(x) for x in self.entries]

    def add(self, raw):
        if not isinstance(raw, str) or not raw.strip() or not Path(raw).expanduser().is_absolute():
            raise ValueError('프로젝트 폴더의 절대 경로를 입력해 주세요.')
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('존재하는 폴더 경로를 입력해 주세요.')
        entry = self.entry(path)
        with self.lock:
            if entry in self.entries:
                return entry, False
            previous = self.entries[:]
            self.entries.append(entry)
            try:
                self.save()
            except OSError:
                self.entries = previous
                raise
        return entry, True

    def remove(self, identifier):
        with self.lock:
            previous = self.entries[:]
            self.entries = [x for x in self.entries if x['id'] != identifier]
            if len(previous) == len(self.entries):
                return False
            try:
                self.save()
            except OSError:
                self.entries = previous
                raise
            return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid placing local project names or paths in access logs.
        pass

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def trusted_request(self):
        expected_port = self.server.server_port
        def valid_authority(authority):
            try:
                parsed = urlsplit('http://' + authority)
                return parsed.hostname in ('127.0.0.1', 'localhost', '::1') and (parsed.port or 80) == expected_port and not parsed.username and parsed.path == ''
            except ValueError:
                return False
        if not valid_authority(self.headers.get('Host', '')):
            return False
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != 'http' or not valid_authority(parsed.netloc) or parsed.path:
                return False
        return True

    def do_GET(self):
        if not self.trusted_request():
            return self.send_json({'error': '로컬 주소에서만 접근할 수 있습니다.'}, 403)
        route = urlsplit(self.path).path
        if route == '/api/projects':
            return self.send_json({'projects': [project_snapshot(x) for x in self.server.store.list()]})
        if route.startswith('/api/'):
            return self.send_json({'error': 'API를 찾을 수 없습니다.'}, 404)
        static = self.server.static_dir.resolve()
        candidate = (static / unquote(route).lstrip('/')).resolve()
        if route == '/':
            candidate = static / 'index.html'
        if not candidate.is_relative_to(static) or not candidate.is_file():
            return self.send_json({'error': '파일을 찾을 수 없습니다.'}, 404)
        try:
            body = candidate.read_bytes()
            mime = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') or mime == 'application/javascript' else ''))
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_json({'error': '파일을 읽을 수 없습니다.'}, 500)

    def mutate(self):
        if not self.trusted_request():
            return self.send_json({'error': '외부 출처의 요청은 허용하지 않습니다.'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.send_json({'error': 'application/json 요청이 필요합니다.'}, 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > MAX_BODY:
                return self.send_json({'error': '요청 크기가 너무 큽니다.'}, 413)
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError('JSON 객체가 필요합니다.')
            route = urlsplit(self.path).path
            if self.command == 'POST' and route == '/api/projects':
                entry, created = self.server.store.add(body.get('path'))
                return self.send_json({'project': project_snapshot(entry)}, 201 if created else 200)
            match = re.fullmatch(r'/api/projects/([a-f0-9]{16})(/open)?', route)
            if not match:
                return self.send_json({'error': 'API를 찾을 수 없습니다.'}, 404)
            identifier, action = match.groups()
            entry = next((x for x in self.server.store.list() if x['id'] == identifier), None)
            if not entry:
                return self.send_json({'error': '등록된 프로젝트가 없습니다.'}, 404)
            if self.command == 'DELETE' and not action:
                self.server.store.remove(identifier)
                return self.send_json({'ok': True})
            if self.command == 'POST' and action:
                if not Path(entry['path']).is_dir():
                    raise ValueError('프로젝트 폴더를 찾을 수 없습니다.')
                if not shutil.which('xdg-open'):
                    return self.send_json({'error': '폴더 열기를 지원하지 않는 환경입니다. 경로를 복사해 주세요.'}, 503)
                try:
                    result = subprocess.run(['xdg-open', entry['path']], capture_output=True, timeout=4)
                except subprocess.TimeoutExpired:
                    return self.send_json({'error': '폴더 열기 응답 시간이 초과되었습니다. 파일 탐색기를 확인해 주세요.'}, 504)
                if result.returncode:
                    return self.send_json({'error': '파일 탐색기를 열지 못했습니다. 경로를 복사해 주세요.'}, 503)
                return self.send_json({'ok': True})
            return self.send_json({'error': '지원하지 않는 요청입니다.'}, 405)
        except (ValueError, UnicodeError) as exc:
            return self.send_json({'error': str(exc)}, 400)
        except OSError:
            return self.send_json({'error': '로컬 파일 작업을 완료하지 못했습니다.'}, 500)

    do_POST = mutate
    do_DELETE = mutate


def create_server(host='127.0.0.1', port=8765, store=None, static_dir=None):
    if host != 'localhost' and not ipaddress.ip_address(host).is_loopback:
        raise ValueError('서버는 로컬 루프백 주소에서만 실행할 수 있습니다.')
    # IPv4 defaults support all ordinary local browser sessions.
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store if store is not None else ProjectStore(ROOT / '.data/projects.json')
    server.static_dir = Path(static_dir or ROOT / 'static')
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', 'localhost'])
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        server = create_server(args.host, args.port)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f'실행 실패: {exc}\n')
    print(f'Resume Board → http://{args.host}:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
