#!/usr/bin/env python3
"""CC String Merge: faster Sim menus in The Sims 3 when you have a lot of custom content.

Every CC object brings its own string tables (STBL, one per language, usually 23). With thousands of objects the game
holds tens of thousands of tiny tables, and every localized name lookup gets slower; the Sim's pie menu sorts its
entries by name and does thousands of those lookups per click. Strings are found by key, not by table, so moving them
all into one table per language changes no text in game.

Modes
  strings (default)  Takes the string tables out of every CC package and puts them into one
                     CCStringMerge_Strings.package. Every other resource is copied byte for byte; your files keep
                     their names and folders.
  --merge            Also merges all CC packages into a few big CCStringMerge_Merged_NNN.package files.

Safety
  * Originals are moved into a backup folder outside Mods first; nothing is ever deleted.
  * Script mods (packages with an S3SA assembly), non-Sims-3 packages and anything that does not read cleanly are
    left alone and listed in the report.
  * After writing, every resource and every string is checked against the backup; on any mismatch the run is undone.
  * Undo: drop the run's backup folder onto the tool (or run it with --undo <folder>).
"""
import argparse, ctypes, datetime, hashlib, json, os, random, shutil, struct, subprocess, sys

VERSION = '0.1.0'
STBL = 0x220557DA
S3SA = 0x073FAA07
MANIFEST_RES = 0x73E93EEB      # per-package manifest written by Sims3Pack converters, instance 0 in every package
DELETED = 0xFFE0
STRINGS_NAME = 'CCStringMerge_Strings.package'
MERGED_PREFIX = 'CCStringMerge_Merged_'
BACKUP_DIR_NAME = 'CCStringMerge backups'
MANIFEST = 'manifest.jsonl'
REPORT = 'report.txt'
DBPF_LIMIT = (1 << 31) - 1
GAME_EXES = ('ts3w.exe', 'ts3.exe')


class PackageError(Exception):
    pass


# ---------------------------------------------------------------- DBPF 2.0 (The Sims 3 .package)

def refpack(src):
    try:
        if src[1] != 0xFB:
            raise PackageError('bad RefPack header')
        s = 2 + (4 if src[0] & 0x80 else 3)
        out = bytearray()
        n = len(src)
        while s < n:
            b0 = src[s]
            if b0 < 0x80:
                b1 = src[s + 1]; s += 2
                plain, copy, offs = b0 & 3, ((b0 & 0x1C) >> 2) + 3, ((b0 & 0x60) << 3) + b1 + 1
            elif b0 < 0xC0:
                b1, b2 = src[s + 1], src[s + 2]; s += 3
                plain, copy, offs = (b1 >> 6) & 3, (b0 & 0x3F) + 4, ((b1 & 0x3F) << 8) + b2 + 1
            elif b0 < 0xE0:
                b1, b2, b3 = src[s + 1], src[s + 2], src[s + 3]; s += 4
                plain, copy, offs = b0 & 3, ((b0 & 0x0C) << 6) + b3 + 5, ((b0 & 0x10) << 12) + (b1 << 8) + b2 + 1
            elif b0 < 0xFC:
                s += 1; plain, copy, offs = ((b0 & 0x1F) << 2) + 4, 0, 0
            else:
                s += 1; out += src[s:s + (b0 & 3)]; break
            out += src[s:s + plain]; s += plain
            if copy:
                if offs > len(out):
                    raise PackageError('bad RefPack data')
                start = len(out) - offs
                if offs >= copy:
                    out += out[start:start + copy]
                else:
                    for i in range(copy):
                        out.append(out[start + i])
        return bytes(out)
    except IndexError:
        raise PackageError('truncated RefPack data')


class Package:
    """Index of one package. Entries are tuples (type, group, inst_hi, inst_lo, offset, size_field, memsize, comp,
    committed); size_field keeps its top flag bit, the payload length is size_field & 0x7FFFFFFF."""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, 'rb') as f:
            h = f.read(96)
            if len(h) < 96 or h[:4] != b'DBPF':
                raise PackageError('not a package (no DBPF header)')
            major, minor = struct.unpack_from('<II', h, 4)
            if (major, minor) != (2, 0):
                raise PackageError('package version %d.%d, not The Sims 3 (2.0)' % (major, minor))
            count, = struct.unpack_from('<I', h, 36)
            isize, = struct.unpack_from('<I', h, 44)
            ipos, = struct.unpack_from('<I', h, 64)
            if ipos == 0:
                ipos, = struct.unpack_from('<I', h, 40)
            self.entries, self.deleted = [], 0
            if count == 0:
                return
            if ipos + isize > self.size or isize < 4:
                raise PackageError('index lies outside the file')
            f.seek(ipos)
            idx = f.read(isize)
        flags, = struct.unpack_from('<I', idx, 0)
        p, const = 4, {}
        for b in range(3):
            if flags & (1 << b):
                if p + 4 > len(idx):
                    raise PackageError('index truncated')
                const[b], = struct.unpack_from('<I', idx, p); p += 4
        per = 4 * (3 - len(const)) + 20
        if p + per * count > len(idx):
            raise PackageError('index truncated')
        for _ in range(count):
            v = []
            for b in range(3):
                if b in const:
                    v.append(const[b])
                else:
                    v.append(struct.unpack_from('<I', idx, p)[0]); p += 4
            il, off, fsz, mem, comp, com = struct.unpack_from('<IIIIHH', idx, p); p += 20
            if comp == DELETED:
                self.deleted += 1
                continue
            if off + (fsz & 0x7FFFFFFF) > self.size:
                raise PackageError('resource %08X:%08X:%08X%08X lies outside the file' % (v[0], v[1], v[2], il))
            self.entries.append((v[0], v[1], v[2], il, off, fsz, mem, comp, com))

    def open(self):
        return open(self.path, 'rb')


def read_raw(f, e):
    f.seek(e[4])
    raw = f.read(e[5] & 0x7FFFFFFF)
    if len(raw) != e[5] & 0x7FFFFFFF:
        raise PackageError('short read')
    return raw


def payload(f, e):
    raw = read_raw(f, e)
    if e[7] == 0xFFFF:
        return refpack(raw)
    if e[7] != 0:
        raise PackageError('unknown compression %04X' % e[7])
    return raw


def tgi(e):
    return e[0], e[1], e[2], e[3]


class Writer:
    """Writes a DBPF 2.0 package via <name>.part, index with no constant fields (the layout the game was tested with)."""

    def __init__(self, path):
        self.path, self.tmp = path, path + '.part'
        self.f = open(self.tmp, 'wb')
        self.f.write(b'\0' * 96)
        self.ents = []

    def add(self, key, raw, size_field, mem, comp, committed):
        o = self.f.tell()
        self.f.write(raw)
        self.ents.append(key + (o, size_field, mem, comp, committed))

    def add_new(self, key, data):
        self.add(key, data, len(data) | 0x80000000, len(data), 0, 1)

    def tell(self):
        return self.f.tell()

    def close(self):
        f = self.f
        ipos = f.tell()
        idx = struct.pack('<I', 0) + b''.join(struct.pack('<IIIIIIIHH', *e) for e in self.ents)
        f.write(idx)
        if f.tell() > DBPF_LIMIT:
            self.abort()
            raise PackageError('result would be over 2 GB')
        hdr = bytearray(96); hdr[0:4] = b'DBPF'
        struct.pack_into('<II', hdr, 4, 2, 0)
        struct.pack_into('<I', hdr, 36, len(self.ents)); struct.pack_into('<I', hdr, 44, len(idx))
        struct.pack_into('<I', hdr, 60, 3); struct.pack_into('<I', hdr, 64, ipos)
        f.seek(0); f.write(hdr)
        f.flush(); os.fsync(f.fileno()); f.close()
        os.replace(self.tmp, self.path)

    def abort(self):
        try:
            self.f.close()
        finally:
            if os.path.exists(self.tmp):
                os.remove(self.tmp)


# ---------------------------------------------------------------- string tables

def stbl_parse(b):
    if b[:4] != b'STBL':
        raise PackageError('string table without STBL magic')
    ver = b[4]
    count, = struct.unpack_from('<Q', b, 7)
    p, out = 17, []
    try:
        for _ in range(count):
            key, n = struct.unpack_from('<QI', b, p); p += 12
            if p + 2 * n > len(b):
                raise PackageError('string table truncated')
            out.append((key, b[p:p + 2 * n].decode('utf-16-le'))); p += 2 * n
    except (struct.error, UnicodeDecodeError) as ex:
        raise PackageError('broken string table (%s)' % ex)
    if p != len(b):
        raise PackageError('string table length mismatch')
    return ver, out


def stbl_build(ver, entries):
    parts = [b'STBL', bytes([ver]), b'\0\0', struct.pack('<Q', len(entries)), b'\0\0']
    for k, s in entries.items():
        enc = s.encode('utf-16-le')
        parts.append(struct.pack('<QI', k, len(enc) // 2) + enc)
    return b''.join(parts)


def read_strings(pkg, f):
    """All string tables of a package as [(lang, version, [(key, text)])]; raises PackageError on any broken one."""
    out = []
    for e in pkg.entries:
        if e[0] == STBL:
            ver, ent = stbl_parse(payload(f, e))
            out.append((e[2] >> 24, ver, ent))
    return out


class StringPool:
    def __init__(self):
        self.tables, self.versions = {}, {}
        self.conflicts, self.same, self.examples, self.source_tables = 0, 0, [], 0

    def add(self, rel, tables):
        for lang, ver, ent in tables:
            self.source_tables += 1
            t = self.tables.setdefault(lang, {})
            self.versions.setdefault(lang, ver)
            for k, v in ent:
                old = t.get(k)
                if old is None:
                    t[k] = v
                elif old == v:
                    self.same += 1
                else:
                    self.conflicts += 1
                    if len(self.examples) < 20:
                        self.examples.append('%s: key %016X lang %02X kept "%s", dropped "%s"'
                                             % (rel, k, lang, old[:40], v[:40]))

    def write(self, path):
        low = random.SystemRandom().getrandbits(56)
        w = Writer(path)
        try:
            for lang in sorted(self.tables):
                inst = (lang << 56) | low
                w.add_new((STBL, 0, inst >> 32, inst & 0xFFFFFFFF), stbl_build(self.versions[lang], self.tables[lang]))
            w.close()
        except BaseException:
            w.abort(); raise

    def count(self):
        return sum(len(t) for t in self.tables.values())


# ---------------------------------------------------------------- planning

class Plan:
    def __init__(self, target, mode):
        self.target, self.mode = target, mode
        self.work = []        # (rel, action) action: 'strip' | 'move'
        self.skipped = []     # (rel, reason)
        self.untouched = 0
        self.pool = StringPool()
        self.dups = 0
        self.dup_examples = []
        self.bytes_in = 0


def rel_of(target, path):
    return os.path.relpath(path, target)


def scan(target, exclude):
    found = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(root, d))) not in exclude]
        for fn in files:
            if fn.lower().endswith('.package'):
                found.append(os.path.join(root, fn))
    found.sort(key=lambda p: rel_of(target, p).lower())
    return found


def progress(i, n, what):
    if n and (i % 50 == 0 or i == n):
        sys.stdout.write('\r  %s %d / %d' % (what, i, n)); sys.stdout.flush()
        if i == n:
            sys.stdout.write('\n')


def make_plan(target, mode, backup_root):
    plan = Plan(target, mode)
    files = scan(target, {os.path.normcase(os.path.abspath(backup_root))})
    seen = set()
    for i, path in enumerate(files, 1):
        progress(i, len(files), 'reading')
        rel = rel_of(target, path)
        try:
            pkg = Package(path)
            if any(e[0] == S3SA for e in pkg.entries):
                plan.skipped.append((rel, 'script mod, left alone')); continue
            has_stbl = any(e[0] == STBL for e in pkg.entries)
            if mode == 'strings' and not has_stbl:
                plan.untouched += 1; continue
            with pkg.open() as f:
                tables = read_strings(pkg, f)
        except (PackageError, OSError, struct.error) as ex:
            plan.skipped.append((rel, str(ex))); continue
        plan.pool.add(rel, tables)
        others = [e for e in pkg.entries if e[0] != STBL]
        if mode == 'merge':
            for e in others:
                k = tgi(e)
                if k in seen:
                    if e[0] == MANIFEST_RES:
                        continue                           # metadata, the game ignores it; not worth a warning
                    plan.dups += 1
                    if len(plan.dup_examples) < 20:
                        plan.dup_examples.append('%s: %08X:%08X:%08X%08X already in an earlier package' % ((rel,) + k))
                else:
                    seen.add(k)
            plan.work.append((rel, 'move'))
        else:
            plan.work.append((rel, 'strip' if others else 'move'))
        plan.bytes_in += pkg.size
    return plan


def plan_report(plan):
    p, L = plan.pool, []
    L.append('Folder: %s' % plan.target)
    L.append('Mode:   %s' % ('merge packages + one strings package' if plan.mode == 'merge' else 'one strings package'))
    L.append('')
    if plan.mode == 'merge':
        L.append('Packages to merge:            %d (%.1f MB)' % (len(plan.work), plan.bytes_in / 2 ** 20))
        L.append('Duplicate resources dropped:  %d (same resource in two packages; the first one in name order is kept)'
                 % plan.dups)
    else:
        strip = sum(1 for _, a in plan.work if a == 'strip')
        L.append('Packages to rewrite:          %d (%.1f MB)' % (strip, plan.bytes_in / 2 ** 20))
        L.append('Packages with only strings:   %d (moved to the backup, their strings live on in %s)'
                 % (len(plan.work) - strip, STRINGS_NAME))
        L.append('Packages without strings:     %d (not touched)' % plan.untouched)
    L.append('String tables:                %d -> %d (one per language)' % (p.source_tables, len(p.tables)))
    L.append('Strings:                      %d' % p.count())
    L.append('Same string in two packages:  %d (kept once)' % p.same)
    L.append('Same key, different text:     %d (the first one in name order is kept)' % p.conflicts)
    L.append('Left alone:                   %d' % len(plan.skipped))
    for rel, why in plan.skipped:
        L.append('  - %s: %s' % (rel, why))
    if p.examples:
        L.append('Different-text examples:')
        L += ['  - ' + x for x in p.examples]
    if plan.dup_examples:
        L.append('Duplicate examples:')
        L += ['  - ' + x for x in plan.dup_examples]
    return '\n'.join(L)


# ---------------------------------------------------------------- apply, verify, undo

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def backup_root_for(target):
    """Outside Mods, so the game never loads the backups: next to the Mods folder if the target is inside one."""
    t = os.path.abspath(target)
    cur = t
    while True:
        parent = os.path.dirname(cur)
        if os.path.basename(cur).lower() == 'mods':
            return os.path.join(parent, BACKUP_DIR_NAME)
        if parent == cur:
            break
        cur = parent
    parent = os.path.dirname(t)
    return os.path.join(parent if parent != t else t, BACKUP_DIR_NAME)


def move_file(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        if os.path.getsize(src) != os.path.getsize(dst) or sha256(src) != sha256(dst):
            os.remove(dst)
            raise
        os.remove(src)


class Manifest:
    def __init__(self, path):
        self.path = path

    def write(self, **rec):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
            f.flush(); os.fsync(f.fileno())


def read_manifest(run_dir):
    with open(os.path.join(run_dir, MANIFEST), encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def apply(plan, backup_root):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
    run_dir, n = os.path.join(backup_root, stamp), 1
    while os.path.exists(run_dir):
        n += 1; run_dir = os.path.join(backup_root, '%s_%d' % (stamp, n))
    os.makedirs(run_dir)
    m = Manifest(os.path.join(run_dir, MANIFEST))
    m.write(op='run', version=VERSION, mode=plan.mode, target=os.path.abspath(plan.target), started=stamp)
    with open(os.path.join(run_dir, REPORT), 'w', encoding='utf-8') as f:
        f.write(plan_report(plan) + '\n')
    try:
        for i, (rel, action) in enumerate(plan.work, 1):
            progress(i, len(plan.work), 'writing')
            src, bak = os.path.join(plan.target, rel), os.path.join(run_dir, 'files', rel)
            m.write(op='moved', rel=rel, sha=sha256(src))   # before the move, so a crash in between is still undone
            move_file(src, bak)
            if action == 'strip':
                pkg = Package(bak)
                w = Writer(src)
                try:
                    with pkg.open() as f:
                        for e in pkg.entries:
                            if e[0] != STBL:
                                w.add(tgi(e), read_raw(f, e), e[5], e[6], e[7], e[8])
                    w.close()
                except BaseException:
                    w.abort(); raise
                m.write(op='written', rel=rel, sha=sha256(src))
        parts = []
        if plan.mode == 'merge':
            parts = write_merged(plan, run_dir, m)
        spath = os.path.join(plan.target, STRINGS_NAME)
        if os.path.exists(spath):
            raise PackageError('%s is still in the folder' % STRINGS_NAME)
        m.write(op='creating', rel=STRINGS_NAME)
        plan.pool.write(spath)
        m.write(op='created', rel=STRINGS_NAME, sha=sha256(spath))
        print('  checking every resource and string against the backup ...')
        verify(plan, run_dir, parts)
        m.write(op='done')
        return run_dir
    except BaseException as ex:
        print('\nSomething went wrong: %s\nPutting everything back ...' % ex)
        m.write(op='failed', error=str(ex))
        undo(run_dir, rollback=True)
        raise


def write_merged(plan, run_dir, m, limit=None):
    limit = limit or plan.part_limit
    seen, parts, w = set(), [], None
    try:
        for rel, _ in plan.work:
            pkg = Package(os.path.join(run_dir, 'files', rel))
            with pkg.open() as f:
                for e in pkg.entries:
                    if e[0] == STBL or tgi(e) in seen:
                        continue
                    size = e[5] & 0x7FFFFFFF
                    if w is None or (w.tell() + size + 32 * (len(w.ents) + 1) > limit and w.ents):
                        if w is not None:
                            w.close(); m.write(op='created', rel=parts[-1], sha=sha256(w.path))
                        name = '%s%03d.package' % (MERGED_PREFIX, len(parts) + 1)
                        path = os.path.join(plan.target, name)
                        if os.path.exists(path):
                            raise PackageError('%s is still in the folder' % name)
                        m.write(op='creating', rel=name)
                        w = Writer(path); parts.append(name)
                    seen.add(tgi(e))
                    w.add(tgi(e), read_raw(f, e), e[5], e[6], e[7], e[8])
        if w is not None:
            w.close(); m.write(op='created', rel=parts[-1], sha=sha256(w.path))
        return parts
    except BaseException:
        if w is not None and not w.f.closed:
            w.abort()
        raise


def verify(plan, run_dir, parts):
    sp = Package(os.path.join(plan.target, STRINGS_NAME))
    with sp.open() as f:
        got = {}
        for lang, ver, ent in read_strings(sp, f):
            if lang in got:
                raise PackageError('two string tables for language %02X' % lang)
            got[lang] = dict(ent)
    if got != plan.pool.tables:
        raise PackageError('strings package does not match the planned strings')
    merged = {}
    for name in parts:
        pk = Package(os.path.join(plan.target, name))
        for e in pk.entries:
            merged[tgi(e)] = (pk, e)
    winners = set()
    for rel, action in plan.work:
        orig = Package(os.path.join(run_dir, 'files', rel))
        with orig.open() as f:
            for lang, ver, ent in read_strings(orig, f):
                t = got.get(lang, {})
                for k, v in ent:
                    if k not in t:
                        raise PackageError('%s: string %016X missing' % (rel, k))
            others = [e for e in orig.entries if e[0] != STBL]
            if plan.mode == 'merge':
                for e in others:
                    k = tgi(e)
                    if k not in merged:
                        raise PackageError('%s: resource %08X:%08X:%08X%08X missing' % ((rel,) + k))
                    if k in winners:
                        continue
                    winners.add(k)
                    pk, me = merged[k]
                    with pk.open() as g:
                        if read_raw(g, me) != read_raw(f, e) or me[5:9] != e[5:9]:
                            raise PackageError('%s: resource %08X:%08X:%08X%08X differs' % ((rel,) + k))
            elif action == 'strip':
                new = Package(os.path.join(plan.target, rel))
                if [tgi(e) + e[5:9] for e in new.entries] != [tgi(e) + e[5:9] for e in others]:
                    raise PackageError('%s: index differs' % rel)
                with new.open() as g:
                    for a, b in zip(new.entries, others):
                        if read_raw(g, a) != read_raw(f, b):
                            raise PackageError('%s: resource differs' % rel)
            elif rel != STRINGS_NAME and os.path.exists(os.path.join(plan.target, rel)):
                raise PackageError('%s: still in the folder' % rel)
    if plan.mode == 'merge' and set(merged) != winners:
        raise PackageError('merged packages hold resources from nowhere')


def find_runs(backup_root):
    runs = []
    if os.path.isdir(backup_root):
        for d in sorted(os.listdir(backup_root)):
            rd = os.path.join(backup_root, d)
            if os.path.isfile(os.path.join(rd, MANIFEST)):
                recs = read_manifest(rd)
                runs.append((rd, recs))
    return runs


def undo(run_dir, force=False, rollback=False):
    """Checks everything first and changes nothing if a file was edited or replaced since the run (unless force,
    which moves such files to <run>/changed-since instead). rollback: undo a run that just failed, our own files only."""
    run_dir = os.path.abspath(run_dir)
    recs = read_manifest(run_dir)
    target = recs[0]['target']
    if any(r['op'] == 'undone' for r in recs):
        print('This run was already undone.'); return False
    if not rollback:
        for rd, rr in find_runs(os.path.dirname(run_dir)):
            if rd > run_dir and rr[0]['target'] == target and any(r['op'] == 'done' for r in rr) \
                    and not any(r['op'] == 'undone' for r in rr):
                print('A later run (%s) on the same folder has to be undone first.' % os.path.basename(rd))
                return False
    written = {r['rel']: r['sha'] for r in recs if r['op'] == 'written'}
    created = {r['rel']: r.get('sha') for r in recs if r['op'] in ('creating', 'created') and
               (r['op'] == 'created' or r['rel'] not in [x['rel'] for x in recs if x['op'] == 'created'])}
    remove, aside, restore, blocking, notes = [], [], [], [], []
    for rel, sha in created.items():
        r = {'rel': rel}
        p = os.path.join(target, rel)
        if os.path.exists(p):
            if rollback or sha256(p) == sha:
                remove.append(p)
            else:
                blocking.append('%s changed since the run' % r['rel']); aside.append((p, r['rel']))
    for r in recs:
        if r['op'] != 'moved':
            continue
        rel = r['rel']
        bak, dst = os.path.join(run_dir, 'files', rel), os.path.join(target, rel)
        if not os.path.exists(bak):
            if not os.path.exists(dst):
                notes.append('%s: neither the file nor its backup is there' % rel)
            continue                                    # never moved (stopped right after the manifest line)
        if os.path.exists(dst):
            ours = written.get(rel) or created.get(rel)          # rewritten, or recreated under the same name
            if rollback or (ours and sha256(dst) == ours):
                remove.append(dst)
            else:
                blocking.append('%s changed or was replaced since the run' % rel); aside.append((dst, rel))
        elif rel in written and not rollback:
            notes.append('%s was deleted since the run, not brought back (its backup stays in %s)' % (rel, run_dir))
            continue
        restore.append((bak, dst))
    if blocking and not (force or rollback):
        for p in blocking:
            print('  - ' + p)
        print('Nothing changed. Run again with --force to move those files to %s and undo anyway.'
              % os.path.join(run_dir, 'changed-since'))
        return False
    for p, rel in aside:
        move_file(p, os.path.join(run_dir, 'changed-since', rel))
    for p in dict.fromkeys(remove):
        if os.path.exists(p):
            os.remove(p)
    for p in [os.path.join(target, r['rel']) + '.part' for r in recs if r['op'] in ('moved', 'creating')]:
        if os.path.exists(p):
            os.remove(p)
    for bak, dst in restore:
        move_file(bak, dst)
    Manifest(os.path.join(run_dir, MANIFEST)).write(op='undone', notes=notes, aside=[rel for _, rel in aside])
    for p in notes + ['%s moved to changed-since' % rel for _, rel in aside]:
        print('  - ' + p)
    print('Undone.')
    return True


# ---------------------------------------------------------------- console

def documents_folder():
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return buf.value
    except (AttributeError, OSError):
        pass
    return os.path.join(os.path.expanduser('~'), 'Documents')


def default_target():
    return os.path.join(documents_folder(), 'Electronic Arts', 'The Sims 3', 'Mods', 'Packages')


def game_running():
    if os.name != 'nt':
        return False
    try:
        out = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.lower()
    except OSError:
        return False
    return any('"%s"' % exe in out for exe in GAME_EXES)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ccstringmerge', description='Faster Sim menus in The Sims 3 with lots of CC.')
    ap.add_argument('folder', nargs='?', help='Mods\\Packages folder (default: the one in Documents), '
                                              'or a backup run folder to undo it')
    ap.add_argument('--merge', action='store_true', help='also merge all CC packages into a few big ones')
    ap.add_argument('--part-size', type=int, default=1024, metavar='MB', help='size of each merged package (default 1024)')
    ap.add_argument('--dry-run', action='store_true', help='only show what would change')
    ap.add_argument('--undo', metavar='RUN_FOLDER', help='undo one run, given its backup folder')
    ap.add_argument('--force', action='store_true', help='undo even if files changed since the run '
                                                         '(they are moved aside, not deleted)')
    ap.add_argument('--yes', action='store_true', help='do not ask, do not wait for Enter at the end')
    ap.add_argument('--version', action='version', version=VERSION)
    a = ap.parse_args(argv)
    pause = not a.yes and sys.stdin is not None and sys.stdin.isatty()
    try:
        return run(a)
    except KeyboardInterrupt:
        print('\nStopped.'); return 1
    finally:
        if pause:
            input('\nPress Enter to close.')


def run(a):
    print('CC String Merge %s\n' % VERSION)
    folder = a.undo or a.folder
    if folder and os.path.isfile(os.path.join(folder, MANIFEST)):
        if game_running():
            print('The Sims 3 is running. Close the game first.'); return 1
        print('Undoing the run in %s' % folder)
        if not a.yes and input('Type Y and press Enter to undo: ').strip().lower() != 'y':
            print('Nothing changed.'); return 0
        return 0 if undo(folder, force=a.force) else 1
    if a.undo:
        print('%s is not a backup run folder (no %s in it).' % (a.undo, MANIFEST)); return 1
    target = os.path.abspath(folder or default_target())
    if not os.path.isdir(target):
        print('Folder not found: %s\nDrop your Mods\\Packages folder onto the program instead.' % target); return 1
    if game_running() and not a.dry_run:
        print('The Sims 3 is running. Close the game first.'); return 1
    backup_root = backup_root_for(target)
    mode = 'merge' if a.merge else 'strings'
    print('Reading %s ...' % target)
    plan = make_plan(target, mode, backup_root)
    plan.part_limit = max(16, a.part_size) << 20
    print('\n' + plan_report(plan) + '\n')
    if not plan.work:
        print('Nothing to do.'); return 0
    if mode == 'strings' and len(plan.work) == 1 and plan.work[0][0] == STRINGS_NAME:
        print('Already merged, nothing new.'); return 0
    if a.dry_run:
        print('Dry run: nothing changed.'); return 0
    print('Originals go to: %s' % backup_root)
    if not a.yes and input('Type Y and press Enter to go ahead: ').strip().lower() != 'y':
        print('Nothing changed.'); return 0
    run_dir = apply(plan, backup_root)
    print('\nDone. Backup and report: %s\nTo undo, drop that folder onto the program.' % run_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
