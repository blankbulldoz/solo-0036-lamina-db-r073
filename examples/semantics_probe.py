#!/usr/bin/env python3
"""Teaching probe for LaminaDB query semantics.

Companion to docs/query-semantics-guide.md (core group) and
docs/query-semantics-extended.md (extended group).

The probe plays participant request sequences against a real temporary
instance and compares observed answers with answers computed independently
from the recorded input history. The Model below only sees successful
participant requests (DDL, commits, vacuums); it never reads engine state,
so agreement between "expect" and "observed" is evidence that the
documented rules let a reader derive null, tie-order and count/sum answers
from history alone. SQL and structured queries are treated as two
observation entries over the same history; neither is used as the arbiter
for the other.

This is a bounded teaching demonstration, not a baseline test suite.
"""
import argparse, functools, json, pathlib, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error

RELATION = 'spectra'
PK = 'sample'
SCHEMA = {'name': 'spectra', 'primary': 'sample', 'columns': [
    {'name': 'sample', 'type': 'int'},
    {'name': 'band', 'type': 'text'},
    {'name': 'amplitude', 'type': 'int', 'nullable': True},
    {'name': 'valid', 'type': 'bool'}]}
TS = object()  # tombstone marker inside the model
INT64_MAX, INT64_MIN = 2**63 - 1, -2**63


class SnapshotGone(Exception):
    def __init__(self, horizon, sequence):
        self.horizon, self.sequence = horizon, sequence


class Overflow(Exception):
    pass


class Model:
    """Independent semantics computed only from the recorded request history."""

    def __init__(self):
        self.seq = 0
        self.horizon = 0
        self.chains = {}  # key -> list of (sequence, row | TS)

    def record(self):
        """A journaled non-commit record (relation/index) advances the sequence."""
        self.seq += 1

    def commit(self, writes):
        self.seq += 1
        for key, row in writes:
            self.chains.setdefault(key, []).append((self.seq, row))

    def vacuum(self, requested, active_snapshots=()):
        horizon = min([requested, *active_snapshots])
        self.horizon = horizon
        self.seq += 1  # vacuum is itself a journaled record
        removed = retained = 0
        for key in list(self.chains):
            chain = self.chains[key]
            keep = 0
            while keep + 1 < len(chain) and chain[keep + 1][0] <= horizon:
                keep += 1
            if chain and chain[-1][0] <= horizon and chain[-1][1] is TS:
                removed += len(chain)
                del self.chains[key]
                continue
            removed += keep
            self.chains[key] = chain[keep:]
            retained += len(chain) - keep
        return {'horizon': horizon, 'removed': removed,
                'retained': retained, 'pinned': horizon < requested}

    def visible(self, at):
        out = {}
        for key, chain in self.chains.items():
            current = None
            for seq, row in chain:
                if seq <= at:
                    current = row
                else:
                    break
            if current is not None and current is not TS:
                out[key] = current
        return out

    def query(self, at, where=(), order=(), limit=0, aggregate=None, overlay=None, columns=()):
        if at < self.horizon or at > self.seq:
            raise SnapshotGone(self.horizon, self.seq)
        rows = dict(self.visible(at))
        for key, row in (overlay or {}).items():
            if row is TS:
                rows.pop(key, None)
            else:
                rows[key] = row
        selected = [row for _, row in sorted(rows.items()) if matches(row, where)]
        matched = len(selected)
        if aggregate:
            return {'matched': matched, 'rows': [reduce(selected, aggregate)]}
        selected.sort(key=functools.cmp_to_key(lambda a, b: cmp_rows(a, b, order)))
        truncated = False
        effective = limit or 1000
        if len(selected) > effective:
            selected = selected[:effective]
            truncated = True
        if columns:
            selected = [{c: row[c] for c in columns} for row in selected]
        return {'matched': matched, 'rows': selected, 'truncated': truncated}


def cmp_val(x, y):
    if x is None and y is None:
        return 0
    if x is None:
        return -1  # null sorts before any value; DESC flips the comparison
    if y is None:
        return 1
    return (x > y) - (x < y)


def cmp_rows(a, b, order):
    for col, desc in order:
        n = cmp_val(a[col], b[col])
        if n:
            return -n if desc else n
    return cmp_val(a[PK], b[PK])  # ties always break by ascending primary key


def matches(row, where):
    for col, op, value in where:
        v = row[col]
        if op == 'is_null':
            if v is not None:
                return False
            continue
        if op == 'not_null':
            if v is None:
                return False
            continue
        if v is None or value is None:
            return False  # null never matches an ordinary comparison, even !=
        n = (v > value) - (v < value)
        if not {'=': n == 0, '!=': n != 0, '<': n < 0, '<=': n <= 0,
                '>': n > 0, '>=': n >= 0}[op]:
            return False
    return True


def reduce(rows, aggregate):
    fn, col = aggregate
    total = count = 0
    for row in rows:
        v = row.get(col) if col not in ('', '*') else None
        if col not in ('', '*') and v is None:
            continue  # count(column) and sum skip nulls
        count += 1
        if fn == 'sum':
            if (v > 0 and total > INT64_MAX - v) or (v < 0 and total < INT64_MIN - v):
                raise Overflow()
            total += v
    if fn == 'count':
        return {'count': count}
    return {'sum': total if count else None}  # sum of empty input is null


def norm(row):
    """Full-row replacement: a missing nullable column becomes null."""
    return {'sample': row['sample'], 'band': row['band'],
            'amplitude': row.get('amplitude'), 'valid': row['valid']}


def subset(expect, observed):
    if isinstance(expect, dict):
        return isinstance(observed, dict) and all(
            k in observed and subset(v, observed[k]) for k, v in expect.items())
    return expect == observed


class Recorder:
    def __init__(self):
        self.records = []
        self.failed = 0

    def check(self, cid, request, expect, observed):
        ok = subset(expect, observed)
        self.records.append({'id': cid, 'request': request,
                             'expect': expect, 'observed': observed, 'ok': ok})
        if not ok:
            self.failed += 1


class Session:
    def __init__(self, binary, root, restore=None):
        self.binary = str(binary)
        self.root = pathlib.Path(root)
        self.restore = str(restore) if restore else None
        self.process = None
        self.log = None
        self.labels = {}

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/v1/lamina'
        args = [self.binary, '-data', str(self.root), '-listen', f'127.0.0.1:{port}']
        if self.restore:
            args += ['-restore', self.restore]
            self.restore = None
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(args, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(self.log.read().decode())
            try:
                self.call('GET', '/health')
                return self
            except (OSError, urllib.error.URLError):
                time.sleep(.03)
        raise RuntimeError('service did not become ready')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
            self.log = None

    def call(self, method, path, data=None, status=200, raw=False):
        encoded = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url + path, data=encoded, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                code, body = response.status, response.read()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()
        if code != status:
            raise RuntimeError(f'{method} {path}: expected {status}, got {code}: {body.decode()}')
        return body if raw else json.loads(body)

    # Participant vocabulary, labelled so emitted requests stay readable.
    def begin(self, label, readonly=False):
        out = self.call('POST', '/transactions', {'read_only': readonly}, 201)
        self.labels[out['id']] = label
        return out['id'], out['snapshot']

    def put(self, tx, row, insert_only=False):
        return self.call('POST', f'/transactions/{tx}/put',
                         {'relation': RELATION, 'row': row, 'insert_only': insert_only})

    def delete(self, tx, key):
        return self.call('POST', f'/transactions/{tx}/delete',
                         {'relation': RELATION, 'key': key})

    def commit(self, tx, status=200):
        return self.call('POST', f'/transactions/{tx}/commit', status=status)

    def rollback(self, tx):
        return self.call('POST', f'/transactions/{tx}/rollback')

    def show(self, text):
        for tx, label in self.labels.items():
            text = text.replace(tx, f'<{label}>')
        return text

    def desc(self, method, path, body=None):
        text = method + ' ' + path
        if body is not None:
            text += ' ' + json.dumps(body, ensure_ascii=False)
        return self.show(text)


def observe(resp):
    return {'rows': resp['rows'], 'matched': resp['matched'], 'scanned': resp['scanned'],
            'truncated': resp['truncated'],
            'plan': {'access': resp['plan']['access'], 'candidates': resp['plan']['candidates']}}


def qcheck(rec, cid, s, m, body, expect_extra=None, sql=None, overlay=None, at=None):
    """Run one structured or SQL query and compare with the model's answer."""
    if sql is not None:
        http_body = {'sql': sql}
        for k in ('transaction', 'at'):
            if k in body:
                http_body[k] = body[k]
        request = s.desc('POST', '/sql', http_body)
    else:
        http_body = {'relation': RELATION, **body}
        request = s.desc('POST', '/query', http_body)
    # The model always evaluates the structured form; for SQL checks the caller
    # supplies the equivalent structured body so the expectation is still
    # derived from history, not from the SQL text or the engine.
    where = tuple((p['column'], p['op'], p.get('value')) for p in body.get('where', ()))
    order = tuple((o['column'], bool(o.get('desc'))) for o in body.get('order', ()))
    limit = body.get('limit', 0)
    aggregate = None
    if 'aggregate' in body:
        aggregate = (body['aggregate']['function'], body['aggregate'].get('column', '*'))
    columns = tuple(body.get('columns', ()))
    snapshot = at
    if snapshot is None:
        snapshot = body.get('at')
    if snapshot is None:
        snapshot = body.get('_snapshot', m.seq)
    try:
        expected = m.query(snapshot, where=where, order=order, limit=limit,
                           aggregate=aggregate, overlay=overlay, columns=columns)
        resp = s.call('POST', '/sql' if sql is not None else '/query', http_body)
        expect = {'rows': expected['rows'], 'matched': expected['matched']}
        if expect_extra:
            expect.update(expect_extra)
        rec.check(cid, request, expect, observe(resp))
    except SnapshotGone as gone:
        resp = s.call('POST', '/sql' if sql is not None else '/query', http_body, status=410)
        rec.check(cid, request, {'status': 410, 'code': 'snapshot'},
                  {'status': 410, 'code': resp['error']['code'], 'message': resp['error']['message']})
    except Overflow:
        resp = s.call('POST', '/sql' if sql is not None else '/query', http_body, status=400)
        rec.check(cid, request, {'status': 400, 'code': 'invalid'},
                  {'status': 400, 'code': resp['error']['code'], 'message': resp['error']['message']})


def echeck(rec, cid, s, method, path, body, status, code):
    resp = s.call(method, path, body, status)
    rec.check(cid, s.desc(method, path, body), {'status': status, 'code': code},
              {'status': status, 'code': resp['error']['code'], 'message': resp['error']['message']})


def seed_rows():
    amps = [40, 20, 40, None, 60, 40, None]
    return [{'sample': n, 'band': f'b{n}', 'amplitude': amps[n - 1],
             'valid': n != 7} for n in range(1, 8)]


def core(s, rec):
    m = Model()
    s.call('POST', '/relations', SCHEMA, 201)
    m.record()  # seq 1: relation
    tx, _ = s.begin('S')
    for row in seed_rows():
        s.put(tx, row)
    s.commit(tx)
    m.commit([(r['sample'], r) for r in seed_rows()])  # seq 2: seed

    # A: null matching rules
    qcheck(rec, 'A1', s, m, {'where': [{'column': 'amplitude', 'op': '!=', 'value': 20}],
                             'columns': ['sample', 'amplitude']})
    qcheck(rec, 'A2', s, m, {'where': [{'column': 'amplitude', 'op': '=', 'value': None}]})
    qcheck(rec, 'A2-sql', s, m, {'where': [{'column': 'amplitude', 'op': '=', 'value': None}]},
           sql='SELECT * FROM spectra WHERE amplitude = null;')
    qcheck(rec, 'A3', s, m, {'where': [{'column': 'amplitude', 'op': 'is_null'}],
                             'columns': ['sample', 'amplitude']})
    qcheck(rec, 'A4-count-star', s, m, {'aggregate': {'function': 'count', 'column': '*'}})
    qcheck(rec, 'A4-count-col', s, m, {'aggregate': {'function': 'count', 'column': 'amplitude'}})
    qcheck(rec, 'A4-sum', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'A4-sum-sql', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           sql='SELECT sum(amplitude) FROM spectra;')
    qcheck(rec, 'A5-count-star', s, m, {'where': [{'column': 'amplitude', 'op': 'is_null'}],
                                        'aggregate': {'function': 'count', 'column': '*'}})
    qcheck(rec, 'A5-count-col', s, m, {'where': [{'column': 'amplitude', 'op': 'is_null'}],
                                       'aggregate': {'function': 'count', 'column': 'amplitude'}})
    qcheck(rec, 'A5-sum', s, m, {'where': [{'column': 'amplitude', 'op': 'is_null'}],
                                 'aggregate': {'function': 'sum', 'column': 'amplitude'}})

    # B: ordering with ties and nulls
    qcheck(rec, 'B1', s, m, {'order': [{'column': 'amplitude'}],
                             'columns': ['sample', 'amplitude']})
    qcheck(rec, 'B2', s, m, {'order': [{'column': 'amplitude', 'desc': True}],
                             'columns': ['sample', 'amplitude']})
    qcheck(rec, 'B3', s, m, {'columns': ['sample']})
    qcheck(rec, 'B4', s, m, {'order': [{'column': 'amplitude', 'desc': True}], 'limit': 2,
                             'columns': ['sample', 'amplitude']},
           expect_extra={'truncated': True})

    # C: staged replacement vs other observers
    r_tx, r_snap = s.begin('R')
    w_tx, _ = s.begin('W')
    staged = {}
    s.put(w_tx, {'sample': 1, 'band': 'b1x', 'valid': True})
    staged[1] = norm({'sample': 1, 'band': 'b1x', 'valid': True})  # amplitude -> null
    s.delete(w_tx, 2)
    staged[2] = TS
    s.put(w_tx, {'sample': 8, 'band': 'b8', 'amplitude': 80, 'valid': True})
    staged[8] = norm({'sample': 8, 'band': 'b8', 'amplitude': 80, 'valid': True})
    qcheck(rec, 'C1', s, m, {'transaction': w_tx, 'order': [{'column': 'sample'}],
                             'columns': ['sample', 'amplitude']},
           overlay=staged, at=r_snap, expect_extra={'plan': {'access': 'scan'}})
    qcheck(rec, 'C1-sum', s, m, {'transaction': w_tx,
                                 'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           overlay=staged, at=r_snap)
    qcheck(rec, 'C2', s, m, {'transaction': r_tx,
                             'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           at=r_snap)
    qcheck(rec, 'C3', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    committed = s.commit(w_tx)
    rec.check('C4-commit', s.desc('POST', f'/transactions/{w_tx}/commit'),
              {'sequence': 3, 'writes': 3}, committed)
    m.commit([(k, v) for k, v in staged.items()])  # seq 3
    qcheck(rec, 'C4', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'C4-count-col', s, m, {'aggregate': {'function': 'count', 'column': 'amplitude'}})
    qcheck(rec, 'C5', s, m, {'transaction': r_tx,
                             'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           at=r_snap)
    qcheck(rec, 'C6', s, m, {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'C6-sql', s, m, {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           sql='SELECT sum(amplitude) FROM spectra;')
    qcheck(rec, 'C7-sql', s, m, {'transaction': r_tx,
                                 'aggregate': {'function': 'count', 'column': '*'}},
           sql='SELECT count(*) FROM spectra;', at=r_snap)
    s.rollback(r_tx)

    # D: aggregate boundaries
    x_tx, _ = s.begin('X')
    s.put(x_tx, {'sample': 9, 'band': 'b9', 'amplitude': INT64_MAX, 'valid': True})
    s.put(x_tx, {'sample': 10, 'band': 'b10', 'amplitude': 1, 'valid': True})
    s.commit(x_tx)
    m.commit([(9, norm({'sample': 9, 'band': 'b9', 'amplitude': INT64_MAX, 'valid': True})),
              (10, norm({'sample': 10, 'band': 'b10', 'amplitude': 1, 'valid': True}))])  # seq 4
    qcheck(rec, 'D1', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'D2', s, m, {'aggregate': {'function': 'count', 'column': '*'}, 'limit': 1})
    qcheck(rec, 'D2-sql', s, m, {'aggregate': {'function': 'count', 'column': '*'}, 'limit': 1},
           sql='SELECT count(*) FROM spectra LIMIT 1;')
    y_tx, _ = s.begin('Y')
    s.delete(y_tx, 9)
    s.delete(y_tx, 10)
    s.commit(y_tx)
    m.commit([(9, TS), (10, TS)])  # seq 5
    qcheck(rec, 'D3', s, m, {'aggregate': {'function': 'count', 'column': '*'}})

    # E: vacuum horizon decides which snapshots remain computable
    p_tx, p_snap = s.begin('P')
    z_tx, _ = s.begin('Z')
    s.put(z_tx, {'sample': 3, 'band': 'b3', 'amplitude': 44, 'valid': True})
    s.commit(z_tx)
    m.commit([(3, norm({'sample': 3, 'band': 'b3', 'amplitude': 44, 'valid': True}))])  # seq 6
    qcheck(rec, 'E0-reader', s, m, {'transaction': p_tx,
                                    'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           at=p_snap)
    qcheck(rec, 'E0-current', s, m, {'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    expected_vac = m.vacuum(6, active_snapshots=[p_snap])  # seq 7
    observed_vac = s.call('POST', '/vacuum', {'horizon': 6})
    rec.check('E1', s.desc('POST', '/vacuum', {'horizon': 6}), expected_vac, observed_vac)
    qcheck(rec, 'E2', s, m, {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'E2-sql', s, m, {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}},
           sql='SELECT sum(amplitude) FROM spectra;')
    qcheck(rec, 'E2-boundary', s, m, {'at': 5,
                                      'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    s.rollback(p_tx)
    expected_vac = m.vacuum(6)  # seq 8
    observed_vac = s.call('POST', '/vacuum', {'horizon': 6})
    rec.check('E3', s.desc('POST', '/vacuum', {'horizon': 6}), expected_vac, observed_vac)
    qcheck(rec, 'E4-old-boundary', s, m, {'at': 5,
                                          'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'E4-new-boundary', s, m, {'at': 6,
                                          'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    echeck(rec, 'E5-ahead', s, 'POST', '/vacuum', {'horizon': 9}, 400, 'invalid')
    echeck(rec, 'E5-retreat', s, 'POST', '/vacuum', {'horizon': 5}, 400, 'invalid')
    stats = s.call('GET', '/stats')
    rec.check('E6-stats', s.desc('GET', '/stats'),
              {'sequence': m.seq, 'horizon': m.horizon},
              {'sequence': stats['sequence'], 'horizon': stats['horizon']})


def extended(s, rec, temp, binary):
    m = Model()
    s.call('POST', '/relations', SCHEMA, 201)
    m.record()  # seq 1
    tx, _ = s.begin('S')
    for n in range(1, 6):
        s.put(tx, {'sample': n, 'band': f'b{n}', 'amplitude': n * 10, 'valid': True})
    s.commit(tx)
    m.commit([(n, norm({'sample': n, 'band': f'b{n}', 'amplitude': n * 10, 'valid': True}))
              for n in range(1, 6)])  # seq 2
    s.call('POST', '/indexes', {'name': 'by_amplitude', 'relation': RELATION,
                                'column': 'amplitude', 'unique': False}, 201)
    m.record()  # seq 3
    u_tx, _ = s.begin('U')
    s.put(u_tx, {'sample': 2, 'band': 'b2', 'amplitude': 25, 'valid': True})
    s.commit(u_tx)
    m.commit([(2, norm({'sample': 2, 'band': 'b2', 'amplitude': 25, 'valid': True}))])  # seq 4

    # X1: index keeps historical candidates; rows always come from the snapshot
    qcheck(rec, 'X1', s, m, {'where': [{'column': 'amplitude', 'op': '=', 'value': 20}]},
           expect_extra={'scanned': 1, 'plan': {'access': 'index', 'candidates': 1}})
    qcheck(rec, 'X1-scan', s, m,
           {'where': [{'column': 'amplitude', 'op': '=', 'value': 20}], 'force_scan': True},
           expect_extra={'scanned': 5, 'plan': {'access': 'scan', 'candidates': 5}})

    # X2: a transaction with staged writes on the relation always scans
    t_tx, t_snap = s.begin('T')
    s.put(t_tx, {'sample': 3, 'band': 'b3', 'amplitude': 35, 'valid': True})
    staged = {3: norm({'sample': 3, 'band': 'b3', 'amplitude': 35, 'valid': True})}
    qcheck(rec, 'X2-tx', s, m, {'transaction': t_tx,
                                'where': [{'column': 'amplitude', 'op': '>=', 'value': 30}],
                                'columns': ['sample', 'amplitude']},
           overlay=staged, at=t_snap, expect_extra={'plan': {'access': 'scan'}})
    qcheck(rec, 'X2-no-tx', s, m, {'where': [{'column': 'amplitude', 'op': '>=', 'value': 30}],
                                   'columns': ['sample', 'amplitude']},
           expect_extra={'plan': {'access': 'index'}})
    s.rollback(t_tx)

    # X3: first committer wins; the loser publishes nothing and is closed
    a_tx, _ = s.begin('A')
    b_tx, _ = s.begin('B')
    s.put(a_tx, {'sample': 1, 'band': 'b1', 'amplitude': 11, 'valid': True})
    s.put(b_tx, {'sample': 1, 'band': 'b1', 'amplitude': 12, 'valid': True})
    s.put(b_tx, {'sample': 6, 'band': 'b6', 'amplitude': 60, 'valid': True})
    s.commit(a_tx)
    m.commit([(1, norm({'sample': 1, 'band': 'b1', 'amplitude': 11, 'valid': True}))])  # seq 5
    echeck(rec, 'X3-conflict', s, 'POST', f'/transactions/{b_tx}/commit', None, 409, 'conflict')
    echeck(rec, 'X3-closed', s, 'POST', f'/transactions/{b_tx}/commit', None, 404, 'not_found')
    qcheck(rec, 'X3', s, m, {'columns': ['sample', 'amplitude']})

    # X4: an empty commit publishes no record and does not advance the sequence
    before = s.call('GET', '/stats')
    e_tx, e_snap = s.begin('E')
    result = s.commit(e_tx)
    after = s.call('GET', '/stats')
    rec.check('X4', s.desc('POST', f'/transactions/{e_tx}/commit'),
              {'commit': {'sequence': e_snap, 'writes': 0},
               'sequence_before': m.seq, 'sequence_after': m.seq},
              {'commit': result, 'sequence_before': before['sequence'],
               'sequence_after': after['sequence'],
               'journal_records_after': after['journal_records']})

    # X5: deleting a missing key still stages a tombstone chain
    t2_tx, _ = s.begin('D')
    s.delete(t2_tx, 99)
    s.commit(t2_tx)
    m.commit([(99, TS)])  # seq 6
    stats = s.call('GET', '/stats')
    rec.check('X5-stats', s.desc('GET', '/stats'),
              {'keys': len(m.chains), 'versions': sum(len(c) for c in m.chains.values())},
              {'keys': stats['keys'], 'versions': stats['versions']})
    echeck(rec, 'X5-get', s, 'POST', '/get',
           {'relation': RELATION, 'key': 99, 'transaction': ''}, 404, 'not_found')
    qcheck(rec, 'X5', s, m, {'aggregate': {'function': 'count', 'column': '*'}})

    # X6: unique conflicts surface at commit, not at put
    s.call('POST', '/indexes', {'name': 'uq_band', 'relation': RELATION,
                                'column': 'band', 'unique': True}, 201)
    m.record()  # seq 7
    u1_tx, _ = s.begin('U1')
    u2_tx, _ = s.begin('U2')
    s.put(u1_tx, {'sample': 6, 'band': 'shared', 'amplitude': 60, 'valid': True})
    s.put(u2_tx, {'sample': 7, 'band': 'shared', 'amplitude': 70, 'valid': True})
    s.commit(u1_tx)
    m.commit([(6, norm({'sample': 6, 'band': 'shared', 'amplitude': 60, 'valid': True}))])  # seq 8
    echeck(rec, 'X6', s, 'POST', f'/transactions/{u2_tx}/commit', None, 409, 'conflict')

    # X7: a unique index admits any number of nulls
    s.call('POST', '/indexes', {'name': 'uq_amp', 'relation': RELATION,
                                'column': 'amplitude', 'unique': True}, 201)
    m.record()  # seq 9
    n_tx, _ = s.begin('N')
    s.put(n_tx, {'sample': 8, 'band': 'b8', 'valid': True})
    s.put(n_tx, {'sample': 9, 'band': 'b9', 'valid': True})
    s.commit(n_tx)
    m.commit([(8, norm({'sample': 8, 'band': 'b8', 'valid': True})),
              (9, norm({'sample': 9, 'band': 'b9', 'valid': True}))])  # seq 10
    qcheck(rec, 'X7', s, m, {'where': [{'column': 'amplitude', 'op': 'is_null'}],
                             'aggregate': {'function': 'count', 'column': '*'}})

    # X8..X12: SQL surface maps onto the same request semantics
    qcheck(rec, 'X8', s, m, {'order': [{'column': 'amplitude', 'desc': True}], 'limit': 2,
                             'columns': ['sample', 'amplitude']},
           sql='SELECT SAMPLE, AMPLITUDE FROM spectra WHERE VALID = true '
               'ORDER BY AMPLITUDE DESC LIMIT 2;',
           expect_extra={'truncated': True})
    echeck(rec, 'X9', s, 'POST', '/sql',
           {'sql': 'SELECT * FROM spectra; SELECT * FROM spectra;'}, 400, 'invalid')
    echeck(rec, 'X10-sql', s, 'POST', '/sql',
           {'sql': "SELECT * FROM spectra WHERE amplitude = '20';"}, 400, 'invalid')
    echeck(rec, 'X10-structured', s, 'POST', '/query',
           {'relation': RELATION, 'where': [{'column': 'amplitude', 'op': '=', 'value': '20'}]},
           400, 'invalid')
    echeck(rec, 'X11', s, 'POST', '/sql', {'sql': 'SELECT sum(band) FROM spectra;'}, 400, 'invalid')
    echeck(rec, 'X12', s, 'POST', '/sql', {'sql': 'SELECT * FROM spectra LIMIT 0;'}, 400, 'invalid')

    # X13/X14: snapshot selection boundaries
    q_tx, _ = s.begin('Q')
    echeck(rec, 'X13', s, 'POST', '/query',
           {'relation': RELATION, 'at': 4, 'transaction': q_tx}, 400, 'invalid')
    s.rollback(q_tx)
    echeck(rec, 'X14', s, 'POST', '/query', {'relation': RELATION, 'at': 0}, 410, 'snapshot')

    # X7b: default order follows the typed primary key, not insertion order
    w2_tx, _ = s.begin('W2')
    s.put(w2_tx, {'sample': 7, 'band': 'b7', 'amplitude': 70, 'valid': True})
    s.commit(w2_tx)
    m.commit([(7, norm({'sample': 7, 'band': 'b7', 'amplitude': 70, 'valid': True}))])  # seq 11
    qcheck(rec, 'X7b', s, m, {'columns': ['sample']})

    # X15/X16: the computable range travels with retained history, not with the instance
    backup = s.call('GET', '/backup', raw=True)
    image = pathlib.Path(temp) / 'frozen.lmi'
    image.write_bytes(backup)
    expected_vac = m.vacuum(8)  # seq 11
    observed_vac = s.call('POST', '/vacuum', {'horizon': 8})
    rec.check('X15-vacuum', s.desc('POST', '/vacuum', {'horizon': 8}), expected_vac, observed_vac)
    qcheck(rec, 'X15-original', s, m, {'at': 2,
                                       'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    restored = Session(binary, pathlib.Path(temp) / 'restored', image).start()
    try:
        m2 = Model()
        m2.record()
        m2.commit([(n, norm({'sample': n, 'band': f'b{n}', 'amplitude': n * 10, 'valid': True}))
                   for n in range(1, 6)])
        m2.record()
        m2.commit([(2, norm({'sample': 2, 'band': 'b2', 'amplitude': 25, 'valid': True}))])
        m2.commit([(1, norm({'sample': 1, 'band': 'b1', 'amplitude': 11, 'valid': True}))])
        m2.commit([(99, TS)])
        m2.record()
        m2.commit([(6, norm({'sample': 6, 'band': 'shared', 'amplitude': 60, 'valid': True}))])
        m2.record()
        m2.commit([(8, norm({'sample': 8, 'band': 'b8', 'valid': True})),
                   (9, norm({'sample': 9, 'band': 'b9', 'valid': True}))])
        m2.commit([(7, norm({'sample': 7, 'band': 'b7', 'amplitude': 70, 'valid': True}))])
        qcheck(rec, 'X15-restored', restored, m2,
               {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}})
        expected_vac2 = m2.vacuum(8)
        observed_vac2 = restored.call('POST', '/vacuum', {'horizon': 8})
        rec.check('X16-vacuum', restored.desc('POST', '/vacuum', {'horizon': 8}),
                  expected_vac2, observed_vac2)
        qcheck(rec, 'X16-restored', restored, m2,
               {'at': 2, 'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    finally:
        restored.stop()

    # X17: the horizon survives a restart
    s.call('POST', '/checkpoint')
    s.stop()
    s.start()
    stats = s.call('GET', '/stats')
    rec.check('X17-stats', s.desc('GET', '/stats'),
              {'sequence': m.seq, 'horizon': m.horizon},
              {'sequence': stats['sequence'], 'horizon': stats['horizon']})
    qcheck(rec, 'X17-old', s, m, {'at': 2,
                                  'aggregate': {'function': 'sum', 'column': 'amplitude'}})
    qcheck(rec, 'X17-boundary', s, m, {'at': 8,
                                       'aggregate': {'function': 'sum', 'column': 'amplitude'}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--group', choices=['core', 'extended', 'all'], default='all')
    args = parser.parse_args()
    project = pathlib.Path(__file__).resolve().parents[1]
    binary = project / 'bin/laminad'
    if not binary.exists():
        raise SystemExit('build first: go build -o bin/laminad ./cmd/laminad')
    groups = ['core', 'extended'] if args.group == 'all' else [args.group]
    failed = 0
    for group in groups:
        rec = Recorder()
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='lamina-probe-') as temp:
            s = Session(binary, pathlib.Path(temp) / 'data').start()
            try:
                if group == 'core':
                    core(s, rec)
                else:
                    extended(s, rec, temp, binary)
            finally:
                s.stop()
        elapsed = time.monotonic() - started
        for record in rec.records:
            print(json.dumps({'group': group, **record}, ensure_ascii=False))
        print(json.dumps({'group': group, 'summary': True, 'checks': len(rec.records),
                          'failed': rec.failed, 'seconds': round(elapsed, 2)}))
        failed += rec.failed
    if failed:
        raise SystemExit(f'{failed} check(s) failed')


if __name__ == '__main__':
    main()
