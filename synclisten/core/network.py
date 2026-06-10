# -*- coding: utf-8 -*-
"""网络状态管理：在线 / 离线模式切换。

设计要点：
- 不开后台线程。检测只发生在两处：① 启动时一次；② 每次关键联网操作。
- 在线态遵循“用失败做信号”：直接尝试真实云端调用，只有抛异常时才 probe() 确认。
- 离线态遵循“仅操作时检测”：每次操作前 probe() 一次，网络恢复即切回在线。
- probe 探测真实 API 域名的 TCP 可达性，而非泛化 ping。
"""

import socket

from ..config import NETWORK_PROBE_HOSTS, NETWORK_PROBE_TIMEOUT


class NetworkManager:
    """管理在线/离线模式的状态机。"""

    ONLINE = "online"
    OFFLINE = "offline"

    def __init__(self, mode=ONLINE):
        self.mode = mode

    def is_online(self):
        return self.mode == self.ONLINE

    def probe(self, timeout=None):
        """探测网络：任一目标 API 域名 TCP 可达即返回 True。"""
        timeout = NETWORK_PROBE_TIMEOUT if timeout is None else timeout
        for host, port in NETWORK_PROBE_HOSTS:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    return True
            except OSError:
                continue
        return False

    def go_offline(self):
        """切到离线，返回是否发生了状态变化。"""
        changed = self.mode != self.OFFLINE
        self.mode = self.OFFLINE
        return changed

    def go_online(self):
        """切到在线，返回是否发生了状态变化。"""
        changed = self.mode != self.ONLINE
        self.mode = self.ONLINE
        return changed

    def refresh(self):
        """探测并更新模式，返回 (online: bool, changed: bool)。"""
        online = self.probe()
        changed = self.go_online() if online else self.go_offline()
        return online, changed
