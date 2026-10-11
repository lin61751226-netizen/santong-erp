"""依打卡座標對照已設定地圖位置的工地。沒有座標的工地不猜測。"""
from __future__ import annotations

from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt

from sqlmodel import Session, select

from app.core.config import settings
from app.models import WorkAssignment, Worksite


MISMATCH_ANOMALY = "打卡工地與派工不符"
OUT_OF_RANGE_PREFIX = "定位不在任何工地範圍"
ASSIGNED_FAR_PREFIX = "定位不在派工工地範圍"
LOCATION_NOTE_MARKERS = (
    MISMATCH_ANOMALY,
    OUT_OF_RANGE_PREFIX,
    "定位超出工地範圍",
    ASSIGNED_FAR_PREFIX,
    "尚未設定座標",
    "員工表示不在最近工地",
)


@dataclass
class ClockSitePlan:
    needs_confirmation: bool
    record_site_id: int | None
    matched_site_id: int | None
    distance_m: float | None
    assigned_site_id: int | None
    assigned_site_name: str | None
    matched_site_name: str | None
    assigned_distance_m: float | None
    assigned_accept_m: float | None
    assigned_has_coords: bool
    anomalies: list[str]


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    value = sin(delta_lat / 2) ** 2 + cos(phi1) * cos(phi2) * sin(delta_lon / 2) ** 2
    return earth_radius_m * 2 * atan2(sqrt(value), sqrt(max(0, 1 - value)))


def accept_radius_m(site: Worksite) -> int:
    fence = site.geofence_radius_m or 0
    return max(settings.clock_site_radius_m, fence)


def note_has_location_flag(note: str | None) -> bool:
    return bool(note) and any(marker in note for marker in LOCATION_NOTE_MARKERS)


def anomaly_labels_from_note(note: str | None) -> list[str]:
    if not note:
        return []
    labels: list[str] = []
    if MISMATCH_ANOMALY in note:
        labels.append(MISMATCH_ANOMALY)
    if OUT_OF_RANGE_PREFIX in note:
        labels.append(OUT_OF_RANGE_PREFIX)
    if "定位超出工地範圍" in note:
        labels.append("定位超出工地範圍")
    if ASSIGNED_FAR_PREFIX in note:
        labels.append(ASSIGNED_FAR_PREFIX)
    if "尚未設定座標" in note:
        labels.append("派工工地尚未設定座標")
    if "員工表示不在最近工地" in note:
        labels.append("員工表示不在最近工地")
    return labels


def _coordinated(site: Worksite | None) -> bool:
    return site is not None and site.latitude is not None and site.longitude is not None


def plan_clock_site(
    session: Session,
    latitude: float,
    longitude: float,
    assignment: WorkAssignment | None,
) -> ClockSitePlan:
    assigned = session.get(Worksite, assignment.site_id) if assignment else None
    sites = list(session.exec(select(Worksite).where(Worksite.is_active.is_(True))).all())
    if assigned is not None and assigned.id not in {site.id for site in sites}:
        sites.append(assigned)

    ranked: list[tuple[Worksite, float, int]] = []
    for site in sites:
        if not _coordinated(site):
            continue
        ranked.append((
            site,
            distance_meters(latitude, longitude, site.latitude, site.longitude),
            accept_radius_m(site),
        ))
    ranked.sort(key=lambda item: item[1])

    assigned_distance = None
    assigned_accept = None
    assigned_has_coords = _coordinated(assigned)
    if assigned_has_coords and assigned is not None:
        assigned_distance = distance_meters(latitude, longitude, assigned.latitude, assigned.longitude)
        assigned_accept = accept_radius_m(assigned)

    nearest = ranked[0] if ranked else None

    def base(**overrides) -> ClockSitePlan:
        payload = {
            "needs_confirmation": False,
            "record_site_id": None,
            "matched_site_id": None,
            "distance_m": nearest[1] if nearest else assigned_distance,
            "assigned_site_id": assigned.id if assigned else None,
            "assigned_site_name": assigned.name if assigned else None,
            "matched_site_name": None,
            "assigned_distance_m": assigned_distance,
            "assigned_accept_m": assigned_accept,
            "assigned_has_coords": assigned_has_coords,
            "anomalies": [],
        }
        payload.update(overrides)
        return ClockSitePlan(**payload)

    if assigned_has_coords and assigned is not None and assigned_distance is not None and assigned_accept is not None:
        if assigned_distance <= assigned_accept:
            return base(
                record_site_id=assigned.id,
                matched_site_id=assigned.id,
                distance_m=assigned_distance,
                matched_site_name=assigned.name,
            )

    if nearest is not None and nearest[1] <= nearest[2]:
        site, dist, _accept = nearest
        if assigned is None or site.id != assigned.id:
            return base(
                needs_confirmation=True,
                matched_site_id=site.id,
                distance_m=dist,
                matched_site_name=site.name,
            )

    anomalies: list[str] = []
    if nearest is None:
        anomalies.append(f"{OUT_OF_RANGE_PREFIX}（沒有工地已設定座標，無法對照）")
    else:
        site, dist, _accept = nearest
        anomalies.append(f"{OUT_OF_RANGE_PREFIX}（最近的「{site.name}」約 {round(dist)} 公尺）")
    if assigned is not None and not assigned_has_coords:
        anomalies.append(f"派工工地「{assigned.name}」尚未設定座標")
    return base(
        record_site_id=assigned.id if assigned else None,
        anomalies=anomalies,
    )


def resolve_clock_site(
    plan: ClockSitePlan,
    choice: str | None,
) -> tuple[int | None, int | None, float | None, list[str]]:
    """回傳要寫入的工地、定位對到的工地、距離與要附加的異常。"""
    if choice == "yes" and plan.needs_confirmation and plan.matched_site_id is not None:
        notes: list[str] = []
        if plan.assigned_site_id and plan.assigned_site_id != plan.matched_site_id:
            notes.append(MISMATCH_ANOMALY)
        return plan.matched_site_id, plan.matched_site_id, plan.distance_m, notes
    if choice == "assigned" and plan.assigned_site_id is not None:
        notes = []
        if (
            plan.assigned_has_coords
            and plan.assigned_distance_m is not None
            and plan.assigned_accept_m is not None
            and plan.assigned_distance_m > plan.assigned_accept_m
        ):
            notes.append(f"{ASSIGNED_FAR_PREFIX}（約 {round(plan.assigned_distance_m)} 公尺）")
        elif plan.assigned_site_name and not plan.assigned_has_coords:
            notes.append(f"派工工地「{plan.assigned_site_name}」尚未設定座標")
        distance = plan.assigned_distance_m if plan.assigned_distance_m is not None else plan.distance_m
        return plan.assigned_site_id, plan.matched_site_id, distance, notes
    if choice == "none":
        name = plan.matched_site_name or "最近工地"
        return None, plan.matched_site_id, plan.distance_m, [f"員工表示不在最近工地「{name}」"]
    return plan.record_site_id, plan.matched_site_id, plan.distance_m, list(plan.anomalies)
