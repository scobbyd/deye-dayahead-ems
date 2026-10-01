"""The EMHASS shadow planner's native compute, one module per concern.

Import direction is one way, in this order, and nothing here imports the
emhass_core facade (ha/scripts/check.sh layers pins both):

  plant       every site number and entity, from plant.json over the reference DEFAULTS
  grid        the 15-minute plan grid; step arithmetic through UTC
  series      prices and the tariff frame, Solcast percentiles, the mix and the
              Growatt split, recorder series on the grid, the same-clock load shape
  objective   what the LP is told to minimise and the plant it is told about:
              pack constants, payload, stress pricing, rebalance, the cut rule, knobs
  deye        the Deye as an actuator: command, response, closed-loop settlement
  addon       HTTP to the add-on; the only module that talks to the network
  archive     the plan archive, the selectors, what the organic chain says
  writer      the plan step in force compiled into the register record, diffed against the real Deye
  repair      the PV curtailment repair and the effective load
  slices      compact and virtual day slices, the rolled display, rehydrate
  planning    run_plan
  scoring     the settlement lanes: a plan on the real day, the 20/20 hindsight
  ab          the A/B tester
"""
