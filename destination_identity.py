"""Offline identity/preimage/rollback bindings. Read-only fixture files, no effects."""
import hashlib
import json
import os
import stat
import unicodedata

import destination_authority as gate
import request_execution_plans as plans

_MANIFEST = {'version','mode','domain_id','identity','source_candidates','destination_path',
             'rollback_path','source_sha256','preimage_sha256','expected_forward_revision'}
_IDENTITY = {'media_type','tmdb_id','imdb_id','season','episode','tmdb_episode_id'}
_HANDLE = {'domain_id','binding_sha256','generation','revision','worker_id'}
_MAX_FILE = 16*1024*1024


def init_schema(conn):
    """Explicit fixture initialization only, no normal db.init or migration."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS destination_identity_bindings (
          domain_id TEXT PRIMARY KEY REFERENCES destination_model_fences(domain_id),
          binding_json TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
          generation INTEGER NOT NULL DEFAULT 1 CHECK(generation=1),
          revision INTEGER NOT NULL DEFAULT 0 CHECK(revision BETWEEN 0 AND 2),
          state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','claimed','checked')),
          worker_id TEXT
        )""",
        """CREATE TRIGGER IF NOT EXISTS destination_identity_immutable
          BEFORE UPDATE OF domain_id,binding_json,binding_sha256,generation ON destination_identity_bindings
          BEGIN SELECT RAISE(ABORT,'immutable identity and rollback binding'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_identity_retained
          BEFORE DELETE ON destination_identity_bindings
          BEGIN SELECT RAISE(ABORT,'retained identity and rollback binding'); END""",
        """CREATE TRIGGER IF NOT EXISTS destination_identity_CAS
          BEFORE UPDATE OF revision,state,worker_id ON destination_identity_bindings
          WHEN NOT ((OLD.revision=0 AND OLD.state='prepared' AND OLD.worker_id IS NULL
                     AND NEW.revision=1 AND NEW.state='claimed' AND NEW.worker_id IS NOT NULL)
                 OR (OLD.revision=1 AND OLD.state='claimed'
                     AND NEW.revision=2 AND NEW.state='checked' AND NEW.worker_id IS OLD.worker_id))
          BEGIN SELECT RAISE(ABORT,'identity claim revision CAS'); END""",
    ):
        conn.execute(statement)


def _path(value):
    if type(value) is not str or not value or len(value)>4096:
        raise plans.PlanConflict('bounded literal relative path required')
    if (value.startswith('/') or any(c in value for c in ('\\','%',':'))
        or any(unicodedata.category(c).startswith('C') for c in value)
        or unicodedata.normalize('NFC',value)!=value):
        raise plans.PlanConflict('ambiguous or encoded path refused')
    parts=value.split('/')
    if len(parts)>32 or any(p in ('','.','..') or len(p.encode())>255 for p in parts):
        raise plans.PlanConflict('noncanonical relative path refused')
    return parts


def _stamp(value):
    return {'device':value.st_dev,'inode':value.st_ino,'mode':value.st_mode,
            'links':value.st_nlink,'size':value.st_size,'mtime_ns':value.st_mtime_ns,'ctime_ns':value.st_ctime_ns}


def _root(root_fd):
    if type(root_fd) is not int or root_fd<0:raise plans.PlanConflict('owned directory descriptor required')
    try:value=os.fstat(root_fd)
    except OSError:raise plans.PlanConflict('unavailable directory descriptor') from None
    if not stat.S_ISDIR(value.st_mode):raise plans.PlanConflict('directory descriptor required')
    return {'device':value.st_dev,'inode':value.st_ino}


def observe_file(root_fd, relative_path):
    """No normalization or symlink following; bounded read under owned root fd.

    Metadata timestamps only detect change during this observation. They are
    never reader absence, authority, freshness or completeness evidence.
    """
    parts=_path(relative_path);_root(root_fd);fd=os.dup(root_fd);chain=[]
    try:
        root=_root(fd)
        for index,part in enumerate(parts):
            matches=[]
            with os.scandir(fd) as entries:
                for number,entry in enumerate(entries):
                    if number>=4096:raise plans.PlanConflict('namespace observation bound exceeded')
                    if unicodedata.normalize('NFC',entry.name).casefold()==part.casefold():matches.append(entry.name)
            if matches!=[part]:raise plans.PlanConflict('missing or ambiguous case/Unicode alias')
            flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK
            if index<len(parts)-1:flags|=os.O_DIRECTORY
            child=os.open(part,flags,dir_fd=fd);os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_dev!=root['device']:raise plans.PlanConflict('cross-device identity refused')
            chain.append(_stamp(info))
        before=os.fstat(fd);chain[-1]=_stamp(before)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or not 0<=before.st_size<=_MAX_FILE:
            raise plans.PlanConflict('regular unaliased bounded file required')
        digest=hashlib.sha256();total=0
        while True:
            data=os.read(fd,65536)
            if not data:break
            total+=len(data)
            if total>_MAX_FILE:raise plans.PlanConflict('file grew beyond observation bound')
            digest.update(data)
        after=os.fstat(fd)
        if _stamp(before)!=_stamp(after) or total!=before.st_size:
            raise plans.PlanConflict('file changed during preimage observation')
        return {'root':root,'path':relative_path,'namespace_key':relative_path.casefold(),
                'chain':chain,'sha256':digest.hexdigest(),'size':total}
    except (OSError,UnicodeError):
        raise plans.PlanConflict('unavailable, symlink or unstable file identity') from None
    finally:
        os.close(fd)


def _parent(conn, handle):
    expected=gate._HANDLE|{'model_coverage_checked','qualified_for_effects'}
    if (type(handle) is not dict or set(handle)!=expected
        or type(handle['revision']) is not int or handle['revision']!=1
        or handle['model_coverage_checked'] is not True or handle['qualified_for_effects'] is not False):
        raise plans.PlanConflict('exact checked model fence required')
    gate._number(handle['fence_revision']);plans._hash(handle['binding_sha256'])
    plan=gate._validated_plan(conn,handle['request_id'],handle['owner_id'],handle['plan_generation'],handle['plan_sha256'])
    row,m=gate._domain(conn,handle['domain_id']);gate._idle(conn,row,m)
    fence=conn.execute('SELECT * FROM destination_model_fences WHERE domain_id=?',(handle['domain_id'],)).fetchone()
    if fence is None:raise plans.PlanConflict('checked fence absent')
    try:
        b=json.loads(fence['binding_json'])
        if plans.scopes.canonical(b)!=fence['binding_json'] or plans._sha(fence['binding_json'])!=fence['binding_sha256']:raise ValueError('fence binding')
    except (ValueError,TypeError):raise plans.PlanConflict('fence integrity changed') from None
    if (row['phase']!='checked' or fence['state']!='checked' or fence['revision']!=1
        or handle['domain_id']!=b['domain_id'] or handle['binding_sha256']!=fence['binding_sha256']
        or handle['worker_id']!=fence['worker_id'] or handle['fence_revision']!=fence['fence_revision']
        or handle['fence_revision']!=row['fence_revision']
        or any(type(handle[k]) is not type(b[k]) or handle[k]!=b[k] for k in ('request_id','owner_id','plan_generation','plan_sha256'))
        or plan['source']!=b['source'] or row['closure_sha256']!=b['closure_sha256']
        or row['manifest_sha256']!=b['manifest_sha256']
        or gate._snapshot(row)!={**b['expected'],'fence_revision':handle['fence_revision']}):
        raise plans.PlanConflict('stale model authority or source/destination generation')
    return plan,row,b


def _manifest(value, expected_hash, plan, row):
    plans._hash(expected_hash)
    if type(value) is not dict or set(value)!=_MANIFEST or plans._sha(plans.scopes.canonical(value))!=expected_hash:
        raise plans.PlanConflict('exact pinned identity manifest required')
    if (type(value['version']) is not int or value['version']!=1
        or value['mode']!='owned_synthetic_identity_binding' or value['domain_id']!=row['domain_id']):
        raise plans.PlanConflict('offline identity namespace mismatch')
    identity=value['identity'];source=plan['source']
    if (type(identity) is not dict or set(identity)!=_IDENTITY
        or identity['media_type']!='tv' or type(identity['tmdb_id']) is not int
        or identity['tmdb_id']!=source['tmdb_id'] or identity['media_type']!=source['media_type']
        or identity['imdb_id']!=source['imdb_id']):
        raise plans.PlanConflict('typed source identity mismatch')
    for key in ('season','episode','tmdb_episode_id'):
        if type(identity[key]) is not int or not (0 if key=='season' else 1)<=identity[key]<=2**31-1:
            raise plans.PlanConflict('strict episode identity required')
    episodes=[e for e in plan['snapshot']['episodes'] if all(e[k]==identity[k] for k in ('season','episode','tmdb_episode_id'))]
    if len(episodes)!=1:raise plans.PlanConflict('episode not in exact owned plan')
    candidates=value['source_candidates']
    if (type(candidates) is not list or len(candidates)!=1 or type(candidates[0]) is not dict
        or set(candidates[0])!={'path','identity'}
        or plans.scopes.canonical(candidates[0]['identity'])!=plans.scopes.canonical(identity)):
        raise plans.PlanConflict('missing or ambiguous source candidates')
    for path in (candidates[0]['path'],value['destination_path'],value['rollback_path']):_path(path)
    for field in ('source_sha256','preimage_sha256'):plans._hash(value[field])
    if (value['preimage_sha256']!=gate._preimage(row['preimage'])
        or type(value['expected_forward_revision']) is not int
        or value['expected_forward_revision']!=row['destination_revision']+1):
        raise plans.PlanConflict('preimage or planned forward revision mismatch')
    return value


def _files(root_fd, manifest):
    observations={role:observe_file(root_fd,path) for role,path in (
        ('source',manifest['source_candidates'][0]['path']),('destination',manifest['destination_path']),('rollback',manifest['rollback_path']))}
    leaves=[(x['chain'][-1]['device'],x['chain'][-1]['inode']) for x in observations.values()]
    namespace=[x['namespace_key'] for x in observations.values()]
    if len(set(leaves))!=3 or len(set(namespace))!=3:
        raise plans.PlanConflict('source, destination and rollback alias each other')
    if (observations['source']['sha256']!=manifest['source_sha256']
        or any(observations[k]['sha256']!=manifest['preimage_sha256'] for k in ('destination','rollback'))):
        raise plans.PlanConflict('source, destination preimage or rollback bytes changed')
    return observations


def _public(row):
    return {'domain_id':row['domain_id'],'binding_sha256':row['binding_sha256'],
            'generation':row['generation'],'revision':row['revision'],'worker_id':row['worker_id'],
            'qualified_for_effects':False,'qualified_for_rollback':False}


def prepare(conn, checked_fence, root_fd, manifest, manifest_sha256):
    """Observe and bind owned fixture files only; no installation or backup copy."""
    with plans._tx(conn):
        plan,row,parent=_parent(conn,checked_fence)
        manifest=_manifest(manifest,manifest_sha256,plan,row);files=_files(root_fd,manifest)
        binding={'version':1,'manifest':manifest,'manifest_sha256':manifest_sha256,
                 'checked_fence':checked_fence,'source':plan['source'],'files':files,
                 'rollback_guard':{'preimage_sha256':manifest['preimage_sha256'],
                                   'planned_forward_sha256':manifest['source_sha256'],
                                   'planned_forward_revision':manifest['expected_forward_revision'],
                                   'actual_forward_receipt_available':False}}
        text=plans.scopes.canonical(binding)
        existing=conn.execute('SELECT * FROM destination_identity_bindings WHERE domain_id=?',(row['domain_id'],)).fetchone()
        if existing is not None:
            if existing['binding_json']!=text:raise plans.PlanConflict('immutable identity binding already exists')
            return _public(existing)
        conn.execute('INSERT INTO destination_identity_bindings(domain_id,binding_json,binding_sha256) VALUES (?,?,?)',(row['domain_id'],text,plans._sha(text)))
        return _public(conn.execute('SELECT * FROM destination_identity_bindings WHERE domain_id=?',(row['domain_id'],)).fetchone())


def _load(conn, checked_fence, root_fd, binding_sha256, generation):
    if type(generation) is not int or generation!=1:raise plans.PlanConflict('stale identity binding generation')
    plans._hash(binding_sha256);plan,parent_row,parent=_parent(conn,checked_fence)
    row=conn.execute('SELECT * FROM destination_identity_bindings WHERE domain_id=?',(checked_fence['domain_id'],)).fetchone()
    if row is None or row['binding_sha256']!=binding_sha256 or row['generation']!=generation:
        raise plans.PlanConflict('identity binding pin changed')
    try:
        b=json.loads(row['binding_json'])
        if plans.scopes.canonical(b)!=row['binding_json'] or plans._sha(row['binding_json'])!=row['binding_sha256']:raise ValueError('binding hash')
    except (ValueError,TypeError):raise plans.PlanConflict('identity binding integrity') from None
    if b['checked_fence']!=checked_fence or b['source']!=plan['source']:
        raise plans.PlanConflict('bound request/fence generation changed')
    manifest=_manifest(b['manifest'],b['manifest_sha256'],plan,parent_row)
    if _files(root_fd,manifest)!=b['files']:
        raise plans.PlanConflict('root, ancestor, file identity or preimage changed')
    return row,b


def claim(conn, checked_fence, root_fd, *, binding_sha256, generation, expected_revision, worker_id):
    if type(expected_revision) is not int or expected_revision!=0:raise plans.PlanConflict('strict prepared revision required')
    if type(worker_id) is not str or not plans._WORKER.fullmatch(worker_id):raise plans.PlanConflict('invalid identity worker')
    with plans._tx(conn):
        row,b=_load(conn,checked_fence,root_fd,binding_sha256,generation)
        changed=conn.execute("UPDATE destination_identity_bindings SET revision=1,state='claimed',worker_id=? WHERE domain_id=? AND generation=? AND revision=0 AND state='prepared' AND binding_sha256=?",
                             (worker_id,row['domain_id'],generation,binding_sha256)).rowcount
        if changed!=1:raise plans.PlanConflict('identity binding CAS lost')
        return {k:v for k,v in _public(conn.execute('SELECT * FROM destination_identity_bindings WHERE domain_id=?',(row['domain_id'],)).fetchone()).items() if k in _HANDLE}


def check(conn, checked_fence, root_fd, handle):
    if (type(handle) is not dict or set(handle)!=_HANDLE or type(handle['revision']) is not int
        or handle['revision']!=1 or type(handle['worker_id']) is not str
        or not plans._WORKER.fullmatch(handle['worker_id']) or type(checked_fence) is not dict
        or handle['domain_id']!=checked_fence.get('domain_id')):
        raise plans.PlanConflict('exact identity claim handle required')
    with plans._tx(conn):
        row,b=_load(conn,checked_fence,root_fd,handle['binding_sha256'],handle['generation'])
        changed=conn.execute("UPDATE destination_identity_bindings SET revision=2,state='checked' WHERE domain_id=? AND generation=? AND revision=1 AND state='claimed' AND worker_id=? AND binding_sha256=?",
                             (handle['domain_id'],handle['generation'],handle['worker_id'],handle['binding_sha256'])).rowcount
        if changed!=1:raise plans.PlanConflict('stale identity claimant')
        return _public(conn.execute('SELECT * FROM destination_identity_bindings WHERE domain_id=?',(handle['domain_id'],)).fetchone())


def check_rollback_binding(conn, checked_fence, root_fd, *, binding_sha256, generation,
                           expected_forward_revision, expected_forward_sha256):
    """Check a planned inverse binding, never evidence of an applied forward effect."""
    if type(expected_forward_revision) is not int:raise plans.PlanConflict('strict planned forward revision required')
    plans._hash(expected_forward_sha256)
    with plans._tx(conn):
        row,b=_load(conn,checked_fence,root_fd,binding_sha256,generation)
        guard=b['rollback_guard']
        if (row['state']!='checked' or row['revision']!=2
            or expected_forward_revision!=guard['planned_forward_revision']
            or expected_forward_sha256!=guard['planned_forward_sha256']):
            raise plans.PlanConflict('rollback generation or postimage binding mismatch')
        return {'rollback_binding_consistent':True,'actual_forward_receipt_available':False,
                'qualified_for_rollback':False,'qualified_for_effects':False,
                'preimage_sha256':guard['preimage_sha256']}


def require_effect_authority(*args, **kwargs):
    plans.require_effect_authority(*args, **kwargs)
