"""Code Pipeline: the standalone engine behind ``workforce/code-pipeline.md``.

A pipeline run is a directory of stage artifacts plus the evidence the Session
Engine recorded while the run happened.  The harness in this package computes
the run's confidence grade from that evidence; an agent never awards the grade.

Commands (all ``python -m pipeline.<name>``):

* ``start``      create a run directory and its ``run.json``; print the start
                 line that fixes the route, the request and the pinned declarations;
* ``depth``      the depth the current diff requires (full or light), by rule;
* ``run_check``  execute one verification criterion and record the evidence;
* ``attest``     a checker signs its report: this content, on this tree;
* ``escalate``   a checker demands full depth, for good;
* ``harness``    compute the grade for a run from the engine's own records;
* ``deliver``    the delivery gate: recompute the grade, refuse below HIGH;
* ``ledger``     append the finished run to ``LEDGER.md`` with a recomputed grade.

Nothing here imports ``app`` or ``daemon``; the package is usable from any
repository.
"""
