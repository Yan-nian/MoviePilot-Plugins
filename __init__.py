import ipaddress
import json
import re
import threading
import paramiko
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.utils.common import retry
from app.utils.system import SystemUtils

lock = threading.Lock()


class ImmortalWrtHosts(_PluginBase):
    # 插件名称
    plugin_name = "ImmortalWrt路由Hosts"
    # 插件描述
    plugin_desc = "定时将本地Hosts同步至ImmortalWrt路由器的/etc/hosts文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/InfinityPacer/MoviePilot-Plugins/main/icons/mihosts.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "InfinityPacer"
    # 作者主页
    author_url = "https://github.com/InfinityPacer"
    # 插件配置项ID前缀
    plugin_config_prefix = "immortalwrt_hosts_"
    # 加载顺序
    plugin_order = 63
    # 可使用的用户级别
    auth_level = 1

    # region 私有属性

    # 是否开启
    _enabled = False
    # 立即运行一次
    _onlyonce = False
    # 任务执行间隔
    _cron = None
    # 发送通知
    _notify = False
    # 路由器IP地址
    _router_ip = None
    # SSH端口
    _ssh_port = 22
    # 用户名
    _username = "root"
    # 密码
    _password = None
    # 私钥文件路径
    _private_key_path = None
    # 忽略的IP或域名
    _ignore = None
    # 定时器
    _scheduler = None
    # 退出事件
    _event = threading.Event()

    # endregion

    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = config.get("enabled")
        self._onlyonce = config.get("onlyonce")
        self._cron = config.get("cron")
        self._notify = config.get("notify")
        self._router_ip = config.get("router_ip")
        self._ssh_port = config.get("ssh_port", 22)
        self._username = config.get("username", "root")
        self._password = config.get("password")
        self._private_key_path = config.get("private_key_path")
        self._ignore = config.get("ignore")

        # 停止现有任务
        self.stop_service()

        # 启动服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._onlyonce:
            logger.info(f"{self.plugin_name}服务，立即运行一次")
            self._scheduler.add_job(
                func=self.fetch_and_update_hosts,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name=f"{self.plugin_name}",
            )
            # 关闭一次性开关
            self._onlyonce = False
            config["onlyonce"] = False
            self.update_config(config=config)

        # 启动服务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            logger.info(f"{self.plugin_name}定时服务启动，时间间隔 {self._cron} ")
            return [{
                "id": self.__class__.__name__,
                "name": f"{self.plugin_name}服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.fetch_and_update_hosts,
                "kwargs": {}
            }]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.info(str(e))

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
                                            'hint': '开启后插件将处于激活状态',
                                            'persistent-hint': True
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
                                            'hint': '是否在特定事件发生时发送通知',
                                            'persistent-hint': True
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
                                            'label': '立即运行一次',
                                            'hint': '插件将立即运行一次',
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
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式',
                                            'hint': '使用cron表达式指定执行周期，如 0 8 * * *',
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
                                            'model': 'router_ip',
                                            'label': '路由器IP地址',
                                            'placeholder': '192.168.1.1',
                                            'hint': '请输入ImmortalWrt路由器的IP地址',
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
                                            'model': 'ssh_port',
                                            'label': 'SSH端口',
                                            'placeholder': '22',
                                            'hint': '请输入SSH端口号，默认为22',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'username',
                                            'label': '用户名',
                                            'placeholder': 'root',
                                            'hint': '请输入SSH登录用户名，通常为root',
                                            'persistent-hint': True
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'password',
                                            'label': '密码',
                                            'hint': '请输入SSH登录密码',
                                            'persistent-hint': True,
                                            'type': 'password'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'private_key_path',
                                            'label': '私钥文件路径',
                                            'hint': 'SSH私钥文件路径（可选，优先使用私钥）',
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
                                            'model': 'ignore',
                                            'label': '忽略的IP或域名',
                                            'hint': '如：10.10.10.1|wiki.movie-pilot.org',
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：本插件通过SSH连接ImmortalWrt路由器，需要开启SSH服务并配置正确的登录凭据'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '注意：修改路由器hosts文件可能影响网络访问，请确保配置正确。建议先备份原始hosts文件'
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
            "notify": True,
            "cron": "0 6 * * *",
            "router_ip": "192.168.1.1",
            "ssh_port": 22,
            "username": "root"
        }

    def get_page(self) -> List[dict]:
        pass

    def fetch_and_update_hosts(self):
        """
        获取本地hosts并更新到路由器
        """
        local_hosts = self.__get_local_hosts()
        if not local_hosts:
            self.__send_message(title="【ImmortalWrt路由Hosts更新】", text="获取本地hosts失败，更新失败，请检查日志")
            return

        # 获取路由器当前hosts
        remote_hosts = self.__fetch_remote_hosts()
        
        # 合并hosts
        updated_hosts = self.__merge_hosts_with_local(local_hosts, remote_hosts)
        if not updated_hosts:
            logger.info("没有需要更新的hosts，跳过")
            return

        # 更新路由器hosts
        self.__update_router_hosts(updated_hosts)

    def __fetch_remote_hosts(self) -> list:
        """
        通过SSH获取路由器当前的hosts文件内容
        """
        logger.info("正在获取路由器hosts文件")
        try:
            ssh_client = self.__create_ssh_connection()
            if not ssh_client:
                return []
            
            stdin, stdout, stderr = ssh_client.exec_command("cat /etc/hosts")
            remote_hosts = stdout.read().decode('utf-8').splitlines()
            ssh_client.close()
            
            logger.info(f"获取路由器hosts成功: {len(remote_hosts)}行")
            return remote_hosts
        except Exception as e:
            logger.error(f"获取路由器hosts失败: {e}")
            return []

    def __update_router_hosts(self, hosts_content: list):
        """
        通过SSH更新路由器的hosts文件
        """
        message_title = "【ImmortalWrt路由Hosts更新】"
        try:
            ssh_client = self.__create_ssh_connection()
            if not ssh_client:
                message_text = "SSH连接失败，无法更新路由器hosts"
                logger.error(message_text)
                self.__send_message(title=message_title, text=message_text)
                return

            # 先备份原始hosts文件
            ssh_client.exec_command("cp /etc/hosts /etc/hosts.backup")
            
            # 创建新的hosts内容
            hosts_string = '\n'.join(hosts_content)
            
            # 写入新的hosts文件
            command = f'echo "{hosts_string}" > /etc/hosts'
            stdin, stdout, stderr = ssh_client.exec_command(command)
            
            # 检查是否有错误
            error_output = stderr.read().decode('utf-8')
            if error_output:
                logger.error(f"更新hosts文件出错: {error_output}")
                message_text = f"更新路由器hosts失败: {error_output}"
            else:
                logger.info("路由器hosts文件更新成功")
                message_text = "路由器hosts文件更新成功"
            
            ssh_client.close()
            
        except Exception as e:
            message_text = f"更新路由器hosts异常: {e}"
            logger.error(message_text)

        self.__send_message(title=message_title, text=message_text)

    def __create_ssh_connection(self):
        """
        创建SSH连接
        """
        try:
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # 优先使用私钥认证
            if self._private_key_path and len(self._private_key_path.strip()) > 0:
                private_key = paramiko.RSAKey.from_private_key_file(self._private_key_path)
                ssh_client.connect(
                    hostname=self._router_ip,
                    port=self._ssh_port,
                    username=self._username,
                    pkey=private_key,
                    timeout=10
                )
            else:
                # 使用密码认证
                ssh_client.connect(
                    hostname=self._router_ip,
                    port=self._ssh_port,
                    username=self._username,
                    password=self._password,
                    timeout=10
                )
            
            logger.info(f"SSH连接成功: {self._router_ip}:{self._ssh_port}")
            return ssh_client
            
        except Exception as e:
            logger.error(f"SSH连接失败: {e}")
            return None

    def __merge_hosts_with_local(self, local_hosts: list, remote_hosts: list) -> list:
        """
        合并本地hosts和路由器hosts，保留原有hosts条目，只更新或新增本地hosts中的域名
        """
        try:
            ignore = self._ignore.split("|") if self._ignore else []
            ignore.extend(["localhost"])

            # 保留路由器hosts的所有内容，并建立域名映射
            merged_hosts = []
            hostname_to_line_index = {}  # 域名到行索引的映射
            local_updates = {}  # 本地hosts中需要更新的域名映射
            
            # 首先处理远程hosts，保留所有内容
            for line in remote_hosts:
                line_stripped = line.strip()
                merged_hosts.append(line)  # 保留原始格式的行
                
                # 如果是有效的hosts条目，记录域名到行索引的映射
                if not line_stripped.startswith('#') and line_stripped and (" " in line_stripped or "\t" in line_stripped):
                    parts = re.split(r'\s+', line_stripped)
                    if len(parts) > 1:
                        ip, hostname = parts[0], parts[1]
                        if not self.__should_ignore_ip(ip) and hostname not in ignore and ip not in ignore:
                            hostname_to_line_index[hostname] = len(merged_hosts) - 1

            # 处理本地hosts，收集需要更新或新增的条目
            for line in local_hosts:
                line = line.lstrip("\ufeff").strip()
                if line.startswith("#") or any(ign in line for ign in ignore) or not line:
                    continue
                parts = re.split(r'\s+', line)
                if len(parts) < 2:
                    continue
                ip, hostname = parts[0], parts[1]
                if not self.__should_ignore_ip(ip) and hostname not in ignore and ip not in ignore:
                    local_updates[hostname] = line

            # 更新已存在的域名条目
            for hostname, new_line in local_updates.items():
                if hostname in hostname_to_line_index:
                    # 更新现有条目
                    line_index = hostname_to_line_index[hostname]
                    merged_hosts[line_index] = new_line
                    logger.info(f"更新域名 {hostname} 的hosts条目")
                else:
                    # 新增条目
                    merged_hosts.append(new_line)
                    logger.info(f"新增域名 {hostname} 的hosts条目")

            logger.info(f"合并后的hosts共{len(merged_hosts)}行，更新了{len(local_updates)}个域名")
            return merged_hosts
            
        except Exception as e:
            logger.error(f"合并hosts失败: {e}")
            return []

    @staticmethod
    def __get_local_hosts() -> list:
        """
        获取本地hosts文件的内容
        """
        try:
            logger.info("正在获取本地hosts")
            # 确定hosts文件的路径
            if SystemUtils.is_windows():
                hosts_path = r"c:\windows\system32\drivers\etc\hosts"
            else:
                hosts_path = '/etc/hosts'
            with open(hosts_path, "r", encoding="utf-8") as file:
                local_hosts = file.readlines()
            # 去除换行符
            local_hosts = [line.rstrip('\n\r') for line in local_hosts]
            logger.info(f"本地hosts文件读取成功: {len(local_hosts)}行")
            return local_hosts
        except Exception as e:
            logger.error(f"读取本地hosts文件失败: {e}")
            return []

    @staticmethod
    def __should_ignore_ip(ip: str) -> bool:
        """
        检查是否应该忽略给定的IP地址
        """
        try:
            ip_obj = ipaddress.ip_address(ip)
            # 忽略本地回环地址 (127.0.0.0/8) 和所有IPv6地址
            if ip_obj.is_loopback or ip_obj.version == 6:
                return True
        except ValueError:
            pass
        return False

    def __send_message(self, title: str, text: str):
        """
        发送消息
        """
        if not self._notify:
            return

        self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
