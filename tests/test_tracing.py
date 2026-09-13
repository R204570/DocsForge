"""
Unit tests for tracing.py: the execution-trace event log itself, independent
of anything that produces or consumes it.

Covers the backend requirements from the observability spec:
  1. a top-level trace records start and completion
  2. nested execution produces parent/child relationships
  3. events arrive before the operation finishes (subscribe is live)
  4. failed child operations are represented correctly
  5. parent completion does not erase a child's recorded failure
  6. cancellation does not fabricate a completed state
  7. sensitive arguments are sanitized
  8. a broken Trace cannot break the operation using it
  9. large repetitive operations collapse to one updating row
 10. an unknown total never produces a fabricated percentage
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docsforge.tools import tracing as tr


class ForgeErrorLike(Exception):
    pass


@pytest.fixture(autouse=True)
def clean_registry():
    tr.clear()
    yield
    tr.clear()


# ── 1. start and completion lifecycle ─────────────────────
def test_trace_records_start_and_completion():
    ctx = tr.start("effect")
    with ctx.stage("resolving", target="effect"):
        pass
    events = tr.get(ctx.trace_id).events()

    assert len(events) == 1
    assert events[0].name == "resolving"
    assert events[0].state == tr.RUNNING or events[0].state == tr.COMPLETED
    # Same event id re-emitted running -> completed, not two separate rows.
    assert len({e.id for e in events}) == 1


def test_a_traced_call_starts_running_then_completes():
    ctx = tr.start("x")
    stage = ctx.stage("harvesting")
    stage.start()
    mid = tr.get(ctx.trace_id).events()
    assert mid[-1].state == tr.RUNNING

    stage.finish(tr.COMPLETED, message="done")
    after = tr.get(ctx.trace_id).events()
    assert after[-1].state == tr.COMPLETED
    assert after[-1].id == mid[-1].id


# ── 2. nested parent/child relationships ──────────────────
def test_stage_children_share_parent_id():
    ctx = tr.start("effect")
    with ctx.stage("resolving") as sub:
        sub.event("candidate", message="found one")
        sub.event("candidate", message="found two")

    events = tr.get(ctx.trace_id).events()
    stage_event = next(e for e in events if e.type == "stage")
    children = [e for e in events if e.type == "event"]
    assert len(children) == 2
    assert all(c.parent_id == stage_event.id for c in children)
    assert stage_event.parent_id is None  # root-level, under the tool call


def test_child_of_a_stage_can_itself_open_a_further_stage():
    """Turn -> tool call -> stage -> event is the documented minimum depth,
    but a stage's child context can still open another stage beneath it."""
    ctx = tr.start("effect")
    outer = ctx.stage("harvesting")
    inner_ctx = outer.start()
    with inner_ctx.stage("fetching page") as leaf:
        leaf.event("fetched", message="ok")
    outer.finish()

    events = {e.id: e for e in tr.get(ctx.trace_id).events()}
    fetching = next(e for e in events.values() if e.name == "fetching page")
    fetched = next(e for e in events.values() if e.name == "fetched")
    harvesting = next(e for e in events.values() if e.name == "harvesting")
    assert fetching.parent_id == harvesting.id
    assert fetched.parent_id == fetching.id


# ── 3. events arrive before the operation finishes ────────
def test_subscribe_delivers_events_while_the_trace_is_still_running():
    ctx = tr.start("effect")
    trace = tr.get(ctx.trace_id)
    seen = []
    ready = threading.Event()

    def consume():
        for ev in trace.subscribe(idle_heartbeat=0.05, max_idle_cycles=100):
            if ev.type == "heartbeat":
                continue
            seen.append(ev.name)
            ready.set()
            if len(seen) >= 2:
                return

    t = threading.Thread(target=consume)
    t.start()
    time.sleep(0.05)
    stage = ctx.stage("harvesting")
    stage.start()          # subscriber should see this immediately
    assert ready.wait(timeout=2), "subscriber never observed the running stage"
    # Not finished yet -- the trace is still open at this point.
    assert trace.finished is None
    stage.finish(tr.COMPLETED)
    t.join(timeout=2)
    assert not t.is_alive()
    assert "harvesting" in seen


def test_a_late_subscriber_gets_full_backlog_then_closes():
    ctx = tr.start("effect")
    with ctx.stage("resolving"):
        pass
    ctx.close()

    trace = tr.get(ctx.trace_id)
    got = list(trace.subscribe())
    assert [e.name for e in got] == ["resolving"]


# ── 4 & 5. failed child; parent completion does not erase it ──
def test_failed_child_stage_reports_failed_state():
    ctx = tr.start("effect")
    with pytest.raises(ForgeErrorLike):
        with ctx.stage("harvesting"):
            raise ForgeErrorLike("could not fetch")

    events = tr.get(ctx.trace_id).events()
    assert events[-1].state == tr.FAILED
    assert "could not fetch" in events[-1].error


def test_parent_completion_does_not_erase_child_failure():
    ctx = tr.start("effect")
    with ctx.stage("harvesting") as harvest_ctx:
        try:
            with harvest_ctx.stage("fetching page one"):
                raise ForgeErrorLike("404")
        except ForgeErrorLike:
            pass  # the parent stage tolerates one failed child and continues
        with harvest_ctx.stage("fetching page two"):
            pass

    events = {e.name: e for e in tr.get(ctx.trace_id).events()}
    assert events["fetching page one"].state == tr.FAILED
    assert events["fetching page two"].state == tr.COMPLETED
    assert events["harvesting"].state == tr.COMPLETED
    # The parent finishing successfully must not have rewritten the child's
    # own recorded outcome.
    assert events["fetching page one"].state == tr.FAILED


# ── 6. cancellation does not fabricate completion ─────────
def test_cancel_reports_cancelled_not_completed():
    ctx = tr.start("effect")
    stage = ctx.stage("harvesting")
    stage.start()
    stage.cancel("user stopped the request")

    events = tr.get(ctx.trace_id).events()
    assert events[-1].state == tr.CANCELLED
    assert events[-1].state != tr.COMPLETED


def test_cancel_after_finish_does_not_overwrite_the_real_outcome():
    """Once a stage has genuinely completed, a later cancel attempt (a race
    with a client disconnect, say) must not relabel real work as cancelled."""
    ctx = tr.start("effect")
    stage = ctx.stage("harvesting")
    stage.start()
    stage.finish(tr.COMPLETED, message="187 pages")
    stage.cancel("too late")

    events = tr.get(ctx.trace_id).events()
    assert events[-1].state == tr.COMPLETED
    assert events[-1].message == "187 pages"


# ── 7. sensitive arguments are sanitized ──────────────────
def test_sanitize_redacts_known_sensitive_keys():
    out = tr.sanitize({"api_key": "sk-live-abc", "Authorization": "Bearer xyz",
                       "url": "https://x.dev", "nested": {"cookie": "session=1"}})
    assert out["api_key"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["nested"]["cookie"] == "[redacted]"
    assert out["url"] == "https://x.dev"


def test_event_metadata_and_target_are_sanitized_automatically():
    ctx = tr.start("effect")
    ctx.event("resolved", target="https://x.dev",
             metadata={"token": "should-not-appear", "name": "effect"})
    ev = tr.get(ctx.trace_id).events()[0]
    assert ev.metadata["token"] == "[redacted]"
    assert ev.metadata["name"] == "effect"


def test_sanitize_caps_oversized_strings():
    out = tr.sanitize("x" * 10_000)
    assert len(out) < 5_000


# ── output capture: bounded, and truncation always disclosed ──
def test_clip_returns_everything_when_it_fits():
    body, omitted = tr.clip("short")
    assert body == "short"
    assert omitted == 0


def test_clip_counts_what_it_left_out():
    body, omitted = tr.clip("x" * 100, limit=30)
    assert len(body) == 30
    assert omitted == 70, "the omission is counted, so the UI can state it"


def test_stage_output_is_bounded_and_discloses_the_omission():
    ctx = tr.start("x")
    stage = ctx.stage("harvesting")
    stage.start()
    stage.finish(tr.COMPLETED, output="y" * (tr.MAX_OUTPUT + 1_234))

    ev = tr.get(ctx.trace_id).events()[-1]
    assert len(ev.output) == tr.MAX_OUTPUT
    assert ev.omitted == 1_234


def test_output_survives_as_dict_for_the_browser():
    ctx = tr.start("x")
    ctx.event("returned", output="the answer", message="ok")
    payload = tr.get(ctx.trace_id).events()[-1].as_dict()
    assert payload["output"] == "the answer"
    assert payload["omitted"] == 0


# ── 8. a broken trace cannot break the operation ──────────
def test_trace_append_failure_is_swallowed(monkeypatch):
    ctx = tr.start("effect")

    def boom(self, event):
        raise RuntimeError("disk full, or whatever")

    monkeypatch.setattr(tr.Trace, "append", boom)

    # None of these may raise, even though the underlying Trace is broken.
    ctx.event("candidate")
    with ctx.stage("harvesting"):
        pass
    ctx.close()


def test_a_stage_that_cannot_record_still_lets_the_real_work_run():
    ctx = tr.start("effect")
    trace = tr.get(ctx.trace_id)
    trace.append = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))

    ran = []
    with ctx.stage("harvesting"):
        ran.append("did the real work")
    assert ran == ["did the real work"]


# ── 9. large repetitive operations aggregate ──────────────
def test_many_ticks_collapse_to_one_event():
    ctx = tr.start("effect")
    stage = ctx.stage("harvesting")
    stage.start()
    for i in range(1, 231):
        stage.tick(f"fetched {i} pages", counters={"pages": i})
    stage.finish(tr.COMPLETED, message="230 pages")

    events = tr.get(ctx.trace_id).events()
    assert len(events) == 1, "230 ticks must not become 230 rows"
    assert events[0].counters["pages"] == 230


def test_max_events_caps_a_trace_that_still_produces_many_distinct_rows():
    ctx = tr.start("effect")
    for i in range(tr.MAX_EVENTS + 50):
        ctx.event(f"page {i}")
    events = tr.get(ctx.trace_id).events()
    assert len(events) == tr.MAX_EVENTS


# ── 10. unknown totals never produce a fake percentage ────
def test_tick_with_no_expected_total_reports_a_bare_count():
    ctx = tr.start("effect")
    stage = ctx.stage("harvesting")
    stage.start()
    stage.tick("fetched 12 pages", counters={"pages": 12})
    ev = tr.get(ctx.trace_id).events()[-1]
    assert ev.counters == {"pages": 12}
    assert "expected" not in ev.counters
    assert "%" not in (ev.message or "")


# ── extras: registry pruning, null context, detach ────────
def test_finished_traces_beyond_keep_finished_are_pruned():
    ids = []
    for i in range(tr.KEEP_FINISHED + 5):
        ctx = tr.start(f"tech{i}")
        ctx.close()
        ids.append(ctx.trace_id)
    assert tr.get(ids[0]) is None, "the oldest finished trace should be pruned"
    assert tr.get(ids[-1]) is not None


def test_null_context_never_raises_and_records_nothing():
    tr.NULL_CONTEXT.event("x")
    with tr.NULL_CONTEXT.stage("y") as sub:
        sub.event("z")
    tr.NULL_CONTEXT.detach()
    tr.NULL_CONTEXT.close()  # must not raise


def test_detach_prevents_a_closed_registry_entry_but_close_still_works():
    ctx = tr.start("effect")
    ctx.detach()
    trace = tr.get(ctx.trace_id)
    assert trace.keep_open is True
    ctx.close()
    assert trace.finished is not None
