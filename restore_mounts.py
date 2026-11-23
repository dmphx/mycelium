"""Pure restore namespace planning. Carriers must still prove filesystem identities.

Logical helper views are read-only, including every nested application bind.
Writes use only explicitly admitted physical roots. No host paths are inferred
from a data-root suffix: the deepest application mount determines placement.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath


class MountMismatch(ValueError):
    """The inspected namespace or a proposed placement differs from its plan."""


def absolute(value):
    if not isinstance(value, str) or not value.startswith('/'):
        raise MountMismatch('absolute path required')
    p = PurePosixPath(value)
    if str(p) != value or '..' in p.parts or '\x00' in value or ':' in value:
        raise MountMismatch('normalized Docker bind path required')
    return p


def reject_physical_shadow(logical_binds, physical_roots):
    if any(absolute(r.helper_root).is_relative_to(b.destination)
           or absolute(b.destination).is_relative_to(r.helper_root)
           for r in physical_roots for b in logical_binds):
        raise MountMismatch('physical helper bind shadows logical namespace')


@dataclass(frozen=True, order=True)
class Bind:
    source: str
    destination: str
    writable: bool
    propagation: str = 'rprivate'

    def __post_init__(self):
        absolute(self.source)
        absolute(self.destination)
        if type(self.writable) is not bool or self.propagation != 'rprivate':
            raise MountMismatch('unsupported bind access/propagation')

    @classmethod
    def inspected(cls, value):
        if value.get('Type') != 'bind' or type(value.get('RW')) is not bool:
            raise MountMismatch('only explicit bind mounts are admitted')
        return cls(value['Source'], value['Destination'], value['RW'],
                   value.get('Propagation', 'rprivate'))


@dataclass(frozen=True)
class PhysicalRoot:
    application_source: str
    pool: str
    host_root: str
    helper_root: str

    def __post_init__(self):
        absolute(self.application_source)
        host, helper = absolute(self.host_root), absolute(self.helper_root)
        if not self.pool or '/' in self.pool or self.pool in ('.', '..', 'user'):
            raise MountMismatch('physical pool required')
        # The caller supplies the concrete pool root; shfs must never be used.
        if not host.is_relative_to(PurePosixPath('/mnt') / self.pool) or host == PurePosixPath('/mnt') / self.pool:
            raise MountMismatch('physical source must be strictly below its pool')
        if not helper.is_relative_to('/physical') or helper == PurePosixPath('/physical'):
            raise MountMismatch('physical helper root required')


@dataclass(frozen=True)
class Placement:
    logical: str
    mount_destination: str
    application_source: str
    pool: str
    host_path: str
    helper_path: str


@dataclass(frozen=True)
class RestoreMountPlan:
    logical_root: str
    application_binds: tuple[Bind, ...]
    physical_roots: tuple[PhysicalRoot, ...]
    auxiliary_binds: tuple[Bind, ...] = ()

    @classmethod
    def from_inspect(cls, mounts, physical_roots, logical_root='/data', auxiliary_binds=()):
        root = absolute(logical_root)
        selected = []
        for row in mounts:
            dest = absolute(row['Destination'])
            if dest == root or dest.is_relative_to(root):
                selected.append(Bind.inspected(row))
        selected.sort(key=lambda b: (len(absolute(b.destination).parts), b.destination))
        if sum(b.destination == logical_root for b in selected) != 1:
            raise MountMismatch('one application data-root bind required')
        if len({b.destination for b in selected}) != len(selected):
            raise MountMismatch('duplicate application destination')
        roots = tuple(physical_roots)
        keys = {(r.application_source, r.pool) for r in roots}
        if len(keys) != len(roots) or not roots:
            raise MountMismatch('duplicate or absent physical placement roots')
        sources = {b.source for b in selected}
        if any(r.application_source not in sources for r in roots):
            raise MountMismatch('physical root is not an application mount source')
        if any(source not in {r.application_source for r in roots} for source in sources):
            raise MountMismatch('every application mount needs explicit physical placement')
        helper_paths = [absolute(r.helper_root) for r in roots]
        hosts = [absolute(r.host_root) for r in roots]
        reject_physical_shadow(selected, roots)
        # Overlapping roots can expose the same writable file under two aliases.
        for paths in (helper_paths, hosts):
            for i, first in enumerate(paths):
                if any(first.is_relative_to(second) or second.is_relative_to(first) for second in paths[i+1:]):
                    raise MountMismatch('overlapping physical roots')
        auxiliary = tuple(auxiliary_binds)
        occupied = [absolute(b.destination) for b in selected] + helper_paths
        for b in auxiliary:
            dest = absolute(b.destination)
            if any(dest.is_relative_to(p) or p.is_relative_to(dest) for p in occupied):
                raise MountMismatch('auxiliary bind shadows a planned namespace')
            occupied.append(dest)
        return cls(logical_root, tuple(selected), roots, auxiliary)

    def validate_live_mounts(self, mounts):
        current = self.from_inspect(mounts, self.physical_roots, self.logical_root, self.auxiliary_binds)
        if current.application_binds != self.application_binds:
            raise MountMismatch('live application mount projection changed')

    def worker_mounts(self, readonly_data=False):
        reject_physical_shadow(self.application_binds, self.physical_roots)
        # Read-only logical views are intentional. Effects use physical binds.
        logical = [Bind(b.source, b.destination, False, b.propagation) for b in self.application_binds]
        physical = [Bind(r.host_root, r.helper_root, not readonly_data) for r in self.physical_roots]
        auxiliary = [Bind(b.source, b.destination, b.writable and not readonly_data, b.propagation)
                     for b in self.auxiliary_binds]
        return tuple(logical + physical + auxiliary)

    def validate_helper_mounts(self, mounts, readonly_data=False, allowed_tmpfs=()):
        reject_physical_shadow(self.application_binds, self.physical_roots)
        actual = []
        seen_tmpfs = set()
        for row in mounts:
            if row.get('Type') == 'tmpfs':
                dest = str(absolute(row['Destination']))
                if dest not in allowed_tmpfs or dest in seen_tmpfs:
                    raise MountMismatch('unexpected helper tmpfs')
                # A tmpfs cannot hide any planned source or destination.
                if any(absolute(b.destination).is_relative_to(dest) or absolute(dest).is_relative_to(b.destination)
                       for b in self.worker_mounts(readonly_data)):
                    raise MountMismatch('tmpfs shadows planned namespace')
                seen_tmpfs.add(dest)
            else:
                actual.append(Bind.inspected(row))
        expected = self.worker_mounts(readonly_data)
        if seen_tmpfs != set(allowed_tmpfs) or len(actual) != len(expected) or set(actual) != set(expected):
            raise MountMismatch('helper namespace differs from complete planned mounts')

    def place(self, logical, pool):
        path, root = absolute(logical), absolute(self.logical_root)
        if path == root or not path.is_relative_to(root):
            raise MountMismatch('file must be strictly below logical root')
        candidates = [b for b in self.application_binds if path.is_relative_to(b.destination)]
        bind = max(candidates, key=lambda b: len(absolute(b.destination).parts))
        relative = path.relative_to(bind.destination)
        if relative == PurePosixPath('.'):
            raise MountMismatch('mount root is not a file target')
        matches = [r for r in self.physical_roots if r.application_source == bind.source and r.pool == pool]
        if len(matches) != 1:
            raise MountMismatch('no exact physical placement for deepest application mount')
        physical = matches[0]
        return Placement(logical, bind.destination, bind.source, pool,
                         str(absolute(physical.host_root) / relative),
                         str(absolute(physical.helper_root) / relative))

    def validate_placement(self, logical, pool, host_path, helper_path):
        expected = self.place(logical, pool)
        if expected.host_path != str(absolute(host_path)) or expected.helper_path != str(absolute(helper_path)):
            raise MountMismatch('placement bypasses deepest application bind')
        return expected
