import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_store import ProjectStore


def concurrent_change(file, project, remove_id, ready, start):
    store = ProjectStore(file)
    ready.put(True)
    if not start.wait(10):
        raise RuntimeError('Test synchronization timeout')
    store.add(project)
    if remove_id:
        store.remove(remove_id)


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.file = self.root / 'state' / 'projects.json'
        self.a = self.root / 'A'
        self.b = self.root / 'B'
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_instances_reload_before_mutation_and_reads(self):
        first = ProjectStore(self.file)
        second = ProjectStore(self.file)
        a, _ = first.add(str(self.a))
        b, _ = second.add(str(self.b))
        self.assertEqual(first.list(), [a, b])
        self.assertEqual(second.list(), [a, b])
        first.remove(a['id'])
        self.assertEqual(second.list(), [b])
        second.remove(b['id'])
        self.assertEqual(first.list(), [])

    def test_concurrent_process_mutations_preserve_all_changes(self):
        # Every process loads the same initial snapshot before the barrier.
        store = ProjectStore(self.file)
        old = self.root / 'old'
        old.mkdir()
        removed, _ = store.add(str(old))
        context = multiprocessing.get_context('spawn')
        ready = context.Queue()
        start = context.Event()
        projects = [self.root / f'project-{number}' for number in range(8)]
        for project in projects:
            project.mkdir()
        processes = [context.Process(target=concurrent_change, args=(
            str(self.file), str(project), removed['id'] if index == 0 else None,
            ready, start,
        )) for index, project in enumerate(projects)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=15))
            start.set()
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual({item['path'] for item in store.list()}, {str(p) for p in projects})
        finally:
            start.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join()
            ready.close()
            ready.join_thread()

    def test_failed_save_rolls_back_and_cleans_unique_temporary(self):
        store = ProjectStore(self.file)
        original = self.file.read_bytes()
        with patch.object(store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.add(str(self.a))
        self.assertEqual(store.entries, [])
        self.assertEqual(self.file.read_bytes(), original)
        entry, _ = store.add(str(self.a))
        original = self.file.read_bytes()
        with patch.object(store, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.remove(entry['id'])
        self.assertEqual(store.entries, [entry])
        self.assertEqual(self.file.read_bytes(), original)
        with patch('project_store.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                store.add(str(self.b))
        self.assertEqual(store.entries, [entry])
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.file.parent.glob('*.tmp')), [])

    def test_corrupt_disk_is_preserved_on_constructor_and_existing_store(self):
        store = ProjectStore(self.file)
        for invalid in ('{bad', '[null]', '{}', '[{"id":"x"}]'):
            self.file.write_text(invalid)
            with self.assertRaises(RuntimeError):
                ProjectStore(self.file, initial=self.a)
            with self.assertRaises(RuntimeError):
                store.add(str(self.b))
            self.assertEqual(self.file.read_text(), invalid)

    def test_stale_direct_save_rejected_and_initial_only_applied_once(self):
        first = ProjectStore(self.file, initial=self.a)
        second = ProjectStore(self.file, initial=self.b)
        self.assertEqual(first.list(), second.list())
        second.add(str(self.b))
        with self.assertRaises(RuntimeError):
            first.save()
        self.assertEqual(len(json.loads(self.file.read_text())), 2)
        first.list()
        first.save()


if __name__ == '__main__':
    unittest.main()
