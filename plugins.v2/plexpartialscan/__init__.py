from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.helper.mediaserver import MediaServerHelper


class PlexPartialScan(_PluginBase):
    # 插件名称
    plugin_name = "Remote Plex Scanner"
    # 插件描述
    plugin_desc = "远程Plex局部扫描 - 跨服务器路径映射，自动刷新rclone缓存并触发Plex扫描"
    # 插件图标
    plugin_icon = "Plex_A.png"
    # 插件版本
    plugin_version = "2.2"
    # 插件作者
    plugin_author = "Yan-nian"
    # 作者主页
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "remoteplexscan_"
    # 加载顺序
    plugin_order = 18
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _onlyonce = False
    _auto_scan = True
    _delay = 10
    _plex_server = None
    _plex_url = None
    _plex_token = None
    _rclone_rc_url = None
    _path_mapping_local = None
    _path_mapping_remote = None
    _path_library_mapping = []  # [{"local": "/我的/动漫", "remote": "/media/动漫", "library_id": "5"}]
    _library_mapping = {}
    _notify = False
    _timeout = 30
    _refresh_rclone = True
    _scheduler: Optional[BackgroundScheduler] = None
    _scan_queue = []
    
    # 媒体服务器助手
    mediaserver_helper: MediaServerHelper = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        
        # 初始化媒体服务器助手
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._auto_scan = config.get("auto_scan", True)
            self._delay = config.get("delay", 10)
            self._plex_server = config.get("plex_server")
            self._rclone_rc_url = config.get("rclone_rc_url")
            self._timeout = config.get("timeout", 30)
            self._notify = config.get("notify", False)
            self._refresh_rclone = config.get("refresh_rclone", True)
            
            # 初始化路径映射变量
            self._path_mapping_local = None
            self._path_mapping_remote = None
            self._path_library_mapping = []
            
            # 尝试从系统获取Plex配置或解析媒体库配置
            self._init_plex_from_system(config)
            
            # 解析路径-库ID映射（优先级最高）
            path_library_mapping = config.get("path_library_mapping", "")
            if path_library_mapping:
                for line in path_library_mapping.strip().split("\n"):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    
                    parts = line.split(":")
                    if len(parts) == 3:
                        local_path = parts[0].strip()
                        remote_path = parts[1].strip()
                        library_id = parts[2].strip()
                        
                        self._path_library_mapping.append({
                            "local": local_path,
                            "remote": remote_path,
                            "library_id": library_id
                        })
                        logger.info(f"Remote Plex Scanner: 路径映射 {local_path} -> {remote_path} (库{library_id})")
            
            # 解析路径映射
            path_mapping = config.get("path_mapping", "")
            if path_mapping:
                # 支持冒号或竖线作为分隔符
                if ":" in path_mapping and "|" not in path_mapping:
                    # Docker compose风格：/我的/:/media/
                    parts = path_mapping.split(":")
                    self._path_mapping_local = parts[0].strip()
                    self._path_mapping_remote = parts[1].strip()
                elif "|" in path_mapping:
                    # 传统风格：/我的/|/media/
                    parts = path_mapping.split("|")
                    self._path_mapping_local = parts[0].strip()
                    self._path_mapping_remote = parts[1].strip()
                else:
                    # 简单模式：只有远程路径
                    self._path_mapping_local = None
                    self._path_mapping_remote = path_mapping.strip()
            
            # 解析库映射
            library_mapping = config.get("library_mapping", "")
            self._library_mapping = {}
            if library_mapping:
                for item in library_mapping.split(","):
                    if ":" in item:
                        key, value = item.split(":")
                        self._library_mapping[key.strip().lower()] = value.strip()

        # 验证配置
        if self._enabled:
            logger.info("=" * 60)
            logger.info("Remote Plex Scanner: 插件配置摘要")
            logger.info("=" * 60)
            
            if not self._plex_url or not self._plex_token:
                logger.error("Remote Plex Scanner: 无法获取Plex配置，请确保已在系统中配置Plex服务器或手动填写")
            else:
                logger.info(f"Remote Plex Scanner: 使用Plex服务器 {self._plex_url}")
                
            if not self._rclone_rc_url:
                logger.warning("Remote Plex Scanner: rclone RC地址未配置，将跳过缓存刷新")
            else:
                logger.info(f"Remote Plex Scanner: rclone RC地址 {self._rclone_rc_url}")
                
            # 检查路径映射配置（优先检查路径-库ID映射）
            if self._path_library_mapping:
                # 已有路径-库ID映射，无需其他路径映射
                pass
            elif not self._path_mapping_remote:
                logger.warning("Remote Plex Scanner: 路径映射未配置，将使用原始路径")
            else:
                if self._path_mapping_local:
                    logger.info(f"Remote Plex Scanner: 路径映射 {self._path_mapping_local} -> {self._path_mapping_remote}")
                else:
                    logger.info(f"Remote Plex Scanner: 115网盘模式，【u115】-> {self._path_mapping_remote}")
            
            logger.info(f"Remote Plex Scanner: 自动扫描 = {self._auto_scan}")
            logger.info(f"Remote Plex Scanner: 延迟时间 = {self._delay} 秒")
            
            if self._path_library_mapping:
                logger.info(f"Remote Plex Scanner: 路径-库ID映射:")
                for mapping in self._path_library_mapping:
                    logger.info(f"  {mapping['local']} -> {mapping['remote']} (库{mapping['library_id']})")
            elif self._library_mapping:
                logger.info(f"Remote Plex Scanner: 库映射配置: {self._library_mapping}")
            else:
                logger.warning("Remote Plex Scanner: 未配置媒体库")
                
            logger.info("=" * 60)

    def _init_plex_from_system(self, config: dict):
        """
        从系统或配置中初始化Plex连接
        优先使用系统配置的Plex，如果没有则使用手动填写的配置
        """
        # 首先尝试从用户手动配置获取（如果填写了）
        manual_url = config.get("plex_url", "").strip()
        manual_token = config.get("plex_token", "").strip()
        
        # 如果用户手动填写了完整配置，直接使用
        if manual_url and manual_token:
            self._plex_url = manual_url
            self._plex_token = manual_token
            logger.info(f"Remote Plex Scanner: 使用手动配置的Plex服务器")
            return
        
        # 优先使用用户选择的Plex服务器
        if self._plex_server and self.mediaserver_helper:
            try:
                service = self.mediaserver_helper.get_service(name=self._plex_server, type_filter="plex")
                if service and service.instance:
                    plex_instance = service.instance
                    if hasattr(plex_instance, '_host') and hasattr(plex_instance, '_token'):
                        self._plex_url = manual_url or plex_instance._host
                        self._plex_token = manual_token or plex_instance._token
                        logger.info(f"Remote Plex Scanner: 使用选择的Plex服务器 ({self._plex_server})")
                        return
            except Exception as e:
                logger.warning(f"Remote Plex Scanner: 获取选择的Plex服务器失败: {str(e)}")
        
        # 如果以上都失败，尝试获取系统中的任意Plex配置
        if self.mediaserver_helper and not (manual_url and manual_token):
            try:
                services = self.mediaserver_helper.get_services(type_filter="plex")
                if services:
                    # 使用第一个Plex服务器
                    first_service = list(services.values())[0]
                    if first_service and first_service.instance:
                        plex_instance = first_service.instance
                        if hasattr(plex_instance, '_host') and hasattr(plex_instance, '_token'):
                            self._plex_url = manual_url or plex_instance._host
                            self._plex_token = manual_token or plex_instance._token
                            logger.info(f"Remote Plex Scanner: 从系统获取Plex配置 ({first_service.name})")
                            return
            except Exception as e:
                logger.warning(f"Remote Plex Scanner: 获取系统Plex配置失败: {str(e)}")
        
        # 最后使用手动配置（可能为空）
        self._plex_url = manual_url
        self._plex_token = manual_token
        
        if not self._plex_url:
            logger.warning("Remote Plex Scanner: 未找到Plex配置，请在系统中配置Plex或手动填写")

        if self._enabled or self._onlyonce:
            # 立即运行一次（测试用）
            if self._onlyonce:
                logger.info("Remote Plex Scanner: 立即运行一次测试...")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.test_connection,
                    trigger='date',
                    run_date=datetime.now() + timedelta(seconds=3),
                    name="Remote Plex Scanner 测试"
                )
                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "enabled": self._enabled,
                    "onlyonce": False,
                    "auto_scan": self._auto_scan,
                    "delay": self._delay,
                    "plex_url": self._plex_url,
                    "plex_token": self._plex_token,
                    "rclone_rc_url": self._rclone_rc_url,
                    "path_mapping": f"{self._path_mapping_local}:{self._path_mapping_remote}" if self._path_mapping_local else "",
                    "timeout": self._timeout,
                    "notify": self._notify
                })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/remote_plex_scan",
            "event": EventType.PluginAction,
            "desc": "远程Plex扫描",
            "category": "Plex",
            "data": {
                "action": "remote_plex_scan"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [{
            "path": "/scan_path",
            "endpoint": self.scan_path_api,
            "methods": ["POST"],
            "summary": "扫描指定路径",
            "description": "刷新rclone缓存并扫描指定路径"
        }, {
            "path": "/test_connection",
            "endpoint": self.test_connection_api,
            "methods": ["GET"],
            "summary": "测试连接",
            "description": "测试Plex和rclone连接状态"
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '测试连接',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'refresh_rclone',
                                            'label': '刷新rclone缓存',
                                            'hint': '扫描前刷新rclone VFS缓存（可选）',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_scan',
                                            'label': '自动扫描',
                                            'hint': '入库完成后自动触发远程扫描',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay',
                                            'label': '延迟扫描（秒）',
                                            'type': 'number',
                                            'hint': '等待文件上传到网盘的时间（建议30-60秒）',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'timeout',
                                            'label': '超时时间（秒）',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'plex_server',
                                            'label': 'Plex服务器',
                                            'items': self.__get_plex_server_options(),
                                            'hint': '选择系统中已配置的Plex服务器（推荐）',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'plex_url',
                                            'label': 'Plex服务器地址（可选）',
                                            'placeholder': 'http://192.168.1.100:32400 或留空使用上方选择',
                                            'hint': '仅在未选择服务器时需要手动填写',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'plex_token',
                                            'label': 'Plex Token（可选）',
                                            'placeholder': 'X-Plex-Token 或留空使用上方选择',
                                            'hint': '仅在未选择服务器时需要手动填写',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'rclone_rc_url',
                                            'label': 'Rclone RC地址 (服务器A)',
                                            'placeholder': 'http://192.168.1.100:5572',
                                            'hint': 'rclone mount --rc 的RC服务地址',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_library_mapping',
                                            'label': '路径-库ID映射（推荐）',
                                            'placeholder': '/我的/动漫:/media/动漫:5\n/我的/网盘剧:/media/网盘剧:4\n/我的/电影:/media/电影:3',
                                            'hint': '格式：本地路径:远程路径:库ID，每行一个映射',
                                            'persistent-hint': True,
                                            'rows': 3
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '📖 快速配置：\n'
                                                    '1. 在"设置→媒体服务器"中添加Plex\n'
                                                    '2. 填写路径-库ID映射（推荐）\n'
                                                    '   格式：/我的/动漫:/media/动漫:5\n'
                                                    '3. 延迟10-30秒\n'
                                                    '4. 点"测试连接"验证'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "refresh_rclone": True,
            "auto_scan": True,
            "delay": 10,
            "timeout": 30,
            "plex_server": "",
            "plex_url": "",
            "plex_token": "",
            "rclone_rc_url": "",
            "path_library_mapping": "/我的/动漫:/media/动漫:5\n/我的/网盘剧:/media/网盘剧:4\n/我的/电影:/media/电影:3",
            "path_mapping": "/我的/:/media/",
            "library_mapping": "movie:1,tv:2"
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        pass

    @eventmanager.register(EventType.TransferComplete)
    def listen_transfer_complete(self, event: Event):
        """
        监听入库完成事件（本地整理完成，等待上传）
        """
        try:
            logger.info("Remote Plex Scanner: 收到 TransferComplete 事件")
            
            if not self._enabled:
                logger.warning("Remote Plex Scanner: 插件未启用，跳过处理")
                return
                
            if not self._auto_scan:
                logger.warning("Remote Plex Scanner: 自动扫描未启用，跳过处理")
                return

            if not event:
                logger.error("Remote Plex Scanner: event 对象为 None")
                return

            event_data = event.event_data
            logger.info(f"Remote Plex Scanner: event_data 类型: {type(event_data)}")
            
            if not event_data:
                logger.warning("Remote Plex Scanner: event.event_data 为空")
                return

            # 获取入库信息
            mediainfo = event_data.get("mediainfo")
            transfer_info = event_data.get("transferinfo")
            
            logger.info(f"Remote Plex Scanner: mediainfo = {type(mediainfo)}, transferinfo = {type(transfer_info)}")

            if not transfer_info:
                logger.warning("Remote Plex Scanner: transferinfo 为空")
                logger.info(f"Remote Plex Scanner: event_data keys: {list(event_data.keys())}")
                return

            # 获取目标路径 - 使用 target_item 或 target_diritem
            target_path = None
            
            # 优先使用 target_item
            if hasattr(transfer_info, 'target_item') and transfer_info.target_item:
                target_item = transfer_info.target_item
                if hasattr(target_item, 'path'):
                    target_path = target_item.path
                elif isinstance(target_item, str):
                    target_path = target_item
            
            # 备选方案：target_diritem
            if not target_path and hasattr(transfer_info, 'target_diritem') and transfer_info.target_diritem:
                target_diritem = transfer_info.target_diritem
                if hasattr(target_diritem, 'path'):
                    target_path = target_diritem.path
                elif isinstance(target_diritem, str):
                    target_path = target_diritem
            
            if not target_path:
                logger.warning(f"Remote Plex Scanner: 无法从 TransferInfo 获取目标路径")
                logger.info(f"Remote Plex Scanner: target_item = {getattr(transfer_info, 'target_item', None)}")
                logger.info(f"Remote Plex Scanner: target_diritem = {getattr(transfer_info, 'target_diritem', None)}")
                return
            
            # 获取媒体类型
            media_type = mediainfo.type.value if mediainfo and hasattr(mediainfo, 'type') else None

            # 添加到扫描队列
            self._scan_queue.append({
                "path": target_path,
                "mediainfo": mediainfo,
                "media_type": media_type,
                "time": datetime.now()
            })

            queue_len = len(self._scan_queue)
            
            # 延迟扫描（等待文件上传到网盘）
            # 检查是否已有待执行的扫描任务，避免重复创建
            if not self._scheduler:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.start()
            
            job_id = "Remote_Plex_Scanner_Task"
            existing_job = self._scheduler.get_job(job_id)
            
            if not existing_job:
                # 只有在没有待执行任务时才创建新任务并输出详细日志
                self._scheduler.add_job(
                    func=self.process_scan_queue,
                    trigger='date',
                    run_date=datetime.now() + timedelta(seconds=self._delay),
                    id=job_id,
                    name="Remote Plex Scanner"
                )
                logger.info(f"Remote Plex Scanner: ✅ [{media_type or 'unknown'}] 加入队列 (1个)，{self._delay}秒后统一处理")
            else:
                # 后续文件只显示队列数量，不重复输出计划信息
                if queue_len % 10 == 0:  # 每10个文件输出一次
                    logger.info(f"Remote Plex Scanner: 📥 队列中 ({queue_len}个)")
            
        except Exception as e:
            logger.error(f"Remote Plex Scanner: 处理 TransferComplete 事件时出错: {str(e)}")
            logger.error(f"Remote Plex Scanner: 错误详情: {e.__class__.__name__}", exc_info=True)

    @eventmanager.register(EventType.PluginAction)
    def listen_plugin_action(self, event: Event):
        """
        监听插件动作事件
        """
        if not self._enabled:
            return

        event_data = event.event_data
        if not event_data:
            return

        action = event_data.get("action")
        if action != "remote_plex_scan":
            return

        logger.info("Remote Plex Scanner: 收到远程命令，开始测试连接...")
        self.test_connection()

    def _detect_media_type_from_path(self, path: str) -> Optional[str]:
        """
        根据路径判断媒体类型
        /media/电影/ -> movie
        /media/网盘剧/ -> tv
        /media/动漫/ -> anime
        """
        path_lower = path.lower()
        
        # 电影
        if any(keyword in path_lower for keyword in ['/电影/', '/movie', '/movies/']):
            return 'movie'
        
        # 动漫
        if any(keyword in path_lower for keyword in ['/动漫/', '/anime/', '/动画/']):
            return 'anime'
        
        # 电视剧
        if any(keyword in path_lower for keyword in ['/网盘剧/', '/电视剧/', '/tv/', '/series/', '/show']):
            return 'tv'
        
        return None

    def process_scan_queue(self):
        """
        处理扫描队列（文件应该已经上传到网盘）
        按目录去重，同一目录只扫描一次
        """
        if not self._scan_queue:
            return

        queue_length = len(self._scan_queue)
        logger.info(f"Remote Plex Scanner: ⏱️ 开始处理扫描队列 ({queue_length} 个任务)")

        # 按目录分组，去重
        dir_map = {}  # {扫描目录: [scan_items]}
        
        for scan_item in self._scan_queue[:]:
            try:
                local_path = scan_item.get("path")
                media_type = scan_item.get("media_type")
                
                # Step 1: 路径转换
                result = self.translate_path(local_path)
                if not result or not result[0]:
                    logger.error(f"Remote Plex Scanner: 路径转换失败: {local_path}")
                    continue
                
                remote_path, library_id = result

                # 提取目录路径
                if remote_path.endswith(('.mp4', '.mkv', '.avi', '.ts', '.m2ts')):
                    scan_dir = '/'.join(remote_path.split('/')[:-1]) + '/'
                else:
                    scan_dir = remote_path
                
                # 根据路径判断媒体类型（覆盖原有的media_type）
                detected_type = self._detect_media_type_from_path(scan_dir)
                if detected_type:
                    media_type = detected_type
                    logger.info(f"Remote Plex Scanner: 🎯 根据路径判定类型: {media_type}")
                
                # 按目录分组
                if scan_dir not in dir_map:
                    dir_map[scan_dir] = {
                        'items': [],
                        'media_type': media_type,
                        'library_id': library_id,  # 保存库ID
                        'rclone_done': False
                    }
                dir_map[scan_dir]['items'].append(scan_item)
                
            except Exception as e:
                logger.error(f"Remote Plex Scanner: 路径处理错误: {str(e)}")
                continue
        
        # 按目录统一处理
        logger.info(f"Remote Plex Scanner: 📁 合并后需扫描 {len(dir_map)} 个目录")
        
        for scan_dir, dir_info in dir_map.items():
            try:
                items = dir_info['items']
                media_type = dir_info['media_type']
                library_id = dir_info.get('library_id')  # 获取库ID
                file_count = len(items)
                
                logger.info(f"Remote Plex Scanner: ➡️ 目录: {scan_dir} ({file_count}个文件)")

                # Step 2: 刷新rclone缓存（每个目录只刷新一次）
                if self._refresh_rclone and self._rclone_rc_url:
                    try:
                        rclone_success = self.refresh_rclone_cache(scan_dir)
                        if not rclone_success:
                            logger.warning(f"Remote Plex Scanner: ⚠️ rclone刷新失败，继续扫描")
                    except Exception as e:
                        logger.error(f"Remote Plex Scanner: rclone错误: {str(e)}")

                # Step 3: 触发Plex扫描（每个目录只扫描一次）
                # 如果有指定的library_id，传递它；否则使用media_type
                scan_success = self.trigger_plex_scan(scan_dir, media_type, library_id)

                if scan_success:
                    logger.info(f"Remote Plex Scanner: ✅ 完成: {scan_dir} ({file_count}个文件)")
                    if self._notify and items:
                        mediainfo = items[0].get("mediainfo")
                        title = mediainfo.title if mediainfo and hasattr(mediainfo, 'title') else "未知媒体"
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="Remote Plex Scanner - 扫描完成",
                            text=f"✅ 已成功扫描\n\n媒体: {title}\n目录: {scan_dir}\n文件数: {file_count}"
                        )
                else:
                    logger.error(f"Remote Plex Scanner: ❌ 扫描失败: {scan_dir}")
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="Remote Plex Scanner - 扫描失败",
                            text=f"❌ 扫描失败\n\n路径: {scan_dir}"
                        )

                # 从队列中移除
                self._scan_queue.remove(scan_item)

            except Exception as e:
                logger.error(f"Remote Plex Scanner: 处理扫描任务时出错: {str(e)}")
                self._scan_queue.remove(scan_item)

    def translate_path(self, local_path: str) -> Optional[Tuple[str, Optional[str]]]:
        """
        Step 2: 路径转换 - 处理MP的特殊路径格式
        返回: (remote_path, library_id) 或 (remote_path, None)
        
        MP的115网盘路径格式：【u115】/我的/网盘剧/...
        支持三种映射方式：
        1. 路径-库映射（优先）：/我的/动漫:/media/动漫:5
        2. 简单模式：/media/ → 【u115】/xxx 映射到 /media/xxx
        3. 高级模式：/我的/|/media/ → 【u115】/我的/xxx 映射到 /media/xxx
        """
        # 检查是否是115网盘路径
        if local_path.startswith("【u115】"):
            logger.info("Remote Plex Scanner: 检测到115网盘路径格式")
            # 去掉【u115】前缀
            path_without_prefix = local_path.replace("【u115】", "", 1)
            
            # 优先使用路径-库映射
            if self._path_library_mapping:
                for mapping in self._path_library_mapping:
                    local_prefix = mapping["local"].replace("\\", "/")
                    remote_prefix = mapping["remote"].replace("\\", "/")
                    library_id = mapping["library_id"]
                    
                    # 标准化路径
                    path_normalized = path_without_prefix.replace("\\", "/")
                    
                    # 确保前缀以/结尾用于匹配
                    local_prefix_match = local_prefix if local_prefix.endswith("/") else local_prefix + "/"
                    
                    # 检查是否匹配（路径以local_prefix开头）
                    if path_normalized.startswith(local_prefix_match) or path_normalized == local_prefix:
                        # 替换前缀
                        remote_path = path_normalized.replace(local_prefix, remote_prefix, 1)
                        
                        logger.info(f"Remote Plex Scanner: 路径-库映射")
                        logger.info(f"  MP路径: {local_path}")
                        logger.info(f"  Plex路径: {remote_path}")
                        logger.info(f"  库ID: {library_id}")
                        return (remote_path, library_id)
            
            # Fallback到原有逻辑
            if not self._path_mapping_remote:
                logger.warning("Remote Plex Scanner: 未配置远程路径前缀，使用默认路径")
                return (path_without_prefix, None)
            
            # 检查是否配置了本地路径（高级模式）
            if self._path_mapping_local:
                # 高级模式：/我的/|/media/
                # path_without_prefix = /我的/网盘剧/xxx
                # 需要替换 /我的/ 为 /media/
                local_prefix = self._path_mapping_local.replace("\\", "/")
                remote_prefix = self._path_mapping_remote.replace("\\", "/")
                
                # 标准化路径
                path_without_prefix = path_without_prefix.replace("\\", "/")
                
                # 确保前缀以/结尾
                if not local_prefix.endswith("/"):
                    local_prefix += "/"
                if not remote_prefix.endswith("/"):
                    remote_prefix += "/"
                
                # 去除path_without_prefix开头的/
                if path_without_prefix.startswith("/"):
                    path_without_prefix = path_without_prefix[1:]
                
                # 检查是否匹配本地前缀
                if path_without_prefix.startswith(local_prefix.lstrip("/")):
                    # 替换前缀
                    remote_path = path_without_prefix.replace(local_prefix.lstrip("/"), remote_prefix.lstrip("/"), 1)
                    # 确保以/开头
                    if not remote_path.startswith("/"):
                        remote_path = "/" + remote_path
                    logger.info(f"Remote Plex Scanner: 115高级路径映射")
                    logger.info(f"  MP路径: {local_path}")
                    logger.info(f"  去前缀: {path_without_prefix}")
                    logger.info(f"  映射规则: 【u115】{local_prefix} -> {remote_prefix}")
                    logger.info(f"  rclone路径: {remote_path}")
                    return (remote_path, None)
                else:
                    logger.warning(f"Remote Plex Scanner: 路径不匹配映射规则")
                    logger.warning(f"  【u115】后的路径: {path_without_prefix}")
                    logger.warning(f"  期望前缀: {local_prefix}")
                    # 使用简单模式作为fallback
            
            # 简单模式：直接替换【u115】为远程路径
            remote_prefix = self._path_mapping_remote.replace("\\", "/")
            if not remote_prefix.endswith("/"):
                remote_prefix += "/"
            
            # 去除开头的/
            if path_without_prefix.startswith("/"):
                path_without_prefix = path_without_prefix[1:]
            
            remote_path = remote_prefix + path_without_prefix
            logger.info(f"Remote Plex Scanner: 115简单路径映射")
            logger.info(f"  MP路径: {local_path}")
            logger.info(f"  rclone路径: {remote_path}")
            return (remote_path, None)
        
        # 如果路径没有【u115】前缀，但有路径-库ID映射配置，也尝试匹配
        if self._path_library_mapping:
            local_path_normalized = local_path.replace("\\", "/")
            for mapping in self._path_library_mapping:
                local_prefix = mapping["local"].replace("\\", "/")
                remote_prefix = mapping["remote"].replace("\\", "/")
                library_id = mapping["library_id"]
                
                # 确保前缀以/结尾用于匹配
                local_prefix_match = local_prefix if local_prefix.endswith("/") else local_prefix + "/"
                
                # 检查是否匹配
                if local_path_normalized.startswith(local_prefix_match) or local_path_normalized == local_prefix:
                    # 替换前缀
                    remote_path = local_path_normalized.replace(local_prefix, remote_prefix, 1)
                    
                    logger.info(f"Remote Plex Scanner: 路径-库映射")
                    logger.info(f"  MP路径: {local_path}")
                    logger.info(f"  Plex路径: {remote_path}")
                    logger.info(f"  库ID: {library_id}")
                    return (remote_path, library_id)
        
        # 标准路径映射逻辑（兼容旧版配置）
        if not self._path_mapping_local or not self._path_mapping_remote:
            logger.warning("Remote Plex Scanner: 未配置路径映射，使用原始路径")
            return (local_path, None)

        # 标准化路径（处理Windows/Linux路径差异）
        local_path = local_path.replace("\\", "/")
        local_prefix = self._path_mapping_local.replace("\\", "/")
        remote_prefix = self._path_mapping_remote.replace("\\", "/")

        # 确保前缀以/结尾
        if not local_prefix.endswith("/"):
            local_prefix += "/"
        if not remote_prefix.endswith("/"):
            remote_prefix += "/"

        # 执行路径替换
        if local_path.startswith(local_prefix):
            remote_path = local_path.replace(local_prefix, remote_prefix, 1)
            return (remote_path, None)
        else:
            logger.warning(f"Remote Plex Scanner: 路径不匹配映射规则")
            logger.warning(f"  路径: {local_path}")
            logger.warning(f"  规则: {local_prefix} -> {remote_prefix}")
            return (local_path, None)

    def refresh_rclone_cache(self, path: str) -> bool:
        """
        Step 3: 刷新远程rclone VFS缓存
        让rclone重新读取网盘上的文件（因为文件是通过其他方式上传到网盘的）
        """
        try:
            import requests

            if not self._rclone_rc_url:
                return False

            url = f"{self._rclone_rc_url}/vfs/refresh"
            payload = {
                "dir": path,
                "recursive": "true"
            }

            response = requests.post(
                url,
                json=payload,
                timeout=self._timeout
            )

            if response.status_code == 200:
                logger.info(f"Remote Plex Scanner: ✅ rclone刷新成功")
                return True
            else:
                logger.error(f"Remote Plex Scanner: ❌ rclone失败 ({response.status_code})")
                try:
                    error_detail = response.json()
                    logger.error(f"  {error_detail.get('error', error_detail)}")
                except:
                    pass
                return False

        except requests.exceptions.Timeout:
            logger.error(f"Remote Plex Scanner: rclone超时")
            return False
        except Exception as e:
            logger.error(f"Remote Plex Scanner: rclone错误: {str(e)}")
            return False

    def trigger_plex_scan(self, path: str, media_type: Optional[str] = None, library_id: Optional[str] = None) -> bool:
        """
        Step 4: 触发远程Plex局部扫描
        """
        try:
            import requests

            if not self._plex_url or not self._plex_token:
                logger.error("Remote Plex Scanner: Plex服务器地址或Token未配置")
                return False

            # 如果指定了library_id，直接使用；否则根据media_type获取
            if library_id:
                library_ids = [library_id]
                logger.info(f"Remote Plex Scanner: 使用指定的库ID: {library_id}")
            else:
                library_ids = self.get_library_ids(media_type)
            if not library_ids:
                logger.error(f"Remote Plex Scanner: 无法确定要扫描的媒体库")
                return False
            
            success_count = 0
            # 对每个库发起扫描
            for library_id in library_ids:
                # 构建请求URL和参数
                url = f"{self._plex_url}/library/sections/{library_id}/refresh"
                params = {
                    'path': path,  # 直接传递未编码的路径
                    'X-Plex-Token': self._plex_token
                }

                logger.info(f"Remote Plex Scanner: 📡 扫描库{library_id}: {path}")

                try:
                    response = requests.get(
                        url,
                        params=params,
                        timeout=self._timeout
                    )

                    if response.status_code == 200:
                        logger.info(f"Remote Plex Scanner: ✅ Plex库{library_id}扫描成功")
                        success_count += 1
                    else:
                        logger.error(f"Remote Plex Scanner: ❌ Plex库{library_id}失败 ({response.status_code})")
                        logger.error(f"  响应: {response.text[:200]}")
                except Exception as e:
                    logger.error(f"Remote Plex Scanner: Plex库{library_id}错误: {str(e)}")
            
            return success_count > 0

        except requests.exceptions.Timeout:
            logger.error(f"Remote Plex Scanner: Plex请求超时 ({self._timeout}秒)")
            return False
        except Exception as e:
            logger.error(f"Remote Plex Scanner: 触发Plex扫描时出错: {str(e)}")
            return False

    def get_library_ids(self, media_type: Optional[str] = None) -> List[str]:
        """
        获取要扫描的媒体库ID列表
        使用媒体类型映射配置
        """
        library_ids = []
        
        # 使用媒体类型映射（兼容旧配置）
        if self._library_mapping:
            if media_type:
                library_id = self.get_library_id(media_type)
                if library_id:
                    return [library_id]
            else:
                # 没有指定类型，返回所有配置的库
                return list(self._library_mapping.values())
        
        return library_ids
    
    def _match_library_type(self, library_type: str, media_type: str) -> bool:
        """
        判断库类型是否匹配媒体类型
        """
        media_type_lower = media_type.lower()
        library_type_lower = library_type.lower()
        
        # 电影类型
        if media_type_lower in ["movie", "电影", "movies"]:
            return library_type_lower == "movie"
        
        # 电视剧/动漫都属于show类型，需要通过库名称区分
        if media_type_lower in ["tv", "电视剧", "series", "show"]:
            return library_type_lower == "show"
        
        if media_type_lower in ["anime", "动漫", "动画"]:
            return library_type_lower == "show"
        
        return True  # 其他情况默认匹配

    def get_library_id(self, media_type: Optional[str]) -> Optional[str]:
        """
        根据媒体类型获取库ID
        """
        if not media_type:
            # 如果没有类型信息，尝试使用第一个配置的库
            if self._library_mapping:
                return list(self._library_mapping.values())[0]
            return None

        # 标准化媒体类型
        media_type_lower = media_type.lower()
        
        # 映射关系: 电影 -> movie, 电视剧 -> tv
        type_mapping = {
            "movie": "movie",
            "电影": "movie",
            "movies": "movie",
            "tv": "tv",
            "电视剧": "tv",
            "series": "tv",
            "show": "tv"
        }

        mapped_type = type_mapping.get(media_type_lower, media_type_lower)
        
        # 从配置中查找对应的库ID
        library_id = self._library_mapping.get(mapped_type)
        
        if not library_id:
            logger.warning(f"Remote Plex Scanner: 未找到媒体类型 '{media_type}' 的库映射")
            # 返回第一个配置的库作为默认值
            if self._library_mapping:
                library_id = list(self._library_mapping.values())[0]
                logger.info(f"Remote Plex Scanner: 使用默认库ID: {library_id}")
        
        return library_id

    def test_connection(self) -> Dict[str, Any]:
        """
        测试Plex和rclone连接
        """
        logger.info("=" * 60)
        logger.info("Remote Plex Scanner: 开始测试连接...")
        logger.info("=" * 60)
        
        results = {
            "plex": False,
            "rclone": False,
            "path_mapping": False
        }

        # 测试Plex连接
        try:
            import requests
            
            if self._plex_url and self._plex_token:
                url = f"{self._plex_url}/library/sections"
                params = {"X-Plex-Token": self._plex_token}
                response = requests.get(url, params=params, timeout=10)
                
                if response.status_code == 200:
                    results["plex"] = True
                    logger.info("✅ Plex连接成功")
                    
                    # 显示可用的媒体库
                    data = response.json()
                    libraries = data.get("MediaContainer", {}).get("Directory", [])
                    logger.info(f"   可用媒体库:")
                    for lib in libraries:
                        logger.info(f"   - {lib.get('title')} (ID: {lib.get('key')})")
                else:
                    logger.error(f"❌ Plex连接失败: HTTP {response.status_code}")
            else:
                logger.error("❌ Plex未配置")
                
        except Exception as e:
            logger.error(f"❌ Plex连接测试异常: {str(e)}")

        # 测试rclone连接
        try:
            import requests
            
            if self._rclone_rc_url:
                url = f"{self._rclone_rc_url}/rc/noop"
                response = requests.post(url, json={}, timeout=10)
                
                if response.status_code == 200:
                    results["rclone"] = True
                    logger.info("✅ rclone RC连接成功")
                else:
                    logger.error(f"❌ rclone RC连接失败: HTTP {response.status_code}")
            else:
                logger.warning("⚠️  rclone RC未配置")
                
        except Exception as e:
            logger.error(f"❌ rclone连接测试异常: {str(e)}")

        # 测试路径映射
        if self._path_mapping_remote:
            results["path_mapping"] = True
            logger.info("✅ 路径映射已配置")
            
            if self._path_mapping_local:
                # 标准映射模式
                logger.info(f"   映射模式: 标准路径映射")
                logger.info(f"   本地路径: {self._path_mapping_local}")
                logger.info(f"   远程路径: {self._path_mapping_remote}")
                
                # 测试示例路径转换
                test_path = f"{self._path_mapping_local}Movies/Test (2024)/"
                converted = self.translate_path(test_path)
                logger.info(f"   示例转换:")
                logger.info(f"   输入: {test_path}")
                logger.info(f"   输出: {converted}")
            else:
                # 115网盘模式
                logger.info(f"   映射模式: 115网盘直连")
                logger.info(f"   【u115】→ {self._path_mapping_remote}")
                
                # 测试示例路径转换
                test_path = "【u115】/我的/网盘剧/测试剧集 (2024)/"
                converted = self.translate_path(test_path)
                logger.info(f"   示例转换:")
                logger.info(f"   输入: {test_path}")
                logger.info(f"   输出: {converted}")
        else:
            logger.warning("⚠️  路径映射未配置")

        # 显示库映射配置
        if self._library_mapping:
            logger.info("📚 媒体库映射配置:")
            for key, value in self._library_mapping.items():
                logger.info(f"   {key} -> 库ID: {value}")
        else:
            logger.warning("⚠️  媒体库映射未配置")

        logger.info("=" * 60)
        logger.info(f"测试结果: Plex={results['plex']}, rclone={results['rclone']}, 路径映射={results['path_mapping']}")
        logger.info("=" * 60)

        # 发送通知
        if self._notify:
            status_text = "✅ 全部正常" if all(results.values()) else "⚠️  部分配置异常"
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="Remote Plex Scanner - 连接测试",
                text=f"{status_text}\n\n"
                     f"Plex: {'✅' if results['plex'] else '❌'}\n"
                     f"rclone: {'✅' if results['rclone'] else '❌' if self._rclone_rc_url else '⚠️ 未配置'}\n"
                     f"路径映射: {'✅' if results['path_mapping'] else '❌'}"
            )

        return results

    def test_connection_api(self) -> Dict[str, Any]:
        """
        API：测试连接
        """
        return self.test_connection()

    def scan_path_api(self, path: str, media_type: Optional[str] = None) -> Dict[str, Any]:
        """
        API：扫描指定路径
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}

        try:
            # 路径转换
            remote_path = self.translate_path(path)
            
            # 刷新rclone缓存
            if self._rclone_rc_url:
                self.refresh_rclone_cache(remote_path)
            
            # 触发Plex扫描
            success = self.trigger_plex_scan(remote_path, media_type)
            
            return {
                "success": success,
                "message": "扫描成功" if success else "扫描失败",
                "local_path": path,
                "remote_path": remote_path
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"扫描出错: {str(e)}"
            }

    def stop_service(self):
        """
        停止插件服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务时出错：{str(e)}")

    def __get_plex_server_options(self):
        """
        获取Plex服务器选项（用于配置表单）
        """
        server_options = []
        if not self.mediaserver_helper:
            return server_options
        
        # 获取所有Plex媒体服务器
        services = self.mediaserver_helper.get_services(type_filter="plex")
        if not services:
            return server_options
        
        # 遍历每个Plex服务器
        for service_name, service_info in services.items():
            plex = service_info.instance
            if not plex:
                continue
            
            try:
                # 获取服务器信息
                server_options.append({
                    'title': service_name,
                    'value': service_name
                })
            except Exception as e:
                logger.warning(f"获取Plex服务器时出错: {str(e)}")
                continue
        
        return server_options

