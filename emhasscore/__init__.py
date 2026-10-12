"""The live EMHASS planner's native compute, one module per concern.

The planner wrapper is pyscript/emhass_shadow.py. It is the live planner, and
"shadow" in its name and in some entity ids is historical.

Import direction is one way, in the order of LAYERS below, and nothing here
imports the emhass_core facade (ha/scripts/check.sh layers pins both, and
that every module file is listed):

  plant       every site number and entity, from plant.json over the reference DEFAULTS
  grid        the 15-minute plan grid; step arithmetic through UTC
  series      prices and the tariff frame, Solcast percentiles, the mix and the
              Growatt split, recorder series on the grid, the same-clock load shape
  objective   what the LP is told to minimise and the plant it is told about:
              pack constants, payload, stress pricing, rebalance, the cut rule, knobs
  rebalance   the rebalancing clock: stretches at the top, the latch, the state file step
  deye        the Deye as an actuator: command, response, closed-loop settlement
  addon       HTTP to the add-on; the only module that talks to the network
  archive     the plan archive, the selectors, what the organic chain says
  writer      the plan step in force compiled into the register record, diffed against the real Deye
  thermal     the house as a second battery: zones, sources, comfort bands, the outdoor
              forecast on the grid, the thermal step in force for the heating follower
  repair      the PV curtailment repair and the effective load
  actuals     the recorder's statistics on the plan grid (DayActuals, the day and window series),
              the export switch, the fallback mask, the load history
  slices      compact and virtual day slices, the rolled display, rehydrate
  planning    run_plan
  scoring     the settlement lanes: a plan on the real day, the 20/20 hindsight
  ladder      the ladder lanes: rungs, windows, the per-day and per-hour tables
  ab          the A/B tester
"""

# The one source for the import order. check_layers.py reads it with ast (no
# import), and the emhass_core facade loops over it. Add a new module here.
LAYERS = (
    "plant", "grid", "series", "objective", "rebalance", "deye", "addon", "archive",
    "writer", "thermal", "repair", "actuals", "slices", "planning", "scoring", "ladder", "ab",
)
