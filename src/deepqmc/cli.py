import importlib
import logging
import shutil
import sys
import time
from functools import partial
from itertools import count
from math import inf
from pathlib import Path

import click
import toml
import tomlkit
import torch
from torch.utils.tensorboard import SummaryWriter

from .defaults import DEEPQMC_MAPPING, collect_kwarg_defaults
from .errors import TrainingCrash
from .evaluate import evaluate
from .molecule import Molecule
from .train import train
from .wf import PauliNet
from .wf.paulinet.omni import OmniSchNet

log = logging.getLogger(__name__)


def import_fullname(fullname):
    module_name, qualname = fullname.split(':')
    module = importlib.import_module(module_name)
    return getattr(module, qualname)


def wf_from_file(workdir):
    params = toml.loads((workdir / 'param.toml').read_text())
    state_file = workdir / 'state.pt'
    state = torch.load(state_file) if state_file.is_file() else None
    pyscf_file = workdir / 'baseline.pyscf'
    system = params.pop('system')
    if isinstance(system, str):
        name, system = system, {}
    else:
        name = system.pop('name')
    if ':' in name:
        mol = import_fullname(name)(**system)
    else:
        mol = Molecule.from_name(name, **system)
    if pyscf_file.is_file():
        mf, mc = pyscf_from_file(pyscf_file)
        # TODO refactor initialisation to avoid duplicate with PauliNet.from_hf
        # TODO as part of that, validate that requested/restored cas/basis match
        wf = PauliNet.from_pyscf(
            mc or mf,
            **{
                'omni_factory': partial(OmniSchNet, **params.pop('omni_kwargs', {})),
                'cusp_correction': True,
                'cusp_electrons': True,
                **params.pop('pauli_kwargs', {}),
            },
        )
        wf.mf = mf
    else:
        wf = PauliNet.from_hf(mol, **params.pop('model_kwargs', {}))
        shutil.copy(wf.mf.chkfile, pyscf_file)
    return wf, params, state


def pyscf_from_file(chkfile):
    import pyscf.gto.mole
    from pyscf import scf, mcscf, lib

    pyscf.gto.mole.float32 = float

    mol = lib.chkfile.load_mol(chkfile)
    mf = scf.RHF(mol)
    mf.__dict__.update(lib.chkfile.load(chkfile, 'scf'))
    mc_dict = lib.chkfile.load(chkfile, 'mcscf')
    if mc_dict:
        mc_dict['ci'] = lib.chkfile.load(chkfile, 'ci')
        mc_dict['nelecas'] = tuple(map(int, lib.chkfile.load(chkfile, 'nelecas')))
        mc = mcscf.CASSCF(mf, 0, 0)
        mc.__dict__.update(mc_dict)
    else:
        mc = None
    return mf, mc


@click.group()
def cli():
    logging.basicConfig(style='{', format='{message}', datefmt='%H:%M:%S')
    logging.getLogger('deepqmc').setLevel(logging.DEBUG)


@cli.command()
@click.option('--commented', '-c', is_flag=True)
def defaults(commented):
    table = tomlkit.table()
    table['model_kwargs'] = collect_kwarg_defaults(PauliNet.from_hf, DEEPQMC_MAPPING)
    table['train_kwargs'] = collect_kwarg_defaults(train, DEEPQMC_MAPPING)
    table['evaluate_kwargs'] = collect_kwarg_defaults(evaluate, DEEPQMC_MAPPING)
    lines = tomlkit.dumps(table).split('\n')
    if commented:
        lines = ['# ' + l if ' = ' in l and l[0] != '#' else l for l in lines]
    click.echo('\n'.join(lines), nl=False)


@cli.command('train')
@click.argument('workdir', type=click.Path(exists=True))
@click.option('--save-every', default=100, show_default=True)
@click.option('--cuda/--no-cuda', default=True)
@click.option('--max-restarts', default=3, show_default=True)
@click.option('--hook', is_flag=True)
def train_at(workdir, save_every, cuda, max_restarts, hook):
    workdir = Path(workdir).resolve()
    if hook:
        sys.path.append(str(workdir))
        import dlqmc_hook  # noqa: F401
    wf, params, state = wf_from_file(workdir)
    if cuda:
        wf.cuda()
    for attempt in range(max_restarts + 1):
        try:
            train(
                wf,
                workdir=workdir,
                state=state,
                save_every=save_every,
                **params.get('train_kwargs', {}),
            )
        except TrainingCrash as e:
            log.warning(f'Caught exception: {e.__cause__!r}')
            if attempt == max_restarts:
                log.error('Maximum number of restarts reached')
                break
            state = e.state
            if state:
                log.warning(f'Restarting from step {state["step"]}')
            else:
                log.warning('Restarting from beginning')
        else:
            break


@cli.command('train-multi')
@click.argument('workdir', type=click.Path(exists=True))
@click.argument('respawn', type=int)
@click.option('--multi-part', default=0)
@click.option('--timeout', default=30 * 60)
@click.option('--check-interval', default=30)
@click.option('--cuda/--no-cuda', default=True)
@click.option('--max-restarts', default=3, show_default=True)
@click.option('--hook', is_flag=True)
def train_multi_at(
    workdir, respawn, multi_part, timeout, check_interval, cuda, max_restarts, hook
):
    workdir = Path(workdir).resolve()
    rank = int(workdir.parts[::-1][multi_part])
    if hook:
        sys.path.append(str(workdir))
        import dlqmc_hook  # noqa: F401
    wf, params, state = wf_from_file(workdir)
    if cuda:
        wf.cuda()
    for cycle in count():
        end_step = (cycle + 1) * respawn
        for attempt in range(max_restarts + 1):
            try:
                interrupted = train(
                    wf,
                    workdir=workdir,
                    state=state,
                    save_every=respawn,
                    return_every=respawn,
                    blowup_threshold=inf,
                    min_rewind=5,
                    **params.get('train_kwargs', {}),
                )
            except TrainingCrash as e:
                log.warning(f'Caught exception: {e.__cause__!r}')
                state = e.state
                if (
                    attempt == max_restarts
                    or not state
                    or state['step'] < cycle * respawn
                ):
                    log.warning('Aborting cycle')
                    (workdir / 'chkpts' / f'state-{end_step:05d}.STOP').touch()
                    interrupted = True
                    break
                log.warning(f'Restarting from step {state["step"]}')
            else:
                break
        if not interrupted:
            return
        start = time.time()
        while True:
            now = time.time()
            if now - start > timeout:
                log.error('Timeout reached, aborting')
                return
            root = workdir.parents[multi_part]
            stem = ('*',) + workdir.parts[::-1][:multi_part]
            root.glob('/'.join(stem + ('param.toml',)))
            n_tasks = len(list(root.glob('/'.join(stem + ('param.toml',)))))
            all_states = {
                int(p.parts[-3 - multi_part]): p
                for p in root.glob(
                    '/'.join(stem + (f'chkpts/state-{end_step:05d}.pt',))
                )
            }
            all_stops = {
                int(p.parts[-3 - multi_part]): None
                for p in root.glob(
                    '/'.join(stem + (f'chkpts/state-{end_step:05d}.STOP',))
                )
            }
            all_states = {**all_states, **all_stops}
            if len(all_states) < n_tasks:
                log.info(f'Missing {n_tasks - len(all_states)} states')
                time.sleep(check_interval)
                continue
            all_states = [(p, torch.load(p)) for p in all_states.values() if p]
            log.info(f'Have {len(all_states)} states for respawning')
            all_states.sort(key=lambda x: x[1]['monitor'].energy)
            all_states = all_states[: n_tasks // 2]
            path, state = all_states[rank % len(all_states)]
            log.info(f'Respawning from {path}')
            break


@cli.command('evaluate')
@click.argument('workdir', type=click.Path(exists=True))
@click.option('--cuda/--no-cuda', default=True)
@click.option('--store-steps/--no-store-steps', default=False)
@click.option('--hook', is_flag=True)
def evaluate_at(workdir, cuda, store_steps, hook):
    workdir = Path(workdir).resolve()
    if hook:
        sys.path.append(str(workdir))
        import dlqmc_hook  # noqa: F401
    wf, params, state = wf_from_file(workdir)
    if state:
        wf.load_state_dict(state['wf'])
    if cuda:
        wf.cuda()
    evaluate(
        wf,
        store_steps=store_steps,
        workdir=workdir,
        **params.get('evaluate_kwargs', {}),
    )


def get_status(path):
    path = Path(path)
    with path.open() as f:
        lines = f.readlines()
    line = ''
    restarts = 0
    for l in lines:
        if 'E=' in l:
            line = l
        elif 'Restarting' in l:
            restarts += 1
    modtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(path.stat().st_mtime),)
    return {'modtime': modtime, 'restarts': restarts, 'line': line.strip()}


def get_status_multi(paths):
    for path in sorted(paths):
        p = Path(path)
        yield {'path': p.parent, **get_status(p)}


@cli.command()
@click.argument('paths', nargs=-1, type=click.Path(exists=True, dir_okay=False))
def status(paths):
    for x in get_status_multi(paths):
        click.echo('{line} -- {modtime}, restarts: {restarts} | {path}'.format_map(x))


@cli.command()
@click.argument('basedir', type=click.Path(exists=False))
@click.argument('HF', type=float)
@click.argument('exact', type=float)
@click.option('--fractions', default='0,90,99,100', type=str)
@click.option('--steps', '-n', default=2_000, type=int)
def draw_hlines(basedir, hf, exact, fractions, steps):
    basedir = Path(basedir)
    fractions = [float(x) / 100 for x in fractions.split(',')]
    for fraction in fractions:
        value = hf + fraction * (exact - hf)
        workdir = basedir / f'line-{value:.3f}'
        with SummaryWriter(log_dir=workdir, flush_secs=15, purge_step=0) as writer:
            for step in range(steps):
                writer.add_scalar('E_loc_loss/mean', value, step)
                writer.add_scalar('E_loc/mean', value, step)
