"""DOI admission, publisher completion and the three user-facing deliverables."""

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from .acquisition.models import Candidate
from .acquisition.naming import paper_target
from .acquisition.openalex_locations import exhaust_openalex
from .acquisition.service import MetadataClients, acquire, failed_urls, resolve_all, verified_artifact
from .config import Settings
from .models import Publisher
from .normalization import clean, normalize_doi, original_doi, publisher
from .storage import Catalog, dumps, write_csv

AGENT_README = """# Agent task list

Read agent_required.csv or one CSV in by_publisher/. Each row contains only
doi, title, year, publisher, target_path. Missing years are blank in CSV and null in JSONL.
target_path already contains the year and title filename. Save to this exact
path; do not extract metadata from the PDF or construct another filename.

The literature-search-download skill automatically executes this handoff using
its bundled ScanPDF engine. Save the matching PDF to the exact target_path. Use only
lawful access and permissions already available to you; never bypass access
controls. Skip papers that remain unavailable.

Do not change DOI identifiers or target paths. Do not work on papers outside
the supplied list. This file does not authorize running any additional tasks.
"""

MASTER_FIELDS = ["doi", "title", "publisher", "journal", "year", "source_hits"]
AGENT_FIELDS = ["doi", "title", "year", "publisher", "target_path"]
DOI_FIELDS = {
    "openalex": "doi",
    "crossref": "DOI",
    "semantic_scholar": "externalIds.DOI",
    "scopus": "prism:doi",
    "lens": "external_ids[type=doi].value",
    "wos": "identifiers.doi",
}


def reconcile_discovery_dois(catalog):
    """Validate existing identifiers against own-paper fields, never payload-wide searches."""
    from .providers import registry

    adapters = registry()
    own = defaultdict(set)
    counts = Counter()
    for pid, provider, payload in catalog.db.execute(
        "SELECT paper_id,provider,payload_json FROM source_records WHERE paper_id IS NOT NULL"
    ):
        record = json.loads(payload)
        if provider in adapters:
            doi = adapters[provider](None).normalize(record).doi
        elif provider == "openaire":
            # Audit historical records only. The OpenAIRE resolver/adapter is unchanged.
            doi = next(
                (p.get("value") for p in record.get("pids") or [] if str(p.get("scheme")).lower() == "doi"),
                None,
            )
        elif provider == "arxiv":
            doi = record.get("doi")
        else:
            doi = None
        own[pid]  # Preserve the distinction between no provenance and no declared DOI.
        if canonical := normalize_doi(doi):
            own[pid].add(canonical)
            counts[provider] += 1
    removed = []
    for paper in catalog.iter_papers():
        if paper.doi and paper.paper_id in own and normalize_doi(paper.doi) not in own[paper.paper_id]:
            removed.append({"paper_id": paper.paper_id, "doi": paper.doi})
            paper.doi = None
            paper.flags = list(dict.fromkeys([*paper.flags, "NO_DECLARED_PAPER_DOI"]))
            catalog.save_paper(paper)
    return {"fields": DOI_FIELDS, "declared_doi_records": dict(counts), "removed": removed}


PAPER_TYPES = {
    "article",
    "journalarticle",
    "proceedingsarticle",
    "conferencepaper",
    "conferenceproceedingsarticle",
    "conference",
    "review",
    "letter",
    "shortsurvey",
    "preprint",
    "postedcontent",
}
NONPAPER_TYPES = {
    "dataset",
    "datasetcollection",
    "organization",
    "institution",
    "funder",
    "grant",
    "book",
    "bookchapter",
    "bookpart",
    "bookseries",
    "monograph",
    "referenceentry",
    "referencebook",
    "dissertation",
    "component",
    "standard",
    "peerreview",
    "report",
    "software",
    "paratext",
}

# Normal anonymous requests to declared official publisher PDFs are permitted by
# the current programmatic-access scope. This does not assert an open license.
PUBLISHER_DOMAINS = {
    "elsevier": ("elsevier.com", "sciencedirect.com"),
    "aps": ("aps.org",),
    "aip": ("aip.org", "scitation.org"),
    "springer_nature": ("nature.com", "springer.com", "springeropen.com"),
    "wiley": ("wiley.com",),
    "acs": ("acs.org",),
    "iop": ("iop.org",),
    "rsc": ("rsc.org",),
    "taylor_francis": ("tandfonline.com",),
    "ieee": ("ieee.org",),
    "oxford": ("oup.com",),
    "cambridge": ("cambridge.org",),
    "sage": ("sagepub.com",),
}


def enable_publisher_requests(catalog, paper):
    domains = PUBLISHER_DOMAINS.get(paper.publisher_normalized.value, ())
    for (raw,) in catalog.db.execute(
        "SELECT candidate_json FROM access_candidates WHERE paper_id=? AND eligible=0", (paper.paper_id,)
    ).fetchall():
        candidate = Candidate.model_validate_json(raw)
        parsed = urlsplit(candidate.source_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme in {"http", "https"}
            and not parsed.username
            and (candidate.direct_pdf or parsed.path.lower().endswith(".pdf"))
            and any(host == d or host.endswith("." + d) for d in domains)
        ):
            candidate.eligible = True
            candidate.reason = "NORMAL_ANONYMOUS_PUBLISHER_REQUEST; not an OA or license assertion"
            candidate.resolver_provenance.append({"policy": "programmatic-publisher-1", "doi": paper.doi})
            catalog.execute(
                "UPDATE access_candidates SET eligible=1,candidate_json=? WHERE candidate_id=?",
                (candidate.model_dump_json(), candidate.candidate_id),
            )


def type_kind(value):
    types = {re.sub(r"[^a-z]", "", t.lower()) for t in str(value or "").split(",")}
    if types & NONPAPER_TYPES:
        return "nonpaper"
    return "paper" if types & PAPER_TYPES else "unknown"


def reliable_publisher(value):
    return bool(clean(value)) and clean(value).lower() not in {"unknown", "none", "null", "n/a", "other"}


def agent_path(root, doi, group):
    return agent_target(root, Publisher(group), doi)


def agent_task_target(catalog, paper, group):
    return paper_target(catalog, catalog.root / "downloads/agent" / Publisher(group).value, paper)


def agent_task(catalog, paper, row):
    return {
        "doi": paper.doi if normalize_doi(paper.doi) == row["doi"] else row["doi"],
        "title": row["title"],
        "year": row["year"],
        "publisher": row["publisher"],
        "target_path": str(agent_task_target(catalog, paper, row["publisher"])),
    }


def write_agent_tasks(folder, rows, *, by_publisher=True):
    for extension in ("csv", "jsonl"):
        write_table(folder / f"agent_required.{extension}", rows, AGENT_FIELDS)
    if by_publisher:
        for group in Publisher:
            write_table(
                folder / "by_publisher" / f"{group.value}.csv",
                [r for r in rows if r["publisher"] == group.value],
                AGENT_FIELDS,
            )
    (folder / "README_AGENT.md").write_text(AGENT_README, encoding="utf-8")


def write_table(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".csv":
        with temporary.open("w", newline="", encoding="utf-8-sig") as stream:
            write_csv(stream, rows, fields, escape_fields={"title", "journal"})
    else:
        temporary.write_text(
            "".join(dumps({k: row[k] for k in fields}) + "\n" for row in rows), encoding="utf-8"
        )
    temporary.replace(path)


async def prepare_master(root, *, online=True, client_factory=None, update_candidates=True):
    catalog = Catalog(root)
    clients = MetadataClients(
        catalog, Settings(data_dir=root, attempts=1, timeout_seconds=20), client_factory
    )
    crossref = {}
    try:
        doi_audit = reconcile_discovery_dois(catalog)
        for (payload,) in catalog.db.execute(
            "SELECT DISTINCT payload_json FROM source_records WHERE provider='crossref'"
        ):
            item = json.loads(payload)
            if doi := normalize_doi(item.get("DOI")):
                crossref[doi] = item
        for doi, payload in catalog.db.execute(
            "SELECT doi,payload_json FROM doi_metadata WHERE payload_json IS NOT NULL"
        ):
            crossref[doi] = json.loads(payload)
        known_lookups = {
            r[0]
            for r in catalog.db.execute(
                "SELECT doi FROM doi_metadata WHERE payload_json IS NOT NULL "
                "OR error_code IN ('HTTP_404','HTTP_410')"
            )
        }
        groups = defaultdict(list)
        catalog.execute("DELETE FROM master_exclusions")
        for paper in catalog.iter_papers():
            doi = normalize_doi(paper.doi)
            if not doi or doi.startswith("10.13039/"):
                catalog.execute(
                    "INSERT INTO master_exclusions VALUES(?,?)", (paper.paper_id, "NO_ARTICLE_DOI")
                )
                continue
            paper.doi = original_doi(paper.doi)
            groups[doi].append(paper)
        unavailable = False
        for doi, papers in sorted(groups.items()):
            papers.sort(
                key=lambda p: (
                    not verified_artifact(catalog, p.paper_id),
                    not bool(p.publisher_raw),
                    p.paper_id,
                )
            )
            paper = papers[0]
            reasons = []
            item = crossref.get(doi)
            need = not reliable_publisher(paper.publisher_raw) or type_kind(paper.document_type) == "unknown"
            if item is None and need and online and doi not in known_lookups and not unavailable:
                transport = clients.transport("crossref")
                try:
                    response = await transport.request(
                        "GET", "https://api.crossref.org/works/" + quote(doi, safe="")
                    )
                    item = response.json()["message"]
                    if normalize_doi(item.get("DOI")) != doi:
                        raise ValueError("CROSSREF_DOI_MISMATCH")
                    catalog.execute(
                        "INSERT OR REPLACE INTO doi_metadata VALUES(?,?,NULL,?)",
                        (doi, dumps(item), transport.last_raw_path),
                    )
                except Exception as error:  # noqa: BLE001 -- optional metadata failure never blocks the list
                    catalog.execute(
                        "INSERT OR REPLACE INTO doi_metadata VALUES(?,NULL,?,NULL)",
                        (
                            doi,
                            f"HTTP_{transport.last_status}"
                            if transport.last_status in {404, 410}
                            else type(error).__name__,
                        ),
                    )
                    # A missing DOI is an individual result; service/network failures stop further calls.
                    if transport.last_status not in {404, 410}:
                        unavailable = True
                    item = None
            kind = type_kind(item.get("type")) if item else type_kind(paper.document_type)
            if kind == "nonpaper":
                for p in papers:
                    catalog.execute(
                        "INSERT INTO master_exclusions VALUES(?,?)", (p.paper_id, "NON_PAPER_TYPE")
                    )
                catalog.execute("DELETE FROM master_records WHERE doi=?", (doi,))
                continue
            if item:
                registered_doi = original_doi(item.get("DOI"))
                if registered_doi and normalize_doi(registered_doi) == doi:
                    paper.doi = registered_doi
                title = clean((item.get("title") or [""])[0])
                if (
                    paper.title
                    and title
                    and paper.title.casefold() != title.casefold()
                    and "TITLE_METADATA_DIFFERS" not in paper.flags
                ):
                    paper.flags.append("TITLE_METADATA_DIFFERS")
                paper.title = paper.title or title
                if reliable_publisher(item.get("publisher")):
                    paper.publisher_raw = clean(item["publisher"])
                paper.journal = (item.get("container-title") or [paper.journal])[0]
                paper.document_type = item.get("type") or paper.document_type
                # Reuse public full-text links as candidates, with the existing permission checks.
                metadata = {
                    "provider": "crossref",
                    "license": item.get("license", []),
                    "link": item.get("link", []),
                }
                if metadata not in paper.oa_metadata:
                    paper.oa_metadata.append(metadata)
            if not clean(paper.title):
                reasons.append("TITLE_MISSING")
            paper.publisher_normalized = publisher(paper.publisher_raw)
            for other in papers[1:]:
                # Final identity is DOI; retain internal records and their original provenance intact.
                catalog.execute(
                    "INSERT INTO master_exclusions VALUES(?,?)", (other.paper_id, "DUPLICATE_DOI")
                )
                paper.source_hits = sorted(set(paper.source_hits) | set(other.source_hits))
                for metadata in other.oa_metadata:
                    if metadata not in paper.oa_metadata:
                        paper.oa_metadata.append(metadata)
            catalog.save_paper(paper)
            # Add newly completed metadata locally; keep prior live resolution and attempts intact.
            from .acquisition.resolver import resolve_metadata

            evidence = catalog.db.execute(
                "SELECT evidence_id FROM doi_metadata WHERE doi=?", (doi,)
            ).fetchone()
            extra = [("crossref", item, {"evidence_id": evidence[0]})] if item and evidence else []
            for candidate in resolve_metadata(paper, extra) if update_candidates else ():
                catalog.save_candidate(paper.paper_id, candidate, replace=False)
            state = (
                "PROGRAM_VERIFIED"
                if verified_artifact(catalog, paper.paper_id)
                else "REVIEW"
                if reasons
                else "AGENT_REQUIRED"
            )
            catalog.execute(
                "INSERT INTO master_records VALUES(?,?,?,?) ON CONFLICT(doi) DO UPDATE SET "
                "paper_id=excluded.paper_id,status=excluded.status,reason_json=excluded.reason_json",
                (doi, paper.paper_id, state, dumps(reasons)),
            )
        catalog.execute(
            "DELETE FROM master_records WHERE paper_id IN (SELECT paper_id FROM master_exclusions)"
        )
        report = export_master(catalog)
        report["publisher_requests"] = clients.report()
        report["crossref_unavailable"] = unavailable
        report["doi_source_audit"] = doi_audit
        return report
    finally:
        await clients.close()
        catalog.close()


def master_rows(catalog):
    papers = {p.paper_id: p for p in catalog.iter_papers()}
    for doi, pid, status, reasons in catalog.db.execute("SELECT * FROM master_records ORDER BY doi"):
        p = papers[pid]
        yield (
            p,
            status,
            json.loads(reasons),
            {
                "doi": doi,
                "title": p.title,
                "publisher": p.publisher_normalized.value,
                "journal": p.journal or "",
                "year": p.year,
                "source_hits": sorted(p.source_hits),
            },
        )


def export_master(catalog):
    rows = [row for p, state, reasons, row in master_rows(catalog)]
    folder = catalog.root / "catalog"
    for extension in ("csv", "jsonl"):
        write_table(folder / f"master_papers.{extension}", rows, MASTER_FIELDS)
    return user_summary(catalog)


def refresh_user_states(catalog):
    for paper, state, reasons, row in list(master_rows(catalog)):
        updated = (
            "PROGRAM_VERIFIED"
            if verified_artifact(catalog, paper.paper_id)
            else "REVIEW"
            if reasons
            else "AGENT_REQUIRED"
        )
        catalog.execute("UPDATE master_records SET status=? WHERE doi=?", (updated, row["doi"]))


def user_summary(catalog):
    counts = Counter({s: 0 for s in ("PROGRAM_VERIFIED", "AGENT_REQUIRED", "REVIEW")})
    counts.update(dict(catalog.db.execute("SELECT status,count(*) FROM master_records GROUP BY status")))
    by_publisher = Counter({p.value: 0 for p in Publisher})
    for paper, state, reasons, row in master_rows(catalog):
        if state == "AGENT_REQUIRED":
            by_publisher[row["publisher"]] += 1
    return {"total_master": sum(counts.values()), **dict(counts), "agent_by_publisher": dict(by_publisher)}


def pending_acquisition(catalog):
    """Agent lists must never silently include papers with untried ordinary paths."""
    pending = []
    for paper, state, reasons, row in master_rows(catalog):
        if reasons or verified_artifact(catalog, paper.paper_id):
            continue
        resolved = catalog.db.execute(
            "SELECT 1 FROM access_resolution WHERE paper_id=?", (paper.paper_id,)
        ).fetchone()
        attempted = failed_urls(catalog, paper.paper_id)
        remaining = any(
            url not in attempted
            for (url,) in catalog.db.execute(
                "SELECT json_extract(candidate_json,'$.source_url') FROM access_candidates WHERE paper_id=? AND eligible=1",
                (paper.paper_id,),
            )
        )
        if not resolved or remaining:
            pending.append(row["doi"])
    return pending


def undecided_papers(catalog):
    """Every input must be admitted or explicitly excluded before acquisition."""
    return catalog.db.execute(
        "SELECT count(*) FROM papers WHERE paper_id NOT IN (SELECT paper_id FROM master_records) "
        "AND paper_id NOT IN (SELECT paper_id FROM master_exclusions)"
    ).fetchone()[0]


def export_agent(root):
    with Catalog(root) as catalog:
        refresh_user_states(catalog)
        if undecided_papers(catalog) or pending_acquisition(catalog):
            raise ValueError("ACQUISITION_INCOMPLETE: run acquire before exporting Agent tasks")
        rows, review = [], []
        for paper, state, reasons, row in master_rows(catalog):
            if state == "REVIEW":
                review.append({**row, "reasons": reasons})
            if state != "AGENT_REQUIRED":
                continue
            if not row["doi"] or not row["publisher"]:
                raise ValueError("INCOMPLETE_AGENT_IDENTITY")
            task = agent_task(catalog, paper, row)
            Path(task["target_path"]).parent.mkdir(parents=True, exist_ok=True)
            rows.append(task)
        write_agent_tasks(catalog.root / "agent_tasks", rows)
        for ext in ("csv", "jsonl"):
            write_table(catalog.root / "catalog" / f"review.{ext}", review, MASTER_FIELDS + ["reasons"])
        report = export_master(catalog)
        from .attachments.service import enabled, local

        if enabled(root):
            try:
                report["attachments"] = local(root, publish=False)
            except Exception as error:  # noqa: BLE001 -- attachment errors must not block PDF handoff
                report["attachments"] = {"status": "ATTACHMENT_ERROR", "error": str(error)}
        return report


def audit_master(root):
    from .library.store import collection_config

    with Catalog(root) as catalog:
        errors, expected = [], {}
        if undecided_papers(catalog):
            errors.append("MASTER_INCOMPLETE")
        managed_pending = set(pending_acquisition(catalog)) if collection_config(root) else set()
        for paper, state, reasons, row in master_rows(catalog):
            if normalize_doi(row["doi"]) != row["doi"] or row["doi"].startswith("10.13039/"):
                errors.append("INVALID_MASTER_DOI")
            if type_kind(paper.document_type) == "nonpaper":
                errors.append("NON_PAPER_IN_MASTER")
            if state == "PROGRAM_VERIFIED" and not verified_artifact(catalog, paper.paper_id):
                errors.append("VERIFIED_FILE_MISSING_OR_CHANGED")
            if state == "AGENT_REQUIRED":
                if reasons or not row["publisher"] or verified_artifact(catalog, paper.paper_id):
                    errors.append("INVALID_AGENT_STATE")
                if row["doi"] in managed_pending:
                    continue
                task = agent_task(catalog, paper, row)
                expected[task["doi"]] = task
        task_file = catalog.root / "agent_tasks/agent_required.jsonl"
        if task_file.exists():
            tasks = [json.loads(line) for line in task_file.read_text(encoding="utf-8").splitlines()]
            if any(set(t) != set(AGENT_FIELDS) for t in tasks):
                errors.append("AGENT_FIELDS_MISMATCH")
            elif len(tasks) != len(expected) or {t["doi"]: t for t in tasks} != expected:
                errors.append("AGENT_LIST_MISMATCH")
            for file in [
                catalog.root / "agent_tasks/agent_required.csv",
                *(catalog.root / "agent_tasks/by_publisher").glob("*.csv"),
            ]:
                with file.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    if reader.fieldnames != AGENT_FIELDS:
                        errors.append("AGENT_CSV_FIELDS_MISMATCH")
                    else:
                        for row in reader:
                            target = expected.get(row["doi"])
                            if (
                                not target
                                or any(row[k] != target[k] for k in ("doi", "publisher", "target_path"))
                                or row.get("year", "") != str(target["year"] or "")
                            ):
                                errors.append("AGENT_CSV_IDENTITY_MISMATCH")
        return {
            **user_summary(catalog),
            "pending_acquisition": len(pending_acquisition(catalog)),
            "undecided_papers": undecided_papers(catalog),
            "errors": sorted(set(errors)),
        }


async def acquire_master(
    root,
    *,
    limit=None,
    sources=None,
    retry_failed=False,
    paper_ids=None,
    max_candidates=3,
    client_factory=None,
    downloader=None,
    verifier=None,
):
    from .acquisition.models import AcquisitionOptions

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    with Catalog(root) as catalog:
        needs_preparation = undecided_papers(catalog) or retry_failed
    if needs_preparation:
        await prepare_master(root, client_factory=client_factory)
    with Catalog(root) as catalog:
        allowed = {
            pid
            for (pid,) in catalog.db.execute("SELECT paper_id FROM master_records WHERE status!='REVIEW'")
            if (paper_ids is None or pid in paper_ids) and not verified_artifact(catalog, pid)
        }
        if limit is not None:
            allowed = set(sorted(allowed)[:limit])
    # One automatic workflow: collect every existing route, then use one downloader loop.
    resolution = await resolve_all(
        root, online=True, paper_ids=allowed, refresh=retry_failed, client_factory=client_factory
    )
    openalex = {}
    if allowed and (not sources or "openalex" in sources):
        openalex = await exhaust_openalex(
            root,
            paper_ids=allowed,
            prepare_only=True,
            handoff=False,
            client_factory=client_factory,
            retry_failed=retry_failed,
            refresh=retry_failed,
        )
    with Catalog(root) as catalog:
        for paper in catalog.iter_papers():
            if paper.paper_id in allowed:
                enable_publisher_requests(catalog, paper)
    if allowed:
        await acquire(
            root,
            limit=limit or len(allowed),
            options=AcquisitionOptions(max_candidates=max_candidates),
            sources=sources,
            retry_failed=retry_failed,
            paper_ids=allowed,
            exhaust_candidates=True,
            **({"downloader": downloader} if downloader is not None else {}),
            **({"verifier": verifier} if verifier is not None else {}),
        )
    with Catalog(root) as catalog:
        refresh_user_states(catalog)
        incomplete = len(pending_acquisition(catalog))
        report = export_master(catalog)
    if not incomplete:
        report = export_agent(root)
    return {
        **report,
        "pending_acquisition": incomplete,
        "agent_exported": not incomplete,
        "resolver_errors": resolution["resolver_errors"] + openalex.get("errors", []),
    }


from .acquisition.layout import agent_target, safe_doi_filename  # noqa: F401 -- public DOI filename helper
