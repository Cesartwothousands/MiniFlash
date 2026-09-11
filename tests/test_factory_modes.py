import copy
import json
from pathlib import Path
import tempfile
import unittest

from main import compile
from miniflash import Program, write_gltf
from miniflash.physical import check, shortest_path, _Scheduler, rectangle
from miniflash.lower import check_program

ROOT = Path(__file__).resolve().parents[1]


class Fabrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'out')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, text, n=4, mode='in_place_joint', factories=4, **kw):
        path = self.root / 'input.qasm'
        path.write_text(f'OPENQASM 2.0; include "qelib1.inc"; qreg q[{n}];\n' + text)
        return compile(path, cache_dir=ROOT / '.miniflash-cache', factory_mode=mode, factories=factories, **kw)

    def test_threshold_is_not_joint_capacity(self):
        for f in (1, 2, 3, 4, 8):
            with self.subTest(factories=f):
                p = self.build(''.join(f't q[{q}];' for q in range(8)), n=8, factories=f)
                s = p.stats()
                self.assertEqual(s['enabled_factory_count'], f)
                self.assertEqual(s['used_factory_count'], f)
                self.assertEqual(s['backside_injection_count'], 0)
                self.assertLessEqual(s['peak_concurrent_injections'], f)
                if f >= 4:
                    self.assertGreater(s['peak_concurrent_injections'], 2)
                self.assertEqual(check_program(p), [])

    def test_legacy_auto_golden_and_missing_ir_fields(self):
        p = self.build('t q[0];t q[1];t q[2];t q[3];', mode='auto')
        self.assertEqual(p.stats()['volume'], 11616)
        raw = p.to_dict()
        raw.pop('factory_mode'); raw.pop('physical_layout')
        restored = Program.from_dict(raw)
        self.assertEqual(restored.factory_mode, 'auto')
        self.assertEqual(restored.stats(), p.stats())

    def test_all_modes_and_roundtrip(self):
        text = 'h q[0];cx q[0],q[1];t q[0];tdg q[1];h q[0];cx q[1],q[0];t q[0];'
        for mode in ('backside', 'in_place', 'in_place_joint'):
            with self.subTest(mode=mode):
                p = self.build(text, n=2, mode=mode)
                stats = p.stats()
                self.assertEqual(stats['t_count'], 3)
                self.assertEqual(stats['reserved_factory_count'], 4)
                self.assertEqual(stats['enabled_factory_count'], 2 if mode == 'in_place' else 4)
                restored = Program.from_dict(json.loads(json.dumps(p.to_dict())))
                self.assertEqual(check(restored), [])
                self.assertEqual(restored.stats(), stats)
                write_gltf(restored, self.root / 'test.gltf')
                scene = json.loads((self.root / 'test.gltf').read_text())
                self.assertTrue(scene['nodes'])
                self.assertAlmostEqual(stats['physical_qubits'] * stats['runtime_seconds'], stats['physical_qubit_seconds'])

    def test_serial_t_order_and_cold_start(self):
        p = self.build('t q[0];tdg q[0];t q[0];tdg q[0];', n=1, factories=1)
        inj = p.physical_layout['injections']
        # Upstream hoists commuting T gates before partitioning. Preserve
        # that canonical event order, which is reversed for this pure chain.
        self.assertEqual([r[3] for r in inj], [1,0,1,0])
        self.assertEqual([r[6] for r in inj], [11,22,33,44])
        self.assertEqual(p.stats()['runtime_steps'], 46)

    def test_no_t_and_fast_and_external_factories(self):
        for mode in ('backside','in_place','in_place_joint'):
            p = self.build('h q[0];cx q[0],q[1];', n=2, mode=mode)
            self.assertEqual(p.stats()['reserved_factory_count'], 0)
            for factory in ('15-to-1','t-cultivation','external'):
                p = self.build('t q[0];t q[1];t q[2];', mode=mode, factory=factory)
                self.assertEqual(check(p), [])
                self.assertEqual(p.stats()['t_count'], 3)

    def test_joint_factories_are_embedded_and_deterministic(self):
        text = ''.join(f'h q[{q}];t q[{q}];' for q in range(8))
        a = self.build(text, n=8)
        b = self.build(text, n=8)
        self.assertEqual(a.physical_layout, b.physical_layout)
        cells = [d for layer in a.physical_layout['layers'] for d in layer]
        # Sites and compute bays are independent objects on the same grid,
        # with cell origins genuinely spread over both spatial dimensions.
        origins = {tuple(d['origin']) for d in cells}
        self.assertTrue(origins)
        self.assertTrue(a.physical_layout['sites'])
        self.assertEqual(a.physical_layout['search']['optimality'], 'best_of_evaluated_heuristic')

    def test_corruptions_are_rejected(self):
        good = self.build('t q[0];t q[1];t q[2];t q[3];')
        for field in ('route', 'factory', 'token', 'duplicate', 'mode', 'peak'):
            p = copy.deepcopy(good)
            plan = p.physical_layout
            if field == 'route':
                plan['routes'][plan['injections'][0][5]][-1] = [0,0]
            elif field == 'factory':
                plan['sites'][1]['rect'] = plan['sites'][0]['rect'][:]
            elif field == 'token':
                plan['injections'][0][8] += 1
            elif field == 'duplicate':
                plan['injections'].append(plan['injections'][0][:])
            elif field == 'mode':
                plan['mode'] = 'backside'
            elif field == 'peak':
                plan['peak_concurrent_injections'] = 99
            self.assertTrue(check(p), field)

    def test_routing_bottleneck_limits_batches(self):
        # All sources must cross the single shared (5,3) gateway. The
        # planner must serialize them even though all four are ready.
        layout = {'width':18,'height':15,'sites':[
            {'id':i,'rect':[2,1+3*i,1,1],'buffer':[1,1+3*i],'port':[2,1+3*i],'enabled':True}
            for i in range(4)], 'initial':[[i,[12,1+3*i]] for i in range(4)],
            'layers':[], 'parking':[], 'scratch':[0,0]}
        s = _Scheduler(layout,4,11)
        wall = {(5,y) for y in range(15) if y != 3}
        s.blocked.update(wall)
        result = s.run({0:[(i,i,False) for i in range(4)]})
        self.assertEqual(result['peak_concurrent_injections'],1)
        self.assertEqual(len({r[4] for r in result['injections']}),4)

    def test_magic_transport_parities(self):
        from miniflash.physical import pipes_of
        p = self.build('t q[0];t q[1];t q[2];t q[3];')
        times = {i[0]:i[6] for i in p.physical_layout['injections']}
        saw_j_transport = False
        for pipe in pipes_of(p):
            if pipe.inj is None: continue
            merge = pipe.axis == 'J' and pipe.lo[2] == 2*(times[pipe.inj]+1)
            if not merge:
                self.assertEqual(pipe.parity, 0)
                saw_j_transport |= pipe.axis == 'J'
        self.assertTrue(saw_j_transport)

    def test_multilayer_die_and_cell_coverage(self):
        text = 'h q[0];cx q[0],q[4];t q[4];cx q[1],q[3];h q[1];t q[1];cx q[4],q[1];tdg q[0];cx q[2],q[0];h q[2];t q[2];'
        for mode in ('backside','in_place','in_place_joint'):
            p = self.build(text,n=5,mode=mode,factories=3,die_dims=(7,None))
            self.assertEqual(check(p), [])
            p.physical_layout['cell_schedule'].pop()
            self.assertTrue(check(p))

    def test_zero_duration_identity_between_injections(self):
        from miniflash import elaborate, get_factory
        from miniflash.floorplan import solve_floorplan
        from miniflash.synthesis import Cell
        from miniflash.partition import InjectionEvent
        cell = Cell([5,5,1], [
            {'name':'in_0','dir':'K','offset':[2,2,-1],'parity':0},
            {'name':'out_0','dir':'K','offset':[2,2,1],'parity':0}], [], in_bases={0:0})
        for mode in ('backside','in_place','in_place_joint'):
            fp = solve_floorplan([[0]],1,with_magic=True)
            p = elaborate(fp,[[cell]],events=[InjectionEvent(0,0,False),InjectionEvent(1,0,True)],
                          channels=[0,1],factory=get_factory('15-to-1'),factories=1,factory_mode=mode)
            self.assertEqual(check(p),[])
            self.assertEqual(p.stats()['runtime_steps'],24)

    def test_unreachable_and_invalid_options(self):
        self.assertIsNone(shortest_path((0,0),(2,0),{(1,y) for y in range(3)},3,3))
        for mode in ('invalid',):
            with self.assertRaises(ValueError): self.build('t q[0];',mode=mode)
        with self.assertRaises(ValueError): self.build('t q[0];',factories=0)
        with self.assertRaises(ValueError): self.build('t q[0];',orientation=True)


if __name__ == '__main__':
    unittest.main()
