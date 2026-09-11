Explicit factory placement modes
===============================

``--factory-mode`` selects a placement and delivery policy. Its default is
``auto``, which preserves the upstream compiler. The three explicit modes
produce a checked, serialized physical fabric, rather than selecting a
crossbar by the size of an injection group.

.. list-table::
   :header-rows: 1

   * - Mode
     - Placement
     - Enabled sources
   * - ``auto``
     - Original side/backside decision and original accounting
     - Original behavior
   * - ``backside``
     - Dedicated factory bank beyond the data band, with separated output rows
     - All reserved factories
   * - ``in_place``
     - Separate factory sites to the two sides of the data band
     - First two factories, or one when only one is reserved
   * - ``in_place_joint``
     - Internal factory islands interleaved with compute and parking bays
     - All reserved factories

The limited in-place mode deliberately limits enabled sources, not the
reserved hardware budget. Additional reserved sites remain idle and occupy
area. Joint mode has no two-injection cardinality limit: independently routed
paths can execute concurrently, while paths sharing a corridor execute in
separate rounds. It never falls back to the backside bank.

Usage
-----

::

   .venv/bin/python main.py circuit.qasm --factory-mode backside --factories 8
   .venv/bin/python main.py circuit.qasm --factory-mode in_place --factories 8
   .venv/bin/python main.py circuit.qasm --factory-mode in_place_joint --factories 8 -o joint.gltf

The library entry points ``main.compile`` and ``miniflash.elaborate`` accept
``factory_mode`` and ``planner_candidates``. The default candidate budget is
8. Program JSON records the policy and the complete selected physical plan;
old JSON without these fields defaults to ``auto``. Statistics and rendering
consume the stored plan instead of performing another search.

Explicit modes currently use upright cells and ordinary K-face ports.
``orientation``, ``side_ports`` and ``fixed_columns`` are rejected together
with an explicit mode. ``die_dims`` still constrains the initial cell
synthesis floorplan, as in the existing compiler; it is not a hard bound on
the final joint chip rectangle. The joint planner reports the allocated
width and height separately. This version translates existing cells and
rebuilds their physical interconnections; it does not rotate or resynthesize
them, alter logical layer dependencies, or relocate T events across their
scheduled Clifford boundaries. The upstream parser's T-hoisting pass still
runs identically in all modes.

Physical model
--------------

Each reserved factory has a non-overlapping permanent site and a one-state
output buffer. An enabled factory pays a complete production interval for
its first state. A full output buffer applies backpressure; consumption
starts the next production interval. Unused reserved factories in limited
in-place mode are disabled. Factory, buffer, data and routing footprints
are included in the allocated chip rectangle for the entire execution.

Data rearrangement precedes the boundary's injections. Data movement uses
vacate-before-occupy ordering and a charged scratch tile for permutation
cycles. Source-to-target paths avoid factory sites, other buffers, and all
unrelated data positions. Each injection terminates in a J-adjacent ancilla,
then uses the K riser and ZZ/selective-readout gadget. The T/TDG correction
tables are unchanged. A whole ancilla route is occupied for the existing
two-step lattice-surgery gadget; distance is not interpreted as one hopping
cycle per tile. Clifford pin geometry and basis compensation remain bound
to each synthesized cell.

Joint search places internal factory islands and shared compute/parking
bays together using a bounded set of aspect ratios and island phases. Cells
are assigned to bays using incoming pin movement cost. A whole-program
area/movement/demand proxy ranks candidates; the first two feasible fully
scheduled candidates are compared by actual allocated area times runtime.
The result is the best evaluated heuristic candidate, not a globally optimal
layout. Infeasibility is explicit. Candidate identities and outcomes are
recorded in the Program. Manhattan A* routes use a bounded cache keyed by
obstacle topology. Source-target orders are reused within each channel;
bit masks test simultaneous route conflicts. The IR interns orthogonal
turn-point polylines, while the independent checker expands every occupied
tile before validating it.

Validation and accounting
-------------------------

The checker independently replays cell schedules, data moves, Hadamard
landings, injection paths and finite-capacity factory tokens. It checks
input/output continuity, physical obstacles, simultaneous path conflicts,
factory availability, event coverage, and channel boundaries. It validates
the compiler's geometric abstraction, not the microscopic implementation of
a magic-state distillation protocol or a new proof of input-circuit unitary
equivalence.

For explicit modes, ``patch_cell_slices`` is allocated rectangle area times
runtime, including cold start and completion of the last injection. With
the experimental profile d=15 and 1 microsecond code cycles, each logical
patch-step is 450 * 15e-6 physical-qubit-seconds. Factory area is not added a
second time. ``volume`` is this allocation in native coordinate units
(stride cubed = 8); ``compute_volume`` is intentionally not reported for
explicit modes, since the allocation already includes embedded factories.

Legacy ``auto`` is a separate reference model. Its existing checker does
not certify factory-to-injection connectivity: some small block injections
use side routes while their factory boxes are drawn at the bank. Its old
numbers remain unchanged and must not be labeled as physical-fabric
validation. The campaign reports its historical additive NCF estimate
separately from the three explicit modes' allocated-chip estimates.
