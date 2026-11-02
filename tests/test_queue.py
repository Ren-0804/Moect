"""Queue behaviour: a whole selection is exported by one sequential job."""
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from core.service import Service

USER = 'wxid_me'
SESSIONS = [('queue_a@chatroom', '队列一'), ('queue_b', '队列二'), ('queue_c@chatroom', '队列三')]


def fixture(root, per_session=6):
    for folder in ('contact', 'session', 'message'):
        (root / folder).mkdir(parents=True)
    with closing(sqlite3.connect(root / 'contact' / 'contact.db')) as con, con:
        con.execute('CREATE TABLE contact (id INTEGER,username TEXT,local_type INTEGER,alias TEXT,'
                    'remark TEXT,nick_name TEXT,big_head_url TEXT,small_head_url TEXT,description TEXT)')
        rows = [(1, USER, '', '我', '自己', '', '', '')]
        rows += [(index + 2, username, '', title, title, '', '', '')
                 for index, (username, title) in enumerate(SESSIONS)]
        con.executemany('INSERT INTO contact VALUES (?,?,0,?,?,?,?,?,?)', rows)
    with closing(sqlite3.connect(root / 'session' / 'session.db')) as con, con:
        con.execute('CREATE TABLE SessionTable (username TEXT,last_timestamp INTEGER,sort_timestamp INTEGER,summary TEXT)')
        con.executemany('INSERT INTO SessionTable VALUES (?,?,?,?)',
                        [(username, 1800000000 - index * 60, 1800000000 - index * 60, 'synthetic')
                         for index, (username, _) in enumerate(SESSIONS)])
    with closing(sqlite3.connect(root / 'message' / 'message_0.db')) as con, con:
        con.execute('CREATE TABLE Name2Id (user_name TEXT)')
        con.executemany('INSERT INTO Name2Id VALUES (?)', [(USER,), ('friend',)])
        for index, (username, _) in enumerate(SESSIONS):
            table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
            con.execute(f'CREATE TABLE "{table}" (local_id INTEGER, server_id TEXT, local_type INTEGER, '
                        'sort_seq INTEGER, real_sender_id INTEGER,create_time INTEGER,message_content BLOB,'
                        'compress_content BLOB,packed_info_data BLOB)')
            for message in range(per_session):
                con.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?,?)',
                            (message + 1, '%s-%s' % (index, message), 1, message,
                             1 if message % 2 else 2,
                             1800000000 + index * 60 + message, '第%d条' % message, None, None))
    return dict(root=str(root), wxid=USER, name='fixture')


class QueueExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lib = fixture(self.root / 'db')

    def tearDown(self):
        self.temp.cleanup()

    def test_selection_becomes_one_sequential_job(self):
        service = Service(Path(__file__).parent.parent / 'ui', root=self.root / 'state')
        service.config['export_dir'] = str(self.root / 'exports')
        service.import_archive(self.lib['root'])
        order = [username for username, _ in reversed(SESSIONS)]
        result = service.export({'sessions': order, 'format': 'json'})
        service.pool.shutdown(wait=True)
        tasks = service.state()['tasks']
        self.assertEqual(len(tasks), 1, 'a queue must not fan out into one job per session')
        task = tasks[0]
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['count'], 6 * len(SESSIONS))
        self.assertEqual(task.get('sessions'), len(SESSIONS))
        self.assertNotIn('folder', task)
        folder = service.task_folder(result['job'])
        documents = sorted(p.name for p in folder.glob('*.json'))
        self.assertEqual(len(documents), len(SESSIONS))
        page = (folder / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(page.count('<li data-search'), len(SESSIONS))
        self.assertIn('id="q"', page)
        positions = [page.index(title) for _, title in reversed(SESSIONS)]
        self.assertEqual(positions, sorted(positions), 'export order must follow the selection')

    def test_unknown_ids_are_ignored(self):
        service = Service(Path(__file__).parent.parent / 'ui', root=self.root / 'state2')
        service.config['export_dir'] = str(self.root / 'exports2')
        service.import_archive(self.lib['root'])
        result = service.export({'sessions': [SESSIONS[0][0], 'missing_session', SESSIONS[1][0], SESSIONS[2][0]],
                                 'format': 'txt'})
        service.pool.shutdown(wait=True)
        task = service.state()['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        folder = service.task_folder(result['job'])
        self.assertEqual(len(list(folder.glob('*.txt'))), len(SESSIONS))


if __name__ == '__main__':
    unittest.main()
