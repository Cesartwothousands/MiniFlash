#!/usr/bin/env python3
"""Hash-bound fixed-QASM four-fabric campaign; run with this checkout's Python."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import shlex
import signal
import statistics as st
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MODES = ('auto','backside','in_place','in_place_joint')
METHODS = ('ncf_upstream_cyclosynth','ncf_legacy','ncf_play_lattice_frozen',
           'ncf_play_lattice_tket','frame_pivot_window_hybrid_priority_tpar','ncf_play_lattice_local')
HERE = Path(__file__).resolve().parent
SCHEMA = 'miniflash_ncf_four_fabrics_v1'


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, obj):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp.{os.getpid()}')
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True) + '\n')
    tmp.replace(path)


def bound(ref):
    path = Path(ref['path'])
    if sha(path) != ref['sha256']: raise ValueError(f'input SHA mismatch: {path}')
    return path


def identity():
    files = [ROOT/'main.py', *sorted((ROOT/'miniflash').glob('*.py')), *sorted(HERE.glob('*.py')), *sorted(HERE.glob('*.json')), *sorted(HERE.glob('*.lock')),
             *sorted((ROOT/'lassynth').rglob('*.py'))]
    return {str(p.relative_to(ROOT)):sha(p) for p in files}


def package_identity():
    return dict(sorted((d.metadata["Name"].lower(), d.version) for d in distributions()))


def verify_install():
    import miniflash
    if Path(miniflash.__file__).resolve().parent != ROOT/'miniflash':
        raise RuntimeError('wrong miniflash import root')
    if Path(sys.prefix).resolve() != ROOT/'.venv':
        raise RuntimeError('use the independent miniflash/.venv/bin/python')
    seeds = read(HERE/'seed_manifest.json')
    if len(seeds)!=469: raise ValueError('expected 469 seed cells')
    for name,digest in seeds.items():
        if sha(ROOT/'.miniflash-cache'/name)!=digest: raise ValueError('seed cache mismatch')
    if len(list((ROOT/'.miniflash-cache').glob('*.json'))) !=469:
        raise ValueError('unexpected extra cache cells')


def normalize(source, target):
    from qiskit import QuantumCircuit, qasm2
    circuit = qasm2.load(source,custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
    if circuit.num_clbits: raise ValueError('classical bits are not accepted')
    qc = QuantumCircuit(circuit.num_qubits)
    allowed = {'h','x','y','z','s','sdg','t','tdg','cx','cz'}
    for inst in circuit.data:
        op = inst.operation
        qs = [circuit.find_bit(q).index for q in inst.qubits]
        if getattr(op,'condition',None) is not None or inst.clbits:
            raise ValueError('conditional operation')
        if op.name in ('id','barrier'): continue
        if op.name in ('sx','sxdg'):
            qc.h(qs[0]);getattr(qc,'s' if op.name=='sx' else 'sdg')(qs[0]);qc.h(qs[0])
        elif op.name=='swap':
            qc.cx(*qs);qc.cx(*qs[::-1]);qc.cx(*qs)
        elif op.name in allowed:
            qc.append(op,qs)
        else: raise ValueError(f'non Clifford+T gate: {op.name}')
    Path(target).write_text(qasm2.dumps(qc)+'\n')
    return {'qubits':qc.num_qubits,'t_count':sum(i.operation.name in ('t','tdg') for i in qc.data),
            'gate_count':len(qc.data),'sha256':sha(target),'path':str(Path(target).resolve())}


def prepare(args):
    verify_install()
    run = args.run.resolve()
    if run.exists(): raise ValueError('new run directory required')
    source = args.source.resolve()
    report = args.rows.resolve()
    rows = list(csv.DictReader(report.open()))
    grouped = {m:{r['benchmark']:r for r in rows if r['method']==m and r['postprocess_status']=='ok'} for m in METHODS}
    common = sorted(set.intersection(*(set(d) for d in grouped.values())))
    if len(common)!=106: raise ValueError(f'expected 106 common circuits, found {len(common)}')
    source_manifest_hash=sha(source/'manifest.json')
    if source_manifest_hash!='d98df616645cd9f21685417a1c6cea186656001065240bad65080dc1a3a46f0a':
        raise ValueError('unexpected source campaign')
    tasks=[]
    run.mkdir(parents=True)
    for bench in common:
        for method in METHODS:
            row=grouped[method][bench]; old_id=int(row['task_id'])
            status_path=source/'status'/f'{old_id:04d}.json'
            status=read(status_path)
            checkpoint_path=bound({'path':status['checkpoint_path'],'sha256':status['checkpoint_sha256']})
            checkpoint=read(checkpoint_path)
            post_path=bound(checkpoint['phases']['postprocess']); post=read(post_path)
            qasm=bound(post['circuit']['qasm'])
            if status['benchmark']!=bench or status['method']!=method or status['manifest_sha256']!=source_manifest_hash:
                raise ValueError('source status identity mismatch')
            if post['classification']!='ok' or post['manifest_sha256']!=source_manifest_hash:
                raise ValueError('postprocess not a successful source-bound result')
            mf_phase=read(bound(checkpoint['phases']['miniflash']))
            mf_path=bound(mf_phase['lattice']); mf=read(mf_path)
            if mf['input']['qasm_sha256']!=sha(qasm): raise ValueError('historical MiniFlash input differs')
            index=len(tasks)+1
            unit=run/'inputs'/f'{index:04d}';unit.mkdir(parents=True)
            original=unit/'original.qasm';original.write_bytes(qasm.read_bytes())
            if sha(original)!=post['circuit']['qasm']['sha256']:raise ValueError('source QASM changed during snapshot')
            normalized=normalize(original,unit/'normalized.qasm')
            original.chmod(0o444);Path(normalized['path']).chmod(0o444)
            tasks.append({'task_id':index,'source_task_id':old_id,'benchmark':bench,'method':method,
                          'reference_only':method=='ncf_upstream_cyclosynth','input':normalized,
                          'source_qasm':post['circuit']['qasm'],'original_sha256':sha(original),
                          'source_status':{'path':str(status_path),'sha256':sha(status_path)},
                          'source_checkpoint':{'path':str(checkpoint_path),'sha256':sha(checkpoint_path)},
                          'source_postprocess':{'path':str(post_path),'sha256':sha(post_path)},
                          'historical_lattice':{'path':str(mf_path),'sha256':sha(mf_path)},
                          'historical_physical_qubit_seconds':float(row['miniflash_volume']),
                          'historical_elapsed_s':float(row['miniflash_elapsed_s']),
                          'factories':max(1,(normalized['qubits']+1)//2)})
            if len(tasks)%50==0: print(f'validated and normalized {len(tasks)}/636 inputs',flush=True)
    if len(tasks)!=636:raise ValueError('incomplete corpus')
    manifest={'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),'source_manifest_sha256':source_manifest_hash,
              'selection_rows':{'path':str(report),'sha256':sha(report)},'cases':common,'methods':METHODS,'modes':MODES,
              'task_count':len(tasks),'outcome_count':len(tasks)*len(MODES),'tasks':tasks,
              'source_files':identity(),'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'package_identity':package_identity(),
              'package_versions':subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True),
              'protocol':{'timeout_s':7200,'region_budget_s':3600,'factory':'15-to-1','factory_size':[6,2],
                          'factory_period':11,'planner_candidates':8,'use_sat':False,'distance':15,'cycle_s':1e-6,
                          'buffer_capacity_per_factory':1,'synthesis_epsilon_of_existing_inputs':1e-8,
                          'resource_model':'allocated_chip_rectangle_v1; auto is legacy reference only'}}
    write(run/'manifest.json',manifest)
    (run/'manifest.json').chmod(0o444)
    print(json.dumps({'run':str(run),'tasks':len(tasks),'outcomes':len(tasks)*4}),flush=True)


def fork(args):
    """Reuse immutable input artifacts in a new, independently bound run."""
    old = read(args.reuse / 'manifest.json')
    run = args.run.resolve()
    if run.exists(): raise ValueError('new run directory required')
    verify_install()
    for task in old['tasks']: bound(task['input'])
    run.mkdir(parents=True)
    old.update(source_files=identity(), package_identity=package_identity(),
               package_versions=subprocess.check_output([sys.executable,"-m","pip","freeze"],text=True), created_utc=datetime.now(timezone.utc).isoformat(),
               git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
               reused_input_manifest={'path':str((args.reuse/'manifest.json').resolve()),'sha256':sha(args.reuse/'manifest.json')})
    write(run/'manifest.json',old)
    (run/'manifest.json').chmod(0o444)
    print(str(run),flush=True)


def load_manifest(run):
    manifest=read(run/'manifest.json')
    if identity()!=manifest['source_files']:raise ValueError('worker source changed after preparation')
    verify_install()
    if package_identity()!=manifest['package_identity']:raise ValueError('Python package versions changed after preparation')
    return manifest


def one(args):
    # Isolated subprocess: a timeout kills this process and any descendants.
    run=args.run.resolve();manifest=load_manifest(run);task=manifest['tasks'][args.task-1]
    source=bound(task['input'])
    from partition_hotfix import install
    hotfix=install()
    from main import compile
    from miniflash import write_gltf
    start=time.monotonic()
    program=compile(source,cache_dir=ROOT/'.miniflash-cache',factory_mode=args.mode,
                    factories=task['factories'],factory='15-to-1',use_sat=False,
                    budget_s=manifest['protocol']['region_budget_s'],planner_candidates=8)
    stats=program.stats()
    if stats.get('t_count',0)!=task['input']['t_count']:raise ValueError('T count mismatch')
    if args.mode=='auto':
        duration=stats['bbox'][2]/2
        legacy=(stats.get('compute_volume',stats['volume'])/8 + (task['factories'] if task['input']['t_count'] else 0)*12*duration)*0.00675
        stats.update(factory_mode='auto',geometry_validated=False,resource_model='legacy_envelope_proxy',
                     legacy_physical_qubit_seconds=legacy,runtime_steps=duration,
                     reserved_factory_count=task['factories'] if task['input']['t_count'] else 0)
    output={'schema':SCHEMA,'status':'ok','mode':args.mode,'task_id':args.task,'input_sha256':sha(source),
            'manifest_sha256':sha(run/'manifest.json'),'elapsed_s':time.monotonic()-start,'stats':stats,'partition_hotfix':hotfix}
    target=run/'units'/f'{args.task:04d}'/f'{args.mode}.json'
    if args.render:
        from plot_layout import plot_layout
        plot_layout(program,target.with_suffix('.png'))
        write_gltf(program,target.with_suffix('.gltf'))
        if program.physical_layout:write(target.with_suffix('.layout.json'),program.physical_layout)
    write(target,output)


def worker(args):
    run=args.run.resolve();manifest=load_manifest(run);task=manifest['tasks'][args.task-1]
    unit=run/'units'/f'{args.task:04d}';unit.mkdir(parents=True,exist_ok=True)
    statuses={}
    for mode in args.modes:
        target=unit/f'{mode}.json'
        if target.exists():
            old=read(target)
            if old.get('manifest_sha256')==sha(run/'manifest.json') and old.get('input_sha256')==task['input']['sha256'] and old.get('status') in ('ok','timeout','infeasible'):
                statuses[mode]=old['status'];continue
        command=[sys.executable,str(Path(__file__).resolve()),'one','--run',str(run),'--task',str(args.task),'--mode',mode]
        if args.render:command.append('--render')
        started=time.monotonic()
        with (unit/f'{mode}.log').open('a') as log:
            proc=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True,cwd=ROOT,
                                  env={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','PYTHONHASHSEED':'0'})
            status='failed'
            try:
                code=proc.wait(timeout=manifest['protocol']['timeout_s'])
                if code==0 and target.exists():status='ok'
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait();status='timeout'
        if status!='ok':
            write(target,{'schema':SCHEMA,'status':status,'mode':mode,'task_id':args.task,
                          'input_sha256':task['input']['sha256'],'manifest_sha256':sha(run/'manifest.json'),
                          'elapsed_s':time.monotonic()-started,'log':str(unit/f'{mode}.log')})
        statuses[mode]=status
        write(run/'status'/f'{args.task}.json',{'task_id':args.task,'modes':statuses,'completed':len(statuses)==4})
    return 0 if all(v in ('ok','timeout','infeasible') for v in statuses.values()) else 1


def aggregate(args):
    run=args.run.resolve();manifest=load_manifest(run);manifest_sha=sha(run/'manifest.json')
    rows=[];counts=Counter();paired=defaultdict(list);allpaired=defaultdict(list)
    for task in manifest['tasks']:
        outcomes={}
        for mode in MODES:
            path=run/'units'/f"{task['task_id']:04d}"/f'{mode}.json'
            obj=read(path) if path.exists() else {'status':'missing'}
            if path.exists() and (obj.get('manifest_sha256')!=manifest_sha or obj.get('input_sha256')!=task['input']['sha256']):
                raise ValueError(f'stale result: {path}')
            status=obj['status'];stats=obj.get('stats',{});counts[f'{mode}:{status}']+=1
            row={'benchmark':task['benchmark'],'method':task['method'],'reference_only':task['reference_only'],
                 'task_id':task['task_id'],'mode':mode,'status':status,'elapsed_s':obj.get('elapsed_s'),
                 **{k:stats.get(k) for k in ('resource_model','physical_qubit_seconds','legacy_physical_qubit_seconds','runtime_steps','allocated_area_patches',
                                            'reserved_factory_count','enabled_factory_count','used_factory_count','peak_concurrent_injections','wait_levels')}}
            rows.append(row);outcomes[mode]=row
        if all(outcomes[m]['status']=='ok' for m in MODES):allpaired[task['method']].append(outcomes)
        if all(outcomes[m]['status']=='ok' for m in MODES[1:]):paired[task['method']].append(outcomes)
    summary={'schema':SCHEMA,'counts':dict(counts),'task_count':636,'outcome_count':2544,
             'complete':not any(k.endswith(':missing') for k in counts),'hard_failure':any(k.endswith((':failed',':missing')) for k in counts),
             'physical_comparison':'three explicit fabrics only; legacy auto is a different-model reference',
             'methods':{}}
    for method in METHODS:
        selected=paired[method]
        summary['methods'][method]={'three_fabric_paired_n':len(selected),'four_mode_paired_n':len(allpaired[method]),
          'arithmetic_means':{m:{metric:st.fmean(float(x[m][metric]) for x in selected) if selected else None
                               for metric in ('physical_qubit_seconds','runtime_steps','allocated_area_patches','peak_concurrent_injections')}
                              for m in MODES[1:]},
          'mean_paired_ratio_to_backside':{m:st.fmean(x[m]['physical_qubit_seconds']/x['backside']['physical_qubit_seconds'] for x in selected) if selected else None for m in MODES[2:]}}
    with (run/'results.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    write(run/'summary.json',summary)
    lines=['# Fixed-QASM factory placement comparison','',f"Outcomes: {sum(v for k,v in counts.items() if not k.endswith(':missing'))}/2544.",
           '', 'Auto is the unmodified historical geometry model. Physical comparisons below use the three explicit fabrics and identical reserved factory budgets.',
           '', '| Method | Paired n | In-place / backside | Joint / backside |','|---|---:|---:|---:|']
    for m,d in summary['methods'].items():
        ratios=d['mean_paired_ratio_to_backside'];fmt=lambda x:'NA' if x is None else f'{x:.6g}'
        lines.append(f"| {m} | {d['three_fabric_paired_n']} | {fmt(ratios['in_place'])} | {fmt(ratios['in_place_joint'])} |")
    lines+=['','All aggregates are arithmetic means on the reported common successful subset. Timeouts and failures remain in results.csv.','', '```json',json.dumps(dict(counts),indent=2),'```']
    (run/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary['counts']),flush=True)


def submit(args):
    run=args.run.resolve();manifest=load_manifest(run)
    indices=args.indices or f'1-{len(manifest["tasks"])}'
    logdir=run/'slurm_logs';logdir.mkdir(exist_ok=True)
    (run/'units').mkdir(exist_ok=True)
    (run/'status').mkdir(exist_ok=True)
    shell=run/f'worker_{int(time.time())}.sh'
    shell.write_text('#!/bin/bash\nset -euo pipefail\ncd '+shlex.quote(str(ROOT))+'\nexec '+shlex.join([sys.executable,str(Path(__file__).resolve()),'worker','--run',str(run),'--task'])+' "$SLURM_ARRAY_TASK_ID"\n')
    result=subprocess.check_output(['sbatch','--parsable','--cpus-per-task=1','--mem=32G','--time=08:15:00',
                                    '--job-name=mf_fabrics','--array='+indices+'%636','--output='+str(logdir/'%A_%a.out'),
                                    '--error='+str(logdir/'%A_%a.err'),
                                    *(['--dependency=afterok:'+args.afterok] if args.afterok else []),str(shell)],text=True).strip().split(';')[0]
    previous=read(run/'submission.json') if (run/'submission.json').exists() else {'array_job_ids':[]}
    previous['array_job_ids'].append(result);previous.update(run_dir=str(run),task_count=636,submitted_utc=datetime.now(timezone.utc).isoformat())
    finalscript=run/f'finalize_{result}.sh'
    finalscript.write_text('#!/bin/bash\nset -euo pipefail\ncd '+shlex.quote(str(ROOT))+'\nexec '+shlex.join([sys.executable,str(Path(__file__).resolve()),'aggregate','--run',str(run)])+'\n')
    final=subprocess.check_output(['sbatch','--parsable','--dependency=afterany:'+result,'--job-name=mf_finalize','--cpus-per-task=1','--mem=4G','--time=00:30:00','--output='+str(logdir/'finalize_%j.out'),str(finalscript)],text=True).strip().split(';')[0]
    previous['finalizer_job_id']=final;write(run/'submission.json',previous)
    # Monitor compute through Slurm, use tmux only for lightweight supervision.
    session='mf_monitor_'+run.name[-35:]
    command=shlex.join([sys.executable,str(Path(__file__).resolve()),'monitor','--run',str(run)])
    subprocess.run(['tmux','new-session','-d','-s',session,command],check=False)
    print(json.dumps(previous),flush=True)


def monitor(args):
    run=args.run.resolve()
    while True:
        submission=read(run/'submission.json')
        ids=','.join(submission['array_job_ids']+[submission['finalizer_job_id']])
        queue=subprocess.run(['squeue','-h','-j',ids,'-o','%i %T %R'],capture_output=True,text=True)
        accounting=subprocess.run(['sacct','-n','-X','-j',ids,'--format=JobID,State,Elapsed,MaxRSS'],capture_output=True,text=True)
        statuses=Counter()
        for p in (run/'units').glob('*/*.json'):
            if p.stem in MODES:
                try:statuses[read(p)['status']]+=1
                except (ValueError,KeyError):statuses['unreadable']+=1
        record={'utc':datetime.now(timezone.utc).isoformat(),'counts':dict(statuses),'squeue':queue.stdout,'sacct':accounting.stdout}
        with (run/'monitor.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
        write(run/'monitor_status.json',record)
        if queue.returncode != 0:
            time.sleep(240)
            continue
        if not queue.stdout.strip():
            aggregate(args)
            return
        time.sleep(240)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for name in ('prepare','fork','one','worker','aggregate','submit','monitor'):
        p=sub.add_parser(name);p.add_argument('--run',required=True,type=Path)
        if name=='fork':p.add_argument('--reuse',required=True,type=Path)
        if name=='prepare':
            p.add_argument('--source',type=Path,default=ROOT.parent/'NCF/experiments/results/formal12_lattice/20260908_130104_ea7277b7')
            p.add_argument('--rows',type=Path,default=ROOT.parent/'NCF/docs/results/formal12_active145_20260911/rows.csv')
        if name in ('one','worker'):
            p.add_argument('--task',required=True,type=int);p.add_argument('--render',action='store_true')
        if name=='one':p.add_argument('--mode',choices=MODES,required=True)
        if name=='worker':p.add_argument('--modes',nargs='+',choices=MODES,default=MODES)
        if name=='submit':
            p.add_argument('--indices')
            p.add_argument('--afterok',help='wait for this preflight array to succeed')
    args=parser.parse_args()
    result=globals()[args.action](args)
    return result or 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        traceback.print_exc();sys.exit(1)
