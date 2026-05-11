#!/usr/bin/env python3
"""Simulation runner: executes a flow, prints transcript, writes trajectory.

Usage:
    .venv/bin/python -m tests.simulation.runner tests/simulation/flows/mtb_trip.yaml
    .venv/bin/python -m tests.simulation.runner tests/simulation/flows/mtb_trip.yaml --keep-workspace
    .venv/bin/python -m tests.simulation.runner tests/simulation/flows/mtb_trip.yaml --beats 1-3
    .venv/bin/python -m tests.simulation.runner --compare runs/run1/trajectory.json runs/run2/trajectory.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Color codes for terminal output
COLORS = {
    "alex": "\033[36m",     # Cyan
    "emeka": "\033[33m",    # Yellow
    "wale": "\033[35m",     # Magenta
    "sam": "\033[34m",     # Blue
    "homer": "\033[32m",    # Green
    "system": "\033[90m",   # Gray
    "pass": "\033[32m",     # Green
    "fail": "\033[31m",     # Red
    "reset": "\033[0m",
}


def cprint(color: str, text: str) -> None:
    """Print colored text."""
    print(f"{COLORS.get(color, '')}{text}{COLORS['reset']}")


def format_beat_header(idx: int, actor_name: str, role: str, latency_ms: int) -> str:
    role_tag = f" ({role})" if role == "guest" else ""
    return f"--- Beat {idx} | {actor_name}{role_tag} | {latency_ms / 1000:.1f}s ---"


async def run_simulation(flow_path: Path, keep_workspace: bool = False, beat_range: tuple[int, int] | None = None, model_override: str | None = None) -> None:
    from tests.simulation.harness import SimulationHarness, BeatResult, Trajectory, check_expectations

    # Suppress nanobot internal logs (chatty during agent loop)
    try:
        from loguru import logger
        logger.disable("nanobot")
    except ImportError:
        pass

    # Use a unique workspace per flow to allow parallel simulation runs
    flow_slug = flow_path.stem
    workspace_root = Path(__file__).parent.parent / f"sim_workspace_{flow_slug}"
    harness = SimulationHarness(flow_path, workspace_root=workspace_root, keep_workspace=keep_workspace)
    flow = harness.flow
    if model_override:
        flow["model"] = model_override
    event_id = flow["event_id"]
    model = flow.get("model", "gemini/gemini-3-flash-preview")

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    trajectory = Trajectory(
        run_id=run_id,
        model=model,
        flow_name=flow["name"],
        event_id=event_id,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    print()
    cprint("system", f"=== {flow['name']} ===")
    cprint("system", f"Model: {model} | Event: {event_id} | Run: {run_id}")
    print()

    # Setup
    cprint("system", "Setting up isolated workspace...")
    try:
        await harness.setup()
        cprint("system", "Setup complete.\n")
    except Exception as e:
        cprint("fail", f"Setup failed: {e}")
        import traceback
        traceback.print_exc()
        return

    beats = flow.get("beats", [])
    start_idx = (beat_range[0] if beat_range else 1) - 1
    end_idx = beat_range[1] if beat_range else len(beats)

    total_passed = 0
    total_checked = 0

    try:
        for i, beat in enumerate(beats[start_idx:end_idx], start=start_idx + 1):
            actor_key = beat["actor"]
            actor = harness.actors[actor_key]
            message = beat["message"].strip()
            expectations = beat.get("expect", {})
            note = beat.get("note", "")

            # Clear session if beat requests it (simulates a new conversation)
            if beat.get("new_session"):
                cprint("system", "  [clearing session — new conversation]")
                harness.clear_session(role=actor.role)

            # Print actor message
            print(format_beat_header(i, actor.name, actor.role, 0))
            cprint(actor_key, f"  {actor.name}: {message}")
            print()

            # Send and get response
            if beat.get("heartbeat"):
                result = await harness.send_heartbeat(message)
            else:
                result = await harness.send_message(actor_key, message)

            # Print response
            cprint("homer", f"  Homer: {result['response']}")
            print()

            # Print tool calls if any
            if result["tool_calls"]:
                cprint("system", f"  Tools: {', '.join(tc['tool'] for tc in result['tool_calls'])}")

            # Check expectations
            # sender_id must match what the harness passes to process_direct:
            # phone-digits for guests, "user" for primaries.
            _sender_id = "user" if actor.role == "primary" else actor.jid.split("@")[0]
            exp_result = check_expectations(
                result["response"], result["tool_calls"],
                expectations, harness, event_id,
                response_source=result.get("response_source"),
                sender_id=_sender_id,
            )
            has_expectations = bool(expectations)
            if has_expectations:
                total_checked += 1
                status = "PASS" if exp_result["pass"] else "FAIL"
                color = "pass" if exp_result["pass"] else "fail"
                if exp_result["pass"]:
                    total_passed += 1
                cprint(color, f"  [{status}] {json.dumps({k: v for k, v in exp_result.items() if k != 'pass'}, indent=None)}")

            if note:
                cprint("system", f"  Note: {note}")

            # Get scope context for trajectory
            scope_ctx = harness.get_scope_context(event_id)
            ctx_info = {
                "injected": [f["content"][:100] for f in scope_ctx.get("context_layers", {}).get("injected", [])],
                "accumulated": [f["content"][:100] for f in scope_ctx.get("context_layers", {}).get("accumulated", [])],
            } if scope_ctx else {}

            # Check for escalation
            escalations = harness.get_pending_escalations()
            esc_id = escalations[0]["escalation_id"] if escalations else None

            # Build trajectory beat
            beat_result = BeatResult(
                beat_idx=i,
                actor=actor_key,
                role=actor.role,
                scope_id=event_id if actor.role == "guest" else None,
                message=message,
                response=result["response"],
                tool_calls=result["tool_calls"],
                context_available=ctx_info,
                escalation_created=esc_id,
                latency_ms=result["latency_ms"],
                tokens=result["tokens"],
                expectations=exp_result if has_expectations else {},
                passed=exp_result.get("pass", True),
                note=note,
            )
            trajectory.beats.append(beat_result)

            # Update the beat header with actual latency
            # (we printed 0 initially; this is a cosmetic limitation of streaming output)

            # Rebuild guest context if beat requests it
            if beat.get("rebuild"):
                cprint("system", "  [rebuilding guest context...]")
                harness.rebuild_guest_context()

            print()

    except KeyboardInterrupt:
        cprint("system", "\n--- Interrupted ---")
    except Exception as e:
        cprint("fail", f"\nError during beat: {e}")
        import traceback
        traceback.print_exc()
    finally:
        trajectory.finished_at = datetime.now(timezone.utc).isoformat()

        # Summary
        trajectory.summary = {
            "total_beats": len(trajectory.beats),
            "expectations_checked": total_checked,
            "expectations_passed": total_passed,
            "expectations_failed": total_checked - total_passed,
            "total_latency_ms": sum(b.latency_ms for b in trajectory.beats),
            "avg_latency_ms": (
                sum(b.latency_ms for b in trajectory.beats) // max(len(trajectory.beats), 1)
            ),
        }

        print()
        cprint("system", "=== Summary ===")
        cprint("system", f"Beats: {trajectory.summary['total_beats']}")
        if total_checked:
            color = "pass" if total_passed == total_checked else "fail"
            cprint(color, f"Expectations: {total_passed}/{total_checked} passed")
        cprint("system", f"Total time: {trajectory.summary['total_latency_ms'] / 1000:.1f}s")
        cprint("system", f"Avg latency: {trajectory.summary['avg_latency_ms'] / 1000:.1f}s per beat")

        # Save trajectory + transcript. Default to the in-tree path so dev
        # checkouts work out of the box; $HOMER_SIM_RUNS_DIR overrides — that's
        # how local dev points runs at `~/homer-portal/simulation_runs/` so
        # the admin portal renders them and they don't get committed to the
        # public homer repo.
        flow_slug = flow_path.stem  # e.g. "mtb_trip", "birthday_followup"
        runs_root = Path(os.environ.get(
            "HOMER_SIM_RUNS_DIR",
            str(Path(__file__).parent / "runs"),
        ))
        runs_dir = runs_root / f"{run_id}_{flow_slug}_{model.replace('/', '-')}"
        runs_dir.mkdir(parents=True, exist_ok=True)

        # Collect context files from workspaces
        context_files = {}
        for label, ws in [("main", harness.main_workspace), ("guest", harness.guest_workspace)]:
            for fname in ["AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md"]:
                fpath = ws / fname
                if fpath.exists():
                    context_files[f"{label}/{fname}"] = fpath.read_text(encoding="utf-8")

        # JSON trajectory
        traj_dict = trajectory.to_dict()
        traj_dict["context_files"] = context_files
        traj_path = runs_dir / "trajectory.json"
        traj_path.write_text(json.dumps(traj_dict, indent=2, ensure_ascii=False))
        cprint("system", f"\nTrajectory: {traj_path}")

        # Markdown transcript
        transcript_path = runs_dir / "transcript.md"
        transcript_path.write_text(_render_transcript(trajectory))
        cprint("system", f"Transcript: {transcript_path}")

        # Config snapshot
        config_path = runs_dir / "config.json"
        config_path.write_text(json.dumps({
            "model": model,
            "flow": str(flow_path),
            "event_id": event_id,
            "beat_range": list(beat_range) if beat_range else None,
        }, indent=2))

        # Snapshot artifacts (event files, scope DB, sessions, USER.md)
        harness.snapshot_artifacts(runs_dir)
        cprint("system", f"Artifacts: {runs_dir / 'artifacts'}")

        # HTML report
        report_path = _render_html_report(runs_dir, trajectory, context_files=context_files)
        cprint("system", f"Report: {report_path}")

        # Prune old runs — replace baseline only if the new run passes
        all_passed = total_checked > 0 and total_passed == total_checked
        _prune_old_runs(runs_dir.parent, flow_slug, model, runs_dir.name, all_passed)

        # Teardown
        cprint("system", "\nCleaning up...")
        await harness.teardown()
        cprint("system", "Done.")

        # Open report in browser
        import webbrowser
        webbrowser.open(f"file://{report_path}")


def _prune_old_runs(
    all_runs_dir: Path, flow_slug: str, model: str,
    current_dir_name: str, current_passed: bool,
) -> None:
    """Replace the baseline run only if the new run passes.

    Only touches runs for the same flow AND same model.
    - New run passes → delete older runs, keep new as baseline.
    - New run fails → keep the existing passing baseline + new failing run for debugging,
      delete other old failing runs.
    """
    import shutil

    model_suffix = model.replace("/", "-")
    matching = sorted(
        d for d in all_runs_dir.iterdir()
        if d.is_dir() and f"_{flow_slug}_{model_suffix}" in d.name and d.name != current_dir_name
    )

    if current_passed:
        # New run is the new baseline — remove all older runs
        for old in matching:
            shutil.rmtree(old)
    else:
        # Keep the most recent passing run as baseline, remove other old failing runs
        kept_baseline = False
        for old in reversed(matching):
            traj = old / "trajectory.json"
            if not traj.exists():
                shutil.rmtree(old)
                continue
            try:
                data = json.loads(traj.read_text())
                summary = data.get("summary", {})
                old_passed = summary.get("expectations_failed", 1) == 0
            except (json.JSONDecodeError, OSError):
                old_passed = False
            if old_passed and not kept_baseline:
                kept_baseline = True  # keep this one as the baseline
            else:
                shutil.rmtree(old)


def _render_html_report(runs_dir: Path, trajectory: "Trajectory",
                        context_files: dict[str, str] | None = None) -> Path:
    """Generate a self-contained HTML report from the trajectory."""
    template_path = Path(__file__).parent / "report_template.html"
    template = template_path.read_text(encoding="utf-8")

    traj_dict = trajectory.to_dict()
    if context_files:
        traj_dict["context_files"] = context_files
    traj_json = json.dumps(traj_dict, indent=2, ensure_ascii=False)
    html = template.replace("{{TRAJECTORY_JSON}}", traj_json)
    html = html.replace("{{FLOW_NAME}}", trajectory.flow_name)
    html = html.replace("{{RUN_ID}}", trajectory.run_id)

    report_path = runs_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path


def _render_transcript(trajectory: Trajectory) -> str:
    """Render trajectory as a markdown transcript."""
    lines = [
        f"# {trajectory.flow_name}",
        f"",
        f"**Run:** {trajectory.run_id}  ",
        f"**Model:** {trajectory.model}  ",
        f"**Event:** {trajectory.event_id}  ",
        f"**Beats:** {trajectory.summary.get('total_beats', 0)}  ",
        f"**Time:** {trajectory.summary.get('total_latency_ms', 0) / 1000:.1f}s  ",
        f"",
        "---",
        "",
    ]

    for b in trajectory.beats:
        role_tag = f" (guest)" if b.role == "guest" else ""
        lines.append(f"### Beat {b.beat_idx} -- {b.actor}{role_tag} [{b.latency_ms / 1000:.1f}s]")
        lines.append(f"")
        lines.append(f"> {b.message}")
        lines.append(f"")
        lines.append(f"**Homer:** {b.response}")
        lines.append(f"")

        if b.tool_calls:
            lines.append("Tools:")
            for tc in b.tool_calls:
                preview = tc.get("args_preview", "")
                if preview:
                    lines.append(f"  - `{tc['tool']}`: `{preview[:120]}`")
                else:
                    lines.append(f"  - `{tc['tool']}`")

        if b.escalation_created:
            lines.append(f"Escalation: `{b.escalation_created}`  ")

        if b.expectations:
            status = "PASS" if b.passed else "FAIL"
            lines.append(f"Expectations: **{status}**  ")
            for key, val in b.expectations.items():
                if key == "pass":
                    continue
                lines.append(f"  - {key}: `{json.dumps(val)}`  ")

        if b.note:
            lines.append(f"*{b.note}*  ")

        lines.append("")
        lines.append("---")
        lines.append("")

    # Summary
    s = trajectory.summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Beats | {s.get('total_beats', 0)} |")
    lines.append(f"| Expectations passed | {s.get('expectations_passed', 0)}/{s.get('expectations_checked', 0)} |")
    lines.append(f"| Total time | {s.get('total_latency_ms', 0) / 1000:.1f}s |")
    lines.append(f"| Avg latency | {s.get('avg_latency_ms', 0) / 1000:.1f}s |")
    lines.append("")

    return "\n".join(lines)


def compare_trajectories(path_a: str, path_b: str) -> None:
    """Compare two trajectory files side by side."""
    traj_a = json.loads(Path(path_a).read_text())
    traj_b = json.loads(Path(path_b).read_text())

    print(f"\nComparing:")
    cprint("system", f"  A: {path_a} ({traj_a['model']})")
    cprint("system", f"  B: {path_b} ({traj_b['model']})")
    print()

    beats_a = {b["beat_idx"]: b for b in traj_a["beats"]}
    beats_b = {b["beat_idx"]: b for b in traj_b["beats"]}

    all_idxs = sorted(set(beats_a.keys()) | set(beats_b.keys()))

    regressions = 0
    improvements = 0

    for idx in all_idxs:
        ba = beats_a.get(idx)
        bb = beats_b.get(idx)

        if not ba or not bb:
            cprint("system", f"Beat {idx}: only in {'A' if ba else 'B'}")
            continue

        pass_a = ba.get("passed", True)
        pass_b = bb.get("passed", True)

        if pass_a and not pass_b:
            regressions += 1
            cprint("fail", f"Beat {idx} ({ba['actor']}): REGRESSION (A=PASS, B=FAIL)")
            cprint("system", f"  A response: {ba['response'][:120]}...")
            cprint("system", f"  B response: {bb['response'][:120]}...")
        elif not pass_a and pass_b:
            improvements += 1
            cprint("pass", f"Beat {idx} ({ba['actor']}): IMPROVED (A=FAIL, B=PASS)")
        elif ba["response"] != bb["response"]:
            cprint("system", f"Beat {idx} ({ba['actor']}): response changed (both {'PASS' if pass_a else 'FAIL'})")

        # Latency comparison
        lat_a = ba.get("latency_ms", 0)
        lat_b = bb.get("latency_ms", 0)
        if lat_b > lat_a * 1.5:
            cprint("system", f"  Latency: {lat_a}ms -> {lat_b}ms (+{((lat_b - lat_a) / max(lat_a, 1)) * 100:.0f}%)")

    print()
    summary_color = "pass" if regressions == 0 else "fail"
    cprint(summary_color, f"Regressions: {regressions} | Improvements: {improvements}")


def main():
    parser = argparse.ArgumentParser(description="Run or compare event simulations")
    parser.add_argument("flow", nargs="?", help="Path to flow YAML file")
    parser.add_argument("--keep-workspace", action="store_true", help="Don't delete sim_workspace after run")
    parser.add_argument("--beats", help="Beat range to run, e.g. '1-3' or '5'")
    parser.add_argument("--compare", nargs=2, metavar=("TRAJ_A", "TRAJ_B"), help="Compare two trajectory files")
    parser.add_argument("--model", help="Override the flow's model (e.g. 'gemini/gemini-3-pro-preview', 'gemini/gemini-2.5-flash')")

    args = parser.parse_args()

    if args.compare:
        compare_trajectories(args.compare[0], args.compare[1])
        return

    if not args.flow:
        parser.error("flow path is required (unless using --compare)")

    beat_range = None
    if args.beats:
        parts = args.beats.split("-")
        if len(parts) == 1:
            beat_range = (int(parts[0]), int(parts[0]))
        else:
            beat_range = (int(parts[0]), int(parts[1]))

    asyncio.run(run_simulation(
        flow_path=Path(args.flow),
        keep_workspace=args.keep_workspace,
        beat_range=beat_range,
        model_override=args.model,
    ))


if __name__ == "__main__":
    main()
