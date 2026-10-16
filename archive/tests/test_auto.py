"""Real SQLite/tar/FITS/files; simulate only CASDA, Slurm and SIF binaries."""
import copy, json, os, shutil, subprocess, sys, tarfile, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from astropy.table import Table
from astropy.io import fits
from astropy.wcs import WCS
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from askap_auto import archive,catalogue,cli,controller,query,science,settings
from askap_auto.common import read_json,write_json
from askap_auto.state import State
from askap_auto.worker import run_job
import pull_products
from template_settings import template_settings, write_settings


def rows():
    return Table({'obs_id':['ASKAP-100','ASKAP-100','ASKAP-200'],
        'filename':['science.SB100.beam00.ms.tar','science.SB100.beam01.ms.tar','science.SB200.beam02.ms.tar'],
        'obs_publisher_did':['ivo:a','ivo:b','ivo:c'],'s_ra':[180.,180.1,180.2],'s_dec':[-28.]*3,
        'em_min':[.21]*3,'em_max':[.3]*3,'t_exptime':[600.]*3,'t_min':[59000.]*3,
        'quality_level':['GOOD']*3,'access_estsize':[1.]*3})

class Casda:
    def __init__(self,root): self.root=root; self.calls=[]
    def stage_data(self,table):
        self.filename=str(table['filename'][0])
        return ['https://example.test/'+self.filename+'?signed=secret','https://example.test/'+self.filename+'.checksum?secret=1']
    def download_files(self,urls,savedir):
        self.calls.append(list(urls)); path=Path(savedir)
        raw=self.root/'fixture'/self.filename[:-4]; raw.mkdir(parents=True,exist_ok=True)
        (raw/'table.dat').write_bytes(b'original')
        with tarfile.open(path/self.filename,'w') as handle: handle.add(raw,arcname=raw.name)
        (path/(self.filename+'.checksum')).write_text('checksum')

class Science:
    def __init__(self): self.calls=[]; self.fail=False; self.missing_v=False; self.ms_time=59000.
    def __call__(self,command,cwd=None,**kw):
        if command[0]=='tar': return subprocess.run(command,cwd=cwd,**kw)
        self.calls.append(command)
        names=[x for x in command if x.startswith('dstools-')]; tool=names[0] if names else 'time'
        if '--help' in command:
            return SimpleNamespace(stdout=' '.join('--'+k for k in settings.MODEL_OPTIONS)+' --verbose --threads --abs-mem --temp-dir --out-dir --name --savelogs --mask-pos --mask-radius --split-ms --datacolumn --minuvdist --phasecentre --noflag --baseline-average --no-baseline-average --primary-beam ',stderr='')
        work=Path(cwd)
        if tool=='dstools-askap-preprocess':
            ms=Path(command[-1]); (ms/'FIELD_OLD').mkdir(); (ms/'table.dat').write_bytes(b'preprocessed')
        elif tool=='dstools-create-model':
            model=work/'wsclean_model'; model.mkdir()
            w=WCS(naxis=2); w.wcs.crpix=[51,51]; w.wcs.cdelt=[-.01,.01]; w.wcs.crval=[180,-28]; w.wcs.ctype=['RA---TAN','DEC--TAN']
            for pol in ['I','V']:
                if pol=='V' and self.missing_v: continue
                fits.PrimaryHDU(np.ones((100,100)),w.to_header()).writeto(model/('wsclean-MFS-'+pol+'-image.fits'))
            (model/'wsclean-MFS-I-model.fits').write_bytes(b'original-model'); (model/'wsclean-0000-I-model.fits').write_bytes(b'channel')
        elif tool=='dstools-insert-model':
            model,ms=map(Path,command[-2:]); assert (ms/'FIELD_OLD').is_dir()
            (model/'wsclean-MFS-I-model.fits').write_bytes(b'masked')
        elif tool=='dstools-subtract-model': Path(command[-1]).with_name('input.subtracted.ms').mkdir()
        elif tool=='dstools-extract-ds':
            if self.fail: raise subprocess.CalledProcessError(1,command)
            Path(command[-1]).write_bytes(b'DS')
        else:
            if self.ms_time is None: raise subprocess.CalledProcessError(1,command)
            return SimpleNamespace(stdout=json.dumps({'mjd':self.ms_time,'scale':'utc','source':'MS'}))
        return SimpleNamespace(stdout='')

class Remote:
    def __init__(self): self.fail_receipt=False
    def inventory(self,products,work):
        return json.loads(subprocess.run([sys.executable,'-c',pull_products.REMOTE_INVENTORY,products,work],text=True,capture_output=True,check=True).stdout)
    def pull(self,products,destination,paths):
        for name in paths:
            out=Path(destination)/name; out.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(Path(products)/name,out)
    def receipt(self,local,work):
        if self.fail_receipt: raise RuntimeError('receipt upload failed')
        out=Path(work)/'state/transfer_receipts'/local.name; out.parent.mkdir(exist_ok=True); shutil.copy2(local,out)

class Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)
        self.s=template_settings(); self.s.update(work_root=str(self.root/'work'),product_root=str(self.root/'products'),container=str(self.root/'image.sif'),query_delay_seconds=0,download_retry_delay_seconds=0,modules=[],parallel_slots=2)
        Path(self.s['container']).write_bytes(b'SIF'); Path(self.s['product_root']).mkdir(); settings.validate(self.s)
        self.source=catalogue.make_source('Star A',180,-28,self.s); self.state=State(self.s['work_root']); self.science=Science(); self.casda=Casda(self.root)
    def snapshot(self,table=None,source=None):
        table=rows() if table is None else table; source=source or self.source
        return query.query_one(source,self.s,self.root/'query'/source['name'],fetch=lambda *_:table,release_check=lambda t:range(len(t)))
    def plan(self,single=False,mode='normal',snapshot=None):
        snap=snapshot or self.snapshot(); index=snap if single else {'revision':'r','sources':[snap]}
        self.state.sync_index(index,single=single)
        return controller.plan(self.state,index,settings.freeze(self.s,{}),mode=mode,single=single,selected=query.select(snap['observations'],[100]) if single else None)
    def execute(self,run):
        job=controller.pack_job(self.state,run); self.assertIsNotNone(job)
        controller.prepare_job(self.state,job,client_factory=lambda _:self.casda)
        manifest=self.state.root/'jobs'/(job['id']+'.json'); write_json(manifest,job)
        rc=run_job(job,manifest,executor_factory=lambda f,s,d:science.Executor(f,s,d,self.science),preflight_fn=lambda _: {})
        self.assertEqual(rc,0); self.state.store_run(run,'complete'); return job
    def test_csv_lossless_index_and_recorded_reordering(self):
        p=self.root/'cat.csv'; self.s['catalogue_path']=str(p); p.write_text('Name,RA,DEC\n001,180,-28\nSource A,12:00:00,-28:00:00\n')
        sources=catalogue.load_catalogue(self.s); self.assertEqual(sources[0]['name'],'001'); self.assertEqual(sources[1]['processing_index'],2)
        p.write_text('Name,RA,DEC\nSource A,180,-28\n001,180,-28\n')
        revised=catalogue.load_catalogue(self.s)
        self.assertEqual([src['name'] for src in revised],['Source_A','001'])
        self.assertEqual(revised[0]['source_id'],sources[1]['source_id'])
        self.assertTrue(list((self.root/'work/logs/catalogues').glob('*/input_catalogue.json')))
    def test_fits_and_missing_optional_column(self):
        p=self.root/'cat.fits'; Table({'Name':['001'],'RA':[180.],'DEC':[-28.]}).write(p)
        self.s.update(catalogue_path=str(p),catalogue_format='fits'); self.assertEqual(catalogue.load_catalogue(self.s)[0]['name'],'001')
        self.s['work_root']=str(self.root/'other'); self.s['catalogue_columns']['pmra']='missing'
        with self.assertRaisesRegex(ValueError,'missing'): catalogue.load_catalogue(self.s)
    def test_collision_and_invalid_motion_fail_early(self):
        p=self.root/'cat.csv'; self.s['catalogue_path']=str(p); p.write_text('Name,RA,DEC\nSource A,180,-28\nSource_A,180,-28\n')
        with self.assertRaisesRegex(ValueError,'row 2.*collision'): catalogue.load_catalogue(self.s)
        p.write_text('Name,RA,DEC,PM\nS,180,-28,\n'); self.s['catalogue_columns']['pmra']='PM'
        with self.assertRaisesRegex(ValueError,'pmra'): catalogue.load_catalogue(self.s)
    def test_sexagesimal_and_cosdec(self):
        s=copy.deepcopy(self.s); s['ra_unit']='hourangle'; source=catalogue.make_source(None,'12:00:00','-60:00:00',s,100,0)
        ra,dec,status=catalogue.position_at(source,source['epoch_mjd']+365.25); self.assertAlmostEqual((ra-180)*3600000,200,places=2)
        with self.assertRaisesRegex(ValueError,'No observation time'): catalogue.position_at(source)
        self.assertEqual(catalogue.position_at(self.source)[2],'NA')
    def test_release_before_nearest_and_beam_expansion(self):
        obs=query.candidates(rows(),self.source,self.s,released={1,2}); self.assertTrue(obs[1]['default_selected']); self.assertFalse(obs[0]['eligible'])
        obs=query.candidates(rows(),self.source,self.s,released=range(3)); self.assertEqual(sum(o['selected'] for o in query.select(obs,[100])),3)
        self.assertEqual(sum(o['selected'] for o in query.select(obs)),2)
        with self.assertRaisesRegex(ValueError,'absent'): query.select(obs,[999])
    def test_invalid_frequency_and_radius(self):
        t=rows(); t['em_min'][0]=-1; obs=query.candidates(t,self.source,self.s,released=range(3))
        self.assertIsNone(obs[0]['within_radius']); self.assertFalse(obs[0]['eligible']); self.assertTrue(obs[1]['eligible'])
        self.assertAlmostEqual(obs[1]['radius_deg'],.6*1.4/((query.C/.21+query.C/.3)/2/1e9))
    def test_query_top_limit_and_disabled_exposure(self):
        self.s['query_limit']=3
        with self.assertRaisesRegex(RuntimeError,'TOP'): self.snapshot()
        t=rows(); t['t_exptime'][0]=400; self.s['exposure_min']=None
        self.assertTrue(query.candidates(t,self.source,self.s,released=range(3))[0]['eligible'])
    def test_changed_query_settings_need_update(self):
        query.query_catalogue([self.source],self.s,fetch=lambda *_:rows(),release_check=lambda _:range(3)); self.s['exposure_min']=700
        with self.assertRaisesRegex(ValueError,'changed'): query.query_catalogue([self.source],self.s)
    def test_failed_query_retains_history_not_current_selection(self):
        query.query_catalogue([self.source],self.s,fetch=lambda *_:rows(),release_check=lambda _:range(3))
        index=query.query_catalogue([self.source],self.s,update=True,fetch=lambda *_:(_ for _ in ()).throw(RuntimeError('TAP failed')))
        self.assertEqual(index['sources'][0]['status'],'query_failed'); self.assertTrue(index['sources'][0]['observations'])
        self.state.sync_index(index); result=self.state.export(); self.assertFalse(result['query_complete']); self.assertEqual(result['known_target_observations'],0)
    def test_progress_includes_not_started(self):
        self.state.sync_index({'revision':'r','sources':[self.snapshot()]}); self.assertEqual(self.state.export()['status_counts'],{'not_started':2})
    def test_single_preserves_archives_ms_full_images_unmasked_models(self):
        run=self.plan(True); self.execute(run)
        for task in run['tasks']:
            raw=read_json(Path(task['raw_root'])/'raw-ready.json'); self.assertEqual((Path(raw['ms'])/'table.dat').read_bytes(),b'original')
            self.assertTrue(Path(read_json(Path(task['raw_root'])/'download-ready.json')['archive']).is_file())
            a=self.state.attempt(task['targets'][0]['attempt_id']); self.assertEqual(a['status'],'complete'); work=Path(a['path'])/'work'
            self.assertTrue((work/'input.subtracted.ms').is_dir()); self.assertEqual((work/'wsclean_model/wsclean-MFS-I-model.fits').read_bytes(),b'original-model')
            self.assertEqual((work/'insertion_model/wsclean-MFS-I-model.fits').read_bytes(),b'masked')
            self.assertEqual(fits.getdata(work/'wsclean_model/wsclean-MFS-I-image.fits').shape,(100,100)); self.assertNotEqual(fits.getdata(a['products']['stokes_i']).shape,(100,100))
        self.assertEqual(len(self.casda.calls),3); self.assertTrue(all(len(c)==2 for c in self.casda.calls))
    def test_batch_shares_model_with_outside_range_partner(self):
        first=self.snapshot(); other=catalogue.make_source('Partner',180,-28,self.s,index=99); second=self.snapshot(source=other)
        index={'revision':'r','sources':[first,second]}; self.state.sync_index(index)
        run=controller.plan(self.state,index,settings.freeze(self.s,{}),begin=1,end=1); self.assertTrue(all(len(t['targets'])==2 for t in run['tasks']))
        self.execute(run); self.assertEqual(sum('dstools-create-model' in c for c in self.science.calls),2); self.assertEqual(self.state.export()['status_counts'],{'complete':4})
    def test_parameter_change_skips_success_failed_rerun_keeps_old_results(self):
        run=self.plan(); self.execute(run); task=run['tasks'][0]; target=task['targets'][0]
        original=self.state.target(target['source']['source_id'],task['observation']['key'])['complete_attempt']; path=self.state.attempt(original)['products']['ds']
        self.s['model']['robust']=-1; self.assertFalse(self.plan()['tasks']); rerun=self.plan(mode='rerun'); self.science.fail=True; self.execute(rerun)
        self.assertEqual(self.state.target(target['source']['source_id'],task['observation']['key'])['complete_attempt'],original); self.assertTrue(Path(path).is_file())
    def test_single_bounded_retry_keeps_failed_inputs(self):
        self.science.fail=True; run=self.plan(True); self.execute(run); paths=[Path(t['path']) for task in run['tasks'] for t in task['targets']]
        self.assertFalse(self.plan(True)['tasks']); retry=self.plan(True,'retry'); self.execute(retry)
        self.assertTrue(controller.add_redownload_retries(self.state,retry)); self.assertFalse(controller.add_redownload_retries(self.state,retry)); self.execute(retry)
        self.assertTrue(all(p.exists() for p in paths)); self.assertFalse(self.plan(True,'retry')['tasks'])
    def test_missing_v_is_failure_and_retains_input(self):
        self.science.missing_v=True; run=self.plan(); self.execute(run)
        self.assertEqual(self.state.export()['status_counts'],{'failed':2}); self.assertTrue(all((Path(t['raw_root'])/'raw-ready.json').exists() for t in run['tasks']))
    def test_ms_time_fallback_and_motion_failure(self):
        t=rows(); t['t_min'][:]=np.nan; source=catalogue.make_source('Moving',180,-28,self.s,100,0); snap=self.snapshot(t,source)
        run=self.plan(True,snapshot=snap); self.execute(run); self.assertEqual(self.state.attempt(run['tasks'][0]['targets'][0]['attempt_id'])['time_source'],'MS')
        self.science.ms_time=None; rerun=self.plan(True,'rerun',snap); self.execute(rerun)
        self.assertTrue(all(self.state.attempt(t['attempt_id'])['error_step']=='proper_motion' for task in rerun['tasks'] for t in task['targets']))
    def test_frozen_settings_and_sif_hash(self):
        frozen=settings.freeze(self.s,{}); self.s['model']['iterations']=1; self.assertEqual(frozen['settings']['model']['iterations'],100000)
        Path(self.s['container']).write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'SIF changed'): science.validate_frozen(frozen)
    def test_real_image_option_preflight_contract(self):
        frozen=settings.freeze(self.s,{}); science.preflight(frozen,self.science)
        with self.assertRaisesRegex(RuntimeError,'does not support'): science.preflight(frozen,lambda *a,**k:SimpleNamespace(stdout='',stderr=''))

    def test_worker_uses_actual_job_slots_for_memory_and_preserves_frozen_settings(self):
        self.s['parallel_slots']=4  # Two datasets produce fewer lanes than the configured cap.
        run=self.plan(); expected=copy.deepcopy(run['frozen']); job=self.execute(run)
        slots=job['slots']; mib=__import__('math').ceil(self.s['memory_gb']*1024)//slots
        creates=[c for c in self.science.calls if 'dstools-create-model' in c]
        self.assertTrue(creates)
        for command in creates:
            env=[command[i+1] for i,token in enumerate(command) if token=='--env']
            self.assertIn('SLURM_MEM_PER_NODE='+str(mib),env)
            self.assertEqual(command[command.index('--abs-mem')+1],str(max(1,int(mib/1024*.75))))
        self.assertEqual(run['frozen'],expected)
    def test_uncertain_submission_never_resubmits(self):
        run=self.plan(); job=controller.pack_job(self.state,run)
        with self.assertRaises(OSError): controller.submit_job(self.state,job,lambda *a,**k:(_ for _ in ()).throw(OSError('lost connection')))
        job=self.state.jobs(run['id'])[0]; self.assertEqual(job['status'],'submitting')
        with self.assertRaisesRegex(RuntimeError,'unknown'): controller.reconcile_job(self.state,job)
    def test_sbatch_dynamic_walltime_and_resources(self):
        run=self.plan(); job=controller.pack_job(self.state,run); script,_=controller.write_sbatch(self.state,job)
        subprocess.run(['bash','-n',str(script)],check=True); text=script.read_text(); self.assertIn('--cpus-per-task=16',text); self.assertNotIn('48:00:00',text); self.assertNotIn('CASDA_PASSWORD',text)
    def test_single_settings_use_shared_config_and_explicit_environment(self):
        config=write_settings(self.root/'shared.py',{**self.s,'parallel_slots':2,'begin':20,'end':30})
        args=cli.build_parser(True).parse_args(['run','--config',str(config),'--work-dir',str(self.root/'single'),'--container',self.s['container']])
        with patch.dict(os.environ,{'ASKAP_PARALLEL_SLOTS':'3','ASKAP_WORK_ROOT':'/author'}): result,origin=cli.settings_from_args(args,True)
        self.assertEqual(result['parallel_slots'],3); self.assertEqual(result['begin'],1); self.assertIsNone(result['end'])
        self.assertEqual(result['work_root'],str((self.root/'single').resolve())); self.assertEqual(origin['parallel_slots'],'environment:ASKAP_PARALLEL_SLOTS')
    def test_host_python_is_portable_and_explicit_override_is_preserved(self):
        for single in (False, True):
            command=['run','--work-dir',str(self.root/'portable'),'--container',self.s['container']]
            with self.subTest(single=single), patch.dict(os.environ,{},clear=True):
                values,_=cli.settings_from_args(cli.build_parser(single).parse_args(command),single)
                self.assertEqual(values['python'],sys.executable)
                explicit='/shared/other-environment/bin/python'
                values,origins=cli.settings_from_args(
                    cli.build_parser(single).parse_args(command+['--set','python='+explicit]),single)
                self.assertEqual(values['python'],explicit)
                self.assertEqual(origins['python'],'CLI')
    def test_negative_sexagesimal_cli_and_failed_query(self):
        with patch('askap_auto.cli.query_one',side_effect=RuntimeError('TAP failed')):
            with self.assertRaisesRegex(RuntimeError,'TAP failed'): cli.main(['query','--ra','12:00:00','--dec','-28:00:00','--ra-unit','hourangle','--work-dir',str(self.root/'cli')],single=True)
    def test_transfer_repeat_repair_and_receipts(self):
        self.execute(self.plan()); remote=Remote(); args=SimpleNamespace(remote_products=self.s['product_root'],remote_work=self.s['work_root'],destination=str(self.root/'ada'),source=[])
        self.assertEqual(pull_products.pull(args,remote)['files_transferred'],6); self.state.export()
        self.assertEqual(pull_products.pull(args,remote)['files_transferred'],0)
        record=remote.inventory(self.s['product_root'],self.s['work_root'])[0]; (Path(args.destination)/record['files']['ds']['relative_path']).write_bytes(b'XX')
        self.assertEqual(pull_products.pull(args,remote)['files_transferred'],1)
    def test_receipt_failure_does_not_mark_transfer_complete(self):
        self.execute(self.plan()); remote=Remote(); remote.fail_receipt=True
        args=SimpleNamespace(remote_products=self.s['product_root'],remote_work=self.s['work_root'],destination=str(self.root/'ada'),source=[])
        with self.assertRaisesRegex(RuntimeError,'receipt upload'): pull_products.pull(args,remote)
        import sqlite3
        db=sqlite3.connect(self.root/'ada/state/transfer_ledger.sqlite')
        try: self.assertEqual(db.execute("SELECT count(*) FROM observations WHERE status='complete'").fetchone()[0],0)
        finally: db.close()


    def test_four_slots_estimate_parallel_load_not_serial_sum(self):
        self.s['parallel_slots']=4
        t=rows()[[0]*8]
        for i in range(8):
            t['obs_id'][i]='ASKAP-'+str(100+i)
            t['filename'][i]='science.SB'+str(100+i)+'.beam00.ms.tar'
            t['obs_publisher_did'][i]='ivo:'+str(i)
        run=self.plan(snapshot=self.snapshot(t)); job=controller.pack_job(self.state,run)
        self.assertEqual(job['slots'],4); self.assertEqual(job['walltime_hours'],7)
        self.assertEqual({t['lane'] for t in job['tasks']},{0,1,2,3})

    def test_two_chunks_reuse_retained_master(self):
        self.s.update(parallel_slots=1,walltime_hours=3,overhead_hours=.5)
        first=self.snapshot(); second=self.snapshot(source=catalogue.make_source('Partner',180,-28,self.s,index=2))
        index={'revision':'r','sources':[first,second]}; self.state.sync_index(index)
        run=controller.plan(self.state,index,settings.freeze(self.s,{})); self.assertEqual(len(run['tasks']),4)
        while any(controller.pending(self.state,t) for t in run['tasks']):
            self.execute(run)
        self.assertEqual(sum('dstools-create-model' in c for c in self.science.calls),2)
        self.assertEqual(self.state.export()['status_counts'],{'complete':4})

    def test_fast_worker_and_full_controller_execution(self):
        run=self.plan()
        calls=[]
        def scheduler(command,**kwargs):
            calls.append(command)
            self.assertEqual(command[0],'sbatch')
            manifest=Path(command[-1]).with_suffix('.json'); job=read_json(manifest)
            run_job(job,manifest,executor_factory=lambda f,s,d:science.Executor(f,s,d,self.science),preflight_fn=lambda _: {})
            return SimpleNamespace(stdout='12345\n')
        controller.run_controller(self.state,run,submit=True,runner=scheduler,client_factory=lambda _:self.casda)
        self.assertIsNone(self.state.active_run()); self.assertEqual(len(calls),1)
        self.assertEqual(self.state.export()['status_counts'],{'complete':2})

    def test_walltime_partial_continues_without_recreating_model(self):
        self.s['parallel_slots']=1
        original=self.science
        failed=[False]
        def slow(command,**kwargs):
            if 'dstools-extract-ds' in command and not failed[0]:
                failed[0]=True
                raise subprocess.TimeoutExpired(command,1)
            return original(command,**kwargs)
        run=self.plan(); calls=[]
        def scheduler(command,**kwargs):
            calls.append(command)
            manifest=Path(command[-1]).with_suffix('.json'); job=read_json(manifest)
            run_job(job,manifest,executor_factory=lambda f,s,d:science.Executor(f,s,d,slow),preflight_fn=lambda _: {})
            return SimpleNamespace(stdout=str(12000+len(calls)))
        controller.run_controller(self.state,run,True,runner=scheduler,client_factory=lambda _:self.casda)
        self.assertEqual(len(calls),2); self.assertEqual(self.state.export()['status_counts'],{'complete':2})
        self.assertEqual(sum('dstools-create-model' in c for c in self.science.calls),2)

    def test_failure_exhausts_continuation_limit(self):
        self.s.update(parallel_slots=1,max_continuations=1)
        run=self.plan(); calls=[]
        def scheduler(command,**kwargs):
            calls.append(command); manifest=Path(command[-1]).with_suffix('.json')
            write_json(manifest.with_suffix('.outcome.json'),{'status':'partial'})
            return SimpleNamespace(stdout=str(12000+len(calls)))
        controller.run_controller(self.state,run,True,runner=scheduler,client_factory=lambda _:self.casda)
        self.assertEqual(len(calls),2); self.assertEqual(self.state.export()['status_counts'],{'env_failed':2})

    def test_missing_motion_time_does_not_fail_zero_motion_partner(self):
        t=rows(); t['t_min'][:]=np.nan; self.science.ms_time=None
        moving=catalogue.make_source('Moving',180,-28,self.s,100,0,index=2)
        index={'revision':'r','sources':[self.snapshot(t),self.snapshot(t,moving)]}; self.state.sync_index(index)
        run=controller.plan(self.state,index,settings.freeze(self.s,{})); self.execute(run)
        self.assertEqual(self.state.export()['status_counts'],{'complete':2,'failed':2})

    def test_cleanup_failure_keeps_scientific_success(self):
        run=self.plan(); real=shutil.rmtree
        def remove(path,*args,**kwargs):
            if Path(path).name=='work': raise OSError('cleanup denied')
            return real(path,*args,**kwargs)
        with patch('askap_auto.science.shutil.rmtree',side_effect=remove): self.execute(run)
        self.assertEqual(self.state.export()['status_counts'],{'complete':2})
        self.assertTrue(all(self.state.attempt(t['attempt_id'])['cleanup_error'] for task in run['tasks'] for t in task['targets']))

    def test_run_reads_config_errors_instead_of_reusing_old_settings(self):
        run=self.plan(); config=self.root/'broken.py'; config.write_text('this is invalid Python!')
        with patch('askap_auto.cli.run_controller') as resume:
            with self.assertRaises(SyntaxError):
                cli.main(['run','--work-dir',self.s['work_root'],'--config',str(config),'--submit'])
        resume.assert_not_called(); self.assertEqual(self.state.active_run()['id'],run['id'])

    def test_download_resume_requires_ready_marker_and_deletes_unusable_downloads(self):
        obs=self.snapshot()['observations'][0]; cache=self.snapshot()['cache']; root=self.root/'download'
        marker=archive.download(obs,cache,root,self.s,client_factory=lambda _:self.casda)
        interrupted=root/'download'/'interrupted'; interrupted.mkdir(); (interrupted/obs['filename']).write_bytes(b'half')
        count=len(self.casda.calls); archive.download(obs,cache,root,self.s,client_factory=lambda _:self.casda)
        self.assertEqual(len(self.casda.calls),count); self.assertFalse(interrupted.exists())
        self.assertTrue(Path(marker['archive']).is_file())
        Path(marker['archive']).write_bytes(b'partial')
        fresh=archive.download(obs,cache,root,self.s,client_factory=lambda _:self.casda)
        self.assertEqual(len(self.casda.calls),count+1); self.assertFalse(Path(marker['archive']).parent.exists())
        self.assertEqual([p.name for p in (root/'download').iterdir()],[Path(fresh['archive']).parent.name])
        self.assertTrue(Path(fresh['checksum']).is_file())

    def test_query_resume_uses_per_source_journals(self):
        second=catalogue.make_source('Second',180,-28,self.s,index=2); count=[]
        def interrupted(source,_):
            count.append(source['name'])
            if source['name']=='Second': raise KeyboardInterrupt()
            return rows()
        with self.assertRaises(KeyboardInterrupt): query.query_catalogue([self.source,second],self.s,fetch=interrupted,release_check=lambda _:range(3))
        resumed=[]
        query.query_catalogue([self.source,second],self.s,fetch=lambda source,_:(resumed.append(source['name']) or rows()),release_check=lambda _:range(3))
        self.assertEqual(resumed,['Second'])

    def test_all_beam_duplicate_dataset_ambiguity_and_tie_stability(self):
        t=rows()[[0,0,2]]; t['obs_publisher_did']=['ivo:a','ivo:different','ivo:c']
        obs=query.candidates(t,self.source,self.s,released=range(3))
        with self.assertRaisesRegex(ValueError,'multiple different'): query.select(obs,[100])
        reversed_obs=query.candidates(t[::-1],self.source,self.s,released=range(3))
        self.assertEqual({o['key'] for o in obs if o['default_selected']},{o['key'] for o in reversed_obs if o['default_selected']})

    def test_explicit_null_and_config_precedence(self):
        args=cli.build_parser(True).parse_args(['query','--ra','180','--dec','-28','--work-dir',str(self.root/'single'),'--exposure-min','null'])
        self.assertIsNone(cli.settings_from_args(args,True)[0]['exposure_min'])
        config=write_settings(self.root/'config.py',{**self.s,'parallel_slots':1})
        with patch.dict(os.environ,{'ASKAP_PARALLEL_SLOTS':'2'}):
            result,origin=settings.load(config,settings.dstools_config_path(config),{'parallel_slots':3},use_environment=True)
        self.assertEqual(result['parallel_slots'],3); self.assertEqual(origin['parallel_slots'],'CLI')

    def test_rerun_selection_does_not_expand_successful_partner(self):
        first=self.snapshot(); other=self.snapshot(source=catalogue.make_source('Other',180,-28,self.s,index=99))
        index={'revision':'r','sources':[first,other]}; self.state.sync_index(index)
        run=controller.plan(self.state,index,settings.freeze(self.s,{})); self.execute(run)
        retry=controller.plan(self.state,index,settings.freeze(self.s,{}),mode='rerun',begin=1,end=1)
        self.assertTrue(all(len(t['targets'])==1 and t['targets'][0]['source']['name']=='Star_A' for t in retry['tasks']))

    def test_transfer_source_change_fails_without_receipt(self):
        self.execute(self.plan()); base=Remote()
        class Changed(Remote):
            def inventory(self,products,work):
                value=super().inventory(products,work)
                if hasattr(self,'read'): value[0]['files']['ds']['sha256']='0'*64
                self.read=True
                return value
            def receipt(self,*args): raise AssertionError('Must not issue receipt')
        args=SimpleNamespace(remote_products=self.s['product_root'],remote_work=self.s['work_root'],destination=str(self.root/'ada'),source=[])
        with self.assertRaisesRegex(RuntimeError,'changed during transfer'): pull_products.pull(args,Changed())


    def test_completed_targets_do_not_release_still_running_job(self):
        run=self.plan(); earlier=controller.pack_job(self.state,run); self.execute(run)
        earlier.update(status='submitted',scheduler_id='54321')
        path=self.state.root/'jobs'/(earlier['id']+'.json'); earlier['manifest_path']=str(path)
        write_json(path,earlier); write_json(path.with_suffix('.submission.json'),{'job_id':'54321'})
        self.state.store_job(earlier); calls=[]
        def accounting(command,**kwargs):
            calls.append(command)
            self.assertEqual(command[0],'sacct')
            write_json(path.with_suffix('.outcome.json'),{'status':'finished'})
            return SimpleNamespace(stdout='54321|RUNNING|',returncode=0)
        with patch('askap_auto.controller.time.sleep'):
            controller.run_controller(self.state,run,True,runner=accounting)
        self.assertEqual(len(calls),1)

    def test_frozen_ecsv_change_refuses_download(self):
        run=self.plan(); task=run['tasks'][0]; Path(task['cache']).write_text('changed')
        job=controller.pack_job(self.state,run)
        controller.prepare_job(self.state,job,client_factory=lambda _:self.casda)
        self.assertTrue(any('metadata changed' in (self.state.attempt(t['attempt_id'])['error'] or '') for task in run['tasks'] for t in task['targets']))

    def test_single_progress_counts_execution_selection_not_candidates(self):
        snap=self.snapshot(); self.state.sync_index(snap,single=True)
        self.assertEqual(self.state.export()['known_target_observations'],2)
        run=self.plan(True,snapshot=snap)
        self.assertEqual(self.state.export()['known_target_observations'],3)

    def test_actual_process_runner_times_out_and_success_captures_output(self):
        result=science.run_process([sys.executable,'-c','print("ok")'],stdout=subprocess.PIPE)
        self.assertEqual(result.stdout.strip(),'ok')
        with self.assertRaises(subprocess.TimeoutExpired):
            science.run_process([sys.executable,'-c','import time; time.sleep(10)'],timeout=.05)

    def test_resource_decimal_memory_is_rendered_in_integer_megabytes(self):
        self.s.update(memory_gb=20.5,tmp_gb=10.5)
        job=controller.pack_job(self.state,self.plan()); script,_=controller.write_sbatch(self.state,job)
        self.assertIn('--mem=20992M',script.read_text()); self.assertIn('--tmp=10752M',script.read_text())

    def test_transfer_script_accepts_python37_grammar(self):
        import ast
        ast.parse(Path(pull_products.__file__).read_text(),feature_version=(3,7))
        ast.parse(pull_products.REMOTE_INVENTORY,feature_version=(3,7))


    def test_four_parallel_workers_execute_multiple_rounds(self):
        import threading,time
        self.s['parallel_slots']=4; t=rows()[[0]*8]
        for i in range(8):
            t['obs_id'][i]='ASKAP-'+str(100+i); t['filename'][i]='science.SB'+str(100+i)+'.beam00.ms.tar'; t['obs_publisher_did'][i]='ivo:'+str(i)
        counter={'current':0,'peak':0}; mutex=threading.Lock(); original=self.science
        def measured(command,**kwargs):
            if 'dstools-create-model' not in command: return original(command,**kwargs)
            with mutex:
                counter['current']+=1; counter['peak']=max(counter['peak'],counter['current'])
            try:
                time.sleep(.05)
                return original(command,**kwargs)
            finally:
                with mutex: counter['current']-=1
        self.science=measured; run=self.plan(snapshot=self.snapshot(t)); self.execute(run)
        self.assertEqual(counter['peak'],4); self.assertEqual(self.state.export()['status_counts'],{'complete':8})

    def test_product_root_cannot_mix_workspaces(self):
        settings.freeze(self.s,{})
        other=copy.deepcopy(self.s); other['work_root']=str(self.root/'other')
        with self.assertRaisesRegex(ValueError,'another workspace'): settings.freeze(other,{})

if __name__=='__main__': unittest.main()
