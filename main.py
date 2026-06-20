"""
astrbot_plugin_rollpigs - 今天是什么小猪

将 NoneBot 插件 nonebot-plugin-rollpig 移植为 AstrBot 插件。
功能：今日小猪、随机小猪、找猪、云端资源同步。

原作者: Bearlele
来源: https://github.com/Bearlele/nonebot-plugin-rollpig
"""

import asyncio
import hashlib
import json
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from pydantic import BaseModel

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp

# ================================ 路径常量 ================================ #

PLUGIN_NAME = "astrbot_plugin_rollpigs"
PLUGIN_DIR = Path(__file__).parent
BUILTIN_RESOURCE_DIR = PLUGIN_DIR / "resource"
BUILTIN_PIG_JSON = BUILTIN_RESOURCE_DIR / "pig.json"
BUILTIN_IMAGE_DIR = BUILTIN_RESOURCE_DIR / "image"
BUILTIN_FONT_DIR = BUILTIN_RESOURCE_DIR / "font"

# 持久化数据目录 (data 目录下，防止插件更新时数据丢失)
DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
RECORDS_PATH = DATA_DIR / "records.json"
CACHE_ROOT = DATA_DIR / "resources"
ACTIVE_RESOURCE_DIR = CACHE_ROOT / "active"
STATE_FILE = CACHE_ROOT / "state.json"

PIG_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ALLOWED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


# ================================ 数据模型 ================================ #


class PigInfo(BaseModel):
    """PigHub 猪猪信息"""

    id: str
    title: str
    image_type: str = ""
    view_count: int = 0
    download_count: int = 0
    thumbnail: str = ""
    duration: str = ""
    filename: str = ""
    mtime: int = 0


class Pigsonality(BaseModel):
    """今日猪格 - 本地猪格池中的条目"""

    id: str
    name: str
    description: str
    analysis: str


class PigRecord(BaseModel):
    """用户抽取记录"""

    pig_id: str
    date: str


@dataclass
class ResourceSyncResult:
    """资源同步结果"""

    updated: bool
    skipped: bool
    resource_version: str = ""
    message: str = ""


# ================================ 资源管理器 ================================ #


class RollPigResourceManager:
    """
    小猪资源管理器。

    管理内置资源和云端同步资源。云端资源下载到 staging 目录，
    全部校验通过后才切换到 active，失败时回退到内置资源。
    """

    def __init__(self, config: AstrBotConfig) -> None:
        self._config = config
        self.resource_dir = BUILTIN_RESOURCE_DIR
        self.image_dirs: list[Path] = [BUILTIN_IMAGE_DIR]
        self.resource_version = "builtin"

    def reload(self) -> None:
        """加载活跃缓存资源，校验失败则回退到内置资源。"""
        active_pig_json = ACTIVE_RESOURCE_DIR / "pig.json"
        if active_pig_json.exists():
            try:
                self._validate_pig_json(active_pig_json)
                self.resource_dir = ACTIVE_RESOURCE_DIR
                self.image_dirs = [ACTIVE_RESOURCE_DIR / "images", BUILTIN_IMAGE_DIR]
                self.resource_version = self._read_state_version() or "cloud"
                logger.info(f"rollpig 资源已加载: version={self.resource_version}")
                return
            except Exception as error:
                logger.warning(f"rollpig 云端资源缓存读取失败，回退到内置资源: {error}")

        self.resource_dir = BUILTIN_RESOURCE_DIR
        self.image_dirs = [BUILTIN_IMAGE_DIR]
        self.resource_version = "builtin"
        logger.info("rollpig 使用内置资源")

    def get_pig_json_path(self) -> Path:
        return self.resource_dir / "pig.json"

    def find_image_file(self, pig_id: str) -> Optional[Path]:
        """在多个图片目录中按 pig_id 查找图片文件。"""
        for image_dir in self.image_dirs:
            for suffix in ALLOWED_IMAGE_SUFFIXES:
                image_file = image_dir / f"{pig_id}{suffix}"
                if image_file.exists():
                    return image_file
        return None

    # ---- 云端同步 ----

    async def sync_from_remote(self, *, force: bool = False) -> ResourceSyncResult:
        """从云端同步资源。先下载到 staging，校验通过后才切换 active。"""
        sync_enabled = self._config.get("resource_sync_enabled", True)
        if not sync_enabled and not force:
            return ResourceSyncResult(
                updated=False, skipped=True, message="云端资源同步未启用"
            )

        manifest_url = str(
            self._config.get(
                "resource_manifest_url",
                "https://pig.felislab.cc/resources/rollpig/manifest.json",
            )
        ).strip()
        if not manifest_url:
            return ResourceSyncResult(
                updated=False, skipped=True, message="未配置资源 manifest URL"
            )

        timeout = max(
            1.0, float(self._config.get("resource_sync_timeout", 10.0))
        )
        max_size = int(self._config.get("resource_max_file_size", 10 * 1024 * 1024))

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            manifest = await self._download_json(client, manifest_url, max_size=max_size)
            resource_version = str(manifest.get("resource_version") or "").strip()
            if not resource_version:
                raise ValueError("manifest 缺少 resource_version")

            if not force and resource_version == self._read_state_version():
                return ResourceSyncResult(
                    updated=False,
                    skipped=True,
                    resource_version=resource_version,
                    message=f"小猪资源已是最新：{resource_version}",
                )

            staging_dir = CACHE_ROOT / f"staging-{int(time.time())}"
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            staging_dir.mkdir(parents=True, exist_ok=True)

            try:
                await self._download_manifest_files(
                    client,
                    manifest_url=manifest_url,
                    manifest=manifest,
                    staging_dir=staging_dir,
                    max_size=max_size,
                )
                self._activate_staging(
                    staging_dir, manifest=manifest, resource_version=resource_version
                )
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

        return ResourceSyncResult(
            updated=True,
            skipped=False,
            resource_version=resource_version,
            message=f"小猪资源同步完成：{resource_version}",
        )

    async def _download_manifest_files(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        manifest: dict[str, Any],
        staging_dir: Path,
        max_size: int,
    ) -> None:
        """下载 manifest 中列出的所有文件到 staging 目录。"""
        pig_json_meta = manifest.get("pig_json")
        if not isinstance(pig_json_meta, dict):
            raise ValueError("manifest 缺少 pig_json")
        await self._download_file(
            client,
            manifest_url=manifest_url,
            meta=pig_json_meta,
            target=staging_dir / "pig.json",
            max_size=max_size,
        )
        self._validate_pig_json(staging_dir / "pig.json")

        image_items = manifest.get("images")
        if not isinstance(image_items, list):
            raise ValueError("manifest 缺少 images 列表")
        image_dir = staging_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for item in image_items:
            if not isinstance(item, dict):
                raise ValueError("manifest images 存在非法条目")
            filename = str(
                item.get("filename") or Path(str(item.get("path") or "")).name
            )
            self._validate_image_filename(filename)
            await self._download_file(
                client,
                manifest_url=manifest_url,
                meta=item,
                target=image_dir / filename,
                max_size=max_size,
            )

    async def _download_json(
        self, client: httpx.AsyncClient, url: str, *, max_size: int
    ) -> dict[str, Any]:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > max_size:
            raise ValueError(f"manifest 过大: {len(content)}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("manifest 必须是 JSON object")
        return data

    async def _download_file(
        self,
        client: httpx.AsyncClient,
        *,
        manifest_url: str,
        meta: dict[str, Any],
        target: Path,
        max_size: int,
    ) -> None:
        """下载单个资源文件，支持 size 和 sha256 校验。"""
        path = str(meta.get("path") or "").strip()
        if not path:
            raise ValueError("manifest 文件条目缺少 path")
        url = urljoin(manifest_url, path)
        response = await client.get(url)
        response.raise_for_status()
        content = response.content

        expected_size = int(meta.get("size") or 0)
        if len(content) > max_size:
            raise ValueError(f"资源文件过大: {path}")
        if expected_size and len(content) != expected_size:
            raise ValueError(f"资源文件大小不匹配: {path}")

        expected_sha256 = str(meta.get("sha256") or "").lower()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(f"资源文件 sha256 不匹配: {path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def _activate_staging(
        self,
        staging_dir: Path,
        *,
        manifest: dict[str, Any],
        resource_version: str,
    ) -> None:
        """将 staging 目录原子性切换为 active。"""
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        backup_dir = CACHE_ROOT / "previous"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if ACTIVE_RESOURCE_DIR.exists():
            ACTIVE_RESOURCE_DIR.replace(backup_dir)
        staging_dir.replace(ACTIVE_RESOURCE_DIR)
        STATE_FILE.write_text(
            json.dumps(
                {"resource_version": resource_version, "manifest": manifest},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # ---- 校验工具 ----

    def _read_state_version(self) -> str:
        if not STATE_FILE.exists():
            return ""
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return str(data.get("resource_version") or "")
        except Exception:
            return ""

    @staticmethod
    def _validate_pig_json(path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"pig.json 必须是 list: {path}")
        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("pig.json 存在非字典条目")
            pig_id = str(item.get("id") or "")
            if not PIG_ID_PATTERN.match(pig_id):
                raise ValueError(f"非法 pig_id: {pig_id}")
            if pig_id in seen_ids:
                raise ValueError(f"重复 pig_id: {pig_id}")
            seen_ids.add(pig_id)

    @staticmethod
    def _validate_image_filename(filename: str) -> None:
        path = Path(filename)
        if path.name != filename or path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"非法图片文件名: {filename}")
        pig_id = path.stem
        if not PIG_ID_PATTERN.match(pig_id):
            raise ValueError(f"非法图片 ID: {filename}")


# ================================ 猪圈 ================================ #


class Pigsty:
    """
    猪圈 - 管理猪猪数据和用户记录的核心业务类。

    职责：
    - 维护 PigHub 在线猪猪列表 (pigs)
    - 维护本地猪格池 (pig_pool)，用于"今日小猪"抽取
    - 管理用户每日抽取记录 (records) 的持久化
    """

    def __init__(
        self, resource_manager: RollPigResourceManager, records_path: Path
    ) -> None:
        self._rm = resource_manager
        self._records_path = records_path
        self.pigs: list[PigInfo] = []
        self.pig_pool: list[Pigsonality] = []
        self.records: dict[str, PigRecord] = {}
        self._records_lock = asyncio.Lock()

    async def load_pigsty(self) -> None:
        """初始化加载：猪格池 + 用户记录 + PigHub 数据。"""
        self._load_pigsonalities()
        self._load_records()
        await self._refresh_pigsty()

    def _load_records(self) -> None:
        if self._records_path.exists():
            try:
                data = json.loads(self._records_path.read_text(encoding="utf-8"))
                self.records = {
                    uid: PigRecord(**rec) for uid, rec in data.items()
                }
            except (json.JSONDecodeError, Exception):
                self.records = {}

    def _save_records(self) -> None:
        self._records_path.parent.mkdir(parents=True, exist_ok=True)
        self._records_path.write_text(
            json.dumps(
                {uid: rec.model_dump() for uid, rec in self.records.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def check_user_record(self, user_id: str) -> Optional[PigRecord]:
        """检查用户今天是否已有抽取记录。"""
        record = self.records.get(user_id)
        if record and record.date == datetime.now().strftime("%Y-%m-%d"):
            return record
        return None

    async def save_user_record(self, user_id: str, pig_id: str) -> None:
        """保存用户今日抽取记录。"""
        async with self._records_lock:
            self.records[user_id] = PigRecord(
                pig_id=pig_id, date=datetime.now().strftime("%Y-%m-%d")
            )
            self._save_records()

    async def _refresh_pigsty(self) -> None:
        """从 PigHub API 刷新在线猪猪数据。"""
        url = "https://pighub.top/api/all-images"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
            data = response.json()
            if data and data.get("images"):
                self.pigs = [PigInfo(**pig) for pig in data["images"]]
                logger.info(f"成功从 PigHub 缓存 {len(self.pigs)} 头猪猪")
            else:
                logger.warning("PigHub 中找不到猪猪")
        except Exception as error:
            logger.warning(f"刷新 PigHub 失败: {error}")

    def _load_pigsonalities(self) -> None:
        """从本地文件加载猪格池数据。"""
        pig_json_path = self._rm.get_pig_json_path()
        try:
            self.pig_pool = [
                Pigsonality(**pig)
                for pig in json.load(pig_json_path.open(encoding="utf-8"))
            ]
            if not self.pig_pool:
                logger.warning("没有找到今日小猪记录，无法抽取")
            else:
                logger.info(
                    f"已加载 {len(self.pig_pool)} 条今日小猪记录，"
                    f"资源版本: {self._rm.resource_version}"
                )
        except Exception as error:
            logger.warning(f"加载猪格池失败: {error}")

    async def random_pigs(self, count: int = 1) -> list[PigInfo]:
        """从 PigHub 随机获取指定数量的猪猪。"""
        if not self.pigs:
            await self._refresh_pigsty()
        if not self.pigs:
            return []
        return random.sample(self.pigs, min(count, len(self.pigs)))

    def catch_today_pig(self, exclude_id: Optional[str] = None) -> Pigsonality:
        """随机选一只今日猪格；可传入上次抽到的 id 以避免重复。"""
        if not self.pig_pool:
            self._load_pigsonalities()
        candidates = [p for p in self.pig_pool if p.id != exclude_id] or self.pig_pool
        return random.choice(candidates)

    def get_pigsonality_img(self, pig_id: str) -> Optional[Path]:
        """获取指定猪格的图片文件路径。"""
        pigsonality = next(
            (pig for pig in self.pig_pool if pig.id == pig_id), None
        )
        if pigsonality:
            return self._rm.find_image_file(pigsonality.id)
        return None

    def get_pigsonality_by_id(self, pig_id: str) -> Optional[Pigsonality]:
        return next(
            (pig for pig in self.pig_pool if pig.id == pig_id), None
        )


# ================================ 插件主类 ================================ #


@register(
    "astrbot_plugin_rollpigs", "Zzzzzzouhang", "抽取属于自己的小猪", "1.1.0"
)
class RollPigPlugin(Star):
    """
    今天是什么小猪 - AstrBot 插件

    命令列表：
    - 今日小猪 (别名: 今天是什么小猪/本日小猪/当日小猪)
    - 随机小猪 [数量]
    - 找猪 [关键词] 或 找猪 id [ID]
    - 同步小猪资源 (仅管理员，别名: 刷新小猪图鉴)
    """

    # ================================ 渲染布局常量 ================================ #
    CANVAS_WIDTH = 800  # 画布宽度
    CANVAS_HEIGHT = 800  # 画布高度
    AVATAR_SIZE = 280  # 头像大小
    SPACING_AVATAR_NAME = 20  # 头像与名称间距
    SPACING_NAME_DESC = 25  # 名称与描述间距
    SPACING_DESC_ANALYSIS = 30  # 描述与解析间距
    NAME_FONT_SIZE = 66  # 名称字体大小
    DESC_FONT_SIZE = 32  # 描述字体大小
    ANALYSIS_FONT_SIZE = 28  # 解析字体大小
    ANALYSIS_LINE_HEIGHT_FACTOR = 1.6  # 解析行高因子
    ANALYSIS_WIDTH_RATIO = 0.85  # 解析宽度比例

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 确保数据目录存在
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 初始化资源管理器和猪圈
        self.resource_manager = RollPigResourceManager(config)
        self.pigsty = Pigsty(self.resource_manager, RECORDS_PATH)

        # 初始化字体（优先插件内置字体，跨平台兼容回退）
        self.font_regular = self._init_regular_font()  # 常规字体（描述/解析）
        self.font_bold = self._init_bold_font()  # 加粗字体（名称）

        # 后台任务引用
        self._bg_tasks: list[asyncio.Task] = []

    async def initialize(self):
        """插件启动：加载模板、资源，启动后台定时任务。"""
        # 加载资源
        self.resource_manager.reload()

        # 启动时尝试云端同步
        sync_enabled = self.config.get("resource_sync_enabled", True)
        if sync_enabled:
            try:
                result = await self.resource_manager.sync_from_remote(force=False)
                if result.updated:
                    self.resource_manager.reload()
                logger.info(result.message or "小猪资源启动同步完成")
            except Exception as error:
                logger.warning(f"rollpig 云端资源启动同步失败，继续使用当前资源: {error}")

        # 加载猪圈数据
        await self.pigsty.load_pigsty()

        # 启动后台定时任务
        self._bg_tasks = [
            asyncio.create_task(self._daily_refresh_loop()),
            asyncio.create_task(self._resource_sync_loop()),
        ]

    async def terminate(self):
        """插件销毁时取消后台任务。"""
        for task in self._bg_tasks:
            task.cancel()
        self._bg_tasks.clear()

    # ================================ 后台任务 ================================ #

    async def _daily_refresh_loop(self):
        """每天 0:00 刷新 PigHub 数据。"""
        while True:
            try:
                now = datetime.now()
                tomorrow = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                wait_seconds = (tomorrow - now).total_seconds()
                await asyncio.sleep(wait_seconds)
                await self.pigsty._refresh_pigsty()
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.warning(f"rollpig 每日刷新失败: {error}")
                await asyncio.sleep(60)  # 出错后等 1 分钟再重试

    async def _resource_sync_loop(self):
        """按配置间隔定时同步云端资源。"""
        while True:
            try:
                interval_hours = max(
                    1, int(self.config.get("resource_sync_interval_hours", 24))
                )
                await asyncio.sleep(interval_hours * 3600)

                if not self.config.get("resource_sync_enabled", True):
                    continue

                result = await self.resource_manager.sync_from_remote(force=False)
                if result.updated:
                    self.resource_manager.reload()
                    self.pigsty._load_pigsonalities()
                logger.info(result.message or "小猪资源定时同步完成")
            except asyncio.CancelledError:
                break
            except Exception as error:
                logger.warning(f"rollpig 云端资源定时同步失败: {error}")
                await asyncio.sleep(60)

    # ================================ 辅助方法 ================================ #

    # ---- 字体加载 ----

    def _load_font(
        self, font_candidates: list[Path], size: int, purpose: str
    ) -> ImageFont.FreeTypeFont:
        """按候选顺序加载可用字体，全部失败则回退到 PIL 默认字体。"""
        for font_path in font_candidates:
            if Path(font_path).exists():
                try:
                    return ImageFont.truetype(str(font_path), size)
                except Exception as error:
                    logger.warning(f"加载{purpose}字体 {font_path} 失败：{error}")
                    continue
        logger.warning(f"未找到{purpose}字体，使用默认字体")
        return ImageFont.load_default()

    def _init_regular_font(self) -> ImageFont.FreeTypeFont:
        """初始化常规字体（可爱字体，用于描述/解析）。"""
        return self._load_font(
            [
                BUILTIN_FONT_DIR / "可爱字体.ttf",
                BUILTIN_FONT_DIR / "SourceHanSansCN-Regular.otf",
                Path("C:/Windows/Fonts/msyh.ttc"),
                Path("C:/Windows/Fonts/simhei.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ],
            self.DESC_FONT_SIZE,
            "常规",
        )

    def _init_bold_font(self) -> ImageFont.FreeTypeFont:
        """初始化加粗字体（荆南麦圆体，用于名称）。"""
        return self._load_font(
            [
                BUILTIN_FONT_DIR / "荆南麦圆体.otf",
                BUILTIN_FONT_DIR / "SourceHanSansCN-Bold.otf",
                Path("C:/Windows/Fonts/msyhbd.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ],
            self.NAME_FONT_SIZE,
            "加粗",
        )

    # ---- 绘制工具 ----

    @staticmethod
    def _get_text_size(
        text: str, font: ImageFont.FreeTypeFont
    ) -> tuple[int, int]:
        """计算文字宽高（兼容不同版本 PIL）。"""
        draw = ImageDraw.Draw(PILImage.new("RGB", (1, 1)))
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return (bbox[2] - bbox[0], bbox[3] - bbox[1])
        except Exception:
            return draw.textsize(text, font=font)

    @staticmethod
    def _draw_bold_text(
        draw: ImageDraw.ImageDraw,
        pos: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple[int, int, int],
    ) -> None:
        """通过四向描边模拟文字加粗（兜底方案）。"""
        x, y = pos
        for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            draw.text((x + ox, y + oy), text, fill=fill, font=font)
        draw.text((x, y), text, fill=fill, font=font)

    # ---- PIL 像素级居中渲染 ----

    def render_pig_image(self, pig_data: Pigsonality) -> Optional[Path]:
        """
        将猪格数据渲染为 800x800 图片（水平+垂直双居中）。

        通过预先计算内容总高度并推导起始 Y 坐标实现像素级垂直居中，
        每个元素再各自水平居中，避免 HTML 渲染在固定画布内居中不可靠的问题。
        """
        canvas_w = self.CANVAS_WIDTH
        canvas_h = self.CANVAS_HEIGHT
        canvas = PILImage.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        pig_id = pig_data.id
        pig_name = pig_data.name or "未知小猪"
        pig_desc = pig_data.description or ""
        pig_analysis = pig_data.analysis or ""

        # 1. 头像（缩放到 280x280 并居中裁剪为正方形）
        avatar_w = avatar_h = self.AVATAR_SIZE
        avatar = None
        avatar_file = self.pigsty.get_pigsonality_img(pig_id)
        if avatar_file:
            try:
                avatar = PILImage.open(avatar_file)
                avatar.thumbnail((avatar_w, avatar_h))
                if avatar.size != (avatar_w, avatar_h):
                    half = self.AVATAR_SIZE // 2
                    cx, cy = avatar.width // 2, avatar.height // 2
                    avatar = avatar.crop((cx - half, cy - half, cx + half, cy + half))
            except Exception as error:
                logger.error(f"加载小猪图片失败：{error}")
                avatar = None
        else:
            logger.warning(f"未找到图片: {pig_id}.*")

        # 2. 名称尺寸
        name_font = self.font_bold
        name_w, name_h = self._get_text_size(pig_name, name_font)

        # 3. 描述尺寸
        desc_font = self.font_regular.font_variant(size=self.DESC_FONT_SIZE)
        desc_w, desc_h = self._get_text_size(pig_desc, desc_font)

        # 4. 解析（按宽度自动换行后计算总高）
        analysis_font = self.font_regular.font_variant(size=self.ANALYSIS_FONT_SIZE)
        line_height = int(self.ANALYSIS_FONT_SIZE * self.ANALYSIS_LINE_HEIGHT_FACTOR)
        max_analysis_width = int(canvas_w * self.ANALYSIS_WIDTH_RATIO)
        analysis_lines: list[str] = []
        current_line = ""
        for char in pig_analysis:
            current_line += char
            line_w, _ = self._get_text_size(current_line, analysis_font)
            if line_w > max_analysis_width:
                analysis_lines.append(current_line[:-1])
                current_line = char
        if current_line:
            analysis_lines.append(current_line)
        analysis_total_h = max(len(analysis_lines), 1) * line_height

        # 5. 计算内容总高度 → 垂直居中起始 Y 坐标（核心）
        total_content_h = (
            avatar_h
            + self.SPACING_AVATAR_NAME
            + name_h
            + self.SPACING_NAME_DESC
            + desc_h
            + self.SPACING_DESC_ANALYSIS
            + analysis_total_h
        )
        start_y = (canvas_h - total_content_h) // 2

        # 6.1 绘制头像（水平+垂直居中）
        avatar_x = (canvas_w - avatar_w) // 2
        avatar_y = start_y
        if avatar:
            canvas.paste(
                avatar,
                (avatar_x, avatar_y),
                mask=avatar if avatar.mode == "RGBA" else None,
            )

        # 6.2 绘制名称（水平居中）
        name_y = avatar_y + avatar_h + self.SPACING_AVATAR_NAME
        name_x = (canvas_w - name_w) // 2
        self._draw_bold_text(draw, (name_x, name_y), pig_name, name_font, (0, 0, 0))

        # 6.3 绘制描述（水平居中）
        desc_y = name_y + name_h + self.SPACING_NAME_DESC
        desc_x = (canvas_w - desc_w) // 2
        draw.text((desc_x, desc_y), pig_desc, fill=(85, 85, 85), font=desc_font)

        # 6.4 绘制解析（逐行水平居中）
        analysis_y = desc_y + desc_h + self.SPACING_DESC_ANALYSIS
        for line in analysis_lines:
            line_w, _ = self._get_text_size(line, analysis_font)
            line_x = (canvas_w - line_w) // 2
            draw.text((line_x, analysis_y), line, fill=(51, 51, 51), font=analysis_font)
            analysis_y += line_height

        # 7. 保存临时文件
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                canvas.save(tmp_path, format="PNG", quality=95)
            if not tmp_path.exists():
                logger.error(f"临时文件创建失败：{tmp_path}")
                return None
            return tmp_path
        except Exception as error:
            logger.error(f"合成图片失败：{error}")
            return None

    async def _render_pig_image(self, pig_data: Pigsonality) -> Optional[str]:
        """在线程池中执行 CPU 密集的 PIL 渲染，返回临时图片路径字符串。"""
        return await asyncio.to_thread(self.render_pig_image, pig_data)

    async def _send_pig_image(self, event: AstrMessageEvent, pig_data: Pigsonality) -> bool:
        """
        渲染并发送今日小猪图片。

        成功发送并清理临时文件返回 True；渲染或发送失败返回 False（调用方负责降级）。
        """
        img_path = await self._render_pig_image(pig_data)
        if not img_path or not Path(img_path).exists():
            return False

        try:
            await event.send(event.image_result(str(img_path)))
            logger.info("今日小猪合成图片发送成功")
            return True
        except Exception as error:
            logger.error(f"发送合成图片失败：{error}")
            return False
        finally:
            try:
                Path(img_path).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(f"清理临时图片失败：{cleanup_err}")

    # ================================ 命令处理器 ================================ #

    @filter.command("今日小猪", alias={"今天是什么小猪", "本日小猪", "当日小猪"})
    async def todays_pig(self, event: AstrMessageEvent):
        """抽取今天属于你的小猪（每次都重新随机一只新的）"""
        user_id = str(event.get_sender_id())

        # 取出上次抽到的小猪 id，避免连续两次抽到同一只
        last_record = self.pigsty.check_user_record(user_id)
        exclude_id = last_record.pig_id if last_record else None

        # 每次都重新随机一只新的小猪（排除上一只，确保和上次不同）
        pig = self.pigsty.catch_today_pig(exclude_id=exclude_id)
        await self.pigsty.save_user_record(user_id, pig.id)

        if not await self._send_pig_image(event, pig):
            yield event.plain_result(
                f"今日小猪：{pig.name}\n{pig.description}\n{pig.analysis}"
            )

    @filter.command("随机小猪")
    async def roll_pig(self, event: AstrMessageEvent, count: int = 1):
        """从PigHub随机获取猪猪图，可指定数量(1-20)"""
        try:
            count = max(1, min(20, int(count)))
        except (ValueError, TypeError):
            count = 1

        pigs = await self.pigsty.random_pigs(count)
        if not pigs:
            yield event.plain_result("猪圈空荡荡，一只猪都没有...")
            return

        if len(pigs) == 1:
            pig = pigs[0]
            image_url = "https://pighub.top/data/" + pig.thumbnail.split("/")[-1]
            yield event.image_result(image_url)
            return

        # 多张猪猪，用消息链发送
        chain: list = []
        for pig in pigs:
            image_url = "https://pighub.top/data/" + pig.thumbnail.split("/")[-1]
            chain.append(Comp.Plain(f"{pig.title} - {pig.id}\n"))
            chain.append(Comp.Image.fromURL(image_url))
            chain.append(Comp.Plain("\n"))
        yield event.chain_result(chain)

    @filter.command("找猪", alias={"搜猪"})
    async def find_pig(self, event: AstrMessageEvent, keyword: str = ""):
        """根据关键词查找猪猪，支持按ID查找(输入: id 数字)"""
        # 确保猪圈数据已加载
        if not self.pigsty.pigs:
            await self.pigsty._refresh_pigsty()
        if not self.pigsty.pigs:
            yield event.plain_result("猪圈空荡荡...")
            return

        # 从原始消息中提取完整参数 (去除指令名和前缀后的部分)
        raw_text = event.message_str.strip()
        # 尝试移除常见前缀和指令名
        args_text = keyword.strip()
        for prefix in ("/", ""):
            for cmd_name in ("找猪", "搜猪"):
                head = f"{prefix}{cmd_name}"
                if raw_text.startswith(head):
                    args_text = raw_text[len(head):].strip()
                    break
            else:
                continue
            break

        found_pigs: list[PigInfo] = []

        # 解析 "id <数字>" 模式
        id_match = re.match(r"^(?:-i|--id|id)\s+(\d+)$", args_text, re.IGNORECASE)
        if id_match:
            search_id = id_match.group(1)
            found_pigs = [pig for pig in self.pigsty.pigs if pig.id == search_id]
        elif args_text:
            kw = args_text.lower()
            found_pigs = [
                pig for pig in self.pigsty.pigs if kw in pig.title.lower()
            ]
        else:
            yield event.plain_result("请输入关键词或图片ID~\n用法：找猪 <关键词> 或 找猪 id <ID>")
            return

        if not found_pigs:
            yield event.plain_result("你要找的猪仔离家出走了~")
            return

        if len(found_pigs) == 1:
            pig = found_pigs[0]
            image_url = "https://pighub.top/data/" + pig.thumbnail.split("/")[-1]
            yield event.chain_result(
                [Comp.Plain(f"{pig.title} - {pig.id}\n"), Comp.Image.fromURL(image_url)]
            )
            return

        # 多只猪，最多显示 20 只
        chain: list = []
        for pig in found_pigs[:20]:
            image_url = "https://pighub.top/data/" + pig.thumbnail.split("/")[-1]
            chain.append(Comp.Plain(f"{pig.title} - {pig.id}\n"))
            chain.append(Comp.Image.fromURL(image_url))
            chain.append(Comp.Plain("\n"))
        if len(found_pigs) > 20:
            chain.append(Comp.Plain(f"(共找到 {len(found_pigs)} 只，仅显示前 20 只)"))
        yield event.chain_result(chain)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同步小猪资源", alias={"刷新小猪图鉴"})
    async def sync_resources(self, event: AstrMessageEvent):
        """手动同步云端小猪资源（仅管理员可用）"""
        try:
            result = await self.resource_manager.sync_from_remote(force=True)
        except Exception as error:
            logger.error(f"rollpig 小猪资源手动同步失败: {error}")
            yield event.plain_result(f"小猪资源同步失败：{error}")
            return

        if result.updated:
            self.resource_manager.reload()
            self.pigsty._load_pigsonalities()

        yield event.plain_result(
            f"{result.message or '小猪资源同步完成'}\n"
            f"当前资源版本：{self.resource_manager.resource_version}｜"
            f"小猪数量：{len(self.pigsty.pig_pool)}"
        )
