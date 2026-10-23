"""Offline re-verification of existing staging files; no HTTP client is constructed."""

import json
from pathlib import Path
from uuid import uuid4

from ..locking import RunLock
from ..master import export_agent, refresh_user_states, user_summary
from ..models import now
from ..storage import Catalog, dumps
from .models import Candidate
from .service import promote, transition, verified_artifact
from .verifier import UnifiedPDFVerifier


def reverify(root):
    root = root.resolve()
    with RunLock(root / "acquisition.lock"), RunLock(Path("workspace/coordination/program.lock")):
        with Catalog(root) as c:
            before = user_summary(c)
            verifier = UnifiedPDFVerifier()
            outcomes = []
            rows = c.db.execute(
                "SELECT a.attempt_id,a.paper_id,a.candidate_id,a.staging_path,a.result_json,c.candidate_json "
                "FROM acquisition_attempts a JOIN access_candidates c USING(candidate_id) "
                "JOIN master_records m ON m.paper_id=a.paper_id WHERE a.status='VERIFY_FAILED' ORDER BY a.rowid"
            ).fetchall()
            papers = {p.paper_id: p for p in c.iter_papers()}
            for old_id, pid, cid, path, raw, candidate_raw in rows:
                if verified_artifact(c, pid) or not (root / path).is_file():
                    continue
                if (
                    c.db.execute(
                        "SELECT 1 FROM acquisition_attempts WHERE json_extract(result_json,'$.offline_reverify_of')=? "
                        "AND json_extract(result_json,'$.verification.verifier_version')='2'",
                        (old_id,),
                    ).fetchone()
                    or json.loads(raw).get("verification", {}).get("verifier_version") == "2"
                ):
                    continue
                result = verifier.verify(root / path, papers[pid])
                attempt_id = uuid4().hex
                detail = {
                    "offline_reverify_of": old_id,
                    "network_requests": 0,
                    "previous_verification": json.loads(raw).get("verification"),
                    "verification": result.model_dump(),
                }
                c.execute(
                    "INSERT INTO acquisition_attempts VALUES(?,?,?,?,NULL,?,?,?)",
                    (attempt_id, pid, cid, now(), "PROGRAM_DOWNLOADED_UNVERIFIED", path, dumps(detail)),
                )
                if result.status == "VERIFIED":
                    detail.update(
                        promote(
                            c,
                            papers[pid],
                            attempt_id,
                            root / path,
                            Candidate.model_validate_json(candidate_raw),
                            result,
                        )
                    )
                    transition(c, papers[pid], "VERIFIED", {"attempt_id": attempt_id})
                c.execute(
                    "UPDATE acquisition_attempts SET status=?,finished_at=?,result_json=? WHERE attempt_id=?",
                    (result.status, now(), dumps(detail), attempt_id),
                )
                outcomes.append(
                    {
                        "paper_id": pid,
                        "doi": papers[pid].doi,
                        "path": path,
                        "status": result.status,
                        "reasons": result.reasons,
                    }
                )
            refresh_user_states(c)
            report = {"before": before, "after": user_summary(c), "network_requests": 0, "outcomes": outcomes}
        export_agent(root)
        (root / "acquisition/reports/reverify-v2-latest.json").write_text(dumps(report), encoding="utf-8")
        return report


if __name__ == "__main__":
    report = reverify(Path("data"))
    print(dumps({k: v for k, v in report.items() if k != "outcomes"}))
