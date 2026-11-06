"""Process-safe persistence for Resume Board's local project registrations."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading


class ProjectStore:
    def __init__(self, file, initial=None):
        self.file = Path(file).resolve()
        self.lock_file = self.file.with_name(self.file.name + '.lock')
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.entries = []
        self._snapshot = None
        with self._transaction():
            existing = self._read_disk()
            if existing is None:
                self.entries = [self.entry(Path(initial).resolve())] if initial else []
                self.save()
            else:
                self.entries = existing
                self._snapshot = self._copy(existing)

    @staticmethod
    def _copy(entries):
        return [dict(entry) for entry in entries]

    @staticmethod
    def entry(path):
        return {'id': hashlib.sha256(str(path).encode()).hexdigest()[:16], 'path': str(path)}

    @contextmanager
    def _transaction(self):
        # The stable sidecar is locked, rather than the atomically replaced JSON
        # inode. Never unlink it: another process may already be waiting on it.
        with self.lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_file.open('a+b') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self._transaction_depth = 1
                try:
                    yield
                finally:
                    self._transaction_depth = 0
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_disk(self):
        try:
            # Only a missing file permits initialization. Corrupt/invalid data
            # must never be replaced with an empty registration list.
            source = self.file.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f'프로젝트 등록 파일을 읽을 수 없습니다: {self.file}: {exc}') from exc
        try:
            entries = json.loads(source)
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get('id'), str)
                or not isinstance(entry.get('path'), str)
                for entry in entries
            ):
                raise ValueError('Invalid project list')
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f'프로젝트 등록 파일을 읽을 수 없습니다: {self.file}: {exc}') from exc
        return entries

    def _reload(self):
        entries = self._read_disk()
        if entries is None:
            raise RuntimeError(f'프로젝트 등록 파일이 없어졌습니다: {self.file}')
        self.entries = entries
        self._snapshot = self._copy(entries)

    def save(self):
        """Atomically persist a transaction, rejecting standalone stale saves."""
        with self._transaction():
            if self._read_disk() != self._snapshot:
                raise RuntimeError('등록 목록이 변경되었습니다. 다시 조회한 후 시도해 주세요.')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', dir=self.file.parent,
                    prefix=self.file.name + '.', suffix='.tmp', delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(self.entries, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.file)
                self._snapshot = self._copy(self.entries)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def list(self):
        with self._transaction():
            self._reload()
            return self._copy(self.entries)

    def add(self, raw):
        if not isinstance(raw, str) or not raw.strip() or not Path(raw).expanduser().is_absolute():
            raise ValueError('프로젝트 폴더의 절대 경로를 입력해 주세요.')
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('존재하는 폴더 경로를 입력해 주세요.')
        entry = self.entry(path)
        with self._transaction():
            self._reload()
            if entry in self.entries:
                return entry, False
            previous = self._copy(self.entries)
            self.entries.append(entry)
            try:
                self.save()
            except Exception:
                self.entries = previous
                raise
        return entry, True

    def remove(self, identifier):
        with self._transaction():
            self._reload()
            previous = self._copy(self.entries)
            self.entries = [entry for entry in self.entries if entry['id'] != identifier]
            if len(previous) == len(self.entries):
                return False
            try:
                self.save()
            except Exception:
                self.entries = previous
                raise
        return True
