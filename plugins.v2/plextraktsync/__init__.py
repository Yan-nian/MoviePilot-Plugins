from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime
from threading import Event as ThreadEvent

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType


class PlexTraktSync(_PluginBase):
    # 插件名称
    plugin_name = "Plex Trakt 同步"
    # 插件描述
    plugin_desc = "同步 Plex 观看记录到 Trakt（使用手动配置的 Plex 信息）"
    # 插件图标
    plugin_icon = "Trakt_A.png"
    # 插件版本
    plugin_version = "2.5.0"
    # 插件作者
    plugin_author = "Yan-nian"
    # 作者主页
    author_url = "https://github.com/Taxel/PlexTraktSync"
    # 插件配置项ID前缀
    plugin_config_prefix = "plextraktsync_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 2
    # 启用数据页面
    plugin_page = True

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _notify = False

    # Plex 配置需要用户手动填写
    _plex_url = None
    _plex_token = None
    _plex_libraries = None

    # Trakt 配置
    _trakt_client_id = None
    _trakt_client_secret = None
    _trakt_username = None
    _trakt_access_token = None  # OAuth Access Token
    _trakt_pin_code = None  # 用户输入的 PIN 码，用于换取 Token

    # 同步选项
    _sync_movies = True
    _sync_shows = True
    _sync_watched = True
    _sync_ratings = True
    _sync_collection = True
    _sync_watchlist = False

    # 高级选项
    _two_way_sync = False
    _sync_from_trakt = False
    _skip_already_synced = True
    _batch_size = 100

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _event = ThreadEvent()

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 2 * * *")
            self._notify = config.get("notify", False)

            # Plex 配置从用户输入获取
            self._plex_url = config.get("plex_url", "")
            self._plex_token = config.get("plex_token", "")
            self._plex_libraries = config.get("plex_libraries", "")

            # Trakt 配置
            self._trakt_client_id = config.get("trakt_client_id", "")
            self._trakt_client_secret = config.get("trakt_client_secret", "")
            self._trakt_username = config.get("trakt_username", "")
            self._trakt_access_token = config.get("trakt_access_token", "")
            self._trakt_pin_code = config.get("trakt_pin_code", "")
            
            # 如果有 PIN 码但没有 Token，尝试换取 Token
            if self._trakt_pin_code and not self._trakt_access_token:
                logger.info("检测到 PIN 码，尝试换取 Access Token...")
                token = self._exchange_pin_for_token(self._trakt_pin_code)
                if token:
                    self._trakt_access_token = token
                    # 保存 Token 到配置并清空 PIN 码
                    config['trakt_access_token'] = token
                    config['trakt_pin_code'] = ""  # 清空 PIN 码
                    self.update_config(config)
                    logger.info("✓ 成功换取并保存 Access Token")
                else:
                    logger.error("✗ PIN 码换取 Token 失败")

            # 同步选项
            self._sync_movies = config.get("sync_movies", True)
            self._sync_shows = config.get("sync_shows", True)
            self._sync_watched = config.get("sync_watched", True)
            self._sync_ratings = config.get("sync_ratings", True)
            self._sync_collection = config.get("sync_collection", True)
            self._sync_watchlist = config.get("sync_watchlist", False)

            # 高级选项
            self._two_way_sync = config.get("two_way_sync", False)
            self._sync_from_trakt = config.get("sync_from_trakt", False)
            self._skip_already_synced = config.get("skip_already_synced", True)
            self._batch_size = config.get("batch_size", 100)

            # 启动定时任务
            if self._enabled or self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                if self._onlyonce:
                    logger.info("Plex Trakt 同步服务，立即运行一次")
                    self._scheduler.add_job(
                        func=self.__sync_task,
                        trigger='date',
                        run_date=datetime.now(),
                        name="Plex Trakt 同步"
                    )
                    # 关闭一次性开关
                    self._onlyonce = False
                    self.update_config({
                        **config,
                        "onlyonce": False
                    })

                if self._enabled and self._cron:
                    try:
                        self._scheduler.add_job(
                            func=self.__sync_task,
                            trigger=CronTrigger.from_crontab(self._cron),
                            name="Plex Trakt 同步"
                        )
                        logger.info(f"Plex Trakt 同步定时任务已启动，执行周期：{self._cron}")
                    except Exception as e:
                        logger.error(f"定时任务配置错误：{str(e)}")

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        """
        获取插件状态
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [
            {
                "path": "/get_auth_url",
                "endpoint": self.get_auth_url,
                "methods": ["GET"],
                "summary": "获取 Trakt 授权 URL",
                "description": "生成 Trakt OAuth 授权链接"
            },
            {
                "path": "/exchange_pin",
                "endpoint": self.exchange_pin,
                "methods": ["POST"],
                "summary": "使用 PIN 码换取 Access Token",
                "description": "将用户授权的 PIN 码换取 Access Token"
            }
        ]
    
    def get_auth_url(self) -> dict:
        """生成 Trakt 授权 URL"""
        try:
            if not self._trakt_client_id:
                return {
                    "success": False,
                    "message": "请先配置 Trakt Client ID"
                }
            
            import urllib.parse
            auth_url = (
                f"https://trakt.tv/oauth/authorize"
                f"?response_type=code"
                f"&client_id={urllib.parse.quote(self._trakt_client_id)}"
                f"&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
            )
            
            return {
                "success": True,
                "auth_url": auth_url,
                "message": "请在浏览器中打开此链接并授权"
            }
        except Exception as e:
            logger.error(f"生成授权 URL 失败: {str(e)}")
            return {
                "success": False,
                "message": f"生成授权 URL 失败: {str(e)}"
            }
    
    def exchange_pin(self, pin_code: str) -> dict:
        """使用 PIN 码换取 Access Token"""
        try:
            if not self._trakt_client_id or not self._trakt_client_secret:
                return {
                    "success": False,
                    "message": "请先配置 Trakt Client ID 和 Client Secret"
                }
            
            if not pin_code:
                return {
                    "success": False,
                    "message": "PIN 码不能为空"
                }
            
            import json
            import urllib.request
            
            # 构造请求
            url = "https://api.trakt.tv/oauth/token"
            data = {
                "code": pin_code.strip(),
                "client_id": self._trakt_client_id,
                "client_secret": self._trakt_client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "authorization_code"
            }
            
            # 发送 POST 请求
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
            
            access_token = result.get('access_token')
            refresh_token = result.get('refresh_token')
            
            if not access_token:
                return {
                    "success": False,
                    "message": "未能获取 Access Token"
                }
            
            # 自动保存 Token 到配置
            config = self.get_config()
            config['trakt_access_token'] = access_token
            self.update_config(config)
            self._trakt_access_token = access_token
            
            logger.info("✓ 成功获取并保存 Trakt Access Token")
            
            return {
                "success": True,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "message": "✓ 成功获取 Access Token！已自动保存到配置中"
            }
            
        except urllib.error.HTTPError as e:
            error_msg = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
            logger.error(f"换取 Token 失败: {error_msg}")
            return {
                "success": False,
                "message": f"换取 Token 失败: {error_msg}"
            }
        except Exception as e:
            logger.error(f"换取 Token 失败: {str(e)}")
            return {
                "success": False,
                "message": f"换取 Token 失败: {str(e)}"
            }
    
    def _exchange_pin_for_token(self, pin_code: str) -> Optional[str]:
        """内部方法：使用 PIN 码换取 Access Token，返回 token 或 None"""
        try:
            import json
            import urllib.request
            import urllib.error
            
            if not self._trakt_client_id or not self._trakt_client_secret:
                logger.error("缺少 Client ID 或 Client Secret")
                return None
            
            # 构造请求
            url = "https://api.trakt.tv/oauth/token"
            data = {
                "code": pin_code.strip(),
                "client_id": self._trakt_client_id,
                "client_secret": self._trakt_client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "authorization_code"
            }
            
            # 发送 POST 请求
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
            
            access_token = result.get('access_token')
            return access_token if access_token else None
            
        except urllib.error.HTTPError as e:
            error_msg = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
            logger.error(f"HTTP 错误: {error_msg}")
            
            # 解析错误信息
            if 'invalid_grant' in error_msg:
                logger.error("")
                logger.error("PIN 码无效或已过期！")
                logger.error("")
                logger.error("常见原因:")
                logger.error("1. PIN 码已被使用过（每个 PIN 码只能使用一次）")
                logger.error("2. PIN 码已过期（通常 10 分钟内有效）")
                logger.error("3. Client ID/Secret 不正确")
                logger.error("")
                logger.error("解决方法:")
                logger.error("1. 访问新的授权 URL 获取新 PIN 码:")
                logger.error(f"   https://trakt.tv/oauth/authorize?response_type=code&client_id={self._trakt_client_id}&redirect_uri=urn:ietf:wg:oauth:2.0:oob")
                logger.error("2. 在授权页面点击「Authorize」")
                logger.error("3. 复制新的 PIN 码（注意不要有空格）")
                logger.error("4. 粘贴到插件配置并立即保存")
                logger.error("")
            
            return None
        except Exception as e:
            logger.error(f"换取 Token 失败: {str(e)}")
            return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "onlyonce",
                                    "label": "立即运行一次"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "notify",
                                    "label": "发送通知"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "cron",
                                    "label": "执行周期",
                                    "placeholder": "0 2 * * *",
                                    "hint": "使用 Cron 表达式，默认每天凌晨2点执行"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "Plex 配置 - 请手动填写 Plex 服务器信息"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "plex_url",
                                    "label": "Plex 服务器地址",
                                    "placeholder": "http://192.168.1.100:32400",
                                    "hint": "Plex 服务器的完整 URL 地址"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "plex_token",
                                    "label": "Plex Token",
                                    "placeholder": "粘贴你的 Plex Token",
                                    "hint": "在 Plex 设置中获取 Token"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "plex_libraries",
                                    "label": "媒体库名称（可选）",
                                    "placeholder": "Movies, TV Shows",
                                    "hint": "要同步的媒体库名称，多个用逗号分隔，留空则同步所有媒体库"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "Trakt 配置 - 从 https://trakt.tv/oauth/applications 创建应用获取"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "trakt_client_id",
                                    "label": "Trakt Client ID",
                                    "placeholder": "Client ID"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "trakt_client_secret",
                                    "label": "Trakt Client Secret",
                                    "placeholder": "Client Secret"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "trakt_username",
                                    "label": "Trakt 用户名",
                                    "placeholder": "username",
                                    "hint": "Trakt 账号用户名"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "🔐 Trakt 授权步骤"
                                },
                                "content": [
                                    {
                                        "component": "div",
                                        "text": "1. 在浏览器中访问以下链接进行授权："
                                    },
                                    {
                                        "component": "div",
                                        "props": {
                                            "style": "margin: 10px 0; padding: 10px; background: rgba(0,0,0,0.1); border-radius: 4px; word-break: break-all; font-family: monospace; font-size: 12px;"
                                        },
                                        "text": "https://trakt.tv/oauth/authorize?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=urn:ietf:wg:oauth:2.0:oob"
                                    },
                                    {
                                        "component": "div",
                                        "text": "（请将 YOUR_CLIENT_ID 替换为上面填写的 Client ID）"
                                    },
                                    {
                                        "component": "div",
                                        "props": {
                                            "style": "margin-top: 10px;"
                                        },
                                        "text": "2. 授权后，页面会显示一个 PIN 码"
                                    },
                                    {
                                        "component": "div",
                                        "text": "3. 将 PIN 码填入下方输入框并保存配置，插件会自动换取 Access Token"
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "trakt_pin_code",
                                    "label": "Trakt PIN 码",
                                    "placeholder": "粘贴授权后获得的 PIN 码",
                                    "hint": "填入 PIN 码并保存后，会自动换取 Token"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "trakt_access_token",
                                    "label": "Trakt Access Token（自动生成）",
                                    "placeholder": "由 PIN 码自动换取，或手动粘贴已有的 token",
                                    "hint": "授权成功后会自动填充"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "同步选项"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_movies",
                                    "label": "同步电影"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_shows",
                                    "label": "同步剧集"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_watched",
                                    "label": "同步观看状态"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_ratings",
                                    "label": "同步评分"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_collection",
                                    "label": "同步收藏"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_watchlist",
                                    "label": "同步想看列表"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": "高级选项 - 请谨慎使用"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "two_way_sync",
                                    "label": "双向同步",
                                    "hint": "同时同步 Plex 到 Trakt 和 Trakt 到 Plex（优先级最高）"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "sync_from_trakt",
                                    "label": "仅从 Trakt 同步到 Plex",
                                    "hint": "关闭双向同步时，仅将 Trakt 数据同步到 Plex"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "text",
                                    "density": "compact",
                                    "text": "默认：仅从 Plex 同步到 Trakt"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "skip_already_synced",
                                    "label": "跳过已同步项",
                                    "hint": "提高同步效率"
                                }
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "batch_size",
                                    "label": "批量处理大小",
                                    "type": "number",
                                    "placeholder": "100",
                                    "hint": "每批次处理的条目数量"
                                }
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success",
                                    "variant": "tonal",
                                    "text": "配置完成后，点击保存并启用插件。首次运行建议使用'立即运行一次'测试配置。"
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
            "cron": "0 2 * * *",
            "notify": False,
            "plex_url": "",
            "plex_token": "",
            "plex_libraries": "",
            "trakt_client_id": "",
            "trakt_client_secret": "",
            "trakt_username": "",
            "trakt_pin_code": "",
            "trakt_access_token": "",
            "sync_movies": True,
            "sync_shows": True,
            "sync_watched": True,
            "sync_ratings": True,
            "sync_collection": True,
            "sync_watchlist": False,
            "two_way_sync": False,
            "sync_from_trakt": False,
            "skip_already_synced": True,
            "batch_size": 100
        }

    def get_page(self) -> List[dict]:
        """
        插件数据页面，显示同步统计和状态
        """
        # 获取最后一次同步统计
        last_stats = self._last_sync_stats if hasattr(self, '_last_sync_stats') and self._last_sync_stats else {}
        last_sync_time = self._last_sync_time if hasattr(self, '_last_sync_time') and self._last_sync_time else "从未同步"
        
        # 获取配置状态（检查手动配置的 Plex 信息）
        try:
            # 检查 Plex URL 和 Token 是否配置
            if self._plex_url and self._plex_token:
                plex_configured = True
                # 尝试解析主机名
                from urllib.parse import urlparse
                parsed = urlparse(self._plex_url)
                plex_host = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname or self._plex_url
            else:
                plex_configured = False
                plex_host = "未配置"
        except Exception as e:
            logger.error(f"获取 Plex 配置失败: {str(e)}")
            plex_configured = False
            plex_host = "获取失败"
        trakt_configured = bool(self._trakt_client_id and self._trakt_client_secret and self._trakt_access_token)
        
        # 构建数据页面
        return [
            {
                "component": "VRow",
                "content": [
                    # 配置状态卡片
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                            "md": 6
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "variant": "tonal"
                                },
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "配置状态"
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VList",
                                                "props": {
                                                    "density": "compact"
                                                },
                                                "content": [
                                                    {
                                                        "component": "VListItem",
                                                        "props": {
                                                            "title": "Plex 连接",
                                                            "subtitle": f"已配置 ({plex_host})" if plex_configured else "未配置（请在系统设置中配置）"
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "template",
                                                                "props": {
                                                                    "v-slot:prepend": ""
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "VIcon",
                                                                        "props": {
                                                                            "icon": "mdi-check-circle" if plex_configured else "mdi-alert-circle",
                                                                            "color": "success" if plex_configured else "error"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VListItem",
                                                        "props": {
                                                            "title": "Trakt 认证",
                                                            "subtitle": "已认证" if trakt_configured else "未认证"
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "template",
                                                                "props": {
                                                                    "v-slot:prepend": ""
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "VIcon",
                                                                        "props": {
                                                                            "icon": "mdi-check-circle" if trakt_configured else "mdi-alert-circle",
                                                                            "color": "success" if trakt_configured else "error"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 最后同步时间卡片
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12,
                            "md": 6
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "variant": "tonal"
                                },
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "同步状态"
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VList",
                                                "props": {
                                                    "density": "compact"
                                                },
                                                "content": [
                                                    {
                                                        "component": "VListItem",
                                                        "props": {
                                                            "title": "最后同步",
                                                            "subtitle": last_sync_time
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "template",
                                                                "props": {
                                                                    "v-slot:prepend": ""
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "VIcon",
                                                                        "props": {
                                                                            "icon": "mdi-clock-outline"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VListItem",
                                                        "props": {
                                                            "title": "同步状态",
                                                            "subtitle": "已启用" if self._enabled else "已禁用"
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "template",
                                                                "props": {
                                                                    "v-slot:prepend": ""
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "VIcon",
                                                                        "props": {
                                                                            "icon": "mdi-sync" if self._enabled else "mdi-sync-off",
                                                                            "color": "success" if self._enabled else "grey"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            # 同步统计卡片
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {
                            "cols": 12
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "variant": "tonal"
                                },
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "同步统计"
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "VRow",
                                                "content": [
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6,
                                                            "md": 3
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h4"
                                                                        },
                                                                        "text": str(last_stats.get('movies_synced', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "电影已同步"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6,
                                                            "md": 3
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h4"
                                                                        },
                                                                        "text": str(last_stats.get('shows_synced', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "剧集已同步"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6,
                                                            "md": 3
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h4"
                                                                        },
                                                                        "text": str(last_stats.get('episodes_synced', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "单集已同步"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6,
                                                            "md": 3
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h4"
                                                                        },
                                                                        "text": str(last_stats.get('ratings_synced', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "评分已同步"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VDivider",
                                                "props": {
                                                    "class": "my-4"
                                                }
                                            },
                                            {
                                                "component": "VRow",
                                                "content": [
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h5"
                                                                        },
                                                                        "text": str(last_stats.get('watched_synced', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "观看记录已同步"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {
                                                            "cols": 6
                                                        },
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {
                                                                    "class": "text-center"
                                                                },
                                                                "content": [
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-h5 text-error" if last_stats.get('errors', 0) > 0 else "text-h5"
                                                                        },
                                                                        "text": str(last_stats.get('errors', 0))
                                                                    },
                                                                    {
                                                                        "component": "div",
                                                                        "props": {
                                                                            "class": "text-caption text-grey"
                                                                        },
                                                                        "text": "错误数量"
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def __sync_task(self):
        """
        执行同步任务
        """
        logger.info("=" * 60)
        logger.info("开始 Plex Trakt 同步任务")
        logger.info("=" * 60)

        # 预检查：验证 pytrakt 包是否正确安装
        logger.info("检查依赖包...")
        try:
            import subprocess
            import sys
            
            # 尝试导入并检查
            try:
                import trakt
                trakt_path = str(trakt.__file__ if hasattr(trakt, '__file__') else '')
                logger.info(f"trakt 模块位置: {trakt_path}")
                
                # 尝试导入关键函数
                from trakt.core import delete, get, post
                logger.info("✓ pytrakt 包验证成功")
                
                # 验证通过，继续执行同步任务
                self.__continue_sync_task()
                return
                
            except ImportError as import_err:
                error_str = str(import_err)
                logger.warning(f"导入失败: {error_str}")
                
                # 检测到错误的包，尝试自动修复
                if 'cannot import name' in error_str and 'delete' in error_str:
                    logger.warning("⚠️ 检测到错误的 trakt 包，尝试自动修复...")
                    
                    try:
                        # 1. 卸载所有 trakt 相关包
                        logger.info("步骤 1/3: 卸载错误的包...")
                        subprocess.run(
                            [sys.executable, "-m", "pip", "uninstall", "trakt", "trakt.py", "-y"],
                            capture_output=True,
                            timeout=30
                        )
                        
                        # 2. 清理缓存
                        logger.info("步骤 2/3: 清理 pip 缓存...")
                        subprocess.run(
                            [sys.executable, "-m", "pip", "cache", "purge"],
                            capture_output=True,
                            timeout=30
                        )
                        
                        # 3. 安装正确的包
                        logger.info("步骤 3/3: 安装正确的依赖包 (pytrakt==4.2.2)...")
                        result = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "--no-cache-dir", 
                             "PlexAPI==4.17.2", "pytrakt==4.2.2"],
                            capture_output=True,
                            text=True,
                            timeout=120
                        )
                        
                        if result.returncode == 0:
                            logger.info("=" * 60)
                            logger.info("✅ 依赖包自动修复成功！")
                            logger.info("⚠️  请重启 MoviePilot 以使更改生效")
                            logger.info("=" * 60)
                            
                            if self._notify:
                                self.post_message(
                                    mtype=NotificationType.SiteMessage,
                                    title="【Plex Trakt 同步】",
                                    text="✅ 依赖包已自动修复完成！\n\n"
                                         "⚠️ 请重启 MoviePilot\n"
                                         "然后重新运行同步任务"
                                )
                        else:
                            logger.error(f"❌ 自动修复失败: {result.stderr}")
                            raise Exception("pip install 失败")
                            
                    except subprocess.TimeoutExpired:
                        logger.error("❌ 自动修复超时")
                        self.__show_manual_fix_instructions()
                    except Exception as fix_err:
                        logger.error(f"❌ 自动修复失败: {str(fix_err)}")
                        self.__show_manual_fix_instructions()
                    
                    return
                else:
                    # 其他导入错误
                    raise import_err
                
        except ImportError as e:
            error_msg = f"依赖包导入失败: {str(e)}"
            logger.error(error_msg)
            self.__show_manual_fix_instructions()
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Plex Trakt 同步失败】",
                    text=f"{error_msg}\n\n请检查依赖包安装"
                )
            return
        except Exception as e:
            logger.error(f"依赖检查失败: {str(e)}")
            return
    
    def __show_manual_fix_instructions(self):
        """显示手动修复说明"""
        logger.error("")
        logger.error("=" * 60)
        logger.error("请手动修复依赖包:")
        logger.error("=" * 60)
        logger.error("1. 进入 MoviePilot 容器:")
        logger.error("   docker exec -it moviepilot bash")
        logger.error("")
        logger.error("2. 卸载错误的包:")
        logger.error("   pip uninstall trakt trakt.py -y")
        logger.error("")
        logger.error("3. 安装正确的包:")
        logger.error("   pip install PlexAPI==4.17.2 pytrakt==4.2.2")
        logger.error("")
        logger.error("4. 退出并重启:")
        logger.error("   exit")
        logger.error("   docker restart moviepilot")
        logger.error("=" * 60)

    def __continue_sync_task(self):
        """继续执行同步任务（验证通过后）"""
        # 验证配置
        if not self.__validate_config():
            return

        # 发送开始通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【Plex Trakt 同步】",
                text="同步任务开始执行..."
            )

        try:
            # 先配置 Trakt（必须在导入其他 trakt 模块之前）
            logger.info("正在配置 Trakt 客户端...")
            
            import os
            import json
            import tempfile
            import requests
            import sys
            
            # 🔍 第一步：直接测试 Token 是否有效
            logger.info("=" * 60)
            logger.info("🔍 开始 Token 验证测试")
            logger.info("=" * 60)
            
            test_headers = {
                'Content-Type': 'application/json',
                'trakt-api-version': '2',
                'trakt-api-key': self._trakt_client_id,
                'Authorization': f'Bearer {self._trakt_access_token}'
            }
            
            try:
                logger.info("测试 1: 调用 Trakt API /users/settings")
                logger.info(f"  - Client ID: {self._trakt_client_id[:20]}...")
                logger.info(f"  - Token: {self._trakt_access_token[:20]}...")
                
                response = requests.get(
                    'https://api.trakt.tv/users/settings',
                    headers=test_headers,
                    timeout=10
                )
                
                logger.info(f"  - HTTP 状态码: {response.status_code}")
                
                if response.status_code == 200:
                    user_data = response.json()
                    logger.info(f"✅ Token 有效! 用户: {user_data.get('user', {}).get('username', 'unknown')}")
                elif response.status_code == 401:
                    logger.error("❌ Token 无效 (401 Unauthorized)")
                    logger.error("   可能原因: Token 已过期或被撤销")
                elif response.status_code == 403:
                    logger.error("❌ 访问被拒绝 (403 Forbidden)")
                    logger.error("   可能原因:")
                    logger.error("   1. Client ID 不正确")
                    logger.error("   2. Trakt 应用未批准")
                    logger.error("   3. Token 与 Client ID 不匹配")
                    logger.error(f"   响应内容: {response.text}")
                else:
                    logger.error(f"❌ 未知错误: {response.status_code}")
                    logger.error(f"   响应: {response.text}")
                    
            except Exception as test_err:
                logger.error(f"❌ Token 测试失败: {str(test_err)}")
            
            logger.info("=" * 60)
            
            # 如果测试失败，不继续
            if response.status_code != 200:
                logger.error("Token 验证失败，请修复后重试")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【Plex Trakt 同步失败】",
                        text=f"Token 验证失败 (HTTP {response.status_code})\n请检查配置"
                    )
                return
            
            # 🔧 关键修复：确保 trakt 模块未被导入，或清除已导入的模块
            logger.info("准备配置 pytrakt...")
            
            # 清除可能已经导入的 trakt 模块
            trakt_modules = [key for key in sys.modules.keys() if key.startswith('trakt')]
            if trakt_modules:
                logger.info(f"发现已导入的 trakt 模块: {trakt_modules}")
                for mod in trakt_modules:
                    del sys.modules[mod]
                logger.info("已清除旧的 trakt 模块")
            
            # 现在导入 trakt.core 并立即设置变量
            import trakt.core
            
            # 设置认证信息
            trakt.core.CLIENT_ID = self._trakt_client_id
            trakt.core.CLIENT_SECRET = self._trakt_client_secret
            trakt.core.OAUTH_TOKEN = self._trakt_access_token
            
            logger.info("✓ Trakt 认证信息已配置")
            logger.info(f"  验证 - CLIENT_ID 已设置: {bool(trakt.core.CLIENT_ID)}")
            logger.info(f"  验证 - OAUTH_TOKEN 已设置: {bool(trakt.core.OAUTH_TOKEN)}")
            
            # 现在导入其他模块
            import trakt.users
            import trakt.movies
            import trakt.tv

            # 使用用户手动配置的 Plex 信息连接服务器
            logger.info("正在连接 Plex 服务器...")
            try:
                if not self._plex_url or not self._plex_token:
                    error_msg = "未配置 Plex 服务器 URL 或 Token，请在插件配置中填写"
                    logger.error(error_msg)
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【Plex Trakt 同步失败】",
                            text=error_msg
                        )
                    return
                
                # 使用 PlexAPI 直接连接
                from plexapi.server import PlexServer
                
                logger.info(f"连接到 Plex 服务器: {self._plex_url}")
                plex = PlexServer(self._plex_url, self._plex_token)
                
                logger.info(f"✓ Plex 连接成功: {plex.friendlyName}")
                logger.info(f"  版本: {plex.version}")
                logger.info(f"  平台: {plex.platform}")
                
            except Exception as plex_err:
                error_msg = f"连接 Plex 服务器失败: {str(plex_err)}"
                logger.error(error_msg)
                logger.error("请检查:")
                logger.error("1. Plex 服务器 URL 是否正确")
                logger.error("2. Plex Token 是否有效")
                logger.error("3. Plex 服务器是否正在运行")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【Plex Trakt 同步失败】",
                        text=error_msg
                    )
                return
            
            if self._trakt_access_token:
                logger.info(f"✓ 使用 Access Token (前缀: {self._trakt_access_token[:20]}...)")
            else:
                logger.warning("⚠ 未配置 Access Token，功能将受限")

            # 获取 Trakt 用户（仅用于从 Trakt 同步）
            trakt_user = None
            if self._sync_from_trakt:
                # 从 Trakt 同步到 Plex 时必需用户对象
                try:
                    logger.info("正在连接 Trakt 用户...")
                    trakt_user = trakt.users.User(self._trakt_username or 'me')
                    logger.info(f"✓ Trakt 用户连接成功: {trakt_user.username}")
                except Exception as e:
                    logger.error(f"✗ 无法连接 Trakt 用户: {str(e)}")
                    logger.error("")
                    logger.error("从 Trakt 同步到 Plex 需要有效的用户连接")
                    logger.error("请检查:")
                    logger.error("1. Access Token 是否有效（尝试重新用 PIN 码换取）")
                    logger.error("2. Trakt 应用是否已批准: https://trakt.tv/oauth/applications")
                    logger.error("3. Client ID 和 Secret 是否正确")
                    
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【Plex Trakt 同步失败】",
                            text="无法连接 Trakt 用户，请检查 Access Token 配置"
                        )
                    return
            else:
                # 从 Plex 同步到 Trakt 时不需要用户对象，只需要 Token
                logger.info("同步方向: Plex → Trakt")
                if self._trakt_access_token:
                    logger.info("✓ 已配置 Access Token，可以直接同步")
                else:
                    logger.error("✗ 缺少 Access Token，无法同步到 Trakt")
                    logger.error("请在配置页面使用 PIN 码换取 Token")
                    return

            # 获取要同步的媒体库
            libraries = self.__get_libraries(plex)
            if not libraries:
                logger.warning("没有找到要同步的媒体库")
                return

            # 统计信息
            stats = {
                'movies_synced': 0,
                'shows_synced': 0,
                'episodes_synced': 0,
                'watched_synced': 0,
                'ratings_synced': 0,
                'collections_synced': 0,
                'errors': 0
            }

            # 同步每个媒体库
            for library in libraries:
                logger.info(f"\n处理媒体库: {library.title} ({library.type})")

                if library.type == 'movie' and self._sync_movies:
                    # 双向同步逻辑
                    if self._two_way_sync:
                        logger.info("📊 双向同步模式")
                        # 先从 Trakt 同步到 Plex
                        if trakt_user:
                            logger.info("  第1步: Trakt → Plex")
                            old_sync_from_trakt = self._sync_from_trakt
                            self._sync_from_trakt = True
                            self.__sync_movies(library, trakt_user, stats)
                            self._sync_from_trakt = old_sync_from_trakt
                        
                        # 再从 Plex 同步到 Trakt
                        logger.info("  第2步: Plex → Trakt")
                        old_sync_from_trakt = self._sync_from_trakt
                        self._sync_from_trakt = False
                        self.__sync_movies(library, trakt_user, stats)
                        self._sync_from_trakt = old_sync_from_trakt
                    else:
                        # 单向同步
                        self.__sync_movies(library, trakt_user, stats)
                        
                elif library.type == 'show' and self._sync_shows:
                    # 双向同步逻辑
                    if self._two_way_sync:
                        logger.info("📊 双向同步模式")
                        # 先从 Trakt 同步到 Plex
                        if trakt_user:
                            logger.info("  第1步: Trakt → Plex")
                            old_sync_from_trakt = self._sync_from_trakt
                            self._sync_from_trakt = True
                            self.__sync_shows(library, trakt_user, stats)
                            self._sync_from_trakt = old_sync_from_trakt
                        
                        # 再从 Plex 同步到 Trakt
                        logger.info("  第2步: Plex → Trakt")
                        old_sync_from_trakt = self._sync_from_trakt
                        self._sync_from_trakt = False
                        self.__sync_shows(library, trakt_user, stats)
                        self._sync_from_trakt = old_sync_from_trakt
                    else:
                        # 单向同步
                        self.__sync_shows(library, trakt_user, stats)
                else:
                    logger.info(f"跳过媒体库 {library.title} (类型: {library.type})")

            # 输出统计信息
            logger.info("\n" + "=" * 60)
            logger.info("同步完成统计:")
            logger.info(f"  电影同步: {stats['movies_synced']} 部")
            logger.info(f"  剧集同步: {stats['shows_synced']} 部")
            logger.info(f"  单集同步: {stats['episodes_synced']} 集")
            logger.info(f"  观看状态: {stats['watched_synced']} 项")
            logger.info(f"  评分同步: {stats['ratings_synced']} 项")
            logger.info(f"  收藏同步: {stats['collections_synced']} 项")
            logger.info(f"  错误数量: {stats['errors']} 项")
            logger.info("=" * 60)

            # 保存统计数据到实例变量，供数据页面显示
            self._last_sync_stats = stats
            self._last_sync_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 发送完成通知
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Plex Trakt 同步】",
                    text=f"同步完成\n"
                         f"电影: {stats['movies_synced']} | "
                         f"剧集: {stats['shows_synced']} | "
                         f"错误: {stats['errors']}"
                )

        except ImportError as e:
            error_msg = f"导入依赖失败: {str(e)}"
            logger.error(error_msg)
            logger.error("请确保已正确安装依赖包:")
            logger.error("  pip install PlexAPI==4.17.2 pytrakt==4.2.2")
            logger.error("")
            logger.error("如果错误提示 'cannot import name delete'，说明安装了错误的 trakt 包")
            logger.error("请执行以下命令修复:")
            logger.error("  pip uninstall trakt trakt.py -y")
            logger.error("  pip install pytrakt==4.2.2")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Plex Trakt 同步失败】",
                    text=f"{error_msg}\n\n"
                         "如出现 'cannot import name delete' 错误，\n"
                         "请卸载错误的包后重新安装:\n"
                         "pip uninstall trakt trakt.py -y && pip install pytrakt==4.2.2"
                )
        except Exception as e:
            error_msg = f"同步任务执行失败: {str(e)}"
            logger.error(error_msg, exc_info=True)
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Plex Trakt 同步失败】",
                    text=error_msg
                )

    def __validate_config(self) -> bool:
        """
        验证配置
        """
        # 验证 Trakt 配置
        if not self._trakt_client_id or not self._trakt_client_secret:
            logger.error("✗ Trakt 配置不完整，请检查 Client ID 和 Client Secret")
            return False

        # 验证 Plex 配置（使用用户手动配置）
        try:
            if not self._plex_url or not self._plex_token:
                logger.error("✗ 未配置 Plex 服务器 URL 或 Token，请在插件配置中填写")
                return False
            
            # 尝试连接 Plex 服务器
            from plexapi.server import PlexServer
            plex = PlexServer(self._plex_url, self._plex_token)
            
            logger.info(f"✓ Plex 配置验证通过: {plex.friendlyName}")
        except Exception as e:
            logger.error(f"✗ Plex 配置验证失败: {str(e)}")
            return False

        logger.info("✓ 配置验证通过")
        return True

    def __get_libraries(self, plex):
        """
        获取要同步的媒体库
        """
        all_libraries = plex.library.sections()

        if not self._plex_libraries:
            # 返回所有电影和剧集库
            return [lib for lib in all_libraries if lib.type in ['movie', 'show']]

        # 解析配置的媒体库名称
        library_names = [name.strip() for name in self._plex_libraries.split(',')]
        libraries = []

        for name in library_names:
            try:
                lib = plex.library.section(name)
                if lib.type in ['movie', 'show']:
                    libraries.append(lib)
                    logger.info(f"✓ 找到媒体库: {lib.title}")
                else:
                    logger.warning(f"⊘ 跳过非媒体库: {lib.title} (类型: {lib.type})")
            except Exception as e:
                logger.warning(f"✗ 找不到媒体库: {name} - {str(e)}")

        return libraries

    def __sync_movies(self, library, trakt_user, stats):
        """
        同步电影
        """
        try:
            import trakt.sync
            from trakt.core import post
            
            movies = library.all()
            total = len(movies)
            logger.info(f"共找到 {total} 部电影")

            # 如果是从 Trakt 同步到 Plex
            if self._sync_from_trakt and trakt_user:
                # 获取 Trakt 观看记录
                watched_movies = {}
                rated_movies = {}
                
                try:
                    # 直接使用 API 获取观看记录
                    logger.info("正在从 Trakt 获取电影观看记录...")
                    
                    from trakt.core import get as trakt_get
                    
                    @trakt_get
                    def get_watched_movies_data():
                        """直接调用 API 获取观看数据"""
                        data = yield 'sync/watched/movies'
                        yield data
                    
                    watched_data = get_watched_movies_data()
                    
                    logger.info(f"  API 调用成功，返回数据类型: {type(watched_data)}")
                    logger.info(f"  数据长度: {len(watched_data) if watched_data else 0}")
                    
                    parsed_count = 0  # 初始化计数器
                    if watched_data:
                        for item in watched_data:
                            if not isinstance(item, dict):
                                logger.debug(f"跳过非字典项: {type(item)}")
                                continue
                            
                            movie_data = item.get('movie', {})
                            movie_ids = movie_data.get('ids', {})
                            
                            # 使用多个 ID 作为键
                            if movie_ids.get('imdb'):
                                watched_movies[f"imdb://{movie_ids['imdb']}"] = movie_data
                                parsed_count += 1
                            if movie_ids.get('tmdb'):
                                watched_movies[f"tmdb://{movie_ids['tmdb']}"] = movie_data
                                if 'imdb' not in movie_ids:
                                    parsed_count += 1
                        
                        logger.info(f"  解析完成: {parsed_count} 部电影")
                    else:
                        logger.warning("  watched_data 为空或 None")
                                
                    logger.info(f"✓ 从 Trakt 获取了 {len(watched_data) if watched_data else 0} 部已观看电影")
                    
                    # 如果启用评分同步，获取评分
                    if self._sync_ratings:
                        logger.info("正在从 Trakt 获取电影评分...")
                        
                        try:
                            # 使用 User.get_ratings() 方法获取评分
                            ratings_data = trakt_user.get_ratings('movies')
                            logger.info(f"  评分数据长度: {len(ratings_data) if ratings_data else 0}")
                        except Exception as rating_err:
                            logger.error(f"  获取评分失败: {str(rating_err)}")
                            ratings_data = None
                        
                        if ratings_data:
                            for item in ratings_data:
                                if not isinstance(item, dict):
                                    continue
                                
                                rating = item.get('rating', 0)
                                movie_data = item.get('movie', {})
                                movie_ids = movie_data.get('ids', {})
                                
                                # Trakt 评分是 1-10，Plex 也是 0-10
                                plex_rating = float(rating)
                                
                                if movie_ids.get('imdb'):
                                    rated_movies[f"imdb://{movie_ids['imdb']}"] = plex_rating
                                if movie_ids.get('tmdb'):
                                    rated_movies[f"tmdb://{movie_ids['tmdb']}"] = plex_rating
                                    
                        logger.info(f"✓ 从 Trakt 获取了 {len(ratings_data) if ratings_data else 0} 个电影评分")
                        
                except Exception as e:
                    logger.error(f"获取 Trakt 数据失败: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
                    return

                logger.info(f"开始处理 {total} 部 Plex 电影...")
                
                # 遍历 Plex 电影，应用 Trakt 数据
                for idx, movie in enumerate(movies, 1):
                    try:
                        if idx % 10 == 0:
                            logger.info(f"  处理进度: {idx}/{total}")

                        # 检查 Plex 电影的 GUID
                        matched_guid = None
                        for guid in movie.guids:
                            if guid.id in watched_movies or guid.id in rated_movies:
                                matched_guid = guid.id
                                break
                        
                        if matched_guid:
                            # 同步观看状态
                            if self._sync_watched and matched_guid in watched_movies and not movie.isWatched:
                                try:
                                    logger.info(f"  标记为已观看: {movie.title} ({movie.year})")
                                    movie.markWatched()
                                    stats['watched_synced'] += 1
                                    # 添加短暂延迟，避免 Plex 服务器负载过高
                                    import time
                                    time.sleep(0.1)
                                except Exception as mark_err:
                                    logger.warning(f"  标记失败 {movie.title}: {str(mark_err)}")
                            
                            # 同步评分
                            if self._sync_ratings and matched_guid in rated_movies:
                                trakt_rating = rated_movies[matched_guid]
                                current_rating = movie.userRating if hasattr(movie, 'userRating') else None
                                
                                # 只在评分不同时更新
                                if current_rating != trakt_rating:
                                    logger.info(f"  更新评分: {movie.title} - {trakt_rating}/10")
                                    movie.rate(trakt_rating)
                                    stats['ratings_synced'] += 1
                            
                            stats['movies_synced'] += 1

                    except Exception as e:
                        logger.error(f"处理电影失败 {movie.title}: {str(e)}")
                        stats['errors'] += 1
            
            # 如果是从 Plex 同步到 Trakt（批量同步）
            else:
                if not self._sync_watched:
                    return
                
                # 收集要同步的电影
                movies_to_sync = []
                for idx, movie in enumerate(movies, 1):
                    try:
                        if idx % 10 == 0:
                            logger.info(f"  处理进度: {idx}/{total}")

                        if movie.isWatched:
                            # 获取 IMDB/TMDB ID
                            movie_ids = self.__extract_ids(movie)
                            
                            if movie_ids.get('imdb'):
                                movies_to_sync.append({
                                    'ids': {'imdb': movie_ids['imdb']},
                                    'title': movie.title,
                                    'year': movie.year
                                })
                            elif movie_ids.get('tmdb'):
                                movies_to_sync.append({
                                    'ids': {'tmdb': int(movie_ids['tmdb'])},
                                    'title': movie.title,
                                    'year': movie.year
                                })

                    except Exception as e:
                        logger.error(f"处理电影失败 {movie.title}: {str(e)}")
                        stats['errors'] += 1

                # 批量同步到 Trakt
                if movies_to_sync:
                    try:
                        logger.info(f"正在批量同步 {len(movies_to_sync)} 部电影到 Trakt...")
                        
                        # 使用 Trakt Sync API 批量添加历史记录
                        @post
                        def sync_movies_to_trakt(movies_data):
                            """批量同步电影到 Trakt"""
                            data = yield 'sync/history'
                            yield data
                        
                        response = sync_movies_to_trakt({'movies': movies_to_sync})
                        
                        if response and 'added' in response:
                            added = response['added'].get('movies', 0)
                            logger.info(f"✓ 成功同步 {added} 部电影到 Trakt")
                            stats['movies_synced'] = added
                            stats['watched_synced'] += added
                        else:
                            logger.warning(f"同步响应异常: {response}")
                            
                    except Exception as e:
                        logger.error(f"批量同步到 Trakt 失败: {str(e)}")
                        if 'Forbidden' in str(e):
                            logger.error("提示: 请确保 Access Token 有效且应用已在 Trakt 授权")
                        stats['errors'] += 1

        except Exception as e:
            logger.error(f"同步电影库失败: {str(e)}")
            stats['errors'] += 1

    def __sync_shows(self, library, trakt_user, stats):
        """
        同步剧集
        """
        try:
            from trakt.core import post, get as trakt_get
            
            shows = library.all()
            total = len(shows)
            logger.info(f"共找到 {total} 部剧集")

            # 如果是从 Trakt 同步到 Plex
            if self._sync_from_trakt and trakt_user:
                # 获取 Trakt 观看记录和评分
                watched_shows = {}
                rated_shows = {}
                rated_episodes = {}
                
                try:
                    logger.info("正在从 Trakt 获取剧集观看记录...")
                    
                    # 注意：get_watched 返回的是 TVShow 对象列表，不是原始字典
                    # 我们需要直接使用 API 来获取完整的观看数据
                    from trakt.core import get as trakt_get
                    
                    @trakt_get
                    def get_watched_shows_data():
                        """直接调用 API 获取完整的观看数据（包含季和集信息）"""
                        data = yield 'sync/watched/shows'
                        yield data
                    
                    watched_data = get_watched_shows_data()
                    
                    logger.info(f"调试 - watched_data 类型: {type(watched_data)}")
                    logger.info(f"调试 - watched_data 长度: {len(watched_data) if watched_data else 0}")
                    
                    if watched_data:
                        # 显示第一项的结构用于调试
                        if len(watched_data) > 0:
                            first_item = watched_data[0]
                            logger.info(f"调试 - 第一项类型: {type(first_item)}")
                            logger.info(f"调试 - 第一项键: {first_item.keys() if isinstance(first_item, dict) else 'Not a dict'}")
                        
                        parsed_count = 0
                        for item in watched_data:
                            # item 应该是字典，包含 'show' 和 'seasons' 信息
                            if not isinstance(item, dict):
                                logger.debug(f"跳过非字典项: {type(item)}")
                                continue
                                
                            show_data = item.get('show', {})
                            seasons_data = item.get('seasons', [])
                            
                            if not show_data or not seasons_data:
                                logger.debug(f"跳过不完整的项: show={bool(show_data)}, seasons={bool(seasons_data)}")
                                continue
                            
                            # 获取 show IDs
                            show_ids = show_data.get('ids', {})
                            show_key = None
                            
                            if show_ids.get('tvdb'):
                                show_key = f"tvdb://{show_ids['tvdb']}"
                            elif show_ids.get('tmdb'):
                                show_key = f"tmdb://{show_ids['tmdb']}"
                            elif show_ids.get('imdb'):
                                show_key = f"imdb://{show_ids['imdb']}"
                            
                            if show_key:
                                # 收集所有已观看的集
                                watched_episodes = set()
                                for season_data in seasons_data:
                                    season_num = season_data.get('number', 0)
                                    episodes = season_data.get('episodes', [])
                                    for ep_data in episodes:
                                        ep_num = ep_data.get('number', 0)
                                        watched_episodes.add(f"S{season_num:02d}E{ep_num:02d}")
                                
                                watched_shows[show_key] = {
                                    'show': show_data,
                                    'episodes': watched_episodes
                                }
                                parsed_count += 1
                                
                        logger.info(f"✓ 从 Trakt 获取了 {len(watched_shows)} 部已观看剧集")
                        
                        # 显示几个示例用于调试
                        if watched_shows:
                            sample_count = 0
                            for show_key, show_info in watched_shows.items():
                                if sample_count < 3:
                                    ep_count = len(show_info['episodes'])
                                    logger.info(f"  示例: {show_key} - {ep_count} 集已观看")
                                    sample_count += 1
                                else:
                                    break
                    
                    # 如果启用评分同步，获取评分
                    if self._sync_ratings:
                        logger.info("正在从 Trakt 获取剧集评分...")
                        
                        # 获取剧集评分
                        show_ratings_data = trakt_user.get_ratings('shows')
                        if show_ratings_data:
                            for item in show_ratings_data:
                                if not isinstance(item, dict):
                                    continue
                                    
                                rating = item.get('rating', 0)
                                show_data = item.get('show', {})
                                show_ids = show_data.get('ids', {})
                                
                                plex_rating = float(rating)
                                
                                if show_ids.get('tvdb'):
                                    rated_shows[f"tvdb://{show_ids['tvdb']}"] = plex_rating
                                elif show_ids.get('tmdb'):
                                    rated_shows[f"tmdb://{show_ids['tmdb']}"] = plex_rating
                        
                        # 获取单集评分
                        episode_ratings_data = trakt_user.get_ratings('episodes')
                        if episode_ratings_data:
                            for item in episode_ratings_data:
                                if not isinstance(item, dict):
                                    continue
                                    
                                rating = item.get('rating', 0)
                                episode_data = item.get('episode', {})
                                show_data = item.get('show', {})
                                show_ids = show_data.get('ids', {})
                                
                                season_num = episode_data.get('season', 0)
                                ep_num = episode_data.get('number', 0)
                                ep_key = f"S{season_num:02d}E{ep_num:02d}"
                                
                                plex_rating = float(rating)
                                
                                if show_ids.get('tvdb'):
                                    show_key = f"tvdb://{show_ids['tvdb']}"
                                    if show_key not in rated_episodes:
                                        rated_episodes[show_key] = {}
                                    rated_episodes[show_key][ep_key] = plex_rating
                                elif show_ids.get('tmdb'):
                                    show_key = f"tmdb://{show_ids['tmdb']}"
                                    if show_key not in rated_episodes:
                                        rated_episodes[show_key] = {}
                                    rated_episodes[show_key][ep_key] = plex_rating
                        
                        logger.info(f"✓ 从 Trakt 获取了 {len(show_ratings_data) if show_ratings_data else 0} 个剧集评分和 {sum(len(eps) for eps in rated_episodes.values())} 个单集评分")
                    
                    # 调试：显示前几个
                    if watched_shows:
                        sample_keys = list(watched_shows.keys())[:2]
                        for key in sample_keys:
                            ep_count = len(watched_shows[key]['episodes'])
                            logger.info(f"  示例: {key} - {ep_count} 集已观看")
                    
                except Exception as e:
                    logger.error(f"获取 Trakt 观看记录失败: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
                    return

                # 遍历 Plex 剧集，应用 Trakt 数据
                for idx, show in enumerate(shows, 1):
                    try:
                        if idx % 10 == 0:
                            logger.info(f"  处理进度: {idx}/{total}")

                        # 检查 Plex 剧集的 GUID
                        matched_show_key = None
                        for guid in show.guids:
                            if guid.id in watched_shows or guid.id in rated_shows or guid.id in rated_episodes:
                                matched_show_key = guid.id
                                break
                        
                        if matched_show_key:
                            # 同步剧集整体评分
                            if self._sync_ratings and matched_show_key in rated_shows:
                                try:
                                    trakt_rating = rated_shows[matched_show_key]
                                    current_rating = show.userRating if hasattr(show, 'userRating') else None
                                    
                                    if current_rating != trakt_rating:
                                        logger.info(f"  更新剧集评分: {show.title} - {trakt_rating}/10")
                                        show.rate(trakt_rating)
                                        stats['ratings_synced'] += 1
                                except Exception as e:
                                    logger.debug(f"  剧集评分同步失败: {str(e)}")
                            
                            # 同步观看状态和单集评分
                            watched_episodes = watched_shows.get(matched_show_key, {}).get('episodes', set())
                            episode_ratings = rated_episodes.get(matched_show_key, {})
                            
                            # 遍历所有季和集
                            for season in show.seasons():
                                for episode in season.episodes():
                                    ep_key = f"S{episode.seasonNumber:02d}E{episode.index:02d}"
                                    
                                    # 同步观看状态
                                    if self._sync_watched and ep_key in watched_episodes and not episode.isWatched:
                                        try:
                                            import time
                                            logger.info(f"  标记为已观看: {show.title} {ep_key}")
                                            episode.markWatched()
                                            stats['watched_synced'] += 1
                                            stats['episodes_synced'] += 1
                                            # 添加短暂延迟，避免 Plex 服务器负载过高
                                            time.sleep(0.1)
                                        except Exception as mark_err:
                                            logger.warning(f"  标记失败 {show.title} {ep_key}: {str(mark_err)}")
                                            # 不计入错误统计，继续处理其他集
                                    
                                    # 同步单集评分
                                    if self._sync_ratings and ep_key in episode_ratings:
                                        try:
                                            trakt_rating = episode_ratings[ep_key]
                                            current_rating = episode.userRating if hasattr(episode, 'userRating') else None
                                            
                                            if current_rating != trakt_rating:
                                                logger.info(f"  更新单集评分: {show.title} {ep_key} - {trakt_rating}/10")
                                                episode.rate(trakt_rating)
                                                stats['ratings_synced'] += 1
                                        except Exception as e:
                                            logger.debug(f"  单集评分同步失败: {str(e)}")
                            
                            stats['shows_synced'] += 1

                    except Exception as e:
                        logger.error(f"处理剧集失败 {show.title}: {str(e)}")
                        stats['errors'] += 1
            
            # 如果是从 Plex 同步到 Trakt（批量同步）
            else:
                if not self._sync_watched:
                    return
                
                # 收集要同步的剧集
                episodes_to_sync = []
                for idx, show in enumerate(shows, 1):
                    try:
                        if idx % 10 == 0:
                            logger.info(f"  处理进度: {idx}/{total}")

                        # 获取 TVDB/TMDB ID
                        show_ids = self.__extract_ids(show)
                        
                        if show_ids.get('tvdb') or show_ids.get('tmdb'):
                            has_watched = False
                            
                            # 遍历所有季和集
                            for season in show.seasons():
                                for episode in season.episodes():
                                    if episode.isWatched:
                                        ep_data = {
                                            'season': episode.seasonNumber,
                                            'number': episode.index
                                        }
                                        
                                        # 添加剧集 ID
                                        if show_ids.get('tvdb'):
                                            ep_data['ids'] = {'tvdb': int(show_ids['tvdb'])}
                                        elif show_ids.get('tmdb'):
                                            ep_data['ids'] = {'tmdb': int(show_ids['tmdb'])}
                                        
                                        episodes_to_sync.append(ep_data)
                                        has_watched = True
                            
                            if has_watched:
                                stats['shows_synced'] += 1

                    except Exception as e:
                        logger.error(f"处理剧集失败 {show.title}: {str(e)}")
                        stats['errors'] += 1

                # 批量同步到 Trakt
                if episodes_to_sync:
                    try:
                        logger.info(f"正在批量同步 {len(episodes_to_sync)} 集到 Trakt...")
                        
                        # 使用 Trakt Sync API 批量添加历史记录
                        @post
                        def sync_episodes_to_trakt(episodes_data):
                            """批量同步剧集到 Trakt"""
                            data = yield 'sync/history'
                            yield data
                        
                        response = sync_episodes_to_trakt({'episodes': episodes_to_sync})
                        
                        if response and 'added' in response:
                            added = response['added'].get('episodes', 0)
                            logger.info(f"✓ 成功同步 {added} 集到 Trakt")
                            stats['episodes_synced'] = added
                            stats['watched_synced'] += added
                        else:
                            logger.warning(f"同步响应异常: {response}")
                            
                    except Exception as e:
                        logger.error(f"批量同步到 Trakt 失败: {str(e)}")
                        if 'Forbidden' in str(e):
                            logger.error("提示: 请确保 Access Token 有效且应用已在 Trakt 授权")
                        stats['errors'] += 1

        except Exception as e:
            logger.error(f"同步剧集库失败: {str(e)}")
            stats['errors'] += 1

    def __extract_ids(self, item) -> dict:
        """
        从 Plex 媒体项提取外部 ID
        """
        ids = {}
        
        try:
            # 遍历所有 GUID
            for guid in item.guids:
                guid_id = guid.id.lower()
                
                # 提取 IMDB ID
                if 'imdb://' in guid_id:
                    ids['imdb'] = guid_id.replace('imdb://', '')
                # 提取 TMDB ID
                elif 'tmdb://' in guid_id:
                    ids['tmdb'] = guid_id.replace('tmdb://', '')
                # 提取 TVDB ID
                elif 'tvdb://' in guid_id:
                    ids['tvdb'] = guid_id.replace('tvdb://', '')
        except Exception as e:
            logger.debug(f"提取 ID 失败: {str(e)}")
        
        return ids

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown(wait=False)
                    self._event.clear()
                self._scheduler = None
                logger.info("Plex Trakt 同步服务已停止")
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")
