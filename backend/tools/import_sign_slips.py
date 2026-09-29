"""Import the eight reviewed September 2026 paper sign slips without overwrites."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlmodel import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.db import session_scope  # noqa: E402
from app.models import Employee, Role, SignSlipRecord, Worksite  # noqa: E402
from app.routes.admin import batch_create_sign_slips  # noqa: E402
from app.schemas import SignSlipBatch  # noqa: E402


SOURCE = BACKEND_DIR.parent / "sign_slips_0901_0908.json"
EXPECTED = {
    "2026-09-01": "0002761",
    "2026-09-02": "0002762",
    "2026-09-03": "0002763",
    "2026-09-04": "0002766",
    "2026-09-05": "0002768",
    "2026-09-06": "0002769",
    "2026-09-07": "0002770",
    "2026-09-08": "0002771",
}


def load_items(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["items"]
    observed = {row["slip_date"]: row["slip_no"] for row in rows}
    if len(rows) != len(EXPECTED) or observed != EXPECTED:
        raise ValueError("Source must contain exactly the eight expected dates and slip numbers")
    SignSlipBatch.model_validate({"items": rows, "overwrite": False})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write new slips after preflight")
    parser.add_argument("--actor-code", default="ADMIN001")
    args = parser.parse_args()
    rows = load_items(SOURCE)

    with session_scope() as session:
        site = session.exec(select(Worksite).where(Worksite.code == "53")).first()
        if site is None:
            raise RuntimeError("Worksite 53 is missing; import stopped")
        existing = session.exec(select(SignSlipRecord)).all()
        by_no = {item.slip_no: item for item in existing}
        by_date = {
            item.slip_date.isoformat(): item
            for item in existing
            if item.site_code == "53"
        }
        for day, no in EXPECTED.items():
            if no in by_no and by_no[no].slip_date.isoformat() != day:
                raise RuntimeError(f"Slip number conflict: {no}")
            if day in by_date and by_date[day].slip_no != no:
                raise RuntimeError(f"Date conflict at worksite 53: {day}")

        planned = [row["slip_no"] for row in rows if row["slip_no"] not in by_no]
        skipped = [row["slip_no"] for row in rows if row["slip_no"] in by_no]
        print(f"preflight create={planned} skip={skipped} overwrite=False")
        if not args.apply:
            return

        actor = session.exec(select(Employee).where(Employee.employee_code == args.actor_code)).first()
        if actor is None or actor.role not in {Role.owner, Role.admin}:
            raise RuntimeError("Import actor must be an existing owner or admin")
        for row in rows:
            row["worksite_id"] = site.id
        payload = SignSlipBatch.model_validate({"items": rows, "overwrite": False})
        result = asyncio.run(batch_create_sign_slips(payload, session, actor))
        print(f"result={result}")
        if result["backup_status"] not in {"saved", "no_change"}:
            raise RuntimeError("Slips were written but the Google Drive snapshot needs attention")


if __name__ == "__main__":
    main()
