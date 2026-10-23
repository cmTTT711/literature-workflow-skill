"""User workflows: isolated search runs, shared PDF reuse, resumable publication."""

import json
from pathlib import Path
from uuid import uuid4

from .discovery import discover
from .library.collections import attach_collection, publish_collection, reuse_library, saved_profile, years
from .library.store import atomic_json, collection_config
from .locking import RunLock
from .master import acquire_master, prepare_master


def discovery_checkpoint(catalog, providers):
    """Only retry failed/incomplete providers; an intentional budget stop is complete."""
    pending, states = [], {}
    for provider in providers:
        latest = catalog.db.execute(
            "SELECT q.run_id,r.finished_at FROM provider_queries q JOIN search_runs r USING(run_id) "
            "WHERE q.provider=? ORDER BY q.rowid DESC LIMIT 1",
            (provider,),
        ).fetchone()
        queries = (
            catalog.db.execute(
                "SELECT status,warnings_json FROM provider_queries WHERE provider=? AND run_id=?",
                (provider, latest[0]),
            ).fetchall()
            if latest
            else []
        )
        failed = (
            not queries
            or not latest[1]
            or any(
                state not in {"SUCCESS", "TRUNCATED"}
                or bool(
                    set(json.loads(warnings or "[]"))
                    & {"REPEATED_PAGE", "REMOTE_TOTAL_NOT_EXHAUSTED", "CURSOR_STALLED_OR_PROVIDER_CEILING"}
                )
                for state, warnings in queries
            )
        )
        if failed:
            pending.append(provider)
        states[provider] = "INCOMPLETE" if failed else "COMPLETE"
    return {"status": "PARTIAL" if pending else "SUCCESS", "pending_providers": pending, "providers": states}


def update_run(root, **values):
    config = collection_config(root)
    path = (Path(root) / config["result"]).resolve() / "run.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    atomic_json(path, {**saved, **values})


def finish_run(root, report, **values):
    report.update(values)
    discovery = report.get("discovery_status")
    report["status"] = (
        "NO_DATA"
        if discovery == "NO_DATA"
        else "PARTIAL"
        if discovery == "PARTIAL"
        or report.get("program_pending")
        or report.get("acquisition", {}).get("resolver_errors")
        else "COMPLETED"
    )
    update_run(root, **report)
    return report


async def run_search(profile, settings, workspace, *, label=None, root=None):
    workspace = Path(workspace).resolve()
    root = Path(root).resolve() if root is not None else workspace / "workspace" / "runs" / uuid4().hex[:12]
    if root.parent != workspace / "workspace" / "runs" or (root / "catalog.sqlite").exists():
        raise ValueError("NEW_RUN_ROOT_INVALID_OR_ALREADY_EXISTS")
    settings = settings.model_copy(update={"data_dir": root})
    attach_collection(root, workspace, profile, label=label)
    config = collection_config(root)
    config["settings"] = settings.model_dump(mode="json", exclude={"data_dir"})
    atomic_json(root / "collection.json", config)
    with RunLock(root / "workflow.lock"):
        try:
            update_run(root, status="DISCOVERING")
            discovery = await discover(profile, settings)
            await prepare_master(root, online=not settings.offline)
            reused = reuse_library(root)
            if not settings.offline and discovery["status"] != "NO_DATA":
                update_run(root, status="ACQUIRING", discovery=discovery)
                acquisition = await acquire_master(root)
            else:
                acquisition = {}
            return finish_run(
                root,
                publish_collection(root, compact=True),
                discovery_status=discovery["status"],
                reused=reused,
                acquisition=acquisition,
                offline=settings.offline,
            )
        except BaseException as error:
            update_run(root, status="INTERRUPTED", error=type(error).__name__)
            raise


async def acquire_collection(root, workspace, *, new=False, **options):
    root = Path(root).resolve()
    with RunLock(root / "workflow.lock"):
        profile = saved_profile(root)
        previous = collection_config(root)
        # Continue the active collection by default, keeping published handoff paths stable.
        attach_collection(
            root,
            workspace,
            profile,
            new=new,
            label=previous["label"].removesuffix("_" + years(profile)) if previous else None,
        )
        try:
            reused = reuse_library(root)
            update_run(root, status="ACQUIRING")
            acquisition = await acquire_master(root, **options)
            return finish_run(
                root,
                publish_collection(root, compact=True),
                reused=reused,
                acquisition=acquisition,
            )
        except BaseException as error:
            update_run(root, status="INTERRUPTED", error=type(error).__name__)
            raise


async def resume_collection(destination, settings):
    destination = Path(destination).resolve()
    saved = json.loads((destination / "run.json").read_text(encoding="utf-8"))
    root = (destination / saved["catalog"]).resolve()
    config = collection_config(root)
    if not config or (root / config["result"]).resolve() != destination:
        raise ValueError(
            "RUN_IS_NOT_ACTIVE: publish it as a new run instead of overwriting a historical snapshot"
        )
    from .models import SearchProfile
    from .storage import Catalog

    # Resume with the original provider selection and budgets, while allowing offline inspection.
    if "settings" in config:
        settings = settings.__class__(**config["settings"]).model_copy(update={"offline": settings.offline})

    with RunLock(root / "workflow.lock"):
        with Catalog(root) as catalog:
            checkpoint = discovery_checkpoint(catalog, settings.providers)
        if checkpoint["pending_providers"]:
            await discover(
                SearchProfile(**config["profile"]),
                settings.model_copy(
                    update={
                        "data_dir": root,
                        "providers": checkpoint["pending_providers"],
                        "force_refresh": not settings.offline,
                    }
                ),
            )
        await prepare_master(root, online=not settings.offline)
    if settings.offline:
        report = publish_collection(root, compact=True)
    else:
        report = await acquire_collection(root, destination.parent.parent, new=False)
    with Catalog(root) as catalog:
        checkpoint = discovery_checkpoint(catalog, settings.providers)
    return finish_run(
        root, report, discovery_status=checkpoint["status"], discovery=checkpoint, offline=settings.offline
    )
