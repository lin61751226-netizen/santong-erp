from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import mimetypes
import secrets
import shutil
from typing import Any
from urllib.parse import urlencode

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlmodel import Session, select

from app.core.config import DATA_DIR, settings
from app.models import Employee, LineLinkSession, LineLinkStatus


RICH_MENU_DIR = DATA_DIR / "richmenus"
RICH_MENU_DIR.mkdir(parents=True, exist_ok=True)
STATIC_RICH_MENU_DIR = Path(__file__).resolve().parents[1] / "static" / "richmenus"


class LinePlatformError(Exception):
    pass


class LinePlatformService:
    api_base = "https://api.line.me/v2/bot"
    data_base = "https://api-data.line.me/v2/bot"

    def _headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        if not settings.line_channel_access_token:
            raise LinePlatformError("LINE_CHANNEL_ACCESS_TOKEN 尚未設定，無法呼叫正式 LINE API。")
        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, json=json, headers=headers, content=content)
        if response.status_code >= 400:
            raise LinePlatformError(f"LINE API 失敗：{response.status_code} {response.text}")
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    async def get_webhook_endpoint(self) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"{self.api_base}/channel/webhook/endpoint",
            headers=self._headers(None),
        )

    async def set_webhook_endpoint(self, endpoint: str) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"{self.api_base}/channel/webhook/endpoint",
            headers=self._headers(),
            json={"endpoint": endpoint},
        )

    async def test_webhook_endpoint(self, endpoint: str | None = None) -> dict[str, Any]:
        payload = {"endpoint": endpoint} if endpoint else {}
        return await self._request(
            "POST",
            f"{self.api_base}/channel/webhook/test",
            headers=self._headers(),
            json=payload,
        )

    async def issue_link_token(self, user_id: str) -> str:
        response = await self._request(
            "POST",
            f"{self.api_base}/user/{user_id}/linkToken",
            headers=self._headers(None),
        )
        return response["linkToken"]

    async def create_rich_menu(self, payload: dict[str, Any]) -> str:
        response = await self._request(
            "POST",
            f"{self.api_base}/richmenu",
            headers=self._headers(),
            json=payload,
        )
        return response["richMenuId"]

    async def upload_rich_menu_image(self, rich_menu_id: str, image_path: Path) -> None:
        content_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        await self._request(
            "POST",
            f"{self.data_base}/richmenu/{rich_menu_id}/content",
            headers=self._headers(content_type),
            content=image_path.read_bytes(),
        )

    async def set_default_rich_menu(self, rich_menu_id: str) -> None:
        await self._request(
            "POST",
            f"{self.api_base}/user/all/richmenu/{rich_menu_id}",
            headers=self._headers(None),
        )

    async def create_or_update_alias(self, alias_id: str, rich_menu_id: str) -> dict[str, Any]:
        try:
            return await self._request(
                "POST",
                f"{self.api_base}/richmenu/alias",
                headers=self._headers(),
                json={"richMenuAliasId": alias_id, "richMenuId": rich_menu_id},
            )
        except LinePlatformError:
            return await self._request(
                "POST",
                f"{self.api_base}/richmenu/alias/{alias_id}",
                headers=self._headers(),
                json={"richMenuId": rich_menu_id},
            )

    async def link_rich_menu_to_user(self, user_id: str, rich_menu_id: str) -> None:
        await self._request(
            "POST",
            f"{self.api_base}/user/{user_id}/richmenu/{rich_menu_id}",
            headers=self._headers(None),
        )

    async def get_message_content(self, message_id: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.data_base}/message/{message_id}/content",
                headers=self._headers(None),
            )
        if response.status_code >= 400:
            raise LinePlatformError(f"LINE 圖片下載失敗：{response.status_code} {response.text}")
        content_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, content_type


    async def get_source_display_name(self, source: dict[str, Any]) -> str | None:
        source_type = str(source.get("type") or "").strip()
        user_id = str(source.get("userId") or "").strip()
        if not source_type or not user_id:
            return None

        if source_type == "user":
            endpoint = f"{self.api_base}/profile/{user_id}"
        elif source_type == "group":
            group_id = str(source.get("groupId") or "").strip()
            if not group_id:
                return None
            endpoint = f"{self.api_base}/group/{group_id}/member/{user_id}"
        elif source_type == "room":
            room_id = str(source.get("roomId") or "").strip()
            if not room_id:
                return None
            endpoint = f"{self.api_base}/room/{room_id}/member/{user_id}"
        else:
            return None

        try:
            profile = await self._request("GET", endpoint, headers=self._headers(None))
        except LinePlatformError:
            return None

        display_name = str(profile.get("displayName") or "").strip()
        return display_name or None


line_platform_service = LinePlatformService()


def _load_font(size: int):
    font_candidates = [
        Path("C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/microsoftjhengheiui.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size=size)
    return ImageFont.load_default()


def _draw_centered_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font, fill: str) -> None:
    left, top, right, bottom = box
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8, align="center")
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = left + ((right - left) - text_width) / 2
    y = top + ((bottom - top) - text_height) / 2
    draw.multiline_text((x, y), text, font=font, fill=fill, spacing=8, align="center")


def generate_default_rich_menu_images() -> dict[str, Path]:
    bundled_outputs = {
        "main": STATIC_RICH_MENU_DIR / "santong-main.png",
        "tools": STATIC_RICH_MENU_DIR / "santong-tools.png",
    }
    if all(path.exists() for path in bundled_outputs.values()):
        for name, bundled_path in bundled_outputs.items():
            target_path = RICH_MENU_DIR / bundled_path.name
            if not target_path.exists():
                shutil.copyfile(bundled_path, target_path)
        return bundled_outputs

    width, height = 2500, 1686
    tab_height = 250
    column_width = width // 4

    font_title = _load_font(84)
    font_tab = _load_font(54)
    font_body = _load_font(62)

    definitions = [
        (
            "main",
            "#1f4d45",
            "#f5eee0",
            [
                ((0, 0, width // 2, tab_height), "主選單"),
                ((width // 2, 0, width, tab_height), "工作工具"),
                ((0, tab_height, column_width, height), "我的\n行程"),
                ((column_width, tab_height, column_width * 2, height), "我的\n請假"),
                ((column_width * 2, tab_height, column_width * 3, height), "上班\n打卡"),
                ((column_width * 3, tab_height, width, height), "開始\n綁定"),
            ],
        ),
        (
            "tools",
            "#6a4a2f",
            "#f7f2e8",
            [
                ((0, 0, width // 2, tab_height), "主選單"),
                ((width // 2, 0, width, tab_height), "工作工具"),
                ((0, tab_height, column_width, height), "到達\n工地"),
                ((column_width, tab_height, column_width * 2, height), "工作\n開始"),
                ((column_width * 2, tab_height, column_width * 3, height), "工作\n完成"),
                ((column_width * 3, tab_height, width, height), "異常\n回報"),
            ],
        ),
    ]

    outputs: dict[str, Path] = {}
    for name, bg, panel, blocks in definitions:
        image = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((32, 32, width - 32, height - 32), radius=48, outline="#ffffff", width=6)
        draw.text((90, 78), "三通工程", font=font_title, fill="#ffffff")

        for index, (box, label) in enumerate(blocks):
            fill_color = panel if index >= 2 else ("#ead6b7" if index == 1 and name == "main" else "#dbe8e3")
            draw.rounded_rectangle(box, radius=0, fill=fill_color, outline="#ffffff", width=4)
            font = font_tab if box[3] <= tab_height else font_body
            fill = "#23403b" if fill_color != "#ead6b7" else "#5a391f"
            _draw_centered_text(draw, box, label, font, fill)

        image_path = RICH_MENU_DIR / f"santong-{name}.png"
        image.save(image_path, format="PNG")
        outputs[name] = image_path
    return outputs


def build_default_rich_menu_payloads(base_url: str) -> dict[str, dict[str, Any]]:
    return {
        "main": {
            "size": {"width": 2500, "height": 1686},
            "selected": True,
            "name": "santong-main",
            "chatBarText": "三通主選單",
            "areas": [
                {
                    "bounds": {"x": 0, "y": 0, "width": 1250, "height": 250},
                    "action": {"type": "postback", "label": "主選單", "data": "action=menu:main", "displayText": "主選單"},
                },
                {
                    "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 250},
                    "action": {"type": "richmenuswitch", "richMenuAliasId": "santong-tools", "data": "action=menu:switch-tools"},
                },
                {
                    "bounds": {"x": 0, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "我的行程", "text": "我的行程"},
                },
                {
                    "bounds": {"x": 625, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "我的請假", "text": "我的請假"},
                },
                {
                    "bounds": {"x": 1250, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "上班打卡", "text": "上班打卡"},
                },
                {
                    "bounds": {"x": 1875, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "postback", "label": "開始綁定", "data": f"action=bind:start&base={base_url}", "displayText": "開始綁定"},
                },
            ],
        },
        "tools": {
            "size": {"width": 2500, "height": 1686},
            "selected": False,
            "name": "santong-tools",
            "chatBarText": "三通工作工具",
            "areas": [
                {
                    "bounds": {"x": 0, "y": 0, "width": 1250, "height": 250},
                    "action": {"type": "richmenuswitch", "richMenuAliasId": "santong-main", "data": "action=menu:switch-main"},
                },
                {
                    "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 250},
                    "action": {"type": "postback", "label": "工作工具", "data": "action=menu:tools", "displayText": "工作工具"},
                },
                {
                    "bounds": {"x": 0, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "到達工地", "text": "到達工地"},
                },
                {
                    "bounds": {"x": 625, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "工作開始", "text": "工作開始"},
                },
                {
                    "bounds": {"x": 1250, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "工作完成", "text": "工作完成"},
                },
                {
                    "bounds": {"x": 1875, "y": 250, "width": 625, "height": 1436},
                    "action": {"type": "message", "label": "異常回報", "text": "異常回報 現場缺料"},
                },
            ],
        },
    }


async def deploy_default_rich_menus(base_url: str) -> dict[str, Any]:
    images = generate_default_rich_menu_images()
    payloads = build_default_rich_menu_payloads(base_url)

    main_id = await line_platform_service.create_rich_menu(payloads["main"])
    await line_platform_service.upload_rich_menu_image(main_id, images["main"])

    tools_id = await line_platform_service.create_rich_menu(payloads["tools"])
    await line_platform_service.upload_rich_menu_image(tools_id, images["tools"])

    await line_platform_service.set_default_rich_menu(main_id)
    await line_platform_service.create_or_update_alias("santong-main", main_id)
    await line_platform_service.create_or_update_alias("santong-tools", tools_id)

    return {
        "main_rich_menu_id": main_id,
        "tools_rich_menu_id": tools_id,
        "images": {name: str(path) for name, path in images.items()},
        "base_url": base_url,
    }


async def start_account_link_session(session: Session, line_user_id: str, base_url: str) -> dict[str, Any]:
    link_token = await line_platform_service.issue_link_token(line_user_id)
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    line_session = LineLinkSession(
        line_user_id=line_user_id,
        link_token=link_token,
        expires_at=expires_at,
        status=LineLinkStatus.issued,
    )
    session.add(line_session)
    session.commit()
    session.refresh(line_session)

    link_url = f"{base_url.rstrip('/')}/line/account-link?{urlencode({'linkToken': link_token})}"
    return {"link_token": link_token, "link_url": link_url, "expires_at": expires_at.isoformat()}


def prepare_account_link_redirect(
    session: Session,
    link_token: str,
    employee_code: str,
    bind_token: str,
) -> str:
    link_session = session.exec(
        select(LineLinkSession).where(LineLinkSession.link_token == link_token)
    ).first()
    if not link_session:
        raise LinePlatformError("找不到有效的 LINE 綁定流程，請從 LINE Rich Menu 重新開始。")
    if link_session.expires_at < datetime.utcnow():
        link_session.status = LineLinkStatus.expired
        session.add(link_session)
        session.commit()
        raise LinePlatformError("LINE 綁定連結已過期，請重新開始。")

    employee = session.exec(
        select(Employee).where(Employee.employee_code == employee_code, Employee.bind_token == bind_token)
    ).first()
    if not employee:
        raise LinePlatformError("員工代碼或綁定碼錯誤，請向行政確認。")

    nonce = secrets.token_urlsafe(24)
    link_session.employee_code = employee.employee_code
    link_session.bind_token_snapshot = bind_token
    link_session.nonce = nonce
    link_session.status = LineLinkStatus.authorized
    session.add(link_session)
    session.commit()

    return f"https://access.line.me/dialog/bot/accountLink?{urlencode({'linkToken': link_token, 'nonce': nonce})}"


def complete_account_link_session(session: Session, line_user_id: str, nonce: str, result: str) -> dict[str, Any]:
    link_session = session.exec(
        select(LineLinkSession).where(LineLinkSession.nonce == nonce)
    ).first()
    if not link_session:
        raise LinePlatformError("找不到對應的帳號綁定流程。")

    link_session.line_user_id = line_user_id
    link_session.completed_at = datetime.utcnow()
    if result != "ok":
        link_session.status = LineLinkStatus.failed
        session.add(link_session)
        session.commit()
        return {"status": "failed"}

    employee = session.exec(
        select(Employee).where(Employee.employee_code == link_session.employee_code)
    ).first()
    if not employee:
        raise LinePlatformError("綁定流程存在，但對應員工不存在。")

    employee.line_user_id = line_user_id
    link_session.status = LineLinkStatus.completed
    session.add(employee)
    session.add(link_session)
    session.commit()
    return {"status": "completed", "employee_code": employee.employee_code, "employee_name": employee.name}
