"""Topology regression and pure projection/placement refusal boundaries."""
from dataclasses import replace
import unittest

from restore_mounts import Bind, MountMismatch, PhysicalRoot, RestoreMountPlan


def row(source, dest, rw=True, kind='bind'):
    return dict(Type=kind, Source=source, Destination=dest, RW=rw, Propagation='rprivate')


def fixture():
    mounts=[row('/storage/data','/data'),row('/storage/library','/data/media')]
    roots=[PhysicalRoot('/storage/data','pool','/mnt/pool/app/data','/physical/data'),
           PhysicalRoot('/storage/library','pool','/mnt/pool/app/library','/physical/library')]
    return mounts,roots


class MountProjection(unittest.TestCase):
    def setUp(self):
        self.mounts,self.roots=fixture();self.plan=RestoreMountPlan.from_inspect(self.mounts,self.roots)
    def inspected_helper(self,readonly=False):
        return [row(b.source,b.destination,b.writable) for b in self.plan.worker_mounts(readonly)]
    def test_nested_rw_application_bind_is_mirrored_as_ro_logical_view(self):
        self.assertIn(Bind('/storage/library','/data/media',False),self.plan.worker_mounts())
        self.assertTrue(all(not b.writable for b in self.plan.worker_mounts() if b.destination.startswith('/data')))
        self.plan.validate_helper_mounts(self.inspected_helper())
    def test_old_omitted_nested_bind_refuses(self):
        old=[m for m in self.inspected_helper() if m['Destination']!='/data/media']
        with self.assertRaises(MountMismatch):self.plan.validate_helper_mounts(old)
    def test_placement_uses_library_not_hidden_under_data(self):
        p=self.plan.place('/data/media/series/Example/Season 01/Example S01E01.nfo','pool')
        self.assertEqual(p.host_path,'/mnt/pool/app/library/series/Example/Season 01/Example S01E01.nfo')
        self.assertEqual(p.helper_path,'/physical/library/series/Example/Season 01/Example S01E01.nfo')
        with self.assertRaises(MountMismatch):self.plan.validate_placement(p.logical,'pool','/mnt/pool/app/data/media/series/Example/Season 01/Example S01E01.nfo','/physical/data/media/series/Example/Season 01/Example S01E01.nfo')
    def test_source_quarantine_still_uses_data_mount(self):
        self.assertEqual(self.plan.place('/data/quarantine/example.nfo','pool').host_path,'/mnt/pool/app/data/quarantine/example.nfo')
    def test_missing_nested_physical_root_refuses(self):
        with self.assertRaises(MountMismatch):RestoreMountPlan.from_inspect(self.mounts,self.roots[:1])
    def test_missing_specific_pool_never_falls_back(self):
        with self.assertRaises(MountMismatch):self.plan.place('/data/media/series/x.nfo','other')
    def test_live_added_or_removed_nested_bind_refuses(self):
        for mounts in [self.mounts[:1],self.mounts+[row('/foreign','/data/media/new')]]:
            with self.assertRaises(MountMismatch):self.plan.validate_live_mounts(mounts)
    def test_changed_source_rw_or_propagation_refuses(self):
        for key,value in [('Source','/foreign'),('RW',False),('Propagation','rshared')]:
            mounts=[dict(m) for m in self.mounts];mounts[1][key]=value
            with self.assertRaises(MountMismatch):self.plan.validate_live_mounts(mounts)
    def test_wrong_helper_rw_source_extra_or_duplicate_refuses(self):
        for change in ['rw','source','extra','duplicate']:
            mounts=self.inspected_helper()
            if change=='rw':mounts[1]['RW']=True
            elif change=='source':mounts[1]['Source']='/storage/data/media'
            elif change=='extra':mounts.append(row('/foreign','/extra'))
            else:mounts.append(dict(mounts[0]))
            with self.assertRaises(MountMismatch):self.plan.validate_helper_mounts(mounts)
    def test_all_ro_inventory(self):
        self.assertTrue(all(not b.writable for b in self.plan.worker_mounts(True)))
        self.plan.validate_helper_mounts(self.inspected_helper(True),True)
    def test_unapproved_volume_or_tmpfs_cannot_shadow_nested_bind(self):
        for kind in ['volume','tmpfs']:
            mounts=self.inspected_helper();mounts[1]['Type']=kind
            with self.assertRaises(MountMismatch):self.plan.validate_helper_mounts(mounts)
        with self.assertRaises(MountMismatch):self.plan.validate_helper_mounts(self.inspected_helper()+[row('','/data/media',kind='tmpfs')],allowed_tmpfs=('/data/media',))
    def test_explicit_tmpfs_and_auxiliary_views(self):
        plan=RestoreMountPlan.from_inspect(self.mounts,self.roots,auxiliary_binds=(Bind('/operation/input','/inputs',False),Bind('/operation/output','/outputs',True)))
        mounts=[row(b.source,b.destination,b.writable) for b in plan.worker_mounts()]+[row('','/tmp',kind='tmpfs')]
        plan.validate_helper_mounts(mounts,allowed_tmpfs=('/tmp',))
        with self.assertRaises(MountMismatch):RestoreMountPlan.from_inspect(self.mounts,self.roots,auxiliary_binds=(Bind('/foreign','/data',True),))
    def test_deepest_of_three_mounts_wins(self):
        mounts=self.mounts+[row('/storage/special','/data/media/special')]
        roots=self.roots+[PhysicalRoot('/storage/special','pool','/mnt/pool/app/special','/physical/special')]
        p=RestoreMountPlan.from_inspect(mounts,roots).place('/data/media/special/x.nfo','pool')
        self.assertEqual(p.host_path,'/mnt/pool/app/special/x.nfo')
    def test_normalization_and_root_target_refuse(self):
        for path in ['/data','/data/media','/data/../outside','/data//x','relative','/database/x']:
            with self.assertRaises(MountMismatch):self.plan.place(path,'pool')
    def test_duplicate_or_unsupported_application_mounts_refuse(self):
        for mounts in [self.mounts+[self.mounts[1]],[],[row('/storage/data','/data',kind='volume')]]:
            with self.assertRaises(MountMismatch):RestoreMountPlan.from_inspect(mounts,self.roots)
    def test_shfs_or_wrong_pool_or_overlapping_physical_roots_refuse(self):
        for pool,host in [('user','/mnt/user/app/library'),('pool','/mnt/other/app/library'),('pool','/mnt/pool')]:
            with self.assertRaises(MountMismatch):PhysicalRoot('/storage/library',pool,host,'/physical/library')
        roots=[self.roots[0],replace(self.roots[1],host_root='/mnt/pool/app/data/media')]
        with self.assertRaises(MountMismatch):RestoreMountPlan.from_inspect(self.mounts,roots)

if __name__=='__main__':unittest.main()
