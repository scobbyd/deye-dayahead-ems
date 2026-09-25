"""The EMHASS shadow planner's native compute, one module per concern.

Import direction is one way, in this order, and nothing here imports the
emhass_core facade (ha/scripts/check.sh layers pins both):

  grid        the 15-minute plan grid; step arithmetic through UTC
  series      prices and the tariff frame, Solcast percentiles, the mix and the
              Growatt split, recorder series on the grid, the same-clock load shape
  objective   what the LP is told to minimise and the plant it is told about:
              pack constants, payload, stress pricing, rebalance, the cut rule, knobs
  deye        the Deye as an actuator: command, response, closed-loop settlement
  addon       HTTP to the add-on; the only module that talks to the network
  archive     the plan archive, the selectors, what the organic chain says
  repair      the PV curtailment repair and the effective load
  scoreboard  scores.csv and the rolling window
  slices      compact and virtual day slices, the rolled display, rehydrate
  planning    run_plan
  scoring     the lanes, hindsight, the replay of a past day, score_day
  ab          the A/B tester
"""
