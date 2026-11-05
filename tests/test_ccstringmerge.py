"""Round-trip tests on synthetic packages: python -m unittest discover -s tests"""
import hashlib, os, shutil, struct, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ccstringmerge as m

OBJD, IMG = 0x319E4F1D, 0x00B2D882


def refpack_literal(data):
    """Valid RefPack stream using literal blocks only (enough to test the decoder path the game data uses)."""
    out = bytearray([0x10, 0xFB]) + len(data).to_bytes(3, 'big')
    p = 0
    while len(data) - p >= 4:
        n = min(112, (len(data) - p) // 4 * 4)
        out.append(0xE0 | ((n - 4) >> 2)); out += data[p:p + n]; p += n
    out.append(0xFC | (len(data) - p)); out += data[p:]
    return bytes(out)


def stbl(pairs):
    return m.stbl_build(2, dict(pairs))


def write_pkg(path, resources, flags=0, header_pos_40=False, major=2, minor=0):
    """resources: [(type, group, inst, bytes, compress)]; flags 2 = constant group (needs one group for all)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body, ents = bytearray(b'\0' * 96), []
    for t, g, inst, data, comp in resources:
        raw = refpack_literal(data) if comp else data
        ents.append((t, g, inst >> 32, inst & 0xFFFFFFFF, len(body), len(raw) | 0x80000000, len(data),
                     0xFFFF if comp else 0, 1))
        body += raw
    ipos = len(body)
    idx = struct.pack('<I', flags)
    if flags & 2:
        idx += struct.pack('<I', resources[0][1])
    for t, g, ih, il, off, sz, mem, comp, c in ents:
        idx += struct.pack('<I', t)
        if not flags & 2:
            idx += struct.pack('<I', g)
        idx += struct.pack('<IIIIIHH', ih, il, off, sz, mem, comp, c)
    body += idx
    body[0:4] = b'DBPF'
    struct.pack_into('<II', body, 4, major, minor)
    struct.pack_into('<I', body, 36, len(ents)); struct.pack_into('<I', body, 44, len(idx))
    struct.pack_into('<I', body, 60, 3); struct.pack_into('<I', body, 40 if header_pos_40 else 64, ipos)
    with open(path, 'wb') as f:
        f.write(body)


def tree_hashes(root):
    out = {}
    for d, _, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(d, fn)
            with open(p, 'rb') as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def langs(prefix_inst, pairs_by_lang):
    return [(m.STBL, 0, (lang << 56) | prefix_inst, stbl(pairs), comp) for lang, pairs, comp in pairs_by_lang]


class Base(unittest.TestCase):
    def setUp(self):
        m.game_running = lambda: False      # the real check would refuse while the game runs
        self.root = tempfile.mkdtemp(prefix='ccsm-')
        self.mods = os.path.join(self.root, 'The Sims 3', 'Mods', 'Packages')
        P = lambda *a: os.path.join(self.mods, *a)
        # a normal CC object: OBJD + image + 3 languages, one compressed
        write_pkg(P('chair.package'), [(OBJD, 0, 1, b'objd-chair' * 50, False), (IMG, 0, 2, b'img' * 300, True)]
                  + langs(0x11, [(0, [(1, 'Chair'), (2, 'Nice chair')], True), (1, [(1, 'Stuhl')], False),
                                 (2, [(1, 'Chaise')], False)]))
        # index with constant group (flags 2), in a subfolder, group != 0
        write_pkg(P('Sub', 'table.package'), [(OBJD, 7, 3, b'objd-table' * 40, False),
                                               (m.STBL, 7, 0x22, stbl([(3, 'Table')]), False),
                                               (m.STBL, 7, (1 << 56) | 0x22, stbl([(3, 'Tisch')]), True)], flags=2)
        # same key as chair with different text -> conflict, and a same-text duplicate
        write_pkg(P('zz_lamp.package'), [(OBJD, 0, 4, b'objd-lamp', False)]
                  + langs(0x33, [(0, [(1, 'Lamp?'), (2, 'Nice chair'), (5, 'Lamp')], False)]))
        # strings only (e.g. a translation package)
        write_pkg(P('strings_only.package'), langs(0x44, [(0, [(9, 'Only text')], False)]))
        # no strings at all, index position only at offset 40
        write_pkg(P('nostrings.package'), [(IMG, 0, 5, b'pixels' * 99, False)], header_pos_40=True)
        # script mod, Sims 4 package, garbage
        write_pkg(P('script.package'), [(m.S3SA, 0, 6, b'MZ' * 100, False)] + langs(0x55, [(0, [(7, 'S')], False)]))
        write_pkg(P('sims4.package'), [(OBJD, 0, 8, b'x', False)], major=2, minor=1)
        with open(P('broken.package'), 'wb') as f:
            f.write(b'DBPF' + b'\0' * 200)
        with open(P('chair.package'), 'rb') as f:
            bad = bytearray(f.read())
        struct.pack_into('<I', bad, 64, len(bad) + 10)
        with open(P('badindex.package'), 'wb') as f:
            f.write(bad)
        self.before = tree_hashes(self.mods)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_tool(self, *args):
        return m.main(list(args) + ['--yes'])


class StringsMode(Base):
    def test_plan(self):
        plan = m.make_plan(self.mods, 'strings', m.backup_root_for(self.mods))
        work = dict(plan.work)
        self.assertEqual(work, {'chair.package': 'strip', os.path.join('Sub', 'table.package'): 'strip',
                                'strings_only.package': 'move', 'zz_lamp.package': 'strip'})
        skipped = dict(plan.skipped)
        self.assertIn('script', skipped['script.package'])
        self.assertIn('2.1', skipped['sims4.package'])
        self.assertIn('badindex.package', skipped)
        self.assertEqual(plan.untouched, 1)
        self.assertEqual(plan.pool.conflicts, 1)
        self.assertEqual(plan.pool.same, 1)
        self.assertEqual(plan.pool.tables[0], {1: 'Chair', 2: 'Nice chair', 3: 'Table', 5: 'Lamp', 9: 'Only text'})
        self.assertEqual(plan.pool.tables[1], {1: 'Stuhl', 3: 'Tisch'})

    def test_apply_and_undo(self):
        self.assertEqual(self.run_tool(self.mods), 0)
        backup_root = os.path.join(self.root, 'The Sims 3', m.BACKUP_DIR_NAME)
        self.assertTrue(os.path.isdir(backup_root), 'backup must sit next to Mods, outside it')
        sp = m.Package(os.path.join(self.mods, m.STRINGS_NAME))
        self.assertEqual(len(sp.entries), 3)
        chair = m.Package(os.path.join(self.mods, 'chair.package'))
        self.assertEqual([e[0] for e in chair.entries], [OBJD, IMG])
        self.assertFalse(os.path.exists(os.path.join(self.mods, 'strings_only.package')))
        for name in ('script.package', 'sims4.package', 'broken.package', 'nostrings.package', 'badindex.package'):
            self.assertEqual(tree_hashes(self.mods)[name], self.before[name], name + ' must stay untouched')
        # second run: nothing new
        self.assertEqual(self.run_tool(self.mods), 0)
        self.assertEqual(len(os.listdir(backup_root)), 1)
        run_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        self.assertEqual(self.run_tool(run_dir), 0)
        self.assertEqual(tree_hashes(self.mods), self.before)

    def test_new_cc_after_a_run(self):
        self.run_tool(self.mods)
        write_pkg(os.path.join(self.mods, 'new_sofa.package'),
                  [(OBJD, 0, 10, b'sofa', False)] + langs(0x66, [(0, [(11, 'Sofa')], False)]))
        after_first = tree_hashes(self.mods)
        self.run_tool(self.mods)
        sp = m.Package(os.path.join(self.mods, m.STRINGS_NAME))
        with sp.open() as f:
            en = dict(next(t for t in m.read_strings(sp, f) if t[0] == 0)[2])
        self.assertEqual(en[11], 'Sofa'); self.assertEqual(en[1], 'Chair')
        backup_root = os.path.join(self.root, 'The Sims 3', m.BACKUP_DIR_NAME)
        first, second = sorted(os.path.join(backup_root, d) for d in os.listdir(backup_root))
        self.assertNotEqual(self.run_tool(first), 0, 'older run must not be undone before the newer one')
        self.assertEqual(self.run_tool(second), 0)
        self.assertEqual(tree_hashes(self.mods), after_first)
        self.assertEqual(self.run_tool(first), 0)
        h = tree_hashes(self.mods); h.pop('new_sofa.package')
        self.assertEqual(h, self.before)

    def test_failure_rolls_back(self):
        orig = m.verify
        m.verify = lambda *a: (_ for _ in ()).throw(m.PackageError('forced'))
        try:
            with self.assertRaises(m.PackageError):
                self.run_tool(self.mods)
        finally:
            m.verify = orig
        self.assertEqual(tree_hashes(self.mods), self.before)

    def test_undo_keeps_changed_files(self):
        self.run_tool(self.mods)
        with open(os.path.join(self.mods, 'chair.package'), 'ab') as f:
            f.write(b'user edit')
        backup_root = os.path.join(self.root, 'The Sims 3', m.BACKUP_DIR_NAME)
        run_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        changed = tree_hashes(self.mods)
        self.assertNotEqual(self.run_tool(run_dir), 0)
        self.assertEqual(tree_hashes(self.mods), changed, 'a blocked undo changes nothing')
        self.assertEqual(self.run_tool(run_dir, '--force'), 0)
        self.assertTrue(os.path.exists(os.path.join(run_dir, 'changed-since', 'chair.package')), 'edit kept aside')
        self.assertEqual(tree_hashes(self.mods), self.before)

    def test_deleted_cc_does_not_block_undo(self):
        self.run_tool(self.mods)
        os.remove(os.path.join(self.mods, 'zz_lamp.package'))
        backup_root = os.path.join(self.root, 'The Sims 3', m.BACKUP_DIR_NAME)
        run_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        self.assertEqual(self.run_tool(run_dir), 0)
        expect = dict(self.before); expect.pop('zz_lamp.package')
        self.assertEqual(tree_hashes(self.mods), expect)
        self.assertTrue(os.path.exists(os.path.join(run_dir, 'files', 'zz_lamp.package')))


class MergeMode(Base):
    def test_merge_and_undo(self):
        write_pkg(os.path.join(self.mods, 'dup.package'), [(OBJD, 0, 1, b'other bytes', False)])
        before = tree_hashes(self.mods)
        self.assertEqual(m.main([self.mods, '--merge', '--part-size', '16', '--yes']), 0)
        names = sorted(os.listdir(self.mods))
        self.assertIn(m.MERGED_PREFIX + '001.package', names)
        self.assertIn(m.STRINGS_NAME, names)
        for n in ('chair.package', 'zz_lamp.package', 'nostrings.package', 'dup.package'):
            self.assertNotIn(n, names)
        for n in ('script.package', 'sims4.package', 'broken.package'):
            self.assertIn(n, names)
        merged = m.Package(os.path.join(self.mods, m.MERGED_PREFIX + '001.package'))
        k = {m.tgi(e): e for e in merged.entries}
        with merged.open() as f:
            self.assertEqual(m.read_raw(f, k[(OBJD, 0, 0, 1)]), b'objd-chair' * 50, 'first in name order wins')
        self.assertIn((OBJD, 7, 0, 3), k)
        backup_root = os.path.join(self.root, 'The Sims 3', m.BACKUP_DIR_NAME)
        run_dir = os.path.join(backup_root, os.listdir(backup_root)[0])
        self.assertEqual(self.run_tool(run_dir), 0)
        self.assertEqual(tree_hashes(self.mods), before)


class RefPack(unittest.TestCase):
    def test_literal_roundtrip(self):
        for n in (0, 1, 3, 4, 5, 113, 1000):
            d = os.urandom(n)
            self.assertEqual(m.refpack(refpack_literal(d)), d)


if __name__ == '__main__':
    unittest.main()
