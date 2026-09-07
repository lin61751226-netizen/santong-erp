from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    AckStatus,
    AssignmentMember,
    AttendanceEvent,
    AttendanceEventType,
    DeliveryStatus,
    Employee,
    Forklift,
    LeaveType,
    LeaveRequest,
    LeaveStatus,
    NotificationBatch,
    NotificationCategory,
    NotificationDelivery,
    PhotoUploadLog,
    WorkAssignment,
    Worksite,
)
from app.services.google_drive import GoogleDriveWorklogError, google_drive_worklog_service
from app.services.hr import (
    ATTENDANCE_COMMAND_MAP,
    evaluate_leave_policy,
    find_assignment_for_employee,
    find_assignment_member,
    format_policy_notes,
    get_latest_attendance_event,
    get_today_arrival_site,
    record_attendance_event,
    record_work_report_event,
)
from app.services import forklift_service
from app.services.line_platform import (
    LinePlatformError,
    bind_employee_line_user,
    complete_account_link_session,
    line_platform_service,
    start_account_link_session,
)


class LineService:
    api_base = "https://api.line.me/v2/bot/message"

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        if not settings.line_channel_secret:
            return True
        if not signature:
            return False
        digest = hmac.new(
            settings.line_channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if not settings.line_channel_access_token:
            return True, "LINE token 未設定，已改為模擬送出"
        headers = {
            "Authorization": f"Bearer {settings.line_channel_access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.api_base}/{endpoint}", headers=headers, json=payload)
        if response.is_success:
            return True, "sent"
        return False, response.text

    async def reply_text(self, reply_token: str, text: str) -> tuple[bool, str]:
        return await self.reply_messages(reply_token, [{"type": "text", "text": text}])

    async def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        payload = {"replyToken": reply_token, "messages": messages}
        return await self._post("reply", payload)

    async def push_text(self, user_id: str, text: str) -> tuple[bool, str]:
        payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
        return await self._post("push", payload)


line_service = LineService()

# LINE location events arrive as a separate message after the user chooses a
# check-in action. This short-lived state avoids changing the existing schema.
_pending_location_attendance: dict[str, str] = {}


def _quick_reply(items: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "items": [
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": label,
                    "text": text,
                },
            }
            for label, text in items
        ]
    }


async def _reply_attendance_options(reply_token: str) -> None:
    await line_service.reply_messages(
        reply_token,
        [
            {
                "type": "text",
                "text": "請選擇出勤打卡項目：",
                "quickReply": _quick_reply(
                    [
                        ("上班打卡", "上班打卡"),
                        ("下班打卡", "下班打卡"),
                    ]
                ),
            }
        ],
    )


async def _reply_location_prompt(reply_token: str, command: str) -> None:
    await line_service.reply_messages(
        reply_token,
        [{
            "type": "text",
            "text": f"請點下方「傳送目前位置」，完成{command}定位打卡：",
            "quickReply": {"items": [
                {"type": "action", "action": {"type": "location", "label": "傳送目前位置"}},
                {"type": "action", "action": {"type": "message", "label": "取消", "text": "取消定位打卡"}},
            ]},
        }],
    )


async def _reply_leave_options(reply_token: str) -> None:
    await line_service.reply_messages(
        reply_token,
        [
            {
                "type": "text",
                "text": "請選擇請假別：",
                "quickReply": _quick_reply(
                    [(leave_type.value, f"請假 {leave_type.value}") for leave_type in LeaveType]
                ),
            }
        ],
    )


async def _backup_line_bindings(session: Session) -> str | None:
    try:
        result = await google_drive_worklog_service.backup_line_bindings(session)
    except Exception:
        return "Google Drive 綁定備份失敗，請通知管理者"
    if result.get("status") == "unconfigured":
        return "Google Drive 綁定備份尚未設定"
    return None


def _is_group_command(text: str) -> bool:
    """Keep normal group conversation silent while preserving explicit bot actions."""
    exact_commands = {
        "開始綁定",
        "我的行程",
        "我的打卡",
        "我的請假",
        "上班打卡",
        "下班打卡",
        "到達工地",
        "離開工地",
        "外出",
        "返回",
        "加班開始",
        "加班結束",
        "已收到",
        "已到場",
        "工作開始",
        "工作完成",
    }
    return text in exact_commands or text.startswith(("綁定 ", "請假 ", "異常回報 ", "到達工地:"))


def _build_schedule_summary(
    assignment: WorkAssignment,
    worksite: Worksite,
    supervisor_name: str | None,
) -> str:
    start_time = assignment.start_time.strftime("%H:%M") if assignment.start_time else "-"
    end_time = assignment.end_time.strftime("%H:%M") if assignment.end_time else "-"
    return (
        f"【三通工程每日工作安排】\n"
        f"日期：{assignment.work_date:%Y/%m/%d}\n"
        f"工地：{worksite.name}\n"
        f"工作內容：{assignment.work_item}\n"
        f"負責主管：{supervisor_name or '-'}\n"
        f"時間：{start_time} - {end_time}\n"
        f"車輛/機具：{assignment.vehicle or '-'} / {assignment.equipment or '-'}\n"
        f"注意事項：{assignment.notes or '-'}"
    )


def _build_leave_summary(leave_request: LeaveRequest, employee: Employee) -> str:
    return (
        f"【請假申請通知】\n"
        f"員工：{employee.name}\n"
        f"假別：{leave_request.leave_type}\n"
        f"日期：{leave_request.start_date:%Y/%m/%d} - {leave_request.end_date:%Y/%m/%d}\n"
        f"原因：{leave_request.reason}\n"
        f"狀態：待審核"
    )


def _build_my_leave_summary(leaves: list[LeaveRequest]) -> str:
    if not leaves:
        return "目前沒有請假申請紀錄。"
    top_items = leaves[:3]
    lines = ["【我的請假】"]
    for leave_item in top_items:
        lines.append(
            f"{leave_item.leave_type}｜{leave_item.start_date:%Y/%m/%d}-{leave_item.end_date:%Y/%m/%d}｜{leave_item.status.value}"
        )
        if leave_item.policy_note:
            lines.append(f"備註：{leave_item.policy_note}")
    return "\n".join(lines)


def _build_my_attendance_summary(
    employee: Employee,
    assignment: WorkAssignment | None,
    member: AssignmentMember | None,
    latest_event,
) -> str:
    if not assignment and not latest_event:
        return "目前尚無打卡或回報資料。"
    lines = ["【個人狀態】", f"員工：{employee.name}"]
    if assignment:
        lines.append(f"工作日：{assignment.work_date:%Y/%m/%d}")
    if latest_event:
        lines.append(f"最近打卡：{latest_event.event_type}")
        lines.append(f"時間：{latest_event.happened_at:%Y/%m/%d %H:%M}")
    if member:
        lines.append(f"工作回報：{member.ack_status.value}")
        lines.append(f"最後動作：{member.last_line_action or '-'}")
        if member.note:
            lines.append(f"備註：{member.note}")
    return "\n".join(lines)


def _employee_by_line_user(session: Session, line_user_id: str) -> Employee | None:
    return session.exec(select(Employee).where(Employee.line_user_id == line_user_id)).first()


def _binding_base_url(postback_data: str | None) -> str:
    if not postback_data:
        return settings.public_base_url
    parsed = parse_qs(postback_data)
    return parsed.get("base", [settings.public_base_url])[0]


def _fallback_uploader_name(line_user_id: str | None, display_name: str | None) -> str:
    if display_name:
        cleaned = display_name.strip()
        if cleaned:
            return cleaned
    suffix = (line_user_id or "unknown")[-6:]
    return f"LINE-{suffix}"


async def _reply_account_link_prompt(session: Session, reply_token: str, line_user_id: str, base_url: str) -> None:
    try:
        link_data = await start_account_link_session(session, line_user_id, base_url)
    except LinePlatformError as exc:
        await line_service.reply_text(reply_token, str(exc))
        return

    await line_service.reply_messages(
        reply_token,
        [
            {
                "type": "template",
                "altText": "三通工程 LINE 綁定",
                "template": {
                    "type": "buttons",
                    "title": "三通工程",
                    "text": "點擊下方按鈕，使用員工代碼與綁定碼完成正式身分綁定。",
                    "actions": [
                        {
                            "type": "uri",
                            "label": "開始正式綁定",
                            "uri": link_data["link_url"],
                        }
                    ],
                },
            }
        ],
    )


async def notify_employees(
    session: Session,
    sender: Employee | None,
    employees: list[Employee],
    category: NotificationCategory,
    target_scope: str,
    target_value: str | None,
    content: str,
    assignment_id: int | None = None,
    meeting_id: int | None = None,
) -> dict[str, Any]:
    batch = NotificationBatch(
        sender_id=sender.id if sender else None,
        category=category,
        target_scope=target_scope,
        target_value=target_value,
        content=content,
        assignment_id=assignment_id,
        meeting_id=meeting_id,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    sent = 0
    skipped = 0
    failed = 0

    for employee in employees:
        status = DeliveryStatus.pending
        message = ""
        if not employee.line_user_id:
            status = DeliveryStatus.skipped
            message = "員工尚未綁定 LINE"
            skipped += 1
        else:
            ok, detail = await line_service.push_text(employee.line_user_id, content)
            if ok and settings.line_channel_access_token:
                status = DeliveryStatus.sent
                sent += 1
                message = detail
            elif ok:
                status = DeliveryStatus.simulated
                skipped += 1
                message = detail
            else:
                status = DeliveryStatus.failed
                failed += 1
                message = detail

        session.add(
            NotificationDelivery(
                batch_id=batch.id,
                employee_id=employee.id,
                line_user_id=employee.line_user_id,
                delivery_status=status,
                delivery_message=message,
            )
        )
    session.commit()

    return {
        "batch_id": batch.id,
        "recipient_count": len(employees),
        "sent_count": sent,
        "skipped_count": skipped,
        "failed_count": failed,
    }


def _event_datetime(event: dict[str, Any]) -> datetime:
    timestamp = event.get("timestamp")
    if not timestamp:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)


async def _backup_preserved_records() -> None:
    """新增營運記錄後立即更新快照；失敗時保留本機資料並交由排程補做。"""
    try:
        await google_drive_worklog_service.backup_database()
    except Exception as exc:
        print(f"[record-backup] Google Drive 快照失敗，等待排程補做：{type(exc).__name__}")


async def _handle_image_message(
    session: Session,
    *,
    event: dict[str, Any],
    message: dict[str, Any],
    reply_token: str,
    line_user_id: str,
    employee: Employee | None,
) -> None:
    if not reply_token:
        return
    message_id = str(message.get("id", "")).strip()
    if not message_id:
        await line_service.reply_text(reply_token, "目前無法取得這張照片的訊息編號，請再重新上傳一次。")
        return

    if not employee:
        display_name = await line_platform_service.get_source_display_name(event.get("source", {}))
        uploader_name = _fallback_uploader_name(line_user_id, display_name)

        try:
            upload = await google_drive_worklog_service.upload_line_photo(
                message_id=message_id,
                employee_name=uploader_name,
                happened_at=_event_datetime(event),
            )
        except (GoogleDriveWorklogError, LinePlatformError) as exc:
            await line_service.reply_text(
                reply_token,
                f"照片暫存到 Google 雲端硬碟失敗，請稍後再試。原因：{exc}",
            )
            return

        # 即使尚未綁定員工也留下上傳記錄，讓後台「工作相片上傳記錄」看得到
        try:
            session.add(
                PhotoUploadLog(
                    employee_id=None,
                    assignment_id=None,
                    site_id=None,
                    line_user_id=line_user_id,
                    source_message_id=message_id,
                    file_name=upload.file_name,
                    drive_file_id=upload.file_id,
                    drive_folder_id=upload.folder_id,
                    drive_url=upload.file_url,
                    note=f"未綁定員工上傳（LINE 顯示名稱：{uploader_name}）",
                )
            )
            session.commit()
            await _backup_preserved_records()
        except Exception as log_exc:
            session.rollback()
            print(f"[_handle_image_message] 未綁定照片記錄寫入失敗：{log_exc}")

        # 靜默上傳，不回覆 LINE 訊息
        return

    message_id = str(message.get("id", "")).strip()
    if not message_id:
        await line_service.reply_text(reply_token, "找不到圖片內容，請重新傳送一次。")
        return

    assignment = find_assignment_for_employee(session, employee.id)
    member = find_assignment_member(session, employee.id, assignment.id if assignment else None)
    site = None
    if assignment:
        site = session.get(Worksite, assignment.site_id)
    else:
        site = get_today_arrival_site(session, employee.id)
        if not site and employee.home_site_id:
            site = session.get(Worksite, employee.home_site_id)

    try:
        upload = await google_drive_worklog_service.upload_line_photo(
            message_id=message_id,
            employee_name=employee.name,
            happened_at=_event_datetime(event),
            site_name=site.name if site else None,
        )
    except (GoogleDriveWorklogError, LinePlatformError) as exc:
        await line_service.reply_text(reply_token, f"已收到照片，但上傳 Google 雲端硬碟失敗：{exc}")
        return

    if assignment:
        site = session.get(Worksite, assignment.site_id)
        report_photos = list(assignment.report_photos or [])
        report_photos.append(upload.file_url)
        assignment.report_photos = report_photos
        session.add(assignment)
    elif employee.home_site_id:
        site = session.get(Worksite, employee.home_site_id)

    if member:
        member.photo_url = upload.file_url
        member.last_line_action = "上傳照片"
        session.add(member)

    session.add(
        PhotoUploadLog(
            employee_id=employee.id,
            assignment_id=assignment.id if assignment else None,
            site_id=site.id if site else None,
            line_user_id=line_user_id,
            source_message_id=message_id,
            file_name=upload.file_name,
            drive_file_id=upload.file_id,
            drive_folder_id=upload.folder_id,
            drive_url=upload.file_url,
            note="line image upload",
        )
    )
    session.commit()
    await _backup_preserved_records()

    # 靜默上傳，不回覆 LINE 訊息


async def process_webhook_event(session: Session, event: dict[str, Any]) -> None:
    event_type = event.get("type")
    reply_token = event.get("replyToken", "")
    source = event.get("source", {})
    line_user_id = source.get("userId")
    source_type = source.get("type")

    if event_type == "follow" and reply_token:
        await line_service.reply_text(
            reply_token,
            "歡迎使用三通工程系統。請點 Rich Menu 的「開始綁定」，或輸入：開始綁定",
        )
        return

    if event_type == "accountLink":
        if not line_user_id:
            return
        link_info = event.get("link", {})
        nonce = link_info.get("nonce", "")
        result = link_info.get("result", "failed")
        try:
            completed = complete_account_link_session(session, line_user_id, nonce, result)
        except LinePlatformError:
            completed = {"status": "failed"}
        if reply_token:
            if completed["status"] == "completed":
                backup_warning = await _backup_line_bindings(session)
                message = f"綁定完成：{completed['employee_name']} ({completed['employee_code']})"
                if backup_warning:
                    message += f"\n雲端備份提醒：{backup_warning}"
                await line_service.reply_text(
                    reply_token,
                    message,
                )
            else:
                await line_service.reply_text(reply_token, "LINE 正式綁定失敗，請重新從 Rich Menu 開始。")
        return

    if event_type == "postback":
        if not reply_token or not line_user_id:
            return
        data = event.get("postback", {}).get("data", "")
        if data.startswith("action=bind:start"):
            await _reply_account_link_prompt(session, reply_token, line_user_id, _binding_base_url(data))
            return
        if data == "action=attendance:menu":
            await _reply_attendance_options(reply_token)
            return
        if data == "action=leave:menu":
            await _reply_leave_options(reply_token)
            return
        if data.startswith("action=menu:"):
            return
        await line_service.reply_text(reply_token, "已收到選單操作。")
        return

    if event_type != "message":
        return
    message = event.get("message", {})
    message_type = message.get("type")
    employee = _employee_by_line_user(session, line_user_id) if line_user_id else None

    if message_type == "location" and line_user_id:
        pending_command = _pending_location_attendance.pop(line_user_id, None)
        if not employee:
            if reply_token:
                await line_service.reply_text(reply_token, "此 LINE 帳號尚未綁定員工身分，請先完成綁定。")
            return
        if not pending_command:
            if reply_token:
                await line_service.reply_text(reply_token, "請先按「上班打卡」或「下班打卡」，再傳送目前位置。")
            return
        location = message.get("latitude"), message.get("longitude")
        try:
            latitude = float(location[0])
            longitude = float(location[1])
        except (TypeError, ValueError):
            if reply_token:
                await line_service.reply_text(reply_token, "位置資料無效，請重新按打卡後傳送位置。")
            return
        result = record_attendance_event(
            session, employee, pending_command,
            note=message.get("address") or "LINE 位置打卡",
            latitude=latitude,
            longitude=longitude,
        )
        await _backup_preserved_records()
        if reply_token:
            reply_lines = [f"已完成定位打卡：{pending_command}", f"位置：{latitude:.6f}, {longitude:.6f}"]
            if result.anomalies:
                reply_lines.append(f"提醒：{'；'.join(result.anomalies)}")
            await line_service.reply_text(reply_token, "\n".join(reply_lines))
        return

    if message_type == "image" and line_user_id:
        await _handle_image_message(
            session,
            event=event,
            message=message,
            reply_token=reply_token,
            line_user_id=line_user_id,
            employee=employee,
        )
        return

    if message_type != "text" or not line_user_id:
        return

    text = str(message.get("text", "")).strip()

    if source_type in {"group", "room"} and not _is_group_command(text):
        active_inspection = forklift_service.get_session(line_user_id)
        inspection_command = text in {"點檢", "堆高機點檢", "開始點檢", "取消點檢"} or (
            active_inspection and (
                text in {"正常", "異常"} or text.startswith(("點檢工地:", "點檢堆高機:"))
            )
        )
        if not inspection_command:
            return

    if text == "開始綁定":
        await _reply_account_link_prompt(session, reply_token, line_user_id, settings.public_base_url)
        return

    if text.startswith("綁定 "):
        bind_value = text.split(" ", 1)[1].strip()
        target = session.exec(
            select(Employee).where(
                (Employee.bind_token == bind_value) | (Employee.employee_code == bind_value)
            )
        ).first()
        if not target:
            await line_service.reply_text(reply_token, "找不到綁定碼，請向行政確認。")
            return
        try:
            bind_employee_line_user(session, target, line_user_id)
        except LinePlatformError as exc:
            await line_service.reply_text(reply_token, str(exc))
            return
        session.commit()
        backup_warning = await _backup_line_bindings(session)
        message = f"綁定完成：{target.name} ({target.employee_code})"
        if backup_warning:
            message += f"\n雲端備份提醒：{backup_warning}"
        await line_service.reply_text(reply_token, message)
        return

    if not employee:
        await line_service.reply_text(reply_token, "此 LINE 帳號尚未綁定員工身分，請先點 Rich Menu 的「開始綁定」。")
        return

    # ---- 堆高機點檢流程 ----
    if forklift_service.get_session(line_user_id) and (
        employee.status != "active" or forklift_service.get_session(line_user_id).employee_id != employee.id
    ):
        forklift_service.clear_session(line_user_id)
        await line_service.reply_text(reply_token, "員工身分已異動，請聯絡管理員確認後重新開始點檢。")
        return
    if text == "點檢" or text == "堆高機點檢" or text == "開始點檢":
        if employee.status != "active":
            await line_service.reply_text(reply_token, "此員工帳號已停用，請聯絡管理員。")
            return
        # 自動查詢今日「到達工地」打卡的工地
        arrival_site = get_today_arrival_site(session, employee.id)
        forklift_service.start_session(line_user_id, employee.id)

        if arrival_site:
            # 已有打卡記錄，自動帶入工地，直接顯示所有堆高機
            session_state = forklift_service.get_session(line_user_id)
            session_state.site_id = arrival_site.id
            session_state.step = "select_forklift"
            forklift_items = forklift_service.build_all_forklift_quick_replies(session)
            if not forklift_items:
                await line_service.reply_text(reply_token, "目前沒有可用的堆高機。")
                forklift_service.clear_session(line_user_id)
                return
            await line_service.reply_messages(
                reply_token,
                [{
                    "type": "text",
                    "text": f"🚜 堆高機每日點檢\n\n工地：{arrival_site.name}（自動帶入今日打卡工地）\n\n請選擇今日開的堆高機：",
                    "quickReply": {"items": [
                        {"type": "action", "action": {"type": "message", "label": label, "text": text}}
                        for label, text in forklift_items
                    ]},
                }],
            )
        else:
            # 沒有打卡記錄，顯示工地列表讓員工選擇
            site_items = forklift_service.build_site_quick_replies(session)
            if not site_items:
                await line_service.reply_text(reply_token, "目前沒有可用的工地。")
                forklift_service.clear_session(line_user_id)
                return
            await line_service.reply_messages(
                reply_token,
                [{
                    "type": "text",
                    "text": "🚜 堆高機每日點檢\n\n今日尚未打卡到達工地，請先選擇工地：",
                    "quickReply": {"items": [
                        {"type": "action", "action": {"type": "message", "label": label, "text": text}}
                        for label, text in site_items
                    ]},
                }],
            )
        return

    if text.startswith("點檢工地:"):
        session_state = forklift_service.get_session(line_user_id)
        if not session_state or session_state.step != "select_site":
            await line_service.reply_text(reply_token, "請先輸入「點檢」開始點檢流程。")
            return
        try:
            site_id = int(text.split(":", 1)[1])
        except (ValueError, IndexError):
            await line_service.reply_text(reply_token, "工地選擇無效，請重新輸入「點檢」。")
            forklift_service.clear_session(line_user_id)
            return
        site = session.get(Worksite, site_id)
        if not site or not site.is_active:
            await line_service.reply_text(reply_token, "找不到該工地，請重新輸入「點檢」。")
            forklift_service.clear_session(line_user_id)
            return
        session_state.site_id = site_id
        session_state.step = "select_forklift"
        # 顯示所有堆高機（不限工地，員工選今日開的車）
        forklift_items = forklift_service.build_all_forklift_quick_replies(session)
        if not forklift_items:
            await line_service.reply_text(reply_token, "目前沒有可用的堆高機。")
            forklift_service.clear_session(line_user_id)
            return
        await line_service.reply_messages(
            reply_token,
            [{
                "type": "text",
                "text": f"工地：{site.name}\n\n請選擇今日開的堆高機：",
                "quickReply": {"items": [
                    {"type": "action", "action": {"type": "message", "label": label, "text": text}}
                    for label, text in forklift_items
                ]},
            }],
        )
        return

    if text.startswith("點檢堆高機:"):
        session_state = forklift_service.get_session(line_user_id)
        if not session_state or session_state.step != "select_forklift":
            await line_service.reply_text(reply_token, "請先輸入「點檢」開始點檢流程。")
            return
        try:
            forklift_id = int(text.split(":", 1)[1])
        except (ValueError, IndexError):
            await line_service.reply_text(reply_token, "堆高機選擇無效，請重新輸入「點檢」。")
            forklift_service.clear_session(line_user_id)
            return
        forklift = session.get(Forklift, forklift_id)
        if not forklift or forklift.status == "inactive":
            await line_service.reply_text(reply_token, "找不到該堆高機，請重新輸入「點檢」。")
            forklift_service.clear_session(line_user_id)
            return
        session_state.forklift_id = forklift_id
        session_state.step = "inspecting"
        session_state.current_item_index = 0
        item = forklift_service.get_current_item(session_state)
        await line_service.reply_messages(
            reply_token,
            [{
                "type": "text",
                "text": f"堆高機：{forklift.forklift_code}（{forklift.model}）\n\n開始點檢（第 1/10 項）\n\n【{item['label']}】\n\n請回覆：正常 或 異常",
                "quickReply": {"items": [
                    {"type": "action", "action": {"type": "message", "label": "✅ 正常", "text": "正常"}},
                    {"type": "action", "action": {"type": "message", "label": "⚠️ 異常", "text": "異常"}},
                    {"type": "action", "action": {"type": "message", "label": "取消點檢", "text": "取消點檢"}},
                ]},
            }],
        )
        return

    if text == "取消點檢":
        forklift_service.clear_session(line_user_id)
        await line_service.reply_text(reply_token, "已取消點檢流程。")
        return

    # 點檢過程中回覆「正常」或「異常」
    session_state = forklift_service.get_session(line_user_id)
    if session_state and session_state.step == "inspecting":
        if text not in {"正常", "異常"}:
            await line_service.reply_text(reply_token, "請點選「正常」或「異常」，或輸入「取消點檢」。這則訊息不會列入點檢結果。")
            return
        is_normal = text == "正常"
        has_next = forklift_service.record_item_result(session_state, is_normal)
        if has_next:
            item = forklift_service.get_current_item(session_state)
            current_num = session_state.current_item_index + 1
            await line_service.reply_messages(
                reply_token,
                [{
                    "type": "text",
                    "text": f"第 {current_num}/10 項\n\n【{item['label']}】\n\n請回覆：正常 或 異常",
                    "quickReply": {"items": [
                        {"type": "action", "action": {"type": "message", "label": "✅ 正常", "text": "正常"}},
                        {"type": "action", "action": {"type": "message", "label": "⚠️ 異常", "text": "異常"}},
                        {"type": "action", "action": {"type": "message", "label": "取消點檢", "text": "取消點檢"}},
                    ]},
                }],
            )
        else:
            try:
                inspection = forklift_service.save_inspection(session, session_state)
            except ValueError as exc:
                forklift_service.clear_session(line_user_id)
                await line_service.reply_text(reply_token, str(exc))
                return
            await _backup_preserved_records()
            summary = forklift_service.build_inspection_summary(session, inspection)
            forklift_service.clear_session(line_user_id)
            from app.services.forklift_notifications import queue_inspection_alert, queue_vehicle_warning
            # Persist notifications before replying; an expired reply token cannot lose the alert.
            queue_inspection_alert(session, inspection)
            queue_vehicle_warning(session, session.get(Forklift, inspection.forklift_id))
            from app.services.forklift_notifications import deliver_forklift_notifications
            await deliver_forklift_notifications(session)
            await line_service.reply_text(reply_token, summary)
        return

    if text == "我的行程":
        assignment = find_assignment_for_employee(session, employee.id)
        if not assignment:
            await line_service.reply_text(reply_token, "今天沒有排定工作。")
            return
        worksite = session.get(Worksite, assignment.site_id)
        supervisor = session.get(Employee, assignment.supervisor_id) if assignment.supervisor_id else None
        await line_service.reply_text(
            reply_token,
            _build_schedule_summary(assignment, worksite, supervisor.name if supervisor else None),
        )
        return

    if text == "我的打卡":
        assignment = find_assignment_for_employee(session, employee.id)
        member = find_assignment_member(session, employee.id, assignment.id if assignment else None)
        latest_event = get_latest_attendance_event(session, employee.id, date.today())
        await line_service.reply_text(
            reply_token,
            _build_my_attendance_summary(employee, assignment, member, latest_event),
        )
        return

    if text == "我的請假":
        leaves = session.exec(
            select(LeaveRequest).where(LeaveRequest.employee_id == employee.id).order_by(LeaveRequest.requested_at.desc())
        ).all()
        await line_service.reply_text(reply_token, _build_my_leave_summary(leaves))
        return

    if text.startswith("請假 "):
        parts = text.split(" ", 4)
        if len(parts) == 2 and parts[1] in {leave_type.value for leave_type in LeaveType}:
            today_iso = date.today().isoformat()
            await line_service.reply_text(
                reply_token,
                f"已選擇：{parts[1]}\n請輸入：請假 {parts[1]} 開始日 結束日 原因\n範例：請假 {parts[1]} {today_iso} {today_iso} 家中有事",
            )
            return
        if len(parts) < 5:
            await line_service.reply_text(reply_token, "格式錯誤，請使用：請假 假別 開始日 結束日 原因（日期格式 YYYY-MM-DD）")
            return
        _, leave_type, start_date_text, end_date_text, reason = parts
        try:
            start_date = date.fromisoformat(start_date_text)
            end_date = date.fromisoformat(end_date_text)
        except ValueError:
            await line_service.reply_text(reply_token, "日期格式必須為 YYYY-MM-DD")
            return
        if end_date < start_date:
            await line_service.reply_text(reply_token, "請假結束日期不得早於開始日期")
            return

        policy = evaluate_leave_policy(session, employee, leave_type, start_date, end_date)
        if policy.errors:
            await line_service.reply_text(reply_token, "；".join(policy.errors))
            return

        leave_request = LeaveRequest(
            employee_id=employee.id,
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            status=LeaveStatus.pending,
            policy_note=format_policy_notes(policy.notes),
        )
        session.add(leave_request)
        session.commit()
        session.refresh(leave_request)

        managers = session.exec(
            select(Employee).where(Employee.role.in_(["owner", "admin", "site_manager"]))
        ).all()
        await notify_employees(
            session=session,
            sender=employee,
            employees=managers,
            category=NotificationCategory.leave,
            target_scope="management",
            target_value=None,
            content=_build_leave_summary(leave_request, employee),
        )
        response_lines = ["請假申請已送出，主管審核後會再通知你。"]
        if leave_request.policy_note:
            response_lines.append(f"提醒：{leave_request.policy_note}")
        if policy.conflicts:
            response_lines.append(f"期間內已有 {len(policy.conflicts)} 筆工作安排，主管核准後會需要改派。")
        await line_service.reply_text(reply_token, "\n".join(response_lines))
        return

    # 「到達工地」：有今日派工就沿用統一流程記派工工地；無派工則讓員工點選實際工地，
    # 確保後續「堆高機點檢」能正確自動帶入工地（堆高機不固定在同一工地）。
    if text == "到達工地":
        arrival_assignment = find_assignment_for_employee(session, employee.id)
        if arrival_assignment is None:
            sites = session.exec(
                select(Worksite).where(Worksite.is_active.is_(True)).order_by(Worksite.name)
            ).all()
            if sites:
                await line_service.reply_messages(
                    reply_token,
                    [{
                        "type": "text",
                        "text": "請選擇你到達的工地：",
                        "quickReply": _quick_reply([(ws.name, f"到達工地:{ws.id}") for ws in sites]),
                    }],
                )
                return

    if text.startswith("到達工地:"):
        raw_site_id = text.split(":", 1)[1].strip()
        arrival_site = session.get(Worksite, int(raw_site_id)) if raw_site_id.isdigit() else None
        if not arrival_site or not arrival_site.is_active:
            await line_service.reply_text(reply_token, "工地選擇無效，請重新輸入「到達工地」。")
            return
        session.add(AttendanceEvent(
            employee_id=employee.id,
            site_id=arrival_site.id,
            event_type=AttendanceEventType.arrive_site.value,
            source="line",
        ))
        arr_assignment = find_assignment_for_employee(session, employee.id)
        arr_member = find_assignment_member(session, employee.id, arr_assignment.id if arr_assignment else None)
        if arr_member:
            arr_member.ack_status = AckStatus.arrived
            arr_member.last_line_action = "到達工地"
            session.add(arr_member)
        session.commit()
        await _backup_preserved_records()
        await line_service.reply_text(
            reply_token,
            f"已記錄：到達工地\n工地：{arrival_site.name}\n接著可點「堆高機點檢」開始今日點檢。",
        )
        return

    if text in ATTENDANCE_COMMAND_MAP:
        if text in {"上班打卡", "下班打卡"}:
            _pending_location_attendance[line_user_id] = text
            await _reply_location_prompt(reply_token, text)
        else:
            result = record_attendance_event(session, employee, text)
            await _backup_preserved_records()
            reply_lines = [f"已記錄：{text}"]
            if result.assignment:
                worksite = session.get(Worksite, result.assignment.site_id)
                reply_lines.append(f"工地：{worksite.name if worksite else '-'}")
            if result.anomalies:
                reply_lines.append(f"提醒：{'；'.join(result.anomalies)}")
            await line_service.reply_text(reply_token, "\n".join(reply_lines))
        return

    if text == "取消定位打卡":
        _pending_location_attendance.pop(line_user_id, None)
        await line_service.reply_text(reply_token, "已取消定位打卡。")
        return

    assignment = find_assignment_for_employee(session, employee.id)
    member = find_assignment_member(session, employee.id, assignment.id if assignment else None)

    # 「工作開始」前提醒完成堆高機點檢（不強制阻擋）
    if text == "工作開始":
        today_inspections = forklift_service.list_today_inspections_by_operator(session, employee.id)
        if not today_inspections:
            await line_service.reply_text(
                reply_token,
                "⚠️ 提醒：今日尚未完成堆高機點檢\n\n建議先點 Rich Menu 的「堆高機點檢」完成點檢，再開始工作。\n\n若今日不開堆高機，可直接繼續工作。",
            )
            # 仍然繼續記錄工作開始，不強制阻擋

    action_map = {
        "已收到": AckStatus.received,
        "已到場": AckStatus.arrived,
        "工作開始": AckStatus.started,
        "工作完成": AckStatus.completed,
    }
    if text in action_map:
        if member:
            member.ack_status = action_map[text]
            member.last_line_action = text
            session.add(member)
            session.commit()
            record_work_report_event(session, employee, text, assignment=assignment)
            await _backup_preserved_records()
            await line_service.reply_text(reply_token, f"已記錄：{text}")
        else:
            # 今日尚未排定工作時仍回覆確認，不要落到可用指令清單
            record_work_report_event(
                session, employee, text, assignment=assignment,
                note="今日尚未排定工作，已先記錄動作回報。",
            )
            await _backup_preserved_records()
            await line_service.reply_text(
                reply_token,
                f"已記錄：{text}\n提醒：今日尚未排定工作，已先記錄你的動作回報。",
            )
        return

    # 只點「異常回報」還沒填問題：帶出常見堆高機問題清單供點選
    if text == "異常回報":
        options = [
            (option, f"異常回報 {option}")
            for option in forklift_service.FORKLIFT_EXCEPTION_OPTIONS
        ]
        await line_service.reply_messages(
            reply_token,
            [{
                "type": "text",
                "text": "請選擇異常問題（或直接輸入「異常回報 問題描述」）：",
                "quickReply": _quick_reply(options),
            }],
        )
        return

    if text.startswith("異常回報 "):
        detail = text.split(" ", 1)[1].strip()
        if not detail:
            await line_service.reply_text(reply_token, "請輸入異常內容，例如：異常回報 煞車異常")
            return
        if member:
            member.ack_status = AckStatus.exception
            member.last_line_action = "異常回報"
            member.note = detail
            session.add(member)
            session.commit()
        record_work_report_event(session, employee, "異常回報", assignment=assignment, note=detail)
        await _backup_preserved_records()
        # 自動關聯今日點檢的堆高機與打卡工地，同步通知老闆與管理員
        try:
            today_inspections = forklift_service.list_today_inspections_by_operator(session, employee.id)
            exc_forklift = (
                session.get(Forklift, today_inspections[-1].forklift_id)
                if today_inspections else None
            )
            exc_site = get_today_arrival_site(session, employee.id)
            from app.services.forklift_notifications import (
                deliver_forklift_notifications,
                queue_field_exception,
            )
            queue_field_exception(session, employee, exc_site, exc_forklift, detail)
            await deliver_forklift_notifications(session)
        except Exception as exc:
            print(f"[異常回報] 通知管理層失敗：{type(exc).__name__}: {exc}")
        await line_service.reply_text(reply_token, f"異常回報已送出：{detail}\n已同步通知老闆與管理員。")
        return

    # 完整可用指令清單：只在員工主動要求（輸入「指令/說明/選單」等）時出現一次，
    # 其他無法識別的輸入只給一行簡短提示，避免每則訊息都跳一長串清單。
    help_keywords = {
        "指令", "可用指令", "說明", "幫助", "help", "功能",
        "選單", "主選單", "工作工具", "?", "？",
    }
    if text.strip() in help_keywords:
        available_commands = [
            "開始綁定",
            "點檢（堆高機每日點檢）",
            "我的行程 / 我的打卡 / 我的請假",
            "請假（格式：請假 假別 開始日 結束日 原因）",
            "上班打卡 / 下班打卡",
            "到達工地 / 離開工地 / 外出 / 返回",
            "加班開始 / 加班結束",
            "已收到 / 已到場 / 工作開始 / 工作完成",
            "異常回報 現場缺料",
        ]
        await line_service.reply_text(
            reply_token,
            "可用指令：\n" + "\n".join(f"・{cmd}" for cmd in available_commands),
        )
        return

    await line_service.reply_text(
        reply_token,
        "沒看懂這個指令，可直接點下方 Rich Menu 按鈕，或輸入「指令」查看可用功能。",
    )
